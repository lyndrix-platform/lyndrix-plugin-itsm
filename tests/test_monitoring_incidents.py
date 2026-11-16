"""monitoring:outage_persisted / outage_recovered → incidents (plan-itsm §6)."""

from __future__ import annotations

import asyncio
import json

import pytest
from fakes import HYDRA, registry

SERVICE_CI = "5e1f0a3c-7b2d-4e8f-9a01-23456789abcd"
OTHER_CI = "7a7a7a7a-1111-2222-3333-444444444444"
SINCE = "2026-10-18T01:00:00Z"
SINCE_2 = "2026-10-18T05:00:00Z"
NAME = "aac-grafana @ docker-hydra"
RULE = {
    "enabled": True,
    "offline_minutes": 10,
    "severity": "high",
    "auto_resolve": True,
}


def outage_id(ci: str = SERVICE_CI, since: str = SINCE) -> str:
    return f"{ci}:{since}"


def persisted(**over) -> dict:
    data = {
        "outage_id": outage_id(),
        "ci_id": SERVICE_CI,
        "kind": "service",
        "name": NAME,
        "host_ci_id": HYDRA,
        "monitor_id": 42,
        "since": SINCE,
        "persisted_at": "2026-10-18T01:10:00Z",
        "duration_s": 600,
        "rule": dict(RULE),
        "error": "connection refused",
    }
    data.update(over)
    return data


def recovered(**over) -> dict:
    data = {
        "outage_id": outage_id(),
        "ci_id": SERVICE_CI,
        "since": SINCE,
        "recovered_at": "2026-10-18T02:30:00Z",
        "duration_s": 5400,
    }
    data.update(over)
    return data


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def incidents(svc):
    from app.logic.monitoring_incidents import MonitoringIncidents

    ctx = registry(svc)
    ctx.known[SERVICE_CI] = {
        "uuid": SERVICE_CI,
        "entity_type": "service",
        "display_name": NAME,
    }
    return MonitoringIncidents(svc)


def _incidents(svc) -> list[dict]:
    return svc.list_records(record_type="incident", include_closed=True)["records"]


def _failures(svc) -> list[dict]:
    return svc._ctx.payloads("itsm:ingest_failed")


# ── outage_persisted ──────────────────────────────────────────────────────────


def test_persisted_opens_incident_with_priority_links_and_fields(incidents, svc):
    uuid = run(incidents.handle_outage_persisted(persisted()))
    rec = svc.get_record(uuid)
    assert (
        rec["record_type"] == "incident"
        and rec["subtype"] == "outage"
        and rec["category"] == "monitoring"
    )
    assert (rec["impact"], rec["urgency"], rec["priority"]) == (1, 2, "P2")
    assert rec["source"] == "monitoring" and rec["external_id"] == outage_id()
    assert rec["created_by"] == "monitoring" and rec["requester"] == "monitoring"
    assert rec["tags"] == ["auto", "monitoring", "service"]
    assert rec["custom_fields"] == {
        "monitor_rule": json.dumps(RULE, sort_keys=True),
        "outage_id": outage_id(),
        "ci_kind": "service",
    }
    assert NAME in rec["title"] and "2026-10-18 01:00 UTC" in rec["title"]
    assert (
        "connection refused" in rec["description"]
        and "docker-hydra" in rec["description"]
    )
    links = {
        (c["entity_uuid"], c["role"], c["display_name"], c["entity_type"])
        for c in rec["ci_links"]
    }
    assert links == {
        (SERVICE_CI, "affected", NAME, "service"),
        (HYDRA, "related", "docker-hydra", "host"),
    }
    assert "itsm:record_created" in svc._ctx.topics() and not _failures(svc)


def test_persisted_delivered_twice_is_one_record(incidents, svc):
    first = run(incidents.handle_outage_persisted(persisted()))
    second = run(incidents.handle_outage_persisted(persisted()))
    assert first == second and len(_incidents(svc)) == 1
    assert len(svc.get_record(first)["journal"]) == 1  # no extra note for the replay


def test_second_outage_on_open_incident_adds_work_note(incidents, svc):
    first = run(incidents.handle_outage_persisted(persisted()))
    again = run(
        incidents.handle_outage_persisted(
            persisted(outage_id=outage_id(since=SINCE_2), since=SINCE_2)
        )
    )
    assert again == first and len(_incidents(svc)) == 1
    rec = svc.get_record(first)
    note = rec["journal"][-1]
    assert (
        note["kind"] == "work_note"
        and note["author"] == "monitoring"
        and outage_id(since=SINCE_2) in note["body"]
    )
    assert (
        rec["custom_fields"]["outage_id"] == outage_id()
    )  # first outage stays the reference


