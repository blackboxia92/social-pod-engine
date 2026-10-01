"""Social-Pod boundary around upstream profile leases and browser instances."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol


class BrowserHandle(Protocol):
    page: Any


class ExecutionGatewayFailureKind(str, Enum):
    PROFILE_BUSY = "profile_busy"
    BROWSER_LAUNCH_FAILED = "browser_launch_failed"
    RPC_UNAVAILABLE = "rpc_unavailable"


@dataclass(frozen=True, slots=True)
class ExecutionGatewayError(RuntimeError):
    """A structured upstream failure that occurred before a social action."""

    kind: ExecutionGatewayFailureKind
    message: str

    def __str__(self) -> str:
        return self.message


class UpstreamExecutionGateway(Protocol):
    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> Any: ...
    async def release_lease(self, lease: Any) -> None: ...
    async def open_execution_browser(self, lease: Any) -> BrowserHandle: ...
    async def close_execution_browser(self, browser: BrowserHandle) -> None: ...
