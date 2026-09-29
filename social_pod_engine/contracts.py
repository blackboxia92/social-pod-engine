"""Small browser and adapter contracts used by the isolated extension."""

from abc import ABC, abstractmethod
from typing import Protocol

from .models import AccountProfile, HealthReport, PlatformCapabilities


class Locator(Protocol):
    async def count(self) -> int: ...

    async def get_attribute(self, name: str) -> str | None: ...

    async def inner_text(self) -> str: ...


class SocialPage(Protocol):
    """The minimal Playwright-compatible surface required by adapters."""

    url: str

    async def goto(self, url: str) -> object: ...

    def locator(self, selector: str) -> Locator: ...


class SocialPlatformAdapter(ABC):
    """Read-only social platform boundary.

    The initial adapter contract purposefully excludes posting and engagement.
    Any future write operation needs its own explicit consent and rate-limit
    design instead of inheriting a browser profile's authority.
    """

    platform_id: str
    display_name: str

    @abstractmethod
    async def open_home(self, page: SocialPage) -> None:
        """Navigate a supplied browser page to the platform home page."""

    @abstractmethod
    async def check_session(self, page: SocialPage) -> HealthReport:
        """Return a conservative, read-only session health assessment."""

    @abstractmethod
    async def get_profile(self, page: SocialPage, *, profile_id: str | None = None) -> AccountProfile:
        """Read a lightweight account identity from an authenticated page."""

    @abstractmethod
    def get_capabilities(self) -> PlatformCapabilities:
        """Declare enabled and planned capabilities for this platform."""
