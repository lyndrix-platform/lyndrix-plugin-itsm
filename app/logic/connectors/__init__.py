"""Connector adapters — the seam towards external ITSM systems (gateway mode).

Native mode needs none of this. A connector makes an external system (BMC
Helix, ServiceNow, Jira SM, …) the system of record for a record type; the
plugin then keeps a shadow copy and mirrors changes both ways. Only the
interface ships in this phase.
"""
from .base import ConnectorAdapter, ConnectorRegistry, ExternalRef, connector_registry

__all__ = ["ConnectorAdapter", "ConnectorRegistry", "ExternalRef", "connector_registry"]
