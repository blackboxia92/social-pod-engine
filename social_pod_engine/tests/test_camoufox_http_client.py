from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from social_pod_engine.execution.gateway import ExecutionGatewayError, ExecutionGatewayFailureKind
from social_pod_engine.integrations.camoufox_http import (
    CamoufoxHttpClient,
    CamoufoxHttpConflict,
    CamoufoxHttpError,
    CamoufoxHttpGateway,
    CamoufoxHttpNotFound,
    CamoufoxHttpUnavailable,
)
from social_pod_engine.onboarding import UpstreamProfileNotFound


def _transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_service_status_and_profile_listing_use_public_versioned_routes():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "healthy", "api_version": "0.5", "database": "connected", "profiles_count": 1})
        assert request.url.path == "/api/v1/profiles"
        return httpx.Response(
            200,
            json={
                "profiles": [{"id": "profile-1", "name": "X one", "group": None, "status": "active"}],
                "total": 1,
                "page": 1,
                "per_page": 100,
                "has_next": False,
                "has_prev": False,
            },
        )

    client = CamoufoxHttpClient(transport=_transport(handler))
    status = await client.service_status()
    profiles = await client.list_profiles()

    assert status.available is True
    assert profiles[0].id == "profile-1"
    assert [request.url.path for request in requests] == ["/health", "/api/v1/profiles"]


@pytest.mark.asyncio
async def test_launch_and_close_are_sent_once():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/launch"):
            assert json.loads(request.content) == {"headless": False}
            return httpx.Response(
                200,
                json={"profile_id": "profile-1", "status": "launched", "message": "ok", "process_id": 456},
            )
        return httpx.Response(200, json={"profile_id": "profile-1", "status": "closed", "message": "ok"})

    gateway = CamoufoxHttpGateway(CamoufoxHttpClient(transport=_transport(handler), retries=3))
    launch = await gateway.launch_profile("profile-1")
    close = await gateway.close_profile_browser("profile-1")

    assert launch.process_id == 456
    assert close == "closed"
    assert calls == ["/api/v1/profiles/profile-1/launch", "/api/v1/profiles/profile-1/close"]


@pytest.mark.asyncio
async def test_profile_creation_sends_only_non_secret_identity_fields():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/profiles"
        assert json.loads(request.content) == {
            "name": "operator-x",
            "group": "cohort-a",
            "generate_fingerprint": True,
        }
        return httpx.Response(
            201,
            json={"id": "profile-2", "name": "operator-x", "group": "cohort-a", "status": "active"},
        )

    profile = await CamoufoxHttpClient(transport=_transport(handler)).create_profile(
        name=" operator-x ", group="cohort-a"
    )

    assert profile.id == "profile-2"


@pytest.mark.asyncio
async def test_remote_page_uses_only_the_profile_bound_handle():
    calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path.endswith("launch-remote"):
            return httpx.Response(
                200,
                json={
                    "profile_id": "profile-1",
                    "status": "launched",
                    "message": "ok",
                    "remote_control": {
                        "type": "http_page_rpc",
                        "endpoint": "/api/v1/profiles/profile-1/remote/page",
                        "handle": "temporary-handle",
                        "url": "https://x.com/home",
                    },
                },
            )
        assert request.headers["x-remote-control-handle"] == "temporary-handle"
        return httpx.Response(200, json={"result": {"url": "https://x.com/compose"}})

    client = CamoufoxHttpClient(transport=_transport(handler))
    remote = await client.launch_remote_profile("profile-1")
    from social_pod_engine.integrations.camoufox_http import RemotePage

    page = RemotePage(client, remote)
    await page.goto("https://x.com/compose")
    assert page.url == "https://x.com/compose"
    assert calls[1][1] == {"operation": "goto", "selector": None, "value": "https://x.com/compose"}


