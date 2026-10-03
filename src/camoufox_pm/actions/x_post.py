"""One safe, high-level X POST transaction owned entirely by CPM.

No page, selector, cookie, or browser handle crosses the HTTP boundary.  This
is intentionally the only productive X POST path used by Social Pod.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Any
from urllib.parse import urlsplit

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
    # Production write selectors live in CPM. All editor candidates are scoped
    # to Home's primary column; generic textboxes never qualify on their own.
    composer_selectors = (
        "[data-testid='primaryColumn'] [data-testid='tweetTextarea_0'][contenteditable='true']",
        "[data-testid='primaryColumn'] [data-testid='tweetTextarea_0']",
        "[data-testid='primaryColumn'] div[role='textbox'][data-testid*='tweetTextarea'][contenteditable='true']",
        composer_fallback,
    )
    # Choose the closest block joining this editor to a POST button. The entire
    # primary column, articles and dialogs must never act as composer blocks.
    composer_container_path = (
        "xpath=ancestor::*[.//*[@data-testid='tweetButtonInline' or @data-testid='tweetButton']]"
        "[ancestor::*[@data-testid='primaryColumn']]"
        "[not(self::article) and not(@role='dialog')][1]"
    )
    post_button_selectors = ("[data-testid='tweetButtonInline']", "[data-testid='tweetButton']")
    login_selector = "input[name='text'], input[name='password'], [data-testid='loginButton']"
    challenge_selector = (
        "input[name='challenge_response'], [data-testid='ocfEnterTextTextInput'], "
        "[data-testid='ocfEnterTextNextButton']"
    )
    authenticated_selector = (
        "[data-testid='SideNav_AccountSwitcher_Button'], a[data-testid='AppTabBar_Profile_Link']"
    )
    candidate_selector = "div[role='textbox'], [contenteditable='true'], [aria-label='Post text']"
    error_selector = "[data-testid='error-detail'], [role='alert']"
    profile_link_selector = "a[data-testid='AppTabBar_Profile_Link']"
    tweet_selector = "article[data-testid='tweet']"

    def __init__(
        self, manager: Any, *, confirmation_timeout: float = 8.0, poll_interval: float = 0.4,
        home_ready_timeout: float = 12.0, navigation_timeout_ms: float = 15000,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if home_ready_timeout < 0 or navigation_timeout_ms <= 0 or poll_interval <= 0:
            raise ValueError("readiness timeout must be non-negative; navigation and polling must be positive")
        self.manager = manager
        self.confirmation_timeout = confirmation_timeout
        self.poll_interval = poll_interval
        self.home_ready_timeout = home_ready_timeout
        self.navigation_timeout_ms = navigation_timeout_ms
        self._sleep = sleep
        self._clock = clock

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
                    page, selection = await self._select_page(session.browser)
                    result = await self._post(page, text, page_selection=selection)
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

    @classmethod
    async def _select_page(cls, context: Any) -> tuple[Any, dict[str, object]]:
        """Reuse an X tab in this profile's context, otherwise create our own."""
        pages = [page for page in context.pages if not page.is_closed()]
        x_pages = [page for page in pages if urlsplit(str(page.url)).hostname in {"x.com", "www.x.com"}]
        home_pages = [page for page in x_pages if urlsplit(str(page.url)).path.rstrip("/") == "/home"]
        chosen = home_pages or x_pages
        page = chosen[0] if chosen else await context.new_page()
        return page, {
            "pages_before": len(pages), "pages": len(context.pages),
            "page_selection": "existing_x_page" if chosen else "new_controlled_page",
            "initial_url": cls._safe_url(str(page.url)),
            "selected_page": cls._safe_url(str(page.url)),
        }

    async def _post(
        self, page: Any, text: str, *, page_selection: dict[str, object] | None = None
    ) -> XPostActionResult:
        logger.info("X_POST_STARTED")
        started = self._clock()
        await page.goto(self.home_url, wait_until="domcontentloaded", timeout=self.navigation_timeout_ms)
        timings: dict[str, object] = {"navigation_ms": round((self._clock() - started) * 1000, 1)}
        classification, active_composer = await self._wait_for_home_ready(page, timings)
        if active_composer is None:
            diagnostics = await self._home_diagnostics(page, classification)
            diagnostics.update(page_selection or {})
            diagnostics["selected_page"] = self._safe_url(str(page.url))
            diagnostics["timings"] = timings
            reason = {
                "LOGIN": "LOGIN_REQUIRED", "CHALLENGE": "CHALLENGE_REQUIRED",
            }.get(classification, "COMPOSER_NOT_FOUND")
            logger.bind(x_post_diagnostics=diagnostics).warning(
                "X_HOME_NOT_READY reason={} diagnostics={}", reason, diagnostics
            )
            return XPostActionResult("FAILED_PRE_SUBMIT", reason=reason, diagnostics=diagnostics)
        composer, container, selector = active_composer
        logger.info("X_HOME_READY composer_selector={}", selector)
        logger.info("COMPOSER_READY")
        await composer.fill(text)
        if text not in ((await composer.text_content()) or ""):
            return XPostActionResult("FAILED_PRE_SUBMIT", reason="composer_did_not_retain_text")
        button = await self._first_enabled(container)
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

    async def _wait_for_home_ready(
        self, page: Any, timings: dict[str, object]
    ) -> tuple[str, tuple[Any, Any, str] | None]:
        started = self._clock()
        deadline = started + self.home_ready_timeout
        while True:
            classification = await self._session_classification(page)
            elapsed = round((self._clock() - started) * 1000, 1)
            if classification in {"LOGIN", "CHALLENGE"}:
                timings["readiness_ms"] = elapsed
                return classification, None
            if classification == "AUTHENTICATED":
                timings.setdefault("authenticated_navigation_ms", elapsed)
            composer = await self._find_composer(page)
            if composer is not None:
                timings["composer_ms"] = round((self._clock() - started) * 1000, 1)
                return "AUTHENTICATED", composer
            # Authentication markers alone are not HOME_READY. Hydration can
            # render the navigation before it renders the editable surface.
            if self._clock() >= deadline:
                timings["readiness_ms"] = elapsed
                return classification, None
            await self._sleep(max(0.0, min(self.poll_interval, deadline - self._clock())))

    async def _session_classification(self, page: Any) -> str:
        path = urlsplit(str(page.url)).path.lower()
        if path.startswith("/account/access") or await self._has_visible(page, self.challenge_selector):
            return "CHALLENGE"
        if path.startswith(("/i/flow/login", "/login", "/signup")) or await self._has_visible(page, self.login_selector):
            return "LOGIN"
        if await self._has_visible(page, self.authenticated_selector):
            return "AUTHENTICATED"
        return "UNKNOWN"

    async def _find_composer(self, page: Any) -> tuple[Any, Any, str] | None:
        if urlsplit(str(page.url)).hostname not in {"x.com", "www.x.com"}:
            return None
        if urlsplit(str(page.url)).path.rstrip("/") != "/home":
            return None
        for selector in self.composer_selectors:
            editors = page.locator(selector)
            matches = []
            for index in range(min(await editors.count(), 12)):
                editor = editors.nth(index)
                if not await editor.is_visible():
                    continue
                # A tweetTextarea testid can be on a wrapper rather than on
                # the actual editing surface. is_editable would throw for an
                # ordinary div, aborting readiness before the safe fallback.
                if await editor.get_attribute("contenteditable", timeout=500) != "true":
                    continue
                if not await editor.is_editable(timeout=500):
                    continue
                # Never accept an editor living inside a timeline article, DM,
                # search form, or dialog, even if it imitates a tweet testid.
                excluded = editor.locator(
                    "xpath=ancestor::*[self::article or @role='dialog' or @role='search' "
                    "or @data-testid='DMDrawer' or @data-testid='DmActivityViewport']"
                )
                if await excluded.count():
                    continue
                containers = editor.locator(self.composer_container_path)
                if await containers.count() != 1:
                    continue
                container = containers.first
                if await self._unique_visible_button(container) is not None:
                    matches.append((editor, container, selector))
            # Ambiguous visible Home composers are not safe to fill.
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                return None
        return None

    @classmethod
    async def _unique_visible_button(cls, container: Any) -> Any | None:
        buttons = container.locator(", ".join(cls.post_button_selectors))
        count = await buttons.count()
        if count > 12:
            return None
        visible = [buttons.nth(i) for i in range(count)
                   if await buttons.nth(i).is_visible()]
        return visible[0] if len(visible) == 1 else None

    @staticmethod
    async def _has_visible(page: Any, selector: str) -> bool:
        locators = page.locator(selector)
        for index in range(min(await locators.count(), 12)):
            if await locators.nth(index).is_visible():
                return True
        return False

    async def _home_diagnostics(self, page: Any, classification: str) -> dict[str, object]:
        """Read only bounded structural facts. Never include editor text or HTML."""
        diagnostics: dict[str, object] = {
            "url": self._safe_url(str(page.url)), "session_classification": classification,
            "authenticated": classification == "AUTHENTICATED",
        }
        try:
            diagnostics["title"] = (await page.title())[:160]
            for name, selector in (
                ("tweetTextarea", self.composer_selector),
                ("contenteditables", "[contenteditable='true']"),
                ("role_textboxes", "div[role='textbox']"),
                ("post_text_candidates", "[aria-label='Post text']"),
                ("articles", "article"),
            ):
                diagnostics[name] = await page.locator(selector).count()
            candidates = page.locator(self.candidate_selector)
            snapshots = []
            for index in range(min(await candidates.count(), 12)):
                candidate = candidates.nth(index)
                # Fixed projection local to CPM, not client-supplied evaluate.
                snapshot = await candidate.evaluate(
                    """element => ({
                        tag: element.tagName.toLowerCase(),
                        role: element.getAttribute('role'),
                        data_testid: element.getAttribute('data-testid'),
                        aria_label: element.getAttribute('aria-label'),
                        contenteditable: element.getAttribute('contenteditable'),
                        placeholder: element.getAttribute('placeholder')
                    })""", timeout=500,
                )
                snapshot = {key: value[:120] if isinstance(value, str) else value
                            for key, value in snapshot.items()}
                snapshot["visible"] = await candidate.is_visible()
                snapshot["enabled"] = await candidate.is_enabled(timeout=500)
                supports_editing = (
                    snapshot.get("contenteditable") == "true"
                    or snapshot.get("tag") in {"input", "textarea"}
                )
                snapshot["editable"] = (
                    await candidate.is_editable(timeout=500) if supports_editing else None
                )
                snapshots.append(snapshot)
            diagnostics["editor_candidates"] = snapshots
        except Exception as exc:
            # Diagnostics must never replace the known pre-submit failure.
            diagnostics["diagnostic_read_error"] = type(exc).__name__
        return diagnostics

    @staticmethod
    def _safe_url(url: str) -> str:
        parsed = urlsplit(url)
        if parsed.scheme == "about" and parsed.path == "blank":
            return "about:blank"
        # Drop query, fragment and user info; these can carry session secrets.
        return f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path}" if parsed.hostname else parsed.scheme + ":"

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

    @classmethod
    async def _first_enabled(cls, container: Any) -> Any | None:
        button = await cls._unique_visible_button(container)
        return button if button is not None and await button.is_enabled() else None

    @staticmethod
    async def _first_text(page: Any, selector: str) -> str | None:
        locator = page.locator(selector)
        return await locator.first.text_content() if await locator.count() else None
