from __future__ import annotations

import pytest

from camoufox_pm.api.dependencies import get_profile_manager
from camoufox_pm.core.browser_session import BrowserSession


class _Locator:
    async def count(self) -> int:
        return 1

    async def fill(self, value: str) -> None:
        self.value = value

    async def click(self) -> None:
        return None

    async def get_attribute(self, name: str) -> str | None:
        return "/operator" if name == "href" else None


class _Page:
    url = "https://x.com/home"

    async def goto(self, url: str) -> None:
        self.url = url

    def locator(self, selector: str) -> _Locator:
        del selector
        return _Locator()


class _Context:
    def __init__(self) -> None:
        self.pages = [_Page()]

    async def new_page(self) -> _Page:
        page = _Page()
        self.pages.append(page)
        return page


@pytest.mark.asyncio
async def test_remote_control_is_profile_bound_and_invalidated_on_close(client, monkeypatch):
    first = (await client.post("/api/profiles", json={"name": "first"})).json()["id"]
    second = (await client.post("/api/profiles", json={"name": "second"})).json()["id"]
    manager = get_profile_manager()
    manager.browser_sessions.active_sessions[first] = BrowserSession(first, None, browser=_Context())

    async def launch(profile_id: str, **kwargs):
        assert profile_id == first
        assert kwargs["headless"] is False
        return {"status": "already_running", "message": "lease-owned browser", "process_id": 1}

    monkeypatch.setattr(manager, "launch_browser", launch)
    launched = await client.post(f"/api/profiles/{first}/launch-remote", json={"headless": False})
    assert launched.status_code == 200
    remote = launched.json()["remote_control"]
    assert remote["type"] == "http_page_rpc"

    headers = {"X-Remote-Control-Handle": remote["handle"]}
    current = await client.post(
        f"/api/profiles/{first}/remote/page", json={"operation": "url"}, headers=headers
    )
    assert current.json()["result"]["url"] == "https://x.com/home"
    wrong_profile = await client.post(
        f"/api/profiles/{second}/remote/page", json={"operation": "url"}, headers=headers
    )
    assert wrong_profile.status_code == 409

    closed = await client.post(f"/api/profiles/{first}/close")
    assert closed.status_code == 200
    expired = await client.post(
        f"/api/profiles/{first}/remote/page", json={"operation": "url"}, headers=headers
    )
    assert expired.status_code == 409
    assert (await client.get(f"/api/profiles/{first}")).status_code == 200
