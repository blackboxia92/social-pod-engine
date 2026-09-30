"""Editorial content domain models; no model in this module performs network I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from ..domain import SocialPlatform, utc_now


class ContentDraftStatus(str, Enum):
    GENERATED = "generated"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ContentType(str, Enum):
    POST = "post"
    REPLY = "reply"
    COMMENT = "comment"
    CAPTION = "caption"


class GenerationSource(str, Enum):
    TEMPLATE = "template"
    LLM = "llm"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class ContentGenerationRequest:
    campaign_id: UUID
    narrative_id: UUID
    brief_id: UUID
    assignment_id: UUID
    account_id: UUID
    platform: SocialPlatform
    editorial_role: str | None
    content_type: ContentType
    requested_variants: int = 1
    source_context: dict[str, Any] = field(default_factory=dict)
    constraints: dict[str, Any] = field(default_factory=dict)
    requested_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        object.__setattr__(self, "platform", SocialPlatform(self.platform))
        object.__setattr__(self, "content_type", ContentType(self.content_type))
        if self.requested_variants < 1:
            raise ValueError("requested_variants must be at least one")
        _safe_data(self.source_context)
        _safe_data(self.constraints)


@dataclass(frozen=True, slots=True)
class GeneratedContent:
    text: str
    variant: int
    generator_name: str
    generator_version: str


@dataclass(slots=True)
class ContentDraft:
    campaign_id: UUID
    narrative_id: UUID
    brief_id: UUID
    assignment_id: UUID
    account_id: UUID
    platform: SocialPlatform
    content_type: ContentType
    text: str
    revision: int
    generation_source: GenerationSource
    editorial_role: str | None = None
    execution_task_id: UUID | None = None
    language: str | None = None
    status: ContentDraftStatus = ContentDraftStatus.GENERATED
    generator_name: str | None = None
    generator_version: str | None = None
    prompt_fingerprint: str | None = None
    duplicate_content: bool = False
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    approved_at: datetime | None = None
    rejected_at: datetime | None = None
    rejection_reason: str | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        self.platform = SocialPlatform(self.platform)
        self.content_type = ContentType(self.content_type)
        self.generation_source = GenerationSource(self.generation_source)
        self.status = ContentDraftStatus(self.status)
        self.text = self.text.strip()
        self.editorial_role = self.editorial_role.strip().lower() if self.editorial_role else None
        if not self.text or self.revision < 1:
            raise ValueError("draft text is required and revision must be positive")


@dataclass(frozen=True, slots=True)
class ContentGenerationPolicy:
    max_concurrency: int = 5
    variants_per_assignment: int = 2
    max_length: int = 280
    min_length: int = 1
    require_review: bool = True
    reject_duplicates: bool = False


@dataclass(frozen=True, slots=True)
class ContentGenerationReport:
    total_assignments: int
    total_drafts: int
    generated_count: int
    duplicate_count: int
    failed_count: int
    pending_review_count: int
    approved_count: int
    rejected_count: int
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContentReviewReport:
    total_drafts: int
    approved_count: int
    rejected_count: int
    pending_review_count: int


def _safe_data(value: dict[str, Any]) -> None:
    forbidden = {"password", "token", "secret", "cookie", "authorization", "credential"}
    for key in value:
        normalized = str(key).lower()
        if normalized in forbidden or normalized.endswith("_token"):
            raise ValueError("editorial generation data must not contain secrets")
