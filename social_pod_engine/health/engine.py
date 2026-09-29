"""Concurrent-safe fleet health checks over caller-supplied upstream resources."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import UUID

from ..adapters.base import BaseSocialAdapter, ChallengeType, ExecutionContext
from ..domain import (
    HealthIssueType,
    HealthStatus,
    LifecycleStatus,
    SessionStatus,
    SocialAccount,
    utc_now,
)
from ..persistence import SocialPodDatabase
from ..registry import AdapterRegistry
from .gateway import HeadlessBrowser, ProxyHealthResult, UpstreamHealthGateway
from .models import HealthBatchReport, HealthCheckResult, OperationalCheckStatus
from .persistence import HealthRepository

_SESSION_ISSUES = {
    HealthIssueType.SESSION_EXPIRED,
    HealthIssueType.CHALLENGE_REQUIRED,
    HealthIssueType.SESSION_INVALID,
}


@dataclass(frozen=True, slots=True)
class HealthEngineConfig:
    max_concurrency: int = 3
    healthy_checks_required_for_recovery: int = 2

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        if self.healthy_checks_required_for_recovery < 1:
            raise ValueError("healthy_checks_required_for_recovery must be at least one")


class HealthEngine:
    """Checks isolated accounts while every upstream interaction stays behind a gateway."""

    def __init__(
        self,
        database: SocialPodDatabase,
        adapters: AdapterRegistry,
        upstream: UpstreamHealthGateway,
        *,
        config: HealthEngineConfig | None = None,
        repository: HealthRepository | None = None,
    ) -> None:
        self.database = database
        self.adapters = adapters
        self.upstream = upstream
        self.config = config or HealthEngineConfig()
        self.repository = repository or HealthRepository(database)
        self.repository.initialize()
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)

    async def check_account_health(self, account_id: UUID) -> HealthCheckResult:
        account = self.database.get_social_account(account_id)
        if account is None:
            raise KeyError(f"SocialAccount not found: {account_id}")
        return await self._check_account(account)

    async def run_health_check_batch(
        self, group_id: UUID | str | None = None, limit: int = 50
    ) -> HealthBatchReport:
        if limit < 1:
            return HealthBatchReport.from_results([])
        accounts = self.repository.list_candidates(group_id)[:limit]
        return await self.run_accounts([account.id for account in accounts])

    async def run_accounts(self, account_ids: list[UUID]) -> HealthBatchReport:
        async def run_one(account_id: UUID) -> HealthCheckResult:
            async with self._semaphore:
                return await self.check_account_health(account_id)

        results = await asyncio.gather(*(run_one(account_id) for account_id in account_ids))
        return HealthBatchReport.from_results(list(results))

    async def _check_account(self, account: SocialAccount) -> HealthCheckResult:
        eligibility = self._eligibility_result(account)
        if eligibility is not None:
            return self._persist_result(account, eligibility)

        started = monotonic()
        try:
            proxy = await self.upstream.check_proxy(account.proxy_id)
            if not proxy.available:
                result = HealthCheckResult(
                    account_id=account.id,
                    platform=account.platform,
                    username=account.username,
                    operational_status=OperationalCheckStatus.FAILED_INFRASTRUCTURE,
                    health_status=HealthStatus.UNAVAILABLE,
                    session_status=account.session_status,
                    issue_type=proxy.issue or HealthIssueType.PROXY_UNAVAILABLE,
                    latency_ms=proxy.latency_ms,
                    details={"proxy_available": False},
                )
                return self._persist_result(account, result)
            result = await self._deep_check(account, proxy, started)
        except Exception as exc:  # noqa: BLE001 - a bad account must not stop a fleet batch
            result = HealthCheckResult(
                account_id=account.id,
                platform=account.platform,
                username=account.username,
                operational_status=OperationalCheckStatus.FAILED_INFRASTRUCTURE,
                health_status=HealthStatus.UNAVAILABLE,
                session_status=account.session_status,
                issue_type=HealthIssueType.PLATFORM_UNAVAILABLE,
                latency_ms=_elapsed_ms(started),
                details={"error_type": type(exc).__name__},
            )
        return self._persist_result(account, result)

    def _eligibility_result(self, account: SocialAccount) -> HealthCheckResult | None:
        if account.lifecycle_status in {LifecycleStatus.ARCHIVED, LifecycleStatus.DISABLED}:
            return HealthCheckResult(
                account.id,
                account.platform,
                account.username,
                OperationalCheckStatus.SKIPPED_INELIGIBLE,
                None,
                account.session_status,
                None,
                None,
                {"lifecycle_status": account.lifecycle_status.value},
            )
        if account.lifecycle_status is LifecycleStatus.PENDING_SETUP and account.upstream_profile_id is None:
            return HealthCheckResult(
                account.id,
                account.platform,
                account.username,
                OperationalCheckStatus.SKIPPED_NOT_ONBOARDED,
                None,
                account.session_status,
                None,
                None,
                {"reason": "upstream_profile_id is absent"},
            )
        if account.upstream_profile_id is None:
            return HealthCheckResult(
                account.id,
                account.platform,
                account.username,
                OperationalCheckStatus.SKIPPED_NOT_ONBOARDED,
                None,
                account.session_status,
                None,
                None,
                {"reason": "upstream_profile_id is absent"},
            )
        return None

    async def _deep_check(
        self, account: SocialAccount, proxy: ProxyHealthResult, started: float
    ) -> HealthCheckResult:
        lease: Any | None = None
        browser: HeadlessBrowser | None = None
        try:
            lease = await self.upstream.acquire_lease(account.upstream_profile_id or "", proxy_id=account.proxy_id)
            browser = await self.upstream.open_headless_browser(lease)
            adapter = self._adapter_for(account)
            context = ExecutionContext(
                page=browser.page,
                social_account_id=str(account.id),
                upstream_profile_id=account.upstream_profile_id,
                metadata={"proxy_id": account.proxy_id} if account.proxy_id else {},
            )
            session_status = await adapter.validate_session(context)
            signal = await adapter.check_health(context)
            challenge = await adapter.detect_challenge(context)
            health_status, issue_type = _classify_health(session_status, signal.health_status, challenge, proxy)
            return HealthCheckResult(
                account_id=account.id,
                platform=account.platform,
                username=account.username,
                operational_status=OperationalCheckStatus.CHECKED,
                health_status=health_status,
                session_status=session_status,
                issue_type=issue_type,
                latency_ms=proxy.latency_ms if proxy.latency_ms is not None else _elapsed_ms(started),
                details={
                    "proxy_available": proxy.available,
                    "proxy_degraded": proxy.degraded,
                    "adapter_reasons": list(signal.reasons),
                    "challenge": challenge.value,
                },
            )
        finally:
            if browser is not None:
                await self._close_quietly(browser)
            if lease is not None:
                await self._release_quietly(lease)

    def _persist_result(self, account: SocialAccount, result: HealthCheckResult) -> HealthCheckResult:
        checked_at = utc_now()
        result = HealthCheckResult(
            account_id=result.account_id,
            platform=result.platform,
            username=result.username,
            operational_status=result.operational_status,
            health_status=result.health_status,
            session_status=result.session_status,
            issue_type=result.issue_type,
            latency_ms=result.latency_ms,
            details=result.details,
            checked_at=checked_at,
        )
        if result.health_status is not None:
            account.health_status = result.health_status
            if result.session_status is not None:
                account.session_status = result.session_status
            self._apply_quarantine_policy(account, result)
        account.last_healthcheck_at = checked_at
        account.updated_at = checked_at
        self.database.save_social_account(account)
        self.repository.save_event(result.to_event())
        return result

    def _apply_quarantine_policy(self, account: SocialAccount, result: HealthCheckResult) -> None:
        if result.health_status in {HealthStatus.ACTION_REQUIRED, HealthStatus.UNAVAILABLE}:
            account.quarantined = True
            account.quarantine_reason = result.issue_type.value if result.issue_type else "unknown"
            account.quarantine_issue_type = result.issue_type or HealthIssueType.UNKNOWN
            account.quarantined_at = utc_now()
            account.healthy_check_streak = 0
            return
        if result.health_status is not HealthStatus.HEALTHY:
            account.healthy_check_streak = 0
            return
        if not account.quarantined:
            return
        if account.quarantine_issue_type in _SESSION_ISSUES:
            account.healthy_check_streak = 0
            return
        account.healthy_check_streak += 1
        if account.healthy_check_streak >= self.config.healthy_checks_required_for_recovery:
            account.quarantined = False
            account.quarantine_reason = None
            account.quarantine_issue_type = None
            account.quarantined_at = None
            account.healthy_check_streak = 0

    def _adapter_for(self, account: SocialAccount) -> BaseSocialAdapter:
        return self.adapters.get(account.platform.value)

    async def _close_quietly(self, browser: HeadlessBrowser) -> None:
        try:
            await self.upstream.close_headless_browser(browser)
        except Exception:  # noqa: BLE001 - release must still happen after cleanup failure
            pass

    async def _release_quietly(self, lease: Any) -> None:
        try:
            await self.upstream.release_lease(lease)
        except Exception:  # noqa: BLE001 - never abort a batch for cleanup alone
            pass


def _classify_health(
    session_status: SessionStatus,
    adapter_health: HealthStatus,
    challenge: ChallengeType,
    proxy: ProxyHealthResult,
) -> tuple[HealthStatus, HealthIssueType | None]:
    if challenge is not ChallengeType.NONE or session_status is SessionStatus.CHALLENGE_REQUIRED:
        return HealthStatus.ACTION_REQUIRED, HealthIssueType.CHALLENGE_REQUIRED
    if session_status is SessionStatus.EXPIRED:
        return HealthStatus.ACTION_REQUIRED, HealthIssueType.SESSION_EXPIRED
    if session_status is SessionStatus.INVALID:
        return HealthStatus.ACTION_REQUIRED, HealthIssueType.SESSION_INVALID
    if adapter_health is HealthStatus.UNAVAILABLE:
        return HealthStatus.UNAVAILABLE, HealthIssueType.PLATFORM_UNAVAILABLE
    if adapter_health is HealthStatus.ACTION_REQUIRED:
        return HealthStatus.ACTION_REQUIRED, HealthIssueType.UNKNOWN
    if proxy.degraded:
        return HealthStatus.DEGRADED, HealthIssueType.PROXY_DEGRADED
    return adapter_health, None


def _elapsed_ms(started: float) -> float:
    return round((monotonic() - started) * 1000, 3)
