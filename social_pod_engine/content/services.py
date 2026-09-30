"""Generation, review and task-linking services with no browser or adapter execution."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable
from uuid import UUID

from ..adapters.base import Capability
from ..campaign.bridge import ExecutionBridge
from ..campaign.execution_persistence import ExecutionTaskStore
from ..campaign.models import ApprovalStatus, ExecutionTask
from ..narrative.persistence import NarrativeCampaignStore
from ..persistence import SocialPodDatabase
from .generator import ContentGenerator
from .models import (
    ContentDraft,
    ContentDraftStatus,
    ContentGenerationPolicy,
    ContentGenerationReport,
    ContentGenerationRequest,
    ContentReviewReport,
    ContentType,
    GenerationSource,
)
from .persistence import ContentDraftStore


class ContentContextBuilder:
    """Builds deterministic, serializable editorial context before generation."""

    def __init__(self, database: SocialPodDatabase, campaigns: NarrativeCampaignStore) -> None:
        self.database = database
        self.campaigns = campaigns

    def build(self, assignment_id: UUID, *, content_type: ContentType = ContentType.POST, variants: int = 1) -> ContentGenerationRequest:
        assignment = self.campaigns.get_assignment(assignment_id)
        if assignment is None:
            raise KeyError(f"Assignment not found: {assignment_id}")
        account = self.database.get_social_account(assignment.account_id)
        narrative = self.campaigns.get_narrative(assignment.narrative_id)
        brief = self.campaigns.get_content_brief(assignment.brief_id)
        if account is None or narrative is None or brief is None:
            raise ValueError("assignment references missing editorial data")
        campaign = self.campaigns.get_campaign(narrative.campaign_id)
        if campaign is None:
            raise ValueError("narrative references missing campaign")
        policy = self.campaigns.get_editorial_policy_for_campaign(campaign.id)
        role = account.editorial_role or assignment.role.value
        return ContentGenerationRequest(
            campaign_id=campaign.id, narrative_id=narrative.id, brief_id=brief.id, assignment_id=assignment.id,
            account_id=account.id, platform=account.platform, editorial_role=role, content_type=content_type,
            requested_variants=variants,
            source_context={
                "campaign_name": campaign.name, "narrative_angle": narrative.angle, "key_messages": narrative.key_messages,
                "brief_instructions": brief.prompt_instructions, "tone_of_voice": policy.tone_of_voice if policy else None,
                "hashtags": policy.hashtags if policy else [], "tags": account.tags, "group_id": str(account.group_id) if account.group_id else None,
            },
            constraints={
                "forbidden_terms": policy.forbidden_terms if policy else [],
                "required_disclaimers": policy.required_disclaimers if policy else [],
            },
        )


class ContentGenerationService:
    def __init__(self, store: ContentDraftStore, builder: ContentContextBuilder, generator: ContentGenerator, policy: ContentGenerationPolicy | None = None) -> None:
        self.store, self.builder, self.generator = store, builder, generator
        self.policy = policy or ContentGenerationPolicy()

    async def generate_for_assignment(self, assignment_id: UUID, *, content_type: ContentType = ContentType.POST, variants: int | None = None) -> list[ContentDraft]:
        request = self.builder.build(assignment_id, content_type=content_type, variants=variants or self.policy.variants_per_assignment)
        return await self.generate(request)

    async def generate(self, request: ContentGenerationRequest) -> list[ContentDraft]:
        generated = await self.generator.generate(request)
        revision = self.store.next_revision(request.assignment_id)
        self.store.supersede_assignment(request.assignment_id, below_revision=revision)
        fingerprint = _fingerprint(request, getattr(self.generator, "version", "unknown"))
        drafts: list[ContentDraft] = []
        batch_texts: set[str] = set()
        for item in generated:
            duplicate = item.text in batch_texts or self.store.text_exists(request.campaign_id, item.text)
            batch_texts.add(item.text)
            _validate_text(item.text, request.constraints, self.policy)
            if duplicate and self.policy.reject_duplicates:
                continue
            draft = ContentDraft(
                campaign_id=request.campaign_id, narrative_id=request.narrative_id, brief_id=request.brief_id,
                assignment_id=request.assignment_id, account_id=request.account_id, platform=request.platform,
                editorial_role=request.editorial_role, content_type=request.content_type, text=item.text, revision=revision,
                status=ContentDraftStatus.UNDER_REVIEW if self.policy.require_review else ContentDraftStatus.GENERATED,
                generation_source=GenerationSource.TEMPLATE, generator_name=item.generator_name, generator_version=item.generator_version,
                prompt_fingerprint=fingerprint, duplicate_content=duplicate,
            )
            self.store.save(draft, event="DRAFT_REGENERATED" if revision > 1 else "DRAFT_GENERATED")
            drafts.append(draft)
        return drafts

    async def generate_batch(self, assignment_ids: Iterable[UUID]) -> ContentGenerationReport:
        ids = list(assignment_ids)
        semaphore = asyncio.Semaphore(self.policy.max_concurrency)
        errors: list[str] = []

        async def one(item: UUID) -> list[ContentDraft]:
            async with semaphore:
                try:
                    return await self.generate_for_assignment(item)
                except (KeyError, ValueError) as exc:
                    errors.append(str(exc))
                    return []

        groups = await asyncio.gather(*(one(item) for item in ids))
        drafts = [draft for group in groups for draft in group]
        return ContentGenerationReport(len(ids), len(drafts), len(drafts), sum(d.duplicate_content for d in drafts), len(errors), sum(d.status is ContentDraftStatus.UNDER_REVIEW for d in drafts), 0, 0, tuple(errors))

    async def generate_for_campaign(self, campaign_id: UUID) -> ContentGenerationReport:
        return await self.generate_batch(self.builder.campaigns.list_assignment_ids(campaign_id))

    async def generate_for_group(self, campaign_id: UUID, group_id: UUID) -> ContentGenerationReport:
        return await self.generate_batch(self.builder.campaigns.list_assignment_ids(campaign_id, group_id=group_id))

    async def generate_for_role(self, campaign_id: UUID, role: str) -> ContentGenerationReport:
        return await self.generate_batch(self.builder.campaigns.list_assignment_ids(campaign_id, role=role))

    def register_manual(self, request: ContentGenerationRequest, text: str) -> ContentDraft:
        _validate_text(text, request.constraints, self.policy)
        revision = self.store.next_revision(request.assignment_id)
        self.store.supersede_assignment(request.assignment_id, below_revision=revision)
        draft = ContentDraft(campaign_id=request.campaign_id, narrative_id=request.narrative_id, brief_id=request.brief_id, assignment_id=request.assignment_id, account_id=request.account_id, platform=request.platform, editorial_role=request.editorial_role, content_type=request.content_type, text=text, revision=revision, generation_source=GenerationSource.MANUAL, status=ContentDraftStatus.UNDER_REVIEW if self.policy.require_review else ContentDraftStatus.GENERATED, prompt_fingerprint=_fingerprint(request, "manual"), duplicate_content=self.store.text_exists(request.campaign_id, text))
        return self.store.save(draft)


class ContentReviewService:
    _transitions = {
        ContentDraftStatus.GENERATED: {ContentDraftStatus.UNDER_REVIEW},
        ContentDraftStatus.UNDER_REVIEW: {ContentDraftStatus.APPROVED, ContentDraftStatus.REJECTED, ContentDraftStatus.SUPERSEDED},
    }

    def __init__(self, store: ContentDraftStore, database: SocialPodDatabase) -> None:
        self.store, self.database = store, database

    def mark_under_review(self, draft_id: UUID) -> ContentDraft:
        return self._transition(draft_id, ContentDraftStatus.UNDER_REVIEW, "DRAFT_UNDER_REVIEW")

    def approve(self, draft_id: UUID) -> ContentDraft:
        return self._transition(draft_id, ContentDraftStatus.APPROVED, "DRAFT_APPROVED")

    def reject(self, draft_id: UUID, reason: str) -> ContentDraft:
        return self._transition(draft_id, ContentDraftStatus.REJECTED, "DRAFT_REJECTED", reason)

    def supersede(self, draft_id: UUID) -> ContentDraft:
        return self._transition(draft_id, ContentDraftStatus.SUPERSEDED, "DRAFT_SUPERSEDED")

    def list_pending_review(self, campaign_id: UUID | None = None) -> list[ContentDraft]:
        return self.store.list(campaign_id=campaign_id, status=ContentDraftStatus.UNDER_REVIEW)

    def list_approved(self, campaign_id: UUID | None = None) -> list[ContentDraft]:
        return self.store.list(campaign_id=campaign_id, status=ContentDraftStatus.APPROVED)

    def approve_batch(self, *, campaign_id: UUID | None = None, brief_id: UUID | None = None, group_id: UUID | None = None, role: str | None = None, draft_ids: Iterable[UUID] | None = None) -> ContentReviewReport:
        selected = set(draft_ids or [])
        drafts = self.list_pending_review(campaign_id)
        chosen = []
        for draft in drafts:
            account = self.database.get_social_account(draft.account_id)
            if selected and draft.id not in selected:
                continue
            if brief_id and draft.brief_id != brief_id:
                continue
            if group_id and (account is None or account.group_id != group_id):
                continue
            if role and draft.editorial_role != role.strip().lower():
                continue
            chosen.append(self.approve(draft.id))
        return ContentReviewReport(len(drafts), len(chosen), 0, len(drafts) - len(chosen))

    def _transition(self, draft_id: UUID, target: ContentDraftStatus, event: str, reason: str | None = None) -> ContentDraft:
        draft = self.store.required(draft_id)
        if target not in self._transitions.get(draft.status, set()):
            raise ValueError(f"cannot transition {draft.status.value} to {target.value}")
        return self.store.transition(draft.id, target, reason=reason, event=event)


class ApprovedContentTaskLinker:
    """Links approved content to a task revision; it never dispatches or executes it."""
    def __init__(self, store: ContentDraftStore, bridge: ExecutionBridge, queue: ExecutionTaskStore) -> None:
        self.store, self.bridge, self.queue = store, bridge, queue

    def link(self, draft_id: UUID, *, capability: Capability = Capability.POST) -> ExecutionTask:
        draft = self.store.required(draft_id)
        if draft.status is not ContentDraftStatus.APPROVED:
            raise ValueError("only approved drafts can be linked")
        task = self.bridge.create_task(campaign_id=draft.campaign_id, assignment_id=draft.assignment_id, capability=capability, revision=draft.revision, inherited_approval_status=ApprovalStatus.APPROVED)
        payload = {"content_draft_id": str(draft.id), "revision": draft.revision, "content_type": draft.content_type.value, "text": draft.text, "brief_id": str(draft.brief_id), "narrative_id": str(draft.narrative_id), "editorial_role": draft.editorial_role}
        task = self.queue.update_payload(task.id, payload)
        self.store.link_task(draft.id, task.id)
        return task


def _fingerprint(request: ContentGenerationRequest, generator_version: str) -> str:
    material = {"campaign": str(request.campaign_id), "narrative": str(request.narrative_id), "brief": str(request.brief_id), "role": request.editorial_role, "platform": request.platform.value, "constraints": request.constraints, "generator_version": generator_version}
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


def _validate_text(text: str, constraints: dict[str, object], policy: ContentGenerationPolicy) -> None:
    if not text.strip() or not policy.min_length <= len(text) <= policy.max_length:
        raise ValueError("content does not meet configured length constraints")
    terms = constraints.get("forbidden_terms", [])
    forbidden = {str(item).casefold() for item in terms} if isinstance(terms, list) else set()
    if any(item in text.casefold() for item in forbidden):
        raise ValueError("content contains a forbidden term")
