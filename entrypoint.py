"""lyndrix-plugin-itsm — Plugin entrypoint.

IT Service Management for Lyndrix: a generic record engine (incident, change,
problem, request, task — all defined in catalog/record_types/*.yml) that
works as its own suite (native mode) and is shaped to become a gateway to
corporate ITSM systems (BMC Helix, ServiceNow) via connector adapters.

Architecture
------------
  catalog/record_types/*.yml  — process definitions (states, fields, matrix)
  app/model/models.py         — Record / JournalEntry / CiLink / RecordLink /
                                Attachment / Counter
  app/logic/catalog.py        — validated catalog loader
  app/logic/service.py        — CRUD, transitions, journal, links, attachments
  app/logic/ingest.py         — base of the bus-event ingest handlers (dedupe,
                                CI links, race handling, ingest_failed)
  app/logic/monitoring_incidents.py — monitoring outages → incidents / auto-resolve
  app/logic/maintenance_tasks.py    — missing maintenance window → task
  app/logic/connectors/       — ConnectorAdapter seam (gateway mode, later)
  app/api/__init__.py         — REST router (both UIs use only this)
  app/ui/react/               — React UI (bundle → app/ui/static/ui_bundle.js)

Events consumed
---------------
  itsm:record_create  — record fields (+ author, request_id?) → reply on
                        itsm:record_create_result {request_id, ok, record|error}
  itsm:record_update  — {ref, changes, author, version?, request_id?} → reply on
                        itsm:record_update_result
  itsm:transition     — {ref, to_state, fields?, note?, author, version?, request_id?}
                        → reply on itsm:transition_result
  itsm:journal_add    — {ref, kind, body, author}
  itsm:ci_link        — {ref, ci | entity_uuid, role?, author, request_id?}
                        → reply on itsm:ci_link_result {request_id, ok, record, ci}
                        ``ci`` is any reference the Entity Registry resolves
                        (UUID, scheme:value, 'docker-hydra', display name).
  Use ctx.request() for the round-trips; failures without a request_id are
  reported on itsm:ingest_failed.
  plugin:settings_changed — core ≥ 1.5.1, bridged to every instance: settings
                        of this plugin were saved → re-apply them here.
  itsm:catalog_reloaded — a peer reloaded the catalog (payload["_origin"]) →
                        reload ours.
  monitoring:outage_persisted / monitoring:outage_recovered — maintenance-
                        windows contract (core docs/plugins.md → Maintenance
                        windows): an outage outside a window opens an incident
                        (source=monitoring, external_id=outage_id, priority from
                        the rule's severity), the recovery auto-resolves it with
                        resolution_code auto_recovered or leaves a comment.
  maintenance:window_missing — a deploy_policy: window deploy ran without a
                        window in the horizon → task "define a maintenance
                        window" (source=maintenance, external_id=event_id).
  Payloads are parsed here (no dependency on core.api.maintenance), handlers
  are idempotent (UNIQUE(source, external_id) + "open record per CI").

Events emitted
--------------
  itsm:record_created / itsm:record_updated / itsm:state_changed /
  itsm:journal_added  — broadcasts (see app/logic/service.py)
  itsm:ingest_failed  — {topic, error} for a rejected bus payload
  entity_registry:register — every new record becomes an itsm_record entity
  entity_registry:lookup   — request to resolve CI references for links
                             (reply: entity_registry:lookup_result)
  itsm:catalog_reloaded    — cluster=True after POST /catalog/reload

Cluster
-------
Routes run on every instance against the shared DB. The attachment directory
must be shared storage (RWX/NFS); health() and the runtime status report
``degraded`` when the other instances' markers are not visible in it.
"""
import asyncio
import time

from core.api import ModuleManifest, PluginHealthStatus, db_instance

from .app.api import RecordCreate, build_plugin_router
from .app.logic import maintenance_tasks, monitoring_incidents
from .app.logic.service import (
    CATALOG_RELOADED_TOPIC,
    ItsmError,
    NotFound,
    TransitionError,
    VersionConflict,
)
from .app.logic.service import itsm_service as svc

PLUGIN_ID = "lyndrix.plugin.itsm"
UI_RECORD_LINK = "/apps/lyndrix-plugin-itsm/itsm?record="

