from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from social_pod_engine.adapters.base import Capability, ExecutionContext, ExternalExecutionResult
from social_pod_engine.adapters.x import XAdapter
from social_pod_engine.campaign.dispatcher import ExecutionDispatcher
from social_pod_engine.campaign.execution_persistence import ExecutionTaskStore
from social_pod_engine.campaign.models import ApprovalStatus, ExecutionTask, TaskStatus
from social_pod_engine.content.models import ContentDraftStatus
from social_pod_engine.domain import (
    HealthStatus,
    LifecycleStatus,
    Persona,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
)
from social_pod_engine.execution.gateway import ExecutionGatewayError, ExecutionGatewayFailureKind
from social_pod_engine.persistence import SocialPodDatabase
from social_pod_engine.registry import AdapterRegistry


@pytest.fixture
def database(tmp_path):
    value = SocialPodDatabase(tmp_path / "targeted-execution.sqlite3")
    value.initialize()
    yield value
    value.close()


class RecordingXAdapter(XAdapter):
    def __init__(
        self,
        *,
        fail_after_attempt: bool = False,
        unconfirmed_after_attempt: bool = False,
        reconciliation_result: ExternalExecutionResult | None = None,
    ) -> None:
        self.executed_texts: list[str] = []
        self.fail_after_attempt = fail_after_attempt
        self.unconfirmed_after_attempt = unconfirmed_after_attempt
        self.reconciliation_result = reconciliation_result
        self.reconciled_texts: list[str] = []

    async def execute_capability(self, capability, payload, context):
        assert capability is Capability.POST
        self.executed_texts.append(str(payload["text"]))
        if self.fail_after_attempt:
            raise RuntimeError("confirmation timed out after the post attempt")
        if self.unconfirmed_after_attempt:
            return ExternalExecutionResult(success=True, confirmed=False)
        return ExternalExecutionResult(
            success=True,
            confirmed=True,
            external_id="confirmed-post",
            external_url="https://x.com/example/status/confirmed-post",
        )

    async def find_published_post(self, text: str, page: object):
        del page
        self.reconciled_texts.append(text)
        return self.reconciliation_result

    async def validate_session(self, context: ExecutionContext) -> SessionStatus:
        assert context.page is not None
        return SessionStatus.VALID


@dataclass
class FakeBrowser:
    page: object


class FakeExecutionGateway:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.opened: list[object] = []
        self.closed: list[object] = []
        self.released: list[object] = []
        self._leased_profiles: set[str] = set()
        self.active_profiles: set[str] = set()
        self.max_active_profiles = 0
        self.execution_started = asyncio.Event()
        self.allow_execution = asyncio.Event()
        self.block_execution_open = False
        self.open_failure: ExecutionGatewayError | None = None

    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> object:
        del proxy_id
        if upstream_profile_id in self._leased_profiles:
            raise ExecutionGatewayError(
                ExecutionGatewayFailureKind.PROFILE_BUSY, "profile is already leased"
            )
        self._leased_profiles.add(upstream_profile_id)
        self.acquired.append(upstream_profile_id)
        return {"profile": upstream_profile_id}

    async def release_lease(self, lease: object) -> None:
        self.released.append(lease)
        self._leased_profiles.discard(lease["profile"])

    async def open_execution_browser(self, lease: object) -> FakeBrowser:
        if self.open_failure is not None:
            raise self.open_failure
        profile_id = lease["profile"]
        assert profile_id in self._leased_profiles
        assert profile_id not in self.active_profiles
        self.active_profiles.add(profile_id)
        self.max_active_profiles = max(self.max_active_profiles, len(self.active_profiles))
        browser = FakeBrowser(page=SimpleNamespace(profile_id=profile_id))
        self.opened.append(lease)
        self.execution_started.set()
        if self.block_execution_open:
            await self.allow_execution.wait()
        return browser

    async def close_execution_browser(self, browser: FakeBrowser) -> None:
        self.closed.append(browser)
        self.active_profiles.discard(browser.page.profile_id)


class ApprovedDrafts:
    def __init__(self, texts: dict[UUID, str]) -> None:
        self.texts = texts

    def get(self, draft_id: UUID):
        text = self.texts.get(draft_id)
        if text is None:
            return None
        return SimpleNamespace(status=ContentDraftStatus.APPROVED, revision=1, text=text)


def _task(account: SocialAccount, *, text: str) -> tuple[ExecutionTask, UUID]:
    draft_id = uuid4()
    task = ExecutionTask(
        campaign_id=uuid4(),
        assignment_id=uuid4(),
        account_id=account.id,
        platform=SocialPlatform.X,
        capability=Capability.POST,
        payload={
            "content_draft_id": str(draft_id),
            "revision": 1,
            "content_type": "post",
            "text": text,
        },
        idempotency_key=f"targeted-{uuid4()}",
        status=TaskStatus.READY,
        approval_status=ApprovalStatus.APPROVED,
    )
    return task, draft_id


