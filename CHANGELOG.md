# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow the
manifest `version` and the matching git tag.

## [0.3.0] - 2026-10-18

Maintenance-windows contract (core `docs/plugins.md` → *Maintenance windows*,
`features/maintenance-windows/00-ergebnis-p0.md`): incidents from monitoring
outages, tasks for services without a maintenance window. No dependency on
`core.api.maintenance` — payload dicts are parsed here, `min_core_version`
stays `1.4.0`.

### Added
- `monitoring:outage_persisted` → incident (`subtype: outage`, `category:
  monitoring`, `source="monitoring"`, `external_id=outage_id`, author
  `monitoring`, tags `auto`/`monitoring`/`<kind>`): priority from the rule's
  severity (`critical → P1`, `high → P2`, `medium → P3`, `low → P4`, unknown →
  `high`), service CI `affected`, host CI `related`, custom fields
  `monitor_rule` (JSON), `outage_id`, `ci_kind`. Dedupe per outage
  (`UNIQUE(source, external_id)`, a lost insert race uses the peer's record)
  and per CI (an open monitoring incident gets a work note instead of a second
  record). A new outage after `resolved` opens a new incident (no reopen).
- `monitoring:outage_recovered` → auto-resolve with the new catalog code
  `resolution_code: auto_recovered` when the stored rule says
  `auto_resolve: true`; a comment instead when it says `false`, when the
  catalog forbids the transition (`on_hold`) or lacks the code; a repeated
  version conflict is re-read once and then given up with a log line. The end
  of an outage that was only annotated (dedupe per CI) is annotated as well.
- `maintenance:window_missing` → task "Wartungsfenster definieren: <service>"
  (`source="maintenance"`, `external_id=event_id`, P4, category `maintenance`,
  group `infra`, custom fields `source_ref`, `due` = detected + 14 days, CI
  links `affected`/`related`); deduped per event and per open task on the CI
  (work note), a `done`/`cancelled` task is not reopened.
- Notification endpoint `record_created`: a P1/P2 incident the plugin opened
  itself (payload carries `link` to the record). Manual records stay silent.
- Setting `auto_text_locale` (`de` | `en`, default `de`): language of the
  generated titles, descriptions and journal entries (section `auto` of the
  plugin locales).
- Service lookups for ingest handlers: `find_by_external`, `find_open_for_ci`
  (open = state kind `open`, not `closed_at IS NULL`), `open_state_names`;
  `DuplicateRecord` (an `ItsmError`) for the `(source, external_id)` collision.
- UI: tag chips in list and detail, `auto` highlighted; filter "automatic only"
  (`tag=auto`).

### Changed
- Catalog `task.yml`: category `maintenance` (the previously empty list
  accepted any category; now only listed ids are valid, no category is still
  fine), custom field `source_ref`. `incident.yml`: resolution code
  `auto_recovered`, custom fields `monitor_rule`, `outage_id`, `ci_kind`.
- Manifest subscribes to `monitoring:outage_persisted`,
  `monitoring:outage_recovered`, `maintenance:window_missing` and its own
  `itsm:record_created`.
- Test doubles moved to `tests/fakes.py`, fixture `svc` to `tests/conftest.py`.

## [0.2.0] - 2026-10-15

Cluster readiness (core cluster mode, N instances on one MariaDB 12).

### Fixed
- Table bootstrap runs under `db_instance.advisory_lock("lyndrix_bootstrap")`
  (when the core offers it), off the event loop, and treats "already exists"
  (1050/1060/1061) as a peer's success; a failed bootstrap is retried.
- Record numbers are allocated in their own short READ COMMITTED transaction
  (atomic counter increment); `create_record` retries on MariaDB 1020/1205/1213
  as well as on unique-key collisions.
- Optimistic locking is enforced by the database: a conditional
  `UPDATE … WHERE version = :v`; a miss (or 1020/1205/1213) raises
  `VersionConflict` (HTTP 409). Without `expected_version` the write is retried
  once on fresh data.
- An attachment file is removed again when the DB commit after writing it fails.

### Changed
- `itsm_attachments.storage_path` is stored relative to `attachment_dir`;
  absolute paths of existing rows are still read.
- In cluster mode the attachment directory must be shared (RWX/NFS). Each
  instance writes `<attachment_dir>/.cluster/<instance_id>`; when other live
  instances exist but none of their markers is visible, `health()` and the
  runtime status report `degraded` (non-blocking).
- Settings are re-applied on `plugin:settings_changed` (core ≥ 1.5.1) on every
  instance; a configured but missing `catalog_dir` reports `degraded`; clearing
  it returns to the built-in catalog.
- `POST /catalog/reload` announces `itsm:catalog_reloaded` cluster-wide; peers
  reload their catalog.
- `health()` caches the attachment-dir probe for 60 s and runs one count
  (open records) instead of a GROUP BY plus a full count.

## [0.1.0] - 2026-10-14

### Added
- Initial release: catalog-driven record engine (incident, change, problem,
  request, task), journal, CI and record links, attachments, REST and bus API,
  React UI.
