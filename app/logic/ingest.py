"""Bus-event ingest — the shared shape of handlers that turn foreign events
into records.

Other plugins report facts on the bus (the monitoring plugin an outage, the
IaC orchestrator a deploy without a maintenance window); ITSM turns them into
records. Every such handler does the same things, so they live here once:

* validate the payload — a missing or malformed field is reported on
  ``itsm:ingest_failed`` (never raised into the bus),
* deduplicate by the emitter's deterministic ``(source, external_id)`` key —
  the DB ``UNIQUE`` constraint makes that cluster-safe — and by "an open record
  of this source already links the configuration item",
* link the configuration items through the Entity Registry when it answers
  and fall back to raw UUID links when it does not (an alarm path must never
  fail because the registry is down or does not know the CI yet),
* create the record as a system author; a lost race (a peer instance inserted
  the same key first) counts as success and the winner's record is used.

Handlers are idempotent, so the transport rule of the maintenance-windows
contract (events emitted locally, consumers persist in the shared DB) and a
later switch to cluster-wide emission both work without changes here.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from core.api import get_logger

from .service import DuplicateRecord, ItsmError, ItsmService
from .texts import Texts

log = get_logger("Plugin:ITSM:Ingest")

CI_ID_LENGTH = 36
#: How long a handler waits for the tables when an event arrives during boot
#: (monitoring may report an outage before ``db:connected`` reached us).
READY_WAIT_SECONDS = 30.0
_READY_POLL_SECONDS = 0.5


class IngestError(ValueError):
    """The payload cannot be processed — reported on ``itsm:ingest_failed``."""


# ── Payload helpers ──────────────────────────────────────────────────────────


def parse_iso(value: object) -> datetime | None:
    """ISO-8601 timestamp (``Z`` accepted, naive = UTC) → aware UTC datetime, or None."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Aware datetime → ``YYYY-MM-DDTHH:MM:SSZ`` (what the bus carries)."""
    return (
        dt.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def fmt_ts(value: object) -> str:
    """Human timestamp for generated texts (``2026-10-18 01:23 UTC``); the raw
    value when unparsable, an em dash when missing."""
    dt = parse_iso(value)
    if dt is not None:
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    return str(value) if value not in (None, "") else "—"


def fmt_duration(seconds: object) -> str:
    """``5400`` → ``1 h 30 min``; unknown → ``?``."""
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "?"
    if total < 60:
        return f"{total} s"
    minutes, _ = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days} d {hours:02d} h"
    if hours:
        return f"{hours} h {minutes:02d} min"
    return f"{minutes} min"


def is_ci_id(value: object) -> bool:
    return isinstance(value, str) and len(value) == CI_ID_LENGTH


# ── Base handler ─────────────────────────────────────────────────────────────


