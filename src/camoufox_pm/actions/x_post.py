"""One safe, high-level X POST transaction owned entirely by CPM.

No page, selector, cookie, or browser handle crosses the HTTP boundary.  This
is intentionally the only productive X POST path used by Social Pod.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from camoufox_pm.core.leases import ProfileLocked


@dataclass(frozen=True, slots=True)
class XPostActionResult:
    state: str
    attempted: bool = False
    confirmed: bool = False
    external_id: str | None = None
    external_url: str | None = None
    reason: str | None = None
    diagnostics: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state,
            "attempted": self.attempted,
            "confirmed": self.confirmed,
            "external_id": self.external_id,
            "external_url": self.external_url,
            "reason": self.reason,
            "diagnostics": self.diagnostics,
        }


class XPostWorker:
    """Run prepare, submit, observe and confirm against one local Page."""

    home_url = "https://x.com/home"
    composer_selector = "[data-testid='tweetTextarea_0']"
    composer_fallback = (
        "[data-testid='primaryColumn'] div[role='textbox'][contenteditable='true'][aria-label='Post text']"
    )
    button_selector = (
        "[data-testid='primaryColumn']:has([data-testid='tweetTextarea_0']) "
        "[data-testid='tweetButtonInline']"
    )
    button_fallback = (
        "[data-testid='primaryColumn']:has([data-testid='tweetTextarea_0']) "
        "[data-testid='tweetButton']"
    )
    login_selector = "input[name='password'], [data-testid='loginButton']"
    challenge_selector = "input[name='challenge_response'], [data-testid='ocfEnterTextNextButton']"
    error_selector = "[data-testid='error-detail'], [role='alert']"
    profile_link_selector = "a[data-testid='AppTabBar_Profile_Link']"
    tweet_selector = "article[data-testid='tweet']"

    def __init__(self, manager: Any, *, confirmation_timeout: float = 8.0, poll_interval: float = 0.4) -> None:
        self.manager = manager
        self.confirmation_timeout = confirmation_timeout
        self.poll_interval = poll_interval

    async def execute(
        self, *, profile_id: str, text: str, idempotency_key: str, execution_task_id: str
    ) -> XPostActionResult:
        receipt = await self.manager.storage.get_external_action_receipt(idempotency_key)
        if receipt is not None:
            if receipt["profile_id"] != profile_id or receipt["execution_task_id"] != execution_task_id:
                return XPostActionResult("FAILED_PRE_SUBMIT", reason="idempotency_key_conflict")
            if str(receipt["state"]) == "IN_PROGRESS":
                # Crash-safe default: do not replay an action whose browser
                # transaction may have started before CPM exited.
                return XPostActionResult(
                    "UNKNOWN_EXTERNAL_STATE", True, False, reason="prior_action_in_progress"
                )
            if not bool(receipt["attempted"]):
                # A known pre-submit failure is safe to retry; the final result
                # below replaces this receipt atomically.
                receipt = None
        if receipt is not None:
            return XPostActionResult(
                state=str(receipt["state"]), attempted=bool(receipt["attempted"]),
                confirmed=bool(receipt["confirmed"]), external_id=receipt.get("external_id"),
                external_url=receipt.get("external_url"), reason=receipt.get("reason"),
                diagnostics={"receipt_reused": True},
            )

        # Reserve before opening the profile. A process crash after this point
        # deliberately resolves as UNKNOWN rather than risking a second post.
        await self.manager.storage.save_external_action_receipt(
            idempotency_key=idempotency_key,
            execution_task_id=execution_task_id,
            profile_id=profile_id,
            state="IN_PROGRESS",
            attempted=False,
            confirmed=False,
            external_id=None,
            external_url=None,
            reason=None,
            created_at=datetime.now(timezone.utc),
        )

        launched = False
        try:
            if self.manager.browser_sessions.is_live(profile_id):
                result = XPostActionResult("MODE_CONFLICT", reason="profile_has_active_browser")
            elif not await self.manager.get_profile(profile_id):
                result = XPostActionResult("FAILED_PRE_SUBMIT", reason="profile_not_found")
            else:
                try:
                    await self.manager.launch_browser(profile_id, headless=True)
                    launched = True
                except ProfileLocked:
                    result = XPostActionResult("PROFILE_BUSY", reason="profile_lease_held")
                else:
                    session = self.manager.browser_sessions.active_sessions[profile_id]
                    page = await self.manager.browser_sessions._page_for(session)
                    result = await self._post(page, text)
        except Exception as exc:  # Do not leak browser/session implementation details.
            logger.exception("X_POST internal execution error for profile {}", profile_id)
            result = XPostActionResult("INTERNAL_EXECUTION_ERROR", reason=type(exc).__name__)
        finally:
            if launched:
                try:
                    await self.manager.close_browser(profile_id)
                except Exception:
                    logger.exception("X_POST cleanup failed for profile {}", profile_id)

        await self.manager.storage.save_external_action_receipt(
            idempotency_key=idempotency_key,
            execution_task_id=execution_task_id,
            profile_id=profile_id,
            state=result.state,
            attempted=result.attempted,
            confirmed=result.confirmed,
            external_id=result.external_id,
            external_url=result.external_url,
            reason=result.reason,
            created_at=datetime.now(timezone.utc),
        )
        logger.info("X_POST_FINISHED state={}", result.state)
        return result

    async def _post(self, page: Any, text: str) -> XPostActionResult:
        logger.info("X_POST_STARTED")
        await page.goto(self.home_url)
        if await page.locator(self.login_selector).count():
            return XPostActionResult("FAILED_PRE_SUBMIT", reason="LOGIN_REQUIRED")
        if await page.locator(self.challenge_selector).count():
            return XPostActionResult("FAILED_PRE_SUBMIT", reason="CHALLENGE_REQUIRED")
        composer = await self._first_present(page, (self.composer_selector, self.composer_fallback))
        if composer is None:
            return XPostActionResult("FAILED_PRE_SUBMIT", reason="COMPOSER_NOT_FOUND")
        logger.info("COMPOSER_READY")
        await composer.fill(text)
        if text not in ((await composer.text_content()) or ""):
            return XPostActionResult("FAILED_PRE_SUBMIT", reason="composer_did_not_retain_text")
        button = await self._first_enabled(page, (self.button_selector, self.button_fallback))
        if button is None:
            return XPostActionResult("FAILED_PRE_SUBMIT", reason="POST_BUTTON_NOT_READY")
        logger.info("POST_READY")
        try:
            await button.click()  # Exactly one irreversible click.
        except TimeoutError:
            # Playwright actionability timeout occurs before it dispatches the
            # click, so a normal retry policy remains safe.
            return XPostActionResult("FAILED_PRE_SUBMIT", reason="POST_BUTTON_NOT_READY")
        except Exception:
            # A transport/browser fault around click cannot prove whether the
            # browser dispatched it. Never let Social Pod replay automatically.
            return XPostActionResult("UNKNOWN_EXTERNAL_STATE", True, False, reason="post_click_indeterminate")
        logger.info("POST_CLICKED")
        confirmed = await self._confirm(page, text)
        if confirmed is not None:
            external_id, external_url = confirmed
            logger.info("POST_CONFIRMED")
            return XPostActionResult("COMPLETED", True, True, external_id, external_url)
        error = await self._first_text(page, self.error_selector)
        if error:
            return XPostActionResult(
                "PLATFORM_REJECTED", True, False, reason="platform_error", diagnostics={"error": error[:160]}
            )
        if text in ((await composer.text_content()) or ""):
            return XPostActionResult("PLATFORM_REJECTED", True, False, reason="composer_retained_text")
        return XPostActionResult("UNKNOWN_EXTERNAL_STATE", True, False, reason="post_click_state_indeterminate")

    async def _confirm(self, page: Any, text: str) -> tuple[str, str] | None:
        deadline = asyncio.get_running_loop().time() + self.confirmation_timeout
        while True:
            direct = self._status_from_url(str(getattr(page, "url", "")))
            if direct:
                return direct
            profile_link = page.locator(self.profile_link_selector)
            href = await profile_link.first.get_attribute("href") if await profile_link.count() else None
            if href:
                await page.goto(f"https://x.com{href}")
                tweets = page.locator(self.tweet_selector)
                for index in range(min(await tweets.count(), 20)):
                    tweet = tweets.nth(index)
                    tweet_text = await tweet.locator("[data-testid='tweetText']").first.text_content()
                    if text != (tweet_text or ""):
                        continue
                    status = await tweet.locator("a[href*='/status/']").first.get_attribute("href")
                    if status:
                        return self._status_from_url(f"https://x.com{status}")
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(self.poll_interval)

    @staticmethod
    def _status_from_url(url: str) -> tuple[str, str] | None:
        match = re.search(r"/status/(\d+)", url)
        return (match.group(1), url) if match else None

    @staticmethod
    async def _first_present(page: Any, selectors: tuple[str, ...]) -> Any | None:
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count() and await locator.first.is_visible():
                return locator.first
        return None

    @staticmethod
    async def _first_enabled(page: Any, selectors: tuple[str, ...]) -> Any | None:
        for selector in selectors:
            locator = page.locator(selector)
            if await locator.count() and await locator.first.is_visible() and await locator.first.is_enabled():
                return locator.first
        return None

    @staticmethod
    async def _first_text(page: Any, selector: str) -> str | None:
        locator = page.locator(selector)
        return await locator.first.text_content() if await locator.count() else None
