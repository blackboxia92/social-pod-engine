"""Isolated, multi-platform social account adapters.

This package deliberately has no import dependency on :mod:`camoufox_pm`.
It can be composed with an upstream-managed browser page without changing the
upstream profile manager, its database, or its API.
"""

from .domain import (
    AccountGroup,
    HealthIssueType,
    HealthStatus,
    LifecycleStatus,
    Persona,
    QuotaStatus,
    SessionStatus,
    SocialAccount,
    SocialPlatform,
)
from .engine import SocialPodEngine
from .models import AccountProfile, HealthReport, PlatformCapabilities, SessionState
from .registry import AdapterRegistry

__all__ = [
    "AccountProfile",
    "AccountGroup",
    "AdapterRegistry",
    "HealthReport",
    "HealthIssueType",
    "HealthStatus",
    "LifecycleStatus",
    "Persona",
    "PlatformCapabilities",
    "QuotaStatus",
    "SessionStatus",
    "SessionState",
    "SocialAccount",
    "SocialPodEngine",
    "SocialPlatform",
]
