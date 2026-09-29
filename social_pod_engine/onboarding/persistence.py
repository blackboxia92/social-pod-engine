"""Private persistence for the onboarding queue, isolated from upstream storage."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, select
from sqlalchemy.orm import Session

from ..domain import LifecycleStatus, SessionStatus, SocialAccount, normalize_tags, utc_now
from ..persistence import SocialAccountSchema, SocialPodDatabase, _account_from_schema
from .models import OnboardingQueueState

_metadata = MetaData()
_queue_table = Table(
    "social_pod_onboarding_queues",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("state", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)


class OnboardingQueueStore:
    """Owns only Social Pod's queue table and reads the isolated account table."""

    def __init__(self, database: SocialPodDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        _metadata.create_all(self.database.engine)

    def save(self, state: OnboardingQueueState) -> None:
        values = {"state": state.to_payload(), "updated_at": utc_now()}
        with self.database.engine.begin() as connection:
            existing = connection.execute(
                select(_queue_table.c.id).where(_queue_table.c.id == str(state.id))
            ).first()
            if existing is None:
                connection.execute(_queue_table.insert().values(id=str(state.id), **values))
            else:
                connection.execute(
                    _queue_table.update().where(_queue_table.c.id == str(state.id)).values(**values)
                )

    def get(self, queue_id: UUID) -> OnboardingQueueState | None:
        self.initialize()
        with self.database.engine.connect() as connection:
            row = connection.execute(
                select(_queue_table.c.id, _queue_table.c.state).where(_queue_table.c.id == str(queue_id))
            ).one_or_none()
        return _queue_from_row(row) if row is not None else None

    def latest_resumable(self) -> OnboardingQueueState | None:
        self.initialize()
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(_queue_table.c.id, _queue_table.c.state).order_by(_queue_table.c.updated_at.desc())
            )
            for row in rows:
                state = _queue_from_row(row)
                if state.current_index < state.total_accounts and state.status.value != "completed":
                    return state
        return None

    def find_candidates(
        self,
        *,
        group_id: UUID | str | None = None,
        filter_tags: Sequence[str] | None = None,
        include_non_valid_sessions: bool = False,
    ) -> list[SocialAccount]:
        expected_group_id = str(group_id) if group_id is not None else None
        expected_tags = set(normalize_tags(list(filter_tags or [])))
        with Session(self.database.engine) as session:
            rows = session.scalars(
                select(SocialAccountSchema).order_by(SocialAccountSchema.created_at, SocialAccountSchema.id)
            ).all()
        accounts = [_account_from_schema(row) for row in rows]
        return [
            account
            for account in accounts
            if self._is_candidate(
                account,
                group_id=expected_group_id,
                filter_tags=expected_tags,
                include_non_valid_sessions=include_non_valid_sessions,
            )
        ]

    @staticmethod
    def _is_candidate(
        account: SocialAccount,
        *,
        group_id: str | None,
        filter_tags: set[str],
        include_non_valid_sessions: bool,
    ) -> bool:
        needs_setup = account.lifecycle_status is LifecycleStatus.PENDING_SETUP
        needs_session = include_non_valid_sessions and account.session_status is not SessionStatus.VALID
        if not (needs_setup or needs_session):
            return False
        if group_id is not None and str(account.group_id) != group_id:
            return False
        return not filter_tags or filter_tags.issubset(set(account.tags))


def _queue_from_row(row: Any) -> OnboardingQueueState:
    queue_id = UUID(str(row.id))
    payload = dict(row.state)
    return OnboardingQueueState.from_payload(queue_id, payload)
