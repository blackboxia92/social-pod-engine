"""Read-only X adapter implemented against Playwright-compatible pages."""

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from ..contracts import SocialPage
from ..domain import HealthStatus, SessionStatus
from ..models import AccountProfile, HealthReport, PlatformCapabilities, SessionState
from .base import (
    BaseSocialAdapter,
    Capability,
    CapabilityNotSupported,
    ChallengeType,
    ExecutionContext,
    ExecutionResult,
    HealthSignal,
)


class XAdapter(BaseSocialAdapter):
    platform_id = "x"
    display_name = "X"
    home_url = "https://x.com/home"

    _challenge_selectors = (
        "input[name='challenge_response']",
        "[data-testid='ocfEnterTextTextInput']",
        "[data-testid='ocfEnterTextNextButton']",
    )
    _authenticated_selectors = (
        "[data-testid='SideNav_AccountSwitcher_Button']",
        "a[data-testid='AppTabBar_Profile_Link']",
    )

    @property
    def platform_name(self) -> str:
        return self.platform_id

    async def open_home(self, page: SocialPage) -> None:
        await page.goto(self.home_url)

    async def check_session(self, page: SocialPage) -> HealthReport:
        path = urlparse(page.url).path.lower()
        if any(token in path for token in ("/i/flow/login", "/login", "/signup")):
            return HealthReport(self.platform_id, SessionState.SIGNED_OUT, ("login URL detected",))
        if await self._has_any(page, self._challenge_selectors):
            return HealthReport(self.platform_id, SessionState.CHALLENGE, ("challenge UI detected",))
        if await self._has_any(page, self._authenticated_selectors):
            return HealthReport(self.platform_id, SessionState.ACTIVE)
        return HealthReport(self.platform_id, SessionState.UNKNOWN, ("no stable session marker found",))

    @staticmethod
    async def _has_any(page: SocialPage, selectors: tuple[str, ...]) -> bool:
        """Check selectors sequentially so ordinary Playwright pages work unchanged."""
        for selector in selectors:
            if await page.locator(selector).count():
                return True
        return False

    async def get_profile(self, page: SocialPage, *, profile_id: str | None = None) -> AccountProfile:
        profile_link = page.locator("a[data-testid='AppTabBar_Profile_Link']")
        href = await profile_link.get_attribute("href") if await profile_link.count() else None
        handle = href.strip("/").split("/")[0] if href else None
        return AccountProfile(platform_id=self.platform_id, handle=handle, profile_id=profile_id)

    def get_capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(
            enabled=frozenset({"session_health", "read_profile"}),
            planned=frozenset({"post", "reply", "like", "repost", "follow", "unfollow"}),
        )

    async def validate_session(self, context: ExecutionContext) -> SessionStatus:
        report = await self.check_session(self._page_from(context))
        return {
            SessionState.ACTIVE: SessionStatus.VALID,
            SessionState.SIGNED_OUT: SessionStatus.EXPIRED,
            SessionState.CHALLENGE: SessionStatus.CHALLENGE_REQUIRED,
            SessionState.UNKNOWN: SessionStatus.UNKNOWN,
        }[report.state]

    async def check_health(self, context: ExecutionContext) -> HealthSignal:
        report = await self.check_session(self._page_from(context))
        session_status = await self.validate_session(context)
        health_status = {
            SessionState.ACTIVE: HealthStatus.HEALTHY,
            SessionState.SIGNED_OUT: HealthStatus.DEGRADED,
            SessionState.CHALLENGE: HealthStatus.ACTION_REQUIRED,
            SessionState.UNKNOWN: HealthStatus.UNAVAILABLE,
        }[report.state]
        return HealthSignal(health_status, session_status, report.reasons)

    async def detect_challenge(self, context: ExecutionContext) -> ChallengeType:
        report = await self.check_session(self._page_from(context))
        return ChallengeType.AUTHENTICATION if report.state is SessionState.CHALLENGE else ChallengeType.NONE

    def get_supported_capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.SESSION_HEALTH, Capability.READ_PROFILE})

    async def execute_capability(
        self,
        capability: Capability,
        payload: Mapping[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult:
        if not isinstance(capability, Capability):
            capability = Capability(capability)
        if capability is Capability.SESSION_HEALTH:
            signal = await self.check_health(context)
            return ExecutionResult(
                capability,
                success=True,
                data={
                    "health_status": signal.health_status.value,
                    "session_status": signal.session_status.value,
                },
                reasons=signal.reasons,
            )
        if capability is Capability.READ_PROFILE:
            profile = await self.get_profile(
                self._page_from(context), profile_id=context.upstream_profile_id
            )
            return ExecutionResult(
                capability,
                success=True,
                data={
                    "platform_id": profile.platform_id,
                    "handle": profile.handle,
                    "display_name": profile.display_name,
                    "profile_id": profile.profile_id,
                },
            )
        raise CapabilityNotSupported(self.platform_name, capability)

    @staticmethod
    def _page_from(context: ExecutionContext) -> SocialPage:
        if context.page is None:
            raise ValueError("X adapter requires a caller-owned browser page in ExecutionContext")
        return context.page
