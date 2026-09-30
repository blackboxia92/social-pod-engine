"""One reusable execution eligibility policy over Control Plane state."""

from __future__ import annotations

from dataclasses import dataclass

from ..adapters.base import Capability
from ..domain import HealthStatus, LifecycleStatus, QuotaStatus, SessionStatus, SocialAccount
from ..registry import AdapterRegistry
from .models import BlockReason


@dataclass(frozen=True, slots=True)
class ExecutionEligibilityPolicy:
    """Explicit operational policy; it does not recalculate health or session state."""

    allow_degraded_accounts: bool = False


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    eligible: bool
    block_reason: BlockReason | None = None
    description: str | None = None


class ExecutionEligibilityEvaluator:
    """Consumes persisted Control Plane signals for Bridge and Dispatcher alike."""

    def __init__(self, adapters: AdapterRegistry, policy: ExecutionEligibilityPolicy | None = None) -> None:
        self.adapters = adapters
        self.policy = policy or ExecutionEligibilityPolicy()

    def evaluate(self, account: SocialAccount, capability: Capability) -> EligibilityResult:
        if account.quarantined:
            return _blocked(BlockReason.ACCOUNT_QUARANTINED, "account has a technical quarantine")
        if account.lifecycle_status not in {LifecycleStatus.WARMUP, LifecycleStatus.ACTIVE}:
            return _blocked(BlockReason.LIFECYCLE_INELIGIBLE, "account lifecycle is not eligible")
        if account.health_status in {HealthStatus.ACTION_REQUIRED, HealthStatus.UNAVAILABLE}:
            return _blocked(BlockReason.ACCOUNT_UNHEALTHY, "account health requires intervention")
        if account.health_status is HealthStatus.DEGRADED and not self.policy.allow_degraded_accounts:
            return _blocked(BlockReason.ACCOUNT_UNHEALTHY, "degraded health is disallowed by policy")
        if account.session_status is not SessionStatus.VALID:
            return _blocked(BlockReason.SESSION_INVALID, "account session is not valid")
        if account.quota_status is QuotaStatus.EXHAUSTED:
            return _blocked(BlockReason.QUOTA_EXHAUSTED, "account quota is exhausted")
        try:
            adapter = self.adapters.get(account.platform.value)
        except KeyError:
            return _blocked(BlockReason.CAPABILITY_NOT_SUPPORTED, "no platform adapter is registered")
        if capability not in adapter.get_supported_capabilities():
            return _blocked(BlockReason.CAPABILITY_NOT_SUPPORTED, "platform adapter does not support capability")
        return EligibilityResult(eligible=True)


def _blocked(reason: BlockReason, description: str) -> EligibilityResult:
    return EligibilityResult(eligible=False, block_reason=reason, description=description)
