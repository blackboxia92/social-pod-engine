from __future__ import annotations

import asyncio
import random
import sqlite3
from collections.abc import Mapping
from datetime import timedelta
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
    HealthIssueType,
    HealthStatus,
    LifecycleStatus,
    Persona,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
    utc_now,
)
from social_pod_engine.health import (
    HealthEngine,
    HealthEngineConfig,
    HealthScheduler,
    HealthSchedulerConfig,
    OperationalCheckStatus,
    ProxyHealthResult,
)
from social_pod_engine.health.gateway import HeadlessBrowser
from social_pod_engine.health.models import HealthCheckEvent, SuggestedAction
from social_pod_engine.health.persistence import HealthRepository
from social_pod_engine.persistence import SocialPodDatabase
from social_pod_engine.registry import AdapterRegistry


class StubAdapter(BaseSocialAdapter):
    platform_id = "x"

    def __init__(
        self,
        *,
        sessions: dict[str, SessionStatus] | None = None,
        health: dict[str, HealthStatus] | None = None,
        challenges: set[str] | None = None,
        delay_seconds: float = 0,
    ) -> None:
        self.sessions = sessions or {}
        self.health = health or {}
        self.challenges = challenges or set()
        self.delay_seconds = delay_seconds

    @property
    def platform_name(self) -> str:
        return self.platform_id

    async def validate_session(self, context: ExecutionContext) -> SessionStatus:
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return self.sessions.get(context.social_account_id or "", SessionStatus.VALID)

    async def check_health(self, context: ExecutionContext) -> HealthSignal:
        session = self.sessions.get(context.social_account_id or "", SessionStatus.VALID)
        health = self.health.get(context.social_account_id or "", HealthStatus.HEALTHY)
        return HealthSignal(health, session, ("structured adapter check",))

    async def detect_challenge(self, context: ExecutionContext) -> ChallengeType:
        return ChallengeType.AUTHENTICATION if context.social_account_id in self.challenges else ChallengeType.NONE

    def get_supported_capabilities(self) -> frozenset[Capability]:
        return frozenset({Capability.SESSION_HEALTH})

    async def execute_capability(
        self, capability: Capability, payload: Mapping[str, Any], context: ExecutionContext
    ) -> ExecutionResult:
        return ExecutionResult(capability, True)


class StubHealthGateway:
    def __init__(
        self,
        *,
        proxy_results: list[ProxyHealthResult] | None = None,
        fail_open_for: set[str] | None = None,
    ) -> None:
        self.proxy_results = proxy_results or [ProxyHealthResult(available=True, latency_ms=12)]
        self.fail_open_for = fail_open_for or set()
        self.acquired: list[str] = []
        self.released: list[str] = []
        self.closed: list[str] = []
        self.active = 0
        self.max_active = 0

    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> str:
        del proxy_id
        self.acquired.append(upstream_profile_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        return upstream_profile_id

    async def release_lease(self, lease: str) -> None:
        self.released.append(lease)
        self.active -= 1

    async def open_headless_browser(self, lease: str) -> HeadlessBrowser:
        if lease in self.fail_open_for:
            raise RuntimeError("headless browser unavailable")
        return HeadlessBrowser(page={"lease": lease}, handle=lease)

    async def close_headless_browser(self, browser: HeadlessBrowser) -> None:
        self.closed.append(str(browser.handle))

    async def check_proxy(self, proxy_id: str | None) -> ProxyHealthResult:
        del proxy_id
        if len(self.proxy_results) > 1:
            return self.proxy_results.pop(0)
        return self.proxy_results[0]


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
    lifecycle: LifecycleStatus = LifecycleStatus.WARMUP,
    upstream_profile_id: str | None = "profile",
    next_healthcheck_at=None,
) -> SocialAccount:
    persona = Persona(alias=f"persona-{username}")
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.X,
        username=username,
        lifecycle_status=lifecycle,
        upstream_profile_id=upstream_profile_id,
        next_healthcheck_at=next_healthcheck_at,
    )
    database.save_persona(persona)
    database.save_social_account(account)
    return account