@pytest.mark.parametrize(
    "severity, expected",
    [
        ("critical", (1, 1, "P1")),
        ("high", (1, 2, "P2")),
        ("medium", (2, 2, "P3")),
        ("low", (3, 2, "P4")),
        ("nuclear", (1, 2, "P2")),
    ],
)
def test_severity_maps_to_priority_unknown_is_high(incidents, svc, severity, expected):
    uuid = run(
        incidents.handle_outage_persisted(
            persisted(rule={**RULE, "severity": severity})
        )
    )
    rec = svc.get_record(uuid)
    assert (rec["impact"], rec["urgency"], rec["priority"]) == expected


def test_invalid_payloads_report_ingest_failed(incidents, svc):
    assert (
        run(incidents.handle_outage_persisted(persisted(ci_id="docker-hydra"))) is None
    )
    assert run(incidents.handle_outage_persisted(persisted(rule=None))) is None
    assert run(incidents.handle_outage_persisted({"ci_id": SERVICE_CI})) is None
    assert run(incidents.handle_outage_persisted("garbage")) is None
    errors = [f["error"] for f in _failures(svc)]
    assert len(errors) == 4 and all(
        f["topic"] == "monitoring:outage_persisted" for f in _failures(svc)
    )
    assert "ci_id" in errors[0] and "rule" in errors[1] and "outage_id" in errors[2]
    assert _incidents(svc) == []


def test_registry_unavailable_links_raw_uuids(svc):
    from app.logic.monitoring_incidents import MonitoringIncidents

    registry(svc, available=False)
    uuid = run(MonitoringIncidents(svc).handle_outage_persisted(persisted()))
    links = {
        (c["entity_uuid"], c["role"], c["display_name"], c["entity_type"])
        for c in svc.get_record(uuid)["ci_links"]
    }
    assert links == {
        (SERVICE_CI, "affected", NAME, "service"),
        (HYDRA, "related", None, None),
    }


def test_registry_not_knowing_the_ci_still_opens_the_incident(svc):
    """The registry answers 'unknown' (e.g. the orchestrator has not identified
    the service yet) — the outage is real, so link the raw UUID."""
    from app.logic.monitoring_incidents import MonitoringIncidents

    registry(svc)  # knows hydra, not the service CI
    uuid = run(MonitoringIncidents(svc).handle_outage_persisted(persisted()))
    links = {
        (c["entity_uuid"], c["role"], c["display_name"])
        for c in svc.get_record(uuid)["ci_links"]
    }
    assert links == {(SERVICE_CI, "affected", NAME), (HYDRA, "related", "docker-hydra")}


def test_lost_insert_race_uses_the_peers_record(incidents, svc, monkeypatch):
    """Dedupe passed, then a peer instance inserted the same (source, external_id)
    first: create_record raises DuplicateRecord → the winner is the result."""
    real_find = svc.find_by_external
    calls = {"n": 0}

    def _find(source, external_id):
        calls["n"] += 1
        if calls["n"] == 1:
            # Our pre-check sees nothing yet …
            return None
        return real_find(source, external_id)

    monkeypatch.setattr(svc, "find_by_external", _find)
    # … but the peer commits before our insert.
    peer = svc.create_record(
        {
            "record_type": "incident",
            "title": "peer",
            "source": "monitoring",
            "external_id": outage_id(),
        },
        author="peer",
    )
    uuid = run(incidents.handle_outage_persisted(persisted()))
    assert uuid == peer["uuid"] and len(_incidents(svc)) == 1 and not _failures(svc)


def test_handler_waits_for_tables_then_gives_up(incidents, svc, monkeypatch):
    from app.logic import ingest

    monkeypatch.setattr(ingest, "READY_WAIT_SECONDS", 0.0)
    svc._ready = False
    assert run(incidents.handle_outage_persisted(persisted())) is None
    assert _failures(svc)[-1]["error"] == "database not ready"


# ── outage_recovered ──────────────────────────────────────────────────────────


def test_recovered_auto_resolves_with_auto_recovered_code(incidents, svc):
    uuid = run(incidents.handle_outage_persisted(persisted()))
    assert run(incidents.handle_outage_recovered(recovered())) == uuid
    rec = svc.get_record(uuid)
    assert (
        rec["state"] == "resolved" and rec["resolved_at"] and rec["closed_at"] is None
    )
    assert rec["custom_fields"]["resolution_code"] == "auto_recovered"
    assert "1 h 30 min" in rec["custom_fields"]["resolution_notes"]
    change = rec["journal"][-1]
    assert change["kind"] == "state_change" and change["author"] == "monitoring"
    assert change["data"] == {
        "from": "new",
        "to": "resolved",
        "fields": ["resolution_code", "resolution_notes"],
    }
    assert svc._ctx.topics()[-1] == "itsm:state_changed"


