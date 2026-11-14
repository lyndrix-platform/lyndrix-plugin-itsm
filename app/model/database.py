"""Database helpers for the ITSM plugin.

Cluster notes (core API ≥ 1.7): N core instances share one MariaDB. Bootstrap
DDL runs under ``db_instance.advisory_lock("lyndrix_bootstrap")`` and treats
"already exists" errors as "a peer did it". MariaDB ≥ 11.6 runs
``innodb_snapshot_isolation=ON``: a REPEATABLE READ transaction that updates a
row another transaction changed since its first read fails with error 1020
instead of waiting — so sessions run READ COMMITTED and the short write
transactions are retried on 1020/1205/1213 (see ``retry_write``).
"""
from __future__ import annotations

import re
import time
from contextlib import contextmanager, nullcontext
from typing import Callable, Generator, TypeVar

from sqlalchemy import String, cast, func, inspect, select, text
from sqlalchemy.orm import Session

from core.api import db_instance, get_logger

from .models import Record, RecordTag, _Base, tag_key

log = get_logger("Plugin:ITSM:DB")

T = TypeVar("T")

# MariaDB error codes.
RETRYABLE_CODES = frozenset({1020, 1205, 1213})  # snapshot conflict, lock wait timeout, deadlock
_EXISTS_CODES = frozenset({1050, 1060, 1061})    # table / column / index already exists
_EXISTS_TEXT = ("already exists", "duplicate column")

# Columns added after a table's first release, keyed by table — create_all()
# never alters existing tables, so this is the hand-rolled migration path:
# {"itsm_records": {"new_col": "VARCHAR(64)"}}.
_NEW_COLUMNS: dict[str, dict[str, str]] = {}

# Indexes added after a table's first release (create_all() never adds them to
# an existing table): {"table": {"index_name": "col_a, col_b"}}.
_NEW_INDEXES: dict[str, dict[str, str]] = {
    "itsm_records": {"ix_itsm_records_closed_opened": "closed_at, opened_at"},
}
_BACKFILL_CHUNK = 500


# ── Error classification ──────────────────────────────────────────────────────

def db_error_code(exc: BaseException) -> int | None:
    """The MySQL/MariaDB error number behind a (SQLAlchemy-wrapped) DB error."""
    orig = getattr(exc, "orig", None) or exc
    errno = getattr(orig, "errno", None)
    if isinstance(errno, int):
        return errno
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    match = re.match(r"\(?(\d{4})", str(orig))
    return int(match.group(1)) if match else None


def is_retryable(exc: BaseException) -> bool:
    return db_error_code(exc) in RETRYABLE_CODES


def is_already_exists(exc: BaseException) -> bool:
    if db_error_code(exc) in _EXISTS_CODES:
        return True
    msg = str(getattr(exc, "orig", None) or exc).lower()
    return any(t in msg for t in _EXISTS_TEXT)


def retry_write(fn: Callable[[], T], *, attempts: int = 3, pause: float = 0.05) -> T:
    """Run ``fn()`` — one complete short transaction — again on 1020/1205/1213."""
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt + 1 < attempts and is_retryable(exc):
                log.debug(f"DB: retrying write after {db_error_code(exc)} (attempt {attempt + 1})")
                time.sleep(pause * (attempt + 1))
                continue
            raise
    raise AssertionError("unreachable")  # pragma: no cover


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def _bootstrap_lock():
    lock = getattr(db_instance, "advisory_lock", None)  # core API ≥ 1.7
    return lock("lyndrix_bootstrap") if callable(lock) else nullcontext()


