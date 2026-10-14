"""ITSM — business logic for the generic record engine.

Everything that is "process" (which states exist, which transitions are
legal, which fields a state demands, how priority is derived) comes from the
catalog. This module enforces it, keeps the audit trail and emits events.

Events emitted
--------------
itsm:record_created   — {record}
itsm:record_updated   — {record, changes: {field: {old, new}}}
itsm:state_changed    — {record, from_state, to_state}
itsm:journal_added    — {record_uuid, number, entry}
entity_registry:register — every new record is announced as an itsm_record
                            entity (fire-and-forget; works without the registry)
entity_registry:lookup   — request (via ctx.request) to resolve a CI reference
                            for a CI link; reply on entity_registry:lookup_result
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from core.api import get_logger

from ..model.database import ensure_tables, get_session
from ..model.models import Attachment, CiLink, Counter, JournalEntry, Record, RecordLink
from . import attachments as att
from .catalog import CI_LINK_ROLES, RECORD_LINK_RELATIONS, CatalogError, CatalogLoader, RecordTypeDef

log = get_logger("Plugin:ITSM:Service")

SOURCE_ID = "itsm"
JOURNAL_USER_KINDS = ("comment", "work_note")

# Fields a PATCH may touch. state is changed only via transition(); identity
# and gateway keys never change after create.
_UPDATABLE = (
    "title", "description", "subtype", "category", "impact", "urgency",
    "assignment_group", "assignee", "requester", "tags", "custom_fields", "due_at",
)


class ItsmError(ValueError):
    """Invalid input → HTTP 400."""


class NotFound(LookupError):
    """Unknown record / link / attachment → HTTP 404."""


class VersionConflict(Exception):
    """Optimistic lock failed → HTTP 409."""


class TransitionError(Exception):
    """Transition not allowed from the current state → HTTP 409."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_dt(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise ItsmError(f"'{value}' is not an ISO-8601 datetime") from None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