class EventIngest:
    """Base of the bus-event handlers; subclasses set the class attributes and
    implement one ``async def _<event>(self, data: dict) -> str | None`` per
    topic, wrapped by :meth:`_guarded`.

    All DB work goes through :class:`ItsmService` in ``asyncio.to_thread`` so
    the event loop never blocks.
    """

    source: str = ""  # Record.source — the dedupe namespace
    author: str = ""  # journal author / created_by
    record_type: str = ""

    def __init__(self, service: ItsmService, texts: Texts | None = None) -> None:
        self.service = service
        self.texts = texts or service.texts

    # ── Guard rails ────────────────────────────────────────────────────────

    async def _guarded(
        self,
        topic: str,
        handler: Callable[[dict], Awaitable[str | None]],
        payload: object,
    ) -> str | None:
        """Run ``handler`` for one event; never raises. Returns the uuid of the
        record created or touched, or None when nothing was done."""
        data = payload if isinstance(payload, dict) else {}
        try:
            if not await self._wait_ready():
                raise IngestError("database not ready")
            return await handler(data)
        except IngestError as exc:
            self.service.report_ingest_failure(topic, str(exc))
        except Exception as exc:
            log.error(f"ITSM: {topic} failed — {exc}")
            self.service.report_ingest_failure(topic, f"internal error: {exc}")
        return None

    async def _wait_ready(self, timeout: float | None = None) -> bool:
        """Tables may still be bootstrapping when the first event of a boot
        arrives — wait a little instead of dropping it."""
        if timeout is None:
            timeout = READY_WAIT_SECONDS
        deadline = asyncio.get_running_loop().time() + timeout
        while not self.service.is_ready:
            if asyncio.get_running_loop().time() >= deadline:
                return False
            await asyncio.sleep(_READY_POLL_SECONDS)
        return True

    # ── Payload access ─────────────────────────────────────────────────────

    @staticmethod
    def _text(data: dict, key: str) -> str:
        value = data.get(key)
        if value is None or str(value).strip() == "":
            raise IngestError(f"{key} is required")
        return str(value).strip()

    @staticmethod
    def _ci(data: dict, key: str, *, required: bool = True) -> str | None:
        value = data.get(key)
        if value in (None, ""):
            if required:
                raise IngestError(f"{key} is required")
            return None
        if not is_ci_id(value):
            raise IngestError(
                f"{key} must be a {CI_ID_LENGTH}-char Entity Registry UUID"
            )
        return str(value)

    # ── Service round-trips ────────────────────────────────────────────────

    async def _find_by_external(self, external_id: str) -> dict | None:
        return await asyncio.to_thread(
            self.service.find_by_external, self.source, external_id
        )

    async def _find_open_for_ci(self, ci_id: str) -> dict | None:
        return await asyncio.to_thread(
            self.service.find_open_for_ci,
            ci_id,
            record_type=self.record_type,
            source=self.source,
        )

    async def _journal(self, ref: str, kind: str, body: str) -> dict:
        return await asyncio.to_thread(
            self.service.add_journal, ref, kind, body, author=self.author
        )

    async def _ci_links(
        self, ci_id: str, host_ci_id: str | None, *, kind: str | None, name: str | None
    ) -> list[dict]:
        """``affected`` link for the CI, ``related`` for its host — names and
        types from the registry when it answers, raw UUIDs otherwise."""
        wanted = [
            {
                "entity_uuid": ci_id,
                "role": "affected",
                "display_name": name,
                "entity_type": kind,
            }
        ]
        if host_ci_id and host_ci_id != ci_id:
            wanted.append({"entity_uuid": host_ci_id, "role": "related"})
        links: list[dict] = []
        for raw in wanted:
            try:
                links.extend(await self.service.resolve_ci_links([raw]))
            except ItsmError as exc:
                # The registry answered but does not know this CI (yet). The
                # event is still real — link the UUID and let the registry catch up.
                log.warning(
                    f"ITSM: CI {raw['entity_uuid']} not resolvable ({exc}) — linking the raw UUID"
                )
                links.append(raw)
        return links

    async def _create(self, external_id: str, data: dict) -> dict:
        """Create the record under ``(source, external_id)``; when a peer
        instance inserted the same key first, that record is the result."""

        def _tx() -> dict:
            body = {
                **data,
                "record_type": self.record_type,
                "source": self.source,
                "external_id": external_id,
            }
            try:
                return self.service.create_record(body, author=self.author)
            except DuplicateRecord:
                winner = self.service.find_by_external(self.source, external_id)
                if (
                    winner is None
                ):  # pragma: no cover - vanished between the two statements
                    raise
                log.info(
                    f"ITSM: {self.source}/{external_id} was created concurrently — using {winner['number']}"
                )
                return winner

        return await asyncio.to_thread(_tx)

    def _is_open(self, record: dict) -> bool:
        return record.get("state") in self.service.open_state_names(self.record_type)


def plus_days(start: datetime | None, days: int) -> datetime:
    return (start or datetime.now(timezone.utc)) + timedelta(days=days)
