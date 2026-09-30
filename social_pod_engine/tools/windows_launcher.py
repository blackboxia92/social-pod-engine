"""Windows-friendly startup orchestration for the local Social Pod operator."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import urlopen


DEFAULT_CAMOUFOX_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_START_TIMEOUT_SECONDS = 30.0
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class LauncherError(RuntimeError):
    """A concise error that can be shown directly to an operator."""


@dataclass(frozen=True, slots=True)
class LauncherConfig:
    camoufox_base_url: str
    timeout_seconds: float

    @classmethod
    def from_environment(cls) -> "LauncherConfig":
        raw_timeout = os.getenv(
            "SOCIAL_POD_CPM_START_TIMEOUT", str(DEFAULT_START_TIMEOUT_SECONDS)
        )
        try:
            timeout_seconds = float(raw_timeout)
        except ValueError as exc:
            raise LauncherError("El tiempo de espera configurado no es válido.") from exc
        if timeout_seconds <= 0:
            raise LauncherError("El tiempo de espera debe ser mayor que cero.")
        return cls(
            camoufox_base_url=os.getenv("CAMOUFOX_BASE_URL", DEFAULT_CAMOUFOX_BASE_URL),
            timeout_seconds=timeout_seconds,
        )


def repository_root() -> Path:
    """Return the checkout root even when Windows launched the cmd from elsewhere."""
    return Path(__file__).resolve().parents[2]


def health_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/health"


def parse_local_base_url(base_url: str) -> tuple[str, int]:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LauncherError("CAMOUFOX_BASE_URL no es una dirección HTTP válida.")
    if parsed.hostname not in LOCAL_HOSTS:
        raise LauncherError(
            "Camoufox no responde en la dirección configurada. "
            "El inicio automático solo puede abrir un servicio local."
        )
    try:
        port = parsed.port or 8000
    except ValueError as exc:
        raise LauncherError("CAMOUFOX_BASE_URL tiene un puerto no válido.") from exc
    return parsed.hostname, port


def camoufox_is_online(base_url: str, *, timeout: float = 1.5) -> bool:
    """Return true only for CPM's healthy response; failures are normal while starting."""
    try:
        with urlopen(health_url(base_url), timeout=timeout) as response:  # noqa: S310 - local URL is explicit configuration
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "healthy"
    except (HTTPError, URLError, OSError, ValueError, json.JSONDecodeError):
        return False


def wait_for_camoufox(
    base_url: str,
    timeout_seconds: float,
    *,
    check: Callable[[str], bool] = camoufox_is_online,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    deadline = clock() + timeout_seconds
    while clock() < deadline:
        if check(base_url):
            return True
        sleep(0.5)
    return check(base_url)


def camoufox_command(python: str, base_url: str) -> list[str]:
    host, port = parse_local_base_url(base_url)
    return [
        python,
        "-m",
        "camoufox_pm.cli",
        "--no-browser",
        "--host",
        host,
        "--port",
        str(port),
    ]


def start_camoufox(python: str, base_url: str, *, cwd: Path) -> None:
    """Start CPM detached so it remains ready for the next operator session."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        subprocess.Popen(
            camoufox_command(python, base_url),
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    except OSError as exc:
        raise LauncherError("No se pudo iniciar Camoufox Profile Manager.") from exc


def ensure_camoufox_online(
    config: LauncherConfig,
    *,
    python: str,
    cwd: Path,
    check: Callable[[str], bool] = camoufox_is_online,
    start: Callable[[str, str], None] | None = None,
    wait: Callable[[str, float], bool] | None = None,
) -> bool:
    """Reuse an existing CPM instance or start exactly one local instance."""
    if check(config.camoufox_base_url):
        return False

    # Validate before creating a process. A remote custom URL may be reused, but
    # launching an arbitrary remote machine is never the launcher's job.
    parse_local_base_url(config.camoufox_base_url)
    start = start or (lambda executable, url: start_camoufox(executable, url, cwd=cwd))
    wait = wait or (
        lambda url, seconds: wait_for_camoufox(url, seconds, check=check)
    )
    start(python, config.camoufox_base_url)
    if not wait(config.camoufox_base_url, config.timeout_seconds):
        raise LauncherError("No se pudo iniciar Camoufox Profile Manager.")
    return True


def main() -> int:
    try:
        config = LauncherConfig.from_environment()
        started_here = ensure_camoufox_online(
            config,
            python=sys.executable,
            cwd=repository_root(),
        )
    except LauncherError as exc:
        print(f"\n{exc}")
        return 1

    if started_here:
        print("Camoufox Profile Manager: ONLINE (iniciado para esta sesión)")
    else:
        print("Camoufox Profile Manager: ONLINE")
    print("Social Pod DB: OK")
    print()
    completed = subprocess.run(
        [sys.executable, "-m", "social_pod_engine.tools.x_operator"],
        cwd=repository_root(),
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
