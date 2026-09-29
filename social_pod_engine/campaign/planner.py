"""Deterministic dry-run assignment of narrative briefs to eligible accounts."""

from __future__ import annotations

from uuid import UUID

from ..domain import (
    HealthStatus,
    LifecycleStatus,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    normalize_tags,
)
from ..narrative.domain import ContentBrief, NarrativeRole
from ..narrative.persistence import NarrativeCampaignStore
from ..persistence import SocialPodDatabase
from .models import AssignmentPlan, PlannedAssignment


class CampaignPlannerService:
    """Models assignments only; it intentionally has no adapter or browser dependency."""

    def __init__(self, database: SocialPodDatabase, campaigns: NarrativeCampaignStore) -> None:
        self.database = database
        self.campaigns = campaigns

    def assign_briefs_to_accounts(
        self, campaign_id: UUID, account_group_ids: list[UUID]
    ) -> AssignmentPlan:
        if self.campaigns.get_campaign(campaign_id) is None:
            raise KeyError(f"Campaign not found: {campaign_id}")
        briefs = self.campaigns.list_briefs_for_campaign(campaign_id)
        if not briefs:
            return self._save_plan(campaign_id, [])

        assignments: list[PlannedAssignment] = []
        for group_id in account_group_ids:
            accounts = self.eligible_accounts_for_group(group_id)
            assignments.extend(self._assign_group(group_id, accounts, briefs))
        return self._save_plan(campaign_id, assignments)

    def eligible_accounts_for_group(
        self, group_id: UUID, *, filter_tags: list[str] | None = None
    ) -> list[SocialAccount]:
        expected_tags = set(normalize_tags(filter_tags or []))
        return [
            account
            for account in self.database.list_healthcheck_candidates(group_id)
            if account.lifecycle_status in {LifecycleStatus.WARMUP, LifecycleStatus.ACTIVE}
            and account.health_status is HealthStatus.HEALTHY
            and account.session_status is SessionStatus.VALID
            and not account.quarantined
            and account.quota_status is not QuotaStatus.EXHAUSTED
            and (not expected_tags or expected_tags.issubset(set(account.tags)))
        ]

    @staticmethod
    def _assign_group(
        group_id: UUID, accounts: list[SocialAccount], briefs: list[ContentBrief]
    ) -> list[PlannedAssignment]:
        by_platform_role: dict[object, dict[NarrativeRole, list[ContentBrief]]] = {}
        for brief in briefs:
            by_platform_role.setdefault(brief.platform, {}).setdefault(brief.role, []).append(brief)
        role_offsets: dict[tuple[object, NarrativeRole], int] = {}
        role_counts: dict[tuple[object, NarrativeRole], int] = {}
        assignments: list[PlannedAssignment] = []
        for account in accounts:
            roles = sorted(by_platform_role.get(account.platform, {}), key=lambda role: role.value)
            if not roles:
                continue
            role = min(roles, key=lambda item: (role_counts.get((account.platform, item), 0), item.value))
            role_briefs = by_platform_role[account.platform][role]
            key = (account.platform, role)
            brief = role_briefs[role_offsets.get(key, 0) % len(role_briefs)]
            role_offsets[key] = role_offsets.get(key, 0) + 1
            role_counts[key] = role_counts.get(key, 0) + 1
            assignments.append(
                PlannedAssignment(
                    account_id=account.id,
                    account_group_id=group_id,
                    brief_id=brief.id,
                    narrative_id=brief.narrative_id,
                    role=role,
                )
            )
        return assignments

    def _save_plan(self, campaign_id: UUID, assignments: list[PlannedAssignment]) -> AssignmentPlan:
        plan = AssignmentPlan(campaign_id=campaign_id, assignments=assignments)
        self.campaigns.save_assignment_plan(plan)
        return plan
