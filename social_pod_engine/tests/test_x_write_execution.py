from __future__ import annotations

import pytest

from social_pod_engine.adapters.base import Capability, ExecutionContext, ExternalExecutionResult
from social_pod_engine.adapters.x import XAdapter


class FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self.page, self.selector = page, selector

    async def count(self) -> int:
        if self.selector in (
            "input[name='challenge_response']",
            "[data-testid='ocfEnterTextTextInput']",
            "[data-testid='ocfEnterTextNextButton']",
            "input[name='text']",
            "input[name='password']",
            "[data-testid='loginButton']",
        ):
            return 0
        return 1

    async def fill(self, text: str) -> None:
        self.page.text = text

    async def click(self) -> None:
        self.page.url = "https://x.com/example/status/12345"

    async def get_attribute(self, name: str) -> str | None:
        if name == "contenteditable" and "tweetTextarea" in self.selector:
            return "true"
        if name == "data-testid" and "tweetButton" in self.selector:
            return "tweetButton"
        return None

    async def is_visible(self) -> bool:
        return True

    async def is_enabled(self) -> bool:
        return True

    async def text_content(self) -> str | None:
        return self.page.text if "tweetTextarea" in self.selector else None


class FakePage:
    def __init__(self) -> None:
        self.url = "https://x.com/home"
        self.text = ""

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


class ConfirmationLocator:
    def __init__(self, page, selector: str) -> None:
        self.page, self.selector = page, selector

    async def count(self) -> int:
        if self.selector in (
            "input[name='challenge_response']",
            "[data-testid='ocfEnterTextTextInput']",
            "[data-testid='ocfEnterTextNextButton']",
            "input[name='text']",
            "input[name='password']",
            "[data-testid='loginButton']",
        ):
            return 0
        if "error-detail" in self.selector or "[role='alert']" in self.selector:
            return int(self.page.error_visible)
        if "[data-testid='toast']" in self.selector or "[role='status']" in self.selector:
            return int(self.page.toast_visible)
        if "tweetButton" in self.selector and not self.page.button_visible:
            return 0
        return 1

    async def fill(self, text: str) -> None:
        self.page.text = text

    async def click(self) -> None:
        if "tweetButton" in self.selector:
            self.page.clicks += 1
            if self.page.clear_composer_on_click:
                self.page.text = ""
            if self.page.after_click_text is not None:
                self.page.text = self.page.after_click_text
            if self.page.direct_status_url is not None:
                self.page.url = self.page.direct_status_url

    async def get_attribute(self, name: str) -> str | None:
        if name == "href" and "Profile_Link" in self.selector:
            return "/authenticated"
        if name == "contenteditable" and "tweetTextarea" in self.selector:
            return "true"
        if name == "disabled" and "tweetButton" in self.selector and not self.page.button_enabled:
            return ""
        if name == "aria-disabled" and "tweetButton" in self.selector and not self.page.button_enabled:
            return "true"
        if name == "data-testid" and "tweetButton" in self.selector:
            return "tweetButton"
        return None

    async def is_visible(self) -> bool:
        return self.page.button_visible if "tweetButton" in self.selector else True

    async def is_enabled(self) -> bool:
        return self.page.button_enabled if "tweetButton" in self.selector else True

    async def text_content(self) -> str | None:
        return self.page.text if "tweetTextarea" in self.selector else None


class ConfirmationPage:
    def __init__(
        self,
        snapshots,
        *,
        direct_status_url: str | None = None,
        button_enabled: bool = True,
        button_visible: bool = True,
        clear_composer_on_click: bool = True,
        error_visible: bool = False,
        toast_visible: bool = False,
        after_click_text: str | None = None,
    ) -> None:
        self.url = "https://x.com/home"
        self.text = ""
        self.snapshots = list(snapshots)
        self.direct_status_url = direct_status_url
        self.clicks = 0
        self.visited: list[str] = []
        self.button_enabled = button_enabled
        self.button_visible = button_visible
        self.clear_composer_on_click = clear_composer_on_click
        self.error_visible = error_visible
        self.toast_visible = toast_visible
        self.after_click_text = after_click_text

    def locator(self, selector: str) -> ConfirmationLocator:
        return ConfirmationLocator(self, selector)

    async def goto(self, url: str) -> None:
        self.url = url
        self.visited.append(url)

    async def locator_snapshots(self, selector: str, *, text_selector: str, href_selector: str):
        assert selector == "article[data-testid='tweet']"
        assert text_selector == "[data-testid='tweetText']"
        assert href_selector == "a[href*='/status/']"
        return [type("Snapshot", (), {"text": text, "href": href}) for text, href in self.snapshots]


