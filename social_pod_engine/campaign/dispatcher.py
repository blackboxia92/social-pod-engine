"""A deliberately dry-run dispatcher for durable execution intents."""

from __future__ import annotations

from ..persistence import SocialPodDatabase
from ..registry import AdapterRegistry
from .eligibility import ExecutionEligibilityEvaluator, ExecutionEligibilityPolicy
from .execution_persistence import ExecutionTaskStore
from .models import BlockReason, DispatchOutcome, ExecutionDispatchResult


class ExecutionDispatcher:
    """Claims and simulates work. Network execution is intentionally unavailable in this phase.

    Before real writes are enabled, the task model must add an
    ``UNKNOWN_EXTERNAL_STATE`` outcome for send-timeout/unknown-result cases.
    """

    def __init__(
        self,
        database: SocialPodDatabase,
        queue: ExecutionTaskStore,
        adapters: AdapterRegistry,
        *,
        execution_enabled: bool = False,
        eligibility_policy: ExecutionEligibilityPolicy | None = None,
    ) -> None:
        if execution_enabled:
            raise ValueError("network execution is not enabled in this dry-run phase")
        self.database = database
        self.queue = queue
        self.eligibility = ExecutionEligibilityEvaluator(adapters, eligibility_policy)

    def run_once(self, worker_id: str) -> ExecutionDispatchResult:
        ready = self.queue.dequeue_ready(limit=1)
        if not ready:
            return ExecutionDispatchResult(DispatchOutcome.NO_TASK)
        claimed = self.queue.claim(ready[0].id, worker_id=worker_id, increment_attempt=False)
        if claimed is None:
            return ExecutionDispatchResult(DispatchOutcome.CLAIM_LOST)
        account = self.database.get_social_account(claimed.account_id)
        if account is None:
            task = self.queue.block(claimed.id, BlockReason.ACCOUNT_NOT_FOUND, "account no longer exists")
            return ExecutionDispatchResult(DispatchOutcome.BLOCKED, task)
        eligibility = self.eligibility.evaluate(account, claimed.capability)
        if not eligibility.eligible:
            task = self.queue.block(
                claimed.id,
                eligibility.block_reason or BlockReason.UNKNOWN,
                eligibility.description,
            )
            return ExecutionDispatchResult(DispatchOutcome.BLOCKED, task)
        # Dry-run validates the entire queue path, then releases the logical lease.
        # COMPLETED remains reserved for a future confirmed external execution.
        task = self.queue.release(claimed.id, event="WOULD_EXECUTE")
        return ExecutionDispatchResult(DispatchOutcome.WOULD_EXECUTE, task)
