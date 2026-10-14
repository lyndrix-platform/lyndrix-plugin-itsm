"""ConnectorAdapter — contract every external-ITSM connector implements.

A record type whose catalog ``mode`` is ``gateway:<connector id>`` is owned by
that external system. The ITSM service then:

  create   → adapter.create()      → stores external_id/external_url, sync_state=None
  update   → adapter.update()      → on failure sync_state="failed" + sync_error
  journal  → adapter.add_journal()
  inbound  → adapter.fetch() (poll) or adapter.handle_webhook() (push) → local upsert
             keyed by (source=connector id, external_id)

Every method is ``async`` and must not block the event loop — wrap HTTP
clients in ``asyncio.to_thread`` or use an async client. Credentials come
from the plugin's Vault secrets (``ctx.get_secret``), never from the catalog.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExternalRef:
    """Where the external system keeps the record."""

    external_id: str
    external_url: str | None = None


class ConnectorAdapter(ABC):
    #: Stable id, used as ``mode: gateway:<id>`` and as ``Record.source``.
    id: str = ""
    label: str = ""

    @abstractmethod
    async def create(self, record: dict[str, Any]) -> ExternalRef:
        """Create the record externally; return its external reference."""

    @abstractmethod
    async def update(self, ref: ExternalRef, record: dict[str, Any], changes: dict[str, Any]) -> None:
        """Push changed fields (incl. state) for an existing external record."""

    @abstractmethod
    async def add_journal(self, ref: ExternalRef, entry: dict[str, Any]) -> None:
        """Mirror a comment / work note to the external record."""

    @abstractmethod
    async def fetch(self, ref: ExternalRef) -> dict[str, Any] | None:
        """Read the external record (normalized to the local record shape), or None."""

    async def handle_webhook(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Translate an inbound webhook into normalized record dicts (default: none)."""
        return []


class ConnectorRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ConnectorAdapter] = {}

    def register(self, adapter: ConnectorAdapter) -> None:
        if not adapter.id:
            raise ValueError("ConnectorAdapter.id must be set")
        self._adapters[adapter.id] = adapter

    def get(self, connector_id: str) -> ConnectorAdapter | None:
        return self._adapters.get(connector_id)

    def ids(self) -> list[str]:
        return sorted(self._adapters)


connector_registry = ConnectorRegistry()
