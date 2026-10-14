"""ITSM service tests — real service against in-memory SQLite + shipped catalog."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


class _FakeCtx:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, topic, payload):
        self.events.append((topic, payload))

    def topics(self) -> list[str]:
        return [t for t, _ in self.events]


@pytest.fixture()
def svc(monkeypatch, tmp_path):
    from app.logic.service import ItsmService
    from app.model import database

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    # Enforce FKs like MariaDB does, so flush-order bugs fail here too.
    event.listen(engine, "connect", lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"))
    monkeypatch.setattr(
        database, "db_instance",
        SimpleNamespace(engine=engine, SessionLocal=sessionmaker(bind=engine), is_connected=True),
    )
    service = ItsmService()
    service.attachment_dir = tmp_path / "att"
    service.set_context(_FakeCtx())
    service.catalog.load()
    service.on_db_connected()
    return service


def _inc(svc, **extra):
    return svc.create_record({"record_type": "incident", "title": "DNS down", **extra}, author="marvin")


# ── catalog ────────────────────────────────────────────────────────────────────

def test_shipped_catalog_loads_all_types(svc):
    assert set(svc.catalog.types()) == {"incident", "change", "problem", "request", "task"}
    inc = svc.catalog.record_type("incident")
    assert inc.initial_state().id == "new"
    assert inc.derive_priority(1, 1) == "P1" and inc.derive_priority(3, 3) == "P4"


def test_catalog_rejects_broken_definition(tmp_path):
    from app.logic.catalog import CatalogError, CatalogLoader

    d = tmp_path / "record_types"
    d.mkdir()
    (d / "x.yml").write_text(
        "id: x\nlabel: X\nprefix: XX\nstates: [{id: a, label: A, initial: true}]\n"
        "transitions: {a: [nowhere]}\n"
    )
    with pytest.raises(CatalogError, match="unknown state"):
        CatalogLoader(tmp_path).load()


# ── create ─────────────────────────────────────────────────────────────────────

def test_create_numbers_defaults_and_journal(svc):
    a = _inc(svc)
    b = _inc(svc, title="second")
    assert a["number"] == "INC0000001" and b["number"] == "INC0000002"
    assert a["state"] == "new" and a["version"] == 1
    assert (a["impact"], a["urgency"], a["priority"]) == (3, 3, "P4")
    assert a["created_by"] == "marvin" and a["requester"] == "marvin"
    assert [j["kind"] for j in a["journal"]] == ["system"]
    assert a["allowed_transitions"] == ["in_progress", "resolved", "cancelled"]
    topics = svc._ctx.topics()
    assert topics[:2] == ["itsm:record_created", "entity_registry:register"]
    reg = svc._ctx.events[1][1]
    assert reg["uuid"] == a["uuid"] and reg["identifiers"] == {"itsm.number": "INC0000001"}


def test_prefix_per_type_and_priority_matrix(svc):
    chg = svc.create_record({"record_type": "change", "title": "Upgrade", "subtype": "normal",
                             "impact": 1, "urgency": 1}, author="m")
    assert chg["number"] == "CHG0000001" and chg["priority"] == "P1"
    task = svc.create_record({"record_type": "task", "title": "do it"}, author="m")
    assert task["number"] == "TSK0000001"


def test_create_validates_against_catalog(svc):
    from app.logic.service import ItsmError

    with pytest.raises(ItsmError, match="title"):
        svc.create_record({"record_type": "incident"})
    with pytest.raises(ItsmError, match="subtype"):
        _inc(svc, subtype="meteor")
    with pytest.raises(ItsmError, match="category"):
        _inc(svc, category="weather")
    with pytest.raises(ItsmError, match="custom field"):
        _inc(svc, custom_fields={"nope": 1})
    with pytest.raises(ItsmError, match="impact"):
        _inc(svc, impact=9)
    with pytest.raises(Exception, match="record type"):
        svc.create_record({"record_type": "spaceship", "title": "x"})


def test_create_with_ci_links_and_custom_fields(svc):
    ci = "11111111-2222-3333-4444-555555555555"
    rec = _inc(svc, category="dns", custom_fields={"affected_users": "40", "security_relevant": "no"},
               ci_links=[{"entity_uuid": ci, "role": "affected", "display_name": "docker-atlas"}],
               tags=["prod", "dns", "prod"])
    assert rec["custom_fields"] == {"affected_users": 40, "security_relevant": False}
    assert rec["ci_links"][0]["display_name"] == "docker-atlas"
    assert rec["tags"] == ["dns", "prod"]
    assert svc.list_records(ci=ci)["total"] == 1


# ── transitions ────────────────────────────────────────────────────────────────

def test_illegal_transition_and_required_fields(svc):
    from app.logic.service import ItsmError, TransitionError

    rec = _inc(svc)
    with pytest.raises(TransitionError, match="not allowed"):
        svc.transition(rec["uuid"], "closed", author="m")
    with pytest.raises(ItsmError, match="requires: resolution_code, resolution_notes"):
        svc.transition(rec["uuid"], "resolved", author="m")
    with pytest.raises(ItsmError, match="Unknown state"):
        svc.transition(rec["uuid"], "vanished", author="m")


def test_resolve_close_reopen_lifecycle(svc):
    rec = _inc(svc)
    res = svc.transition(rec["uuid"], "resolved", author="m", note="fixed the zone",
                         fields={"resolution_code": "fixed", "resolution_notes": "reloaded pdns"})
    assert res["state"] == "resolved" and res["resolved_at"] and res["closed_at"] is None
    assert res["version"] == 2
    assert res["custom_fields"]["resolution_code"] == "fixed"
    sc = res["journal"][-1]
    assert sc["kind"] == "state_change" and sc["body"] == "fixed the zone"
    assert sc["data"]["from"] == "new" and sc["data"]["to"] == "resolved"
    assert svc._ctx.events[-1][0] == "itsm:state_changed"

    closed = svc.transition(rec["uuid"], "closed", author="m")
    assert closed["closed_at"] and closed["allowed_transitions"] == []
    assert svc.list_records()["total"] == 0  # closed hidden by default
    assert svc.list_records(include_closed=True)["total"] == 1

    # Reopen from resolved clears timestamps.
    rec2 = _inc(svc, title="again")
    svc.transition(rec2["uuid"], "resolved", fields={"resolution_code": "fixed", "resolution_notes": "x"})
    back = svc.transition(rec2["uuid"], "in_progress", author="m")
    assert back["resolved_at"] is None and back["state"] == "in_progress"


def test_transition_can_satisfy_requirement_with_core_field(svc):
    """A state may require a core field (e.g. assignee) — settable in the same call."""
    from app.logic.catalog import CatalogLoader

    # Build a tiny catalog where in_progress requires an assignee.
    import yaml
    src = yaml.safe_load((Path(svc.catalog.catalog_dir) / "record_types" / "task.yml").read_text())
    src["states"][1]["requires"] = ["assignee"]
    custom_dir = Path(svc.attachment_dir).parent / "cat"
    (custom_dir / "record_types").mkdir(parents=True)
    (custom_dir / "record_types" / "task.yml").write_text(yaml.safe_dump(src))
    svc.catalog = CatalogLoader(custom_dir)
    svc.catalog.load()

    from app.logic.service import ItsmError
    t = svc.create_record({"record_type": "task", "title": "x"})
    with pytest.raises(ItsmError, match="requires: assignee"):
        svc.transition(t["uuid"], "in_progress")
    done = svc.transition(t["uuid"], "in_progress", fields={"assignee": "marvin"})
    assert done["assignee"] == "marvin" and done["state"] == "in_progress"


# ── update / optimistic lock ───────────────────────────────────────────────────

def test_update_versions_journal_and_priority(svc):
    from app.logic.service import ItsmError, VersionConflict

    rec = _inc(svc)
    with pytest.raises(VersionConflict):
        svc.update_record(rec["uuid"], {"title": "x"}, expected_version=99)
    with pytest.raises(ItsmError, match="transition"):
        svc.update_record(rec["uuid"], {"state": "closed"})
    with pytest.raises(ItsmError, match="not updatable"):
        svc.update_record(rec["uuid"], {"number": "INC1"})

    upd = svc.update_record(rec["uuid"], {"impact": 1, "urgency": 1, "assignee": "marvin",
                                          "custom_fields": {"affected_users": 5}},
                            author="m", expected_version=1)
    assert upd["version"] == 2 and upd["priority"] == "P1"
    fc = upd["journal"][-1]
    assert fc["kind"] == "field_change"
    assert set(fc["data"]["changes"]) == {"impact", "urgency", "assignee", "priority", "custom_fields.affected_users"}
    assert fc["data"]["changes"]["priority"] == {"old": "P4", "new": "P1"}
    topic, payload = svc._ctx.events[-1]
    assert topic == "itsm:record_updated" and "priority" in payload["changes"]

    # No-op update: version unchanged, no journal, no event.
    n_events = len(svc._ctx.events)
    same = svc.update_record(rec["uuid"], {"assignee": "marvin"}, expected_version=2)
    assert same["version"] == 2 and len(svc._ctx.events) == n_events


# ── journal / links / attachments ─────────────────────────────────────────────

def test_journal_comments(svc):
    from app.logic.service import ItsmError

    rec = _inc(svc)
    c = svc.add_journal(rec["uuid"], "comment", "User called back", author="m")
    w = svc.add_journal(rec["number"], "work_note", "checked pdns logs", author="m")  # by number
    assert c["kind"] == "comment" and w["kind"] == "work_note"
    assert [j["kind"] for j in svc.get_record(rec["uuid"])["journal"]] == ["system", "comment", "work_note"]
    with pytest.raises(ItsmError):
        svc.add_journal(rec["uuid"], "state_change", "nope")
    with pytest.raises(ItsmError):
        svc.add_journal(rec["uuid"], "comment", "   ")
    assert svc._ctx.events[-1][0] == "itsm:journal_added"


def test_record_links_with_inverse_view(svc):
    from app.logic.service import ItsmError, NotFound

    inc = _inc(svc)
    prb = svc.create_record({"record_type": "problem", "title": "flaky dns"})
    svc.link_records(inc["uuid"], prb["uuid"], "caused_by", author="m")
    svc.link_records(inc["uuid"], prb["uuid"], "caused_by")  # idempotent
    svc.link_records(prb["number"], inc["number"], "causes")  # inverse of an existing link → same
    inc_d, prb_d = svc.get_record(inc["uuid"]), svc.get_record(prb["uuid"])
    assert len(inc_d["links"]) == 1 and len(prb_d["links"]) == 1
    assert inc_d["links"][0]["relation_from_here"] == "caused_by"
    assert prb_d["links"][0]["relation_from_here"] == "causes"
    assert prb_d["links"][0]["other"]["number"] == "INC0000001"
    with pytest.raises(ItsmError):
        svc.link_records(inc["uuid"], inc["uuid"], "related_to")
    with pytest.raises(ItsmError):
        svc.link_records(inc["uuid"], prb["uuid"], "teleports")
    svc.unlink_records(prb["uuid"], inc["uuid"], "causes")  # remove via the inverse side
    assert svc.get_record(inc["uuid"])["links"] == []
    with pytest.raises(NotFound):
        svc.unlink_records(inc["uuid"], prb["uuid"], "caused_by")


def test_ci_links_add_remove(svc):
    from app.logic.service import ItsmError, NotFound

    rec = _inc(svc)
    ci = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    d = svc.link_ci(rec["uuid"], ci, "affected", display_name="docker-atlas", entity_type="host", author="m")
    assert d["ci_links"][0]["display_name"] == "docker-atlas"
    svc.link_ci(rec["uuid"], ci, "affected")  # idempotent
    assert len(svc.get_record(rec["uuid"])["ci_links"]) == 1
    with pytest.raises(ItsmError):
        svc.link_ci(rec["uuid"], ci, "owns")
    d = svc.unlink_ci(rec["uuid"], ci, "affected", author="m")
    assert d["ci_links"] == []
    assert [j["kind"] for j in d["journal"]].count("link") == 2
    with pytest.raises(NotFound):
        svc.unlink_ci(rec["uuid"], ci, "affected")


def test_attachments_roundtrip_and_limits(svc):
    from app.logic.service import ItsmError, NotFound

    rec = _inc(svc)
    meta = svc.add_attachment(rec["uuid"], "../../evil log.txt", "text/plain; charset=utf-8",
                              b"hello", author="m")
    assert meta["filename"] == "evil log.txt" and meta["content_type"] == "text/plain"
    assert meta["size"] == 5 and len(meta["sha256"]) == 64
    got, path = svc.get_attachment(rec["uuid"], meta["uuid"])
    assert path.read_bytes() == b"hello" and path.parent.name == rec["uuid"]
    assert svc.get_record(rec["uuid"])["attachments"][0]["uuid"] == meta["uuid"]

    svc.max_attachment_bytes = 3
    with pytest.raises(ItsmError, match="limit"):
        svc.add_attachment(rec["uuid"], "big.txt", "text/plain", b"toolong")
    with pytest.raises(ItsmError, match="not allowed"):
        svc.add_attachment(rec["uuid"], "x.exe", "application/x-msdownload", b"x")
    with pytest.raises(ItsmError, match="Empty"):
        svc.add_attachment(rec["uuid"], "x.txt", "text/plain", b"")

    assert svc.delete_attachment(rec["uuid"], meta["uuid"], author="m") is True
    assert not path.exists()
    with pytest.raises(NotFound):
        svc.get_attachment(rec["uuid"], meta["uuid"])


# ── list / stats / ingest key ─────────────────────────────────────────────────

def test_list_filters_and_stats(svc):
    a = _inc(svc, impact=1, urgency=1, tags=["prod"])
    _inc(svc, title="minor", tags=["lab"])
    svc.create_record({"record_type": "change", "title": "patch"})
    svc.transition(a["uuid"], "in_progress", fields={"assignee": "marvin"})
    assert svc.list_records(record_type="incident")["total"] == 2
    assert svc.list_records(q="INC0000002")["total"] == 1
    assert svc.list_records(q="patch")["records"][0]["record_type"] == "change"
    assert svc.list_records(tag="prod")["total"] == 1
    assert svc.list_records(priority="P1")["total"] == 1
    assert svc.list_records(state="in_progress")["total"] == 1
    stats = svc.get_stats()
    assert stats["open"] == 3 and stats["open_by_type"] == {"incident": 2, "change": 1}
    assert stats["open_by_priority"]["P1"] == 1


def test_external_source_key_is_unique(svc):
    from app.logic.service import ItsmError

    _inc(svc, source="bmc_helix", external_id="INC000000123")
    with pytest.raises(ItsmError, match="already exists"):
        _inc(svc, source="bmc_helix", external_id="INC000000123")
    with pytest.raises(ItsmError, match="together"):
        _inc(svc, source="bmc_helix")


def test_get_by_number_and_unknown(svc):
    rec = _inc(svc)
    assert svc.get_record("INC0000001")["uuid"] == rec["uuid"]
    assert svc.get_record("INC9999999") is None
