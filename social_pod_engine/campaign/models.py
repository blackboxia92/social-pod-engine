"""Persistable, non-executing campaign assignment plan models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from ..domain import utc_now
from ..narrative.domain import NarrativeRole


class AssignmentStatus(str, Enum):
    PLANNED = "planned"


@dataclass(frozen=True, slots=True)
class PlannedAssignment:
    account_id: UUID
    account_group_id: UUID
    brief_id: UUID
    narrative_id: UUID
    role: NarrativeRole
    status: AssignmentStatus = AssignmentStatus.PLANNED


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
