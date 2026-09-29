"""Isolated, multi-platform social account adapters.

This package deliberately has no import dependency on :mod:`camoufox_pm`.
It can be composed with an upstream-managed browser page without changing the
upstream profile manager, its database, or its API.
"""

from .engine import SocialPodEngine
from .models import AccountProfile, HealthReport, PlatformCapabilities, SessionState
from .registry import AdapterRegistry

__all__ = [
    "AccountProfile",
    "AdapterRegistry",
    "HealthReport",
    "PlatformCapabilities",
    "SessionState",
    "SocialPodEngine",
]
