"""Small operator-facing shell. It never asks for credentials in the terminal."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Callable

from ..domain import SocialPlatform
from ..persistence import SocialPodDatabase


@dataclass
class XOperator:
    database: SocialPodDatabase
    onboard: Callable[[object], object] | None = None
    healthcheck: Callable[[object], object] | None = None
    smoke_post: Callable[[object, str], object] | None = None
    input_fn: Callable[[str], str] = input
    output_fn: Callable[[str], None] = print

    def accounts(self):
        return [item for item in self.database.list_healthcheck_candidates() if item.platform is SocialPlatform.X]

    def show_accounts(self) -> list[object]:
        accounts = self.accounts()
        if not accounts:
            self.output_fn("No hay cuentas X registradas.")
        for index, account in enumerate(accounts, 1):
            self.output_fn(f"{index}. @{account.username}\n   session: {account.session_status.value.upper()}\n   health: {account.health_status.value.upper()}\n   quarantine: {'SI' if account.quarantined else 'NO'}\n   perfil: {'SI' if account.upstream_profile_id else 'NO'}")
        return accounts

    def select_account(self):
        accounts = self.show_accounts()
        if not accounts:
            return None
        try:
            return accounts[int(self.input_fn("Elegí una cuenta: ")) - 1]
        except (ValueError, IndexError):
            self.output_fn("Selección inválida.")
            return None

    def run(self) -> None:
        while True:
            self.output_fn("\nSOCIAL POD — X OPERATOR\n1. Ver cuentas X\n2. Onboardear nueva cuenta\n3. Reautenticar cuenta\n4. Ejecutar healthcheck\n5. Publicar post de prueba\n6. Salir")
            option = self.input_fn("Opción: ").strip()
            if option == "6": return
            if option == "1": self.show_accounts(); continue
            account = self.select_account()
            if account is None: continue
            if option in {"2", "3"}: self._call(self.onboard, account, "Onboarding")
            elif option == "4": self._call(self.healthcheck, account, "Healthcheck")
            elif option == "5": self._post(account)
            else: self.output_fn("Opción inválida.")

    def _post(self, account) -> None:
        text = self.input_fn("Texto del post de prueba: ").strip()
        self.output_fn(f"\nPREVIEW\n@{account.username}: {text}\nADVERTENCIA: esto publicará en X.")
        if self.input_fn("Escribí PUBLICAR para continuar: ") != "PUBLICAR":
            self.output_fn("Publicación cancelada."); return
        self._call(self.smoke_post, account, "Smoke test", text)

    def _call(self, action, account, label: str, *args) -> None:
        if action is None:
            self.output_fn(f"{label} requiere que la aplicación inyecte su gateway Camoufox.")
            return
        result = action(account, *args)
        if hasattr(result, "__await__"): result = asyncio.run(result)
        self.output_fn(str(result))


def main() -> None:
    database = SocialPodDatabase(); database.initialize()
    XOperator(database).run()


if __name__ == "__main__": main()
