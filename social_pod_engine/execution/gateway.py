"""Social-Pod boundary around upstream profile leases and browser instances."""

from __future__ import annotations

from typing import Any, Protocol


class BrowserHandle(Protocol):
    page: Any


class UpstreamExecutionGateway(Protocol):
    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> Any: ...
    async def release_lease(self, lease: Any) -> None: ...
    async def open_browser(self, lease: Any) -> BrowserHandle: ...
    async def close_browser(self, browser: BrowserHandle) -> None: ...
