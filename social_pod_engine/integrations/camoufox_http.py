"""HTTP-only boundary for Camoufox Profile Manager's public API.

This module intentionally stops at the public HTTP boundary.  In particular,
the current launch response has no Playwright/CDP connection endpoint, so it
cannot manufacture the ``page`` required by Social Pod's browser gateways.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_CAMOUFOX_BASE_URL = "http://127.0.0.1:8000"
_API_PREFIX = "/api/v1"


class CamoufoxHttpError(RuntimeError):
    """A safe, contextual error returned by the remote public API."""


class CamoufoxHttpUnavailable(CamoufoxHttpError):
    """The local Camoufox Profile Manager service could not be reached."""


class CamoufoxPageAccessUnavailable(CamoufoxHttpError):
    """The public API has launched a browser but exposes no usable page."""


@dataclass(frozen=True, slots=True)
class CamoufoxServiceStatus:
    available: bool
    status: str
    api_version: str | None
    database: str | None
    profiles_count: int | None


@dataclass(frozen=True, slots=True)
class CamoufoxProfile:
    """The non-secret profile fields Social Pod needs for an external reference."""

    id: str
    name: str
    group: str | None
    status: str


@dataclass(frozen=True, slots=True)
class CamoufoxLaunch:
    """Public launch acknowledgement, deliberately not a browser-page handle."""

    profile_id: str
    status: str
    message: str
    process_id: int | None


class CamoufoxHttpClient:
    """Small async client for confirmed CPM HTTP routes.

    ``GET`` requests are retried because they are safe.  Mutating calls are
    deliberately sent once: a client-side timeout must not duplicate profile
    creation or browser launch.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_CAMOUFOX_BASE_URL,
        *,
        timeout: float = 10.0,
        retries: int = 1,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._retries = retries
        self._api_key = api_key
        self._transport = transport

    async def service_status(self) -> CamoufoxServiceStatus:
        payload = await self._request("GET", "/health")
        return CamoufoxServiceStatus(
            available=payload.get("status") == "healthy",
            status=str(payload.get("status", "unknown")),
            api_version=_optional_string(payload.get("api_version")),
            database=_optional_string(payload.get("database")),
            profiles_count=_optional_int(payload.get("profiles_count")),
        )

    async def list_profiles(self, *, page: int = 1, per_page: int = 100) -> list[CamoufoxProfile]:
        payload = await self._request(
            "GET", f"{_API_PREFIX}/profiles", params={"page": page, "per_page": per_page}
        )
        profiles = payload.get("profiles")
        if not isinstance(profiles, list):
            raise CamoufoxHttpError("Camoufox returned an invalid profile list")
        return [_profile_from_payload(profile) for profile in profiles]

    async def get_profile(self, profile_id: str) -> CamoufoxProfile:
        payload = await self._request("GET", f"{_API_PREFIX}/profiles/{profile_id}")
        return _profile_from_payload(payload)

    async def create_profile(
        self,
        *,
        name: str,
        group: str | None = None,
        notes: str | None = None,
    ) -> CamoufoxProfile:
        if not name.strip():
            raise ValueError("profile name cannot be empty")
        payload: dict[str, Any] = {"name": name.strip(), "generate_fingerprint": True}
        if group is not None:
            payload["group"] = group
        if notes is not None:
            payload["notes"] = notes
        response = await self._request("POST", f"{_API_PREFIX}/profiles", json=payload)
        return _profile_from_payload(response)

    async def launch_profile(
        self, profile_id: str, *, headless: bool = False, window_size: str | None = None
    ) -> CamoufoxLaunch:
        payload: dict[str, Any] = {"headless": headless}
        if window_size is not None:
            payload["window_size"] = window_size
        response = await self._request(
            "POST", f"{_API_PREFIX}/profiles/{profile_id}/launch", json=payload
        )
        return CamoufoxLaunch(
            profile_id=str(response.get("profile_id", profile_id)),
            status=str(response.get("status", "unknown")),
            message=str(response.get("message", "")),
            process_id=_optional_int(response.get("process_id")),
        )

    async def close_profile_browser(self, profile_id: str) -> str:
        payload = await self._request("POST", f"{_API_PREFIX}/profiles/{profile_id}/close")
        return str(payload.get("status", "unknown"))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts = self._retries + 1 if method == "GET" else 1
        headers = {"X-API-Key": self._api_key} if self._api_key else None
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout,
                    headers=headers,
                    transport=self._transport,
                ) as client:
                    response = await client.request(method, path, params=params, json=json)
            except httpx.TransportError as exc:
                if attempt + 1 < attempts:
                    continue
                raise CamoufoxHttpUnavailable(
                    "Camoufox Profile Manager is not available. Start the service and try again."
                ) from exc

            if response.is_error:
                detail = _error_detail(response)
                raise CamoufoxHttpError(
                    f"Camoufox Profile Manager returned HTTP {response.status_code}: {detail}"
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise CamoufoxHttpError("Camoufox returned a non-JSON API response") from exc
            if not isinstance(payload, dict):
                raise CamoufoxHttpError("Camoufox returned an invalid API response")
            return payload
        raise AssertionError("unreachable")


class CamoufoxHttpGateway:
    """Public HTTP operations that can be safely composed today.

    It intentionally does *not* implement ``UpstreamOnboardingGateway``,
    ``UpstreamHealthGateway``, or ``UpstreamExecutionGateway``.  Each requires
    a Playwright-compatible page, and the current public launch API does not
    expose a CDP endpoint, WebSocket URL, or remote page handle.
    """

    def __init__(self, client: CamoufoxHttpClient) -> None:
        self._client = client

    async def service_status(self) -> CamoufoxServiceStatus:
        return await self._client.service_status()

    async def list_profiles(self) -> list[CamoufoxProfile]:
        return await self._client.list_profiles()

    async def get_profile(self, profile_id: str) -> CamoufoxProfile:
        return await self._client.get_profile(profile_id)

    async def create_profile(self, *, name: str, group: str | None = None) -> CamoufoxProfile:
        return await self._client.create_profile(name=name, group=group)

    async def launch_profile(self, profile_id: str, *, headless: bool = False) -> CamoufoxLaunch:
        return await self._client.launch_profile(profile_id, headless=headless)

    async def close_profile_browser(self, profile_id: str) -> str:
        return await self._client.close_profile_browser(profile_id)

    def require_playwright_page(self) -> None:
        raise CamoufoxPageAccessUnavailable(
            "Camoufox launched the profile, but its public HTTP API does not expose a "
            "Playwright/CDP connection or page handle."
        )


def _profile_from_payload(payload: Any) -> CamoufoxProfile:
    if not isinstance(payload, dict) or not all(key in payload for key in ("id", "name", "status")):
        raise CamoufoxHttpError("Camoufox returned an invalid profile response")
    group = payload.get("group")
    return CamoufoxProfile(
        id=str(payload["id"]),
        name=str(payload["name"]),
        group=str(group) if group is not None else None,
        status=str(payload["status"]),
    )


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


def _optional_int(value: Any) -> int | None:
    return int(value) if isinstance(value, int) else None


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.reason_phrase
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, str):
            return detail
    return response.reason_phrase
