# IT Service Management (ITSM) — Documentation

## Overview

The ITSM plugin brings incident, change, problem, request and task management to Lyndrix —
as its **own suite** (native mode) and, via connector adapters, as a **gateway** to existing
enterprise systems (BMC Helix ITSM, ServiceNow). The goal is to fit into an existing tool
landscape instead of being yet another tool: in gateway operation the external system stays the
"source of truth", while Lyndrix feeds and mirrors it.

Phase 1 (this state) delivers the **core functions** — deliberately modular so that later
extensions (event management, change approvals, connectors, SLAs) can attach without touching the
model.

**Guiding principle: the process is configuration.** What an incident or change *is* — status,
transitions, required fields, subtypes, categories, priority matrix — is defined in YAML
(`catalog/record_types/*.yml`), not in code. A new record type or an additional field requires
no code change.

## Architecture

```
catalog/record_types/*.yml   # process definitions (incident, change, problem, request, task)
app/model/models.py          # Record / JournalEntry / CiLink / RecordLink / Attachment / Counter
app/model/database.py        # session helpers + table bootstrap (hand-written migration)
app/logic/catalog.py         # validated catalog loader (pydantic, cross-reference checks)
app/logic/service.py         # CRUD, transitions, journal, links, attachments, events
app/logic/ingest.py          # base of the bus event handlers (dedupe, CI links, race handling, ingest_failed)
app/logic/monitoring_incidents.py  # monitoring outages → incidents / auto-resolve
app/logic/maintenance_tasks.py     # missing maintenance window → task
app/logic/texts.py           # generated texts from locales/itsm.<locale>.json (auto section)
app/logic/attachments.py     # file storage (UUID file names, no path traversal)
app/logic/connectors/        # ConnectorAdapter interface (gateway operation, later phase)
app/api/__init__.py          # REST router — both UIs use only this
app/ui/react/                # React UI (bundle → app/ui/static/ui_bundle.js)
```

### Data model

| Table | Content |
|---|---|
| `itsm_records` | one record per row: `uuid` (PK), `number` (`INC0000042`, unique), `record_type`, `subtype`, `category`, `title`, `description`, `state`, `impact`, `urgency`, `priority` (derived), `assignment_group`, `assignee`, `requester`, `created_by`, `custom_fields` (JSON), `tags` (JSON), `opened_at`/`resolved_at`/`closed_at`/`due_at`, `version` (optimistic locking), gateway fields `source`/`external_id`/`external_url`/`sync_state`/`sync_error` |
| `itsm_journal` | append-only history: `comment`, `work_note`, `state_change`, `field_change`, `link`, `attachment`, `system` — every change leaves an entry (audit trail) |
| `itsm_ci_links` | record ↔ configuration item (UUID from the Entity Registry) with role `affected` / `caused_by` / `changed` / `related`; display name/type are cached so the UI also works without the registry |
| `itsm_record_links` | record ↔ record (`parent_of`, `caused_by`, `implements`, `duplicate_of`, `related_to` + inverses). Since 0.4.0 unique per pair via `pair_key` (on the DB side; existing inverse duplicates are removed during migration, the oldest row stays) |
| `itsm_record_tags` | since 0.4.0 indexed mirror of `itsm_records.tags` (one entry per tag); `?tag=` filters via it. Backfilled in batches on first start |
| `itsm_ingest_tombstones` | since 0.4.0 short-lived markers for bus events that were overtaken by their counterpart (e.g. `outage_recovered` before `outage_persisted`, 24 h lifetime) |
| `itsm_attachments` | metadata; the bytes are stored under `<attachment_dir>/<record uuid>/<attachment uuid>` |
| `itsm_counters` | number range per type, incremented under a row lock in the same transaction as the insert |

### Rules enforced by the service

- **Transitions** only along the `transitions` of the catalog → otherwise HTTP 409. Fields that are
  changed together with a transition are recorded in the journal with old and new value
  (`field_change`) and trigger `itsm:record_updated` (since 0.4.0; this also makes the assignment
  notification fire when the assignee is set during a status change).
- **Links** are unique per pair; a link created twice at the same time (records or CIs) is
  idempotent instead of HTTP 500; `DELETE` removes all matching rows.
