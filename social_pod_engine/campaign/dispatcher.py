"""A deliberately dry-run dispatcher for durable execution intents."""

from __future__ import annotations

from ..persistence import SocialPodDatabase
from ..registry import AdapterRegistry
from .bridge import TaskPreconditionValidator
from .execution_persistence import ExecutionTaskStore
from .models import ExecutionTask


class ExecutionDispatcher:
    """Claims and simulates work. Network execution is intentionally unavailable in this phase."""

    def __init__(
        self,
        database: SocialPodDatabase,
        queue: ExecutionTaskStore,
        adapters: AdapterRegistry,
        *,
        execution_enabled: bool = False,
    ) -> None:
        if execution_enabled:
            raise ValueError("network execution is not enabled in this dry-run phase")
        self.queue = queue
        self.validator = TaskPreconditionValidator(database, adapters)

    def run_once(self, worker_id: str) -> ExecutionTask | None:
        ready = self.queue.dequeue_ready(limit=1)
        if not ready:
            return None
        claimed = self.queue.claim(ready[0].id, worker_id=worker_id)
        if claimed is None:
            return None
        account = self.validator.database.get_social_account(claimed.account_id)
        if account is None:
            return self.queue.block(claimed.id, "ACCOUNT_NOT_FOUND")
        reason = self.validator.reason(account, claimed.capability)
        if reason:
            return self.queue.block(claimed.id, reason)
        # This phase deliberately records a successful simulation only. It never calls adapters.
        return self.queue.complete(claimed.id)