class ItsmService:
    def __init__(self) -> None:
        self._ctx = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.catalog = CatalogLoader()
        self._ready = False
        self.attachment_dir = Path("/data/storage/itsm/attachments")
        self.max_attachment_bytes = 25 * 1024 * 1024
        self.allowed_mime: tuple[str, ...] = att.DEFAULT_ALLOWED_MIME
        self._registry_warned = False

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def set_context(self, ctx) -> None:
        self._ctx = ctx
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    def on_db_connected(self) -> None:
        ensure_tables()
        self._ready = True
        log.info("ITSM: DB tables ready")

    @property
    def is_ready(self) -> bool:
        return self._ready

    def _require_ready(self) -> None:
        if not self._ready:
            raise RuntimeError("ITSM database tables not ready")

    def set_catalog_dir(self, path: str | Path) -> None:
        self.catalog.catalog_dir = Path(path)

    def reload_catalog(self) -> None:
        self.catalog.reload()

    # ── Internal helpers ─────────────────────────────────────────────────────

    def _next_number(self, s, rt: RecordTypeDef) -> str:
        """Allocate the next human number under a row lock (same transaction)."""
        counter = s.execute(
            select(Counter).where(Counter.record_type == rt.id).with_for_update()
        ).scalar_one_or_none()
        if counter is None:
            counter = Counter(record_type=rt.id, last_value=0)
            s.add(counter)
            s.flush()
        counter.last_value += 1
        return f"{rt.prefix}{counter.last_value:0{rt.number_width}d}"

    def _journal(self, s, record_uuid: str, kind: str, author: str | None,
                 body: str | None = None, data: dict | None = None) -> JournalEntry:
        entry = JournalEntry(record_uuid=record_uuid, kind=kind, author=author,
                             body=body, data=data or {})
        s.add(entry)
        return entry

    def _get(self, s, ref: str) -> Record | None:
        """Fetch by uuid or by human number (INC0000042)."""
        rec = s.get(Record, ref) if len(ref) == 36 else None
        if rec is None:
            rec = s.execute(select(Record).where(Record.number == ref)).scalar_one_or_none()
        return rec

    def _detail(self, s, rec: Record) -> dict:
        data = rec.to_dict()
        rt = self.catalog.types().get(rec.record_type)
        data["allowed_transitions"] = rt.allowed_transitions(rec.state) if rt else []
        data["journal"] = [
            j.to_dict() for j in s.execute(
                select(JournalEntry).where(JournalEntry.record_uuid == rec.uuid)
                .order_by(JournalEntry.created_at, JournalEntry.id)
            ).scalars()
        ]
        data["ci_links"] = [
            c.to_dict() for c in s.execute(
                select(CiLink).where(CiLink.record_uuid == rec.uuid).order_by(CiLink.id)
            ).scalars()
        ]
        links = s.execute(
            select(RecordLink).where(or_(RecordLink.from_uuid == rec.uuid, RecordLink.to_uuid == rec.uuid))
        ).scalars().all()
        others = {lk.to_uuid if lk.from_uuid == rec.uuid else lk.from_uuid for lk in links}
        summaries = {}
        if others:
            summaries = {
                o.uuid: {"number": o.number, "title": o.title, "record_type": o.record_type,
                         "state": o.state, "priority": o.priority}
                for o in s.execute(select(Record).where(Record.uuid.in_(others))).scalars()
            }
        data["links"] = []
        for lk in links:
            outgoing = lk.from_uuid == rec.uuid
            other = lk.to_uuid if outgoing else lk.from_uuid
            relation = lk.relation if outgoing else RECORD_LINK_RELATIONS.get(lk.relation, lk.relation)
            data["links"].append({
                **lk.to_dict(), "direction": "out" if outgoing else "in",
                "relation_from_here": relation, "other_uuid": other,
                "other": summaries.get(other),
            })
        data["attachments"] = [
            a.to_dict() for a in s.execute(
                select(Attachment).where(Attachment.record_uuid == rec.uuid).order_by(Attachment.created_at)
            ).scalars()
        ]
        return data

    def _validate_classification(self, rt: RecordTypeDef, data: dict, *, partial: bool) -> dict:
        """Check subtype / category / group / impact / urgency against the catalog."""
        out: dict = {}
        if "subtype" in data:
            v = data["subtype"] or None
            if v is not None and rt.subtypes and v not in rt.subtype_ids():
                raise ItsmError(f"Unknown subtype '{v}' for {rt.id}")
            out["subtype"] = v
        if "category" in data:
            v = data["category"] or None
            if v is not None and rt.categories and v not in rt.category_ids():
                raise ItsmError(f"Unknown category '{v}' for {rt.id}")
            out["category"] = v
        if "assignment_group" in data:
            v = data["assignment_group"] or None
            if v is not None and rt.assignment_groups and v not in rt.group_ids():
                raise ItsmError(f"Unknown assignment group '{v}' for {rt.id}")
            out["assignment_group"] = v
        for key in ("impact", "urgency"):
            if key in data:
                v = data[key]
                if v in (None, ""):
                    out[key] = None
                    continue
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise ItsmError(f"{key} must be an integer") from None
                allowed = {int(o.id) for o in getattr(rt.priority, key)} if rt.priority else set()
                if allowed and v not in allowed:
                    raise ItsmError(f"{key} {v} is not defined for {rt.id}")
                out[key] = v
        if "tags" in data:
            tags = data["tags"] or []
            if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
                raise ItsmError("tags must be a list of strings")
            out["tags"] = sorted({t.strip() for t in tags if t.strip()})
        if "due_at" in data:
            out["due_at"] = _parse_dt(data["due_at"])
        for key in ("title", "description", "assignee", "requester"):
            if key in data:
                v = data[key]
                out[key] = (str(v).strip() or None) if v is not None else None
        if not partial or "title" in out:
            if not out.get("title"):
                raise ItsmError("title is required")
        return out

    # ── Create / read / list ─────────────────────────────────────────────────

    def create_record(self, data: dict, *, author: str | None = None) -> dict:
        self._require_ready()
        rt = self.catalog.record_type(str(data.get("record_type") or ""))
        fields = self._validate_classification(rt, data, partial=False)
        d_imp, d_urg = rt.default_impact_urgency()
        impact = fields.get("impact", d_imp) if fields.get("impact") is not None else d_imp
        urgency = fields.get("urgency", d_urg) if fields.get("urgency") is not None else d_urg
        try:
            custom = rt.validate_custom_fields(data.get("custom_fields") or {})
        except CatalogError as exc:
            raise ItsmError(str(exc)) from exc
        ci_links = self._check_ci_links(data.get("ci_links") or [])
        source = (data.get("source") or "").strip() or None
        external_id = (data.get("external_id") or "").strip() or None
        if bool(source) != bool(external_id):
            raise ItsmError("source and external_id must be given together")

        for attempt in range(3):
            with get_session() as s:
                try:
                    if source and s.execute(
                        select(Record.uuid).where(Record.source == source, Record.external_id == external_id)
                    ).scalar_one_or_none():
                        raise ItsmError(f"Record {source}/{external_id} already exists")
                    number = self._next_number(s, rt)
                    rec = Record(
                        uuid=str(uuid4()), number=number, record_type=rt.id,
                        subtype=fields.get("subtype"), category=fields.get("category"),
                        title=fields["title"], description=fields.get("description"),
                        state=rt.initial_state().id, impact=impact, urgency=urgency,
                        priority=rt.derive_priority(impact, urgency),
                        assignment_group=fields.get("assignment_group"),
                        assignee=fields.get("assignee"), requester=fields.get("requester") or author,
                        created_by=author, custom_fields=custom, tags=fields.get("tags", []),
                        opened_at=_now(), due_at=fields.get("due_at"), version=1,
                        source=source, external_id=external_id,
                        external_url=(data.get("external_url") or None),
                    )
                    s.add(rec)
                    s.flush()  # parent row first — CiLink/Journal FKs reference it
                    for link in ci_links:
                        s.add(CiLink(record_uuid=rec.uuid, **link))
                    self._journal(s, rec.uuid, "system", author, "created",
                                  {"state": rec.state, "ci_links": len(ci_links)})
                    s.commit()
                except IntegrityError as exc:
                    s.rollback()
                    # Lost a race with a concurrent push of the same external key.
                    # MariaDB names the constraint; SQLite lists the columns.
                    msg = str(exc.orig)
                    if source and ("uq_itsm_source_ext" in msg or "external_id" in msg):
                        raise ItsmError(f"Record {source}/{external_id} already exists") from exc
                    if attempt == 2:
                        raise
                    continue  # counter/number race — retry
                s.refresh(rec)
                detail = self._detail(s, rec)
            break

        log.info(f"ITSM: created {detail['number']} '{detail['title']}' by {author}")
        self._emit("itsm:record_created", {"record": detail})
        self._announce_entity(detail)
        return detail

    def get_record(self, ref: str) -> dict | None:
        self._require_ready()
        with get_session() as s:
            rec = self._get(s, ref)
            return self._detail(s, rec) if rec else None

    def list_records(
        self, *, record_type: str | None = None, state: str | None = None,
        priority: str | None = None, assignee: str | None = None,
        assignment_group: str | None = None, tag: str | None = None,
        ci: str | None = None, q: str | None = None, include_closed: bool = False,
        limit: int = 200, offset: int = 0,
    ) -> dict:
        self._require_ready()
        with get_session() as s:
            stmt = select(Record)
            if not include_closed:
                stmt = stmt.where(Record.closed_at.is_(None))
            if record_type:
                stmt = stmt.where(Record.record_type == record_type)
            if state:
                stmt = stmt.where(Record.state == state)
            if priority:
                stmt = stmt.where(Record.priority == priority)
            if assignee:
                stmt = stmt.where(Record.assignee == assignee)
            if assignment_group:
                stmt = stmt.where(Record.assignment_group == assignment_group)
            if ci:
                stmt = stmt.where(Record.uuid.in_(select(CiLink.record_uuid).where(CiLink.entity_uuid == ci)))
            if q:
                like = f"%{q.strip()}%"
                stmt = stmt.where(or_(Record.number.like(like), Record.title.like(like)))
            rows = s.execute(stmt.order_by(Record.opened_at.desc())).scalars().all()
            if tag:  # JSON list → filter in Python (portable across MariaDB/SQLite)
                rows = [r for r in rows if tag in (r.tags or [])]
            total = len(rows)
            rows = rows[offset: offset + limit]
            uuids = [r.uuid for r in rows]
            ci_counts: dict[str, int] = {}
            if uuids:
                for ru, n in s.execute(
                    select(CiLink.record_uuid, func.count()).where(CiLink.record_uuid.in_(uuids))
                    .group_by(CiLink.record_uuid)
                ).all():
                    ci_counts[ru] = n
            return {"total": total, "records": [{**r.to_dict(), "ci_count": ci_counts.get(r.uuid, 0)} for r in rows]}

    # ── Update / transition ──────────────────────────────────────────────────

    def update_record(self, ref: str, changes: dict, *, author: str | None = None,
                      expected_version: int | None = None) -> dict:
        self._require_ready()
        if "state" in changes:
            raise ItsmError("Use transition() to change the state")
        unknown = sorted(set(changes) - set(_UPDATABLE))
        if unknown:
            raise ItsmError(f"Field(s) not updatable: {', '.join(unknown)}")
        with get_session() as s:
            rec = self._get(s, ref)
            if rec is None:
                raise NotFound(f"Unknown record: {ref}")
            if expected_version is not None and rec.version != expected_version:
                raise VersionConflict(
                    f"{rec.number} was modified (version {rec.version}, you sent {expected_version})"
                )
            rt = self.catalog.record_type(rec.record_type)
            fields = self._validate_classification(rt, changes, partial=True)
            diff: dict[str, dict] = {}
            if "custom_fields" in changes:
                try:
                    new_custom = rt.validate_custom_fields(changes["custom_fields"] or {})
                except CatalogError as exc:
                    raise ItsmError(str(exc)) from exc
                merged = {**(rec.custom_fields or {}), **new_custom}
                for k, v in new_custom.items():
                    old = (rec.custom_fields or {}).get(k)
                    if old != v:
                        diff[f"custom_fields.{k}"] = {"old": old, "new": v}
                rec.custom_fields = merged
            for key, value in fields.items():
                old = getattr(rec, key)
                if old != value:
                    diff[key] = {"old": old.isoformat() if isinstance(old, datetime) else old,
                                 "new": value.isoformat() if isinstance(value, datetime) else value}
                    setattr(rec, key, value)
            if "impact" in diff or "urgency" in diff:
                new_prio = rt.derive_priority(rec.impact, rec.urgency)
                if new_prio != rec.priority:
                    diff["priority"] = {"old": rec.priority, "new": new_prio}
                    rec.priority = new_prio
            if not diff:
                return self._detail(s, rec)
            rec.version += 1
            self._journal(s, rec.uuid, "field_change", author, None, {"changes": diff})
            s.commit()
            s.refresh(rec)
            detail = self._detail(s, rec)
        log.info(f"ITSM: updated {detail['number']} {sorted(diff)} by {author}")
        self._emit("itsm:record_updated", {"record": detail, "changes": diff})
        return detail

    def transition(self, ref: str, to_state: str, *, fields: dict | None = None,
                   author: str | None = None, note: str | None = None,
                   expected_version: int | None = None) -> dict:
        self._require_ready()
        fields = dict(fields or {})
        with get_session() as s:
            rec = self._get(s, ref)
            if rec is None:
                raise NotFound(f"Unknown record: {ref}")
            if expected_version is not None and rec.version != expected_version:
                raise VersionConflict(
                    f"{rec.number} was modified (version {rec.version}, you sent {expected_version})"
                )
            rt = self.catalog.record_type(rec.record_type)
            target = rt.state(to_state)
            if target is None:
                raise ItsmError(f"Unknown state '{to_state}' for {rt.id}")
            if to_state not in rt.allowed_transitions(rec.state):
                raise TransitionError(
                    f"{rec.number}: {rec.state} → {to_state} is not allowed "
                    f"(allowed: {', '.join(rt.allowed_transitions(rec.state)) or 'none'})"
                )

            # Apply fields supplied with the transition, then check requirements.
            core_fields = {k: v for k, v in fields.items() if k in ("assignee", "assignment_group", "category", "subtype", "description")}
            custom_in = {k: v for k, v in fields.items() if k not in core_fields}
            applied = self._validate_classification(rt, core_fields, partial=True)
            for k, v in applied.items():
                setattr(rec, k, v)
            try:
                custom = rt.validate_custom_fields(custom_in)
            except CatalogError as exc:
                raise ItsmError(str(exc)) from exc
            rec.custom_fields = {**(rec.custom_fields or {}), **custom}

            missing = []
            for req in target.requires:
                value = getattr(rec, req, None) if req in ("assignee", "assignment_group", "category", "subtype", "description") \
                    else (rec.custom_fields or {}).get(req)
                if value in (None, "", []):
                    missing.append(req)
            if missing:
                raise ItsmError(f"State '{to_state}' requires: {', '.join(missing)}")

            from_state = rec.state
            rec.state = to_state
            now = _now()
            if target.kind == "resolved":
                rec.resolved_at = now
                rec.closed_at = None
            elif target.kind == "closed":
                rec.closed_at = now
                rec.resolved_at = rec.resolved_at or now
            else:
                rec.resolved_at = None
                rec.closed_at = None
            rec.version += 1
            self._journal(s, rec.uuid, "state_change", author, note,
                          {"from": from_state, "to": to_state, "fields": sorted(fields)})
            s.commit()
            s.refresh(rec)
            detail = self._detail(s, rec)
        log.info(f"ITSM: {detail['number']} {from_state} → {to_state} by {author}")
        self._emit("itsm:state_changed", {"record": detail, "from_state": from_state, "to_state": to_state})
        return detail

    # ── Journal ──────────────────────────────────────────────────────────────

    def add_journal(self, ref: str, kind: str, body: str, *, author: str | None = None) -> dict:
        self._require_ready()
        if kind not in JOURNAL_USER_KINDS:
            raise ItsmError(f"kind must be one of {', '.join(JOURNAL_USER_KINDS)}")
        if not (body or "").strip():
            raise ItsmError("body is required")
        with get_session() as s:
            rec = self._get(s, ref)
            if rec is None:
                raise NotFound(f"Unknown record: {ref}")
            entry = self._journal(s, rec.uuid, kind, author, body.strip())
            s.commit()
            s.refresh(entry)
            data = entry.to_dict()
            number = rec.number
        self._emit("itsm:journal_added", {"record_uuid": data["record_uuid"], "number": number, "entry": data})
        return data

    # ── Links ────────────────────────────────────────────────────────────────

    def _check_ci_links(self, links: list) -> list[dict]:
        out = []
        for raw in links:
            if not isinstance(raw, dict) or len(str(raw.get("entity_uuid") or "")) != 36:
                raise ItsmError("ci_links entries need a 36-char entity_uuid")
            role = raw.get("role") or "affected"
            if role not in CI_LINK_ROLES:
                raise ItsmError(f"Unknown CI link role '{role}'")
            out.append({"entity_uuid": raw["entity_uuid"], "role": role,
                        "display_name": raw.get("display_name"), "entity_type": raw.get("entity_type")})
        return out

    # ── CI resolution (Entity Registry — optional, degrades gracefully) ──────

    async def resolve_ci(self, ref: str, *, timeout: float = 5.0) -> dict | None:
        """Ask the Entity Registry which configuration item ``ref`` means.

        ``ref`` is anything the registry resolves: a UUID (merged tombstones are
        followed to the surviving CI), ``scheme:value``, a bare identifier value
        such as ``docker-hydra`` or a display name.

        Returns a link stub ``{entity_uuid, display_name, entity_type}``.
        Raises ItsmError when the registry answered but knows no such CI (near
        misses are named in the message). Returns None when the registry is
        not available (plugin absent, no ``ctx.request`` on an old core, timeout)
        — the caller decides how to degrade.
        """
        ctx = self._ctx
        if ctx is None or not hasattr(ctx, "request"):
            return None
        try:
            reply = await ctx.request(
                "entity_registry:lookup", {"ref": str(ref)},
                reply_topic="entity_registry:lookup_result", timeout=timeout,
            )
        except Exception as exc:  # missing permission, bus failure → registry "absent"
            log.debug(f"ITSM: registry lookup for '{ref}' failed — {exc}")
            return None
        if not reply or not reply.get("ok"):
            return None
        entity = reply.get("entity")
        if not entity:
            # ITSM records are registry entities too; they are never CIs here.
            cands = [c for c in (reply.get("candidates") or []) if c.get("entity_type") != "itsm_record"]
            hint = ", ".join(
                f"{c.get('display_name')} [{c.get('entity_type')}] {str(c.get('uuid'))[:8]}…"
                for c in cands[:5]
            )
            what = "is ambiguous" if reply.get("match") == "ambiguous" else "is unknown to the Entity Registry"
            raise ItsmError(f"CI '{ref}' {what}" + (f" — candidates: {hint}" if hint else ""))
        if entity.get("entity_type") == "itsm_record":
            raise ItsmError(
                f"'{ref}' is an ITSM record, not a configuration item — relate records via record links"
            )
        return {"entity_uuid": entity["uuid"], "display_name": entity.get("display_name"),
                "entity_type": entity.get("entity_type")}

    async def resolve_ci_links(self, links: list) -> list[dict]:
        """Normalise CI link inputs into validated link dicts.

        Each entry carries ``entity_uuid`` and/or ``ci`` (any ref the registry
        resolves) plus ``role``. The registry is consulted for every entry so
        names are cached from the source of truth, typos are rejected and
        merged UUIDs are re-pointed. Without the registry a raw UUID still links
        (with whatever name the caller supplied); a name alone cannot.
        """
        out: list[dict] = []
        for raw in links or []:
            if not isinstance(raw, dict):
                raise ItsmError("ci_links entries must be objects")
            uuid = str(raw.get("entity_uuid") or "").strip()
            ci_ref = str(raw.get("ci") or raw.get("ref") or "").strip()
            if not uuid and not ci_ref:
                raise ItsmError("ci_links entries need an entity_uuid or a ci reference")
            link = {"entity_uuid": uuid, "role": raw.get("role") or "affected",
                    "display_name": raw.get("display_name"), "entity_type": raw.get("entity_type")}
            resolved = await self.resolve_ci(uuid or ci_ref)
            if resolved is not None:
                link["entity_uuid"] = resolved["entity_uuid"]
                link["display_name"] = resolved["display_name"] or link["display_name"]
                link["entity_type"] = resolved["entity_type"] or link["entity_type"]
            elif not uuid:
                raise ItsmError(
                    f"Cannot resolve CI '{ci_ref}': the Entity Registry is not available — link by UUID instead"
                )
            out.append(link)
        return self._check_ci_links(out)

    def link_ci(self, ref: str, entity_uuid: str, role: str = "affected", *,
                display_name: str | None = None, entity_type: str | None = None,
                author: str | None = None) -> dict:
        self._require_ready()
        link = self._check_ci_links([{"entity_uuid": entity_uuid, "role": role,
                                      "display_name": display_name, "entity_type": entity_type}])[0]
        with get_session() as s:
            rec = self._get(s, ref)
            if rec is None:
                raise NotFound(f"Unknown record: {ref}")
            existing = s.execute(select(CiLink).where(
                CiLink.record_uuid == rec.uuid, CiLink.entity_uuid == entity_uuid, CiLink.role == link["role"]
            )).scalar_one_or_none()
            if existing:
                return self._detail(s, rec)
            s.add(CiLink(record_uuid=rec.uuid, **link))
            self._journal(s, rec.uuid, "link", author, None,
                          {"ci": entity_uuid, "role": link["role"], "display_name": display_name, "action": "added"})
            s.commit()
            return self._detail(s, rec)

    def unlink_ci(self, ref: str, entity_uuid: str, role: str, *, author: str | None = None) -> dict:
        self._require_ready()
        with get_session() as s:
            rec = self._get(s, ref)
            if rec is None:
                raise NotFound(f"Unknown record: {ref}")
            link = s.execute(select(CiLink).where(
                CiLink.record_uuid == rec.uuid, CiLink.entity_uuid == entity_uuid, CiLink.role == role
            )).scalar_one_or_none()
            if link is None:
                raise NotFound("Unknown CI link")
            s.delete(link)
            self._journal(s, rec.uuid, "link", author, None, {"ci": entity_uuid, "role": role, "action": "removed"})
            s.commit()
            return self._detail(s, rec)

    def link_records(self, from_ref: str, to_ref: str, relation: str, *, author: str | None = None) -> dict:
        self._require_ready()
        if relation not in RECORD_LINK_RELATIONS:
            raise ItsmError(f"Unknown relation '{relation}'")
        with get_session() as s:
            a, b = self._get(s, from_ref), self._get(s, to_ref)
            if a is None or b is None:
                raise NotFound("Unknown record")
            if a.uuid == b.uuid:
                raise ItsmError("A record cannot link to itself")
            # Store one row for the pair: if the inverse already exists, that's the same link.
            inverse = RECORD_LINK_RELATIONS[relation]
            existing = s.execute(select(RecordLink).where(or_(
                (RecordLink.from_uuid == a.uuid) & (RecordLink.to_uuid == b.uuid) & (RecordLink.relation == relation),
                (RecordLink.from_uuid == b.uuid) & (RecordLink.to_uuid == a.uuid) & (RecordLink.relation == inverse),
            ))).scalar_one_or_none()
            if existing is None:
                s.add(RecordLink(from_uuid=a.uuid, to_uuid=b.uuid, relation=relation))
                self._journal(s, a.uuid, "link", author, None, {"record": b.number, "relation": relation, "action": "added"})
                self._journal(s, b.uuid, "link", author, None, {"record": a.number, "relation": inverse, "action": "added"})
                s.commit()
            return self._detail(s, a)

    def unlink_records(self, from_ref: str, to_ref: str, relation: str, *, author: str | None = None) -> dict:
        self._require_ready()
        inverse = RECORD_LINK_RELATIONS.get(relation, relation)
        with get_session() as s:
            a, b = self._get(s, from_ref), self._get(s, to_ref)
            if a is None or b is None:
                raise NotFound("Unknown record")
            link = s.execute(select(RecordLink).where(or_(
                (RecordLink.from_uuid == a.uuid) & (RecordLink.to_uuid == b.uuid) & (RecordLink.relation == relation),
                (RecordLink.from_uuid == b.uuid) & (RecordLink.to_uuid == a.uuid) & (RecordLink.relation == inverse),
            ))).scalar_one_or_none()
            if link is None:
                raise NotFound("Unknown record link")
            s.delete(link)
            self._journal(s, a.uuid, "link", author, None, {"record": b.number, "relation": relation, "action": "removed"})
            self._journal(s, b.uuid, "link", author, None, {"record": a.number, "relation": inverse, "action": "removed"})
            s.commit()
            return self._detail(s, a)

    # ── Attachments ──────────────────────────────────────────────────────────

    def add_attachment(self, ref: str, filename: str, content_type: str, data: bytes,
                       *, author: str | None = None) -> dict:
        self._require_ready()
        if len(data) == 0:
            raise ItsmError("Empty file")
        if len(data) > self.max_attachment_bytes:
            raise ItsmError(f"File exceeds the limit of {self.max_attachment_bytes // (1024 * 1024)} MB")
        ctype = (content_type or "application/octet-stream").split(";")[0].strip().lower()
        if self.allowed_mime and ctype not in self.allowed_mime:
            raise ItsmError(f"Content type '{ctype}' is not allowed")
        name = att.safe_filename(filename)
        with get_session() as s:
            rec = self._get(s, ref)
            if rec is None:
                raise NotFound(f"Unknown record: {ref}")
            att_uuid = str(uuid4())
            path, digest = att.store_bytes(self.attachment_dir, rec.uuid, att_uuid, data)
            row = Attachment(uuid=att_uuid, record_uuid=rec.uuid, filename=name, content_type=ctype,
                             size=len(data), sha256=digest, storage_path=path, uploaded_by=author)
            s.add(row)
            self._journal(s, rec.uuid, "attachment", author, None,
                          {"attachment": att_uuid, "filename": name, "size": len(data), "action": "added"})
            s.commit()
            s.refresh(row)
            return row.to_dict()

    def get_attachment(self, ref: str, attachment_uuid: str) -> tuple[dict, Path]:
        self._require_ready()
        with get_session() as s:
            rec = self._get(s, ref)
            row = s.get(Attachment, attachment_uuid) if rec else None
            if rec is None or row is None or row.record_uuid != rec.uuid:
                raise NotFound("Unknown attachment")
            return row.to_dict(), Path(row.storage_path)

    def delete_attachment(self, ref: str, attachment_uuid: str, *, author: str | None = None) -> bool:
        self._require_ready()
        with get_session() as s:
            rec = self._get(s, ref)
            row = s.get(Attachment, attachment_uuid) if rec else None
            if rec is None or row is None or row.record_uuid != rec.uuid:
                return False
            path, name = row.storage_path, row.filename
            s.delete(row)
            self._journal(s, rec.uuid, "attachment", author, None,
                          {"attachment": attachment_uuid, "filename": name, "action": "removed"})
            s.commit()
        att.remove_file(path)
        return True

    # ── Stats ────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        self._require_ready()
        with get_session() as s:
            open_rows = s.execute(
                select(Record.record_type, Record.priority, func.count())
                .where(Record.closed_at.is_(None))
                .group_by(Record.record_type, Record.priority)
            ).all()
            total_all = s.execute(select(func.count()).select_from(Record)).scalar_one()
        by_type: dict[str, int] = {}
        by_priority: dict[str, int] = {}
        for rtype, prio, n in open_rows:
            by_type[rtype] = by_type.get(rtype, 0) + n
            by_priority[prio or "none"] = by_priority.get(prio or "none", 0) + n
        return {"open": sum(by_type.values()), "total": total_all,
                "open_by_type": by_type, "open_by_priority": by_priority}

    # ── Events ───────────────────────────────────────────────────────────────

    def _announce_entity(self, detail: dict) -> None:
        """Register the record as an itsm_record entity (best effort)."""
        payload = {
            "uuid": detail["uuid"], "entity_type": "itsm_record",
            "display_name": f"{detail['number']} {detail['title']}"[:255],
            "identifiers": {"itsm.number": detail["number"]},
            "attributes": {"record_type": detail["record_type"], "state": detail["state"],
                           "priority": detail["priority"]},
            "source": SOURCE_ID,
        }
        try:
            self._emit("entity_registry:register", payload)
        except PermissionError:
            if not self._registry_warned:
                log.warning("ITSM: manifest lacks emit permission for entity_registry:register")
                self._registry_warned = True

    def _emit(self, topic: str, payload: dict) -> None:
        if not self._ctx:
            return
        # Service methods run in to_thread workers; async bus subscribers need
        # the loop thread, so marshal emits back onto it.
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        loop = self._loop
        if running is None and loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._ctx.emit, topic, payload)
            return
        self._ctx.emit(topic, payload)


itsm_service = ItsmService()
