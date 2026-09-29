"""Explicit adapter registration with no plugin auto-discovery side effects."""

from .adapters.base import BaseSocialAdapter


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, BaseSocialAdapter] = {}

    def register(self, adapter: BaseSocialAdapter) -> None:
        platform_id = adapter.platform_name.lower()
        if platform_id in self._adapters:
            raise ValueError(f"Adapter already registered: {platform_id}")
        self._adapters[platform_id] = adapter

    def get(self, platform_id: str) -> BaseSocialAdapter:
        try:
            return self._adapters[platform_id.lower()]
        except KeyError as exc:
            available = ", ".join(sorted(self._adapters)) or "none"
            raise KeyError(f"Unknown platform {platform_id!r}; registered: {available}") from exc

    def platform_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))
