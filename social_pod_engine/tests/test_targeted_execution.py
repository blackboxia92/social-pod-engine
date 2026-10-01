from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from social_pod_engine.adapters.base import Capability, ExternalExecutionResult
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
from social_pod_engine.persistence import SocialPodDatabase
from social_pod_engine.registry import AdapterRegistry


@pytest.fixture
def database(tmp_path):
    value = SocialPodDatabase(tmp_path / "targeted-execution.sqlite3")
    value.initialize()
    yield value
    value.close()


class RecordingXAdapter(XAdapter):
    def __init__(self, *, fail_after_attempt: bool = False) -> None:
        self.executed_texts: list[str] = []
        self.fail_after_attempt = fail_after_attempt

    async def execute_capability(self, capability, payload, context):
        assert capability is Capability.POST
        self.executed_texts.append(str(payload["text"]))
        if self.fail_after_attempt:
            raise RuntimeError("confirmation timed out after the post attempt")
        return ExternalExecutionResult(
            success=True,
            confirmed=True,
            external_id="confirmed-post",
            external_url="https://x.com/example/status/confirmed-post",
        )


@dataclass
class FakeBrowser:
    page: object


class FakeExecutionGateway:
    def __init__(self) -> None:
        self.acquired: list[str] = []
        self.opened: list[object] = []
        self.closed: list[object] = []
        self.released: list[object] = []

    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> object:
        del proxy_id
        self.acquired.append(upstream_profile_id)
        return {"profile": upstream_profile_id}

    async def release_lease(self, lease: object) -> None:
        self.released.append(lease)

    async def open_browser(self, lease: object) -> FakeBrowser:
        browser = FakeBrowser(page=object())
        self.opened.append(lease)
        return browser

    async def close_browser(self, browser: FakeBrowser) -> None:
        self.closed.append(browser)


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


def _dispatcher(database: SocialPodDatabase, *, fail_after_attempt: bool = False):
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
    adapter = RecordingXAdapter(fail_after_attempt=fail_after_attempt)
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
