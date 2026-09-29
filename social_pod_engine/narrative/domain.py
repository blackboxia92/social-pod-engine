"""Storage-independent campaign, narrative and brief domain entities."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from ..domain import SocialPlatform, utc_now


class CampaignStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    ARCHIVED = "archived"


class NarrativeRole(str, Enum):
    STATS_AND_FACTS = "stats_and_facts"
    TESTIMONIAL = "testimonial"
    DEBATE_STARTER = "debate_starter"
    ANALYSIS = "analysis"
    AMPLIFIER = "amplifier"


def _normalized_strings(values: list[str], *, field_name: str) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise TypeError(f"{field_name} must contain only strings")
        value = raw.strip()
        key = value.casefold()
        if value and key not in seen:
            normalized.append(value)
            seen.add(key)
    return normalized


def _optional_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("campaign timestamps must be timezone-aware")
    return value.astimezone(utc_now().tzinfo)


@dataclass(slots=True)
class Campaign:
    name: str
    status: CampaignStatus = CampaignStatus.DRAFT
    start_at: datetime | None = None
    end_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Campaign.name is required")
        self.status = CampaignStatus(self.status)
        self.start_at = _optional_datetime(self.start_at)
        self.end_at = _optional_datetime(self.end_at)
        if self.start_at and self.end_at and self.end_at < self.start_at:
            raise ValueError("Campaign.end_at cannot precede start_at")


@dataclass(slots=True)
class Narrative:
    campaign_id: UUID
    title: str
    angle: str
    key_messages: list[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        self.title = self.title.strip()
        self.angle = self.angle.strip()
        if not self.title or not self.angle:
            raise ValueError("Narrative.title and Narrative.angle are required")
        self.key_messages = _normalized_strings(self.key_messages, field_name="key_messages")


@dataclass(slots=True)
class EditorialPolicy:
    campaign_id: UUID
    tone_of_voice: str
    forbidden_terms: list[str] = field(default_factory=list)
    required_disclaimers: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        self.tone_of_voice = self.tone_of_voice.strip()
        if not self.tone_of_voice:
            raise ValueError("EditorialPolicy.tone_of_voice is required")
        self.forbidden_terms = _normalized_strings(self.forbidden_terms, field_name="forbidden_terms")
        self.required_disclaimers = _normalized_strings(
            self.required_disclaimers, field_name="required_disclaimers"
        )
        self.hashtags = _normalized_strings(self.hashtags, field_name="hashtags")


@dataclass(slots=True)
class ContentBrief:
    narrative_id: UUID
    role: NarrativeRole
    platform: SocialPlatform
    prompt_instructions: str
    target_audience: str | None = None
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        self.role = NarrativeRole(self.role)
        self.platform = SocialPlatform(self.platform)
        self.prompt_instructions = self.prompt_instructions.strip()
        if not self.prompt_instructions:
            raise ValueError("ContentBrief.prompt_instructions is required")
        if self.target_audience is not None:
            self.target_audience = self.target_audience.strip() or None
