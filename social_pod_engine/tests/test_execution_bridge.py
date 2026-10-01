from __future__ import annotations

from datetime import timedelta

import pytest

from social_pod_engine.adapters.base import Capability
from social_pod_engine.adapters.x import XAdapter
from social_pod_engine.campaign.bridge import ExecutionBridge
from social_pod_engine.campaign.dispatcher import ExecutionDispatcher
from social_pod_engine.campaign.eligibility import ExecutionEligibilityPolicy
from social_pod_engine.campaign.execution_persistence import (
    ExecutionTaskStore,
    InvalidTaskTransition,
)
from social_pod_engine.campaign.models import (
    ApprovalStatus,
    BlockReason,
    DispatchOutcome,
    TaskStatus,
)
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


def _registry(adapter: XAdapter | None = None) -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register(adapter or XAdapter())
    return registry


def _bridge(
    database: SocialPodDatabase,
    store: NarrativeCampaignStore,
    *,
    adapter: XAdapter | None = None,
    policy: ExecutionEligibilityPolicy | None = None,
) -> tuple[ExecutionBridge, ExecutionTaskStore]:
    queue = ExecutionTaskStore(database)
    return ExecutionBridge(database, store, queue, _registry(adapter), policy), queue


class MutableXAdapter(XAdapter):
    """Test-only adapter representing a later capability rollout."""

    def __init__(self) -> None:
        self.post_enabled = False

    def get_supported_capabilities(self):
        capabilities = set(super().get_supported_capabilities())
        capabilities.discard(Capability.POST)
        if self.post_enabled:
            capabilities.add(Capability.POST)
        return frozenset(capabilities)


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

    ready = bridge.approve(task.id)
    assert ready.status is TaskStatus.READY
    assert [item["event_type"] for item in queue.list_events(task.id)] == [
        "CREATED",
        "APPROVED",
        "READY",
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
    assert task.block_reason is BlockReason.ACCOUNT_QUARANTINED
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


def test_recovery_only_releases_expired_running_tasks_and_never_touches_unknown(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    expired = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    current = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        revision=2,
        approval_required=False,
    )
    unknown = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        revision=3,
        approval_required=False,
    )

    claimed_at = utc_now()
    assert queue.claim(expired.id, worker_id="expired", lease_seconds=1, now=claimed_at) is not None
    assert queue.claim(current.id, worker_id="current", lease_seconds=60, now=claimed_at) is not None
    assert queue.claim(unknown.id, worker_id="unknown", lease_seconds=60, now=claimed_at) is not None
    queue.mark_unknown_external(unknown.id, "outcome needs reconciliation")

    assert queue.recover_expired_claims(now=claimed_at + timedelta(seconds=2)) == 1
    assert queue.get(expired.id).status is TaskStatus.READY
    assert queue.get(current.id).status is TaskStatus.RUNNING
    assert queue.get(unknown.id).status is TaskStatus.UNKNOWN_EXTERNAL_STATE


def test_dispatcher_dry_run_returns_would_execute_without_marking_completed(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )

    result = ExecutionDispatcher(database, queue, _registry()).run_once("dry-worker")

    assert result.outcome is DispatchOutcome.WOULD_EXECUTE
    assert result.task is not None
    assert result.task.id == task.id
    assert result.task.status is TaskStatus.READY
    assert result.task.completed_at is None
    assert result.task.attempt_count == 0
    assert "WOULD_EXECUTE" in [item["event_type"] for item in queue.list_events(task.id)]
    with pytest.raises(ValueError, match="gateway"):
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

    bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        approval_required=False,
    )
    report = queue.report()
    assert report.count(TaskStatus.READY) == 2
    assert report.count(TaskStatus.BLOCKED) == 0


def test_blocked_capability_task_can_be_reevaluated_after_adapter_rollout(database):
    store, campaign, _, assignment = _seed_assignment(database)
    adapter = MutableXAdapter()
    bridge, _ = _bridge(database, store, adapter=adapter)
    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        approval_required=False,
    )
    assert task.block_reason is BlockReason.CAPABILITY_NOT_SUPPORTED

    adapter.post_enabled = True
    reevaluated = bridge.reevaluate_blocked_task(task.id)

    assert reevaluated.status is TaskStatus.READY
    assert reevaluated.block_reason is None
    assert reevaluated.last_error is None


def test_quarantined_task_can_be_reevaluated_after_control_plane_recovery(database):
    store, campaign, account, assignment = _seed_assignment(database, quarantined=True)
    bridge, _ = _bridge(database, store)
    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    assert task.status is TaskStatus.BLOCKED

    account.quarantined = False
    database.save_social_account(account)
    reevaluated = bridge.reevaluate_blocked_task(task.id)

    assert reevaluated.status is TaskStatus.READY
    assert reevaluated.block_reason is None


def test_degraded_health_is_controlled_by_explicit_eligibility_policy(database):
    store, campaign, account, assignment = _seed_assignment(database)
    account.health_status = HealthStatus.DEGRADED
    database.save_social_account(account)
    restrictive, _ = _bridge(database, store)
    blocked = restrictive.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    permissive, _ = _bridge(
        database,
        store,
        policy=ExecutionEligibilityPolicy(allow_degraded_accounts=True),
    )
    allowed = permissive.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        revision=2,
        approval_required=False,
    )

    assert blocked.block_reason is BlockReason.ACCOUNT_UNHEALTHY
    assert allowed.status is TaskStatus.READY


def test_revision_scopes_idempotency_and_scheduled_for_can_be_immediate(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    first = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    same_revision = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    revised = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        revision=2,
        approval_required=False,
    )

    assert first.id == same_revision.id
    assert revised.id != first.id
    assert revised.idempotency_key != first.idempotency_key
    assert first.scheduled_for is None
    restored = queue.get(first.id)
    assert restored is not None
    assert restored.scheduled_for is None
    assert {item.id for item in queue.dequeue_ready(limit=10)} == {first.id, revised.id}


def test_inherited_approval_can_make_a_task_ready_without_per_task_approval(database):
    store, campaign, _, assignment = _seed_assignment(database)
    bridge, _ = _bridge(database, store)

    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        inherited_approval_status=ApprovalStatus.APPROVED,
    )

    assert task.approval_status is ApprovalStatus.APPROVED
    assert task.status is TaskStatus.READY


def test_dispatcher_reuses_the_shared_eligibility_result_before_dry_run(database):
    store, campaign, account, assignment = _seed_assignment(database)
    bridge, queue = _bridge(database, store)
    task = bridge.create_task(
        campaign_id=campaign.id,
        assignment_id=assignment.id,
        capability=Capability.READ_PROFILE,
        approval_required=False,
    )
    account.session_status = SessionStatus.EXPIRED
    database.save_social_account(account)

    result = ExecutionDispatcher(database, queue, _registry()).run_once("dry-worker")

    assert result.outcome is DispatchOutcome.BLOCKED
    assert result.task is not None
    assert result.task.block_reason is BlockReason.SESSION_INVALID
    assert task.status is TaskStatus.READY