- **Required fields** of a target status (`requires`) must be present — either already on the
  record or in the `fields` part of the transition → otherwise HTTP 400 with a list of the missing
  fields.
- **Priority** is derived from `impact × urgency` via the matrix and recalculated on change; it can
  never be set directly.
- **Optimistic locking**: `PATCH` and transitions may send `version`; if it does not match →
  HTTP 409. Every change increments `version`.
- **Status only via transition** — `PATCH` with `state` is rejected.
- Timestamps follow the status `kind`: `resolved` sets `resolved_at`, `closed` sets `closed_at`,
  a change back into an `open` status clears both.
- Custom fields are type-checked (`text` / `select` / `int` / `bool` / `datetime`); unknown fields
  are rejected.
- Every new record is registered as an entity `itsm_record` with the Entity Registry
  (`entity_registry:register`, best effort).

## Catalog schema (`catalog/record_types/<type>.yml`)

```yaml
id: incident                 # type ID (snake_case)
label: Incident
prefix: INC                  # number prefix, unique across all types
number_width: 7              # INC0000042
icon: report_problem         # Material Icon
mode: native                 # native | gateway:<connector id>
description: ...
subtypes: [{id: outage, label: Outage}, ...]
categories:                  # arbitrarily nested
  - {id: network, label: Network, children: [{id: dns, label: DNS}]}
priority:
  impact:  [{id: 1, label: High}, {id: 2, label: Medium}, {id: 3, label: Low}]
  urgency: [{id: 1, label: High}, {id: 2, label: Medium}, {id: 3, label: Low}]
  defaults: {impact: 3, urgency: 3}
  matrix: {"1-1": P1, "1-2": P2, ...}          # "<impact>-<urgency>" → level
  levels: [{id: P1, label: Critical, color: down}, ...]   # color = theme state token
states:
  - {id: new, label: New, initial: true, kind: open}      # exactly one initial
  - {id: resolved, label: Resolved, kind: resolved, requires: [resolution_code, resolution_notes]}
  - {id: closed, label: Closed, kind: closed, final: true}
transitions:
  new: [in_progress, resolved, cancelled]
  resolved: [closed, in_progress]
fields:                      # custom fields → custom_fields JSON
  - {id: resolution_code, label: Resolution code, kind: select, options: [{id: fixed, label: Fixed}]}
  - {id: resolution_notes, label: Resolution notes, kind: text, multiline: true}
assignment_groups:
  - {id: infra, label: Infrastructure, core_group: INT_ADMIN}
```

The loader checks cross-references (transitions → states, `requires` → fields or core fields
`assignee`/`assignment_group`/`category`/`subtype`/`description`, matrix → levels) and the
uniqueness of IDs and prefixes. A defective catalog prevents the plugin from starting, or, on
`reload`, the old state stays active (HTTP 400).

Shipped: `incident`, `change` (standard/normal/emergency, risk, plan/rollback fields, status
`approval` as a placeholder for the later approval logic), `problem` (known error),
`request`, `task`.

Fields that the plugin fills itself (sections *Automatic incidents* and *Tasks from maintenance
events*): `incident` has the resolution code `auto_recovered` and the custom fields
`monitor_rule` (rule as JSON), `outage_id`, `ci_kind`; `task` has the category `maintenance` and
the custom field `source_ref`. A custom catalog (`catalog_dir`) should keep them — if
`auto_recovered` is missing, auto-resolve only leaves a comment.

**Note on categories:** an empty `categories` list allows any category; as soon as the list has
entries (since 0.3.0 for `task`), only these IDs are valid — a record without a category remains
allowed.

## REST API

