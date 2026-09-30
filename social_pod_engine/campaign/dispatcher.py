"""A deliberately dry-run dispatcher for durable execution intents."""

from __future__ import annotations

from ..adapters.base import Capability, ExecutionContext, ExternalExecutionResult
from ..content.models import ContentDraftStatus
from ..content.persistence import ContentDraftStore
from ..domain import SocialPlatform
from ..execution.gateway import UpstreamExecutionGateway
from ..persistence import SocialPodDatabase
from ..registry import AdapterRegistry
from .eligibility import ExecutionEligibilityEvaluator, ExecutionEligibilityPolicy
from .execution_persistence import ExecutionTaskStore
from .models import BlockReason, DispatchOutcome, ExecutionDispatchResult, TaskStatus


class ExecutionDispatcher:
    """Dry-run by default; explicit real mode supports confirmed X POST only."""

    def __init__(
        self,
        database: SocialPodDatabase,
        queue: ExecutionTaskStore,
        adapters: AdapterRegistry,
        *,
        execution_enabled: bool = False,
        eligibility_policy: ExecutionEligibilityPolicy | None = None,
        gateway: UpstreamExecutionGateway | None = None,
        content_store: ContentDraftStore | None = None,
    ) -> None:
        if execution_enabled and (gateway is None or content_store is None):
            raise ValueError("real execution requires explicit gateway and content store")
        self.database = database
        self.queue = queue
        self.eligibility = ExecutionEligibilityEvaluator(adapters, eligibility_policy)
        self.adapters = adapters
        self.execution_enabled = execution_enabled
        self.gateway = gateway
        self.content_store = content_store

    def run_once(self, worker_id: str) -> ExecutionDispatchResult:
        if self.execution_enabled:
            raise RuntimeError("use await run_once_async() for real execution")
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

    async def run_once_async(self, worker_id: str) -> ExecutionDispatchResult:
        if not self.execution_enabled:
            return self.run_once(worker_id)
        ready = self.queue.dequeue_ready(limit=1)
        if not ready:
            return ExecutionDispatchResult(DispatchOutcome.NO_TASK)
        task = self.queue.claim(ready[0].id, worker_id=worker_id)
        if task is None:
            return ExecutionDispatchResult(DispatchOutcome.CLAIM_LOST)
        account = self.database.get_social_account(task.account_id)
        if account is None:
            return ExecutionDispatchResult(DispatchOutcome.BLOCKED, self.queue.block(task.id, BlockReason.ACCOUNT_NOT_FOUND, "account missing"))
        eligibility = self.eligibility.evaluate(account, task.capability)
        if not eligibility.eligible:
            return ExecutionDispatchResult(DispatchOutcome.BLOCKED, self.queue.block(task.id, eligibility.block_reason or BlockReason.UNKNOWN, eligibility.description))
        draft = self._approved_draft(task)
        if draft is None or task.capability is not Capability.POST or task.platform is not SocialPlatform.X:
            return ExecutionDispatchResult(DispatchOutcome.BLOCKED, self.queue.block(task.id, BlockReason.CONTENT_NOT_APPROVED, "approved X POST content is required"))
        if task.external_id or task.status is TaskStatus.COMPLETED:
            return ExecutionDispatchResult(DispatchOutcome.BLOCKED, self.queue.block(task.id, BlockReason.UNKNOWN, "external execution already confirmed"))
        gateway = self.gateway
        assert gateway is not None
        lease = browser = None
        attempted = False
        try:
            self.queue.add_event(task.id, "EXECUTION_STARTED")
            lease = await gateway.acquire_lease(account.upstream_profile_id or "", proxy_id=account.proxy_id)
            browser = await gateway.open_browser(lease)
            adapter = self.adapters.get("x")
            attempted = True
            self.queue.add_event(task.id, "EXTERNAL_ACTION_ATTEMPTED")
            result = await adapter.execute_capability(Capability.POST, task.payload, ExecutionContext(page=browser.page, social_account_id=str(account.id), upstream_profile_id=account.upstream_profile_id))
            if isinstance(result, ExternalExecutionResult) and result.confirmed:
                return ExecutionDispatchResult(DispatchOutcome.WOULD_EXECUTE, self.queue.complete_external(task.id, external_id=result.external_id, external_url=result.external_url))
            return ExecutionDispatchResult(DispatchOutcome.BLOCKED, self.queue.mark_unknown_external(task.id, "external action could not be confirmed"))
        except Exception as exc:
            if attempted:
                return ExecutionDispatchResult(DispatchOutcome.BLOCKED, self.queue.mark_unknown_external(task.id, f"post-action error: {type(exc).__name__}"))
            return ExecutionDispatchResult(DispatchOutcome.BLOCKED, self.queue.fail(task.id, f"pre-execution error: {type(exc).__name__}"))
        finally:
            if browser is not None:
                await gateway.close_browser(browser)
            if lease is not None:
                await gateway.release_lease(lease)

    def reconcile_unknown_task(self, task_id, *, confirmed: bool | None):
        task = self.queue.get(task_id)
        if task is None or task.status is not TaskStatus.UNKNOWN_EXTERNAL_STATE:
            raise ValueError("only unknown external tasks can be reconciled")
        self.queue.add_event(task.id, "RECONCILIATION_STARTED")
        if confirmed is True:
            return self.queue.complete_external(task.id, external_id=task.external_id, external_url=task.external_url)
        if confirmed is False:
            return self.queue.release(task.id, event="RECONCILIATION_RESOLVED")
        return task

    def _approved_draft(self, task):
        if self.content_store is None:
            return None
        draft_id = task.payload.get("content_draft_id")
        if not isinstance(draft_id, str) or task.payload.get("revision") != task.revision:
            return None
        from uuid import UUID
        draft = self.content_store.get(UUID(draft_id))
        if draft is None or draft.status is not ContentDraftStatus.APPROVED or draft.revision != task.revision:
            return None
        if task.payload.get("text") != draft.text or task.payload.get("content_type") != "post":
            return None
        return draft
