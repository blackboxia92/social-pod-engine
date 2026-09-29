"""Structured outcomes, history records and operator reports for health checks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from ..domain import HealthIssueType, HealthStatus, SessionStatus, SocialPlatform, utc_now


class OperationalCheckStatus(str, Enum):
    CHECKED = "checked"
    SKIPPED_NOT_ONBOARDED = "skipped_not_onboarded"
    SKIPPED_INELIGIBLE = "skipped_ineligible"
    FAILED_INFRASTRUCTURE = "failed_infrastructure"


class SuggestedAction(str, Enum):
    REAUTHENTICATE = "reauthenticate"
    MANUAL_VERIFICATION = "manual_verification"
    CHECK_OR_REPLACE_PROXY = "check_or_replace_proxy"
    RETRY_LATER = "retry_later"
    MANUAL_REVIEW = "manual_review"


_SENSITIVE_DETAIL_KEYS = {
    "authorization",
    "authorization_header",
    "cookie",
    "cookies",
    "password",
    "proxy_password",
    "storage_state",
    "token",
}


def normalize_health_details(details: dict[str, Any]) -> dict[str, Any]:
    """Reject secrets recursively instead of relying on redaction after persistence."""

    def clean(value: Any) -> Any:
        if isinstance(value, dict):
            result: dict[str, Any] = {}
            for key, child in value.items():
                normalized = str(key).strip().lower()
                if normalized in _SENSITIVE_DETAIL_KEYS or normalized.endswith("_token"):
                    raise ValueError(f"health details must not contain secrets ({key!r})")
                result[str(key)] = clean(child)
            return result
        if isinstance(value, list):
            return [clean(child) for child in value]
        return value

    cleaned = clean(dict(details))
    try:
        json.dumps(cleaned)
    except (TypeError, ValueError) as exc:
        raise ValueError("health details must be JSON serializable") from exc
    return cleaned


def suggested_action_for(issue_type: HealthIssueType) -> SuggestedAction:
    return {
        HealthIssueType.SESSION_EXPIRED: SuggestedAction.REAUTHENTICATE,
        HealthIssueType.CHALLENGE_REQUIRED: SuggestedAction.MANUAL_VERIFICATION,
        HealthIssueType.SESSION_INVALID: SuggestedAction.REAUTHENTICATE,
        HealthIssueType.PROXY_UNAVAILABLE: SuggestedAction.CHECK_OR_REPLACE_PROXY,
        HealthIssueType.PROXY_DEGRADED: SuggestedAction.CHECK_OR_REPLACE_PROXY,
        HealthIssueType.PLATFORM_UNAVAILABLE: SuggestedAction.RETRY_LATER,
        HealthIssueType.ACCOUNT_RESTRICTED: SuggestedAction.MANUAL_REVIEW,
        HealthIssueType.UNKNOWN: SuggestedAction.MANUAL_REVIEW,
    }[issue_type]


@dataclass(frozen=True, slots=True)
class HealthCheckEvent:
    account_id: UUID
    operational_status: OperationalCheckStatus
    health_status: HealthStatus | None
    session_status: SessionStatus | None
    issue_type: HealthIssueType | None
    latency_ms: float | None
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=utc_now)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.latency_ms is not None and self.latency_ms < 0:
            raise ValueError("health event latency must be non-negative")
        object.__setattr__(self, "details", normalize_health_details(self.details))


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    account_id: UUID
    platform: SocialPlatform
    username: str
    operational_status: OperationalCheckStatus
    health_status: HealthStatus | None
    session_status: SessionStatus | None
    issue_type: HealthIssueType | None
    latency_ms: float | None
    details: dict[str, Any] = field(default_factory=dict)
    checked_at: datetime = field(default_factory=utc_now)

    @property
    def requires_attention(self) -> bool:
        return self.health_status in {HealthStatus.ACTION_REQUIRED, HealthStatus.UNAVAILABLE}

    def to_event(self) -> HealthCheckEvent:
        return HealthCheckEvent(
            account_id=self.account_id,
            operational_status=self.operational_status,
            health_status=self.health_status,
            session_status=self.session_status,
            issue_type=self.issue_type,
            latency_ms=self.latency_ms,
            details=self.details,
            checked_at=self.checked_at,
        )


@dataclass(frozen=True, slots=True)
class AccountExceptionSummary:
    account_id: UUID
    platform: SocialPlatform
    username: str
    issue_type: HealthIssueType
    health_status: HealthStatus
    session_status: SessionStatus | None
    last_check: datetime
    suggested_action: SuggestedAction


@dataclass(frozen=True, slots=True)
class HealthBatchReport:
    total_checked: int
    healthy_count: int
    degraded_count: int
    action_required_count: int
    unavailable_count: int
    skipped_count: int
    failed_count: int
    exceptions: list[AccountExceptionSummary]
    results: list[HealthCheckResult]

    @classmethod
    def from_results(cls, results: list[HealthCheckResult]) -> HealthBatchReport:
        exceptions = [
            AccountExceptionSummary(
                account_id=result.account_id,
                platform=result.platform,
                username=result.username,
                issue_type=result.issue_type or HealthIssueType.UNKNOWN,
                health_status=result.health_status,
                session_status=result.session_status,
                last_check=result.checked_at,
                suggested_action=suggested_action_for(result.issue_type or HealthIssueType.UNKNOWN),
            )
            for result in results
            if result.requires_attention and result.health_status is not None
        ]
        return cls(
            total_checked=sum(
                result.operational_status
                not in {
                    OperationalCheckStatus.SKIPPED_NOT_ONBOARDED,
                    OperationalCheckStatus.SKIPPED_INELIGIBLE,
                }
                for result in results
            ),
            healthy_count=sum(result.health_status is HealthStatus.HEALTHY for result in results),
            degraded_count=sum(result.health_status is HealthStatus.DEGRADED for result in results),
            action_required_count=sum(result.health_status is HealthStatus.ACTION_REQUIRED for result in results),
            unavailable_count=sum(result.health_status is HealthStatus.UNAVAILABLE for result in results),
            skipped_count=sum(result.operational_status.name.startswith("SKIPPED_") for result in results),
            failed_count=sum(
                result.operational_status is OperationalCheckStatus.FAILED_INFRASTRUCTURE for result in results
            ),
            exceptions=exceptions,
            results=results,
        )
