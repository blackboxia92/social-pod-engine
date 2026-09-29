"""Composition root for the isolated social adapter layer."""

from .adapters.facebook import FacebookAdapter
from .adapters.instagram import InstagramAdapter
from .adapters.x import XAdapter
from .registry import AdapterRegistry


class SocialPodEngine:
    """Owns only adapters; upstream browser/session lifecycle stays upstream."""

    def __init__(self, registry: AdapterRegistry | None = None) -> None:
        self.registry = registry or AdapterRegistry()

    @classmethod
    def with_builtin_adapters(cls) -> "SocialPodEngine":
        registry = AdapterRegistry()
        for adapter in (XAdapter(), InstagramAdapter(), FacebookAdapter()):
            registry.register(adapter)
        return cls(registry)