def _dispatcher(
    database: SocialPodDatabase,
    *,
    fail_after_attempt: bool = False,
    unconfirmed_after_attempt: bool = False,
    reconciliation_result: ExternalExecutionResult | None = None,
    max_concurrency: int = 1,
):
    persona = Persona(alias="targeted execution")
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.X,
        username="targeted-account",
        upstream_profile_id="cpm-targeted-profile",
        health_status=HealthStatus.HEALTHY,
        session_status=SessionStatus.VALID,
        lifecycle_status=LifecycleStatus.WARMUP,
        quota_status=QuotaStatus.AVAILABLE,
    )
    database.save_persona(persona)
    database.save_social_account(account)
    first, first_draft = _task(account, text="old SAFE task")
    second, second_draft = _task(account, text="confirmed preview task")
    queue = ExecutionTaskStore(database)
    queue.enqueue(first)
    queue.enqueue(second)
    adapter = RecordingXAdapter(
        fail_after_attempt=fail_after_attempt,
        unconfirmed_after_attempt=unconfirmed_after_attempt,
        reconciliation_result=reconciliation_result,
    )
    registry = AdapterRegistry()
    registry.register(adapter)
    gateway = FakeExecutionGateway()
    drafts = ApprovedDrafts({first_draft: "old SAFE task", second_draft: "confirmed preview task"})
    dispatcher = ExecutionDispatcher(
        database,
        queue,
        registry,
        execution_enabled=True,
        gateway=gateway,
        content_store=drafts,  # type: ignore[arg-type]
        max_concurrency=max_concurrency,
    )
    return dispatcher, queue, adapter, gateway, first, second


@pytest.mark.asyncio
async def test_targeted_execution_claims_and_executes_only_the_previewed_ready_task(database):
    dispatcher, queue, adapter, gateway, old_task, preview_task = _dispatcher(database)

    result = await dispatcher.run_task_async(preview_task.id, "operator")

    assert result.task is not None and result.task.id == preview_task.id
    assert result.task.status is TaskStatus.COMPLETED
    assert queue.get(old_task.id).status is TaskStatus.READY  # type: ignore[union-attr]
    assert adapter.executed_texts == ["confirmed preview task"]
    assert gateway.acquired == ["cpm-targeted-profile"]
    assert len(gateway.opened) == len(gateway.closed) == len(gateway.released) == 1
    assert "CLAIMED" not in [event["event_type"] for event in queue.list_events(old_task.id)]
    assert "CLAIMED" in [event["event_type"] for event in queue.list_events(preview_task.id)]


@pytest.mark.asyncio
async def test_targeted_execution_keeps_unknown_external_state_and_cleans_up(database):
    dispatcher, queue, adapter, gateway, old_task, preview_task = _dispatcher(
        database, fail_after_attempt=True
    )

    result = await dispatcher.run_task_async(preview_task.id, "operator")

    assert result.task is not None and result.task.status is TaskStatus.UNKNOWN_EXTERNAL_STATE
    assert queue.get(old_task.id).status is TaskStatus.READY  # type: ignore[union-attr]
    assert adapter.executed_texts == ["confirmed preview task"]
    assert len(gateway.opened) == len(gateway.closed) == len(gateway.released) == 1


@pytest.mark.asyncio
async def test_rpc_failure_before_adapter_execution_stays_retryable_and_keeps_safe_context(database):
    dispatcher, queue, adapter, gateway, _, preview_task = _dispatcher(database)
    gateway.open_failure = ExecutionGatewayError(
        ExecutionGatewayFailureKind.RPC_UNAVAILABLE,
        "Camoufox remote page control is unavailable",
        operation="launch_remote_profile",
        endpoint="/api/v1/profiles/cpm-targeted-profile/launch-remote",
        exception_type="ReadTimeout",
        safe_detail="timeout waiting for launch-remote",
    )

    result = await dispatcher.run_task_async(preview_task.id, "operator")
    current = queue.get(preview_task.id)

    assert result.outcome.value == "rpc_unavailable"
    assert current is not None and current.status is TaskStatus.READY
    assert current.last_error is not None and current.last_error.startswith("RPC_UNAVAILABLE")
    assert "operation=launch_remote_profile" in current.last_error
    assert adapter.executed_texts == []
    assert gateway.closed == []
    assert len(gateway.released) == 1
    events = queue.list_events(preview_task.id)
    rpc_event = next(event for event in events if event["event_type"] == "EXECUTION_RPC_FAILED")
    assert rpc_event["details"]["exception_type"] == "ReadTimeout"