def test_recovered_from_on_hold_leaves_comment_and_state(incidents, svc):
    uuid = run(incidents.handle_outage_persisted(persisted()))
    svc.transition(uuid, "in_progress", author="marvin")
    svc.transition(
        uuid, "on_hold", fields={"hold_reason": "awaiting_vendor"}, author="marvin"
    )
    run(incidents.handle_outage_recovered(recovered()))
    rec = svc.get_record(uuid)
    assert rec["state"] == "on_hold"
    comment = rec["journal"][-1]
    assert (
        comment["kind"] == "comment"
        and comment["author"] == "monitoring"
        and "on_hold" in comment["body"]
    )
    assert not _failures(svc)


def test_recovered_without_auto_resolve_comments_only(incidents, svc):
    uuid = run(
        incidents.handle_outage_persisted(
            persisted(rule={**RULE, "auto_resolve": False})
        )
    )
    run(incidents.handle_outage_recovered(recovered()))
    rec = svc.get_record(uuid)
    assert rec["state"] == "new" and rec["resolved_at"] is None
    assert (
        rec["journal"][-1]["kind"] == "comment"
        and rec["journal"][-1]["author"] == "monitoring"
    )
    assert svc._ctx.topics()[-1] == "itsm:journal_added"


def test_recovered_for_unknown_outage_is_a_noop(incidents, svc):
    assert run(incidents.handle_outage_recovered(recovered())) is None
    assert _incidents(svc) == [] and not _failures(svc)
    assert (
        run(incidents.handle_outage_recovered({"since": SINCE})) is None
    )  # outage_id missing
    assert _failures(svc)[-1]["topic"] == "monitoring:outage_recovered"


def test_recovered_of_an_additional_outage_is_noted_on_the_open_incident(
    incidents, svc
):
    uuid = run(
        incidents.handle_outage_persisted(
            persisted(rule={**RULE, "auto_resolve": False})
        )
    )
    run(
        incidents.handle_outage_persisted(
            persisted(outage_id=outage_id(since=SINCE_2), since=SINCE_2)
        )
    )
    assert (
        run(
            incidents.handle_outage_recovered(
                recovered(outage_id=outage_id(since=SINCE_2), since=SINCE_2)
            )
        )
        == uuid
    )
    rec = svc.get_record(uuid)
    assert rec["state"] == "new"
    assert [j["kind"] for j in rec["journal"]] == ["system", "work_note", "work_note"]
    assert outage_id(since=SINCE_2) in rec["journal"][-1]["body"]


def test_recovered_twice_and_after_manual_close_changes_nothing(incidents, svc):
    uuid = run(incidents.handle_outage_persisted(persisted()))
    run(incidents.handle_outage_recovered(recovered()))
    n = len(svc.get_record(uuid)["journal"])
    run(incidents.handle_outage_recovered(recovered()))
    assert len(svc.get_record(uuid)["journal"]) == n
    svc.transition(uuid, "closed", author="marvin")
    run(incidents.handle_outage_recovered(recovered()))
    assert (
        svc.get_record(uuid)["state"] == "closed"
        and len(svc.get_record(uuid)["journal"]) == n + 1
    )


def test_new_outage_after_resolved_opens_a_new_incident(incidents, svc):
    first = run(incidents.handle_outage_persisted(persisted()))
    run(incidents.handle_outage_recovered(recovered()))
    second = run(
        incidents.handle_outage_persisted(
            persisted(outage_id=outage_id(since=SINCE_2), since=SINCE_2)
        )
    )
    assert second != first and len(_incidents(svc)) == 2
    assert (
        svc.get_record(first)["state"] == "resolved"
        and svc.get_record(second)["state"] == "new"
    )
    assert svc.get_record(second)["number"] == "INC0000002"


def test_recovered_retries_once_on_version_conflict(incidents, svc, monkeypatch):
    from app.logic.service import VersionConflict

    uuid = run(incidents.handle_outage_persisted(persisted()))
    real = svc.transition
    calls = {"n": 0}

    def _flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise VersionConflict("simulated")
        return real(*a, **kw)

    monkeypatch.setattr(svc, "transition", _flaky)
    run(incidents.handle_outage_recovered(recovered()))
    assert calls["n"] == 2 and svc.get_record(uuid)["state"] == "resolved"


