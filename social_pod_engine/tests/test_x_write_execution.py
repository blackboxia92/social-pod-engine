from __future__ import annotations

import pytest

from social_pod_engine.adapters.base import Capability, ExecutionContext, ExternalExecutionResult
from social_pod_engine.adapters.x import XAdapter


class FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self.page, self.selector = page, selector

    async def count(self) -> int:
        return 1

    async def fill(self, text: str) -> None:
        self.page.text = text

    async def click(self) -> None:
        self.page.url = "https://x.com/example/status/12345"


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
        return 1

    async def fill(self, text: str) -> None:
        self.page.text = text

    async def click(self) -> None:
        self.page.clicks += 1
        if self.page.direct_status_url is not None:
            self.page.url = self.page.direct_status_url

    async def get_attribute(self, name: str) -> str | None:
        if name == "href" and "Profile_Link" in self.selector:
            return "/authenticated"
        return None


class ConfirmationPage:
    def __init__(self, snapshots, *, direct_status_url: str | None = None) -> None:
        self.url = "https://x.com/home"
        self.text = ""
        self.snapshots = list(snapshots)
        self.direct_status_url = direct_status_url
        self.clicks = 0
        self.visited: list[str] = []

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
