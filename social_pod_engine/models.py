"""Platform-neutral values owned by Social Pod Engine, not by CPM."""

from dataclasses import dataclass, field
from enum import StrEnum


class SessionState(StrEnum):
    """A conservative reading of a page's account state."""

    ACTIVE = "active"
    SIGNED_OUT = "signed_out"
    CHALLENGE = "challenge"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PlatformCapabilities:
    """Declared capabilities; planned operations are never executed implicitly."""

    enabled: frozenset[str] = frozenset()
    planned: frozenset[str] = frozenset()

    def supports(self, capability: str) -> bool:
        return capability in self.enabled


@dataclass(frozen=True)
class AccountProfile:
    """Portable identity view obtained from a platform page.

    ``profile_id`` is an opaque CPM profile identifier supplied by the caller;
    this package neither reads nor writes CPM storage.
    """

    platform_id: str
    handle: str | None = None
    display_name: str | None = None
    profile_id: str | None = None


@dataclass(frozen=True)
class HealthReport:
    """Read-only session health result for a social platform."""

    platform_id: str
    state: SessionState
    reasons: tuple[str, ...] = field(default_factory=tuple)
