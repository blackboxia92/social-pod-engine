"""Storage-independent domain entities for the Social Pod control plane."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from .models import StringEnum


class SocialPlatform(StringEnum):
    X = "x"
    INSTAGRAM = "instagram"
    FACEBOOK = "facebook"


class HealthStatus(StringEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    ACTION_REQUIRED = "action_required"
    UNAVAILABLE = "unavailable"


class SessionStatus(StringEnum):
    VALID = "valid"
    EXPIRED = "expired"
    CHALLENGE_REQUIRED = "challenge_required"
    INVALID = "invalid"
    UNKNOWN = "unknown"


class LifecycleStatus(StringEnum):
    PENDING_SETUP = "pending_setup"
    WARMUP = "warmup"
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"
    ARCHIVED = "archived"


class QuotaStatus(StringEnum):
    AVAILABLE = "available"
    EXHAUSTED = "exhausted"


class HealthIssueType(StringEnum):
    SESSION_EXPIRED = "session_expired"
    CHALLENGE_REQUIRED = "challenge_required"
    SESSION_INVALID = "session_invalid"
    PROXY_UNAVAILABLE = "proxy_unavailable"
    PROXY_DEGRADED = "proxy_degraded"
    PLATFORM_UNAVAILABLE = "platform_unavailable"
    ACCOUNT_RESTRICTED = "account_restricted"
    UNKNOWN = "unknown"


_SENSITIVE_METADATA_KEYS = {
    "api_key",
    "credential",
    "credentials",
    "password",
    "private_key",
    "secret",
    "token",
}
_EDITORIAL_ROLE_SEPARATOR = re.compile(r"[^a-z0-9_]+")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def normalize_tags(tags: list[str]) -> list[str]:
    """Return portable labels with whitespace/case duplicates removed in order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        if not isinstance(tag, str):
            raise TypeError("tags must contain only strings")
        value = tag.strip().lower()
        if value and value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def normalize_editorial_role(role: str | None) -> str | None:
    """Normalize a free-form role without restricting the editorial vocabulary."""
    if role is None:
        return None
    if not isinstance(role, str):
        raise TypeError("editorial_role must be a string or None")
    value = _EDITORIAL_ROLE_SEPARATOR.sub("-", role.strip().lower()).strip("-")
    return value or None


def normalize_account_username(value: str) -> str:
    """Canonical account identifier used by the first supported platforms."""
    if not isinstance(value, str):
        raise TypeError("username must be a string")
    normalized = value.strip().lstrip("@").replace(" ", "").lower()
    if not normalized:
        raise ValueError("SocialAccount.username is required")
    return normalized


def normalize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Copy JSON-safe contextual metadata while rejecting obvious secret fields."""
    copied = dict(metadata)
    for key in copied:
        normalized_key = str(key).strip().lower()
        if normalized_key in _SENSITIVE_METADATA_KEYS or normalized_key.endswith("_token"):
            raise ValueError(f"metadata must not contain secrets ({key!r})")
    try:
        json.dumps(copied)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON serializable") from exc
    return copied


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(slots=True)
class Persona:
    id: UUID = field(default_factory=uuid4)
    alias: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.alias = self.alias.strip()
        if not self.alias:
            raise ValueError("Persona.alias is required")
        self.metadata = normalize_metadata(self.metadata)
        self.created_at = _normalize_datetime(self.created_at)


@dataclass(slots=True)
class AccountGroup:
    """Minimal future grouping anchor; SocialAccount keeps a soft reference."""

    id: UUID = field(default_factory=uuid4)
    alias: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.alias = self.alias.strip()
        if not self.alias:
            raise ValueError("AccountGroup.alias is required")
        self.metadata = normalize_metadata(self.metadata)
        self.created_at = _normalize_datetime(self.created_at)


@dataclass(slots=True)
class SocialAccount:
    id: UUID = field(default_factory=uuid4)
    persona_id: UUID = field(default_factory=uuid4)
    platform: SocialPlatform = SocialPlatform.X
    username: str = ""
    upstream_profile_id: str | None = None
    proxy_id: str | None = None
    group_id: UUID | str | None = None
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    editorial_role: str | None = None
    health_status: HealthStatus = HealthStatus.UNAVAILABLE
    session_status: SessionStatus = SessionStatus.UNKNOWN
    lifecycle_status: LifecycleStatus = LifecycleStatus.PENDING_SETUP
    quota_status: QuotaStatus = QuotaStatus.AVAILABLE
    quarantined: bool = False
    quarantine_reason: str | None = None
    quarantine_issue_type: HealthIssueType | None = None
    quarantined_at: datetime | None = None
    healthy_check_streak: int = 0
    last_healthcheck_at: datetime | None = None
    next_healthcheck_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        self.platform = SocialPlatform(self.platform)
        self.username = normalize_account_username(self.username)
        self.upstream_profile_id = _normalize_optional_text(self.upstream_profile_id)
        self.proxy_id = _normalize_optional_text(self.proxy_id)
        self.group_id = _normalize_group_id(self.group_id)
        self.tags = normalize_tags(self.tags)
        self.metadata = normalize_metadata(self.metadata)
        self.editorial_role = normalize_editorial_role(self.editorial_role)
        self.health_status = HealthStatus(self.health_status)
        self.session_status = SessionStatus(self.session_status)
        self.lifecycle_status = LifecycleStatus(self.lifecycle_status)
        self.quota_status = QuotaStatus(self.quota_status)
        self.quarantined = bool(self.quarantined)
        self.quarantine_reason = _normalize_optional_text(self.quarantine_reason)
        self.quarantine_issue_type = (
            HealthIssueType(self.quarantine_issue_type) if self.quarantine_issue_type is not None else None
        )
        self.quarantined_at = _normalize_optional_datetime(self.quarantined_at)
        if self.healthy_check_streak < 0:
            raise ValueError("healthy_check_streak must be non-negative")
        self.last_healthcheck_at = _normalize_optional_datetime(self.last_healthcheck_at)
        self.next_healthcheck_at = _normalize_optional_datetime(self.next_healthcheck_at)
        if not self.quarantined:
            self.quarantine_reason = None
            self.quarantine_issue_type = None
            self.quarantined_at = None
            self.healthy_check_streak = 0
        self.created_at = _normalize_datetime(self.created_at)
        self.updated_at = _normalize_datetime(self.updated_at)


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("external identifiers must be strings or None")
    return value.strip() or None


def _normalize_group_id(value: UUID | str | None) -> UUID | str | None:
    if value is None or isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise TypeError("group_id must be UUID, string, or None")
    return value.strip() or None


def _normalize_optional_datetime(value: datetime | None) -> datetime | None:
    return _normalize_datetime(value) if value is not None else None