# ── Manifest ──────────────────────────────────────────────────────────────────

manifest = ModuleManifest(
    id=PLUGIN_ID,
    name="IT Service Management",
    version="0.3.0",
    description=(
        "Incidents, changes, problems, requests and tasks with catalog-driven "
        "processes, audit journal, CI links and attachments. Native suite today, "
        "gateway to BMC Helix / ServiceNow via connectors."
    ),
    author="Lyndrix",
    icon="support_agent",
    type="PLUGIN",
    min_core_version="1.4.0",
    auto_enable_on_install=True,
    repo_url="https://github.com/lyndrix-platform/lyndrix-plugin-itsm",
    ui_route="/itsm",
    react_ui=True,
    i18n_namespace="itsm",
    react_routes=[
        {"path": "/itsm", "label": "Service Management", "icon": "support_agent",
         "sidebar_visible": True},
    ],
    # Operator settings — edited via the core's schema-driven settings modal,
    # stored in Vault under plugins/<id>/settings, applied on vault:ready_for_data
    # and on plugin:settings_changed (every instance).
    settings_schema=[
        {"key": "attachment_dir", "label": "Attachment directory", "kind": "str",
         "category": "Attachments", "default": "/data/storage/itsm/attachments",
         "description": "Where uploaded files are stored (inside the core's data volume). "
                        "In cluster mode this must be shared storage (RWX/NFS) mounted on every instance."},
        {"key": "max_attachment_mb", "label": "Max attachment size (MB)", "kind": "int",
         "category": "Attachments", "default": "25"},
        {"key": "catalog_dir", "label": "Catalog directory", "kind": "str",
         "category": "Processes", "default": "",
         "description": "Optional: directory with your own record_types/*.yml (replaces the shipped set). Empty = built-in catalog."},
        {"key": "auto_text_locale", "label": "Language of generated texts", "kind": "select",
         "options": ["de", "en"], "category": "Automation", "default": "de",
         "description": "Titles, descriptions and journal entries of records the plugin opens itself "
                        "(incidents from monitoring outages, tasks from maintenance events)."},
    ],
    permissions={
        "subscribe": [
            "db:connected",
            "vault:ready_for_data",
            "plugin:settings_changed",
            CATALOG_RELOADED_TOPIC,
            "itsm:record_create",
            "itsm:record_update",
            "itsm:transition",
            "itsm:journal_add",
            "itsm:ci_link",
            # Reply topic of our entity_registry:lookup requests.
            "entity_registry:lookup_result",
            # Maintenance-windows contract: outages → incidents, missing window → task.
            monitoring_incidents.TOPIC_PERSISTED,
            monitoring_incidents.TOPIC_RECOVERED,
            maintenance_tasks.TOPIC_WINDOW_MISSING,
            # Own broadcasts, consumed for notification routing.
            "itsm:record_created",
            "itsm:record_updated",
            "itsm:state_changed",
            "itsm:journal_added",
        ],
        "emit": [
            "itsm:record_created",
            "itsm:record_updated",
            "itsm:state_changed",
            "itsm:journal_added",
            "itsm:record_create_result",
            "itsm:record_update_result",
            "itsm:transition_result",
            "itsm:ci_link_result",
            "itsm:ingest_failed",
            CATALOG_RELOADED_TOPIC,
            "entity_registry:register",
            "entity_registry:lookup",
        ],
    },
    notification_endpoints=[
        {"name": "record_created",
         "description": "A P1/P2 incident was opened automatically (monitoring outage)",
         "default_active": True, "internal_toast": True, "internal_persist": True},
        {"name": "record_assigned", "description": "A record was assigned to someone",
         "default_active": True, "internal_toast": True, "internal_persist": True},
        {"name": "record_state_changed", "description": "A record changed state",
         "default_active": True, "internal_toast": False, "internal_persist": True},
        {"name": "record_commented", "description": "A comment was added to a record",
         "default_active": True, "internal_toast": False, "internal_persist": True},
    ],
    custom_permissions=[
        {"id": "itsm:admin", "label": "ITSM Administration",
         "description": "Reload the process catalog and manage ITSM configuration",
         "icon": "admin_panel_settings"},
    ],
    roles=[
        {"id": "admin", "label": "ITSM Administrator",
         "permissions": [f"plugin:{PLUGIN_ID}:api:read", f"plugin:{PLUGIN_ID}:api:write",
                         f"plugin:{PLUGIN_ID}:itsm:admin"],
         "auto_map_groups": ["INT_ADMIN"],
         "description": "Full control incl. catalog reload."},
        {"id": "agent", "label": "ITSM Agent",
         "permissions": [f"plugin:{PLUGIN_ID}:api:read", f"plugin:{PLUGIN_ID}:api:write"],
         "auto_map_groups": [],
         "description": "Work records: create, update, transition, comment, link, attach."},
        {"id": "viewer", "label": "ITSM Viewer",
         "permissions": [f"plugin:{PLUGIN_ID}:api:read"],
         "auto_map_groups": [],
         "description": "Read-only access to records."},
    ],
)