class DiscoveryLocator:
    def __init__(self, page, selector: str) -> None:
        self.page, self.selector = page, selector

    async def count(self) -> int:
        return int(self.page.matches(self.selector))

    async def fill(self, text: str) -> None:
        self.page.filled_selector = self.selector
        self.page.text = text

    async def click(self) -> None:
        if "tweetButton" in self.selector:
            self.page.clicks += 1
            self.page.url = "https://x.com/operator/status/99"

    async def get_attribute(self, name: str) -> str | None:
        if name == "contenteditable" and self.page.is_composer_selector(self.selector):
            return "true"
        if name == "data-testid" and "tweetButton" in self.selector:
            return "tweetButton"
        if name == "href" and "Profile_Link" in self.selector:
            return "/operator"
        return None

    async def is_visible(self) -> bool:
        return bool(self.page.matches(self.selector))

    async def is_enabled(self) -> bool:
        return bool(self.page.matches(self.selector))

    async def text_content(self) -> str | None:
        return self.page.text if self.page.is_composer_selector(self.selector) else None


class DiscoveryPage:
    fallback_selector = (
        "[data-testid='primaryColumn'] div[role='textbox'][contenteditable='true']"
        "[aria-label='Post text']"
    )

    def __init__(self, *, mode: str = "fallback", delayed: bool = False) -> None:
        self.url = "https://x.com/home" if mode != "login" else "https://x.com/i/flow/login"
        self.mode = mode
        self.delayed = delayed
        self.ready = not delayed
        self.text = ""
        self.clicks = 0
        self.filled_selector: str | None = None
        self.visited: list[str] = []

    async def goto(self, url: str) -> None:
        self.url = url
        self.visited.append(url)

    def locator(self, selector: str) -> DiscoveryLocator:
        return DiscoveryLocator(self, selector)

    def is_composer_selector(self, selector: str) -> bool:
        return selector == self.fallback_selector

    def matches(self, selector: str) -> int:
        if self.mode == "login":
            return int(selector in ("input[name='text']", "[data-testid='loginButton']"))
        if self.mode == "challenge":
            return int(selector == "input[name='challenge_response']")
        if selector == self.fallback_selector:
            return int(self.mode == "fallback" and self.ready)
        if selector in (
            "[data-testid='tweetTextarea_0']",
            "[data-testid='tweetTextarea_0'][contenteditable='true']",
            "div[role='textbox'][data-testid*='tweetTextarea']",
        ):
            return 0
        if selector in (
            "[data-testid='SideNav_AccountSwitcher_Button']",
            "a[data-testid='AppTabBar_Profile_Link']",
        ):
            return int(self.mode in ("fallback", "missing"))
        if "tweetButton" in selector:
            return int(self.mode == "fallback" and self.ready)
        if selector == "div[role='textbox']":
            return 2 if self.mode == "missing" else 3
        if selector == "[contenteditable='true']":
            return 1 if self.mode == "missing" else 2
        if selector == "[data-testid]":
            return 7
        return 0

    async def element_snapshots(self, selector: str):
        assert selector == "div[role='textbox'], [contenteditable='true']"
        return [
            {
                "tag": "div",
                "role": "textbox",
                "data_testid": "SearchBox_Search_Input",
                "aria_label": "Search query",
                "contenteditable": "true",
                "placeholder": "Search",
                "text": "",
            },
            {
                "tag": "div",
                "role": "textbox",
                "data_testid": "dmComposerTextInput",
                "aria_label": "Message",
                "contenteditable": "true",
                "placeholder": "",
                "text": "",
            },
        ]


