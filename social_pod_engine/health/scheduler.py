"""Persistent due-date scheduler without a permanent task per social account."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from ..domain import utc_now
from ..persistence import SocialPodDatabase
from .engine import HealthEngine
from .models import HealthBatchReport


class _UniformRandom(Protocol):
    def uniform(self, lower: float, upper: float) -> float: ...


@dataclass(frozen=True, slots=True)
class HealthSchedulerConfig:
    interval_seconds: float = 900.0
    jitter_seconds: float = 60.0

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if self.jitter_seconds < 0:
            raise ValueError("jitter_seconds must be non-negative")


class HealthScheduler:
    """Selects persisted due accounts, delegates checking, and persists the next run."""

    def __init__(
        self,
        database: SocialPodDatabase,
        engine: HealthEngine,
        *,
        config: HealthSchedulerConfig | None = None,
        rng: _UniformRandom | None = None,
        now_provider: Callable[[], datetime] = utc_now,
    ) -> None:
        self.database = database
        self.engine = engine
        self.config = config or HealthSchedulerConfig()
        self.rng = rng or random.Random()
        self.now_provider = now_provider

    async def run_due_healthchecks(self, *, limit: int = 50) -> HealthBatchReport:
        now = self.now_provider()
        accounts = self.engine.repository.list_due(now, limit=limit)
        report = await self.engine.run_accounts([account.id for account in accounts])
        for result in report.results:
            account = self.database.get_social_account(result.account_id)
            if account is None:
                continue
            jitter = self.rng.uniform(0, self.config.jitter_seconds)
            account.next_healthcheck_at = now + timedelta(seconds=self.config.interval_seconds + jitter)
            account.updated_at = now
            self.database.save_social_account(account)
        return report
