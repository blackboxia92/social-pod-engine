"""Persistable, non-executing campaign assignment plan models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from ..adapters.base import Capability
from ..domain import SocialPlatform, utc_now
from ..narrative.domain import NarrativeRole


class AssignmentStatus(str, Enum):
    PLANNED = "planned"


class TaskStatus(str, Enum):
    """Lifecycle of an execution intent; no value implies a network write."""

    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    account_id: UUID
    account_group_id: UUID
    brief_id: UUID
    narrative_id: UUID
    role: NarrativeRole
    status: AssignmentStatus = AssignmentStatus.PLANNED
    id: UUID = field(default_factory=uuid4)


@dataclass(slots=True)
class AssignmentPlan:
    campaign_id: UUID
    assignments: list[PlannedAssignment]
    status: AssignmentStatus = AssignmentStatus.PLANNED
    created_at: datetime = field(default_factory=utc_now)
    id: UUID = field(default_factory=uuid4)

    def to_dict(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "campaign_id": str(self.campaign_id),
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "assignments": [
                {
                    "account_id": str(item.account_id),
                    "account_group_id": str(item.account_group_id),
                    "brief_id": str(item.brief_id),
                    "narrative_id": str(item.narrative_id),
                    "role": item.role.value,
                    "status": item.status.value,
                }
                for item in self.assignments
            ],
        }


@dataclass(slots=True)
class ExecutionTask:
    """A durable, approval-gated execution intent owned by Social Pod."""

    campaign_id: UUID
    assignment_id: UUID
    account_id: UUID
    platform: SocialPlatform
    capability: Capability
    payload: dict[str, object]
    idempotency_key: str
    scheduled_for: datetime
    status: TaskStatus = TaskStatus.PLANNED
    approval_status: ApprovalStatus = ApprovalStatus.PENDING
    attempt_count: int = 0
    max_attempts: int = 3
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    last_error: str | None = None
    claimed_by: str | None = None
    claimed_until: datetime | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        self.platform = SocialPlatform(self.platform)
        self.capability = Capability(self.capability)
        self.status = TaskStatus(self.status)
        self.approval_status = ApprovalStatus(self.approval_status)
        if not self.idempotency_key.strip():
            raise ValueError("ExecutionTask.idempotency_key is required")
        if self.attempt_count < 0 or self.max_attempts < 1:
            raise ValueError("attempt counts must be non-negative and max_attempts must be positive")
        self.payload = _normalize_payload(self.payload)
        for name in ("scheduled_for", "created_at", "started_at", "completed_at", "claimed_until"):
            value = getattr(self, name)
            if value is not None:
                if value.tzinfo is None:
                    raise ValueError(f"ExecutionTask.{name} must be timezone-aware")
                setattr(self, name, value.astimezone(timezone.utc))
        self.last_error = self.last_error.strip() if self.last_error else None
        self.claimed_by = self.claimed_by.strip() if self.claimed_by else None


def _normalize_payload(payload: dict[str, object]) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise TypeError("ExecutionTask.payload must be a dictionary")
    forbidden = {"password", "token", "secret", "cookie", "authorization", "credential"}
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        key_string = str(key)
        if key_string.lower() in forbidden or key_string.lower().endswith("_token"):
            raise ValueError(f"ExecutionTask.payload must not contain secrets ({key_string!r})")
        normalized[key_string] = value
    return normalized
