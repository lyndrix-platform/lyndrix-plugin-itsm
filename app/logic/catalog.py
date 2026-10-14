"""Record-type catalog — the ITSM process definitions, loaded from YAML.

``catalog/record_types/*.yml`` each define one record type: numbering,
subtypes, categories, priority matrix, states + transitions, custom fields
and assignment groups. The loader validates the whole thing with pydantic
and cross-checks references (transitions → states, requires → fields,
matrix → levels) so a broken process definition fails at boot, not when an
agent tries to resolve a ticket.

Operators may point the plugin at their own catalog dir (setting
``catalog_dir``); a partial override is not supported — a dir replaces the
shipped set so there is exactly one place a process is defined.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

from core.api import get_logger

log = get_logger("Plugin:ITSM:Catalog")

DEFAULT_CATALOG_DIR = Path(__file__).resolve().parents[2] / "catalog"
RECORD_TYPES_SUBDIR = "record_types"

# Core (non-custom) field names a state may require.
CORE_REQUIRABLE_FIELDS = {"assignee", "assignment_group", "category", "subtype", "description"}

# Record ↔ record relations. Stored on the "from" side; the inverse is derived.
RECORD_LINK_RELATIONS: dict[str, str] = {
    "parent_of": "child_of",
    "child_of": "parent_of",
    "caused_by": "causes",
    "causes": "caused_by",
    "implements": "implemented_by",
    "implemented_by": "implements",
    "duplicate_of": "duplicates",
    "duplicates": "duplicate_of",
    "related_to": "related_to",
}

# Record ↔ configuration item roles.
CI_LINK_ROLES = ("affected", "caused_by", "changed", "related")

FieldKind = Literal["text", "select", "int", "bool", "datetime"]
StateKind = Literal["open", "resolved", "closed"]


class CatalogError(ValueError):
    """Catalog failed to load or validate."""


class Option(BaseModel):
    id: str | int
    label: str


class Category(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    children: list["Category"] = Field(default_factory=list)


class FieldDef(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    kind: FieldKind = "text"
    options: list[Option] = Field(default_factory=list)
    multiline: bool = False
    description: str = ""

    @model_validator(mode="after")
    def _select_needs_options(self):
        if self.kind == "select" and not self.options:
            raise ValueError(f"field '{self.id}': select needs options")
        return self


class StateDef(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    kind: StateKind = "open"
    initial: bool = False
    final: bool = False
    requires: list[str] = Field(default_factory=list)


class PriorityDef(BaseModel):
    impact: list[Option]
    urgency: list[Option]
    defaults: dict[str, int] = Field(default_factory=dict)
    matrix: dict[str, str]
    levels: list[Option]

    def level_ids(self) -> set[str]:
        return {str(lv.id) for lv in self.levels}


class AssignmentGroup(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    core_group: Optional[str] = None  # Lyndrix group auto-mapped to this queue


class RecordTypeDef(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$", max_length=32)
    label: str
    prefix: str = Field(pattern=r"^[A-Z]{2,6}$")
    number_width: int = Field(default=7, ge=4, le=10)
    icon: str = "assignment"
    mode: str = "native"  # native | gateway:<connector id>
    description: str = ""
    subtypes: list[Option] = Field(default_factory=list)
    categories: list[Category] = Field(default_factory=list)
    priority: Optional[PriorityDef] = None
    states: list[StateDef] = Field(min_length=1)
    transitions: dict[str, list[str]] = Field(default_factory=dict)
    fields: list[FieldDef] = Field(default_factory=list)
    assignment_groups: list[AssignmentGroup] = Field(default_factory=list)

    @model_validator(mode="after")
    def _cross_check(self):
        state_ids = [s.id for s in self.states]
        if len(set(state_ids)) != len(state_ids):
            raise ValueError("duplicate state id")
        initials = [s.id for s in self.states if s.initial]
        if len(initials) != 1:
            raise ValueError(f"exactly one initial state required, got {initials}")
        field_ids = {f.id for f in self.fields}
        if len(field_ids) != len(self.fields):
            raise ValueError("duplicate field id")
        for s in self.states:
            for req in s.requires:
                if req not in field_ids and req not in CORE_REQUIRABLE_FIELDS:
                    raise ValueError(f"state '{s.id}' requires unknown field '{req}'")
        for src, targets in self.transitions.items():
            if src not in state_ids:
                raise ValueError(f"transition from unknown state '{src}'")
            for tgt in targets:
                if tgt not in state_ids:
                    raise ValueError(f"transition {src} → unknown state '{tgt}'")
        if self.priority:
            levels = self.priority.level_ids()
            for key, lvl in self.priority.matrix.items():
                if lvl not in levels:
                    raise ValueError(f"priority matrix '{key}' → unknown level '{lvl}'")
        if not self.mode == "native" and not self.mode.startswith("gateway:"):
            raise ValueError(f"mode must be 'native' or 'gateway:<id>', got '{self.mode}'")
        return self

    # ── Lookups ──────────────────────────────────────────────────────────────

    def initial_state(self) -> StateDef:
        return next(s for s in self.states if s.initial)

    def state(self, state_id: str) -> StateDef | None:
        return next((s for s in self.states if s.id == state_id), None)

    def allowed_transitions(self, from_state: str) -> list[str]:
        return list(self.transitions.get(from_state, []))

    def field(self, field_id: str) -> FieldDef | None:
        return next((f for f in self.fields if f.id == field_id), None)

    def category_ids(self) -> set[str]:
        out: set[str] = set()

        def walk(cats: list[Category]):
            for c in cats:
                out.add(c.id)
                walk(c.children)

        walk(self.categories)
        return out

    def subtype_ids(self) -> set[str]:
        return {str(s.id) for s in self.subtypes}

    def group_ids(self) -> set[str]:
        return {g.id for g in self.assignment_groups}

    def derive_priority(self, impact: int | None, urgency: int | None) -> str | None:
        if not self.priority or impact is None or urgency is None:
            return None
        return self.priority.matrix.get(f"{impact}-{urgency}")

    def default_impact_urgency(self) -> tuple[int | None, int | None]:
        if not self.priority:
            return None, None
        d = self.priority.defaults
        return d.get("impact"), d.get("urgency")

    def validate_custom_fields(self, values: dict[str, Any]) -> dict[str, Any]:
        """Type-check custom field values; unknown keys are rejected."""
        out: dict[str, Any] = {}
        for key, value in (values or {}).items():
            fdef = self.field(key)
            if fdef is None:
                raise CatalogError(f"Unknown custom field '{key}' for {self.id}")
            if value is None or value == "":
                out[key] = None
                continue
            if fdef.kind == "select":
                if str(value) not in {str(o.id) for o in fdef.options}:
                    raise CatalogError(f"Field '{key}': '{value}' is not an allowed option")
                out[key] = str(value)
            elif fdef.kind == "int":
                try:
                    out[key] = int(value)
                except (TypeError, ValueError):
                    raise CatalogError(f"Field '{key}' must be an integer") from None
            elif fdef.kind == "bool":
                if isinstance(value, bool):
                    out[key] = value
                elif str(value).lower() in ("true", "1", "yes"):
                    out[key] = True
                elif str(value).lower() in ("false", "0", "no"):
                    out[key] = False
                else:
                    raise CatalogError(f"Field '{key}' must be a boolean")
            elif fdef.kind == "datetime":
                from datetime import datetime

                try:
                    datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                except ValueError:
                    raise CatalogError(f"Field '{key}' must be an ISO-8601 datetime") from None
                out[key] = str(value)
            else:
                out[key] = str(value)
        return out

    def to_payload(self) -> dict:
        return self.model_dump()


class CatalogLoader:
    def __init__(self, catalog_dir: Path | None = None) -> None:
        self.catalog_dir = Path(catalog_dir or DEFAULT_CATALOG_DIR)
        self._types: dict[str, RecordTypeDef] = {}
        self._loaded = False

    def load(self) -> dict[str, RecordTypeDef]:
        folder = self.catalog_dir / RECORD_TYPES_SUBDIR
        files = sorted(folder.glob("*.yml")) + sorted(folder.glob("*.yaml"))
        if not files:
            raise CatalogError(f"No record type definitions found in {folder}")
        types: dict[str, RecordTypeDef] = {}
        prefixes: dict[str, str] = {}
        for path in files:
            try:
                with path.open("r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}
                rt = RecordTypeDef.model_validate(raw)
            except (ValidationError, ValueError, yaml.YAMLError) as exc:
                raise CatalogError(f"{path.name}: {exc}") from exc
            if rt.id in types:
                raise CatalogError(f"{path.name}: duplicate record type id '{rt.id}'")
            if rt.prefix in prefixes:
                raise CatalogError(
                    f"{path.name}: prefix '{rt.prefix}' already used by '{prefixes[rt.prefix]}'"
                )
            types[rt.id] = rt
            prefixes[rt.prefix] = rt.id
        self._types = types
        self._loaded = True
        log.info(f"Catalog loaded from {folder}: {', '.join(sorted(types))}")
        return types

    def reload(self) -> dict[str, RecordTypeDef]:
        return self.load()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def types(self) -> dict[str, RecordTypeDef]:
        if not self._loaded:
            self.load()
        return self._types

    def record_type(self, type_id: str) -> RecordTypeDef:
        rt = self.types().get(type_id)
        if rt is None:
            raise CatalogError(f"Unknown record type '{type_id}'")
        return rt

    def to_payload(self) -> dict:
        return {
            "record_types": [rt.to_payload() for rt in self.types().values()],
            "record_link_relations": RECORD_LINK_RELATIONS,
            "ci_link_roles": list(CI_LINK_ROLES),
        }