def make_engine(
    database: SocialPodDatabase,
    adapter: StubAdapter | None = None,
    gateway: StubHealthGateway | None = None,
    **config: Any,
) -> tuple[HealthEngine, StubAdapter, StubHealthGateway]:
    registry = AdapterRegistry()
    adapter = adapter or StubAdapter()
    gateway = gateway or StubHealthGateway()
    registry.register(adapter)
    return HealthEngine(database, registry, gateway, config=HealthEngineConfig(**config)), adapter, gateway


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_status", "challenge", "expected_health", "expected_issue"),
    [
        (SessionStatus.VALID, False, HealthStatus.HEALTHY, None),
        (SessionStatus.EXPIRED, False, HealthStatus.ACTION_REQUIRED, HealthIssueType.SESSION_EXPIRED),
        (SessionStatus.CHALLENGE_REQUIRED, False, HealthStatus.ACTION_REQUIRED, HealthIssueType.CHALLENGE_REQUIRED),
        (SessionStatus.INVALID, False, HealthStatus.ACTION_REQUIRED, HealthIssueType.SESSION_INVALID),
        (SessionStatus.VALID, True, HealthStatus.ACTION_REQUIRED, HealthIssueType.CHALLENGE_REQUIRED),
    ],
)
async def test_session_states_are_classified_structurally(
    database, session_status, challenge, expected_health, expected_issue
):
    account = add_account(database, f"session-{session_status.value}")
    adapter = StubAdapter(
        sessions={str(account.id): session_status}, challenges={str(account.id)} if challenge else set()
    )
    engine, _, gateway = make_engine(database, adapter)

    result = await engine.check_account_health(account.id)
    stored = database.get_social_account(account.id)

    assert result.operational_status is OperationalCheckStatus.CHECKED
    assert result.health_status is expected_health
    assert result.issue_type is expected_issue
    assert stored is not None and stored.lifecycle_status is LifecycleStatus.WARMUP
    assert stored.quarantined is (expected_health is HealthStatus.ACTION_REQUIRED)
    assert gateway.acquired == ["profile"]
    assert gateway.closed == ["profile"]
    assert gateway.released == ["profile"]


@pytest.mark.asyncio
async def test_health_and_proxy_signals_cover_degraded_and_unavailable(database):
    degraded = add_account(database, "degraded")
    unavailable = add_account(database, "unavailable")
    adapter = StubAdapter(health={str(degraded.id): HealthStatus.DEGRADED})
    gateway = StubHealthGateway(
        proxy_results=[
            ProxyHealthResult(available=True, degraded=True, latency_ms=350),
            ProxyHealthResult(available=False, issue=HealthIssueType.PROXY_UNAVAILABLE),
        ]
    )
    engine, _, _ = make_engine(database, adapter, gateway)

    degraded_result = await engine.check_account_health(degraded.id)
    unavailable_result = await engine.check_account_health(unavailable.id)
    unavailable_stored = database.get_social_account(unavailable.id)

    assert degraded_result.health_status is HealthStatus.DEGRADED
    assert degraded_result.issue_type is HealthIssueType.PROXY_DEGRADED
    assert unavailable_result.operational_status is OperationalCheckStatus.FAILED_INFRASTRUCTURE
    assert unavailable_result.health_status is HealthStatus.UNAVAILABLE
    assert unavailable_stored is not None and unavailable_stored.quarantined


@pytest.mark.asyncio
async def test_infrastructure_recovers_after_configured_healthy_streak_but_session_does_not(database):
    infrastructure = add_account(database, "infrastructure")
    session = add_account(database, "session")
    adapter = StubAdapter(sessions={str(session.id): SessionStatus.EXPIRED})
    gateway = StubHealthGateway(
        proxy_results=[
            ProxyHealthResult(available=False),
            ProxyHealthResult(available=True),
            ProxyHealthResult(available=True),
            ProxyHealthResult(available=True),
            ProxyHealthResult(available=True),
            ProxyHealthResult(available=True),
        ]
    )
    engine, _, _ = make_engine(database, adapter, gateway, healthy_checks_required_for_recovery=2)

    await engine.check_account_health(infrastructure.id)
    await engine.check_account_health(infrastructure.id)
    assert database.get_social_account(infrastructure.id).quarantined
    await engine.check_account_health(infrastructure.id)
    assert not database.get_social_account(infrastructure.id).quarantined

    await engine.check_account_health(session.id)
    adapter.sessions[str(session.id)] = SessionStatus.VALID
    await engine.check_account_health(session.id)
    await engine.check_account_health(session.id)
    stored_session = database.get_social_account(session.id)
    assert stored_session is not None and stored_session.quarantined
    assert stored_session.quarantine_issue_type is HealthIssueType.SESSION_EXPIRED


