"""Incidents from monitoring outages (maintenance-windows contract, P4).

The monitoring plugin reports an outage **once** after a CI has been down for
its rule's ``offline_minutes`` outside a maintenance window
(``monitoring:outage_persisted``) and once more when the monitor is back
(``monitoring:outage_recovered``). This module turns the first into an
incident and the second into an auto-resolve (or a comment).

Rules the contract fixes (``features/maintenance-windows/00-ergebnis-p0.md``):

* priority from the rule's severity — ``critical → P1``, ``high → P2``,
  ``medium → P3``, ``low → P4`` via ``impact × urgency``;
* dedupe by ``source="monitoring"`` + ``external_id=outage_id``
  (``"<ci_id>:<since>"``), and by "an open incident of this source already
  links the CI" → work note instead of a second record;
* a new outage after ``resolved`` opens a **new** incident (no reopen);
* events may arrive out of order: a recovery that finds no incident is
  remembered for 24 h (``itsm_ingest_tombstones``), and the ``persisted`` event
  that then creates the incident applies it right away — the incident never
  stays open for an outage that is already over;
* the service CI is ``affected``, its host ``related``;
* auto-resolve uses resolution code ``auto_recovered``; when the catalog does
  not allow the transition (``on_hold``) a comment is left instead;
* P1/P2 incidents opened this way raise the ``record_created`` notification
  (wired in ``entrypoint.py``).

ITSM parses the payload dicts itself — no dependency on ``core.api.maintenance``
so the plugin keeps running on cores before 1.11.
"""

from __future__ import annotations

import asyncio
import json

from core.api import get_logger

from .ingest import EventIngest, IngestError, fmt_duration, fmt_ts
from .service import ItsmError, TransitionError, VersionConflict

log = get_logger("Plugin:ITSM:Monitoring")

TOPIC_PERSISTED = "monitoring:outage_persisted"
TOPIC_RECOVERED = "monitoring:outage_recovered"

SOURCE = "monitoring"
AUTHOR = "monitoring"
SUBTYPE = "outage"
CATEGORY = "monitoring"
RESOLUTION_CODE = "auto_recovered"

#: severity (incident rule) → (impact, urgency); the catalog matrix makes that P1…P4.
SEVERITY_TO_IMPACT_URGENCY: dict[str, tuple[int, int]] = {
    "critical": (1, 1),
    "high": (1, 2),
    "medium": (2, 2),
    "low": (3, 2),
}
DEFAULT_SEVERITY = "high"
#: How long an ``outage_recovered`` that found no incident is remembered so a
#: late ``outage_persisted`` of the same outage still gets closed out.
RECOVERY_MEMO_SECONDS = 24 * 3600.0


def impact_urgency(severity: object) -> tuple[int, int]:
    """Map a rule severity; unknown values count as ``high`` (and are logged)."""
    if severity in SEVERITY_TO_IMPACT_URGENCY:
        return SEVERITY_TO_IMPACT_URGENCY[str(severity)]
    log.warning(
        f"ITSM: unknown outage severity {severity!r} — using '{DEFAULT_SEVERITY}'"
    )
    return SEVERITY_TO_IMPACT_URGENCY[DEFAULT_SEVERITY]


class _RefLock:
    """``async with`` an asyncio.Lock shared by everyone using the same key; the
    map entry is dropped when the last user leaves, so the map holds only CIs
    that are being processed right now (no unbounded growth)."""

    def __init__(self, registry: dict, key: str) -> None:
        self._registry = registry
        self._key = key

    async def __aenter__(self) -> None:
        entry = self._registry.setdefault(self._key, [asyncio.Lock(), 0])
        entry[1] += 1  # users, counted before awaiting: no await between lookup and increment
        try:
            await entry[0].acquire()
        except BaseException:
            self._release_ref(entry)
            raise

    async def __aexit__(self, *exc: object) -> None:
        entry = self._registry[self._key]
        entry[0].release()
        self._release_ref(entry)

    def _release_ref(self, entry: list) -> None:
        entry[1] -= 1
        if entry[1] <= 0:
            self._registry.pop(self._key, None)