# ── Health ────────────────────────────────────────────────────────────────────

async def health(ctx) -> PluginHealthStatus:
    start = time.perf_counter()
    if not db_instance.is_connected:
        return PluginHealthStatus(status="error", details={"reason": "db_unavailable"})
    if not svc.is_ready:
        return PluginHealthStatus(status="error", details={"reason": "tables_not_initialized"})
    try:
        status, details = await asyncio.to_thread(svc.health_report)
    except Exception as exc:
        return PluginHealthStatus(
            status="error", details={"reason": "db_query_failed", "error": str(exc)},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )
    return PluginHealthStatus(status=status, details=details,
                              latency_ms=round((time.perf_counter() - start) * 1000, 1))


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup(ctx):
    ctx.log.info("ITSM: setup started")
    svc.set_context(ctx)
    svc.catalog.load()  # fail loudly on a broken shipped catalog
    ctx.register_routes(build_plugin_router(svc))

    # DDL runs off-loop under the core bootstrap lock (see app/model/database.py).
    if db_instance.is_connected:
        ctx.create_task(svc.init_db("DB already connected"), name="itsm:db_init")

    @ctx.subscribe("db:connected")
    async def _on_db_connected(payload):
        await svc.init_db("db:connected")

    # ── Settings (Vault-backed, schema in manifest) ──────────────────────────
    async def _apply_settings(reason: str) -> None:
        def _run() -> None:  # Vault + disk I/O are blocking
            svc.apply_settings(ctx.get_all_settings() or {})
            svc.storage_check(force=True)  # writes this instance's cluster marker
            svc.sync_runtime_status()
        try:
            await asyncio.to_thread(_run)
        except Exception as exc:
            ctx.log.warning(f"ITSM: could not apply settings ({reason}) — {exc}")

    @ctx.subscribe("vault:ready_for_data")
    async def _on_vault_ready(payload):
        await _apply_settings("vault:ready_for_data")

    @ctx.subscribe("plugin:settings_changed")
    async def _on_settings_changed(payload):
        if isinstance(payload, dict) and payload.get("plugin_id") == PLUGIN_ID:
            await _apply_settings("plugin:settings_changed")

    @ctx.subscribe(CATALOG_RELOADED_TOPIC)
    async def _on_catalog_reloaded(payload):
        # Our own announcement has no _origin — that reload already happened.
        if not isinstance(payload, dict) or not payload.get("_origin"):
            return
        try:
            await asyncio.to_thread(svc.reload_catalog)
            ctx.log.info(f"ITSM: catalog reloaded (peer {payload['_origin']})")
        except Exception as exc:
            ctx.log.error(f"ITSM: catalog reload from peer failed — {exc}")

    # Announce this instance in the attachment dir right away (cluster sharing check).
    async def _initial_storage_check() -> None:
        try:
            await asyncio.to_thread(lambda: (svc.storage_check(force=True), svc.sync_runtime_status()))
        except Exception as exc:
            ctx.log.debug(f"ITSM: initial storage check failed — {exc}")

    ctx.create_task(_initial_storage_check(), name="itsm:storage_check")

    # ── Inbound bus API ──────────────────────────────────────────────────────
    def _fail(topic: str, error: str) -> None:
        svc.report_ingest_failure(topic, error)

    def _reply(reply_topic: str, request_id, **kw) -> None:
        if request_id is not None:
            ctx.emit(reply_topic, {"request_id": request_id, **kw})
        elif not kw.get("ok", True):
            _fail(reply_topic.replace("_result", ""), str(kw.get("error")))

    _DOMAIN_ERRORS = (ItsmError, NotFound, TransitionError, VersionConflict)

    @ctx.subscribe("itsm:record_create")
    async def _on_record_create(payload):
        data = payload if isinstance(payload, dict) else {}
        request_id = data.get("request_id")
        if not svc.is_ready:
            _reply("itsm:record_create_result", request_id, ok=False, error="database not ready")
            return
        author = str(data.get("author") or "bus")
        try:
            body = {k: v for k, v in data.items() if k not in ("request_id", "author")}
            payload_model = RecordCreate.model_validate(body)
            rec_data = payload_model.model_dump(exclude_unset=True)
            rec_data["ci_links"] = await svc.resolve_ci_links([c.model_dump() for c in payload_model.ci_links])
            record = await asyncio.to_thread(svc.create_record, rec_data, author=author)
            _reply("itsm:record_create_result", request_id, ok=True, record=record)
        except Exception as exc:
            _reply("itsm:record_create_result", request_id, ok=False, error=str(exc))

    @ctx.subscribe("itsm:ci_link")
    async def _on_ci_link(payload):
        """Attach a configuration item to an existing record (event management,
        connectors). ``ci`` may be a name — the Entity Registry resolves it."""
        data = payload if isinstance(payload, dict) else {}
        request_id = data.get("request_id")
        if not svc.is_ready or not data.get("ref") or not (data.get("ci") or data.get("entity_uuid")):
            _reply("itsm:ci_link_result", request_id, ok=False, error="ref and ci required / DB ready")
            return
        try:
            link = (await svc.resolve_ci_links([{
                "ci": data.get("ci"), "entity_uuid": data.get("entity_uuid"),
                "role": data.get("role") or "affected",
                "display_name": data.get("display_name"), "entity_type": data.get("entity_type"),
            }]))[0]
            record = await asyncio.to_thread(
                svc.link_ci, str(data["ref"]), link["entity_uuid"], link["role"],
                display_name=link["display_name"], entity_type=link["entity_type"],
                author=str(data.get("author") or "bus"),
            )
            _reply("itsm:ci_link_result", request_id, ok=True, record=record, ci=link)
        except _DOMAIN_ERRORS as exc:
            _reply("itsm:ci_link_result", request_id, ok=False, error=str(exc))
        except Exception as exc:
            ctx.log.error(f"ITSM: ci_link failed — {exc}")
            _reply("itsm:ci_link_result", request_id, ok=False, error="internal error")

    @ctx.subscribe("itsm:record_update")
    async def _on_record_update(payload):
        data = payload if isinstance(payload, dict) else {}
        request_id = data.get("request_id")
        if not svc.is_ready or not data.get("ref"):
            _reply("itsm:record_update_result", request_id, ok=False, error="ref required / DB ready")
            return
        try:
            record = await asyncio.to_thread(
                svc.update_record, str(data["ref"]), dict(data.get("changes") or {}),
                author=str(data.get("author") or "bus"), expected_version=data.get("version"),
            )
            _reply("itsm:record_update_result", request_id, ok=True, record=record)
        except _DOMAIN_ERRORS as exc:
            _reply("itsm:record_update_result", request_id, ok=False, error=str(exc))
        except Exception as exc:
            ctx.log.error(f"ITSM: record_update failed — {exc}")
            _reply("itsm:record_update_result", request_id, ok=False, error="internal error")

    @ctx.subscribe("itsm:transition")
    async def _on_transition(payload):
        data = payload if isinstance(payload, dict) else {}
        request_id = data.get("request_id")
        if not svc.is_ready or not data.get("ref") or not data.get("to_state"):
            _reply("itsm:transition_result", request_id, ok=False, error="ref and to_state required / DB ready")
            return
        try:
            record = await asyncio.to_thread(
                svc.transition, str(data["ref"]), str(data["to_state"]),
                fields=dict(data.get("fields") or {}), note=data.get("note"),
                author=str(data.get("author") or "bus"), expected_version=data.get("version"),
            )
            _reply("itsm:transition_result", request_id, ok=True, record=record)
        except _DOMAIN_ERRORS as exc:
            _reply("itsm:transition_result", request_id, ok=False, error=str(exc))
        except Exception as exc:
            ctx.log.error(f"ITSM: transition failed — {exc}")
            _reply("itsm:transition_result", request_id, ok=False, error="internal error")

    @ctx.subscribe("itsm:journal_add")
    async def _on_journal_add(payload):
        data = payload if isinstance(payload, dict) else {}
        if not svc.is_ready or not data.get("ref") or not data.get("body"):
            _fail("itsm:journal_add", "ref and body required / DB ready")
            return
        try:
            await asyncio.to_thread(
                svc.add_journal, str(data["ref"]), str(data.get("kind") or "work_note"),
                str(data["body"]), author=str(data.get("author") or "bus"),
            )
        except _DOMAIN_ERRORS as exc:
            _fail("itsm:journal_add", str(exc))
        except Exception as exc:
            ctx.log.error(f"ITSM: journal_add failed — {exc}")

    # ── Maintenance-windows contract (other plugins' events → records) ───────
    # The handlers never raise: rejected payloads land on itsm:ingest_failed.
    incidents = monitoring_incidents.MonitoringIncidents(svc)
    tasks = maintenance_tasks.MaintenanceTasks(svc)

    @ctx.subscribe(monitoring_incidents.TOPIC_PERSISTED)
    async def _on_outage_persisted(payload):
        await incidents.handle_outage_persisted(payload)

    @ctx.subscribe(monitoring_incidents.TOPIC_RECOVERED)
    async def _on_outage_recovered(payload):
        await incidents.handle_outage_recovered(payload)

    @ctx.subscribe(maintenance_tasks.TOPIC_WINDOW_MISSING)
    async def _on_window_missing(payload):
        await tasks.handle_window_missing(payload)

    # ── Notifications (routed by core per user preferences) ──────────────────
    def _notify(endpoint: str, record: dict, title: str, body: str, severity: str = "info", **extra) -> None:
        try:
            ctx.notify(endpoint, {"record_uuid": record.get("uuid"), "number": record.get("number"),
                                  "record_type": record.get("record_type"), "state": record.get("state"),
                                  "priority": record.get("priority"), **extra},
                       notification_id=f"itsm:{record.get('uuid')}:{endpoint}",
                       title=title, body=body, severity=severity)
        except Exception as exc:
            ctx.log.debug(f"ITSM: notify {endpoint} failed — {exc}")

    @ctx.subscribe("itsm:record_created")
    async def _notify_created(payload):
        """Only incidents the plugin opened itself from a monitoring outage, and
        only P1/P2 — a manually created record stays silent on create."""
        record = (payload or {}).get("record") or {}
        if record.get("source") != monitoring_incidents.SOURCE or record.get("priority") not in ("P1", "P2"):
            return
        _notify("record_created", record,
                svc.texts("notify_created_title", number=record.get("number")),
                record.get("title") or "", "warning",
                link=f"{UI_RECORD_LINK}{record.get('uuid')}")

    @ctx.subscribe("itsm:record_updated")
    async def _notify_assigned(payload):
        changes = (payload or {}).get("changes") or {}
        record = (payload or {}).get("record") or {}
        if "assignee" in changes and changes["assignee"].get("new"):
            _notify("record_assigned", record,
                    f"{record.get('number')} assigned to {changes['assignee']['new']}",
                    record.get("title") or "")

    @ctx.subscribe("itsm:state_changed")
    async def _notify_state(payload):
        record = (payload or {}).get("record") or {}
        severity = "warning" if record.get("priority") in ("P1", "P2") else "info"
        _notify("record_state_changed", record,
                f"{record.get('number')}: {payload.get('from_state')} → {payload.get('to_state')}",
                record.get("title") or "", severity)

    @ctx.subscribe("itsm:journal_added")
    async def _notify_comment(payload):
        entry = (payload or {}).get("entry") or {}
        if entry.get("kind") != "comment":
            return
        _notify("record_commented", {"uuid": payload.get("record_uuid"), "number": payload.get("number")},
                f"{payload.get('number')}: new comment", (entry.get("body") or "")[:200])

    ctx.log.info("ITSM: setup complete")
