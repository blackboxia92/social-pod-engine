"""Provider-neutral content generation contract and deterministic implementation."""

from __future__ import annotations

from typing import Protocol

from .models import ContentGenerationRequest, GeneratedContent


class ContentGenerator(Protocol):
    async def generate(self, request: ContentGenerationRequest) -> list[GeneratedContent]: ...


class DeterministicTemplateGenerator:
    """Offline generator used for repeatable tests and template-based workflows."""

    name = "deterministic-template"
    version = "1"

    async def generate(self, request: ContentGenerationRequest) -> list[GeneratedContent]:
        narrative = str(request.source_context.get("narrative_angle", ""))
        role = request.editorial_role or "general"
        return [
            GeneratedContent(
                text=f"{role}: {narrative} ({index + 1})".strip(),
                variant=index + 1,
                generator_name=self.name,
                generator_version=self.version,
            )
            for index in range(request.requested_variants)
        ]