@pytest.mark.asyncio
async def test_x_post_requires_final_text_and_returns_confirmed_external_reference():
    page = FakePage()
    result = await XAdapter().execute_capability(
        Capability.POST,
        {"content_draft_id": "draft-1", "revision": 1, "content_type": "post", "text": "Prueba controlada"},
        ExecutionContext(page=page),
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is True
    assert result.external_id == "12345"
    assert page.text == "Prueba controlada"


@pytest.mark.asyncio
async def test_x_post_does_not_accept_empty_content():
    with pytest.raises(ValueError, match="non-empty"):
        await XAdapter().execute_capability(Capability.POST, {"text": ""}, ExecutionContext(page=FakePage()))


@pytest.mark.asyncio
async def test_x_post_confirms_from_exact_text_on_profile_when_home_url_does_not_change():
    page = ConfirmationPage(
        [
            ("a pinned old post", "/authenticated/status/old-post"),
            ("Exact post published", "/authenticated/status/new-post"),
        ]
    )
    result = await XAdapter(confirmation_timeout_seconds=0).execute_capability(
        Capability.POST,
        {"text": "Exact post published"},
        ExecutionContext(page=page),
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is True
    assert result.external_id == "new-post"
    assert result.external_url == "https://x.com/authenticated/status/new-post"
    assert page.visited == ["https://x.com/authenticated"]
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_x_post_does_not_confirm_an_old_or_pinned_post_with_different_text():
    page = ConfirmationPage([("a pinned old post", "/authenticated/status/old-post")])
    result = await XAdapter(confirmation_timeout_seconds=0).execute_capability(
        Capability.POST,
        {"text": "Exact post published"},
        ExecutionContext(page=page),
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is False
    assert result.external_id is None
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_x_post_polls_for_profile_evidence_without_a_second_click():
    page = ConfirmationPage([])

    async def publish_on_poll(_: float) -> None:
        page.snapshots = [("Exact post published", "/authenticated/status/eventual-post")]

    result = await XAdapter(
        confirmation_timeout_seconds=1,
        confirmation_poll_interval_seconds=0.1,
        sleep=publish_on_poll,
    ).execute_capability(Capability.POST, {"text": "Exact post published"}, ExecutionContext(page=page))

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is True
    assert result.external_id == "eventual-post"
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_x_post_records_composer_text_without_persisting_the_plaintext_in_diagnostics():
    page = ConfirmationPage([], button_enabled=False)
    result = await XAdapter(submit_ready_timeout_seconds=0).execute_capability(
        Capability.POST, {"text": "Contenido privado de prueba"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.success is False
    assert result.metadata["post_submit_status"] == "POST_SUBMIT_NOT_STARTED"
    assert result.metadata["composer"]["text_present"] is True
    assert result.metadata["composer"]["text_length"] == len("Contenido privado de prueba")
    assert "Contenido privado de prueba" not in repr(result.metadata)
    assert page.clicks == 0


@pytest.mark.asyncio
async def test_x_post_waits_for_an_enabled_button_then_clicks_exactly_once():
    page = ConfirmationPage(
        [("Post habilitado", "/authenticated/status/enabled-post")], button_enabled=False
    )
    polls = 0

    async def enable_on_poll(_: float) -> None:
        nonlocal polls
        polls += 1
        page.button_enabled = True

    result = await XAdapter(
        submit_ready_timeout_seconds=1,
        submit_ready_poll_interval_seconds=0.1,
        confirmation_timeout_seconds=0,
        sleep=enable_on_poll,
    ).execute_capability(Capability.POST, {"text": "Post habilitado"}, ExecutionContext(page=page))

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is True
    assert polls == 1
    assert page.clicks == 1
    assert result.metadata["post_button_clicked"] is True


@pytest.mark.asyncio
async def test_x_post_with_cleared_composer_attempts_timeline_confirmation():
    page = ConfirmationPage([("Post confirmado", "/authenticated/status/confirmed")])
    result = await XAdapter(confirmation_timeout_seconds=0).execute_capability(
        Capability.POST, {"text": "Post confirmado"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is True
    assert result.metadata["post_click"]["composer_cleared"] is True
    assert page.visited == ["https://x.com/authenticated"]
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_x_post_with_retained_composer_and_error_is_a_known_platform_rejection():
    page = ConfirmationPage([], clear_composer_on_click=False, error_visible=True)
    result = await XAdapter(confirmation_timeout_seconds=0).execute_capability(
        Capability.POST, {"text": "Post rechazado"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.success is False
    assert result.metadata["post_submit_status"] == "PLATFORM_REJECTED"
    assert result.metadata["post_click"]["composer_retained_text"] is True
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_x_post_with_ambiguous_post_click_state_does_not_click_again():
    page = ConfirmationPage([], after_click_text="different draft state")
    result = await XAdapter(confirmation_timeout_seconds=0).execute_capability(
        Capability.POST, {"text": "Post ambiguo"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is False
    assert result.metadata["post_submit_status"] == "UNKNOWN_EXTERNAL_STATE"
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_x_post_uses_the_scoped_post_text_fallback_not_another_textbox():
    page = DiscoveryPage(mode="fallback")
    result = await XAdapter().execute_capability(
        Capability.POST, {"text": "Fallback composer"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is True
    assert page.filled_selector == DiscoveryPage.fallback_selector
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_x_post_waits_for_home_composer_to_mount_before_filling():
    page = DiscoveryPage(mode="fallback", delayed=True)

    async def mount_composer(_: float) -> None:
        page.ready = True

    result = await XAdapter(
        home_ready_timeout_seconds=1,
        home_ready_poll_interval_seconds=0.1,
        sleep=mount_composer,
    ).execute_capability(Capability.POST, {"text": "Eventually ready"}, ExecutionContext(page=page))

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is True
    assert page.clicks == 1


@pytest.mark.asyncio
async def test_x_post_navigates_to_home_before_looking_for_the_composer():
    page = DiscoveryPage(mode="fallback")
    page.url = "https://x.com/explore"
    result = await XAdapter().execute_capability(
        Capability.POST, {"text": "Return home first"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.confirmed is True
    assert page.visited == ["https://x.com/home"]


@pytest.mark.asyncio
async def test_x_post_classifies_login_before_reporting_a_missing_composer():
    page = DiscoveryPage(mode="login")
    result = await XAdapter(home_ready_timeout_seconds=0).execute_capability(
        Capability.POST, {"text": "Never publish"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.metadata["post_submit_status"] == "SESSION_EXPIRED"
    assert result.metadata["detail"] == "LOGIN_REQUIRED"
    assert page.clicks == 0


@pytest.mark.asyncio
async def test_x_post_classifies_challenge_before_reporting_a_missing_composer():
    page = DiscoveryPage(mode="challenge")
    result = await XAdapter(home_ready_timeout_seconds=0).execute_capability(
        Capability.POST, {"text": "Never publish"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.metadata["post_submit_status"] == "CHALLENGE_REQUIRED"
    assert page.clicks == 0


@pytest.mark.asyncio
async def test_x_post_reports_bounded_dom_diagnostics_when_authenticated_composer_is_missing():
    page = DiscoveryPage(mode="missing")
    result = await XAdapter(home_ready_timeout_seconds=0).execute_capability(
        Capability.POST, {"text": "Never publish"}, ExecutionContext(page=page)
    )

    assert isinstance(result, ExternalExecutionResult)
    assert result.metadata["post_submit_status"] == "POST_SUBMIT_NOT_STARTED"
    assert "authenticated=True" in result.metadata["detail"]
    assert "textboxes=2" in result.metadata["detail"]
    diagnostics = result.metadata["page_diagnostics"]
    assert diagnostics["composer_candidates"][0]["data_testid"] == "SearchBox_Search_Input"
    assert page.clicks == 0
