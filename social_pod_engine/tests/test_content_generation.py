from __future__ import annotations

import pytest

from social_pod_engine.adapters.base import Capability
from social_pod_engine.adapters.x import XAdapter
from social_pod_engine.campaign.bridge import ExecutionBridge
from social_pod_engine.campaign.execution_persistence import ExecutionTaskStore
from social_pod_engine.campaign.planner import CampaignPlannerService
from social_pod_engine.content.generator import DeterministicTemplateGenerator
from social_pod_engine.content.models import (
    ContentDraftStatus,
    ContentGenerationPolicy,
)
from social_pod_engine.content.persistence import ContentDraftStore
from social_pod_engine.content.services import (
    ApprovedContentTaskLinker,
    ContentContextBuilder,
    ContentGenerationService,
    ContentReviewService,
)
from social_pod_engine.domain import (
    AccountGroup,
    HealthStatus,
    LifecycleStatus,
    Persona,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
)
from social_pod_engine.narrative import (
    Campaign,
    CampaignStatus,
    ContentBrief,
    Narrative,
    NarrativeCampaignStore,
    NarrativeRole,
)
from social_pod_engine.persistence import SocialPodDatabase
from social_pod_engine.registry import AdapterRegistry


@pytest.fixture
def stack(tmp_path):
    database = SocialPodDatabase(tmp_path / "pod.sqlite3")
    database.initialize()
    group, persona = AccountGroup(alias="football"), Persona(alias="writer")
    account = SocialAccount(persona_id=persona.id, platform=SocialPlatform.X, username="writer", group_id=group.id, editorial_role="Stats And Facts", tags=["Sports"], health_status=HealthStatus.HEALTHY, session_status=SessionStatus.VALID, lifecycle_status=LifecycleStatus.WARMUP)
    database.save_account_group(group)
    database.save_persona(persona)
    database.save_social_account(account)
    narratives = NarrativeCampaignStore(database)
    campaign = Campaign(name="Mercado de pases", status=CampaignStatus.ACTIVE)
    narrative = Narrative(campaign_id=campaign.id, title="Datos", angle="Datos verificables", key_messages=["Datos"])
    brief = ContentBrief(narrative_id=narrative.id, role=NarrativeRole.STATS_AND_FACTS, platform=SocialPlatform.X, prompt_instructions="Usar fuentes públicas")
    narratives.save_campaign(campaign)
    narratives.save_narrative(narrative)
    narratives.save_content_brief(brief)
    assignment = CampaignPlannerService(database, narratives).assign_briefs_to_accounts(campaign.id, [group.id]).assignments[0]
    store = ContentDraftStore(database)
    builder = ContentContextBuilder(database, narratives)
    generator = ContentGenerationService(store, builder, DeterministicTemplateGenerator(), ContentGenerationPolicy(variants_per_assignment=2))
    review = ContentReviewService(store, database)
    yield database, narratives, campaign, group, account, assignment, store, builder, generator, review
    database.close()


async def test_generation_context_template_variants_and_persistence(stack):
    _, _, campaign, _, account, assignment, store, builder, generator, _ = stack
    request = builder.build(assignment.id, variants=2)
    drafts = await generator.generate(request)

    assert request.editorial_role == "stats-and-facts"
    assert request.source_context["tags"] == ["sports"]
    assert len(drafts) == 2 and drafts[0].text != drafts[1].text
    assert {item.status for item in drafts} == {ContentDraftStatus.UNDER_REVIEW}
    assert store.get(drafts[0].id).prompt_fingerprint == drafts[0].prompt_fingerprint
    assert drafts[0].campaign_id == campaign.id and drafts[0].account_id == account.id


async def test_review_transitions_batch_filters_and_invalid_transition(stack):
    _, _, campaign, group, _, assignment, _, builder, generator, review = stack
    drafts = await generator.generate(builder.build(assignment.id, variants=2))
    report = review.approve_batch(campaign_id=campaign.id, group_id=group.id, role="stats-and-facts")
    assert report.approved_count == 2
    assert len(review.list_approved(campaign.id)) == 2
    with pytest.raises(ValueError):
        review.approve(drafts[0].id)


async def test_regeneration_supersedes_old_drafts_and_detects_duplicates(stack):
    _, _, _, _, _, assignment, store, builder, generator, _ = stack
    first = await generator.generate(builder.build(assignment.id, variants=1))
    second = await generator.generate(builder.build(assignment.id, variants=1))
    assert first[0].revision == 1 and second[0].revision == 2
    assert store.get(first[0].id).status is ContentDraftStatus.SUPERSEDED

    manual = generator.register_manual(builder.build(assignment.id), second[0].text)
    assert manual.duplicate_content is True


async def test_approved_draft_links_structured_payload_without_execution(stack):
    database, narratives, campaign, _, _, assignment, _, builder, generator, review = stack
    draft = (await generator.generate(builder.build(assignment.id, variants=1)))[0]
    review.approve(draft.id)
    registry = AdapterRegistry()
    registry.register(XAdapter())
    queue = ExecutionTaskStore(database)
    bridge = ExecutionBridge(database, narratives, queue, registry)
    task = ApprovedContentTaskLinker(review.store, bridge, queue).link(draft.id, capability=Capability.POST)

    assert task.payload["content_draft_id"] == str(draft.id)
    assert task.payload["revision"] == draft.revision
    assert task.completed_at is None
    assert queue.get(task.id).status.name != "COMPLETED"


def test_manual_content_and_constraints_are_editorial_only(stack):
    _, _, _, _, _, assignment, _, builder, generator, _ = stack
    request = builder.build(assignment.id)
    draft = generator.register_manual(request, "Texto externo aprobado para revisión")
    assert draft.generation_source.value == "manual"
    with pytest.raises(ValueError):
        generator.register_manual(request, "")
