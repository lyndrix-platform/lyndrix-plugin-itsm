"""Test doubles shared by the test modules: the plugin context (event
recorder) and an Entity Registry answering ``ctx.request`` lookups."""

from __future__ import annotations

HYDRA = "60c443da-a68e-4ba2-8925-aa934ca5dca0"
MERGED_AWAY = "11111111-1111-1111-1111-111111111111"
ATLAS = "aaaaaaaa-0000-0000-0000-000000000000"


class FakeCtx:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.cluster_topics: list[str] = []

    def emit(self, topic, payload, cluster=False):
        self.events.append((topic, payload))
        if cluster:
            self.cluster_topics.append(topic)

    def topics(self) -> list[str]:
        return [t for t, _ in self.events]

    def payloads(self, topic: str) -> list[dict]:
        return [p for t, p in self.events if t == topic]


class RegistryCtx(FakeCtx):
    """Fake ctx.request answering like the Entity Registry's lookup handler."""

    def __init__(
        self, known: dict[str, dict] | None = None, available: bool = True
    ) -> None:
        super().__init__()
        self.known = known or {}
        self.available = available
        self.requests: list[tuple[str, dict]] = []

    async def request(self, topic, payload, *, reply_topic, timeout=10.0):
        self.requests.append((topic, payload))
        if not self.available:
            return None  # nobody answered within the timeout
        ref = payload["ref"]
        hit = self.known.get(ref) or self.known.get(ref.lower())
        if hit:
            return {"ok": True, "entity": hit, "match": "identifier", "candidates": []}
        cands = [e for k, e in self.known.items() if ref.lower() in k.lower()]
        return {
            "ok": True,
            "entity": None,
            "match": "ambiguous" if len(cands) > 1 else None,
            "candidates": cands,
        }


def registry(svc, available=True) -> RegistryCtx:
    """Attach a registry that knows docker-hydra (also under a merged-away UUID)
    and docker-atlas to the service and return it."""
    hydra = {"uuid": HYDRA, "entity_type": "host", "display_name": "docker-hydra"}
    ctx = RegistryCtx(
        {
            "docker-hydra": hydra,
            HYDRA: hydra,
            "iac.host:docker-hydra": hydra,
            MERGED_AWAY: hydra,
            "docker-atlas": {
                "uuid": ATLAS,
                "entity_type": "host",
                "display_name": "docker-atlas",
            },
        },
        available=available,
    )
    svc.set_context(ctx)
    return ctx
