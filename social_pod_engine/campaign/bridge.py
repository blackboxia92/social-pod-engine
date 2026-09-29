"""Safe translation from a planned assignment to an approval-gated task."""

from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from uuid import UUID

from ..adapters.base import Capability
from ..domain import (
    HealthStatus,
    LifecycleStatus,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    utc_now,
)
from ..narrative.persistence import NarrativeCampaignStore
from ..persistence import SocialPodDatabase
from ..registry import AdapterRegistry
from .execution_persistence import ExecutionTaskStore
from .models import ApprovalStatus, ExecutionTask, TaskStatus


class TaskPreconditionValidator:
    """The execution plane consumes, but does not mutate, Control Plane eligibility."""

    def __init__(self, database: SocialPodDatabase, adapters: AdapterRegistry) -> None:
        self.database = database
        self.adapters = adapters

    def reason(self, account: SocialAccount, capability: Capability) -> str | None:
        if account.quarantined:
            return "ACCOUNT_QUARANTINED"
        if account.lifecycle_status not in {LifecycleStatus.WARMUP, LifecycleStatus.ACTIVE}:
            return "ACCOUNT_LIFECYCLE_INELIGIBLE"
        if account.health_status is not HealthStatus.HEALTHY:
            return "ACCOUNT_HEALTH_NOT_HEALTHY"
        if account.session_status is not SessionStatus.VALID:
            return "ACCOUNT_SESSION_NOT_VALID"
        if account.quota_status is QuotaStatus.EXHAUSTED:
            return "ACCOUNT_QUOTA_EXHAUSTED"
        try:
            adapter = self.adapters.get(account.platform.value)
        except KeyError:
            return "CAPABILITY_NOT_SUPPORTED"
        if capability not in adapter.get_supported_capabilities():
            return "CAPABILITY_NOT_SUPPORTED"
        return None


class ExecutionBridge:
    """Creates structured execution intents only; it never calls adapter execution methods."""

    def __init__(
        self,
        database: SocialPodDatabase,
        campaigns: NarrativeCampaignStore,
        queue: ExecutionTaskStore,
        adapters: AdapterRegistry,
    ) -> None:
        self.database = database
        self.campaigns = campaigns
        self.queue = queue
        self.validator = TaskPreconditionValidator(database, adapters)

    def create_task(
        self,
        *,
        campaign_id: UUID,
        assignment_id: UUID,
        capability: Capability = Capability.POST,
        approval_required: bool = True,
        scheduled_for: datetime | None = None,
    ) -> ExecutionTask:
        assignment = self.campaigns.get_assignment(assignment_id)
        if assignment is None:
            raise KeyError(f"Assignment not found: {assignment_id}")
        account = self.database.get_social_account(assignment.account_id)
        if account is None:
            raise KeyError(f"SocialAccount not found: {assignment.account_id}")
        key = self.idempotency_key(assignment.id, capability)
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
            scheduled_for=scheduled_for or utc_now(),
            approval_status=(ApprovalStatus.PENDING if approval_required else ApprovalStatus.NOT_REQUIRED),
        )
        stored = self.queue.enqueue(task)
        if stored.id != task.id or approval_required:
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
        account = self.database.get_social_account(task.account_id)
        if account is None:
            return self.queue.block(task.id, "ACCOUNT_NOT_FOUND")
        reason = self.validator.reason(account, task.capability)
        if reason:
            return self.queue.block(task.id, reason)
        if task.status is TaskStatus.PLANNED:
            return self.queue.mark_ready(task.id)
        return task

    @staticmethod
    def idempotency_key(assignment_id: UUID, capability: Capability) -> str:
        material = f"{assignment_id}:{Capability(capability).value}".encode()
        return sha256(material).hexdigest()
