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


def test_operator_shows_a_structured_rpc_failure_with_safe_detail():
    task = SimpleNamespace(
        id=uuid4(),
        status=TaskStatus.READY,
        last_error=(
            "RPC_UNAVAILABLE | operation=launch_remote_profile | "
            "endpoint=/api/v1/profiles/profile-1/launch-remote | "
            "cause=ReadTimeout | detail=timeout waiting for launch-remote"
        ),
        block_reason=None,
    )

    class Dispatcher:
        async def run_task_async(self, task_id, worker_id):
            del task_id, worker_id
            return ExecutionDispatchResult(DispatchOutcome.RPC_UNAVAILABLE, task)

    lines: list[str] = []
    runtime = SimpleNamespace(dispatcher=Dispatcher(), config=SimpleNamespace(worker_id="operator-worker"))
    XOperator(runtime, output_fn=lines.append)._execute_prepared_task(task)

    assert lines == [
        "❌ NO PUBLICADO\nMotivo: RPC_UNAVAILABLE\n"
        "Detalle: operation=launch_remote_profile | "
        "endpoint=/api/v1/profiles/profile-1/launch-remote | "
        "cause=ReadTimeout | detail=timeout waiting for launch-remote"
    ]


def test_operator_shows_safe_detail_for_an_unknown_submit_state():
    task = SimpleNamespace(
        id=uuid4(),
        status=TaskStatus.UNKNOWN_EXTERNAL_STATE,
        last_error="UNKNOWN_EXTERNAL_STATE: click completed but no deterministic post-submit signal was observed",
    )

    class Dispatcher:
        async def run_task_async(self, task_id, worker_id):
            del task_id, worker_id
            return ExecutionDispatchResult(DispatchOutcome.UNKNOWN_EXTERNAL_STATE, task)

    lines: list[str] = []
    runtime = SimpleNamespace(dispatcher=Dispatcher(), config=SimpleNamespace(worker_id="operator-worker"))
    XOperator(runtime, output_fn=lines.append)._execute_prepared_task(task)

    assert "RESULTADO NO CONFIRMADO" in lines[0]
    assert "Detalle: UNKNOWN_EXTERNAL_STATE" in lines[0]


def test_pending_task_view_recovers_expired_claims_before_displaying_status():
    task = SimpleNamespace(
        id=uuid4(),
        account_id=uuid4(),
        capability=SimpleNamespace(value="post"),
        status=TaskStatus.RUNNING,
        block_reason=None,
        revision=1,
    )

    class Queue:
        def __init__(self) -> None:
            self.recovery_calls = 0

        def recover_expired_claims(self) -> int:
            self.recovery_calls += 1
            task.status = TaskStatus.READY
            return 1

        def list_tasks(self):
            return [task]

    queue = Queue()
    lines: list[str] = []
    runtime = SimpleNamespace(queue=queue)

    XOperator(runtime, output_fn=lines.append).show_tasks(False)

    assert queue.recovery_calls == 1
    assert lines[0] == "Se recuperaron 1 tareas RUNNING con claim vencido."
    assert " ready " in lines[1]