def test_recovered_gives_up_after_repeated_version_conflict(
    incidents, svc, monkeypatch
):
    from app.logic.service import VersionConflict

    uuid = run(incidents.handle_outage_persisted(persisted()))

    def _always(*a, **kw):
        raise VersionConflict("simulated")

    monkeypatch.setattr(svc, "transition", _always)
    assert run(incidents.handle_outage_recovered(recovered())) == uuid
    assert svc.get_record(uuid)["state"] == "new" and not _failures(svc)


def test_recovered_concurrently_resolved_by_an_agent_adds_nothing(
    incidents, svc, monkeypatch
):
    from app.logic.service import VersionConflict

    uuid = run(incidents.handle_outage_persisted(persisted()))
    real = svc.transition

    def _agent_was_faster(*a, **kw):
        monkeypatch.setattr(svc, "transition", real)
        real(
            uuid,
            "resolved",
            fields={"resolution_code": "fixed", "resolution_notes": "restarted"},
            author="marvin",
        )
        raise VersionConflict("simulated")

    monkeypatch.setattr(svc, "transition", _agent_was_faster)
    run(incidents.handle_outage_recovered(recovered()))
    rec = svc.get_record(uuid)
    assert (
        rec["custom_fields"]["resolution_code"] == "fixed"
        and rec["journal"][-1]["author"] == "marvin"
    )


def test_catalog_without_auto_recovered_falls_back_to_comment(incidents, svc):
    uuid = run(incidents.handle_outage_persisted(persisted()))
    field = svc.catalog.record_type("incident").field("resolution_code")
    field.options = [
        o for o in field.options if o.id != "auto_recovered"
    ]  # operator catalog
    run(incidents.handle_outage_recovered(recovered()))
    rec = svc.get_record(uuid)
    assert (
        rec["state"] == "new"
        and rec["journal"][-1]["kind"] == "comment"
        and not _failures(svc)
    )


def test_texts_follow_the_configured_locale(incidents, svc):
    svc.apply_settings({"auto_text_locale": "en"})
    uuid = run(incidents.handle_outage_persisted(persisted()))
    assert "unreachable since" in svc.get_record(uuid)["title"]
    svc.apply_settings({"auto_text_locale": "fr"})  # no such locale file → kept
    assert svc.texts.locale == "en"
    svc.apply_settings({"auto_text_locale": "de"})
    other = run(
        incidents.handle_outage_persisted(
            persisted(ci_id=OTHER_CI, outage_id=outage_id(OTHER_CI))
        )
    )
    assert "nicht erreichbar seit" in svc.get_record(other)["title"]


# ── IT-03: recovery overtakes outage_persisted ────────────────────────────────


def test_recovered_before_persisted_closes_the_late_incident(incidents, svc):
    assert run(incidents.handle_outage_recovered(recovered())) is None
    run(incidents.handle_outage_recovered(recovered()))  # re-delivery: first memo wins
    uuid = run(incidents.handle_outage_persisted(persisted()))
    rec = svc.get_record(uuid)
    assert rec["state"] == "resolved"
    assert rec["custom_fields"]["resolution_code"] == "auto_recovered"
    assert svc.take_ingest_event("monitoring", outage_id()) is None  # consumed


def test_expired_recovery_memo_is_ignored(incidents, svc, monkeypatch):
    from app.logic import monitoring_incidents as mod

    monkeypatch.setattr(mod, "RECOVERY_MEMO_SECONDS", -1.0)
    run(incidents.handle_outage_recovered(recovered()))
    uuid = run(incidents.handle_outage_persisted(persisted()))
    assert svc.get_record(uuid)["state"] == "new"


def test_recovery_racing_the_insert_still_resolves(incidents, svc, monkeypatch):
    """The incident is inserted between the recovery's lookup and its memo."""
    uuid = run(incidents.handle_outage_persisted(persisted()))
    real = incidents._find_by_external
    calls = {"n": 0}

    async def _stale_first(external_id):
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(external_id)

    monkeypatch.setattr(incidents, "_find_by_external", _stale_first)
    monkeypatch.setattr(incidents, "_find_open_for_ci", lambda ci: asyncio.sleep(0))
    assert run(incidents.handle_outage_recovered(recovered())) == uuid
    assert svc.get_record(uuid)["state"] == "resolved"


# ── concurrent outage_persisted for one CI (MO-10, dedupe race) ───────────────


def _slow_registry(handler, monkeypatch):
    """Registry lookups really yield to the loop — the window between the dedupe
    check and the insert in which concurrent events used to overtake each other."""
    real = handler._ci_links

    async def slow(*a, **kw):
        await asyncio.sleep(0.05)
        return await real(*a, **kw)

    monkeypatch.setattr(handler, "_ci_links", slow)


