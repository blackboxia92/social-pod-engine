"""Interactive X operator; credentials are entered only in the CPM browser."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

from ..adapters.base import Capability
from ..campaign.models import AssignmentPlan, PlannedAssignment, TaskStatus
from ..content.models import ContentType
from ..content.services import ContentContextBuilder, ContentGenerationService
from ..domain import Persona, SocialAccount, SocialPlatform
from ..narrative.domain import Campaign, ContentBrief, Narrative, NarrativeRole
from ..onboarding.models import OnboardingQueueState
from ..runtime import SocialPodRuntime, build_runtime


@dataclass
class XOperator:
    runtime: SocialPodRuntime
    input_fn: object = input
    output_fn: object = print

    def ask(self, prompt: str) -> str:
        return self.input_fn(prompt)  # type: ignore[operator]

    def say(self, text: str) -> None:
        self.output_fn(text)  # type: ignore[operator]

    def accounts(self):
        return [
            a
            for a in self.runtime.database.list_healthcheck_candidates()
            if a.platform is SocialPlatform.X
        ]

    def select(self):
        accounts = self.accounts()
        if not accounts:
            self.say("No hay cuentas X registradas.")
            return None
        for index, account in enumerate(accounts, 1):
            self.say(
                f"{index}. @{account.username} | sesión: {account.session_status.value} | salud: {account.health_status.value} | cuarentena: {'SI' if account.quarantined else 'NO'}"
            )
        try:
            return accounts[int(self.ask("Elegí una cuenta: ")) - 1]
        except (ValueError, IndexError):
            self.say("Selección inválida.")
            return None

    def run(self) -> None:
        while True:
            self.say(
                "\nSOCIAL POD — X OPERATOR\n1. Ver cuentas X\n2. Crear / onboardear cuenta X\n3. Reautenticar cuenta\n4. Ejecutar healthcheck\n5. Preparar post de prueba\n6. Ver tareas pendientes\n7. Ver tareas UNKNOWN\n8. Salir"
            )
            option = self.ask("Opción: ").strip()
            if option == "8":
                return
            if option == "1":
                self.select()
            elif option == "2":
                self.create_onboard()
            elif option == "3":
                self.onboard_selected()
            elif option == "4":
                self.healthcheck()
            elif option == "5":
                self.prepare_post()
            elif option == "6":
                self.show_tasks(False)
            elif option == "7":
                self.show_tasks(True)
            else:
                self.say("Opción inválida.")

    def create_onboard(self) -> None:
        username = self.ask("Usuario/handle X: ")
        account = self.runtime.database.find_account_by_platform_username(
            SocialPlatform.X, username
        )
        if account is None:
            alias = self.ask("Alias opcional: ").strip() or username
            persona = Persona(alias=alias)
            self.runtime.database.save_persona(persona)
            account = SocialAccount(
                persona_id=persona.id, platform=SocialPlatform.X, username=username
            )
            self.runtime.database.save_social_account(account)
        else:
            self.say("La cuenta ya existe; se reutilizará.")
        self.onboard(account)

    def onboard_selected(self) -> None:
        account = self.select()
        if account:
            self.onboard(account)

    def onboard(self, account) -> None:
        runner = self.runtime.onboarding
        runner.state = OnboardingQueueState(account_ids=[account.id])
        runner.store.save(runner.state)
        report = asyncio.run(runner.start())
        current = self.runtime.database.get_social_account(account.id)
        self.say(
            f"Onboarding terminado: {report.successful_logins} válido. Sesión: {current.session_status.value if current else 'unknown'}; salud: {current.health_status.value if current else 'unknown'}; cuarentena: {'SI' if current and current.quarantined else 'NO'}"
        )

    def healthcheck(self) -> None:
        account = self.select()
        if not account:
            return
        result = asyncio.run(self.runtime.health.check_account_health(account.id))
        current = self.runtime.database.get_social_account(account.id)
        self.say(
            f"Cuenta: @{account.username}\nSession: {result.session_status}\nHealth: {result.health_status}\nQuarantine: {'SI' if current and current.quarantined else 'NO'}\nIssue: {result.issue_type or 'ninguno'}\nLast check: {result.checked_at.isoformat()}"
        )

    def prepare_post(self) -> None:
        account = self.select()
        if not account:
            return
        text = self.ask("Texto del post: ").strip()
        if not text:
            self.say("El texto no puede estar vacío.")
            return
        campaign = Campaign(name=f"Operator post @{account.username}")
        self.runtime.campaigns.save_campaign(campaign)
        narrative = Narrative(campaign.id, "Post manual", "Contenido ingresado por operador")
        self.runtime.campaigns.save_narrative(narrative)
        brief = ContentBrief(narrative.id, NarrativeRole.ANALYSIS, SocialPlatform.X, "Post manual")
        self.runtime.campaigns.save_content_brief(brief)
        assignment = PlannedAssignment(
            account.id, uuid4(), brief.id, narrative.id, NarrativeRole.ANALYSIS
        )
        self.runtime.campaigns.save_assignment_plan(AssignmentPlan(campaign.id, [assignment]))
        builder = ContentContextBuilder(self.runtime.database, self.runtime.campaigns)
        request = builder.build(assignment.id, content_type=ContentType.POST)
        generator = ContentGenerationService(self.runtime.drafts, builder, None)  # type: ignore[arg-type]
        draft = generator.register_manual(request, text)
        self.say(f"PREVIEW\n@{account.username}: {draft.text}")
        self.runtime.review.approve(draft.id)
        task = self.runtime.linker.link(draft.id, capability=Capability.POST)
        if not self.runtime.config.execution_enabled:
            self.say("Modo SAFE. La tarea quedó preparada pero no se publicará.")
            return
        if (
            self.ask("⚠ ESTA ACCIÓN PUBLICARÁ REALMENTE EN X\nEscribí PUBLICAR para continuar: ")
            != "PUBLICAR"
        ):
            self.say("Publicación cancelada.")
            return
        result = asyncio.run(self.runtime.dispatcher.run_once_async(self.runtime.config.worker_id))
        current = result.task or task
        if current.status is TaskStatus.COMPLETED:
            self.say(
                f"✅ PUBLICADO\nPost ID: {current.external_id}\nURL: {current.external_url}\nTask: COMPLETED"
            )
        elif current.status is TaskStatus.UNKNOWN_EXTERNAL_STATE:
            self.say(
                "⚠ RESULTADO NO CONFIRMADO\nLa acción pudo haberse enviado. No habrá retry automático."
            )
        else:
            self.say(
                f"❌ NO PUBLICADO\nMotivo: {current.last_error or current.block_reason or current.status.value}"
            )

    def show_tasks(self, unknown: bool) -> None:
        from ..campaign.models import TaskStatus

        wanted = (
            {TaskStatus.UNKNOWN_EXTERNAL_STATE}
            if unknown
            else {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.PLANNED, TaskStatus.RUNNING}
        )
        shown = False
        for task in self.runtime.queue.list_tasks():
            if task.status in wanted:
                shown = True
                self.say(
                    f"{task.id} | cuenta {task.account_id} | {task.capability.value} | {task.status.value} | {task.block_reason or '-'} | rev {task.revision}"
                )
        if not shown:
            self.say("No hay tareas para mostrar.")


def main() -> None:
    try:
        runtime = build_runtime()
        status = asyncio.run(runtime.camoufox_status())
        if status is None or not status.available:
            print(
                "\nCamoufox Profile Manager no está disponible.\n\nVerificá que el servicio esté iniciado."
            )
            return
        print(
            f"Camoufox: ONLINE | Social Pod DB: OK | Execution: {'REAL' if runtime.config.execution_enabled else 'SAFE'}"
        )
        XOperator(runtime).run()
    except Exception:
        if os.getenv("SOCIAL_POD_DEBUG", "").lower() == "true":
            raise
        print("\nNo se pudo iniciar Social Pod. Verificá la configuración e intentá nuevamente.")


if __name__ == "__main__":
    main()
