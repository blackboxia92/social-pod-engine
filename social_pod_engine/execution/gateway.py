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
    operation: str = "unknown"
    endpoint: str | None = None
    status_code: int | None = None
    exception_type: str | None = None
    safe_detail: str | None = None

    def __str__(self) -> str:
        return self.message

    def task_error(self) -> str:
        """A bounded, secret-free representation suitable for task persistence."""
        parts = [self.kind.value.upper(), f"operation={self.operation}"]
        if self.endpoint:
            parts.append(f"endpoint={self.endpoint}")
        if self.status_code is not None:
            parts.append(f"cause=HTTP {self.status_code}")
        elif self.exception_type:
            parts.append(f"cause={self.exception_type}")
        detail = self.safe_detail or self.message
        if detail:
            parts.append(f"detail={detail[:300]}")
        return " | ".join(parts)[:500]

    def event_details(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "operation": self.operation,
                "endpoint": self.endpoint,
                "http_status": self.status_code,
                "exception_type": self.exception_type,
                "detail": (self.safe_detail or self.message)[:300],
            }.items()
            if value is not None
        }


class UpstreamExecutionGateway(Protocol):
    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> Any: ...
    async def release_lease(self, lease: Any) -> None: ...
    async def open_execution_browser(self, lease: Any) -> BrowserHandle: ...
    async def close_execution_browser(self, browser: BrowserHandle) -> None: ...
    async def execute_x_post(
        self, upstream_profile_id: str, *, text: str, idempotency_key: str, execution_task_id: str
    ) -> Any: ...
