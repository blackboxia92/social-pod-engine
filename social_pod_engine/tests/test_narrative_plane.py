from __future__ import annotations

from collections import Counter

import pytest

from social_pod_engine.campaign import AssignmentStatus, CampaignPlannerService
from social_pod_engine.domain import (
    AccountGroup,
    HealthStatus,
    LifecycleStatus,
    Persona,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
)
from social_pod_engine.narrative import (
    Campaign,
    CampaignStatus,
    ContentBrief,
    EditorialPolicy,
    Narrative,
    NarrativeCampaignStore,
    NarrativeRole,
)
from social_pod_engine.persistence import SocialPodDatabase


@pytest.fixture
def database(tmp_path):
    database = SocialPodDatabase(tmp_path / "social-pod.sqlite3")
    database.initialize()
    yield database
    database.close()


def campaign_with_briefs(database: SocialPodDatabase):
    store = NarrativeCampaignStore(database)
    campaign = Campaign(name="Mercado de pases", status=CampaignStatus.DRAFT)
    narrative = Narrative(
        campaign_id=campaign.id,
        title="Narrativa A: Datos cuantitativos",
        angle="El mercado se explica con datos verificables.",
        key_messages=["Datos públicos", "Datos públicos", "Contexto"],
    )
    policy = EditorialPolicy(
        campaign_id=campaign.id,
        tone_of_voice="Informativo y sobrio",
        forbidden_terms=["rumor", "RUMOR"],
        required_disclaimers=["Información pública"],
        hashtags=["#Mercado", "#mercado"],
    )
    briefs = [
        ContentBrief(
            narrative_id=narrative.id,
            role=role,
            platform=SocialPlatform.X,
            prompt_instructions=f"Desarrollar el ángulo para {role.value}",
            target_audience="Aficionados informados",
        )
        for role in (
            NarrativeRole.STATS_AND_FACTS,
            NarrativeRole.TESTIMONIAL,
            NarrativeRole.DEBATE_STARTER,
        )
    ]
    store.save_campaign(campaign)
    store.save_narrative(narrative)
    store.save_editorial_policy(policy)
    for brief in briefs:
        store.save_content_brief(brief)
    return store, campaign, narrative, policy, briefs


def add_account(
    database: SocialPodDatabase,
    group: AccountGroup,
    username: str,
    *,
    health_status: HealthStatus = HealthStatus.HEALTHY,
    session_status: SessionStatus = SessionStatus.VALID,
    lifecycle_status: LifecycleStatus = LifecycleStatus.WARMUP,
    quota_status: QuotaStatus = QuotaStatus.AVAILABLE,
    quarantined: bool = False,
    tags: list[str] | None = None,
) -> SocialAccount:
    persona = Persona(alias=f"persona-{username}")
    account = SocialAccount(
        persona_id=persona.id,
        platform=SocialPlatform.X,
        username=username,
        group_id=group.id,
        health_status=health_status,
        session_status=session_status,
        lifecycle_status=lifecycle_status,
        quota_status=quota_status,
        quarantined=quarantined,
        tags=tags or [],
    )
    database.save_persona(persona)
    database.save_social_account(account)
    return account


def test_campaign_narrative_policy_and_briefs_persist_cleanly(database):
    store, campaign, narrative, policy, briefs = campaign_with_briefs(database)

    restored_campaign = store.get_campaign(campaign.id)
    restored_narrative = store.get_narrative(narrative.id)
    restored_policy = store.get_editorial_policy(policy.id)
    restored_briefs = store.list_briefs_for_campaign(campaign.id)

    assert restored_campaign == campaign
    assert restored_narrative is not None
    assert restored_narrative.key_messages == ["Datos públicos", "Contexto"]
    assert restored_policy is not None
    assert restored_policy.forbidden_terms == ["rumor"]
    assert restored_policy.hashtags == ["#Mercado"]
    assert {brief.role for brief in restored_briefs} == {brief.role for brief in briefs}


def test_planner_excludes_unhealthy_quarantined_and_exhausted_accounts(database):
    store, campaign, _, _, _ = campaign_with_briefs(database)
    group = AccountGroup(alias="X principal")
    database.save_account_group(group)
    eligible = add_account(database, group, "eligible", tags=["sports", "argentina"])
    add_account(database, group, "quarantined", quarantined=True)
    add_account(database, group, "degraded", health_status=HealthStatus.DEGRADED)
    add_account(database, group, "quota", quota_status=QuotaStatus.EXHAUSTED)
    add_account(database, group, "expired", session_status=SessionStatus.EXPIRED)
    planner = CampaignPlannerService(database, store)

    plan = planner.assign_briefs_to_accounts(campaign.id, [group.id])

    assert [assignment.account_id for assignment in plan.assignments] == [eligible.id]
    assert plan.status is AssignmentStatus.PLANNED
    assert planner.eligible_accounts_for_group(group.id, filter_tags=["ARGENTINA"]) == [eligible]


def test_planner_balances_roles_within_an_account_group(database):
    store, campaign, _, _, _ = campaign_with_briefs(database)
    group = AccountGroup(alias="Flota X")
    database.save_account_group(group)
    for index in range(7):
        add_account(database, group, f"eligible-{index}")
    planner = CampaignPlannerService(database, store)

    plan = planner.assign_briefs_to_accounts(campaign.id, [group.id])
    role_counts = Counter(item.role for item in plan.assignments)

    assert len(plan.assignments) == 7
    assert max(role_counts.values()) - min(role_counts.values()) <= 1
    assert {item.status for item in plan.assignments} == {AssignmentStatus.PLANNED}


def test_assignment_plan_round_trips_as_dry_run_data_without_execution_dependencies(database):
    store, campaign, _, _, _ = campaign_with_briefs(database)
    group = AccountGroup(alias="Persistencia")
    database.save_account_group(group)
    add_account(database, group, "one")
    add_account(database, group, "two")

    plan = CampaignPlannerService(database, store).assign_briefs_to_accounts(campaign.id, [group.id])
    restored = store.get_assignment_plan(plan.id)
    payload = plan.to_dict()

    assert restored is not None
    assert restored.campaign_id == campaign.id
    assert restored.assignments == plan.assignments
    assert payload["status"] == "planned"
    assert len(payload["assignments"]) == 2
