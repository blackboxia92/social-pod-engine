"""Private SQLAlchemy persistence for Social Pod, separate from CPM storage."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, DateTime, ForeignKey, String, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .domain import (
    AccountGroup,
    HealthStatus,
    LifecycleStatus,
    Persona,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
)


class SocialPodORMBase(DeclarativeBase):
    """Persistence-only base. It is never shared with the upstream ORM."""


class PersonaSchema(SocialPodORMBase):
    __tablename__ = "personas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AccountGroupSchema(SocialPodORMBase):
    __tablename__ = "account_groups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)
    metadata_payload: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SocialAccountSchema(SocialPodORMBase):
    __tablename__ = "social_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    persona_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("personas.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    upstream_profile_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    proxy_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_id_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    editorial_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False)
    session_status: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    quota_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


def default_database_path() -> Path:
    return Path(__file__).parent / "data" / "social_pod.sqlite3"


class SocialPodDatabase:
    """Repository facade for the Social Pod database only.

    ``upstream_profile_id`` is stored as an opaque external reference; no CPM
    table is opened, joined, migrated or given a cross-database foreign key.
    """

    def __init__(self, database_path: Path | str | None = None) -> None:
        self.database_path = Path(database_path or default_database_path())
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(f"sqlite+pysqlite:///{self.database_path}", future=True)
        event.listen(self.engine, "connect", _enable_sqlite_foreign_keys)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        SocialPodORMBase.metadata.create_all(self.engine)

    def close(self) -> None:
        self.engine.dispose()

    @contextmanager
    def _session(self) -> Iterator[Session]:
        with self._sessions.begin() as session:
            yield session

    def save_persona(self, persona: Persona) -> Persona:
        with self._session() as session:
            row = session.get(PersonaSchema, str(persona.id))
            if row is None:
                row = PersonaSchema(id=str(persona.id))
                session.add(row)
            row.alias = persona.alias
            row.metadata_payload = persona.metadata
            row.created_at = persona.created_at
        return persona

    def get_persona(self, persona_id: UUID) -> Persona | None:
        with self._session() as session:
            row = session.get(PersonaSchema, str(persona_id))
            return _persona_from_schema(row) if row else None

    def save_account_group(self, group: AccountGroup) -> AccountGroup:
        with self._session() as session:
            row = session.get(AccountGroupSchema, str(group.id))
            if row is None:
                row = AccountGroupSchema(id=str(group.id))
                session.add(row)
            row.alias = group.alias
            row.metadata_payload = group.metadata
            row.created_at = group.created_at
        return group

    def get_account_group(self, group_id: UUID) -> AccountGroup | None:
        with self._session() as session:
            row = session.get(AccountGroupSchema, str(group_id))
            return _group_from_schema(row) if row else None

    def save_social_account(self, account: SocialAccount) -> SocialAccount:
        with self._session() as session:
            row = session.get(SocialAccountSchema, str(account.id))
            if row is None:
                row = SocialAccountSchema(id=str(account.id))
                session.add(row)
            _apply_account_to_schema(account, row)
        return account

    def get_social_account(self, account_id: UUID) -> SocialAccount | None:
        with self._session() as session:
            row = session.get(SocialAccountSchema, str(account_id))
            return _account_from_schema(row) if row else None

    def list_social_accounts(self, persona_id: UUID) -> list[SocialAccount]:
        with self._session() as session:
            rows = session.scalars(
                select(SocialAccountSchema)
                .where(SocialAccountSchema.persona_id == str(persona_id))
                .order_by(SocialAccountSchema.created_at)
            ).all()
            return [_account_from_schema(row) for row in rows]


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _persona_from_schema(row: PersonaSchema) -> Persona:
    return Persona(
        id=UUID(row.id),
        alias=row.alias,
        metadata=dict(row.metadata_payload),
        created_at=_restore_datetime(row.created_at),
    )


def _group_from_schema(row: AccountGroupSchema) -> AccountGroup:
    return AccountGroup(
        id=UUID(row.id),
        alias=row.alias,
        metadata=dict(row.metadata_payload),
        created_at=_restore_datetime(row.created_at),
    )


def _apply_account_to_schema(account: SocialAccount, row: SocialAccountSchema) -> None:
    row.persona_id = str(account.persona_id)
    row.platform = account.platform.value
    row.username = account.username
    row.upstream_profile_id = account.upstream_profile_id
    row.proxy_id = account.proxy_id
    row.group_id = str(account.group_id) if account.group_id is not None else None
    row.group_id_kind = "uuid" if isinstance(account.group_id, UUID) else "str" if account.group_id else None
    row.tags = list(account.tags)
    row.editorial_role = account.editorial_role
    row.health_status = account.health_status.value
    row.session_status = account.session_status.value
    row.lifecycle_status = account.lifecycle_status.value
    row.quota_status = account.quota_status.value
    row.created_at = account.created_at
    row.updated_at = account.updated_at


def _account_from_schema(row: SocialAccountSchema) -> SocialAccount:
    group_id: UUID | str | None = row.group_id
    if row.group_id and row.group_id_kind == "uuid":
        group_id = UUID(row.group_id)
    return SocialAccount(
        id=UUID(row.id),
        persona_id=UUID(row.persona_id),
        platform=SocialPlatform(row.platform),
        username=row.username,
        upstream_profile_id=row.upstream_profile_id,
        proxy_id=row.proxy_id,
        group_id=group_id,
        tags=list(row.tags),
        editorial_role=row.editorial_role,
        health_status=HealthStatus(row.health_status),
        session_status=SessionStatus(row.session_status),
        lifecycle_status=LifecycleStatus(row.lifecycle_status),
        quota_status=QuotaStatus(row.quota_status),
        created_at=_restore_datetime(row.created_at),
        updated_at=_restore_datetime(row.updated_at),
    )


def _restore_datetime(value: datetime) -> datetime:
    """SQLite does not round-trip tzinfo; stored values are always normalized to UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
