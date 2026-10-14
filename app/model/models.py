"""SQLAlchemy models for the ITSM plugin.

One generic record engine: every record type (incident, change, problem,
request, task, …) is a row in ``itsm_records``; what differs per type lives
in the catalog (states, fields) and in the ``custom_fields`` JSON.

Record      — the ticket. ``version`` is an optimistic-locking counter: a
              PATCH must send the version it read, or it is rejected (409).
JournalEntry— append-only timeline: comments, work notes, state and field
              changes, links. Never updated or deleted → the audit trail.
CiLink      — record ↔ configuration item (Entity Registry UUID). The display
              name/type are cached so the UI works while the registry is down.
RecordLink  — record ↔ record (parent/child, caused_by, implements, …).
Attachment  — file metadata; bytes live on disk under the configured dir.
Counter     — per-type sequence for human numbers (INC0000042); incremented
              under a row lock inside the create transaction.
"""
from __future__ import annotations

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase


class _Base(DeclarativeBase):
    pass


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


class Record(_Base):
    __tablename__ = "itsm_records"
    __table_args__ = (
        # Idempotency key for records pushed in by a gateway/connector; native
        # records keep both NULL (NULLs never collide in a unique index).
        UniqueConstraint("source", "external_id", name="uq_itsm_source_ext"),
    )

    uuid = Column(String(36), primary_key=True)
    number = Column(String(32), nullable=False, unique=True, index=True)
    record_type = Column(String(32), nullable=False, index=True)
    subtype = Column(String(64), nullable=True)
    category = Column(String(64), nullable=True)

    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    state = Column(String(32), nullable=False, index=True)
    impact = Column(Integer, nullable=True)
    urgency = Column(Integer, nullable=True)
    priority = Column(String(8), nullable=True, index=True)  # derived from the matrix

    assignment_group = Column(String(64), nullable=True, index=True)
    assignee = Column(String(255), nullable=True, index=True)
    requester = Column(String(255), nullable=True)
    created_by = Column(String(255), nullable=True)

    custom_fields = Column(JSON, nullable=False, default=dict)
    tags = Column(JSON, nullable=False, default=list)

    opened_at = Column(DateTime, nullable=False, default=func.now())
    resolved_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    due_at = Column(DateTime, nullable=True)  # SLA target — filled by a later phase
    version = Column(Integer, nullable=False, default=1)

    # Gateway preparation (unused in native mode): where the system of record
    # keeps this ticket and how our copy relates to it.
    source = Column(String(64), nullable=True)
    external_id = Column(String(255), nullable=True)
    external_url = Column(String(512), nullable=True)
    sync_state = Column(String(16), nullable=True)  # NULL | pending | failed
    sync_error = Column(Text, nullable=True)

    created_at = Column(DateTime, nullable=False, default=func.now())
    updated_at = Column(DateTime, nullable=False, default=func.now(), onupdate=func.now())

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "number": self.number,
            "record_type": self.record_type,
            "subtype": self.subtype,
            "category": self.category,
            "title": self.title,
            "description": self.description,
            "state": self.state,
            "impact": self.impact,
            "urgency": self.urgency,
            "priority": self.priority,
            "assignment_group": self.assignment_group,
            "assignee": self.assignee,
            "requester": self.requester,
            "created_by": self.created_by,
            "custom_fields": dict(self.custom_fields or {}),
            "tags": list(self.tags or []),
            "opened_at": _iso(self.opened_at),
            "resolved_at": _iso(self.resolved_at),
            "closed_at": _iso(self.closed_at),
            "due_at": _iso(self.due_at),
            "version": self.version,
            "source": self.source,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "sync_state": self.sync_state,
            "sync_error": self.sync_error,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
        }


class JournalEntry(_Base):
    __tablename__ = "itsm_journal"

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_uuid = Column(String(36), ForeignKey("itsm_records.uuid"), nullable=False, index=True)
    # comment (visible to requester) | work_note (internal) | state_change |
    # field_change | link | attachment | system
    kind = Column(String(32), nullable=False)
    author = Column(String(255), nullable=True)
    body = Column(Text, nullable=True)
    data = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, nullable=False, default=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "record_uuid": self.record_uuid,
            "kind": self.kind,
            "author": self.author,
            "body": self.body,
            "data": dict(self.data or {}),
            "created_at": _iso(self.created_at),
        }


class CiLink(_Base):
    __tablename__ = "itsm_ci_links"
    __table_args__ = (
        UniqueConstraint("record_uuid", "entity_uuid", "role", name="uq_itsm_ci_link"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    record_uuid = Column(String(36), ForeignKey("itsm_records.uuid"), nullable=False, index=True)
    entity_uuid = Column(String(36), nullable=False, index=True)
    role = Column(String(32), nullable=False)  # affected | caused_by | changed | related
    display_name = Column(String(255), nullable=True)
    entity_type = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "entity_uuid": self.entity_uuid,
            "role": self.role,
            "display_name": self.display_name,
            "entity_type": self.entity_type,
            "created_at": _iso(self.created_at),
        }


class RecordLink(_Base):
    __tablename__ = "itsm_record_links"
    __table_args__ = (
        UniqueConstraint("from_uuid", "to_uuid", "relation", name="uq_itsm_record_link"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    from_uuid = Column(String(36), ForeignKey("itsm_records.uuid"), nullable=False, index=True)
    to_uuid = Column(String(36), ForeignKey("itsm_records.uuid"), nullable=False, index=True)
    relation = Column(String(32), nullable=False)
    created_at = Column(DateTime, nullable=False, default=func.now())

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "from_uuid": self.from_uuid,
            "to_uuid": self.to_uuid,
            "relation": self.relation,
            "created_at": _iso(self.created_at),
        }


class Attachment(_Base):
    __tablename__ = "itsm_attachments"

    uuid = Column(String(36), primary_key=True)
    record_uuid = Column(String(36), ForeignKey("itsm_records.uuid"), nullable=False, index=True)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(128), nullable=False)
    size = Column(Integer, nullable=False)
    sha256 = Column(String(64), nullable=False)
    storage_path = Column(String(512), nullable=False)
    uploaded_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False, default=func.now())

    def to_dict(self) -> dict:
        return {
            "uuid": self.uuid,
            "record_uuid": self.record_uuid,
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "sha256": self.sha256,
            "uploaded_by": self.uploaded_by,
            "created_at": _iso(self.created_at),
        }


class Counter(_Base):
    __tablename__ = "itsm_counters"

    record_type = Column(String(32), primary_key=True)
    last_value = Column(Integer, nullable=False, default=0)
