"""SQLite repository for immutable-ish, versioned editorial drafts."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    select,
    update,
)

from ..domain import SocialPlatform, utc_now
from ..persistence import SocialPodDatabase, _restore_datetime
from .models import ContentDraft, ContentDraftStatus, ContentType, GenerationSource

_metadata = MetaData()
_drafts = Table(
    "content_drafts", _metadata,
    Column("id", String(36), primary_key=True), Column("campaign_id", String(36), nullable=False, index=True),
    Column("narrative_id", String(36), nullable=False), Column("brief_id", String(36), nullable=False),
    Column("assignment_id", String(36), nullable=False, index=True), Column("account_id", String(36), nullable=False, index=True),
    Column("execution_task_id", String(36), nullable=True), Column("platform", String(32), nullable=False),
    Column("editorial_role", String(255), nullable=True), Column("content_type", String(32), nullable=False),
    Column("text", String, nullable=False), Column("language", String(32), nullable=True), Column("revision", Integer, nullable=False),
    Column("status", String(32), nullable=False, index=True), Column("generation_source", String(32), nullable=False),
    Column("generator_name", String(255), nullable=True), Column("generator_version", String(255), nullable=True),
    Column("prompt_fingerprint", String(128), nullable=True), Column("duplicate_content", Boolean, nullable=False, default=False),
    Column("created_at", DateTime(timezone=True), nullable=False), Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("approved_at", DateTime(timezone=True), nullable=True), Column("rejected_at", DateTime(timezone=True), nullable=True),
    Column("rejection_reason", String(512), nullable=True),
)
_events = Table(
    "content_draft_events", _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True), Column("draft_id", String(36), nullable=False, index=True),
    Column("event_type", String(48), nullable=False), Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("details", JSON, nullable=False),
)


class ContentDraftStore:
    def __init__(self, database: SocialPodDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        _metadata.create_all(self.database.engine)

    def save(self, draft: ContentDraft, *, event: str = "DRAFT_GENERATED") -> ContentDraft:
        self.initialize()
        with self.database.engine.begin() as connection:
            connection.execute(_drafts.insert().values(**_values(draft)))
            self._event(connection, draft.id, event)
        return draft

    def get(self, draft_id: UUID) -> ContentDraft | None:
        self.initialize()
        with self.database.engine.connect() as connection:
            row = connection.execute(select(_drafts).where(_drafts.c.id == str(draft_id))).one_or_none()
        return _from_row(row) if row else None

    def list(self, *, campaign_id: UUID | None = None, status: ContentDraftStatus | None = None) -> list[ContentDraft]:
        self.initialize()
        statement = select(_drafts).order_by(_drafts.c.created_at, _drafts.c.id)
        if campaign_id:
            statement = statement.where(_drafts.c.campaign_id == str(campaign_id))
        if status:
            statement = statement.where(_drafts.c.status == status.value)
        with self.database.engine.connect() as connection:
            rows: list[Any] = list(connection.execute(statement))
        return [_from_row(row) for row in rows]

    def next_revision(self, assignment_id: UUID) -> int:
        drafts = [item for item in self.list() if item.assignment_id == assignment_id]
        return max((item.revision for item in drafts), default=0) + 1

    def text_exists(self, campaign_id: UUID, text: str) -> bool:
        with self.database.engine.connect() as connection:
            return connection.execute(
                select(_drafts.c.id).where(_drafts.c.campaign_id == str(campaign_id)).where(_drafts.c.text == text)
            ).first() is not None

    def transition(self, draft_id: UUID, status: ContentDraftStatus, *, reason: str | None = None, event: str) -> ContentDraft:
        draft = self.required(draft_id)
        values: dict[str, Any] = {"status": status.value, "updated_at": utc_now()}
        if status is ContentDraftStatus.APPROVED:
            values["approved_at"] = utc_now()
        if status is ContentDraftStatus.REJECTED:
            values.update(rejected_at=utc_now(), rejection_reason=reason)
        with self.database.engine.begin() as connection:
            connection.execute(update(_drafts).where(_drafts.c.id == str(draft.id)).values(**values))
            self._event(connection, draft.id, event, {"reason": reason} if reason else {})
        return self.required(draft.id)

    def supersede_assignment(self, assignment_id: UUID, *, below_revision: int) -> None:
        with self.database.engine.begin() as connection:
            rows: list[str] = list(connection.execute(
                select(_drafts.c.id).where(_drafts.c.assignment_id == str(assignment_id)).where(_drafts.c.revision < below_revision)
                .where(_drafts.c.status.in_([ContentDraftStatus.GENERATED.value, ContentDraftStatus.UNDER_REVIEW.value]))
            ).scalars().all())
            if rows:
                connection.execute(update(_drafts).where(_drafts.c.id.in_(rows)).values(status=ContentDraftStatus.SUPERSEDED.value, updated_at=utc_now()))
                for row in rows:
                    self._event(connection, UUID(row), "DRAFT_SUPERSEDED")

    def link_task(self, draft_id: UUID, task_id: UUID) -> ContentDraft:
        with self.database.engine.begin() as connection:
            connection.execute(update(_drafts).where(_drafts.c.id == str(draft_id)).values(execution_task_id=str(task_id), updated_at=utc_now()))
            self._event(connection, draft_id, "DRAFT_LINKED_TO_TASK", {"execution_task_id": str(task_id)})
        return self.required(draft_id)

    def required(self, draft_id: UUID) -> ContentDraft:
        draft = self.get(draft_id)
        if draft is None:
            raise KeyError(f"Content draft not found: {draft_id}")
        return draft

    @staticmethod
    def _event(connection: Any, draft_id: UUID, event: str, details: dict[str, object] | None = None) -> None:
        connection.execute(_events.insert().values(draft_id=str(draft_id), event_type=event, occurred_at=utc_now(), details=details or {}))


def _values(draft: ContentDraft) -> dict[str, object]:
    return {
        "id": str(draft.id), "campaign_id": str(draft.campaign_id), "narrative_id": str(draft.narrative_id),
        "brief_id": str(draft.brief_id), "assignment_id": str(draft.assignment_id), "account_id": str(draft.account_id),
        "execution_task_id": str(draft.execution_task_id) if draft.execution_task_id else None, "platform": draft.platform.value,
        "editorial_role": draft.editorial_role, "content_type": draft.content_type.value, "text": draft.text, "language": draft.language,
        "revision": draft.revision, "status": draft.status.value, "generation_source": draft.generation_source.value,
        "generator_name": draft.generator_name, "generator_version": draft.generator_version, "prompt_fingerprint": draft.prompt_fingerprint,
        "duplicate_content": draft.duplicate_content, "created_at": draft.created_at, "updated_at": draft.updated_at,
        "approved_at": draft.approved_at, "rejected_at": draft.rejected_at, "rejection_reason": draft.rejection_reason,
    }


def _from_row(row: Any) -> ContentDraft:
    return ContentDraft(
        id=UUID(row.id), campaign_id=UUID(row.campaign_id), narrative_id=UUID(row.narrative_id), brief_id=UUID(row.brief_id),
        assignment_id=UUID(row.assignment_id), account_id=UUID(row.account_id), execution_task_id=UUID(row.execution_task_id) if row.execution_task_id else None,
        platform=SocialPlatform(row.platform), editorial_role=row.editorial_role, content_type=ContentType(row.content_type), text=row.text,
        language=row.language, revision=row.revision, status=ContentDraftStatus(row.status), generation_source=GenerationSource(row.generation_source),
        generator_name=row.generator_name, generator_version=row.generator_version, prompt_fingerprint=row.prompt_fingerprint,
        duplicate_content=row.duplicate_content, created_at=_restore_datetime(row.created_at), updated_at=_restore_datetime(row.updated_at),
        approved_at=_restore_datetime(row.approved_at) if row.approved_at else None, rejected_at=_restore_datetime(row.rejected_at) if row.rejected_at else None,
        rejection_reason=row.rejection_reason,
    )
