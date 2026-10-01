"""Read-only X adapter implemented against Playwright-compatible pages."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse

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
    ExternalExecutionResult,
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
    # Ordered, data-testid-first fallbacks; core execution never sees X selectors.
    _composer_selectors = (
        "[data-testid='tweetTextarea_0']",
        "[data-testid='tweetTextarea_0'][contenteditable='true']",
        "div[role='textbox'][data-testid*='tweetTextarea']",
    )
    _post_button_selectors = (
        "[data-testid='tweetButtonInline']",
        "[data-testid='tweetButton']",
        "button[data-testid*='tweetButton']",
    )
    _tweet_container_selector = "article[data-testid='tweet']"
    _tweet_text_selector = "[data-testid='tweetText']"
    _tweet_permalink_selector = "a[href*='/status/']"

    def __init__(
        self,
        *,
        confirmation_timeout_seconds: float = 8.0,
        confirmation_poll_interval_seconds: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if confirmation_timeout_seconds < 0:
            raise ValueError("confirmation_timeout_seconds must be non-negative")
        if confirmation_poll_interval_seconds <= 0:
            raise ValueError("confirmation_poll_interval_seconds must be positive")
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self.confirmation_poll_interval_seconds = confirmation_poll_interval_seconds
        self._sleep = sleep

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
        return frozenset({Capability.SESSION_HEALTH, Capability.READ_PROFILE, Capability.POST})

    async def execute_capability(
        self,
        capability: Capability,
        payload: Mapping[str, Any],
        context: ExecutionContext,
    ) -> ExecutionResult | ExternalExecutionResult:
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
        if capability is Capability.POST:
            return await self._post(payload, self._page_from(context))
        raise CapabilityNotSupported(self.platform_name, capability)

    async def _post(self, payload: Mapping[str, Any], page: SocialPage) -> ExternalExecutionResult:
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("X POST requires non-empty final text")
        composer = await self._first_locator(page, self._composer_selectors)
        if composer is None:
            raise ValueError("X post composer is unavailable")
        await composer.fill(text)
        button = await self._first_locator(page, self._post_button_selectors)
        if button is None:
            raise ValueError("X post button is unavailable")
        await button.click()
        immediate = self._status_reference(getattr(page, "url", ""))
        if immediate is not None:
            external_id, external_url = immediate
            return ExternalExecutionResult(True, True, external_id=external_id, external_url=external_url)
        confirmed = await self.find_published_post(text, page)
        return confirmed or ExternalExecutionResult(True, False)

    async def find_published_post(
        self, text: str, page: SocialPage
    ) -> ExternalExecutionResult | None:
        """Read recent profile posts until the exact submitted text has evidence.

        This is read-only confirmation: it never clicks the composer or retries
        publication.  A missing item remains indeterminate because X timelines
        can update asynchronously.
        """
        profile = await self.get_profile(page)
        if not profile.handle:
            return None
        await page.goto(f"https://x.com/{profile.handle}")
        deadline = monotonic() + self.confirmation_timeout_seconds
        while True:
            for item_text, href in await self._recent_post_snapshots(page):
                if item_text != text:
                    continue
                reference = self._status_reference(href)
                if reference is not None:
                    external_id, external_url = reference
                    return ExternalExecutionResult(
                        True, True, external_id=external_id, external_url=external_url
                    )
            if monotonic() >= deadline:
                return None
            await self._sleep(
                min(self.confirmation_poll_interval_seconds, max(0.0, deadline - monotonic()))
            )

    async def _recent_post_snapshots(self, page: SocialPage) -> list[tuple[str, str]]:
        snapshotter = getattr(page, "locator_snapshots", None)
        if callable(snapshotter):
            remote_snapshots = await snapshotter(
                self._tweet_container_selector,
                text_selector=self._tweet_text_selector,
                href_selector=self._tweet_permalink_selector,
            )
            return [(str(item.text), str(item.href)) for item in remote_snapshots]

        containers: Any = page.locator(self._tweet_container_selector)
        snapshots: list[tuple[str, str]] = []
        for index in range(min(await containers.count(), 20)):
            container = containers.nth(index)
            text_locator = container.locator(self._tweet_text_selector)
            href_locator = container.locator(self._tweet_permalink_selector)
            if not await text_locator.count() or not await href_locator.count():
                continue
            item_text = await text_locator.first.text_content()
            href = await href_locator.first.get_attribute("href")
            if item_text is not None and href is not None:
                snapshots.append((str(item_text), str(href)))
        return snapshots

    @staticmethod
    def _status_reference(url: str) -> tuple[str, str] | None:
        parsed = urlparse(url)
        path = parsed.path.strip("/").split("/")
        if len(path) < 3 or path[-2] != "status" or not path[-1]:
            return None
        return path[-1], urljoin("https://x.com", url)

    @staticmethod
    async def _first_locator(page: SocialPage, selectors: tuple[str, ...]) -> Any | None:
        for selector in selectors:
            locator: Any = page.locator(selector)
            if await locator.count():
                return locator
        return None

    @staticmethod
    def _page_from(context: ExecutionContext) -> SocialPage:
        if context.page is None:
            raise ValueError("X adapter requires a caller-owned browser page in ExecutionContext")
        return context.page