@pytest.mark.asyncio
async def test_ineligible_accounts_are_skipped_without_becoming_unavailable(database):
    pending = add_account(database, "pending", lifecycle=LifecycleStatus.PENDING_SETUP, upstream_profile_id=None)
    archived = add_account(database, "archived", lifecycle=LifecycleStatus.ARCHIVED)
    disabled = add_account(database, "disabled", lifecycle=LifecycleStatus.DISABLED)
    engine, _, gateway = make_engine(database)

    results = [await engine.check_account_health(account.id) for account in (pending, archived, disabled)]

    assert [result.operational_status for result in results] == [
        OperationalCheckStatus.SKIPPED_NOT_ONBOARDED,
        OperationalCheckStatus.SKIPPED_INELIGIBLE,
        OperationalCheckStatus.SKIPPED_INELIGIBLE,
    ]
    assert all(result.health_status is None for result in results)
    assert gateway.acquired == []


@pytest.mark.asyncio
async def test_cleanup_and_batch_progress_survive_individual_failures(database):
    broken = add_account(database, "broken", upstream_profile_id="broken-profile")
    add_account(database, "healthy", upstream_profile_id="healthy-profile")
    gateway = StubHealthGateway(fail_open_for={"broken-profile"})
    engine, _, _ = make_engine(database, gateway=gateway)

    report = await engine.run_health_check_batch()

    assert [result.operational_status for result in report.results] == [
        OperationalCheckStatus.FAILED_INFRASTRUCTURE,
        OperationalCheckStatus.CHECKED,
    ]
    assert report.failed_count == 1
    assert report.healthy_count == 1
    assert gateway.released == ["broken-profile", "healthy-profile"]
    assert gateway.closed == ["healthy-profile"]
    assert database.get_social_account(broken.id).quarantined


@pytest.mark.asyncio
async def test_batch_enforces_configured_concurrency(database):
    for index in range(5):
        add_account(database, f"concurrent-{index}", upstream_profile_id=f"profile-{index}")
    engine, _, gateway = make_engine(
        database, StubAdapter(delay_seconds=0.02), max_concurrency=2
    )

    report = await engine.run_health_check_batch(limit=5)

    assert report.total_checked == 5
    assert gateway.max_active == 2
    assert len(gateway.acquired) == len(gateway.released) == 5


@pytest.mark.asyncio
async def test_scheduler_selects_only_due_accounts_and_persists_seeded_jitter_after_restart(database):
    now = utc_now()
    due = add_account(database, "due", next_healthcheck_at=now - timedelta(seconds=1))
    future = add_account(database, "future", next_healthcheck_at=now + timedelta(hours=1))
    engine, _, gateway = make_engine(database)
    scheduler = HealthScheduler(
        database,
        engine,
        config=HealthSchedulerConfig(interval_seconds=100, jitter_seconds=10),
        rng=random.Random(7),
        now_provider=lambda: now,
    )

    report = await scheduler.run_due_healthchecks(limit=10)
    scheduled_due = database.get_social_account(due.id)
    untouched_future = database.get_social_account(future.id)

    assert [result.account_id for result in report.results] == [due.id]
    assert scheduled_due.next_healthcheck_at is not None and scheduled_due.next_healthcheck_at > now
    assert untouched_future.next_healthcheck_at == future.next_healthcheck_at
    restarted = HealthScheduler(database, engine, now_provider=lambda: now)
    assert (await restarted.run_due_healthchecks(limit=10)).results == []
    assert gateway.released == ["profile"]


@pytest.mark.asyncio
async def test_events_reports_and_secret_safety_are_structured(database):
    account = add_account(database, "event", upstream_profile_id="event-profile")
    adapter = StubAdapter(sessions={str(account.id): SessionStatus.EXPIRED})
    engine, _, _ = make_engine(database, adapter)

    report = await engine.run_health_check_batch()
    events = HealthRepository(database).list_events(account.id)

    assert len(events) == 1
    assert events[0].issue_type is HealthIssueType.SESSION_EXPIRED
    assert len(report.exceptions) == 1
    assert report.exceptions[0].suggested_action is SuggestedAction.REAUTHENTICATE
    with pytest.raises(ValueError, match="must not contain secrets"):
        HealthCheckEvent(
            account_id=account.id,
            operational_status=OperationalCheckStatus.CHECKED,
            health_status=HealthStatus.HEALTHY,
            session_status=SessionStatus.VALID,
            issue_type=None,
            latency_ms=None,
            details={"nested": {"cookies": "forbidden"}},
        )


def test_health_initialization_does_not_touch_an_upstream_database(tmp_path):
    upstream = tmp_path / "upstream.sqlite3"
    with sqlite3.connect(upstream) as connection:
        connection.execute("CREATE TABLE upstream_profiles (id TEXT PRIMARY KEY)")
    database = SocialPodDatabase(tmp_path / "social-pod.sqlite3")
    database.initialize()
    HealthRepository(database).initialize()
    database.close()

    with sqlite3.connect(upstream) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"upstream_profiles"}
