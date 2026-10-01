from __future__ import annotations

from pathlib import Path
from urllib.error import URLError

import pytest
from social_pod_engine.tools import windows_launcher


def test_repository_root_resolves_the_checkout() -> None:
    root = windows_launcher.repository_root()
    assert (root / "pyproject.toml").is_file()
    assert root == Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "script_name", ["START_SOCIAL_POD.cmd", "START_SOCIAL_POD_REAL.cmd", "INSTALL_SOCIAL_POD.cmd"]
)
def test_windows_scripts_resolve_their_own_folder_and_prefer_py(script_name: str) -> None:
    script = (windows_launcher.repository_root() / script_name).read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in script
    assert script.index("where py") < script.index("where python")
    assert "WindowsApps" in script


def test_safe_mode_explicitly_overrides_an_inherited_real_environment() -> None:
    environment = windows_launcher.operator_environment(
        windows_launcher.LaunchMode.SAFE, base={"SOCIAL_POD_EXECUTION_ENABLED": "true"}
    )
    assert environment["SOCIAL_POD_EXECUTION_ENABLED"] == "false"


def test_real_mode_only_enables_execution_for_the_operator_child() -> None:
    original = {"UNCHANGED": "yes"}
    environment = windows_launcher.operator_environment(windows_launcher.LaunchMode.REAL, base=original)
    assert original == {"UNCHANGED": "yes"}
    assert environment["SOCIAL_POD_EXECUTION_ENABLED"] == "true"


def test_launcher_mode_defaults_to_safe_and_accepts_real() -> None:
    assert windows_launcher.parse_launch_mode([]) is windows_launcher.LaunchMode.SAFE
    assert windows_launcher.parse_launch_mode(["--mode", "real"]) is windows_launcher.LaunchMode.REAL


def test_safe_script_forces_safe_mode_and_real_script_requires_confirmation() -> None:
    root = windows_launcher.repository_root()
    safe = (root / "START_SOCIAL_POD.cmd").read_text(encoding="utf-8")
    real = (root / "START_SOCIAL_POD_REAL.cmd").read_text(encoding="utf-8")
    assert 'set "SOCIAL_POD_EXECUTION_ENABLED=false"' in safe
    assert "windows_launcher --mode safe" in safe
    assert "Escribi REAL para continuar" in real
    assert 'if /i not "%SOCIAL_POD_CONFIRMACION%"=="REAL" goto :cancelled' in real
    assert 'set "SOCIAL_POD_EXECUTION_ENABLED=true"' in real
    assert "windows_launcher --mode real" in real
    assert "Inicio cancelado. No se habilito ejecucion real." in real


def test_camoufox_is_online_requires_a_healthy_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        status = 200

        def read(self) -> bytes:
            return b'{"status":"healthy"}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(windows_launcher, "urlopen", lambda *_args, **_kwargs: Response())
    assert windows_launcher.camoufox_is_online("http://127.0.0.1:8000") is True


def test_camoufox_offline_is_a_normal_startup_state(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise URLError("offline")

    monkeypatch.setattr(windows_launcher, "urlopen", unavailable)
    assert windows_launcher.camoufox_is_online("http://127.0.0.1:8000") is False


def test_wait_for_camoufox_retries_until_online() -> None:
    outcomes = iter([False, False, True])
    now = iter([0.0, 0.0, 0.1, 0.2])

    assert windows_launcher.wait_for_camoufox(
        "http://127.0.0.1:8000",
        1,
        check=lambda _url: next(outcomes),
        sleep=lambda _seconds: None,
        clock=lambda: next(now),
    ) is True


def test_wait_for_camoufox_times_out() -> None:
    now = iter([0.0, 0.0, 0.6, 1.1, 1.1])

    assert windows_launcher.wait_for_camoufox(
        "http://127.0.0.1:8000",
        1,
        check=lambda _url: False,
        sleep=lambda _seconds: None,
        clock=lambda: next(now),
    ) is False


def test_existing_camoufox_is_reused_without_starting_another() -> None:
    starts: list[tuple[str, str]] = []
    reused = windows_launcher.ensure_camoufox_online(
        windows_launcher.LauncherConfig("http://127.0.0.1:8000", 1),
        python="python",
        cwd=Path.cwd(),
        check=lambda _url: True,
        start=lambda python, url: starts.append((python, url)),
    )
    assert reused is False
    assert starts == []


def test_offline_local_camoufox_starts_once_and_waits() -> None:
    starts: list[tuple[str, str]] = []
    started = windows_launcher.ensure_camoufox_online(
        windows_launcher.LauncherConfig("http://127.0.0.1:8100", 1),
        python="python",
        cwd=Path.cwd(),
        check=lambda _url: False,
        start=lambda python, url: starts.append((python, url)),
        wait=lambda _url, _timeout: True,
    )
    assert started is True
    assert starts == [("python", "http://127.0.0.1:8100")]


def test_offline_camoufox_reports_timeout() -> None:
    with pytest.raises(windows_launcher.LauncherError, match="No se pudo iniciar"):
        windows_launcher.ensure_camoufox_online(
            windows_launcher.LauncherConfig("http://127.0.0.1:8100", 1),
            python="python",
            cwd=Path.cwd(),
            check=lambda _url: False,
            start=lambda _python, _url: None,
            wait=lambda _url, _timeout: False,
        )


def test_camoufox_command_uses_the_configured_local_port() -> None:
    assert windows_launcher.camoufox_command("python", "http://127.0.0.1:8100") == [
        "python",
        "-m",
        "camoufox_pm.cli",
        "--no-browser",
        "--host",
        "127.0.0.1",
        "--port",
        "8100",
    ]


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (windows_launcher.LaunchMode.SAFE, "false"),
        (windows_launcher.LaunchMode.REAL, "true"),
    ],
)
def test_launch_operator_passes_the_explicit_mode_to_the_child(
    monkeypatch: pytest.MonkeyPatch, mode: windows_launcher.LaunchMode, expected: str
) -> None:
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0

    def runner(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setenv("SOCIAL_POD_EXECUTION_ENABLED", "true")
    assert windows_launcher.launch_operator(mode, runner=runner) == 0
    assert captured["kwargs"]["env"]["SOCIAL_POD_EXECUTION_ENABLED"] == expected
