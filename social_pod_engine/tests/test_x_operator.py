from __future__ import annotations

from types import SimpleNamespace

from social_pod_engine.domain import Persona, SocialAccount, SocialPlatform
from social_pod_engine.onboarding.models import (
    OnboardingBatchReport,
    OnboardingItemResult,
    OnboardingItemStatus,
)
from social_pod_engine.tools.x_operator import XOperator


class _Store:
    def save(self, state) -> None:
        self.state = state


class _Runner:
    def __init__(self, report: OnboardingBatchReport) -> None:
        self.report = report
        self.store = _Store()
        self.state = None

    async def start(self, *, operator_confirmation):
        assert callable(operator_confirmation)
        return self.report


def test_operator_displays_the_preserved_onboarding_failure_reason():
    account = SocialAccount(persona_id=Persona(alias="operator").id, platform=SocialPlatform.X, username="cuervitonp")
    result = OnboardingItemResult(
        account.id,
        account.username,
        OnboardingItemStatus.FAILED,
        "new-profile",
        "Camoufox returned HTTP 404 while opening the browser",
    )
    report = OnboardingBatchReport(1, 0, 0, 0, 1, [result])
    lines: list[str] = []
    runtime = SimpleNamespace(
        onboarding=_Runner(report),
        database=SimpleNamespace(get_social_account=lambda account_id: account),
    )

    XOperator(runtime, input_fn=lambda _: "", output_fn=lines.append).onboard(account)

    assert any("Motivo: Camoufox returned HTTP 404" in line for line in lines)