def ensure_tables() -> None:
    """Create ITSM tables and apply column migrations.

    Idempotent and safe when several instances boot at once: serialized by the
    core bootstrap lock, and a lost race ("table/column/index already exists")
    counts as success.
    """
    if not db_instance.is_connected or not db_instance.engine:
        raise RuntimeError("Database engine not ready — call after db:connected event")
    with _bootstrap_lock():
        try:
            _Base.metadata.create_all(db_instance.engine, checkfirst=True)
        except Exception as exc:
            if not is_already_exists(exc):
                raise
            log.info("DB: a peer created ITSM tables concurrently — re-checking")
            _Base.metadata.create_all(db_instance.engine, checkfirst=True)
        _migrate_columns()
        _migrate_indexes()
        _backfill_record_tags()
    log.info("DB: itsm tables ready")


def _migrate_columns() -> None:
    engine = db_instance.engine
    mariadb = engine.dialect.name in ("mysql", "mariadb")
    insp = inspect(engine)
    for table, new_cols in _NEW_COLUMNS.items():
        existing = {col["name"] for col in insp.get_columns(table)}
        for col, col_type in new_cols.items():
            if col in existing:
                continue
            nullability = "" if "NOT NULL" in col_type else " NULL"
            if_not = " IF NOT EXISTS" if mariadb else ""
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        f"ALTER TABLE {table} ADD COLUMN{if_not} {col} {col_type}{nullability}"
                    ))
                log.info(f"DB: migrated — added column '{col}' to {table}")
            except Exception as exc:
                if not is_already_exists(exc):
                    raise


def _migrate_indexes() -> None:
    engine = db_instance.engine
    insp = inspect(engine)
    for table, new_indexes in _NEW_INDEXES.items():
        existing = {ix["name"] for ix in insp.get_indexes(table)}
        for name, columns in new_indexes.items():
            if name in existing:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"CREATE INDEX {name} ON {table} ({columns})"))
                log.info(f"DB: migrated — added index '{name}' to {table}")
            except Exception as exc:
                if not is_already_exists(exc):
                    raise


def _backfill_record_tags() -> None:
    """Mirror ``Record.tags`` of rows written before 0.3.1 (or by an older
    instance during a rolling upgrade) into ``itsm_record_tags``.

    Only records without any mirror row are read, in committed chunks that
    stay far below the statement cap; idempotent, so every boot may run it.
    """
    engine = db_instance.engine
    mirrored = select(RecordTag.record_uuid)
    last = ""
    total = 0
    while True:
        with engine.begin() as conn:
            rows = conn.execute(
                select(Record.uuid, Record.tags)
                # "[]" is 2 chars: untagged records are skipped by the server
                .where(Record.uuid > last, func.length(cast(Record.tags, String)) > 2,
                       Record.uuid.not_in(mirrored))
                .order_by(Record.uuid).limit(_BACKFILL_CHUNK)
            ).all()
            if not rows:
                break
            last = rows[-1][0]
            values = [
                {"record_uuid": uuid, "tag": key}
                for uuid, tags in rows
                for key in sorted({tag_key(t) for t in (tags or []) if isinstance(t, str) and t})
            ]
            if values:
                try:
                    conn.execute(RecordTag.__table__.insert(), values)
                    total += len(values)
                except Exception as exc:  # a peer without the lock (old core) raced us
                    if not _is_duplicate(exc):
                        raise
    if total:
        log.info(f"DB: backfilled {total} record tag(s) into itsm_record_tags")


def _is_duplicate(exc: BaseException) -> bool:
    return db_error_code(exc) == 1062 or "unique constraint" in str(getattr(exc, "orig", exc)).lower()


# ── Sessions ──────────────────────────────────────────────────────────────────

@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a READ COMMITTED session (no-op on SQLite), rolling back on exception."""
    if not db_instance.is_connected or not db_instance.SessionLocal:
        raise RuntimeError("Database not connected")
    session: Session = db_instance.SessionLocal()
    try:
        bind = session.get_bind()
        if bind is not None and getattr(bind.dialect, "name", "") in ("mysql", "mariadb"):
            session.connection(execution_options={"isolation_level": "READ COMMITTED"})
    except Exception as exc:  # isolation is an optimisation, never a reason to fail
        log.debug(f"DB: READ COMMITTED not applied: {exc}")
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
