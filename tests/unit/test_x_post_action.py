from types import SimpleNamespace

import pytest

from camoufox_pm.actions.x_post import XPostWorker
from tests.unit.test_x_post_readiness import Page, editor, worker


class ReceiptStorage:
    async def get_external_action_receipt(self, _):
        return {
            "state": "COMPLETED", "attempted": True, "confirmed": True,
            "external_id": "existing", "external_url": "https://x.com/demo/status/existing",
            "reason": None, "profile_id": "profile", "execution_task_id": "task",
        }


@pytest.mark.asyncio
async def test_completed_post_clicks_exactly_once_and_confirms_status_url():
    page = Page()
    composer = editor(page)
    button = composer.container.buttons[0]
    button.on_click = lambda: setattr(page, "url", "https://x.com/demo/status/123")
    result = await worker()._post(page, "hello")
    assert result.state == "COMPLETED" and result.confirmed
    assert result.external_id == "123" and button.clicks == 1
    assert composer.text == "hello"


@pytest.mark.asyncio
async def test_missing_composer_is_pre_submit_failure_without_click():
    page = Page()
    result = await worker()._post(page, "hello")
    assert result.state == "FAILED_PRE_SUBMIT" and result.reason == "COMPOSER_NOT_FOUND"
    assert not result.attempted


@pytest.mark.asyncio
async def test_platform_error_after_click_is_known_rejection():
    page = Page()
    composer = editor(page)
    page.error = "Try again"
    result = await worker()._post(page, "hello")
    assert result.state == "PLATFORM_REJECTED" and result.attempted
    assert composer.container.buttons[0].clicks == 1


@pytest.mark.asyncio
async def test_existing_idempotency_receipt_never_reopens_or_reposts():
    manager = SimpleNamespace(storage=ReceiptStorage())
    result = await XPostWorker(manager).execute(
        profile_id="profile", text="hello", idempotency_key="fixed", execution_task_id="task"
    )
    assert result.state == "COMPLETED" and result.external_id == "existing"
    assert result.diagnostics == {"receipt_reused": True}
