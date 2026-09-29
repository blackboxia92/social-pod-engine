"""Storage-neutral queue, item and report models for onboarding."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID, uuid4


class OnboardingQueueStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"


class OnboardingItemStatus(str, Enum):
    SUCCESS = "success"
    CHALLENGE = "challenge"
    SKIPPED = "skipped"
    FAILED = "failed"


class OnboardingSessionStep(str, Enum):
    PROFILE_PROVISIONING = "profile_provisioning"
    LEASE_ACQUISITION = "lease_acquisition"
    BROWSER_OPEN = "browser_open"
    AWAITING_LOGIN = "awaiting_login"
    SESSION_VALIDATION = "session_validation"
    CLOSING = "closing"


@dataclass(frozen=True, slots=True)
class OnboardingItemResult:
    account_id: UUID
    username: str
    status: OnboardingItemStatus
    upstream_profile_id: str | None
    reason: str | None = None

    def to_payload(self) -> dict[str, str | None]:
        return {
            "account_id": str(self.account_id),
            "username": self.username,
            "status": self.status.value,
            "upstream_profile_id": self.upstream_profile_id,
            "reason": self.reason,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> OnboardingItemResult:
        return cls(
            account_id=UUID(str(payload["account_id"])),
            username=str(payload["username"]),
            status=OnboardingItemStatus(str(payload["status"])),
            upstream_profile_id=payload.get("upstream_profile_id"),
            reason=payload.get("reason"),
        )


@dataclass(slots=True)
class OnboardingQueueState:
    """Persisted cursor and counters for a reproducible sequential batch."""

    account_ids: list[UUID]
    id: UUID = field(default_factory=uuid4)
    current_index: int = 0
    total_accounts: int = 0
    completed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    status: OnboardingQueueStatus = OnboardingQueueStatus.READY
    details: list[OnboardingItemResult] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.account_ids = [UUID(str(account_id)) for account_id in self.account_ids]
        if self.total_accounts == 0:
            self.total_accounts = len(self.account_ids)
        if self.total_accounts != len(self.account_ids):
            raise ValueError("total_accounts must match the persisted account_ids")
        if not 0 <= self.current_index <= self.total_accounts:
            raise ValueError("current_index is outside the onboarding queue")
        self.status = OnboardingQueueStatus(self.status)

    @property
    def current_account_id(self) -> UUID | None:
        if self.current_index >= self.total_accounts:
            return None
        return self.account_ids[self.current_index]

    def to_payload(self) -> dict[str, object]:
        return {
            "account_ids": [str(account_id) for account_id in self.account_ids],
            "current_index": self.current_index,
            "total_accounts": self.total_accounts,
            "completed_count": self.completed_count,
            "skipped_count": self.skipped_count,
            "failed_count": self.failed_count,
            "status": self.status.value,
            "details": [item.to_payload() for item in self.details],
        }

    @classmethod
    def from_payload(cls, queue_id: UUID, payload: Mapping[str, Any]) -> OnboardingQueueState:
        raw_details = payload.get("details", [])
        if not isinstance(raw_details, list):
            raise ValueError("persisted onboarding details are invalid")
        if not all(isinstance(item, Mapping) for item in raw_details):
            raise ValueError("persisted onboarding detail is invalid")
        account_ids = payload.get("account_ids")
        if not isinstance(account_ids, list):
            raise ValueError("persisted onboarding account ids are invalid")
        return cls(
            id=queue_id,
            account_ids=[UUID(str(value)) for value in account_ids],
            current_index=int(payload["current_index"]),
            total_accounts=int(payload["total_accounts"]),
            completed_count=int(payload["completed_count"]),
            skipped_count=int(payload["skipped_count"]),
            failed_count=int(payload["failed_count"]),
            status=OnboardingQueueStatus(str(payload["status"])),
            details=[OnboardingItemResult.from_payload(item) for item in raw_details],
        )


@dataclass(frozen=True, slots=True)
class OnboardingBatchReport:
    total_processed: int
    successful_logins: int
    challenges_detected: int
    skipped: int
    failed: int
    details: list[OnboardingItemResult]

    @classmethod
    def from_queue(cls, queue: OnboardingQueueState) -> OnboardingBatchReport:
        details = list(queue.details)
        return cls(
            total_processed=len(details),
            successful_logins=sum(item.status is OnboardingItemStatus.SUCCESS for item in details),
            challenges_detected=sum(item.status is OnboardingItemStatus.CHALLENGE for item in details),
            skipped=sum(item.status is OnboardingItemStatus.SKIPPED for item in details),
            failed=sum(item.status is OnboardingItemStatus.FAILED for item in details),
            details=details,
        )
