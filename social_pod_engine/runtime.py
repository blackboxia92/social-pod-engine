"""One explicit composition root for the local Social Pod operator."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .adapters.x import XAdapter
from .campaign.bridge import ExecutionBridge
from .campaign.dispatcher import ExecutionDispatcher
from .campaign.execution_persistence import ExecutionTaskStore
from .content.persistence import ContentDraftStore
from .content.services import ApprovedContentTaskLinker, ContentReviewService
from .health.engine import HealthEngine, HealthEngineConfig
from .integrations.camoufox_http import CamoufoxHttpClient, CamoufoxHttpGateway
from .narrative.persistence import NarrativeCampaignStore
from .onboarding.runner import OnboardingConfig, OnboardingRunner
from .persistence import SocialPodDatabase
from .registry import AdapterRegistry


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    camoufox_base_url: str = "http://127.0.0.1:8000"
    database_path: str = "social_pod_engine/social_pod.db"
    execution_enabled: bool = False
    worker_id: str = "x-operator"
    http_timeout: float = 10.0
    launch_timeout: float = 30.0
    max_concurrency: int = 1

    @classmethod
    def from_environment(cls) -> RuntimeConfig:
        return cls(
            camoufox_base_url=os.getenv("CAMOUFOX_BASE_URL", "http://127.0.0.1:8000"),
            database_path=os.getenv("SOCIAL_POD_DB_PATH", "social_pod_engine/social_pod.db"),
            execution_enabled=os.getenv("SOCIAL_POD_EXECUTION_ENABLED", "false").lower() == "true",
            worker_id=os.getenv("SOCIAL_POD_WORKER_ID", "x-operator"),
            http_timeout=float(os.getenv("SOCIAL_POD_HTTP_TIMEOUT", "10.0")),
            launch_timeout=float(os.getenv("SOCIAL_POD_CPM_LAUNCH_TIMEOUT", "30.0")),
            max_concurrency=int(os.getenv("SOCIAL_POD_MAX_CONCURRENCY", "1")),
        )


@dataclass(slots=True)
class SocialPodRuntime:
    config: RuntimeConfig
    database: SocialPodDatabase
    gateway: CamoufoxHttpGateway
    onboarding: OnboardingRunner
    health: HealthEngine
    dispatcher: ExecutionDispatcher
    campaigns: NarrativeCampaignStore
    drafts: ContentDraftStore
    review: ContentReviewService
    bridge: ExecutionBridge
    linker: ApprovedContentTaskLinker
    queue: ExecutionTaskStore

    async def camoufox_status(self):
        return await self.gateway.service_status()


def build_runtime(config: RuntimeConfig | None = None) -> SocialPodRuntime:
    config = config or RuntimeConfig.from_environment()
    database = SocialPodDatabase(config.database_path)
    database.initialize()
    adapters = AdapterRegistry()
    adapters.register(XAdapter())
    client = CamoufoxHttpClient(
        config.camoufox_base_url,
        timeout=config.http_timeout,
        launch_timeout=config.launch_timeout,
    )
    gateway = CamoufoxHttpGateway(client)
    campaigns = NarrativeCampaignStore(database)
    campaigns.initialize()
    drafts = ContentDraftStore(database)
    drafts.initialize()
    queue = ExecutionTaskStore(database)
    queue.initialize()
    bridge = ExecutionBridge(database, campaigns, queue, adapters)
    review = ContentReviewService(drafts, database)
    return SocialPodRuntime(
        config, database, gateway,
        OnboardingRunner(database, adapters, gateway, config=OnboardingConfig()),
        HealthEngine(database, adapters, gateway, config=HealthEngineConfig(max_concurrency=config.max_concurrency)),
        ExecutionDispatcher(database, queue, adapters, execution_enabled=config.execution_enabled, gateway=gateway, content_store=drafts, max_concurrency=config.max_concurrency),
        campaigns, drafts, review, bridge, ApprovedContentTaskLinker(drafts, bridge, queue), queue,
    )
