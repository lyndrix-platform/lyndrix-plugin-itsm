"""Attachment storage — bytes on disk, metadata in ``itsm_attachments``.

Layout: ``<root>/<record uuid>/<attachment uuid>`` — the on-disk name is the
attachment UUID, never the user-supplied filename, so there is no path
traversal surface. The original filename is kept in the DB for downloads.

``storage_path`` in the DB is RELATIVE to the attachment root (since 0.2.0), so
every instance resolves it against its own mount of the same directory and
the root can move. Rows written by 0.1.x hold an absolute path; those are still
read as-is.

Cluster mode: every core instance must see the same directory (RWX volume /
NFS mount), otherwise a file uploaded through one instance is missing on the
others. Sharing is detected with marker files: each instance writes
``<root>/.cluster/<instance id>``; the directory counts as shared when at least
one OTHER live instance's marker is visible here.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._ \-()]+")
_MARKER_DIR = ".cluster"
_SAFE_ID = re.compile(r"[^A-Za-z0-9._-]+")

DEFAULT_ALLOWED_MIME = (
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml",
    "text/plain", "text/csv", "text/markdown", "application/json", "application/pdf",
    "application/zip", "application/x-yaml", "text/yaml", "application/octet-stream",
)


def safe_filename(name: str) -> str:
    """Display-safe filename (no separators, bounded length)."""
    base = Path(name or "file").name
    base = _UNSAFE.sub("_", base).strip(" .") or "file"
    return base[:200]


def store_bytes(root: Path, record_uuid: str, attachment_uuid: str, data: bytes) -> tuple[str, str]:
    """Write ``data`` and return ``(storage_path relative to root, sha256)``."""
    folder = root / record_uuid
    folder.mkdir(parents=True, exist_ok=True)
    (folder / attachment_uuid).write_bytes(data)
    return f"{record_uuid}/{attachment_uuid}", hashlib.sha256(data).hexdigest()


def resolve_path(root: Path, storage_path: str) -> Path:
    """Absolute file path for a stored ``storage_path``.

    Relative paths (0.2.0+) resolve against ``root`` and may not escape it;
    absolute legacy paths (0.1.x) are returned unchanged.
    """
    stored = Path(storage_path)
    if stored.is_absolute():
        return stored
    base = root.resolve()
    path = (base / stored).resolve()
    if base != path and base not in path.parents:
        raise ValueError(f"attachment path escapes the attachment directory: {storage_path}")
    return path


def remove_file(path: Path | str) -> None:
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


# ── Cluster sharing check ──────────────────────────────────────────────────────

def _marker_name(instance_id: str) -> str:
    return _SAFE_ID.sub("_", instance_id)[:200] or "_"


def write_marker(root: Path, instance_id: str) -> None:
    """Announce this instance in ``<root>/.cluster/<instance_id>`` (refreshes mtime)."""
    folder = root / _MARKER_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / _marker_name(instance_id)).write_text(instance_id)


def visible_markers(root: Path, instance_ids: list[str]) -> list[str]:
    """The given instance ids whose marker is visible under ``root``."""
    folder = root / _MARKER_DIR
    return [i for i in instance_ids if (folder / _marker_name(i)).is_file()]


def dir_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False
