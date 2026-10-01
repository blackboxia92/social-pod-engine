from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from social_pod_engine.campaign.models import DispatchOutcome, ExecutionDispatchResult, TaskStatus
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


def test_operator_executes_only_the_previewed_task_id():
    selected_task = SimpleNamespace(
        id=uuid4(),
        status=TaskStatus.COMPLETED,
        external_id="post-1",
        external_url="https://x.com/example/status/post-1",
    )
    calls: list[object] = []

    class Dispatcher:
        async def run_task_async(self, task_id, worker_id):
            calls.append((task_id, worker_id))
            return ExecutionDispatchResult(DispatchOutcome.WOULD_EXECUTE, selected_task)

    lines: list[str] = []
    runtime = SimpleNamespace(
        dispatcher=Dispatcher(), config=SimpleNamespace(worker_id="operator-worker")
    )

    XOperator(runtime, output_fn=lines.append)._execute_prepared_task(selected_task)

    assert calls == [(selected_task.id, "operator-worker")]
    assert any("PUBLICADO" in line for line in lines)


def test_operator_reports_a_safe_execution_error_without_raising(monkeypatch):
    task = SimpleNamespace(id=uuid4())

    class Dispatcher:
        async def run_task_async(self, task_id, worker_id):
            del task_id, worker_id
            raise RuntimeError("authorization token should not be shown")

    monkeypatch.delenv("SOCIAL_POD_DEBUG", raising=False)
    lines: list[str] = []
    runtime = SimpleNamespace(dispatcher=Dispatcher(), config=SimpleNamespace(worker_id="operator-worker"))

    XOperator(runtime, output_fn=lines.append)._execute_prepared_task(task)

    assert lines[0] == "❌ No se pudo ejecutar el post"
    assert "token" not in lines[1].lower()
    assert "Ocurrió un error operativo" in lines[1]
