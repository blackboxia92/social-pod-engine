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
