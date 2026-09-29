from __future__ import annotations

from datetime import timedelta

import pytest

from social_pod_engine.adapters.base import Capability
from social_pod_engine.adapters.x import XAdapter
from social_pod_engine.campaign.bridge import ExecutionBridge
from social_pod_engine.campaign.dispatcher import ExecutionDispatcher
from social_pod_engine.campaign.execution_persistence import (
    ExecutionTaskStore,
    InvalidTaskTransition,
)
from social_pod_engine.campaign.models import ApprovalStatus, TaskStatus
from social_pod_engine.campaign.planner import CampaignPlannerService
from social_pod_engine.domain import (
    AccountGroup,
    HealthStatus,
    LifecycleStatus,
    Persona,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
    utc_now,
)
from social_pod_engine.narrative import (
    Campaign,
    CampaignStatus,
    ContentBrief,
    Narrative,
    NarrativeCampaignStore,
    NarrativeRole,
)
from social_pod_engine.persistence import SocialPodDatabase
from social_pod_engine.registry import AdapterRegistry


@pytest.fixture
def database(tmp_path):
    value = SocialPodDatabase(tmp_path / "social-pod.sqlite3")
    value.initialize()
    yield value
    value.close()


def _seed_assignment(database: SocialPodDatabase, *, quarantined: bool = False):
    group = AccountGroup(alias="execution group")
    persona = Persona(alias="execution persona")
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.X,
        username="execution-account",
        group_id=group.id,
        health_status=HealthStatus.HEALTHY,
        session_status=SessionStatus.VALID,
        lifecycle_status=LifecycleStatus.WARMUP,
        quota_status=QuotaStatus.AVAILABLE,
        quarantined=False,
    )
    database.save_account_group(group)
    database.save_persona(persona)
    database.save_social_account(account)
    store = NarrativeCampaignStore(database)
    campaign = Campaign(name="Mercado de pases", status=CampaignStatus.ACTIVE)
    narrative = Narrative(
        campaign_id=campaign.id,
        title="Datos",
        angle="Datos verificables",
        key_messages=["Datos"],
    )
    brief = ContentBrief(
        narrative_id=narrative.id,
        role=NarrativeRole.STATS_AND_FACTS,
        platform=SocialPlatform.X,
        prompt_instructions="Usar hechos",
    )
    store.save_campaign(campaign)
    store.save_narrative(narrative)
    store.save_content_brief(brief)
    plan = CampaignPlannerService(database, store).assign_briefs_to_accounts(campaign.id, [group.id])
    if quarantined:
        account.quarantined = True
        database.save_social_account(account)
    return store, campaign, account, plan.assignments[0]


def _registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(XAdapter())
    return registry


def _bridge(database: SocialPodDatabase, store: NarrativeCampaignStore) -> tuple[ExecutionBridge, ExecutionTaskStore]:
    queue = ExecutionTaskStore(database)
    return ExecutionBridge(database, store, queue, _registry()), queue


def test_bridge_persists_structured_approval_gated_execution_task(database):
    store, campaign, account, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)

    task = bridge.create_task(campaign_id=campaign.id, assignment_id=assignment.id)

    assert task.assignment_id == assignment.id
    assert task.account_id == account.id
    assert task.payload == {
        "brief_id": str(assignment.brief_id),
        "narrative_id": str(assignment.narrative_id),
        "role": "stats_and_facts",
        "content_state": "pending_generation",
    }
    assert task.status is TaskStatus.PLANNED
    assert task.approval_status is ApprovalStatus.PENDING
    assert queue.get(task.id) == task
    assert queue.list_events(task.id)[0]["event_type"] == "CREATED"


def test_pending_approval_cannot_be_ready_and_x_write_capability_is_blocked(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    task = bridge.create_task(campaign_id=campaign.id, assignment_id=assignment.id)

    with pytest.raises(InvalidTaskTransition):
        queue.mark_ready(task.id)

    blocked = bridge.approve(task.id)
    assert blocked.status is TaskStatus.BLOCKED
    assert blocked.last_error == "CAPABILITY_NOT_SUPPORTED"
    assert [item["event_type"] for item in queue.list_events(task.id)] == [
        "CREATED",
        "APPROVED",
        "BLOCKED",
    ]


def test_quarantined_account_is_blocked_without_changing_control_plane_state(database):
    store, campaign, account, assignment = _seed_assignment(database, quarantined=True)
    bridge, _ = _bridge(database, store)

    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )

    assert task.status is TaskStatus.BLOCKED
    assert task.last_error == "ACCOUNT_QUARANTINED"
    restored = database.get_social_account(account.id)
    assert restored is not None
    assert restored.lifecycle_status is LifecycleStatus.WARMUP
    assert restored.quarantined is True


def test_queue_persists_across_store_recreation_and_is_idempotent(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    duplicate = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    reloaded = ExecutionTaskStore(database)

    assert task.status is TaskStatus.READY
    assert duplicate.id == task.id
    assert reloaded.get(task.id) is not None
    assert reloaded.dequeue_ready(limit=10) == [task]


def test_claim_is_atomic_and_expired_claim_is_recovered(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )

    first = queue.claim(task.id, worker_id="one", lease_seconds=1)
    second = queue.claim(task.id, worker_id="two")
    assert first is not None
    assert second is None
    assert first.attempt_count == 1

    queue.recover_expired_claims(now=utc_now() + timedelta(seconds=2))
    assert queue.get(task.id).status is TaskStatus.READY


def test_dispatcher_dry_run_completes_without_calling_adapter_execution(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )

    completed = ExecutionDispatcher(database, queue, _registry()).run_once("dry-worker")

    assert completed is not None
    assert completed.id == task.id
    assert completed.status is TaskStatus.COMPLETED
    assert completed.attempt_count == 1
    with pytest.raises(ValueError, match="dry-run"):
        ExecutionDispatcher(database, queue, _registry(), execution_enabled=True)


def test_failure_retries_only_until_max_attempts_and_report_summarizes_blocks(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    claimed = queue.claim(task.id, worker_id="worker")
    assert claimed is not None
    ready_again = queue.fail(task.id, "TEMPORARY_FAILURE")
    assert ready_again.status is TaskStatus.READY

    blocked_task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        approval_required=False,
    )
    report = queue.report()
    assert report.count(TaskStatus.READY) == 1
    assert report.count(TaskStatus.BLOCKED) == 1
    assert report.blocked_by_account_reason[(str(blocked_task.account_id), "CAPABILITY_NOT_SUPPORTED")] == 1
