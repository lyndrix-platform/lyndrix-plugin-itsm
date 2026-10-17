"""Test bootstrap.

Importing ``core.api`` cold trips a circular import inside lyndrix-core
(core.api → plugins → ui.layout → notifications → core.api). The running
platform never hits it because main.py loads core.services first; mirror that
order here before any plugin module pulls in core.api.
"""

# TODO(agent): fix the core.api ↔ ui.layout cycle in lyndrix-core and drop this.
import core.services  # noqa: F401

from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402
from fakes import FakeCtx  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    """A real ItsmService on in-memory SQLite with the shipped catalog."""
    from app.logic.service import ItsmService
    from app.model import database

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    # Enforce FKs like MariaDB does, so flush-order bugs fail here too.
    event.listen(
        engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON")
    )
    monkeypatch.setattr(
        database,
        "db_instance",
        SimpleNamespace(
            engine=engine, SessionLocal=sessionmaker(bind=engine), is_connected=True
        ),
    )
    service = ItsmService()
    service.attachment_dir = tmp_path / "att"
    service.set_context(FakeCtx())
    service.catalog.load()
    service.on_db_connected()
    return service
