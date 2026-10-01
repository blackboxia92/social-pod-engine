"""HTTP-only boundary for Camoufox Profile Manager's public API.

This module intentionally stops at the public HTTP boundary.  In particular,
the current launch response has no Playwright/CDP connection endpoint, so it
cannot manufacture the ``page`` required by Social Pod's browser gateways.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import httpx

DEFAULT_CAMOUFOX_BASE_URL = "http://127.0.0.1:8000"
_API_PREFIX = "/api/v1"


class CamoufoxHttpError(RuntimeError):
    """A safe, contextual error returned by the remote public API."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "http_request",
        endpoint: str | None = None,
        status_code: int | None = None,
        exception_type: str | None = None,
        safe_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.endpoint = endpoint
        self.status_code = status_code
        self.exception_type = exception_type
        self.safe_detail = safe_detail or message


class CamoufoxHttpUnavailable(CamoufoxHttpError):
    """The local Camoufox Profile Manager service could not be reached."""


class CamoufoxHttpNotFound(CamoufoxHttpError):
    """A public Camoufox API resource was confirmed not to exist."""


class CamoufoxHttpConflict(CamoufoxHttpError):
    """The public API rejected an operation because its resource is busy."""


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


@dataclass(frozen=True, slots=True)
class CamoufoxRemoteLaunch(CamoufoxLaunch):
    handle: str
    endpoint: str
    url: str


@dataclass(frozen=True, slots=True)
class CamoufoxLease:
    profile_id: str
    proxy_id: str | None


class RemoteLocator:
    def __init__(self, page: RemotePage, selector: str) -> None:
        self._page, self._selector = page, selector

    async def count(self) -> int:
        return int((await self._page._operate("count", selector=self._selector)).get("count", 0))

    async def fill(self, text: str) -> None:
        await self._page._operate("fill", selector=self._selector, value=text)

    async def click(self) -> None:
        await self._page._operate("click", selector=self._selector)

    async def get_attribute(self, name: str) -> str | None:
        value = (await self._page._operate("get_attribute", selector=self._selector, value=name)).get("value")
        return str(value) if value is not None else None

    async def is_visible(self) -> bool:
        return bool((await self._page._operate("is_visible", selector=self._selector)).get("value"))

    async def is_enabled(self) -> bool:
        return bool((await self._page._operate("is_enabled", selector=self._selector)).get("value"))

    async def text_content(self) -> str | None:
        value = (await self._page._operate("text_content", selector=self._selector)).get("value")
        return str(value) if value is not None else None


@dataclass(frozen=True, slots=True)
class RemoteLocatorSnapshot:
    """Visible text and link read from one bounded, remote DOM element."""

    text: str
    href: str


