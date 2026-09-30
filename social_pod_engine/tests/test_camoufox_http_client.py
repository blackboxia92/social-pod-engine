from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from social_pod_engine.integrations.camoufox_http import (
    CamoufoxHttpClient,
    CamoufoxHttpError,
    CamoufoxHttpGateway,
    CamoufoxHttpUnavailable,
    CamoufoxPageAccessUnavailable,
)


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
async def test_launch_and_close_are_sent_once_and_expose_no_page_handle():
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
    with pytest.raises(CamoufoxPageAccessUnavailable):
        gateway.require_playwright_page()


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


def test_http_client_has_no_upstream_internal_imports():
    source = pathlib.Path(__file__).parents[1] / "integrations" / "camoufox_http.py"
    text = source.read_text(encoding="utf-8")
    assert "from camoufox" not in text
    assert "import camoufox" not in text
