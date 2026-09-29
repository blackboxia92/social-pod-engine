"""Private SQLAlchemy persistence for Social Pod, separate from CPM storage."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    String,
    create_engine,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .domain import (
    AccountGroup,
    HealthIssueType,
    HealthStatus,
    LifecycleStatus,
    Persona,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
    normalize_account_username,
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
    metadata_payload: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False)
    editorial_role: Mapped[str | None] = mapped_column(String(255), nullable=True)
    health_status: Mapped[str] = mapped_column(String(32), nullable=False)
    session_status: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    quota_status: Mapped[str] = mapped_column(String(32), nullable=False)
    quarantined: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    quarantine_reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    quarantine_issue_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    quarantined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    healthy_check_streak: Mapped[int] = mapped_column(nullable=False, default=0)
    last_healthcheck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_healthcheck_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
        self._migrate_social_account_metadata()
        self._migrate_social_account_health()

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

    def find_persona_by_alias(self, alias: str) -> Persona | None:
        with self._session() as session:
            row = session.scalar(select(PersonaSchema).where(PersonaSchema.alias == alias.strip()))
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

    def find_group_by_alias(self, alias: str) -> AccountGroup | None:
        with self._session() as session:
            row = session.scalar(select(AccountGroupSchema).where(AccountGroupSchema.alias == alias.strip()))
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

    def find_account_by_platform_username(
        self, platform: SocialPlatform, username: str
    ) -> SocialAccount | None:
        """Find by the control-plane identity key, not an upstream profile id."""
        normalized = normalize_account_username(username)
        with self._session() as session:
            rows = session.scalars(
                select(SocialAccountSchema).where(SocialAccountSchema.platform == platform.value)
            ).all()
            for row in rows:
                if normalize_account_username(row.username) == normalized:
                    return _account_from_schema(row)
        return None

    def list_social_accounts(self, persona_id: UUID) -> list[SocialAccount]:
        with self._session() as session:
            rows = session.scalars(
                select(SocialAccountSchema)
                .where(SocialAccountSchema.persona_id == str(persona_id))
                .order_by(SocialAccountSchema.created_at)
            ).all()
            return [_account_from_schema(row) for row in rows]

    def list_healthcheck_candidates(self, group_id: UUID | str | None = None) -> list[SocialAccount]:
        with self._session() as session:
            statement = select(SocialAccountSchema).order_by(SocialAccountSchema.created_at, SocialAccountSchema.id)
            if group_id is not None:
                statement = statement.where(SocialAccountSchema.group_id == str(group_id))
            rows = session.scalars(statement).all()
            return [_account_from_schema(row) for row in rows]

    def list_due_healthchecks(self, now: datetime, *, limit: int) -> list[SocialAccount]:
        if limit < 1:
            return []
        with self._session() as session:
            rows = session.scalars(
                select(SocialAccountSchema)
                .where(
                    (SocialAccountSchema.next_healthcheck_at.is_(None))
                    | (SocialAccountSchema.next_healthcheck_at <= now)
                )
                .order_by(SocialAccountSchema.next_healthcheck_at, SocialAccountSchema.created_at)
                .limit(limit)
            ).all()
            return [_account_from_schema(row) for row in rows]

    def _migrate_social_account_metadata(self) -> None:
        """Add Phase 2 account metadata without affecting any upstream schema."""
        columns = {column["name"] for column in inspect(self.engine).get_columns("social_accounts")}
        if "metadata" not in columns:
            with self.engine.begin() as connection:
                connection.execute(
                    text("ALTER TABLE social_accounts ADD COLUMN metadata JSON NOT NULL DEFAULT '{}'"),
                )

    def _migrate_social_account_health(self) -> None:
        """Add Phase 4 health fields inside the Social Pod database only."""
        columns = {column["name"] for column in inspect(self.engine).get_columns("social_accounts")}
        migrations = {
            "quarantined": "ALTER TABLE social_accounts ADD COLUMN quarantined BOOLEAN NOT NULL DEFAULT 0",
            "quarantine_reason": "ALTER TABLE social_accounts ADD COLUMN quarantine_reason VARCHAR(512)",
            "quarantine_issue_type": "ALTER TABLE social_accounts ADD COLUMN quarantine_issue_type VARCHAR(64)",
            "quarantined_at": "ALTER TABLE social_accounts ADD COLUMN quarantined_at DATETIME",
            "healthy_check_streak": "ALTER TABLE social_accounts ADD COLUMN healthy_check_streak INTEGER NOT NULL DEFAULT 0",
            "last_healthcheck_at": "ALTER TABLE social_accounts ADD COLUMN last_healthcheck_at DATETIME",
            "next_healthcheck_at": "ALTER TABLE social_accounts ADD COLUMN next_healthcheck_at DATETIME",
        }
        with self.engine.begin() as connection:
            for name, statement in migrations.items():
                if name not in columns:
                    connection.execute(text(statement))


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
    row.metadata_payload = dict(account.metadata)
    row.editorial_role = account.editorial_role
    row.health_status = account.health_status.value
    row.session_status = account.session_status.value
    row.lifecycle_status = account.lifecycle_status.value
    row.quota_status = account.quota_status.value
    row.quarantined = account.quarantined
    row.quarantine_reason = account.quarantine_reason
    row.quarantine_issue_type = (
        account.quarantine_issue_type.value if account.quarantine_issue_type is not None else None
    )
    row.quarantined_at = account.quarantined_at
    row.healthy_check_streak = account.healthy_check_streak
    row.last_healthcheck_at = account.last_healthcheck_at
    row.next_healthcheck_at = account.next_healthcheck_at
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
        metadata=dict(row.metadata_payload),
        editorial_role=row.editorial_role,
        health_status=HealthStatus(row.health_status),
        session_status=SessionStatus(row.session_status),
        lifecycle_status=LifecycleStatus(row.lifecycle_status),
        quota_status=QuotaStatus(row.quota_status),
        quarantined=row.quarantined,
        quarantine_reason=row.quarantine_reason,
        quarantine_issue_type=(
            HealthIssueType(row.quarantine_issue_type) if row.quarantine_issue_type is not None else None
        ),
        quarantined_at=_restore_datetime(row.quarantined_at) if row.quarantined_at else None,
        healthy_check_streak=row.healthy_check_streak,
        last_healthcheck_at=_restore_datetime(row.last_healthcheck_at) if row.last_healthcheck_at else None,
        next_healthcheck_at=_restore_datetime(row.next_healthcheck_at) if row.next_healthcheck_at else None,
        created_at=_restore_datetime(row.created_at),
        updated_at=_restore_datetime(row.updated_at),
    )


def _restore_datetime(value: datetime) -> datetime:
    """SQLite does not round-trip tzinfo; stored values are always normalized to UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)
