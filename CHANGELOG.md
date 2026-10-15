# Changelog

All notable changes to this plugin are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow the
manifest `version` and the matching git tag.

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
