from types import SimpleNamespace

import pytest

from camoufox_pm.actions.x_post import XPostWorker


class Locator:
    def __init__(self, *, text="", href=None, visible=True, enabled=True, count=1):
        self.text, self.href, self.visible, self.enabled, self._count = text, href, visible, enabled, count
        self.clicks = 0
        self.first = self

    async def count(self): return self._count
    async def is_visible(self): return self.visible
    async def is_enabled(self): return self.enabled
    async def fill(self, text): self.text = text
    async def text_content(self): return self.text
    async def get_attribute(self, name): return self.href if name == "href" else None
    async def click(self): self.clicks += 1
    def nth(self, _): return self
    def locator(self, selector): return self


class Page:
    def __init__(self, *, status_url="", error="", composer=True, button=True):
        self.url = status_url or "https://x.com/home"
        self.composer = Locator(count=1 if composer else 0)
        self.button = Locator(count=1 if button else 0, enabled=button)
        self.error = Locator(text=error, count=1 if error else 0)
        self.profile = Locator(href="/demo", count=0)

    async def goto(self, url): self.url = url
    def locator(self, selector):
        if "tweetButton" in selector: return self.button
        if "tweetTextarea" in selector or "Post text" in selector: return self.composer
        if "error-detail" in selector or "[role='alert']" in selector: return self.error
        if "Profile_Link" in selector: return self.profile
        return Locator(count=0)


class ReceiptStorage:
    async def get_external_action_receipt(self, _):
        return {
            "state": "COMPLETED", "attempted": True, "confirmed": True,
            "external_id": "existing", "external_url": "https://x.com/demo/status/existing",
            "reason": None, "profile_id": "profile", "execution_task_id": "task",
        }


@pytest.mark.asyncio
async def test_completed_post_clicks_exactly_once_and_confirms_status_url():
    page = Page(status_url="https://x.com/home")
    original = page.button.click
    async def click():
        await original(); page.url = "https://x.com/demo/status/123"
    page.button.click = click
    result = await XPostWorker(SimpleNamespace())._post(page, "hello")
    assert result.state == "COMPLETED" and result.confirmed
    assert result.external_id == "123" and page.button.clicks == 1


@pytest.mark.asyncio
async def test_missing_composer_is_pre_submit_failure_without_click():
    page = Page(composer=False)
    result = await XPostWorker(SimpleNamespace())._post(page, "hello")
    assert result.state == "FAILED_PRE_SUBMIT" and result.reason == "COMPOSER_NOT_FOUND"
    assert page.button.clicks == 0


@pytest.mark.asyncio
async def test_platform_error_after_click_is_known_rejection():
    page = Page(error="Try again")
    result = await XPostWorker(SimpleNamespace(), confirmation_timeout=0)._post(page, "hello")
    assert result.state == "PLATFORM_REJECTED" and result.attempted
    assert page.button.clicks == 1


@pytest.mark.asyncio
async def test_existing_idempotency_receipt_never_reopens_or_reposts():
    manager = SimpleNamespace(storage=ReceiptStorage())
    result = await XPostWorker(manager).execute(
        profile_id="profile", text="hello", idempotency_key="fixed", execution_task_id="task"
    )
    assert result.state == "COMPLETED" and result.external_id == "existing"
    assert result.diagnostics == {"receipt_reused": True}
