"""Explicit placeholders for platforms not implemented in this increment."""

from collections.abc import Mapping
from typing import Any, NoReturn

from ..contracts import SocialPage
from ..domain import SessionStatus
from ..models import AccountProfile, HealthReport, PlatformCapabilities
from .base import (
    BaseSocialAdapter,
    Capability,
    CapabilityNotSupported,
    ChallengeType,
    ExecutionContext,
    ExecutionResult,
    HealthSignal,
)


class PlatformAdapterNotImplemented(NotImplementedError):
    """Raised instead of silently pretending a platform is operational."""


class PlaceholderAdapter(BaseSocialAdapter):
    platform_id: str
    display_name: str
    home_url: str

    @property
    def platform_name(self) -> str:
        return self.platform_id

    async def open_home(self, page: SocialPage) -> None:
        raise PlatformAdapterNotImplemented(
            f"{self.display_name} adapter is a placeholder; navigation is not enabled yet."
        )

    async def check_session(self, page: SocialPage) -> HealthReport:
        raise PlatformAdapterNotImplemented(
            f"{self.display_name} adapter is a placeholder; session checks are not enabled yet."
        )

    async def get_profile(self, page: SocialPage, *, profile_id: str | None = None) -> AccountProfile:
        raise PlatformAdapterNotImplemented(
            f"{self.display_name} adapter is a placeholder; profile reads are not enabled yet."
        )

    def get_capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities()

    async def validate_session(self, context: ExecutionContext) -> SessionStatus:
        self._not_implemented()

    async def check_health(self, context: ExecutionContext) -> HealthSignal:
        self._not_implemented()

    async def detect_challenge(self, context: ExecutionContext) -> ChallengeType:
        self._not_implemented()

    def get_supported_capabilities(self) -> frozenset[Capability]:
        return frozenset()

    async def execute_capability(
        self,
        capability: Capability,
        payload: Mapping[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult:
        if not isinstance(capability, Capability):
            capability = Capability(capability)
        raise CapabilityNotSupported(self.platform_name, capability)

    def _not_implemented(self) -> NoReturn:
        raise PlatformAdapterNotImplemented(
            f"{self.display_name} adapter is a placeholder; no capabilities are enabled yet."
        )