@pytest.mark.asyncio
async def test_unconfirmed_post_attempt_becomes_unknown_without_a_retry(database):
    dispatcher, queue, adapter, gateway, old_task, preview_task = _dispatcher(
        database, unconfirmed_after_attempt=True
    )

    result = await dispatcher.run_task_async(preview_task.id, "operator")

    assert result.task is not None and result.task.status is TaskStatus.UNKNOWN_EXTERNAL_STATE
    assert queue.get(old_task.id).status is TaskStatus.READY  # type: ignore[union-attr]
    assert adapter.executed_texts == ["confirmed preview task"]
    assert len(gateway.opened) == len(gateway.closed) == len(gateway.released) == 1


@pytest.mark.asyncio
async def test_reconciliation_finds_the_original_post_without_executing_it_again(database):
    evidence = ExternalExecutionResult(
        success=True,
        confirmed=True,
        external_id="reconciled-post",
        external_url="https://x.com/targeted-account/status/reconciled-post",
    )
    dispatcher, queue, adapter, gateway, _, preview_task = _dispatcher(
        database, reconciliation_result=evidence
    )
    claimed = queue.claim(preview_task.id, worker_id="previous-worker")
    assert claimed is not None
    queue.mark_unknown_external(preview_task.id, "confirmation timed out")

    result = await dispatcher.reconcile_unknown_task_async(preview_task.id, "reconciler")

    assert result.task is not None and result.task.status is TaskStatus.COMPLETED
    assert result.task.external_id == "reconciled-post"
    assert adapter.executed_texts == []
    assert adapter.reconciled_texts == ["confirmed preview task"]
    assert len(gateway.opened) == len(gateway.closed) == len(gateway.released) == 1


@pytest.mark.asyncio
async def test_same_profile_reopens_headless_after_cleanup_and_keeps_session(database):
    dispatcher, queue, adapter, gateway, first_task, second_task = _dispatcher(database)

    first_result = await dispatcher.run_task_async(second_task.id, "worker-one")
    second_result = await dispatcher.run_task_async(first_task.id, "worker-two")

    assert first_result.task is not None and first_result.task.status is TaskStatus.COMPLETED
    assert second_result.task is not None and second_result.task.status is TaskStatus.COMPLETED
    assert gateway.acquired == ["cpm-targeted-profile", "cpm-targeted-profile"]
    assert len(gateway.opened) == len(gateway.closed) == len(gateway.released) == 2
    assert gateway.active_profiles == set()
    assert await adapter.validate_session(ExecutionContext(page=gateway.closed[-1].page)) is SessionStatus.VALID
    assert queue.get(first_task.id).status is TaskStatus.COMPLETED  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_two_tasks_for_one_profile_return_profile_busy_without_second_browser(database):
    dispatcher, queue, _, gateway, first_task, second_task = _dispatcher(
        database, max_concurrency=2
    )
    gateway.block_execution_open = True
    batch = asyncio.create_task(
        dispatcher.run_tasks_async([first_task.id, second_task.id], "parallel-worker")
    )
    await gateway.execution_started.wait()
    gateway.allow_execution.set()
    results = await batch

    assert {result.outcome.value for result in results} == {"would_execute", "profile_busy"}
    assert len(gateway.opened) == 1
    assert len(gateway.closed) == len(gateway.released) == 1
    busy_task = next(result.task for result in results if result.outcome.value == "profile_busy")
    assert busy_task is not None and busy_task.status is TaskStatus.READY
    assert queue.get(busy_task.id).last_error == "PROFILE_BUSY | operation=unknown | detail=profile is already leased"  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_two_profiles_can_run_with_concurrency_two(database):
    dispatcher, queue, _, gateway, first_task, _ = _dispatcher(database, max_concurrency=2)
    persona = Persona(alias="second execution persona")
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.X,
        username="second-account",
        upstream_profile_id="cpm-second-profile",
        health_status=HealthStatus.HEALTHY,
        session_status=SessionStatus.VALID,
        lifecycle_status=LifecycleStatus.WARMUP,
        quota_status=QuotaStatus.AVAILABLE,
    )
    database.save_persona(persona)
    database.save_social_account(account)
    second_task, second_draft = _task(account, text="second profile task")
    queue.enqueue(second_task)
    dispatcher.content_store.texts[second_draft] = "second profile task"  # type: ignore[union-attr]
    gateway.block_execution_open = True
    batch = asyncio.create_task(
        dispatcher.run_tasks_async([first_task.id, second_task.id], "parallel-worker")
    )
    await gateway.execution_started.wait()
    while len(gateway.opened) < 2:
        await asyncio.sleep(0)
    gateway.allow_execution.set()
    results = await batch

    assert all(result.task is not None and result.task.status is TaskStatus.COMPLETED for result in results)
    assert gateway.max_active_profiles == 2
    assert {lease["profile"] for lease in gateway.released} == {
        "cpm-targeted-profile",
        "cpm-second-profile",
    }
