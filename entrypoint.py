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
  Use ctx.request() for the round-trips; failures without a request_id are
  reported on itsm:ingest_failed.

Events emitted
--------------
  itsm:record_created / itsm:record_updated / itsm:state_changed /
  itsm:journal_added  — broadcasts (see app/logic/service.py)
  entity_registry:register — every new record becomes an itsm_record entity
"""
import asyncio
import time
from pathlib import Path

from core.api import ModuleManifest, PluginHealthStatus, db_instance

from .app.api import RecordCreate, build_plugin_router
from .app.logic.service import ItsmError, NotFound, TransitionError, VersionConflict
from .app.logic.service import itsm_service as svc

PLUGIN_ID = "lyndrix.plugin.itsm"

# ── Manifest ──────────────────────────────────────────────────────────────────

manifest = ModuleManifest(
    id=PLUGIN_ID,
    name="IT Service Management",
    version="0.1.0",
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
    # stored in Vault under plugins/<id>/settings, applied on vault:ready_for_data.
    settings_schema=[
        {"key": "attachment_dir", "label": "Attachment directory", "kind": "str",
         "category": "Attachments", "default": "/data/storage/itsm/attachments",
         "description": "Where uploaded files are stored (inside the core's data volume)."},
        {"key": "max_attachment_mb", "label": "Max attachment size (MB)", "kind": "int",
         "category": "Attachments", "default": "25"},
        {"key": "catalog_dir", "label": "Catalog directory", "kind": "str",
         "category": "Processes", "default": "",
         "description": "Optional: directory with your own record_types/*.yml (replaces the shipped set). Empty = built-in catalog."},
    ],
    permissions={
        "subscribe": [
            "db:connected",
            "vault:ready_for_data",
            "itsm:record_create",
            "itsm:record_update",
            "itsm:transition",
            "itsm:journal_add",
            # Own broadcasts, consumed for notification routing.
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
            "itsm:ingest_failed",
            "entity_registry:register",
        ],
    },
    notification_endpoints=[
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
        stats = await asyncio.to_thread(svc.get_stats)
    except Exception as exc:
        return PluginHealthStatus(
            status="error", details={"reason": "db_query_failed", "error": str(exc)},
            latency_ms=round((time.perf_counter() - start) * 1000, 1),
        )
    details = {"db_connected": True, "ready": True, "catalog_loaded": svc.catalog.is_loaded,
               "record_types": sorted(svc.catalog.types()) if svc.catalog.is_loaded else [],
               "attachment_dir": str(svc.attachment_dir), **stats}
    latency = round((time.perf_counter() - start) * 1000, 1)
    if not svc.catalog.is_loaded:
        return PluginHealthStatus(status="error", details={**details, "reason": "catalog_not_loaded"},
                                  latency_ms=latency)
    writable = await asyncio.to_thread(_dir_writable, svc.attachment_dir)
    if not writable:
        return PluginHealthStatus(status="degraded",
                                  details={**details, "reason": "attachment_dir_not_writable"},
                                  latency_ms=latency)
    return PluginHealthStatus(status="ok", details=details, latency_ms=latency)


def _dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


# ── Setup ─────────────────────────────────────────────────────────────────────

def setup(ctx):
    ctx.log.info("ITSM: setup started")
    svc.set_context(ctx)
    svc.catalog.load()  # fail loudly on a broken shipped catalog
    ctx.register_routes(build_plugin_router(svc))

    def _db_init(reason: str) -> None:
        try:
            svc.on_db_connected()
            ctx.log.info(f"ITSM: tables ready ({reason})")
        except Exception as exc:
            ctx.log.error(f"ITSM: DB init failed — {exc}")

    if db_instance.is_connected:
        _db_init("DB already connected")

    @ctx.subscribe("db:connected")
    async def _on_db_connected(payload):
        _db_init("db:connected")

    # ── Settings (Vault-backed, schema in manifest) ──────────────────────────
    def _apply_settings() -> None:
        settings = ctx.get_all_settings() or {}
        att_dir = (settings.get("attachment_dir") or "").strip()
        if att_dir:
            svc.attachment_dir = Path(att_dir)
        try:
            mb = int(settings.get("max_attachment_mb") or 0)
            if mb > 0:
                svc.max_attachment_bytes = mb * 1024 * 1024
        except (TypeError, ValueError):
            ctx.log.warning("ITSM: max_attachment_mb is not an integer — keeping default")
        cat_dir = (settings.get("catalog_dir") or "").strip()
        if cat_dir:
            if Path(cat_dir).is_dir():
                svc.set_catalog_dir(cat_dir)
                svc.reload_catalog()
                ctx.log.info(f"ITSM: using operator catalog at {cat_dir}")
            else:
                ctx.log.warning(f"ITSM: catalog_dir '{cat_dir}' not found — keeping built-in catalog")

    @ctx.subscribe("vault:ready_for_data")
    async def _on_vault_ready(payload):
        try:
            await asyncio.to_thread(_apply_settings)  # Vault I/O is blocking
        except Exception as exc:
            ctx.log.warning(f"ITSM: could not apply settings — {exc}")

    # ── Inbound bus API ──────────────────────────────────────────────────────
    def _fail(topic: str, error: str) -> None:
        ctx.log.warning(f"ITSM: {topic} rejected — {error}")
        ctx.emit("itsm:ingest_failed", {"topic": topic, "error": error})

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
            rec_data["ci_links"] = [c.model_dump() for c in payload_model.ci_links]
            record = await asyncio.to_thread(svc.create_record, rec_data, author=author)
            _reply("itsm:record_create_result", request_id, ok=True, record=record)
        except Exception as exc:
            _reply("itsm:record_create_result", request_id, ok=False, error=str(exc))

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

    # ── Notifications (routed by core per user preferences) ──────────────────
    def _notify(endpoint: str, record: dict, title: str, body: str, severity: str = "info") -> None:
        try:
            ctx.notify(endpoint, {"record_uuid": record.get("uuid"), "number": record.get("number"),
                                  "record_type": record.get("record_type"), "state": record.get("state"),
                                  "priority": record.get("priority")},
                       notification_id=f"itsm:{record.get('uuid')}:{endpoint}",
                       title=title, body=body, severity=severity)
        except Exception as exc:
            ctx.log.debug(f"ITSM: notify {endpoint} failed — {exc}")

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
