"""A deliberately dry-run dispatcher for durable execution intents."""

from __future__ import annotations

import asyncio
from uuid import UUID

from ..adapters.base import (
    Capability,
    ExecutionContext,
    ExternalActionUncertainError,
    ExternalExecutionResult,
)
from ..content.models import ContentDraftStatus
from ..content.persistence import ContentDraftStore
from ..domain import SocialPlatform
from ..execution.gateway import (
    ExecutionGatewayError,
    ExecutionGatewayFailureKind,
    UpstreamExecutionGateway,
)
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
        max_concurrency: int = 1,
    ) -> None:
        if execution_enabled and (gateway is None or content_store is None):
            raise ValueError("real execution requires explicit gateway and content store")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least one")
        self.database = database
        self.queue = queue
        self.eligibility = ExecutionEligibilityEvaluator(adapters, eligibility_policy)
        self.adapters = adapters
        self.execution_enabled = execution_enabled
        self.gateway = gateway
        self.content_store = content_store
        self._execution_semaphore = asyncio.Semaphore(max_concurrency)

    def run_once(self, worker_id: str) -> ExecutionDispatchResult:
        if self.execution_enabled:
            raise RuntimeError("use await run_once_async() for real execution")
        ready = self.queue.dequeue_ready(limit=1)
        if not ready:
            return ExecutionDispatchResult(DispatchOutcome.NO_TASK)
        return self.run_task(ready[0].id, worker_id)

    def run_task(self, task_id: UUID, worker_id: str) -> ExecutionDispatchResult:
        """Validate exactly one queued task without performing an external write."""
        if self.execution_enabled:
            raise RuntimeError("use await run_task_async() for real execution")
        requested = self.queue.get(task_id)
        if requested is None:
            return ExecutionDispatchResult(DispatchOutcome.NO_TASK)
        if requested.status is not TaskStatus.READY:
            return ExecutionDispatchResult(DispatchOutcome.NOT_READY, requested)
        claimed = self.queue.claim(task_id, worker_id=worker_id, increment_attempt=False)
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
        return await self.run_task_async(ready[0].id, worker_id)

    async def run_task_async(self, task_id: UUID, worker_id: str) -> ExecutionDispatchResult:
        """Run one explicit task, never selecting another READY task as a fallback."""
        if not self.execution_enabled:
            return self.run_task(task_id, worker_id)
        async with self._execution_semaphore:
            return await self._run_real_task_async(task_id, worker_id)

    async def run_tasks_async(
        self, task_ids: list[UUID], worker_id: str
    ) -> list[ExecutionDispatchResult]:
        """Run explicit task ids with the configured execution concurrency limit."""
        return list(
            await asyncio.gather(
                *(self.run_task_async(task_id, worker_id) for task_id in task_ids)
            )
        )

    async def _run_real_task_async(
        self, task_id: UUID, worker_id: str
    ) -> ExecutionDispatchResult:
        requested = self.queue.get(task_id)
        if requested is None:
            return ExecutionDispatchResult(DispatchOutcome.NO_TASK)
        if requested.status is not TaskStatus.READY:
            return ExecutionDispatchResult(DispatchOutcome.NOT_READY, requested)
        task = self.queue.claim(task_id, worker_id=worker_id)
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
            browser = await gateway.open_execution_browser(lease)
            adapter = self.adapters.get("x")
            attempted = True
            self.queue.add_event(task.id, "EXTERNAL_ACTION_ATTEMPTED")
            result = await adapter.execute_capability(Capability.POST, task.payload, ExecutionContext(page=browser.page, social_account_id=str(account.id), upstream_profile_id=account.upstream_profile_id))
            if isinstance(result, ExternalExecutionResult) and result.metadata.get("post_button_clicked"):
                self.queue.add_event(
                    task.id, "POST_BUTTON_CLICKED", self._submit_diagnostics(result.metadata)
                )
            if isinstance(result, ExternalExecutionResult) and result.confirmed:
                return ExecutionDispatchResult(DispatchOutcome.WOULD_EXECUTE, self.queue.complete_external(task.id, external_id=result.external_id, external_url=result.external_url))
            if isinstance(result, ExternalExecutionResult):
                submit_status = result.metadata.get("post_submit_status")
                if submit_status in {
                    "POST_SUBMIT_NOT_STARTED",
                    "POST_SUBMIT_FAILED",
                    "PLATFORM_REJECTED",
                }:
                    detail = str(result.metadata.get("detail", "post submit failed"))
                    self.queue.add_event(
                        task.id, "POST_SUBMIT_FAILED", self._submit_diagnostics(result.metadata)
                    )
                    return ExecutionDispatchResult(
                        DispatchOutcome.POST_NOT_CONFIRMED,
                        self.queue.fail(task.id, f"{submit_status}: {detail}"),
                    )
                if submit_status == "UNKNOWN_EXTERNAL_STATE":
                    detail = str(
                        result.metadata.get(
                            "detail", "click completed but no post confirmation was observed"
                        )
                    )
                    return ExecutionDispatchResult(
                        DispatchOutcome.POST_NOT_CONFIRMED,
                        self.queue.mark_unknown_external(
                            task.id, f"UNKNOWN_EXTERNAL_STATE: {detail}"
                        ),
                    )
            return ExecutionDispatchResult(
                DispatchOutcome.POST_NOT_CONFIRMED,
                self.queue.mark_unknown_external(task.id, "POST_NOT_CONFIRMED"),
            )
        except ExternalActionUncertainError as exc:
            if exc.action_attempted:
                self.queue.add_event(task.id, "POST_BUTTON_CLICKED", self._submit_diagnostics(exc.diagnostics))
            return ExecutionDispatchResult(
                DispatchOutcome.UNKNOWN_EXTERNAL_STATE,
                self.queue.mark_unknown_external(task.id, f"UNKNOWN_EXTERNAL_STATE: {exc}"),
            )
        except ExecutionGatewayError as exc:
            return self._gateway_failure(task.id, exc)
        except Exception as exc:
            if attempted:
                return ExecutionDispatchResult(
                    DispatchOutcome.UNKNOWN_EXTERNAL_STATE,
                    self.queue.mark_unknown_external(task.id, f"UNKNOWN_EXTERNAL_STATE: {type(exc).__name__}"),
                )
            return ExecutionDispatchResult(
                DispatchOutcome.BROWSER_LAUNCH_FAILED,
                self.queue.fail(task.id, f"BROWSER_LAUNCH_FAILED: {type(exc).__name__}"),
            )
        finally:
            try:
                if browser is not None:
                    await gateway.close_execution_browser(browser)
            finally:
                if lease is not None:
                    await gateway.release_lease(lease)

    def _gateway_failure(
        self, task_id: UUID, error: ExecutionGatewayError
    ) -> ExecutionDispatchResult:
        outcome = {
            ExecutionGatewayFailureKind.PROFILE_BUSY: DispatchOutcome.PROFILE_BUSY,
            ExecutionGatewayFailureKind.BROWSER_LAUNCH_FAILED: DispatchOutcome.BROWSER_LAUNCH_FAILED,
            ExecutionGatewayFailureKind.RPC_UNAVAILABLE: DispatchOutcome.RPC_UNAVAILABLE,
        }[error.kind]
        event = (
            "REMOTE_PAGE_NOT_READY"
            if error.operation == "remote_page_readiness"
            else "EXECUTION_RPC_FAILED"
            if error.kind is ExecutionGatewayFailureKind.RPC_UNAVAILABLE
            else "BROWSER_LAUNCH_FAILED"
        )
        self.queue.add_event(task_id, event, error.event_details())
        return ExecutionDispatchResult(outcome, self.queue.fail(task_id, error.task_error()))

    @staticmethod
    def _submit_diagnostics(metadata) -> dict[str, object]:
        """Copy only bounded, content-free adapter diagnostics into audit events."""
        allowed = {
            "url",
            "text_length",
            "text_sha256",
            "composer",
            "post_button",
            "post_click",
            "post_submit_status",
            "detail",
        }
        return {
            key: value
            for key, value in dict(metadata).items()
            if key in allowed
        }

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

    async def reconcile_unknown_task_async(self, task_id: UUID, worker_id: str) -> ExecutionDispatchResult:
        """Read the profile for evidence of an UNKNOWN X POST without retrying it."""
        task = self.queue.get(task_id)
        if task is None or task.status is not TaskStatus.UNKNOWN_EXTERNAL_STATE:
            raise ValueError("only unknown external tasks can be reconciled")
        account = self.database.get_social_account(task.account_id)
        text = task.payload.get("text")
        if account is None or not isinstance(text, str) or not text:
            return ExecutionDispatchResult(DispatchOutcome.UNKNOWN_EXTERNAL_STATE, task)
        gateway = self.gateway
        if gateway is None:
            return ExecutionDispatchResult(DispatchOutcome.UNKNOWN_EXTERNAL_STATE, task)
        async with self._execution_semaphore:
            lease = browser = None
            self.queue.add_event(task.id, "RECONCILIATION_STARTED", {"worker_id": worker_id})
            try:
                lease = await gateway.acquire_lease(
                    account.upstream_profile_id or "", proxy_id=account.proxy_id
                )
                browser = await gateway.open_execution_browser(lease)
                adapter = self.adapters.get("x")
                finder = getattr(adapter, "find_published_post", None)
                evidence = (
                    await finder(text, browser.page) if callable(finder) else None
                )
                if isinstance(evidence, ExternalExecutionResult) and evidence.confirmed:
                    self.queue.add_event(task.id, "RECONCILIATION_RESOLVED")
                    completed = self.queue.complete_external(
                        task.id,
                        external_id=evidence.external_id,
                        external_url=evidence.external_url,
                    )
                    return ExecutionDispatchResult(DispatchOutcome.WOULD_EXECUTE, completed)
                return ExecutionDispatchResult(DispatchOutcome.UNKNOWN_EXTERNAL_STATE, task)
            except ExecutionGatewayError:
                return ExecutionDispatchResult(DispatchOutcome.UNKNOWN_EXTERNAL_STATE, task)
            finally:
                try:
                    if browser is not None:
                        await gateway.close_execution_browser(browser)
                finally:
                    if lease is not None:
                        await gateway.release_lease(lease)

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
