"""Durable, Social-Pod-owned queue storage for dry-run execution tasks."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError

from ..domain import SocialPlatform, utc_now
from ..persistence import SocialPodDatabase, _restore_datetime
from .models import ApprovalStatus, BlockReason, ExecutionTask, TaskStatus

_metadata = MetaData()
_tasks = Table(
    "execution_tasks",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("campaign_id", String(36), nullable=False, index=True),
    Column("assignment_id", String(36), nullable=False, index=True),
    Column("account_id", String(36), nullable=False, index=True),
    Column("platform", String(32), nullable=False),
    Column("capability", String(64), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("status", String(32), nullable=False, index=True),
    Column("approval_status", String(32), nullable=False),
    Column("idempotency_key", String(255), nullable=False, unique=True),
    Column("revision", Integer, nullable=False, default=1),
    Column("attempt_count", Integer, nullable=False, default=0),
    Column("max_attempts", Integer, nullable=False, default=3),
    Column("scheduled_for", DateTime(timezone=True), nullable=False, index=True),
    Column("schedule_is_immediate", Integer, nullable=False, default=0),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    Column("last_error", String(512), nullable=True),
    Column("block_reason", String(64), nullable=True),
    Column("claimed_by", String(255), nullable=True),
    Column("claimed_until", DateTime(timezone=True), nullable=True, index=True),
    Column("external_id", String(255), nullable=True),
    Column("external_url", String(1024), nullable=True),
    Column("external_confirmed_at", DateTime(timezone=True), nullable=True),
)
_events = Table(
    "execution_task_events",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("task_id", String(36), nullable=False, index=True),
    Column("event_type", String(32), nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("reason", String(512), nullable=True),
    Column("details", JSON, nullable=False),
)


class InvalidTaskTransition(ValueError):
    """Raised when a caller tries a state transition outside the queue contract."""


class ExecutionTaskStore:
    """Repository with atomic SQLite claims and durable, append-only task events."""

    _transitions = {
        TaskStatus.PLANNED: {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
        TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
        TaskStatus.RUNNING: {TaskStatus.READY, TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED},
        TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.CANCELLED},
        TaskStatus.UNKNOWN_EXTERNAL_STATE: {TaskStatus.READY, TaskStatus.COMPLETED},
        TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED},
        TaskStatus.COMPLETED: set(),
        TaskStatus.CANCELLED: set(),
    }

    def __init__(self, database: SocialPodDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        _metadata.create_all(self.database.engine)
        columns = {column["name"] for column in inspect(self.database.engine).get_columns("execution_tasks")}
        migrations = {
            "revision": "ALTER TABLE execution_tasks ADD COLUMN revision INTEGER NOT NULL DEFAULT 1",
            "schedule_is_immediate": (
                "ALTER TABLE execution_tasks ADD COLUMN schedule_is_immediate INTEGER NOT NULL DEFAULT 0"
            ),
            "block_reason": "ALTER TABLE execution_tasks ADD COLUMN block_reason VARCHAR(64)",
            "external_id": "ALTER TABLE execution_tasks ADD COLUMN external_id VARCHAR(255)",
            "external_url": "ALTER TABLE execution_tasks ADD COLUMN external_url VARCHAR(1024)",
            "external_confirmed_at": "ALTER TABLE execution_tasks ADD COLUMN external_confirmed_at DATETIME",
        }
        with self.database.engine.begin() as connection:
            for name, statement in migrations.items():
                if name not in columns:
                    connection.execute(text(statement))

    def enqueue(self, task: ExecutionTask) -> ExecutionTask:
        """Persist once per assignment/capability/revision and return an exact retry."""
        self.initialize()
        try:
            with self.database.engine.begin() as connection:
                connection.execute(_tasks.insert().values(**_task_values(task)))
                self._event(connection, task.id, "CREATED")
            return task
        except IntegrityError:
            existing = self.get_by_idempotency_key(task.idempotency_key)
            if existing is None:
                raise
            return existing

    def get(self, task_id: UUID) -> ExecutionTask | None:
        self.initialize()
        with self.database.engine.connect() as connection:
            row = connection.execute(select(_tasks).where(_tasks.c.id == str(task_id))).one_or_none()
        return _task_from_row(row) if row is not None else None

    def get_by_idempotency_key(self, key: str) -> ExecutionTask | None:
        self.initialize()
        with self.database.engine.connect() as connection:
            row = connection.execute(select(_tasks).where(_tasks.c.idempotency_key == key)).one_or_none()
        return _task_from_row(row) if row is not None else None

    def approve(self, task_id: UUID) -> ExecutionTask:
        return self._update(task_id, approval_status=ApprovalStatus.APPROVED, event="APPROVED")

    def reject(self, task_id: UUID, reason: str) -> ExecutionTask:
        return self._update(
            task_id,
            approval_status=ApprovalStatus.REJECTED,
            status=TaskStatus.CANCELLED,
            last_error=reason,
            event="CANCELLED",
        )

    def mark_ready(self, task_id: UUID) -> ExecutionTask:
        task = self._required(task_id)
        if task.approval_status is ApprovalStatus.PENDING:
            raise InvalidTaskTransition("a pending-approval task cannot become READY")
        if task.approval_status is ApprovalStatus.REJECTED:
            raise InvalidTaskTransition("a rejected task cannot become READY")
        return self._transition(task, TaskStatus.READY, event="READY", clear_block=True)

    def block(
        self, task_id: UUID, reason: BlockReason, description: str | None = None
    ) -> ExecutionTask:
        return self._transition(
            self._required(task_id),
            TaskStatus.BLOCKED,
            reason=description,
            block_reason=reason,
            event="BLOCKED",
        )

    def cancel(self, task_id: UUID, reason: str | None = None) -> ExecutionTask:
        return self._transition(self._required(task_id), TaskStatus.CANCELLED, reason=reason, event="CANCELLED")

    def dequeue_ready(self, *, now: datetime | None = None, limit: int = 1) -> list[ExecutionTask]:
        now = now or utc_now()
        self.recover_expired_claims(now=now)
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(_tasks)
                .where(_tasks.c.status == TaskStatus.READY.value)
                .where((_tasks.c.schedule_is_immediate == 1) | (_tasks.c.scheduled_for <= now))
                .order_by(_tasks.c.scheduled_for, _tasks.c.created_at)
                .limit(limit)
            )
        return [_task_from_row(row) for row in rows]

    def claim(
        self,
        task_id: UUID,
        *,
        worker_id: str,
        lease_seconds: int = 60,
        increment_attempt: bool = True,
        now: datetime | None = None,
    ) -> ExecutionTask | None:
        """Atomically claim one due READY task; a second worker receives ``None``."""
        now = now or utc_now()
        claimed_until = now + timedelta(seconds=lease_seconds)
        with self.database.engine.begin() as connection:
            result = connection.execute(
                update(_tasks)
                .where(_tasks.c.id == str(task_id))
                .where(_tasks.c.status == TaskStatus.READY.value)
                .where(_tasks.c.scheduled_for <= now)
                .values(
                    status=TaskStatus.RUNNING.value,
                    claimed_by=worker_id,
                    claimed_until=claimed_until,
                    started_at=now,
                    attempt_count=(
                        _tasks.c.attempt_count + 1 if increment_attempt else _tasks.c.attempt_count
                    ),
                )
            )
            if result.rowcount != 1:
                return None
            self._event(connection, task_id, "CLAIMED", details={"worker_id": worker_id})
        return self.get(task_id)

    def release(self, task_id: UUID, *, event: str = "RELEASED") -> ExecutionTask:
        return self._transition(self._required(task_id), TaskStatus.READY, event=event, clear_claim=True)

    def complete(self, task_id: UUID) -> ExecutionTask:
        return self._transition(
            self._required(task_id),
            TaskStatus.COMPLETED,
            event="COMPLETED",
            clear_claim=True,
            completed_at=utc_now(),
        )

    def complete_external(self, task_id: UUID, *, external_id: str | None, external_url: str | None) -> ExecutionTask:
        task = self._required(task_id)
        completed = self._transition(task, TaskStatus.COMPLETED, event="EXECUTION_COMPLETED", clear_claim=True, completed_at=utc_now())
        return self._update(completed.id, event="EXTERNAL_ACTION_CONFIRMED", external_id=external_id, external_url=external_url, external_confirmed_at=utc_now())

    def mark_unknown_external(self, task_id: UUID, reason: str) -> ExecutionTask:
        return self._transition(self._required(task_id), TaskStatus.UNKNOWN_EXTERNAL_STATE, reason=reason, event="EXTERNAL_STATE_UNKNOWN", clear_claim=True)

    def add_event(self, task_id: UUID, event: str, details: dict[str, object] | None = None) -> None:
        with self.database.engine.begin() as connection:
            self._event(connection, task_id, event, details=details)

    def update_payload(self, task_id: UUID, payload: dict[str, object]) -> ExecutionTask:
        """Attach approved editorial content without changing queue status or executing it."""
        task = self._required(task_id)
        if task.status in {TaskStatus.RUNNING, TaskStatus.COMPLETED, TaskStatus.CANCELLED}:
            raise InvalidTaskTransition("cannot replace payload after execution lifecycle has started")
        replacement = ExecutionTask(
            id=task.id, campaign_id=task.campaign_id, assignment_id=task.assignment_id, account_id=task.account_id,
            platform=task.platform, capability=task.capability, payload=payload, idempotency_key=task.idempotency_key,
            scheduled_for=task.scheduled_for, revision=task.revision, status=task.status, approval_status=task.approval_status,
            attempt_count=task.attempt_count, max_attempts=task.max_attempts, created_at=task.created_at,
            started_at=task.started_at, completed_at=task.completed_at, last_error=task.last_error, block_reason=task.block_reason,
            claimed_by=task.claimed_by, claimed_until=task.claimed_until,
        )
        with self.database.engine.begin() as connection:
            connection.execute(update(_tasks).where(_tasks.c.id == str(task.id)).values(payload=replacement.payload))
            self._event(connection, task.id, "CONTENT_ATTACHED", details={"revision": task.revision})
        return self._required(task.id)

    def fail(self, task_id: UUID, reason: str) -> ExecutionTask:
        task = self._required(task_id)
        target = TaskStatus.READY if task.attempt_count < task.max_attempts else TaskStatus.FAILED
        return self._transition(task, target, reason=reason, event="FAILED", clear_claim=True)

    def recover_expired_claims(self, *, now: datetime | None = None) -> int:
        now = now or utc_now()
        with self.database.engine.begin() as connection:
            result = connection.execute(
                update(_tasks)
                .where(_tasks.c.status == TaskStatus.RUNNING.value)
                .where(_tasks.c.claimed_until <= now)
                .values(status=TaskStatus.READY.value, claimed_by=None, claimed_until=None)
            )
        return result.rowcount or 0

    def report(self) -> ExecutionQueueReport:
        self.initialize()
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(_tasks.c.status, _tasks.c.account_id, _tasks.c.block_reason)
            ).all()
        counts = Counter(TaskStatus(row.status) for row in rows)
        blocks = Counter(
            (str(row.account_id), BlockReason(row.block_reason or BlockReason.UNKNOWN.value))
            for row in rows
            if row.status == TaskStatus.BLOCKED.value
        )
        return ExecutionQueueReport(counts=dict(counts), blocked_by_account_reason=dict(blocks))

    def list_tasks(self) -> list[ExecutionTask]:
        self.initialize()
        with self.database.engine.connect() as connection:
            rows = connection.execute(select(_tasks).order_by(_tasks.c.created_at, _tasks.c.id))
        return [_task_from_row(row) for row in rows]

    def list_events(self, task_id: UUID) -> list[dict[str, object]]:
        self.initialize()
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(_events).where(_events.c.task_id == str(task_id)).order_by(_events.c.id)
            ).all()
        return [
            {
                "event_type": row.event_type,
                "occurred_at": _restore_datetime(row.occurred_at),
                "reason": row.reason,
                "details": dict(row.details),
            }
            for row in rows
        ]

    def _required(self, task_id: UUID) -> ExecutionTask:
        task = self.get(task_id)
        if task is None:
            raise KeyError(f"Execution task not found: {task_id}")
        return task

    def _transition(
        self,
        task: ExecutionTask,
        target: TaskStatus,
        *,
        reason: str | None = None,
        block_reason: BlockReason | None = None,
        event: str,
        clear_claim: bool = False,
        clear_block: bool = False,
        completed_at: datetime | None = None,
    ) -> ExecutionTask:
        if target not in self._transitions[task.status]:
            raise InvalidTaskTransition(f"cannot transition {task.status.value} to {target.value}")
        values: dict[str, Any] = {"status": target.value, "last_error": reason}
        if block_reason is not None:
            values["block_reason"] = block_reason.value
        elif clear_block:
            values["block_reason"] = None
        if clear_claim:
            values.update(claimed_by=None, claimed_until=None)
        if completed_at is not None:
            values["completed_at"] = completed_at
        with self.database.engine.begin() as connection:
            result = connection.execute(
                update(_tasks)
                .where(_tasks.c.id == str(task.id))
                .where(_tasks.c.status == task.status.value)
                .values(**values)
            )
            if result.rowcount != 1:
                raise InvalidTaskTransition("task changed before its transition could be persisted")
            details: dict[str, object] | None = (
                {"block_reason": block_reason.value} if block_reason is not None else None
            )
            self._event(connection, task.id, event, reason=reason, details=details)
        return self._required(task.id)

    def _update(self, task_id: UUID, *, event: str, **values: Any) -> ExecutionTask:
        self._required(task_id)
        prepared = {
            key: value.value if isinstance(value, (TaskStatus, ApprovalStatus)) else value
            for key, value in values.items()
        }
        with self.database.engine.begin() as connection:
            connection.execute(update(_tasks).where(_tasks.c.id == str(task_id)).values(**prepared))
            self._event(connection, task_id, event)
        return self._required(task_id)

    @staticmethod
    def _event(
        connection: Any,
        task_id: UUID,
        event_type: str,
        *,
        reason: str | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        connection.execute(
            _events.insert().values(
                task_id=str(task_id),
                event_type=event_type,
                occurred_at=utc_now(),
                reason=reason,
                details=details or {},
            )
        )


class ExecutionQueueReport:
    """Operational counts without treating blocked tasks as successful work."""

    def __init__(
        self,
        *,
        counts: dict[TaskStatus, int],
        blocked_by_account_reason: dict[tuple[str, BlockReason], int],
    ) -> None:
        self.counts = counts
        self.blocked_by_account_reason = blocked_by_account_reason

    def count(self, status: TaskStatus) -> int:
        return self.counts.get(status, 0)


def _task_values(task: ExecutionTask) -> dict[str, object]:
    return {
        "id": str(task.id),
        "campaign_id": str(task.campaign_id),
        "assignment_id": str(task.assignment_id),
        "account_id": str(task.account_id),
        "platform": task.platform.value,
        "capability": task.capability.value,
        "payload": task.payload,
        "status": task.status.value,
        "approval_status": task.approval_status.value,
        "idempotency_key": task.idempotency_key,
        "revision": task.revision,
        "attempt_count": task.attempt_count,
        "max_attempts": task.max_attempts,
        "scheduled_for": task.scheduled_for or task.created_at,
        "schedule_is_immediate": 1 if task.scheduled_for is None else 0,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "last_error": task.last_error,
        "block_reason": task.block_reason.value if task.block_reason is not None else None,
        "claimed_by": task.claimed_by,
        "claimed_until": task.claimed_until,
        "external_id": task.external_id,
        "external_url": task.external_url,
        "external_confirmed_at": task.external_confirmed_at,
    }


def _task_from_row(row: Any) -> ExecutionTask:
    from ..adapters.base import Capability

    return ExecutionTask(
        id=UUID(str(row.id)),
        campaign_id=UUID(str(row.campaign_id)),
        assignment_id=UUID(str(row.assignment_id)),
        account_id=UUID(str(row.account_id)),
        platform=SocialPlatform(row.platform),
        capability=Capability(row.capability),
        payload=dict(row.payload),
        status=TaskStatus(row.status),
        approval_status=ApprovalStatus(row.approval_status),
        idempotency_key=row.idempotency_key,
        revision=row.revision,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        scheduled_for=None if row.schedule_is_immediate else _restore_datetime(row.scheduled_for),
        created_at=_restore_datetime(row.created_at),
        started_at=_restore_datetime(row.started_at) if row.started_at else None,
        completed_at=_restore_datetime(row.completed_at) if row.completed_at else None,
        last_error=row.last_error,
        block_reason=BlockReason(row.block_reason) if row.block_reason else None,
        claimed_by=row.claimed_by,
        claimed_until=_restore_datetime(row.claimed_until) if row.claimed_until else None,
        external_id=row.external_id,
        external_url=row.external_url,
        external_confirmed_at=_restore_datetime(row.external_confirmed_at) if row.external_confirmed_at else None,
    )