@pytest.mark.asyncio
async def test_remote_page_reads_bounded_locator_snapshots_without_remote_evaluation():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        if request.url.path.endswith("launch-remote"):
            return httpx.Response(
                200,
                json={
                    "profile_id": "profile-1",
                    "status": "launched",
                    "message": "ok",
                    "remote_control": {
                        "type": "http_page_rpc",
                        "endpoint": "/api/v1/profiles/profile-1/remote/page",
                        "handle": "temporary-handle",
                        "url": "https://x.com/home",
                    },
                },
            )
        assert payload == {
            "operation": "locator_snapshots",
            "selector": "article",
            "value": None,
            "text_selector": ".text",
            "href_selector": "a[href*='/status/']",
        }
        return httpx.Response(
            200,
            json={
                "result": {
                    "url": "https://x.com/example",
                    "items": [{"text": "exact post", "href": "/example/status/42"}],
                }
            },
        )

    client = CamoufoxHttpClient(transport=_transport(handler))
    remote = await client.launch_remote_profile("profile-1")
    from social_pod_engine.integrations.camoufox_http import RemotePage

    snapshots = await RemotePage(client, remote).locator_snapshots(
        "article", text_selector=".text", href_selector="a[href*='/status/']"
    )

    assert [(item.text, item.href) for item in snapshots] == [("exact post", "/example/status/42")]


@pytest.mark.asyncio
async def test_http_errors_are_actionable_and_safe_get_retries():
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectTimeout("not running", request=request)
        return httpx.Response(401, json={"detail": "Authentication required"})

    client = CamoufoxHttpClient(transport=_transport(handler), retries=1)
    with pytest.raises(CamoufoxHttpError, match="HTTP 401: Authentication required"):
        await client.service_status()
    assert attempts == 2


@pytest.mark.asyncio
async def test_service_unavailable_has_operator_facing_message():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(CamoufoxHttpUnavailable, match="Start the service"):
        await CamoufoxHttpClient(transport=_transport(handler), retries=0).service_status()


@pytest.mark.asyncio
async def test_gateway_translates_a_missing_profile_to_the_onboarding_contract():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/profiles/stale-profile"
        return httpx.Response(404, json={"detail": "Profile not found"})

    client = CamoufoxHttpClient(transport=_transport(handler), retries=0)
    with pytest.raises(CamoufoxHttpNotFound, match="HTTP 404"):
        await client.get_profile("stale-profile")
    with pytest.raises(UpstreamProfileNotFound, match="does not exist"):
        await CamoufoxHttpGateway(client).get_profile("stale-profile")


@pytest.mark.asyncio
async def test_execution_gateway_uses_headless_remote_launch_while_onboarding_stays_visible():
    launch_payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("launch-remote"):
            launch_payloads.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "profile_id": "profile-1",
                    "status": "launched",
                    "message": "ok",
                    "remote_control": {
                        "endpoint": "/api/v1/profiles/profile-1/remote/page",
                        "handle": f"handle-{len(launch_payloads)}",
                        "url": "https://x.com/home",
                    },
                },
            )
        assert request.url.path.endswith("/close")
        return httpx.Response(200, json={"status": "closed"})

    gateway = CamoufoxHttpGateway(CamoufoxHttpClient(transport=_transport(handler)))
    execution_lease = await gateway.acquire_lease("profile-1", proxy_id=None)
    execution_browser = await gateway.open_execution_browser(execution_lease)
    await gateway.close_execution_browser(execution_browser)
    await gateway.release_lease(execution_lease)
    onboarding_lease = await gateway.acquire_lease("profile-1", proxy_id=None)
    onboarding_browser = await gateway.open_interactive_browser(onboarding_lease)
    await gateway.close_interactive_browser(onboarding_browser)
    await gateway.release_lease(onboarding_lease)

    assert launch_payloads == [{"headless": True}, {"headless": False}]


@pytest.mark.asyncio
async def test_execution_gateway_classifies_conflicting_cpm_profile_lease():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("launch-remote")
        return httpx.Response(409, json={"detail": "Profile is leased by another holder"})

    client = CamoufoxHttpClient(transport=_transport(handler))
    with pytest.raises(CamoufoxHttpConflict, match="HTTP 409"):
        await client.launch_remote_profile("profile-1", headless=True)
    gateway = CamoufoxHttpGateway(client)
    lease = await gateway.acquire_lease("profile-1", proxy_id=None)
    with pytest.raises(ExecutionGatewayError) as error:
        await gateway.open_execution_browser(lease)
    await gateway.release_lease(lease)

    assert error.value.kind is ExecutionGatewayFailureKind.PROFILE_BUSY


def test_http_client_has_no_upstream_internal_imports():
    source = pathlib.Path(__file__).parents[1] / "integrations" / "camoufox_http.py"
    text = source.read_text(encoding="utf-8")
    assert "from camoufox" not in text
    assert "import camoufox" not in text
