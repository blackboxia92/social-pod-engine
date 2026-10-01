"""Stable control-plane adapter contract for every social platform."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ..domain import HealthStatus, SessionStatus


class Capability(str, Enum):
    SESSION_HEALTH = "session_health"
    READ_PROFILE = "read_profile"
    POST = "post"
    REPLY = "reply"
    LIKE = "like"
    FOLLOW = "follow"
    REPOST = "repost"


class ChallengeType(str, Enum):
    NONE = "none"
    AUTHENTICATION = "authentication"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """References supplied by the caller; the adapter never owns a CPM session."""

    page: Any | None = None
    social_account_id: str | None = None
    upstream_profile_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class HealthSignal:
    health_status: HealthStatus
    session_status: SessionStatus
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    capability: Capability
    success: bool
    data: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalExecutionResult:
    success: bool
    confirmed: bool
    external_id: str | None = None
    external_url: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


class ExternalActionUncertainError(RuntimeError):
    """A write may have reached the platform but cannot be confirmed safely."""

    def __init__(
        self,
        message: str,
        *,
        action_attempted: bool = False,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.action_attempted = action_attempted
        self.diagnostics = diagnostics or {}


class CapabilityNotSupported(NotImplementedError):
    def __init__(self, platform_name: str, capability: Capability) -> None:
        self.platform_name = platform_name
        self.capability = capability
        super().__init__(f"{platform_name} does not support capability {capability.value!r}")


class BaseSocialAdapter(ABC):
    """No implicit write behaviour: every enabled capability is explicit."""

    @property
    @abstractmethod
    def platform_name(self) -> str:
        """Stable lowercase platform identifier."""

    @abstractmethod
    async def validate_session(self, context: ExecutionContext) -> SessionStatus:
        """Read the current session state from a caller-owned context."""

    @abstractmethod
    async def check_health(self, context: ExecutionContext) -> HealthSignal:
        """Return health without modifying an account or browser session."""

    @abstractmethod
    async def detect_challenge(self, context: ExecutionContext) -> ChallengeType:
        """Inspect for an interactive challenge; never solve one."""

    @abstractmethod
    def get_supported_capabilities(self) -> frozenset[Capability]:
        """Return only capabilities that are actually implemented."""

    @abstractmethod
    async def execute_capability(
        self,
        capability: Capability,
        payload: Mapping[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult | ExternalExecutionResult:
        """Execute an explicitly supported capability or raise an explicit error."""
