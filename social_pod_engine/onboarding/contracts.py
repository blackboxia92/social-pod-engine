"""Abstract bridge to an upstream-owned profile and browser lifecycle.

The application composes an implementation of this protocol around Camoufox
Profile Manager.  This package deliberately contains no ``camoufox_pm`` import
and never reaches into CPM's database or browser manager directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ..domain import SocialAccount


@dataclass(frozen=True, slots=True)
class InteractiveBrowser:
    """A caller-owned interactive browser surface returned by the gateway."""

    page: Any
    handle: Any


class UpstreamProfileNotFound(RuntimeError):
    """The opaque profile reference no longer exists in the upstream service."""


class UpstreamOnboardingGateway(Protocol):
    """Minimal asynchronous lifecycle boundary implemented outside this package."""

    async def create_profile(self, account: SocialAccount) -> str:
        """Create an upstream physical profile and return its opaque identifier."""

    async def get_profile(self, upstream_profile_id: str) -> Any:
        """Validate an existing opaque profile reference before it is launched.

        Implementations raise :class:`UpstreamProfileNotFound` only when the
        upstream service confirms that the profile does not exist.
        """

    async def acquire_lease(self, upstream_profile_id: str, *, proxy_id: str | None) -> Any:
        """Acquire the upstream lease before launching an interactive browser."""

    async def open_interactive_browser(self, lease: Any) -> InteractiveBrowser:
        """Open an operator-visible browser for a lease."""

    async def close_interactive_browser(self, browser: InteractiveBrowser) -> None:
        """Close the browser window owned by this onboarding attempt."""

    async def release_lease(self, lease: Any) -> None:
        """Release the lease obtained for this onboarding attempt."""
