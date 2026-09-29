"""Pure narrative-plane domain models and isolated SQLite persistence."""

from typing import TYPE_CHECKING

from .domain import (
    Campaign,
    CampaignStatus,
    ContentBrief,
    EditorialPolicy,
    Narrative,
    NarrativeRole,
)

if TYPE_CHECKING:
    from .persistence import NarrativeCampaignStore

__all__ = [
    "Campaign",
    "CampaignStatus",
    "ContentBrief",
    "EditorialPolicy",
    "Narrative",
    "NarrativeCampaignStore",
    "NarrativeRole",
]


def __getattr__(name: str):
    if name == "NarrativeCampaignStore":
        from .persistence import NarrativeCampaignStore

        return NarrativeCampaignStore
    raise AttributeError(name)