Base: `/api/plugins/lyndrix.plugin.itsm/`. Permissions: `plugin:lyndrix.plugin.itsm:api:read`,
`api:write`, `itsm:admin`. `{ref}` is a UUID **or** a number (`INC0000042`).

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/records?type=&state=&priority=&assignee=&group=&tag=&ci=&q=&include_closed=&limit=&offset=` | list (closed records hidden by default). Since 0.4.0 filtering, counting and paging run in SQL; `limit` is capped at 1000, the response shape is unchanged |
| `POST` | `/records` | create → `201 {record}` |
| `GET` | `/records/{ref}` | detail incl. `allowed_transitions`, `journal`, `ci_links`, `links`, `attachments` |
| `PATCH` | `/records/{ref}` | change fields; optional `version` |
| `POST` | `/records/{ref}/transition` | `{to_state, fields?, note?, version?}` |
| `POST` | `/records/{ref}/journal` | `{kind: comment\|work_note, body}` |
| `POST` / `DELETE` | `/records/{ref}/ci-links[/{role}/{entity_uuid}]` | CI links. When creating, `entity_uuid` **or** `ci` (any reference the Entity Registry resolves: UUID, `iac.host:docker-hydra`, bare identifier value `docker-hydra`, display name) plus `role` (`affected`, `caused_by`, `changed`, `related`) |
| `POST` / `DELETE` | `/records/{ref}/links[/{relation}/{to}]` | record links (`to` = UUID or number) |
| `POST` | `/records/{ref}/attachments` | multipart upload (`file`), size/MIME limit |
| `GET` / `DELETE` | `/records/{ref}/attachments/{uuid}` | download / delete |
| `GET` | `/catalog` | all type definitions + relation/role lists |
| `POST` | `/catalog/reload` | reload catalog (`itsm:admin`) |
| `GET` | `/stats` | open records per type and priority |

Error codes: 400 invalid input / missing required fields, 404 unknown, 409 disallowed transition
or outdated `version`, 503 database not ready yet.

`source`/`external_id` on creation (gateway key of an external system) may only be set by
administrators or connectors with `itsm:admin` — an agent cannot pose as BMC this way (otherwise
403).

**CI resolution.** Every CI link — in `ci_links` on creation, afterwards via `/ci-links` or via the
bus — goes through the Entity Registry (`ctx.request("entity_registry:lookup", …)`): a name such as
`docker-hydra` becomes a UUID, display name and type are cached from the registry, a merged UUID
points to the surviving entity, and unknown or ambiguous references are rejected with 400 (the
candidates are listed in the error message). If the registry is unreachable, a raw UUID is still
accepted (with the name supplied), a name alone is not. The list filters with `?ci=<uuid>` on all
records for a CI.

## Event bus

### Consumed

| Topic | Payload | Response |
|---|---|---|
| `itsm:record_create` | record fields + `author`, optional `request_id` | `itsm:record_create_result {request_id, ok, record\|error}` |
| `itsm:record_update` | `{ref, changes, author, version?, request_id?}` | `itsm:record_update_result` |
| `itsm:transition` | `{ref, to_state, fields?, note?, author, version?, request_id?}` | `itsm:transition_result` |
| `itsm:journal_add` | `{ref, kind, body, author}` | — |
| `itsm:ci_link` | `{ref, ci \| entity_uuid, role?, author, request_id?}` — `ci` is any reference the registry resolves (`docker-hydra`, UUID, `schema:value`) | `itsm:ci_link_result {request_id, ok, record, ci}` |

With `ctx.request(...)` (core API ≥ 1.4) these become round trips; errors without `request_id`
end up on `itsm:ingest_failed`. The IaC change bridge (prod deploy → change approval) will be
connected here later.

Events from other plugins under the maintenance window contract (core `docs/plugins.md` →
*Maintenance windows*) that the plugin turns into records **without** request/reply:

| Topic | Emitter | Effect |
|---|---|---|
| `monitoring:outage_persisted` | monitoring, once per outage | create incident (see *Automatic incidents*) |
| `monitoring:outage_recovered` | monitoring | auto-resolve or comment on the incident |
| `maintenance:window_missing` | IaC orchestrator | task "Define maintenance window" (see *Tasks from maintenance events*) |

Invalid payloads (missing required fields, `ci_id` without 36 characters) are reported on
`itsm:ingest_failed {topic, error}`, never as an exception. If an event arrives during boot before
the tables are ready, the handler waits up to 30 s.

### Emitted

`itsm:record_created {record}`, `itsm:record_updated {record, changes}`,
`itsm:state_changed {record, from_state, to_state}`, `itsm:journal_added {record_uuid, number,
entry}`, `itsm:ingest_failed {topic, error}`, `itsm:ci_link_result` (response),
`entity_registry:register` and `entity_registry:lookup` (request for CI resolution, response on
`entity_registry:lookup_result`).

### Notifications

The endpoints `record_created` (only automatically created P1/P2 incidents, payload with a `link`
to the record), `record_assigned`, `record_state_changed`, `record_commented` are delivered via the
core notification router (users control toast/history/external channels in the settings). Manually
created records do not produce a notification on creation.

## Automatic incidents from monitoring (since 0.3.0)

The monitoring plugin reports an outage **once** when a configuration item has been unreachable
outside a maintenance window for longer than the `offline_minutes` of its incident rule
(`monitoring:outage_persisted`), and once when the monitor responds again
(`monitoring:outage_recovered`). The ITSM plugin needs no configuration for this — the rule
(`offline_minutes`, `severity`, `auto_resolve`) comes from `service.yml` or the `incident_defaults`
of the iac-controller and travels with the event.

**Payload `outage_persisted`:** `{outage_id, ci_id, kind, name, host_ci_id?, monitor_id, since,
persisted_at, duration_s, rule: {offline_minutes, severity, auto_resolve}, error?}`;
`outage_id = "<ci_id>:<since>"` is deterministic and is stored as `external_id`.

This is how the incident is created:

| Field | Value |
|---|---|
| `record_type` / `subtype` / `category` | `incident` / `outage` / `monitoring` |
| `source` / `external_id` | `monitoring` / `outage_id` |
| `impact` × `urgency` → priority | `critical → 1/1 (P1)`, `high → 1/2 (P2)`, `medium → 2/2 (P3)`, `low → 3/2 (P4)`; unknown severity counts as `high` |
| `title` | "`<name>`: unreachable since `<since>`" |
| `description` | monitor, CI, host, rule, duration, last error |
| `tags` | `auto`, `monitoring`, `<kind>` (Entity Registry kind, e.g. `service`, `host`) |
| `custom_fields` | `monitor_rule` (rule as JSON), `outage_id`, `ci_kind` |
| CI links | `ci_id` as `affected`, `host_ci_id` as `related` — names/types from the Entity Registry; without the registry (or if it does not yet know the CI) raw UUID links |
| `created_by` / `requester` | `monitoring` |

**Parallel events.** Multiple `outage_persisted` events for the same CI are serialized per `ci_id`
within an instance. If another instance has simultaneously created an incident as well, the oldest
open monitoring incident of the CI survives; the duplicate is cancelled with a reference to it and
the outage is noted as a comment on the survivor (since 0.4.0).

**Dedupe.** (1) Per outage: if `(monitoring, outage_id)` already exists, nothing happens — even if
another cluster instance created the record between check and insert (`UNIQUE(source, external_id)`,
the winner's record applies). (2) Per CI: if the CI already has an **open** monitoring incident
(status with kind `open`: `new`, `in_progress`, `on_hold`), that incident receives a work note
"Renewed outage reported …" instead of a second record. `resolved` does **not** count as open: a new
outage after resolution creates a **new** incident; nothing is reopened (decision F).

**Recovery.** `outage_recovered {outage_id, ci_id, since, recovered_at, duration_s}` looks up the
incident via `outage_id`:

- Rule `auto_resolve: true` (from `custom_fields.monitor_rule`, not from the payload) →
  transition to `resolved` with `resolution_code: auto_recovered` and generated
  `resolution_notes`; notification `record_state_changed` as with every transition.
- `auto_resolve: false` → comment "Monitor reachable again …" (triggers `record_commented`).
- Transition not permitted by the catalog (e.g. from `on_hold`) → comment, status stays.
- Concurrent change (version conflict) → re-read once and retry; if someone has resolved or closed
  it in the meantime, nothing more happens; otherwise a log entry.
- If `outage_recovered` overtakes its `outage_persisted` (incident does not exist yet), the plugin
  remembers the end for 24 h and closes the incident right after creating it (since 0.4.0).
- Already `resolved`/`closed`/`cancelled` → nothing. Unknown `outage_id` → nothing, except if the
  CI has an open monitoring incident with a work note about this outage (dedupe 2) — then the end is
  noted there too.

**Notification.** Automatically created incidents with P1/P2 produce `record_created`
(severity `warning`, payload `link` `/apps/lyndrix-plugin-itsm/itsm?record=<uuid>`).

**Texts.** Title, description, notes and resolution text come from `locales/itsm.<locale>.json`,
section `auto`, in the language of the setting `auto_text_locale` (default `de`, fallback English).
Timestamps are in UTC.

## Tasks from maintenance events (since 0.3.0)

If a service has `deploy_policy: window` but there is no maintenance window within the planning
horizon (default 30 days), the IaC orchestrator deploys immediately (with a warning) and sends
`maintenance:window_missing {event_id, ci_id, kind, name, host_ci_id?, site, stage,
horizon_days, job_id, source, detected_at, reason}` (decision H). From this a **task** is created so
that the gap in the SSoT gets closed:

| Field | Value |
|---|---|
| `title` | "Define maintenance window: `<name>`" |
| `description` | service, site/stage, horizon, job, time, reason and the instructions (window in `service.yml` → `maintenance.windows` or in the controller → host window / `stage_vars.maintenance_default`) |
| Priority | P4 (`impact 3`, `urgency 2`) |
| `category` / `assignment_group` | `maintenance` / `infra` |
| `tags` | `auto`, `maintenance`, `<kind>` |
| `custom_fields` | `source_ref` = `event_id`, `due` = `detected_at` + 14 days |
| `source` / `external_id` | `maintenance` / `event_id` (`window_missing:<ci_id>:<job_id>`) |
| CI links | service `affected`, host `related` |

Dedupe as with the incidents: per event via `(maintenance, event_id)` and per CI — an open task
(`open`, `in_progress`) receives the work note "Renewed deploy without maintenance window: job
…", a `done`/`cancelled` task is not reopened (the next deploy without a window creates a new one).
No notification; the task appears in the list with the tag `auto` (filter "automatic only").

## Settings (plugin settings dialog, stored in Vault)

| Key | Default | Meaning |
|---|---|---|
| `attachment_dir` | `/data/storage/itsm/attachments` | storage location for attachments |
| `max_attachment_mb` | `25` | size limit per file |
| `catalog_dir` | empty (= shipped catalog) | own directory with `record_types/*.yml` — **replaces** the shipped set |
| `auto_text_locale` | `de` | language (`de`/`en`) of the texts the plugin writes itself (incidents from monitoring, tasks from maintenance events) |

The settings are applied after `vault:ready_for_data` and on every `plugin:settings_changed`
(core ≥ 1.5.1, distributed to all instances) — saving in the settings dialog therefore takes effect
immediately on every instance. If `catalog_dir` is set but does not exist on this instance, the
last loaded catalog stays active and `health()` reports `degraded` (`catalog_dir_missing`).

## Cluster operation (since 0.2.0)

With several core instances (`LYNDRIX_CLUSTER_MODE=cluster`) all routes run on every instance
against the shared MariaDB.

- **Attachments need shared storage.** `attachment_dir` must be the same directory on **all**
  instances (RWX volume or NFS mount, in k3s e.g. `/data/storage` as an RWX PVC). Otherwise a file
  uploaded via instance A is missing on instance B (download → 410). A Docker host runner with its
  own `/data/storage` does **not** share the directory.
- **Detection:** every instance writes `<attachment_dir>/.cluster/<instance_id>`. If an instance sees
  the marker of at least one other live instance, the directory counts as shared. If other instances
  are active but none of their markers are visible, `health()` and the runtime status report
  `degraded` (`attachment_dir_not_shared`, banner, non-blocking). Without live neighbours the state
  remains "unknown" (not `degraded`). The check runs every 60 s in the background in its own thread
  (at most one at a time); `health()` only reads the result. If the directory does not respond for
  longer than 10 s (hanging NFS/RWX mount), `health()` reports `degraded` (`attachment_dir_unresponsive`).
- **Paths:** since 0.2.0 `storage_path` is stored **relative** to `attachment_dir`
  (`<record>/<attachment>`); absolute paths of older records are still read.
- **Number assignment** runs in its own short READ-COMMITTED transaction
  (`UPDATE … SET last_value = last_value + 1`); the insert is retried on MariaDB 1020/1205/1213.
  Numbers are unique but not gapless.
- **Optimistic locking** via a conditional `UPDATE … WHERE uuid = :u AND version = :v`.
  A lost race (or 1020/1205/1213) results in `VersionConflict` → HTTP 409; without a sent
  `version` it is retried once with fresh data.
- **Catalog:** `POST /catalog/reload` sends `itsm:catalog_reloaded` with `cluster=True`; the other
  instances reload as well.
- **Bootstrap:** tables are created under `db_instance.advisory_lock("lyndrix_bootstrap")` in the
  thread pool; "already exists" (1050/1060/1061) counts as success, failures are retried.
- **`health()`** counts only open records; details per type/priority are provided by `GET /stats`.

## Roles

| Role | Permissions | Auto-mapping |
|---|---|---|
| `admin` | `api:read`, `api:write`, `itsm:admin` | group `INT_ADMIN` |
| `agent` | `api:read`, `api:write` | — |
| `viewer` | `api:read` | — |

## React UI

Route `/itsm` (in the shell `/apps/lyndrix-plugin-itsm/itsm`). Deep links: `?record=<uuid|number>`
opens a record, `?new=incident&ci=<uuid>` opens the new-record form with the CI prefilled as
*affected* (this is how Server Manager and Entity Registry link "Report incident"), `?ci=<uuid>`
lists all records for a CI.

- **List**: search, filter by type/status/priority, "show closed", "automatic only" (tag `auto`:
  incidents and tasks created by the plugin), CI filter chip; tags as chips, `auto` highlighted
  (also in the detail header).
- **New**: the form is generated from the catalog (subtype, category tree, impact/urgency with
  priority preview, group, custom fields, CI picker).
- **CI picker** (new and detail): type a name or identifier (e.g. `docker-hydra`) or paste a
  UUID — the Entity Registry resolves it and the hit (type, name, identifier) is shown **before**
  linking; Enter takes the first hit, CIs already linked are marked. An unknown UUID is rejected;
  only if the registry is unreachable can a raw UUID be linked.
- **Detail**: header with **only the permitted transitions** as buttons; the transition dialog asks
  for the required fields of the target status; editing with `version`; history with
  comment/work note; CIs (link to the registry, "all records for this CI"), linked records (by
  number), attachments (upload/download).
- **Process catalog**: read-only view of all types, statuses and transitions; "reload" for admins.

## Gateway operation (outlook)

`app/logic/connectors/base.py` defines `ConnectorAdapter` (`create`, `update`, `add_journal`,
`fetch`, `handle_webhook`) and a registry. A type with `mode: gateway:bmc_helix` would delegate
creation and changes to the connector, while the local copy carries `source`/`external_id`/
`sync_state` — the same pattern as the write-back of the Server Manager. The first connector (BMC
Helix: AR-JWT, `HPD:IncidentInterface_Create`, `CHG:ChangeInterface_Create`) follows in Phase 3.

## Development & tests

```bash
npm install && npm run build          # React bundle
ruff check entrypoint.py app tests

docker cp . lyndrix-core-dev:/tmp/itsm
docker exec -w /tmp/itsm -e PYTHONPATH=/app:/tmp/pytest-sm:/tmp/itsm \
  -e LYNDRIX_CLUSTER_MODE=single -e LYNDRIX_COORDINATOR=local \
  lyndrix-core-dev python -m pytest -q tests
```

The service tests (SQLite in memory, fixture `svc` in `tests/conftest.py`, doubles in
`tests/fakes.py`) cover number assignment, the priority matrix, catalog validation, disallowed
transitions, required fields, reopening, optimistic locking, journal, links in both directions,
attachments with limits, and the ingest key. `test_monitoring_incidents.py` and
`test_maintenance_tasks.py` run the bus events of the maintenance-window contract against the real
service (priority mapping, both dedupe levels, auto-resolve incl. `on_hold` and version-conflict
cases, registry unreachable, lost insert race, new incident after `resolved`, task creation/note/
re-creation).
