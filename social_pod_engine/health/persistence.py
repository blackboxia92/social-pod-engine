"""Health history repository backed only by the Social Pod SQLite database."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Column, DateTime, Float, MetaData, String, Table, select

from ..domain import HealthIssueType, HealthStatus, SessionStatus, SocialAccount
from ..persistence import SocialPodDatabase, _restore_datetime
from .models import HealthCheckEvent, OperationalCheckStatus

_metadata = MetaData()
_events = Table(
    "health_check_events",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("account_id", String(36), nullable=False, index=True),
    Column("checked_at", DateTime(timezone=True), nullable=False, index=True),
    Column("operational_status", String(64), nullable=False),
    Column("health_status", String(32), nullable=True),
    Column("session_status", String(32), nullable=True),
    Column("issue_type", String(64), nullable=True),
    Column("latency_ms", Float, nullable=True),
    Column("details", JSON, nullable=False),
)


class HealthRepository:
    """Repository façade: engines use methods, never SQL ad hoc."""

    def __init__(self, database: SocialPodDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        _metadata.create_all(self.database.engine)

    def list_candidates(self, group_id: UUID | str | None = None) -> list[SocialAccount]:
        return self.database.list_healthcheck_candidates(group_id)

    def list_due(self, now, *, limit: int) -> list[SocialAccount]:
        return self.database.list_due_healthchecks(now, limit=limit)

    def save_event(self, event: HealthCheckEvent) -> HealthCheckEvent:
        self.initialize()
        with self.database.engine.begin() as connection:
            connection.execute(
                _events.insert().values(
                    id=str(event.id),
                    account_id=str(event.account_id),
                    checked_at=event.checked_at,
                    operational_status=event.operational_status.value,
                    health_status=event.health_status.value if event.health_status else None,
                    session_status=event.session_status.value if event.session_status else None,
                    issue_type=event.issue_type.value if event.issue_type else None,
                    latency_ms=event.latency_ms,
                    details=event.details,
                )
            )
        return event

    def list_events(self, account_id: UUID) -> list[HealthCheckEvent]:
        self.initialize()
        with self.database.engine.connect() as connection:
            rows = connection.execute(
                select(_events).where(_events.c.account_id == str(account_id)).order_by(_events.c.checked_at)
            )
            return [_event_from_row(row) for row in rows]


def _event_from_row(row: Any) -> HealthCheckEvent:
    return HealthCheckEvent(
        id=UUID(str(row.id)),
        account_id=UUID(str(row.account_id)),
        checked_at=_restore_datetime(row.checked_at),
        operational_status=OperationalCheckStatus(row.operational_status),
        health_status=HealthStatus(row.health_status) if row.health_status else None,
        session_status=SessionStatus(row.session_status) if row.session_status else None,
        issue_type=HealthIssueType(row.issue_type) if row.issue_type else None,
        latency_ms=row.latency_ms,
        details=dict(row.details),
    )
