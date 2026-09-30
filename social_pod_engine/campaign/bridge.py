"""Safe translation from a planned assignment to an approval-gated task."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import UUID

from ..adapters.base import Capability
from ..narrative.persistence import NarrativeCampaignStore
from ..persistence import SocialPodDatabase
from ..registry import AdapterRegistry
from .eligibility import ExecutionEligibilityEvaluator, ExecutionEligibilityPolicy
from .execution_persistence import ExecutionTaskStore
from .models import ApprovalStatus, BlockReason, ExecutionTask, TaskStatus


class ExecutionBridge:
    """Creates structured execution intents only; it never calls adapter execution methods."""

    def __init__(
        self,
        database: SocialPodDatabase,
        campaigns: NarrativeCampaignStore,
        queue: ExecutionTaskStore,
        adapters: AdapterRegistry,
        eligibility_policy: ExecutionEligibilityPolicy | None = None,
    ) -> None:
        self.database = database
        self.campaigns = campaigns
        self.queue = queue
        self.eligibility = ExecutionEligibilityEvaluator(adapters, eligibility_policy)

    def create_task(
        self,
        *,
        campaign_id: UUID,
        assignment_id: UUID,
        capability: Capability = Capability.POST,
        revision: int = 1,
        approval_required: bool | None = None,
        inherited_approval_status: ApprovalStatus | None = None,
        scheduled_for: datetime | None = None,
    ) -> ExecutionTask:
        assignment = self.campaigns.get_assignment(assignment_id)
        if assignment is None:
            raise KeyError(f"Assignment not found: {assignment_id}")
        account = self.database.get_social_account(assignment.account_id)
        if account is None:
            raise KeyError(f"SocialAccount not found: {assignment.account_id}")
        approval_status = self._approval_status(approval_required, inherited_approval_status)
        key = self.idempotency_key(assignment.id, capability, revision)
        task = ExecutionTask(
            campaign_id=campaign_id,
            assignment_id=assignment.id,
            account_id=account.id,
            platform=account.platform,
            capability=capability,
            payload={
                "brief_id": str(assignment.brief_id),
                "narrative_id": str(assignment.narrative_id),
                "role": assignment.role.value,
                "content_state": "pending_generation",
            },
            idempotency_key=key,
            scheduled_for=scheduled_for,
            revision=revision,
            approval_status=approval_status,
        )
        stored = self.queue.enqueue(task)
        if stored.id != task.id or approval_status is ApprovalStatus.PENDING:
            return stored
        return self.prepare(stored.id)

    def approve(self, task_id: UUID) -> ExecutionTask:
        self.queue.approve(task_id)
        return self.prepare(task_id)

    def prepare(self, task_id: UUID) -> ExecutionTask:
        task = self.queue.get(task_id)
        if task is None:
            raise KeyError(f"Execution task not found: {task_id}")
        if task.approval_status is ApprovalStatus.PENDING:
            return task
        if task.approval_status is ApprovalStatus.REJECTED:
            if task.status is TaskStatus.PLANNED:
                return self.queue.cancel(task.id, "approval was rejected by the inherited or task-level policy")
            return task
        account = self.database.get_social_account(task.account_id)
        if account is None:
            return self.queue.block(task.id, BlockReason.ACCOUNT_NOT_FOUND, "account no longer exists")
        eligibility = self.eligibility.evaluate(account, task.capability)
        if not eligibility.eligible:
            return self.queue.block(
                task.id,
                eligibility.block_reason or BlockReason.UNKNOWN,
                eligibility.description,
            )
        if task.status in {TaskStatus.PLANNED, TaskStatus.BLOCKED}:
            return self.queue.mark_ready(task.id)
        return task

    def reevaluate_blocked_task(self, task_id: UUID) -> ExecutionTask:
        """Reapply the shared policy after an adapter/account condition changes."""
        task = self.queue.get(task_id)
        if task is None:
            raise KeyError(f"Execution task not found: {task_id}")
        if task.status is not TaskStatus.BLOCKED:
            raise ValueError("only blocked tasks can be reevaluated")
        return self.prepare(task_id)

    @staticmethod
    def idempotency_key(assignment_id: UUID, capability: Capability, revision: int = 1) -> str:
        material = f"{assignment_id}:{Capability(capability).value}:{revision}".encode()
        return sha256(material).hexdigest()

    @staticmethod
    def _approval_status(
        approval_required: bool | None, inherited_approval_status: ApprovalStatus | None
    ) -> ApprovalStatus:
        if inherited_approval_status is not None:
            return ApprovalStatus(inherited_approval_status)
        if approval_required is False:
            return ApprovalStatus.NOT_REQUIRED
        return ApprovalStatus.PENDING
