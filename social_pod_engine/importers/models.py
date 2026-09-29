"""Public, row-level results for non-aborting bulk imports."""

from dataclasses import dataclass, field
from enum import Enum
from uuid import UUID

from ..domain import SocialPlatform


class ImportRowStatus(str, Enum):
    WOULD_CREATE = "would_create"
    CREATED = "created"
    DUPLICATE = "duplicate"
    INVALID = "invalid"
    # Future-compatible extension points: UPDATED and WOULD_UPDATE.


@dataclass(frozen=True, slots=True)
class ImportRowResult:
    row_number: int
    platform: SocialPlatform | None
    username: str | None
    status: ImportRowStatus
    persona_id: UUID | str | None = None
    group_id: UUID | str | None = None
    account_id: UUID | str | None = None
    reason: str | None = None


@dataclass(slots=True)
class ImportResult:
    total_rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    created_personas: int = 0
    created_groups: int = 0
    created_accounts: int = 0
    skipped_duplicates: int = 0
    rows: list[ImportRowResult] = field(default_factory=list)

    def add_row(self, row: ImportRowResult) -> None:
        self.rows.append(row)
        self.total_rows += 1
        if row.status in {ImportRowStatus.CREATED, ImportRowStatus.WOULD_CREATE}:
            self.valid_rows += 1
        elif row.status is ImportRowStatus.INVALID:
            self.invalid_rows += 1
        elif row.status is ImportRowStatus.DUPLICATE:
            self.skipped_duplicates += 1
