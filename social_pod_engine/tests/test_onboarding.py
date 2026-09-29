from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from social_pod_engine.adapters.base import (
    BaseSocialAdapter,
    Capability,
    ChallengeType,
    ExecutionContext,
    ExecutionResult,
    HealthSignal,
)
from social_pod_engine.domain import (
    HealthStatus,
    LifecycleStatus,
    Persona,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
)
from social_pod_engine.onboarding import (
    InteractiveBrowser,
    OnboardingConfig,
    OnboardingItemStatus,
    OnboardingQueueStatus,
    OnboardingRunner,
)
from social_pod_engine.persistence import SocialPodDatabase
from social_pod_engine.registry import AdapterRegistry


class StubAdapter(BaseSocialAdapter):
    platform_id = "x"

    def __init__(
        self,
        outcomes: dict[str, list[SessionStatus]] | None = None,
        challenges: set[str] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.challenges = challenges or set()
        self.opened_accounts: list[str] = []

    @property
    def platform_name(self) -> str:
        return self.platform_id

    async def open_home(self, page: Any) -> None:
        self.opened_accounts.append(str(page["account_id"]))

    async def validate_session(self, context: ExecutionContext) -> SessionStatus:
        assert context.social_account_id is not None
        values = self.outcomes.get(context.social_account_id, [SessionStatus.VALID])
        return values.pop(0) if len(values) > 1 else values[0]

    async def check_health(self, context: ExecutionContext) -> HealthSignal:
        return HealthSignal(HealthStatus.HEALTHY, await self.validate_session(context))

    async def detect_challenge(self, context: ExecutionContext) -> ChallengeType:
        return (
            ChallengeType.AUTHENTICATION
            if context.social_account_id in self.challenges
            else ChallengeType.NONE
        )

    def get_supported_capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.SESSION_HEALTH})

    async def execute_capability(
        self, capability: Capability, payload: Mapping[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        return ExecutionResult(capability, True)


class StubUpstreamGateway:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.leases: list[tuple[str, str | None]] = []
        self.closed: list[str] = []
        self.released: list[str] = []

    async def create_profile(self, account: SocialAccount) -> str:
        self.created.append(str(account.id))
        return f"upstream-{account.id}"

    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> str:
        self.leases.append((upstream_profile_id, proxy_id))
        return upstream_profile_id

    async def open_interactive_browser(self, lease: str) -> InteractiveBrowser:
        return InteractiveBrowser(page={"account_id": lease.removeprefix("upstream-")}, handle=lease)

    async def close_interactive_browser(self, browser: InteractiveBrowser) -> None:
        self.closed.append(str(browser.handle))

    async def release_lease(self, lease: str) -> None:
        self.released.append(lease)


@pytest.fixture
def database(tmp_path):
    database = SocialPodDatabase(tmp_path / "social-pod.sqlite3")
    database.initialize()
    yield database
    database.close()


def add_account(
    database: SocialPodDatabase,
    username: str,
    *,
    group_id: str | None = None,
    tags: list[str] | None = None,
    lifecycle_status: LifecycleStatus = LifecycleStatus.PENDING_SETUP,
    upstream_profile_id: str | None = None,
    proxy_id: str | None = None,
) -> SocialAccount:
    persona = Persona(alias=f"persona-{username}")
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.X,
        username=username,
        group_id=group_id,
        tags=tags or [],
        lifecycle_status=lifecycle_status,
        upstream_profile_id=upstream_profile_id,
        proxy_id=proxy_id,
    )
    database.save_persona(persona)
    database.save_social_account(account)
    return account


def make_runner(
    database: SocialPodDatabase,
    adapter: StubAdapter | None = None,
    gateway: StubUpstreamGateway | None = None,
    **config: Any,
) -> tuple[OnboardingRunner, StubAdapter, StubUpstreamGateway]:
    registry = AdapterRegistry()
    adapter = adapter or StubAdapter()
    gateway = gateway or StubUpstreamGateway()
    registry.register(adapter)
    return (
        OnboardingRunner(database, registry, gateway, config=OnboardingConfig(**config)),
        adapter,
        gateway,
    )


def test_load_queue_uses_isolated_db_and_group_tag_filters(database):
    included = add_account(database, "included", group_id="launch", tags=["LATAM", "News"])
    add_account(database, "wrong-group", group_id="other", tags=["latam", "news"])
    add_account(database, "wrong-tag", group_id="launch", tags=["latam"])
    add_account(database, "already-warm", group_id="launch", tags=["latam", "news"], lifecycle_status=LifecycleStatus.WARMUP)

    runner, _, _ = make_runner(database)

    assert runner.load_queue(group_id="launch", filter_tags=[" news ", "LATAM"]) == 1
    assert runner.state is not None
    assert runner.state.account_ids == [included.id]
    assert runner.state.total_accounts == 1


@pytest.mark.asyncio
async def test_next_skip_pause_and_resume_advance_a_queue_sequentially(database):
    first = add_account(database, "first")
    second = add_account(database, "second")
    runner, _, _ = make_runner(database)
    runner.load_queue()

    assert runner.pause().total_processed == 0
    assert await runner.next() is None
    assert runner.resume().total_processed == 0
    skipped = runner.skip()
    assert skipped is not None and skipped.account_id == first.id
    assert skipped.status is OnboardingItemStatus.SKIPPED
    completed = await runner.next()

    assert completed is not None and completed.account_id == second.id
    assert completed.status is OnboardingItemStatus.SUCCESS
    assert runner.state is not None and runner.state.status is OnboardingQueueStatus.COMPLETED
    assert runner.report().skipped == 1


@pytest.mark.asyncio
async def test_onboarding_creates_and_links_an_upstream_profile_then_updates_states(database):
    account = add_account(database, "new-profile", proxy_id="proxy-arg-01")
    runner, adapter, gateway = make_runner(database)
    runner.load_queue()

    result = await runner.next()
    stored = database.get_social_account(account.id)

    assert result is not None and result.status is OnboardingItemStatus.SUCCESS
    assert stored is not None
    assert stored.upstream_profile_id == f"upstream-{account.id}"
    assert stored.session_status is SessionStatus.VALID
    assert stored.lifecycle_status is LifecycleStatus.WARMUP
    assert stored.health_status is HealthStatus.HEALTHY
    assert gateway.created == [str(account.id)]
    assert gateway.leases == [(stored.upstream_profile_id, "proxy-arg-01")]
    assert gateway.closed == [stored.upstream_profile_id]
    assert gateway.released == [stored.upstream_profile_id]
    assert adapter.opened_accounts == [str(account.id)]


@pytest.mark.asyncio
async def test_timeout_or_failure_does_not_stop_later_accounts(database):
    timeout = add_account(database, "timeout")
    valid = add_account(database, "valid")
    adapter = StubAdapter(
        outcomes={str(timeout.id): [SessionStatus.UNKNOWN], str(valid.id): [SessionStatus.VALID]}
    )
    runner, _, _ = make_runner(database, adapter, session_timeout_seconds=0)
    runner.load_queue()

    report = await runner.start()
    timed_out = database.get_social_account(timeout.id)
    validated = database.get_social_account(valid.id)

    assert [item.status for item in report.details] == [OnboardingItemStatus.FAILED, OnboardingItemStatus.SUCCESS]
    assert report.failed == 1
    assert report.successful_logins == 1
    assert timed_out is not None and timed_out.session_status is SessionStatus.CHALLENGE_REQUIRED
    assert timed_out.health_status is HealthStatus.ACTION_REQUIRED
    assert validated is not None and validated.session_status is SessionStatus.VALID


@pytest.mark.asyncio
async def test_challenge_and_retry_create_a_new_queue_for_failed_work(database):
    challenged = add_account(database, "challenge")
    adapter = StubAdapter(challenges={str(challenged.id)})
    runner, _, _ = make_runner(database, adapter)
    runner.load_queue()

    result = await runner.next()
    assert result is not None and result.status is OnboardingItemStatus.CHALLENGE
    assert runner.report().challenges_detected == 1
    assert runner.retry(challenged.id) == 1
    assert runner.state is not None and runner.state.current_index == 0


@pytest.mark.asyncio
async def test_new_runner_resumes_persisted_queue_from_last_saved_index(database):
    first = add_account(database, "first-resume")
    second = add_account(database, "second-resume")
    runner, _, gateway = make_runner(database)
    runner.load_queue()
    assert (await runner.next()).account_id == first.id  # type: ignore[union-attr]

    resumed, _, resumed_gateway = make_runner(database, gateway=gateway)
    assert resumed.state is not None
    assert resumed.state.current_index == 1
    assert resumed.resume().total_processed == 1
    report = await resumed.start()

    assert [item.account_id for item in report.details] == [first.id, second.id]
    assert report.successful_logins == 2
    assert resumed_gateway.leases[-1][0] == f"upstream-{second.id}"
