"""maintenance:window_missing → task "define a maintenance window" (decision H)."""

from __future__ import annotations

import asyncio

import pytest
from fakes import HYDRA, registry

SERVICE_CI = "5e1f0a3c-7b2d-4e8f-9a01-23456789abcd"
NAME = "aac-grafana @ docker-hydra"
DETECTED = "2026-10-18T12:00:00Z"


def event_id(job: int = 101) -> str:
    return f"window_missing:{SERVICE_CI}:{job}"


def window_missing(job: int = 101, **over) -> dict:
    data = {
        "event_id": event_id(job),
        "ci_id": SERVICE_CI,
        "kind": "service",
        "name": NAME,
        "host_ci_id": HYDRA,
        "site": "home",
        "stage": "prod",
        "horizon_days": 30,
        "job_id": job,
        "source": "iac-orchestrator",
        "detected_at": DETECTED,
        "reason": "no maintenance window within 30 days",
    }
    data.update(over)
    return data


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def tasks(svc):
    from app.logic.maintenance_tasks import MaintenanceTasks

    ctx = registry(svc)
    ctx.known[SERVICE_CI] = {
        "uuid": SERVICE_CI,
        "entity_type": "service",
        "display_name": NAME,
    }
    return MaintenanceTasks(svc)


def _tasks(svc) -> list[dict]:
    return svc.list_records(record_type="task", include_closed=True)["records"]


def test_window_missing_opens_p4_task_with_links_and_due_date(tasks, svc):
    uuid = run(tasks.handle_window_missing(window_missing()))
    rec = svc.get_record(uuid)
    assert rec["record_type"] == "task" and rec["number"] == "TSK0000001"
    assert rec["title"] == f"Wartungsfenster definieren: {NAME}"
    assert (rec["impact"], rec["urgency"], rec["priority"]) == (3, 2, "P4")
    assert rec["category"] == "maintenance" and rec["assignment_group"] == "infra"
    assert rec["tags"] == ["auto", "maintenance", "service"]
    assert rec["source"] == "maintenance" and rec["external_id"] == event_id()
    assert rec["custom_fields"] == {
        "source_ref": event_id(),
        "due": "2026-11-01T12:00:00Z",
    }
    assert "deploy_policy: window" in rec["description"] and "101" in rec["description"]
    assert "home/prod" in rec["description"] and "service.yml" in rec["description"]
    links = {(c["entity_uuid"], c["role"], c["display_name"]) for c in rec["ci_links"]}
    assert links == {(SERVICE_CI, "affected", NAME), (HYDRA, "related", "docker-hydra")}
    assert rec["created_by"] == "maintenance"
    assert not svc._ctx.payloads("itsm:ingest_failed")


def test_same_event_twice_is_one_task(tasks, svc):
    first = run(tasks.handle_window_missing(window_missing()))
    assert run(tasks.handle_window_missing(window_missing())) == first
    assert len(_tasks(svc)) == 1 and len(svc.get_record(first)["journal"]) == 1


def test_next_deploy_without_window_adds_work_note_to_the_open_task(tasks, svc):
    first = run(tasks.handle_window_missing(window_missing(job=101)))
    svc.transition(first, "in_progress", author="marvin")  # still open
    assert run(tasks.handle_window_missing(window_missing(job=102))) == first
    assert len(_tasks(svc)) == 1
    note = svc.get_record(first)["journal"][-1]
    assert (
        note["kind"] == "work_note"
        and note["author"] == "maintenance"
        and "102" in note["body"]
    )


def test_done_task_is_not_reopened_a_new_one_is_created(tasks, svc):
    first = run(tasks.handle_window_missing(window_missing(job=101)))
    svc.transition(first, "done", author="marvin")
    second = run(tasks.handle_window_missing(window_missing(job=102)))
    assert second != first and len(_tasks(svc)) == 2
    assert (
        svc.get_record(first)["state"] == "done"
        and svc.get_record(second)["state"] == "open"
    )
    assert svc.get_record(second)["custom_fields"]["source_ref"] == event_id(102)


def test_invalid_payloads_report_ingest_failed(tasks, svc):
    assert run(tasks.handle_window_missing(window_missing(ci_id="aac-grafana"))) is None
    assert run(tasks.handle_window_missing(window_missing(name=""))) is None
    assert run(tasks.handle_window_missing({"ci_id": SERVICE_CI, "name": NAME})) is None
    failures = svc._ctx.payloads("itsm:ingest_failed")
    assert [f["topic"] for f in failures] == ["maintenance:window_missing"] * 3
    assert (
        "ci_id" in failures[0]["error"]
        and "name" in failures[1]["error"]
        and "event_id" in failures[2]["error"]
    )
    assert _tasks(svc) == []


def test_without_host_or_detected_at_defaults_are_used(tasks, svc):
    from datetime import datetime, timezone

    payload = window_missing(
        host_ci_id=None, detected_at=None, horizon_days=None, reason=None
    )
    uuid = run(tasks.handle_window_missing(payload))
    rec = svc.get_record(uuid)
    assert [c["role"] for c in rec["ci_links"]] == ["affected"]
    due = datetime.fromisoformat(rec["custom_fields"]["due"].replace("Z", "+00:00"))
    assert 13 <= (due - datetime.now(timezone.utc)).days <= 14
