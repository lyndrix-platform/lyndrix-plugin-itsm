"""REST API for the ITSM plugin.

Mounted by core at ``/api/plugins/lyndrix.plugin.itsm/``. Every capability
is reachable here — both UIs and other plugins' HTTP clients use only this
surface. Domain errors map to HTTP codes: ItsmError → 400, NotFound → 404,
VersionConflict / TransitionError → 409. Sync DB work runs via
``asyncio.to_thread``.
"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, model_validator

from core.api import ApiIdentity, require_permission

from ..logic.service import ItsmError, ItsmService, NotFound, TransitionError, VersionConflict

PLUGIN_ID = "lyndrix.plugin.itsm"
PERM_READ = f"plugin:{PLUGIN_ID}:api:read"
PERM_WRITE = f"plugin:{PLUGIN_ID}:api:write"
PERM_ADMIN = f"plugin:{PLUGIN_ID}:itsm:admin"


# ── Payloads ───────────────────────────────────────────────────────────────────

class CiLinkIn(BaseModel):
    """A configuration item to link. Give the registry UUID, or ``ci`` — any
    reference the Entity Registry resolves (UUID, ``iac.host:docker-hydra``,
    a bare identifier value like ``docker-hydra`` or a display name)."""
    entity_uuid: Optional[str] = Field(default=None, min_length=36, max_length=36)
    ci: Optional[str] = Field(default=None, min_length=1, max_length=255)
    role: str = "affected"
    display_name: Optional[str] = None
    entity_type: Optional[str] = None

    @model_validator(mode="after")
    def _uuid_or_ci(self) -> "CiLinkIn":
        if not self.entity_uuid and not self.ci:
            raise ValueError("entity_uuid or ci is required")
        return self


class RecordCreate(BaseModel):
    record_type: str
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    subtype: Optional[str] = None
    category: Optional[str] = None
    impact: Optional[int] = None
    urgency: Optional[int] = None
    assignment_group: Optional[str] = None
    assignee: Optional[str] = None
    requester: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    custom_fields: Dict[str, Any] = Field(default_factory=dict)
    ci_links: List[CiLinkIn] = Field(default_factory=list)
    due_at: Optional[str] = None
    # Gateway/ingest only: the external system of record's key.
    source: Optional[str] = None
    external_id: Optional[str] = None
    external_url: Optional[str] = None


class RecordPatch(BaseModel):
    # Only supplied fields are applied; ``version`` enables the optimistic lock.
    version: Optional[int] = None
    title: Optional[str] = None
    description: Optional[str] = None
    subtype: Optional[str] = None
    category: Optional[str] = None
    impact: Optional[int] = None
    urgency: Optional[int] = None
    assignment_group: Optional[str] = None
    assignee: Optional[str] = None
    requester: Optional[str] = None
    tags: Optional[List[str]] = None
    custom_fields: Optional[Dict[str, Any]] = None
    due_at: Optional[str] = None


class TransitionIn(BaseModel):
    to_state: str
    fields: Dict[str, Any] = Field(default_factory=dict)
    note: Optional[str] = None
    version: Optional[int] = None


class JournalIn(BaseModel):
    kind: str = "comment"
    body: str = Field(min_length=1)


class RecordLinkIn(BaseModel):
    to: str = Field(min_length=1, max_length=36)  # uuid or number
    relation: str


# ── Router ─────────────────────────────────────────────────────────────────────

def build_plugin_router(service: ItsmService) -> APIRouter:
    router = APIRouter(tags=["ITSM"])

    def _ensure_ready() -> None:
        if not service.is_ready:
            raise HTTPException(status_code=503, detail="ITSM database not ready — retry shortly.")

    async def _call(fn, *args, **kwargs):
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except ItsmError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except NotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (VersionConflict, TransitionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # catalog errors etc. → bad request, not 500
            if type(exc).__name__ == "CatalogError":
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            raise

    async def _ci_links(items: List[CiLinkIn]) -> List[dict]:
        """Resolve CI references through the Entity Registry (same error mapping)."""
        try:
            return await service.resolve_ci_links([c.model_dump() for c in items])
        except ItsmError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ── Records ────────────────────────────────────────────────────────────────
    @router.get("/records")
    async def list_records(
        type: Optional[str] = None,  # noqa: A002
        state: Optional[str] = None,
        priority: Optional[str] = None,
        assignee: Optional[str] = None,
        group: Optional[str] = None,
        tag: Optional[str] = None,
        ci: Optional[str] = None,
        q: Optional[str] = None,
        include_closed: bool = False,
        limit: int = Query(200, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        identity: ApiIdentity = Depends(require_permission(PERM_READ)),
    ):
        _ensure_ready()
        return await _call(
            service.list_records, record_type=type, state=state, priority=priority,
            assignee=assignee, assignment_group=group, tag=tag, ci=ci, q=q,
            include_closed=include_closed, limit=limit, offset=offset,
        )

    @router.post("/records", status_code=201)
    async def create_record(
        payload: RecordCreate, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        # source/external_id mark a record as owned by an external system of
        # record (gateway mode). Only administrators (or connectors running as
        # the system key) may claim that — an agent must not impersonate BMC.
        if (payload.source or payload.external_id) and not identity.allows(PERM_ADMIN):
            raise HTTPException(
                status_code=403,
                detail="Setting source/external_id requires the itsm:admin permission.",
            )
        data = payload.model_dump(exclude_unset=True)
        data["ci_links"] = await _ci_links(payload.ci_links)
        record = await _call(service.create_record, data, author=identity.username)
        return {"record": record}

    @router.get("/records/{ref}")
    async def get_record(ref: str, identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        _ensure_ready()
        record = await _call(service.get_record, ref)
        if record is None:
            raise HTTPException(status_code=404, detail=f"Unknown record: {ref}")
        return {"record": record}

    @router.patch("/records/{ref}")
    async def update_record(
        ref: str, payload: RecordPatch, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        changes = payload.model_dump(exclude_unset=True)
        version = changes.pop("version", None)
        record = await _call(
            service.update_record, ref, changes, author=identity.username, expected_version=version
        )
        return {"record": record}

    @router.post("/records/{ref}/transition")
    async def transition(
        ref: str, payload: TransitionIn, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        record = await _call(
            service.transition, ref, payload.to_state, fields=payload.fields, note=payload.note,
            author=identity.username, expected_version=payload.version,
        )
        return {"record": record}

    @router.post("/records/{ref}/journal", status_code=201)
    async def add_journal(
        ref: str, payload: JournalIn, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        entry = await _call(service.add_journal, ref, payload.kind, payload.body, author=identity.username)
        return {"entry": entry}

    # ── Links ──────────────────────────────────────────────────────────────────
    @router.post("/records/{ref}/ci-links")
    async def link_ci(
        ref: str, payload: CiLinkIn, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        link = (await _ci_links([payload]))[0]
        record = await _call(
            service.link_ci, ref, link["entity_uuid"], link["role"],
            display_name=link["display_name"], entity_type=link["entity_type"], author=identity.username,
        )
        return {"record": record}

    @router.delete("/records/{ref}/ci-links/{role}/{entity_uuid}")
    async def unlink_ci(
        ref: str, role: str, entity_uuid: str,
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        _ensure_ready()
        record = await _call(service.unlink_ci, ref, entity_uuid, role, author=identity.username)
        return {"record": record}

    @router.post("/records/{ref}/links")
    async def link_records(
        ref: str, payload: RecordLinkIn, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        record = await _call(service.link_records, ref, payload.to, payload.relation, author=identity.username)
        return {"record": record}

    @router.delete("/records/{ref}/links/{relation}/{to}")
    async def unlink_records(
        ref: str, relation: str, to: str, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        record = await _call(service.unlink_records, ref, to, relation, author=identity.username)
        return {"record": record}

    # ── Attachments ────────────────────────────────────────────────────────────
    @router.post("/records/{ref}/attachments", status_code=201)
    async def upload_attachment(
        ref: str,
        file: UploadFile = File(...),
        identity: ApiIdentity = Depends(require_permission(PERM_WRITE)),
    ):
        _ensure_ready()
        # Read at most limit+1 bytes so an oversized upload is rejected without
        # buffering the whole thing.
        data = await file.read(service.max_attachment_bytes + 1)
        meta = await _call(
            service.add_attachment, ref, file.filename or "file",
            file.content_type or "application/octet-stream", data, author=identity.username,
        )
        return {"attachment": meta}

    @router.get("/records/{ref}/attachments/{attachment_uuid}")
    async def download_attachment(
        ref: str, attachment_uuid: str, identity: ApiIdentity = Depends(require_permission(PERM_READ))
    ):
        _ensure_ready()
        meta, path = await _call(service.get_attachment, ref, attachment_uuid)
        if not path.is_file():
            raise HTTPException(status_code=410, detail="Attachment file is missing on disk")
        return FileResponse(path, media_type=meta["content_type"], filename=meta["filename"])

    @router.delete("/records/{ref}/attachments/{attachment_uuid}")
    async def delete_attachment(
        ref: str, attachment_uuid: str, identity: ApiIdentity = Depends(require_permission(PERM_WRITE))
    ):
        _ensure_ready()
        if not await _call(service.delete_attachment, ref, attachment_uuid, author=identity.username):
            raise HTTPException(status_code=404, detail="Unknown attachment")
        return {"ok": True}

    # ── Catalog / stats ────────────────────────────────────────────────────────
    @router.get("/catalog")
    async def get_catalog(identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        return await asyncio.to_thread(service.catalog.to_payload)

    @router.post("/catalog/reload")
    async def reload_catalog(identity: ApiIdentity = Depends(require_permission(PERM_ADMIN))):
        def _run() -> dict:
            service.reload_catalog(announce=True)  # peers reload on the cluster event
            return service.catalog.to_payload()

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:  # a broken catalog must not take the plugin down
            raise HTTPException(status_code=400, detail=f"Catalog reload failed: {exc}") from exc

    @router.get("/stats")
    async def stats(identity: ApiIdentity = Depends(require_permission(PERM_READ))):
        _ensure_ready()
        return await _call(service.get_stats)

    return router
