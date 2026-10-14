"""Attachment storage — bytes on local disk, metadata in ``itsm_attachments``.

Layout: ``<root>/<record uuid>/<attachment uuid>`` — the on-disk name is the
attachment UUID, never the user-supplied filename, so there is no path
traversal surface. The original filename is kept in the DB for downloads.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

_UNSAFE = re.compile(r"[^A-Za-z0-9._ \-()]+")

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
    """Write ``data`` and return ``(storage_path, sha256)``."""
    folder = root / record_uuid
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / attachment_uuid
    path.write_bytes(data)
    return str(path), hashlib.sha256(data).hexdigest()


def remove_file(storage_path: str) -> None:
    try:
        Path(storage_path).unlink(missing_ok=True)
    except OSError:
        pass
