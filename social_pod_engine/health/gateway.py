"""The only health-monitoring boundary permitted to touch upstream resources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..domain import HealthIssueType


@dataclass(frozen=True, slots=True)
class HeadlessBrowser:
    """Gateway-owned browser handle with a page usable by ``ExecutionContext``."""

    page: Any
    handle: Any


@dataclass(frozen=True, slots=True)
class ProxyHealthResult:
    available: bool
    latency_ms: float | None = None
    degraded: bool = False
    issue: HealthIssueType | None = None

    def __post_init__(self) -> None:
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("proxy latency must be non-negative")
        if not self.available and self.issue not in {None, HealthIssueType.PROXY_UNAVAILABLE}:
            raise ValueError("an unavailable proxy must have a proxy-unavailable issue or no issue")
        if self.degraded and not self.available:
            raise ValueError("an unavailable proxy cannot also be degraded")


class UpstreamHealthGateway(Protocol):
    """Protocol implemented by a composition layer outside Social Pod core."""

    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> Any:
        """Acquire an upstream lease before opening a headless browser."""

    async def release_lease(self, lease: Any) -> None:
        """Release the previously acquired lease."""

    async def open_headless_browser(self, lease: Any) -> HeadlessBrowser:
        """Open a headless browser owned by the upstream gateway."""

    async def close_headless_browser(self, browser: HeadlessBrowser) -> None:
        """Close the headless browser owned by this check."""

    async def check_proxy(self, proxy_id: str | None) -> ProxyHealthResult:
        """Return a structured proxy result without exposing credentials."""
