"""SQLite persistence owned by the Narrative Plane, never by the upstream."""

from __future__ import annotations

from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import JSON, Column, DateTime, MetaData, String, Table, inspect, select, text

from ..campaign.models import AssignmentPlan, AssignmentStatus, PlannedAssignment
from ..domain import SocialPlatform
from ..persistence import SocialPodDatabase, _restore_datetime
from .domain import (
    Campaign,
    CampaignStatus,
    ContentBrief,
    EditorialPolicy,
    Narrative,
    NarrativeRole,
)

_metadata = MetaData()
_campaigns = Table(
    "narrative_campaigns",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(255), nullable=False),
    Column("status", String(32), nullable=False),
    Column("start_at", DateTime(timezone=True), nullable=True),
    Column("end_at", DateTime(timezone=True), nullable=True),
)
_narratives = Table(
    "narratives",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("campaign_id", String(36), nullable=False, index=True),
    Column("title", String(255), nullable=False),
    Column("angle", String, nullable=False),
    Column("key_messages", JSON, nullable=False),
)
_policies = Table(
    "editorial_policies",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("campaign_id", String(36), nullable=False, index=True),
    Column("tone_of_voice", String(255), nullable=False),
    Column("forbidden_terms", JSON, nullable=False),
    Column("required_disclaimers", JSON, nullable=False),
    Column("hashtags", JSON, nullable=False),
)
_briefs = Table(
    "content_briefs",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("narrative_id", String(36), nullable=False, index=True),
    Column("role", String(64), nullable=False),
    Column("platform", String(32), nullable=False),
    Column("prompt_instructions", String, nullable=False),
    Column("target_audience", String(255), nullable=True),
)
_plans = Table(
    "assignment_plans",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("campaign_id", String(36), nullable=False, index=True),
    Column("status", String(32), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
_plan_items = Table(
    "assignment_plan_items",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("assignment_id", String(36), nullable=True, unique=True),
    Column("plan_id", String(36), nullable=False, index=True),
    Column("account_id", String(36), nullable=False),
    Column("account_group_id", String(36), nullable=False),
    Column("brief_id", String(36), nullable=False),
    Column("narrative_id", String(36), nullable=False),
    Column("role", String(64), nullable=False),
    Column("status", String(32), nullable=False),
)


class NarrativeCampaignStore:
    """Repository for pure modeling artifacts in the Social Pod SQLite database."""

    def __init__(self, database: SocialPodDatabase) -> None:
        self.database = database

    def initialize(self) -> None:
        _metadata.create_all(self.database.engine)
        columns = {column["name"] for column in inspect(self.database.engine).get_columns("assignment_plan_items")}
        if "assignment_id" not in columns:
            with self.database.engine.begin() as connection:
                connection.execute(text("ALTER TABLE assignment_plan_items ADD COLUMN assignment_id VARCHAR(36)"))

    def save_campaign(self, campaign: Campaign) -> Campaign:
        self.initialize()
        self._upsert(
            _campaigns,
            str(campaign.id),
            {
                "name": campaign.name,
                "status": campaign.status.value,
                "start_at": campaign.start_at,
                "end_at": campaign.end_at,
            },
        )
        return campaign

    def get_campaign(self, campaign_id: UUID) -> Campaign | None:
        row = self._get_row(_campaigns, campaign_id)
        return _campaign_from_row(row) if row is not None else None

    def save_narrative(self, narrative: Narrative) -> Narrative:
        self._require_campaign(narrative.campaign_id)
        self._upsert(
            _narratives,
            str(narrative.id),
            {
                "campaign_id": str(narrative.campaign_id),
                "title": narrative.title,
                "angle": narrative.angle,
                "key_messages": narrative.key_messages,
            },
        )
        return narrative

    def get_narrative(self, narrative_id: UUID) -> Narrative | None:
        row = self._get_row(_narratives, narrative_id)
        return _narrative_from_row(row) if row is not None else None

    def save_editorial_policy(self, policy: EditorialPolicy) -> EditorialPolicy:
        self._require_campaign(policy.campaign_id)
        self._upsert(
            _policies,
            str(policy.id),
            {
                "campaign_id": str(policy.campaign_id),
                "tone_of_voice": policy.tone_of_voice,
                "forbidden_terms": policy.forbidden_terms,
                "required_disclaimers": policy.required_disclaimers,
                "hashtags": policy.hashtags,
            },
        )
        return policy

    def get_editorial_policy(self, policy_id: UUID) -> EditorialPolicy | None:
        row = self._get_row(_policies, policy_id)
        return _policy_from_row(row) if row is not None else None

    def save_content_brief(self, brief: ContentBrief) -> ContentBrief:
        if self.get_narrative(brief.narrative_id) is None:
            raise KeyError(f"Narrative not found: {brief.narrative_id}")
        self._upsert(
            _briefs,
            str(brief.id),
            {
                "narrative_id": str(brief.narrative_id),
                "role": brief.role.value,
                "platform": brief.platform.value,
                "prompt_instructions": brief.prompt_instructions,
                "target_audience": brief.target_audience,
            },
        )
        return brief

    def list_briefs_for_campaign(self, campaign_id: UUID) -> list[ContentBrief]:
        with self.database.engine.connect() as connection:
            narrative_ids = cast(list[str], connection.execute(
                select(_narratives.c.id).where(_narratives.c.campaign_id == str(campaign_id))
            ).scalars().all())
            if not narrative_ids:
                return []
            rows = connection.execute(
                select(_briefs).where(_briefs.c.narrative_id.in_(narrative_ids)).order_by(_briefs.c.role, _briefs.c.id)
            )
            return [_brief_from_row(row) for row in rows]

    def save_assignment_plan(self, plan: AssignmentPlan) -> AssignmentPlan:
        self._require_campaign(plan.campaign_id)
        self.initialize()
        with self.database.engine.begin() as connection:
            connection.execute(
                _plans.insert().values(
                    id=str(plan.id),
                    campaign_id=str(plan.campaign_id),
                    status=plan.status.value,
                    created_at=plan.created_at,
                )
            )
            for index, item in enumerate(plan.assignments):
                connection.execute(
                    _plan_items.insert().values(
                        id=f"{plan.id}:{index}",
                        assignment_id=str(item.id),
                        plan_id=str(plan.id),
                        account_id=str(item.account_id),
                        account_group_id=str(item.account_group_id),
                        brief_id=str(item.brief_id),
                        narrative_id=str(item.narrative_id),
                        role=item.role.value,
                        status=item.status.value,
                    )
                )
        return plan

    def get_assignment_plan(self, plan_id: UUID) -> AssignmentPlan | None:
        row = self._get_row(_plans, plan_id)
        if row is None:
            return None
        with self.database.engine.connect() as connection:
            items = connection.execute(
                select(_plan_items).where(_plan_items.c.plan_id == str(plan_id)).order_by(_plan_items.c.id)
            )
            assignments = [_assignment_from_row(item) for item in items]
        return AssignmentPlan(
            id=UUID(str(row.id)),
            campaign_id=UUID(str(row.campaign_id)),
            status=AssignmentStatus(row.status),
            created_at=_restore_datetime(row.created_at),
            assignments=assignments,
        )

    def get_assignment(self, assignment_id: UUID) -> PlannedAssignment | None:
        """Resolve an assignment independently of its parent plan for the execution bridge."""
        self.initialize()
        with self.database.engine.connect() as connection:
            row = connection.execute(
                select(_plan_items).where(_plan_items.c.assignment_id == str(assignment_id))
            ).one_or_none()
        return _assignment_from_row(row) if row is not None else None

    def _require_campaign(self, campaign_id: UUID) -> None:
        if self.get_campaign(campaign_id) is None:
            raise KeyError(f"Campaign not found: {campaign_id}")

    def _get_row(self, table: Table, entity_id: UUID):
        self.initialize()
        with self.database.engine.connect() as connection:
            return connection.execute(select(table).where(table.c.id == str(entity_id))).one_or_none()

    def _upsert(self, table: Table, entity_id: str, values: dict[str, Any]) -> None:
        self.initialize()
        with self.database.engine.begin() as connection:
            exists = connection.execute(select(table.c.id).where(table.c.id == entity_id)).first()
            if exists is None:
                connection.execute(table.insert().values(id=entity_id, **values))
            else:
                connection.execute(table.update().where(table.c.id == entity_id).values(**values))


def _campaign_from_row(row: Any) -> Campaign:
    return Campaign(
        id=UUID(str(row.id)),
        name=row.name,
        status=CampaignStatus(row.status),
        start_at=_restore_datetime(row.start_at) if row.start_at else None,
        end_at=_restore_datetime(row.end_at) if row.end_at else None,
    )


def _narrative_from_row(row: Any) -> Narrative:
    return Narrative(
        id=UUID(str(row.id)),
        campaign_id=UUID(str(row.campaign_id)),
        title=row.title,
        angle=row.angle,
        key_messages=list(row.key_messages),
    )


def _brief_from_row(row: Any) -> ContentBrief:
    return ContentBrief(
        id=UUID(str(row.id)),
        narrative_id=UUID(str(row.narrative_id)),
        role=NarrativeRole(row.role),
        platform=SocialPlatform(row.platform),
        prompt_instructions=row.prompt_instructions,
        target_audience=row.target_audience,
    )


def _policy_from_row(row: Any) -> EditorialPolicy:
    return EditorialPolicy(
        id=UUID(str(row.id)),
        campaign_id=UUID(str(row.campaign_id)),
        tone_of_voice=row.tone_of_voice,
        forbidden_terms=list(row.forbidden_terms),
        required_disclaimers=list(row.required_disclaimers),
        hashtags=list(row.hashtags),
    )


def _assignment_from_row(row: Any) -> PlannedAssignment:
    return PlannedAssignment(
        id=UUID(str(row.assignment_id)) if row.assignment_id else uuid5(NAMESPACE_URL, str(row.id)),
        account_id=UUID(str(row.account_id)),
        account_group_id=UUID(str(row.account_group_id)),
        brief_id=UUID(str(row.brief_id)),
        narrative_id=UUID(str(row.narrative_id)),
        role=NarrativeRole(row.role),
        status=AssignmentStatus(row.status),
    )
