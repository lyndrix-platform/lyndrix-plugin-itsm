"""Tasks from maintenance events (maintenance-windows contract, decision H).

When a service with ``deploy_policy: window`` has no maintenance window
within the scheduling horizon, the IaC orchestrator deploys at once (a deploy
must not hang forever) and emits ``maintenance:window_missing``. ITSM opens a
task "define a maintenance window" for the service so the gap gets fixed in
the SSoT:

* dedupe by ``source="maintenance"`` + ``external_id=event_id``
  (``"window_missing:<ci_id>:<job_id>"``), and by "an open task of this
  source already links the CI" → work note on that task instead;
* a ``done``/``cancelled`` task is never reopened — the next deploy without a
  window opens a new one (same rule as incidents, decision F);
* priority P4 (impact 3 × urgency 2), category ``maintenance``, group
  ``infra``, due 14 days after detection; service CI ``affected``, host
  ``related``. No notification — the task shows up in the list with tag ``auto``.
"""

from __future__ import annotations

from core.api import get_logger

from .ingest import EventIngest, fmt_ts, iso_utc, parse_iso, plus_days

log = get_logger("Plugin:ITSM:Maintenance")

TOPIC_WINDOW_MISSING = "maintenance:window_missing"

SOURCE = "maintenance"
AUTHOR = "maintenance"
CATEGORY = "maintenance"
ASSIGNMENT_GROUP = "infra"
IMPACT, URGENCY = 3, 2  # → P4
DUE_DAYS = 14


class MaintenanceTasks(EventIngest):
    source = SOURCE
    author = AUTHOR
    record_type = "task"

    async def handle_window_missing(self, payload: object) -> str | None:
        """→ uuid of the task created or annotated; None when rejected."""
        return await self._guarded(TOPIC_WINDOW_MISSING, self._window_missing, payload)

    async def _window_missing(self, data: dict) -> str | None:
        event_id = self._text(data, "event_id")
        ci_id = self._ci(data, "ci_id")
        name = self._text(data, "name")
        host_ci_id = self._ci(data, "host_ci_id", required=False)
        kind = str(data.get("kind") or "service")
        site, stage = str(data.get("site") or "—"), str(data.get("stage") or "—")
        job_id = str(data.get("job_id") or "—")
        detected_at = fmt_ts(data.get("detected_at"))

        # Dedupe 1 — this very event was delivered before.
        existing = await self._find_by_external(event_id)
        if existing is not None:
            log.debug(f"ITSM: event {event_id} already tracked as {existing['number']}")
            return str(existing["uuid"])

        # Dedupe 2 — an open task for this CI exists: note the repeat, no second task.
        open_task = await self._find_open_for_ci(ci_id)
        if open_task is not None:
            await self._journal(
                open_task["uuid"],
                "work_note",
                self.texts(
                    "task_repeated",
                    job_id=job_id,
                    detected_at=detected_at,
                    site=site,
                    stage=stage,
                ),
            )
            log.info(
                f"ITSM: window_missing for {name} noted on open task {open_task['number']}"
            )
            return str(open_task["uuid"])

        due = iso_utc(plus_days(parse_iso(data.get("detected_at")), DUE_DAYS))
        ci_links = await self._ci_links(ci_id, host_ci_id, kind=kind, name=name)
        record = await self._create(
            event_id,
            {
                "title": self.texts("task_title", name=name)[:255],
                "description": self.texts(
                    "task_description",
                    name=name,
                    site=site,
                    stage=stage,
                    horizon_days=data.get("horizon_days", "—"),
                    job_id=job_id,
                    detected_at=detected_at,
                    reason=data.get("reason") or "—",
                ),
                "impact": IMPACT,
                "urgency": URGENCY,
                "category": CATEGORY,
                "assignment_group": ASSIGNMENT_GROUP,
                "tags": ["auto", SOURCE, kind],
                "custom_fields": {"source_ref": event_id, "due": due},
                "ci_links": ci_links,
            },
        )
        log.info(
            f"ITSM: opened {record['number']} — define a maintenance window for {name}"
        )
        return str(record["uuid"])