class MonitoringIncidents(EventIngest):
    source = SOURCE
    author = AUTHOR
    record_type = "incident"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # ci_id → [lock, users]; see _RefLock. Locks are bound to the running loop
        # lazily (py3.10+), so one handler instance can serve several loops in tests.
        self._ci_locks: dict[str, list] = {}

    # ── Public bus API ─────────────────────────────────────────────────────

    async def handle_outage_persisted(self, payload: object) -> str | None:
        """→ uuid of the incident created or annotated; None when rejected."""
        return await self._guarded(TOPIC_PERSISTED, self._persisted, payload)

    async def handle_outage_recovered(self, payload: object) -> str | None:
        """→ uuid of the incident resolved or annotated; None when none matched."""
        return await self._guarded(TOPIC_RECOVERED, self._recovered, payload)

    # ── outage_persisted ───────────────────────────────────────────────────

    async def _persisted(self, data: dict) -> str | None:
        outage_id = self._text(data, "outage_id")
        ci_id = self._ci(data, "ci_id")
        rule = data.get("rule")
        if not isinstance(rule, dict):
            raise IngestError(
                "rule must be an object {offline_minutes, severity, auto_resolve}"
            )
        host_ci_id = self._ci(data, "host_ci_id", required=False)
        kind = str(data.get("kind") or "generic")
        name = str(data.get("name") or ci_id)
        since = fmt_ts(data.get("since"))
        duration = fmt_duration(data.get("duration_s"))

        # One outage at a time per CI: the steps below await registry lookups
        # between "no open incident yet" and the insert, so concurrent events of
        # the same CI (a burst from one evaluator pass) would all pass the check.
        async with self._ci_lock(ci_id):
            return await self._persisted_locked(
                data, outage_id, ci_id, rule, host_ci_id, kind, name, since, duration
            )

    async def _persisted_locked(
        self, data: dict, outage_id: str, ci_id: str, rule: dict,
        host_ci_id: str | None, kind: str, name: str, since: str, duration: str,
    ) -> str | None:
        # Dedupe 1 — this very outage was delivered before (or a peer handled it).
        existing = await self._find_by_external(outage_id)
        if existing is not None:
            log.debug(
                f"ITSM: outage {outage_id} already tracked as {existing['number']}"
            )
            return str(existing["uuid"])

        # Dedupe 2 — the CI already has an open monitoring incident: annotate it.
        open_incident = await self._find_open_for_ci(ci_id)
        if open_incident is not None:
            await self._journal(
                open_incident["uuid"],
                "work_note",
                self.texts(
                    "outage_repeated",
                    name=name,
                    since=since,
                    duration=duration,
                    outage_id=outage_id,
                ),
            )
            log.info(
                f"ITSM: outage {outage_id} noted on open incident {open_incident['number']}"
            )
            return str(open_incident["uuid"])

        impact, urgency = impact_urgency(rule.get("severity"))
        ci_links = await self._ci_links(ci_id, host_ci_id, kind=kind, name=name)
        host_label = next(
            (
                lk.get("display_name") or lk["entity_uuid"]
                for lk in ci_links
                if lk["role"] == "related"
            ),
            "—",
        )
        record = await self._create(
            outage_id,
            {
                "subtype": SUBTYPE,
                "category": CATEGORY,
                "title": self.texts("incident_title", name=name, since=since)[:255],
                "description": self.texts(
                    "incident_description",
                    monitor_id=data.get("monitor_id", "—"),
                    name=name,
                    kind=kind,
                    ci_id=ci_id,
                    host=host_label,
                    since=since,
                    persisted_at=fmt_ts(data.get("persisted_at")),
                    duration=duration,
                    offline_minutes=rule.get("offline_minutes", "—"),
                    severity=rule.get("severity", DEFAULT_SEVERITY),
                    auto_resolve=rule.get("auto_resolve", True),
                    error=data.get("error") or "—",
                ),
                "impact": impact,
                "urgency": urgency,
                "tags": ["auto", SOURCE, kind],
                "custom_fields": {
                    "monitor_rule": json.dumps(rule, sort_keys=True),
                    "outage_id": outage_id,
                    "ci_kind": kind,
                },
                "ci_links": ci_links,
            },
        )
        log.info(
            f"ITSM: opened {record['number']} ({record['priority']}) for outage {outage_id}"
        )
        # Write first, check afterwards: the lock only serialises this instance.
        # A peer may have opened an incident for the same CI in the meantime.
        survivor = await self._yield_to_older_incident(record, ci_id)
        if survivor is not None:
            await self._journal(
                survivor["uuid"],
                "work_note",
                self.texts(
                    "outage_repeated", name=name, since=since,
                    duration=duration, outage_id=outage_id,
                ),
            )
            recovery = await self._take_remembered(outage_id)
            if recovery is not None:
                await self._journal(
                    survivor["uuid"],
                    "work_note",
                    self.texts(
                        "recovered_additional", since=since,
                        recovered_at=fmt_ts(recovery.get("recovered_at")),
                        duration=fmt_duration(recovery.get("duration_s")),
                        outage_id=outage_id,
                    ),
                )
            return str(survivor["uuid"])
        # The recovery may have overtaken us (we await registry lookups between
        # the dedupe check and the insert): apply the remembered one now.
        recovery = await self._take_remembered(outage_id)
        if recovery is not None:
            log.info(
                f"ITSM: outage {outage_id} had already recovered — closing out {record['number']}"
            )
            await self._close_out(
                record,
                fmt_ts(recovery.get("recovered_at")),
                fmt_duration(recovery.get("duration_s")),
            )
        return str(record["uuid"])

    def _ci_lock(self, ci_id: str) -> "_RefLock":
        return _RefLock(self._ci_locks, ci_id)

    async def _yield_to_older_incident(self, record: dict, ci_id: str) -> dict | None:
        """Two instances (or a lost lock) may both have created an incident for
        this CI. All open monitoring incidents of the CI are ordered oldest
        first (``opened_at``, uuid) — every instance computes the same order, so
        exactly one survives. Ours is cancelled when it is not the first;
        returns the survivor then, None when ours stays.

        Residual risk: clock skew between instances can rank a record that was
        committed later as the oldest; the check of the one that finished first
        then did not see it. A duplicate is possible only in that window."""
        open_ones = await asyncio.to_thread(
            self.service.list_open_for_ci,
            ci_id, record_type=self.record_type, source=self.source,
        )
        if not open_ones or open_ones[0]["uuid"] == record["uuid"]:
            return None
        survivor = open_ones[0]
        try:
            await asyncio.to_thread(
                self.service.transition,
                record["uuid"],
                "cancelled",
                fields={"cancel_reason": self.texts("outage_duplicate_cancel", number=survivor["number"])},
                note=self.texts(
                    "outage_duplicate_note",
                    outage_id=(record.get("external_id") or "—"), number=survivor["number"],
                ),
                author=self.author,
            )
        except ItsmError as exc:
            # Cannot cancel (version conflict, someone already acted on it):
            # keep it — a visible duplicate is better than a lost incident.
            log.warning(
                f"ITSM: duplicate incident {record['number']} of {survivor['number']} could not be cancelled ({exc})"
            )
            return None
        log.info(
            f"ITSM: {record['number']} cancelled — {survivor['number']} already covers CI {ci_id}"
        )
        return survivor

    # ── outage_recovered ───────────────────────────────────────────────────

    async def _recovered(self, data: dict) -> str | None:
        outage_id = self._text(data, "outage_id")
        recovered_at = fmt_ts(data.get("recovered_at"))
        duration = fmt_duration(data.get("duration_s"))

        record = await self._find_by_external(outage_id)
        if record is None:
            return await self._recovered_without_incident(
                data, outage_id, recovered_at, duration
            )
        if not self._is_open(record):
            log.debug(
                f"ITSM: {record['number']} already {record['state']} — recovery of {outage_id} ignored"
            )
            return str(record["uuid"])

        await self._close_out(record, recovered_at, duration)
        return str(record["uuid"])

    async def _close_out(self, record: dict, recovered_at: str, duration: str) -> None:
        """The outage of this (open) incident is over: auto-resolve, or leave a
        comment when its rule says ``auto_resolve: false``."""
        if self._rule_of(record).get("auto_resolve", True):
            await self._auto_resolve(record, recovered_at, duration)
        else:
            await self._journal(
                record["uuid"],
                "comment",
                self.texts(
                    "recovered_no_auto_resolve",
                    recovered_at=recovered_at,
                    duration=duration,
                ),
            )

    async def _recovered_without_incident(
        self, data: dict, outage_id: str, recovered_at: str, duration: str
    ) -> str | None:
        """No incident carries this outage_id. If the outage was annotated on
        an open incident of the same CI (dedupe 2), note its end there too."""
        ci_id = self._ci(data, "ci_id", required=False)
        open_incident = await self._find_open_for_ci(ci_id) if ci_id else None
        if open_incident is None:
            return await self._remember_recovery(data, outage_id, recovered_at, duration)
        await self._journal(
            open_incident["uuid"],
            "work_note",
            self.texts(
                "recovered_additional",
                since=fmt_ts(data.get("since")),
                recovered_at=recovered_at,
                duration=duration,
                outage_id=outage_id,
            ),
        )
        return str(open_incident["uuid"])

    async def _remember_recovery(
        self, data: dict, outage_id: str, recovered_at: str, duration: str
    ) -> str | None:
        """Nothing to resolve (yet). ``outage_persisted`` of this outage may
        still be on its way — remember the recovery so that handler closes the
        incident it creates. Then look once more: the incident may have been
        inserted between our lookup and the memo (both sides write first and
        check afterwards, so one of them always sees the other)."""
        await self._remember(
            outage_id,
            {
                "recovered_at": data.get("recovered_at"),
                "duration_s": data.get("duration_s"),
            },
            ttl_seconds=RECOVERY_MEMO_SECONDS,
        )
        record = await self._find_by_external(outage_id)
        if record is None:
            log.info(
                f"ITSM: no incident for recovered outage {outage_id} — remembered for a late outage_persisted"
            )
            return None
        if await self._take_remembered(outage_id) is not None and self._is_open(record):
            await self._close_out(record, recovered_at, duration)
        return str(record["uuid"])

    @staticmethod
    def _rule_of(record: dict) -> dict:
        """The incident rule stored at creation (the recovered payload does not carry it)."""
        raw = (record.get("custom_fields") or {}).get("monitor_rule")
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw) if raw else {}
        except (TypeError, ValueError):
            parsed = {}
        return parsed if isinstance(parsed, dict) else {}

    async def _auto_resolve(
        self, record: dict, recovered_at: str, duration: str
    ) -> None:
        """``→ resolved`` with code ``auto_recovered``; a state the catalog does
        not let us leave (``on_hold``) gets a comment instead; a concurrent
        writer is handled by re-reading once."""
        fields = {
            "resolution_code": RESOLUTION_CODE,
            "resolution_notes": self.texts(
                "resolution_notes", recovered_at=recovered_at, duration=duration
            ),
        }
        note = self.texts(
            "recovered_note", recovered_at=recovered_at, duration=duration
        )
        for attempt in range(2):
            try:
                await asyncio.to_thread(
                    self.service.transition,
                    record["uuid"],
                    "resolved",
                    fields=fields,
                    note=note,
                    author=self.author,
                )
                log.info(
                    f"ITSM: {record['number']} auto-resolved (monitor back after {duration})"
                )
                return
            except TransitionError:
                fresh = await asyncio.to_thread(self.service.get_record, record["uuid"])
                if fresh is None or not self._is_open(fresh):
                    return  # someone resolved/closed it meanwhile — nothing to add
                await self._journal(
                    record["uuid"],
                    "comment",
                    self.texts(
                        "recovered_blocked",
                        recovered_at=recovered_at,
                        duration=duration,
                        state=fresh["state"],
                    ),
                )
                return
            except ItsmError as exc:
                # e.g. an operator catalog without the auto_recovered code.
                log.warning(
                    f"ITSM: {record['number']} cannot be auto-resolved ({exc}) — leaving a comment"
                )
                await self._journal(
                    record["uuid"],
                    "comment",
                    self.texts(
                        "recovered_no_auto_resolve",
                        recovered_at=recovered_at,
                        duration=duration,
                    ),
                )
                return
            except VersionConflict:
                fresh = await asyncio.to_thread(self.service.get_record, record["uuid"])
                if fresh is None or not self._is_open(fresh):
                    return
                record = fresh
                if attempt == 0:
                    log.info(
                        f"ITSM: {record['number']} changed concurrently — retrying auto-resolve once"
                    )
        log.warning(
            f"ITSM: giving up auto-resolve of {record['number']} after a repeated version conflict"
        )