def _open_monitoring(svc) -> list[dict]:
    return [r for r in _incidents(svc) if r["state"] in ("new", "in_progress")]


def test_concurrent_outages_of_one_ci_open_exactly_one_incident(incidents, svc, monkeypatch):
    _slow_registry(incidents, monkeypatch)
    events = [
        persisted(outage_id=outage_id(since=f"2026-10-18T01:00:0{i}Z"), since=f"2026-10-18T01:00:0{i}Z")
        for i in range(5)
    ]

    async def main():
        return await asyncio.gather(*(incidents.handle_outage_persisted(e) for e in events))

    uuids = run(main())
    assert len(_incidents(svc)) == 1 and len(set(uuids)) == 1 and not _failures(svc)
    notes = [j for j in svc.get_record(uuids[0])["journal"] if j["kind"] == "work_note"]
    assert len(notes) == 4  # the other four were annotated, not opened
    assert incidents._ci_locks == {}  # the lock map does not keep finished CIs


def test_ci_lock_is_per_ci_and_cleans_up(incidents):
    async def main():
        async with incidents._ci_lock(SERVICE_CI):
            async with incidents._ci_lock(OTHER_CI):  # another CI is not blocked
                assert set(incidents._ci_locks) == {SERVICE_CI, OTHER_CI}
            waiter = asyncio.ensure_future(incidents._ci_lock(SERVICE_CI).__aenter__())
            await asyncio.sleep(0.01)
            assert not waiter.done()  # same CI waits
            assert incidents._ci_locks[SERVICE_CI][1] == 2
        await waiter
        await incidents._ci_lock(SERVICE_CI).__aexit__(None, None, None)

    run(main())
    assert incidents._ci_locks == {}


def test_peer_instance_inserting_in_the_window_leaves_one_open_incident(incidents, svc, monkeypatch):
    """A peer instance (own lock) commits an incident for the same CI while we
    await the registry: our write-then-check sees it, cancels ours and points
    at it. Driven deterministically — the SQLite test DB is single-connection."""
    real = incidents._ci_links
    peer = {}

    async def with_peer_insert(*a, **kw):
        links = await real(*a, **kw)
        peer.update(svc.create_record(
            {"record_type": "incident", "title": "peer", "source": "monitoring",
             "external_id": outage_id(since=SINCE_2), "ci_links": links},
            author="monitoring"))
        return links

    monkeypatch.setattr(incidents, "_ci_links", with_peer_insert)
    # The peer record is "older": backdate it so the order is unambiguous.
    uuid = run(incidents.handle_outage_persisted(persisted()))
    recs = _incidents(svc)
    mine = next(r for r in recs if r["external_id"] == outage_id())
    if uuid == peer["uuid"]:  # peer ranked oldest (same-second opened_at → uuid order)
        assert mine["state"] == "cancelled"
        survivor = peer
    else:  # we ranked oldest: the peer's own check would cancel it
        assert uuid == mine["uuid"] and mine["state"] != "cancelled"
        survivor = mine
    assert survivor["number"]


def test_duplicate_is_cancelled_in_favour_of_the_oldest(incidents, svc):
    """Directly exercise the write-then-check: two open incidents for the CI,
    the younger one yields and the survivor gets the note."""
    from datetime import timedelta

    from app.model.database import get_session
    from app.model import models

    links = [{"entity_uuid": SERVICE_CI, "role": "affected"}]
    old = svc.create_record({"record_type": "incident", "title": "old", "source": "monitoring",
                             "external_id": "a", "ci_links": links}, author="monitoring")
    new = svc.create_record({"record_type": "incident", "title": "new", "source": "monitoring",
                             "external_id": "b", "ci_links": links}, author="monitoring")
    with get_session() as s:  # make the order explicit, independent of the clock
        rec = s.get(models.Record, old["uuid"])
        rec.opened_at = rec.opened_at - timedelta(minutes=5)
        s.commit()
    survivor = run(incidents._yield_to_older_incident(new, SERVICE_CI))
    assert survivor and survivor["uuid"] == old["uuid"]
    assert svc.get_record(new["uuid"])["state"] == "cancelled"
    assert run(incidents._yield_to_older_incident(old, SERVICE_CI)) is None  # the oldest stays
    assert svc.get_record(old["uuid"])["state"] != "cancelled"


def test_single_event_still_creates_exactly_one_incident(incidents, svc):
    uuid = run(incidents.handle_outage_persisted(persisted()))
    assert uuid and len(_open_monitoring(svc)) == 1 and incidents._ci_locks == {}