class RemotePage:
    """Small Playwright-shaped proxy backed by CPM's profile-bound page RPC."""

    def __init__(self, client: CamoufoxHttpClient, remote: CamoufoxRemoteLaunch) -> None:
        self._client, self._remote = client, remote
        self.url = remote.url

    def locator(self, selector: str) -> RemoteLocator:
        return RemoteLocator(self, selector)

    async def goto(self, url: str) -> None:
        await self._operate("goto", value=url)

    async def locator_snapshots(
        self, selector: str, *, text_selector: str, href_selector: str
    ) -> list[RemoteLocatorSnapshot]:
        result = await self._operate_remote(
            "locator_snapshots",
            selector=selector,
            text_selector=text_selector,
            href_selector=href_selector,
        )
        items = result.get("items")
        if not isinstance(items, list):
            raise CamoufoxHttpError(
                "Camoufox returned invalid locator snapshots",
                operation="remote_page_operation:locator_snapshots",
                endpoint=self._remote.endpoint,
            )
        snapshots: list[RemoteLocatorSnapshot] = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(item.get("href"), str):
                raise CamoufoxHttpError(
                    "Camoufox returned invalid locator snapshot",
                    operation="remote_page_operation:locator_snapshots",
                    endpoint=self._remote.endpoint,
                )
            snapshots.append(RemoteLocatorSnapshot(item["text"], item["href"]))
        url = result.get("url")
        if isinstance(url, str):
            self.url = url
        return snapshots

    async def element_snapshots(self, selector: str) -> list[dict[str, str | None]]:
        """Return a bounded allow-list of element metadata for diagnostics.

        This is intentionally not a general DOM/evaluate bridge: the service
        chooses the fixed attributes and truncates visible text server-side.
        """
        result = await self._operate_remote("element_snapshots", selector=selector)
        items = result.get("items")
        if not isinstance(items, list):
            raise CamoufoxHttpError(
                "Camoufox returned invalid element snapshots",
                operation="remote_page_operation:element_snapshots",
                endpoint=self._remote.endpoint,
            )
        fields = {
            "tag",
            "role",
            "data_testid",
            "aria_label",
            "contenteditable",
            "placeholder",
            "text",
        }
        snapshots: list[dict[str, str | None]] = []
        for item in items:
            if not isinstance(item, dict):
                raise CamoufoxHttpError(
                    "Camoufox returned invalid element snapshot",
                    operation="remote_page_operation:element_snapshots",
                    endpoint=self._remote.endpoint,
                )
            snapshots.append(
                {field: str(item[field]) if item.get(field) is not None else None for field in fields}
            )
        url = result.get("url")
        if isinstance(url, str):
            self.url = url
        return snapshots

    async def _operate(
        self, operation: str, *, selector: str | None = None, value: str | None = None
    ) -> dict[str, Any]:
        result = await self._operate_remote(operation, selector=selector, value=value)
        url = result.get("url")
        if isinstance(url, str):
            self.url = url
        return result

    async def _operate_remote(
        self,
        operation: str,
        *,
        selector: str | None = None,
        value: str | None = None,
        text_selector: str | None = None,
        href_selector: str | None = None,
    ) -> dict[str, Any]:
        try:
            return await self._client.remote_page_operation(
                self._remote.profile_id,
                self._remote.handle,
                operation,
                selector=selector,
                value=value,
                text_selector=text_selector,
                href_selector=href_selector,
            )
        except CamoufoxHttpError as exc:
            from ..execution.gateway import ExecutionGatewayError, ExecutionGatewayFailureKind

            raise ExecutionGatewayError(
                ExecutionGatewayFailureKind.RPC_UNAVAILABLE,
                "Camoufox remote page operation failed",
                operation=exc.operation,
                endpoint=exc.endpoint or self._remote.endpoint,
                status_code=exc.status_code,
                exception_type=exc.exception_type or type(exc).__name__,
                safe_detail=exc.safe_detail,
            ) from exc


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
        launch_timeout: float = 30.0,
        retries: int = 1,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if timeout <= 0 or launch_timeout <= 0:
            raise ValueError("timeouts must be positive")
        if retries < 0:
            raise ValueError("retries cannot be negative")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._launch_timeout = launch_timeout
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

    async def launch_remote_profile(
        self, profile_id: str, *, headless: bool = False, window_size: str | None = None
    ) -> CamoufoxRemoteLaunch:
        payload: dict[str, Any] = {"headless": headless}
        if window_size is not None:
            payload["window_size"] = window_size
        response = await self._request(
            "POST",
            f"{_API_PREFIX}/profiles/{profile_id}/launch-remote",
            json=payload,
            operation="launch_remote_profile",
            timeout=self._launch_timeout,
        )
        remote = response.get("remote_control")
        if not isinstance(remote, dict) or not all(key in remote for key in ("handle", "endpoint")):
            raise CamoufoxPageAccessUnavailable(
                "Camoufox did not return a usable remote page handle",
                operation="launch_remote_profile",
                endpoint=f"{_API_PREFIX}/profiles/{profile_id}/launch-remote",
            )
        return CamoufoxRemoteLaunch(
            profile_id=str(response.get("profile_id", profile_id)),
            status=str(response.get("status", "unknown")),
            message=str(response.get("message", "")),
            process_id=_optional_int(response.get("process_id")),
            handle=str(remote["handle"]),
            endpoint=str(remote["endpoint"]),
            url=str(remote.get("url", "")),
        )

    async def remote_page_operation(
        self,
        profile_id: str,
        handle: str,
        operation: str,
        *,
        selector: str | None = None,
        value: str | None = None,
        text_selector: str | None = None,
        href_selector: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, str | None] = {
            "operation": operation,
            "selector": selector,
            "value": value,
        }
        if text_selector is not None:
            payload["text_selector"] = text_selector
        if href_selector is not None:
            payload["href_selector"] = href_selector
        response = await self._request(
            "POST",
            f"{_API_PREFIX}/profiles/{profile_id}/remote/page",
            json=payload,
            headers={"X-Remote-Control-Handle": handle},
            operation=f"remote_page_operation:{operation}",
        )
        result = response.get("result")
        if not isinstance(result, dict):
            raise CamoufoxHttpError(
                "Camoufox returned an invalid remote page response",
                operation=f"remote_page_operation:{operation}",
                endpoint=f"{_API_PREFIX}/profiles/{profile_id}/remote/page",
            )
        return result

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
        headers: dict[str, str] | None = None,
        operation: str = "http_request",
        timeout: float | None = None,
    ) -> dict[str, Any]:
        attempts = self._retries + 1 if method == "GET" else 1
        request_headers = {"X-API-Key": self._api_key} if self._api_key else {}
        if headers:
            request_headers.update(headers)
        for attempt in range(attempts):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=self._timeout if timeout is None else timeout,
                    headers=request_headers or None,
                    transport=self._transport,
                ) as client:
                    response = await client.request(method, path, params=params, json=json)
            except httpx.TimeoutException as exc:
                if attempt + 1 < attempts:
                    continue
                raise CamoufoxHttpUnavailable(
                    "Camoufox Profile Manager request timed out",
                    operation=operation,
                    endpoint=path,
                    exception_type=type(exc).__name__,
                    safe_detail=f"timeout waiting for {path}",
                ) from exc
            except httpx.ConnectError as exc:
                if attempt + 1 < attempts:
                    continue
                raise CamoufoxHttpUnavailable(
                    "Camoufox Profile Manager is not available. Start the service and try again.",
                    operation=operation,
                    endpoint=path,
                    exception_type=type(exc).__name__,
                    safe_detail=f"connection refused or unavailable at {_safe_host(self._base_url)}",
                ) from exc
            except httpx.TransportError as exc:
                if attempt + 1 < attempts:
                    continue
                raise CamoufoxHttpUnavailable(
                    "Camoufox Profile Manager is not available. Start the service and try again.",
                    operation=operation,
                    endpoint=path,
                    exception_type=type(exc).__name__,
                    safe_detail=f"{type(exc).__name__} while requesting {path}",
                ) from exc

            if response.is_error:
                detail = _error_detail(response)
                if response.status_code == 409:
                    raise CamoufoxHttpConflict(
                        f"Camoufox Profile Manager returned HTTP 409: {detail}",
                        operation=operation,
                        endpoint=path,
                        status_code=response.status_code,
                        safe_detail=f"Camoufox returned HTTP 409: {_safe_detail(detail)}",
                    )
                if response.status_code == 404:
                    raise CamoufoxHttpNotFound(
                        f"Camoufox Profile Manager returned HTTP 404: {detail}",
                        operation=operation,
                        endpoint=path,
                        status_code=response.status_code,
                        safe_detail=f"Camoufox returned HTTP 404: {_safe_detail(detail)}",
                    )
                raise CamoufoxHttpError(
                    f"Camoufox Profile Manager returned HTTP {response.status_code}: {detail}",
                    operation=operation,
                    endpoint=path,
                    status_code=response.status_code,
                    safe_detail=f"Camoufox returned HTTP {response.status_code}: {_safe_detail(detail)}",
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise CamoufoxHttpError(
                    "Camoufox returned a non-JSON API response", operation=operation, endpoint=path
                ) from exc
            if not isinstance(payload, dict):
                raise CamoufoxHttpError(
                    "Camoufox returned an invalid API response", operation=operation, endpoint=path
                )
            return payload
        raise AssertionError("unreachable")


class CamoufoxHttpGateway:
    """HTTP implementation of the existing browser gateway protocols.

    CPM remains the sole owner of browser process, persistent context, and
    lease. Social Pod receives only a temporary, profile-bound page proxy.
    """

    def __init__(
        self,
        client: CamoufoxHttpClient,
        *,
        remote_page_ready_attempts: int = 3,
        remote_page_ready_delay: float = 0.2,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if remote_page_ready_attempts < 1:
            raise ValueError("remote_page_ready_attempts must be at least one")
        if remote_page_ready_delay < 0:
            raise ValueError("remote_page_ready_delay must be non-negative")
        self._client = client
        self._leased_profiles: set[str] = set()
        self._lease_lock = Lock()
        self._remote_page_ready_attempts = remote_page_ready_attempts
        self._remote_page_ready_delay = remote_page_ready_delay
        self._sleep = sleep

    async def service_status(self) -> CamoufoxServiceStatus:
        return await self._client.service_status()

    async def list_profiles(self) -> list[CamoufoxProfile]:
        return await self._client.list_profiles()

    async def get_profile(self, profile_id: str) -> CamoufoxProfile:
        from ..onboarding.contracts import UpstreamProfileNotFound

        try:
            return await self._client.get_profile(profile_id)
        except CamoufoxHttpNotFound as exc:
            raise UpstreamProfileNotFound(
                f"Camoufox profile {profile_id!r} does not exist in the current service instance"
            ) from exc

    async def launch_profile(self, profile_id: str, *, headless: bool = False) -> CamoufoxLaunch:
        return await self._client.launch_profile(profile_id, headless=headless)

    async def close_profile_browser(self, profile_id: str) -> str:
        return await self._client.close_profile_browser(profile_id)

    async def create_profile(self, account: Any) -> str:
        profile = await self._client.create_profile(name=account.username, group=str(account.group_id) if account.group_id else None)
        return profile.id

    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> CamoufoxLease:
        from ..execution.gateway import ExecutionGatewayError, ExecutionGatewayFailureKind

        with self._lease_lock:
            if upstream_profile_id in self._leased_profiles:
                raise ExecutionGatewayError(
                    ExecutionGatewayFailureKind.PROFILE_BUSY,
                    f"Camoufox profile {upstream_profile_id!r} is already in use by this Social Pod runtime",
                )
            self._leased_profiles.add(upstream_profile_id)
        # ``launch-remote`` takes CPM's persistent, cross-process lease atomically.
        return CamoufoxLease(upstream_profile_id, proxy_id)

    async def release_lease(self, lease: CamoufoxLease) -> None:
        # Browser close releases CPM's lease; this removes only Social Pod's local guard.
        with self._lease_lock:
            self._leased_profiles.discard(lease.profile_id)

    async def open_interactive_browser(self, lease: CamoufoxLease):
        from ..onboarding.contracts import InteractiveBrowser

        remote = await self._client.launch_remote_profile(lease.profile_id, headless=False)
        return InteractiveBrowser(page=RemotePage(self._client, remote), handle=remote)

    async def close_interactive_browser(self, browser: Any) -> None:
        await self._client.close_profile_browser(browser.handle.profile_id)

    async def open_headless_browser(self, lease: CamoufoxLease):
        from ..health.gateway import HeadlessBrowser

        remote = await self._client.launch_remote_profile(lease.profile_id, headless=True)
        return HeadlessBrowser(page=RemotePage(self._client, remote), handle=remote)

    async def close_headless_browser(self, browser: Any) -> None:
        await self._client.close_profile_browser(browser.handle.profile_id)

    async def open_execution_browser(self, lease: CamoufoxLease):
        from ..execution.gateway import ExecutionGatewayError, ExecutionGatewayFailureKind

        try:
            remote = await self._client.launch_remote_profile(lease.profile_id, headless=True)
        except CamoufoxHttpConflict as exc:
            raise ExecutionGatewayError(
                ExecutionGatewayFailureKind.PROFILE_BUSY,
                "Camoufox profile is busy; retry after its current browser session closes",
                operation=exc.operation,
                endpoint=exc.endpoint,
                status_code=exc.status_code,
                exception_type=type(exc).__name__,
                safe_detail=exc.safe_detail,
            ) from exc
        except (CamoufoxHttpUnavailable, CamoufoxPageAccessUnavailable) as exc:
            raise ExecutionGatewayError(
                ExecutionGatewayFailureKind.RPC_UNAVAILABLE,
                "Camoufox remote page control is unavailable",
                operation=exc.operation,
                endpoint=exc.endpoint,
                status_code=exc.status_code,
                exception_type=exc.exception_type or type(exc).__name__,
                safe_detail=exc.safe_detail,
            ) from exc
        except CamoufoxHttpError as exc:
            raise ExecutionGatewayError(
                ExecutionGatewayFailureKind.BROWSER_LAUNCH_FAILED,
                "Camoufox could not launch the execution browser",
                operation=exc.operation,
                endpoint=exc.endpoint,
                status_code=exc.status_code,
                exception_type=exc.exception_type or type(exc).__name__,
                safe_detail=exc.safe_detail,
            ) from exc
        browser = _RemoteBrowserHandle(RemotePage(self._client, remote), remote)
        try:
            await self._wait_for_remote_page(browser.page)
        except Exception:
            # A successful launch can still race a just-created page RPC.  Never
            # leave CPM's persistent profile locked if the bounded read-only
            # readiness probe does not become available.
            try:
                await self.close_execution_browser(browser)
            except CamoufoxHttpError:
                pass
            raise
        return browser

    async def close_execution_browser(self, browser: Any) -> None:
        await self._client.close_profile_browser(browser.remote.profile_id)

    async def _wait_for_remote_page(self, page: RemotePage) -> None:
        from ..execution.gateway import ExecutionGatewayError, ExecutionGatewayFailureKind

        last_error: ExecutionGatewayError | None = None
        for attempt in range(self._remote_page_ready_attempts):
            try:
                await page._operate("url")
                return
            except ExecutionGatewayError as exc:
                last_error = exc
                if attempt + 1 < self._remote_page_ready_attempts:
                    await self._sleep(self._remote_page_ready_delay)
        assert last_error is not None
        raise ExecutionGatewayError(
            ExecutionGatewayFailureKind.RPC_UNAVAILABLE,
            "Camoufox remote page did not become ready",
            operation="remote_page_readiness",
            endpoint=last_error.endpoint,
            status_code=last_error.status_code,
            exception_type=last_error.exception_type,
            safe_detail=last_error.safe_detail,
        ) from last_error

    async def check_proxy(self, proxy_id: str | None):
        from ..health.gateway import ProxyHealthResult

        if proxy_id is None:
            return ProxyHealthResult(available=True)
        raise CamoufoxHttpError("CPM public API cannot check a proxy by Social Pod proxy_id")


@dataclass(frozen=True, slots=True)
class _RemoteBrowserHandle:
    page: RemotePage
    remote: CamoufoxRemoteLaunch


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


def _safe_host(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.hostname:
        return f"{parsed.hostname}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}"
    return "configured Camoufox endpoint"


def _safe_detail(value: str) -> str:
    """Bound server text before it can reach a task, event, or terminal."""
    lowered = value.lower()
    if any(token in lowered for token in ("password", "token", "cookie", "authorization", "credential")):
        return "Camoufox returned a sensitive error detail; inspect its local service logs."
    return value.replace("\n", " ").replace("\r", " ")[:300]
