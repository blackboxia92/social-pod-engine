"""Sequential, failure-isolated runner for operator-guided session setup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from time import monotonic
from typing import Any
from uuid import UUID

from ..adapters.base import BaseSocialAdapter, ChallengeType, ExecutionContext
from ..domain import HealthStatus, LifecycleStatus, SessionStatus, SocialAccount, utc_now
from ..persistence import SocialPodDatabase
from ..registry import AdapterRegistry
from .contracts import InteractiveBrowser, UpstreamOnboardingGateway
from .models import (
    OnboardingBatchReport,
    OnboardingItemResult,
    OnboardingItemStatus,
    OnboardingQueueState,
    OnboardingQueueStatus,
    OnboardingSessionStep,
)
from .persistence import OnboardingQueueStore


class _QueuePaused(Exception):
    """Internal control flow used to release a live browser before resuming."""


@dataclass(frozen=True, slots=True)
class OnboardingConfig:
    session_timeout_seconds: float = 300.0
    poll_interval_seconds: float = 2.0
    successful_lifecycle_status: LifecycleStatus = LifecycleStatus.WARMUP

    def __post_init__(self) -> None:
        if self.session_timeout_seconds < 0:
            raise ValueError("session_timeout_seconds must be non-negative")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        if self.successful_lifecycle_status not in {LifecycleStatus.WARMUP, LifecycleStatus.ACTIVE}:
            raise ValueError("successful_lifecycle_status must be WARMUP or ACTIVE")


class OnboardingRunner:
    """Runs one account at a time while CPM remains behind an injected gateway."""

    def __init__(
        self,
        database: SocialPodDatabase,
        adapters: AdapterRegistry,
        upstream: UpstreamOnboardingGateway,
        *,
        config: OnboardingConfig | None = None,
    ) -> None:
        self.database = database
        self.adapters = adapters
        self.upstream = upstream
        self.config = config or OnboardingConfig()
        self.store = OnboardingQueueStore(database)
        self.store.initialize()
        self.state = self.store.latest_resumable()
        self.current_step: OnboardingSessionStep | None = None

    def load_queue(
        self,
        group_id: UUID | str | None = None,
        filter_tags: list[str] | None = None,
        *,
        include_non_valid_sessions: bool = False,
    ) -> int:
        accounts = self.store.find_candidates(
            group_id=group_id,
            filter_tags=filter_tags,
            include_non_valid_sessions=include_non_valid_sessions,
        )
        self.state = OnboardingQueueState(account_ids=[account.id for account in accounts])
        self.store.save(self.state)
        return self.state.total_accounts

    async def start(self) -> OnboardingBatchReport:
        state = self._require_state()
        if state.status is OnboardingQueueStatus.PAUSED:
            return self.report()
        if state.current_index >= state.total_accounts:
            state.status = OnboardingQueueStatus.COMPLETED
            self.store.save(state)
            return self.report()
        state.status = OnboardingQueueStatus.RUNNING
        self.store.save(state)
        while state.status is OnboardingQueueStatus.RUNNING and state.current_account_id is not None:
            await self.next()
        if state.current_account_id is None:
            state.status = OnboardingQueueStatus.COMPLETED
            self.store.save(state)
        return self.report()

    async def next(self, *, confirm_login: bool = False) -> OnboardingItemResult | None:
        state = self._require_state()
        if state.status is OnboardingQueueStatus.PAUSED:
            return None
        account_id = state.current_account_id
        if account_id is None:
            state.status = OnboardingQueueStatus.COMPLETED
            self.store.save(state)
            return None
        state.status = OnboardingQueueStatus.RUNNING
        account = self.database.get_social_account(account_id)
        result: OnboardingItemResult | None
        if account is None:
            result = OnboardingItemResult(
                account_id, "<missing>", OnboardingItemStatus.FAILED, None, "account no longer exists"
            )
        else:
            result = await self._process_account(account, confirm_login=confirm_login)
        if result is None:
            self.store.save(state)
            return None
        self._record_result(result, advance=True)
        return result

    def pause(self) -> OnboardingBatchReport:
        state = self._require_state()
        if state.status is not OnboardingQueueStatus.COMPLETED:
            state.status = OnboardingQueueStatus.PAUSED
            self.store.save(state)
        return self.report()

    def resume(self) -> OnboardingBatchReport:
        if self.state is None:
            self.state = self.store.latest_resumable()
        state = self._require_state()
        if state.current_account_id is None:
            state.status = OnboardingQueueStatus.COMPLETED
        else:
            state.status = OnboardingQueueStatus.READY
        self.store.save(state)
        return self.report()

    def skip(self, reason: str = "skipped by operator") -> OnboardingItemResult | None:
        state = self._require_state()
        account_id = state.current_account_id
        if account_id is None or state.status is OnboardingQueueStatus.PAUSED:
            return None
        account = self.database.get_social_account(account_id)
        result = OnboardingItemResult(
            account_id=account_id,
            username=account.username if account else "<missing>",
            status=OnboardingItemStatus.SKIPPED,
            upstream_profile_id=account.upstream_profile_id if account else None,
            reason=reason,
        )
        self._record_result(result, advance=True)
        return result

    def mark_challenge(self, reason: str = "challenge marked by operator") -> OnboardingItemResult | None:
        state = self._require_state()
        account_id = state.current_account_id
        if account_id is None or state.status is OnboardingQueueStatus.PAUSED:
            return None
        account = self.database.get_social_account(account_id)
        if account is None:
            result = OnboardingItemResult(account_id, "<missing>", OnboardingItemStatus.FAILED, None, reason)
        else:
            self._apply_challenge(account)
            result = OnboardingItemResult(
                account.id,
                account.username,
                OnboardingItemStatus.CHALLENGE,
                account.upstream_profile_id,
                reason,
            )
        self._record_result(result, advance=True)
        return result

    def retry(self, account_id: UUID | str | None = None) -> int:
        """Create a new persisted queue for one failed account or all failed accounts."""
        state = self._require_state()
        selected_id = UUID(str(account_id)) if account_id is not None else None
        retryable = {
            item.account_id
            for item in state.details
            if item.status in {OnboardingItemStatus.FAILED, OnboardingItemStatus.CHALLENGE}
            and (selected_id is None or item.account_id == selected_id)
        }
        self.state = OnboardingQueueState(account_ids=[account_id for account_id in state.account_ids if account_id in retryable])
        self.store.save(self.state)
        return self.state.total_accounts

    async def confirm_login(self) -> OnboardingItemResult | None:
        """Perform an immediate validation for the current operator-confirmed account."""
        return await self.next(confirm_login=True)

    def report(self) -> OnboardingBatchReport:
        return OnboardingBatchReport.from_queue(self._require_state())

    async def _process_account(
        self, account: SocialAccount, *, confirm_login: bool
    ) -> OnboardingItemResult | None:
        upstream_profile_id = account.upstream_profile_id
        lease: Any | None = None
        browser: InteractiveBrowser | None = None
        try:
            if upstream_profile_id is None:
                self.current_step = OnboardingSessionStep.PROFILE_PROVISIONING
                upstream_profile_id = await self.upstream.create_profile(account)
                if not upstream_profile_id:
                    raise ValueError("upstream gateway returned an empty profile identifier")
                account.upstream_profile_id = upstream_profile_id
                account.updated_at = utc_now()
                self.database.save_social_account(account)

            self.current_step = OnboardingSessionStep.LEASE_ACQUISITION
            lease = await self.upstream.acquire_lease(upstream_profile_id, proxy_id=account.proxy_id)
            self.current_step = OnboardingSessionStep.BROWSER_OPEN
            browser = await self.upstream.open_interactive_browser(lease)
            adapter = self._adapter_for(account)
            context = ExecutionContext(
                page=browser.page,
                social_account_id=str(account.id),
                upstream_profile_id=upstream_profile_id,
                metadata={"proxy_id": account.proxy_id} if account.proxy_id else {},
            )
            self.current_step = OnboardingSessionStep.AWAITING_LOGIN
            open_home = getattr(adapter, "open_home", None)
            if open_home is None:
                raise RuntimeError(f"{adapter.platform_name} adapter cannot open its login or home page")
            await open_home(browser.page)
            outcome, reason = await self._wait_for_session(adapter, context, confirm_login=confirm_login)
            self.current_step = OnboardingSessionStep.SESSION_VALIDATION
            if outcome is OnboardingItemStatus.SUCCESS:
                self._apply_success(account)
            elif outcome is OnboardingItemStatus.CHALLENGE:
                self._apply_challenge(account)
            else:
                self._apply_failure(account, reason)
            return OnboardingItemResult(account.id, account.username, outcome, upstream_profile_id, reason)
        except _QueuePaused:
            return None
        except Exception as exc:  # noqa: BLE001 - each account must not abort the batch
            self._apply_failure(account, str(exc))
            return OnboardingItemResult(
                account.id, account.username, OnboardingItemStatus.FAILED, upstream_profile_id, str(exc)
            )
        finally:
            self.current_step = OnboardingSessionStep.CLOSING
            if browser is not None:
                await self._close_quietly(browser)
            if lease is not None:
                await self._release_quietly(lease)
            self.current_step = None

    async def _wait_for_session(
        self, adapter: BaseSocialAdapter, context: ExecutionContext, *, confirm_login: bool
    ) -> tuple[OnboardingItemStatus, str | None]:
        timeout = 0.0 if confirm_login else self.config.session_timeout_seconds
        deadline = monotonic() + timeout
        last_status = SessionStatus.UNKNOWN
        while True:
            if self._require_state().status is OnboardingQueueStatus.PAUSED:
                raise _QueuePaused()
            challenge = await adapter.detect_challenge(context)
            if challenge is not ChallengeType.NONE:
                return OnboardingItemStatus.CHALLENGE, f"challenge detected: {challenge.value}"
            last_status = await adapter.validate_session(context)
            if last_status is SessionStatus.VALID:
                return OnboardingItemStatus.SUCCESS, None
            if monotonic() >= deadline:
                reason = "operator confirmation did not validate a session" if confirm_login else "login session timed out"
                if last_status in {SessionStatus.EXPIRED, SessionStatus.INVALID}:
                    reason = f"{reason}: {last_status.value}"
                return OnboardingItemStatus.FAILED, reason
            await asyncio.sleep(min(self.config.poll_interval_seconds, max(0.0, deadline - monotonic())))

    def _apply_success(self, account: SocialAccount) -> None:
        account.session_status = SessionStatus.VALID
        account.lifecycle_status = self.config.successful_lifecycle_status
        account.health_status = HealthStatus.HEALTHY
        account.updated_at = utc_now()
        self.database.save_social_account(account)

    def _apply_challenge(self, account: SocialAccount) -> None:
        account.session_status = SessionStatus.CHALLENGE_REQUIRED
        account.lifecycle_status = LifecycleStatus.PENDING_SETUP
        account.health_status = HealthStatus.ACTION_REQUIRED
        account.updated_at = utc_now()
        self.database.save_social_account(account)

    def _apply_failure(self, account: SocialAccount, reason: str | None) -> None:
        del reason
        account.session_status = SessionStatus.CHALLENGE_REQUIRED
        account.lifecycle_status = LifecycleStatus.PENDING_SETUP
        account.health_status = HealthStatus.ACTION_REQUIRED
        account.updated_at = utc_now()
        self.database.save_social_account(account)

    def _record_result(self, result: OnboardingItemResult, *, advance: bool) -> None:
        state = self._require_state()
        state.details.append(result)
        if result.status is OnboardingItemStatus.SUCCESS:
            state.completed_count += 1
        elif result.status is OnboardingItemStatus.SKIPPED:
            state.skipped_count += 1
        elif result.status is OnboardingItemStatus.FAILED:
            state.failed_count += 1
        if advance:
            state.current_index += 1
        if state.current_account_id is None:
            state.status = OnboardingQueueStatus.COMPLETED
        self.store.save(state)

    def _adapter_for(self, account: SocialAccount) -> BaseSocialAdapter:
        return self.adapters.get(account.platform.value)

    async def _close_quietly(self, browser: InteractiveBrowser) -> None:
        try:
            await self.upstream.close_interactive_browser(browser)
        except Exception:  # noqa: BLE001 - cleanup must not stop the next account
            pass

    async def _release_quietly(self, lease: Any) -> None:
        try:
            await self.upstream.release_lease(lease)
        except Exception:  # noqa: BLE001 - cleanup must not stop the next account
            pass

    def _require_state(self) -> OnboardingQueueState:
        if self.state is None:
            raise RuntimeError("load_queue() before controlling the onboarding runner")
        return self.state
