"""Read-only X adapter implemented against Playwright-compatible pages."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Mapping
from enum import Enum
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
    ExternalActionUncertainError,
    ExternalExecutionResult,
    HealthSignal,
)


class XPageState(str, Enum):
    HOME_READY = "home_ready"
    AUTHENTICATED_BUT_COMPOSER_MISSING = "authenticated_but_composer_missing"
    LOGIN_PAGE = "login_page"
    CHALLENGE_PAGE = "challenge_page"
    UNEXPECTED_PAGE = "unexpected_page"


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
        # X's rendered editor is a contenteditable Draft.js surface.  This
        # fallback is deliberately scoped to the main feed and its stable,
        # user-facing aria label so it cannot select search or DM inputs.
        "[data-testid='primaryColumn'] div[role='textbox'][contenteditable='true'][aria-label='Post text']",
    )
    _post_button_selectors = (
        "[data-testid='tweetButtonInline']",
        "[data-testid='tweetButton']",
        "button[data-testid*='tweetButton']",
    )
    _tweet_container_selector = "article[data-testid='tweet']"
    _tweet_text_selector = "[data-testid='tweetText']"
    _tweet_permalink_selector = "a[href*='/status/']"
    _toast_selectors = ("[data-testid='toast']", "[role='status']")
    _error_selectors = ("[data-testid='error-detail']", "[role='alert']")
    _login_selectors = (
        "input[name='text']",
        "input[name='password']",
        "[data-testid='loginButton']",
    )
    _composer_candidate_selector = "div[role='textbox'], [contenteditable='true']"

    def __init__(
        self,
        *,
        confirmation_timeout_seconds: float = 8.0,
        confirmation_poll_interval_seconds: float = 0.5,
        submit_ready_timeout_seconds: float = 3.0,
        submit_ready_poll_interval_seconds: float = 0.2,
        home_ready_timeout_seconds: float = 5.0,
        home_ready_poll_interval_seconds: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if confirmation_timeout_seconds < 0:
            raise ValueError("confirmation_timeout_seconds must be non-negative")
        if confirmation_poll_interval_seconds <= 0:
            raise ValueError("confirmation_poll_interval_seconds must be positive")
        if submit_ready_timeout_seconds < 0:
            raise ValueError("submit_ready_timeout_seconds must be non-negative")
        if submit_ready_poll_interval_seconds <= 0:
            raise ValueError("submit_ready_poll_interval_seconds must be positive")
        if home_ready_timeout_seconds < 0:
            raise ValueError("home_ready_timeout_seconds must be non-negative")
        if home_ready_poll_interval_seconds <= 0:
            raise ValueError("home_ready_poll_interval_seconds must be positive")
        self.confirmation_timeout_seconds = confirmation_timeout_seconds
        self.confirmation_poll_interval_seconds = confirmation_poll_interval_seconds
        self.submit_ready_timeout_seconds = submit_ready_timeout_seconds
        self.submit_ready_poll_interval_seconds = submit_ready_poll_interval_seconds
        self.home_ready_timeout_seconds = home_ready_timeout_seconds
        self.home_ready_poll_interval_seconds = home_ready_poll_interval_seconds
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
        diagnostics = self._diagnostics(text, page)
        page_state, composer, composer_selector = await self._wait_for_home_ready(page)
        diagnostics["url"] = str(getattr(page, "url", ""))
        diagnostics["page_state"] = page_state.value
        if composer is None:
            page_diagnostics = await self._page_diagnostics(page)
            diagnostics["page_diagnostics"] = page_diagnostics
            if page_state is XPageState.LOGIN_PAGE:
                return self._submit_failure(
                    "SESSION_EXPIRED", "LOGIN_REQUIRED", diagnostics
                )
            if page_state is XPageState.CHALLENGE_PAGE:
                return self._submit_failure(
                    "CHALLENGE_REQUIRED", "X requires manual verification", diagnostics
                )
            return self._submit_failure(
                "POST_SUBMIT_NOT_STARTED",
                self._composer_missing_detail(page_diagnostics),
                diagnostics,
            )
        assert composer_selector is not None
        diagnostics["composer"] = await self._composer_state(composer, composer_selector, text)
        if not self._composer_is_ready(diagnostics["composer"]):
            return self._submit_failure(
                "POST_SUBMIT_NOT_STARTED", "composer was not visible and editable", diagnostics
            )
        await composer.fill(text)
        diagnostics["composer"] = await self._composer_state(composer, composer_selector, text)
        if not diagnostics["composer"]["text_present"]:
            return self._submit_failure(
                "POST_SUBMIT_NOT_STARTED", "composer did not retain text after fill", diagnostics
            )
        button, button_state = await self._wait_for_enabled_post_button(page)
        if button is None:
            return self._submit_failure(
                "POST_SUBMIT_NOT_STARTED", "post button remained disabled", diagnostics
            )
        diagnostics["post_button"] = button_state
        try:
            await button.click()
        except Exception as exc:
            # A click RPC can time out after the remote browser receives it.
            # The dispatcher must reconcile rather than replay a possible POST.
            raise ExternalActionUncertainError(
                "X POST click outcome is unknown", diagnostics=diagnostics
            ) from exc
        diagnostics["post_button_clicked"] = True
        immediate = self._status_reference(getattr(page, "url", ""))
        if immediate is not None:
            external_id, external_url = immediate
            return ExternalExecutionResult(
                True,
                True,
                external_id=external_id,
                external_url=external_url,
                metadata=diagnostics,
            )
        try:
            post_click = await self._post_click_state(page, composer, text)
            diagnostics["post_click"] = post_click
            if post_click["error_visible"]:
                return self._submit_failure(
                    "PLATFORM_REJECTED", "X displayed an error state", diagnostics
                )
            if post_click["composer_retained_text"]:
                return self._submit_failure(
                    "POST_SUBMIT_FAILED", "composer retained text after click", diagnostics
                )
            if not (
                post_click["composer_cleared"]
                or post_click["button_disappeared"]
                or post_click["toast_visible"]
            ):
                return ExternalExecutionResult(
                    True,
                    False,
                    metadata={
                        **diagnostics,
                        "post_submit_status": "UNKNOWN_EXTERNAL_STATE",
                        "detail": "click completed but no deterministic post-submit signal was observed",
                    },
                )
            confirmed = await self.find_published_post(text, page)
        except Exception as exc:
            raise ExternalActionUncertainError(
                "X POST confirmation outcome is unknown",
                action_attempted=True,
                diagnostics=diagnostics,
            ) from exc
        if confirmed is not None:
            return ExternalExecutionResult(
                confirmed.success,
                confirmed.confirmed,
                external_id=confirmed.external_id,
                external_url=confirmed.external_url,
                metadata=diagnostics,
                reasons=confirmed.reasons,
            )
        return ExternalExecutionResult(
            True,
            False,
            metadata={
                **diagnostics,
                "post_submit_status": "UNKNOWN_EXTERNAL_STATE",
                "detail": "click completed but no timeline evidence was observed",
            },
        )

    async def _wait_for_enabled_post_button(self, page: SocialPage) -> tuple[Any | None, dict[str, Any]]:
        deadline = monotonic() + self.submit_ready_timeout_seconds
        state: dict[str, Any] = {"found": False}
        while True:
            button, selector = await self._first_locator_with_selector(page, self._post_button_selectors)
            if button is not None:
                assert selector is not None
                state = await self._button_state(button, selector)
                if state["visible"] and state["enabled"]:
                    return button, state
            if monotonic() >= deadline:
                return None, state
            await self._sleep(
                min(self.submit_ready_poll_interval_seconds, max(0.0, deadline - monotonic()))
            )

    async def _wait_for_home_ready(
        self, page: SocialPage
    ) -> tuple[XPageState, Any | None, str | None]:
        """Navigate to Home and wait for a bounded, observable X page state.

        A successful ``goto`` only means navigation was requested; X mounts its
        client-side composer later.  The loop deliberately performs read-only
        probes and never opens a compose dialog or clicks a control.
        """
        if urlparse(str(getattr(page, "url", ""))).path.rstrip("/").lower() != "/home":
            await self.open_home(page)
        deadline = monotonic() + self.home_ready_timeout_seconds
        last_state = XPageState.UNEXPECTED_PAGE
        while True:
            state = await self._classify_page(page)
            if state in (XPageState.LOGIN_PAGE, XPageState.CHALLENGE_PAGE):
                return state, None, None
            composer, selector = await self._first_locator_with_selector(page, self._composer_selectors)
            if composer is not None:
                return XPageState.HOME_READY, composer, selector
            last_state = state
            if monotonic() >= deadline:
                return last_state, None, None
            await self._sleep(
                min(self.home_ready_poll_interval_seconds, max(0.0, deadline - monotonic()))
            )

    async def _classify_page(self, page: SocialPage) -> XPageState:
        path = urlparse(str(getattr(page, "url", ""))).path.lower()
        if await self._has_any(page, self._challenge_selectors):
            return XPageState.CHALLENGE_PAGE
        if any(token in path for token in ("/i/flow/login", "/login", "/signup")) or await self._has_any(
            page, self._login_selectors
        ):
            return XPageState.LOGIN_PAGE
        if await self._has_any(page, self._authenticated_selectors):
            return XPageState.AUTHENTICATED_BUT_COMPOSER_MISSING
        return XPageState.UNEXPECTED_PAGE

    async def _page_diagnostics(self, page: SocialPage) -> dict[str, Any]:
        """Collect small structural evidence only; never serialise page HTML."""
        candidates: list[dict[str, Any]] = []
        snapshotter = getattr(page, "element_snapshots", None)
        if callable(snapshotter):
            raw_candidates = await snapshotter(self._composer_candidate_selector)
            for candidate in raw_candidates[:12]:
                candidates.append(
                    {
                        key: candidate.get(key)
                        for key in (
                            "tag",
                            "role",
                            "data_testid",
                            "aria_label",
                            "contenteditable",
                            "placeholder",
                            "text",
                        )
                    }
                )
        return {
            "url": str(getattr(page, "url", "")),
            "authenticated": await self._has_any(page, self._authenticated_selectors),
            "login": await self._has_any(page, self._login_selectors),
            "challenge": await self._has_any(page, self._challenge_selectors),
            "textboxes": await page.locator("div[role='textbox']").count(),
            "contenteditables": await page.locator("[contenteditable='true']").count(),
            "data_testids": await page.locator("[data-testid]").count(),
            "composer_candidates": candidates,
        }

    @staticmethod
    def _composer_missing_detail(diagnostics: Mapping[str, Any]) -> str:
        return (
            "composer not found"
            f" | url={diagnostics.get('url', '')}"
            f" | authenticated={diagnostics.get('authenticated', False)}"
            f" | textboxes={diagnostics.get('textboxes', 0)}"
            f" | contenteditables={diagnostics.get('contenteditables', 0)}"
        )

    async def _post_click_state(
        self, page: SocialPage, composer: Any, text: str
    ) -> dict[str, bool]:
        composer_text = await self._text_content(composer)
        button, _ = await self._first_locator_with_selector(page, self._post_button_selectors)
        return {
            "composer_retained_text": self._contains_submitted_text(composer_text, text),
            "composer_cleared": not (composer_text or "").strip(),
            "button_disappeared": button is None,
            "toast_visible": await self._has_any(page, self._toast_selectors),
            "error_visible": await self._has_any(page, self._error_selectors),
        }

    async def _composer_state(self, composer: Any, selector: str, text: str) -> dict[str, Any]:
        contenteditable = await composer.get_attribute("contenteditable")
        text_content = await self._text_content(composer)
        return {
            "found": True,
            "selector": selector,
            "visible": await self._is_visible(composer),
            "enabled": await self._is_enabled(composer),
            "editable": contenteditable == "true",
            "text_present": self._contains_submitted_text(text_content, text),
            "text_length": len(text_content or ""),
        }

    async def _button_state(self, button: Any, selector: str) -> dict[str, Any]:
        disabled = await button.get_attribute("disabled")
        aria_disabled = await button.get_attribute("aria-disabled")
        return {
            "found": True,
            "selector": selector,
            "visible": await self._is_visible(button),
            "enabled": await self._is_enabled(button),
            "disabled": disabled is not None,
            "aria_disabled": aria_disabled == "true",
            "data_testid": await button.get_attribute("data-testid"),
        }

    @staticmethod
    async def _is_visible(locator: Any) -> bool:
        method = getattr(locator, "is_visible", None)
        return bool(await method()) if callable(method) else bool(await locator.count())

    @staticmethod
    async def _is_enabled(locator: Any) -> bool:
        method = getattr(locator, "is_enabled", None)
        if callable(method):
            return bool(await method())
        disabled = await locator.get_attribute("disabled")
        aria_disabled = await locator.get_attribute("aria-disabled")
        return disabled is None and aria_disabled != "true"

    @staticmethod
    async def _text_content(locator: Any) -> str | None:
        method = getattr(locator, "text_content", None)
        if callable(method):
            value = await method()
            return str(value) if value is not None else None
        return None

    @staticmethod
    def _contains_submitted_text(actual: str | None, submitted: str) -> bool:
        return submitted.strip() in (actual or "")

    @staticmethod
    def _composer_is_ready(state: Mapping[str, Any]) -> bool:
        return bool(state["visible"] and state["enabled"] and state["editable"])

    @staticmethod
    def _diagnostics(text: str, page: SocialPage) -> dict[str, Any]:
        return {
            "url": str(getattr(page, "url", "")),
            "text_length": len(text),
            "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        }

    @staticmethod
    def _submit_failure(
        status: str, detail: str, diagnostics: Mapping[str, Any]
    ) -> ExternalExecutionResult:
        return ExternalExecutionResult(
            False,
            False,
            metadata={
                **diagnostics,
                "post_submit_status": status,
                "detail": detail,
            },
        )

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
        locator, _ = await XAdapter._first_locator_with_selector(page, selectors)
        return locator

    @staticmethod
    async def _first_locator_with_selector(
        page: SocialPage, selectors: tuple[str, ...]
    ) -> tuple[Any | None, str | None]:
        for selector in selectors:
            locator: Any = page.locator(selector)
            if await locator.count():
                return locator, selector
        return None, None

    @staticmethod
    def _page_from(context: ExecutionContext) -> SocialPage:
        if context.page is None:
            raise ValueError("X adapter requires a caller-owned browser page in ExecutionContext")
        return context.page
