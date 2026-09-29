"""Explicit placeholders for platforms not implemented in this increment."""

from ..contracts import SocialPage, SocialPlatformAdapter
from ..models import AccountProfile, HealthReport, PlatformCapabilities


class PlatformAdapterNotImplemented(NotImplementedError):
    """Raised instead of silently pretending a platform is operational."""


class PlaceholderAdapter(SocialPlatformAdapter):
    home_url: str

    async def open_home(self, page: SocialPage) -> None:
        raise PlatformAdapterNotImplemented(
            f"{self.display_name} adapter is a placeholder; navigation is not enabled yet."
        )

    async def check_session(self, page: SocialPage) -> HealthReport:
        raise PlatformAdapterNotImplemented(
            f"{self.display_name} adapter is a placeholder; session checks are not enabled yet."
        )

    async def get_profile(self, page: SocialPage, *, profile_id: str | None = None) -> AccountProfile:
        raise PlatformAdapterNotImplemented(
            f"{self.display_name} adapter is a placeholder; profile reads are not enabled yet."
        )

    def get_capabilities(self) -> PlatformCapabilities:
        return PlatformCapabilities(planned=frozenset({"session_health", "read_profile"}))
