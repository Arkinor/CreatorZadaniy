"""Turn a service configuration into a concrete Windows command line."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import (
    LAUNCHER_AUTO,
    LAUNCHER_CSCRIPT,
    LAUNCHER_CMD,
    LAUNCHER_CUSTOM,
    LAUNCHER_DIRECT,
    LAUNCHER_POWERSHELL,
    LAUNCHER_PYTHON,
    ServiceConfig,
)

_CREATE_NO_WINDOW = 0x08000000


@dataclass
class CommandSpec:
    command: str = ""
    error: str = ""
    warning: str = ""
    launcher: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def quote(path: str) -> str:
    return f'"{path}"'


def expand_path(value: str) -> str:
    """Expand %VARIABLES% and ~ in paths typed by the user."""
    text = (value or "").strip()
    if not text:
        return ""
    return os.path.expandvars(os.path.expanduser(text))


def _with_args(executable: str, target: str, arguments: str, flags: str = "") -> str:
    parts = [quote(executable)]
    if flags:
        parts.append(flags)
    parts.append(quote(target))
    if arguments.strip():
        parts.append(arguments.strip())
    return " ".join(parts)


def _system32(name: str) -> str:
    windir = os.environ.get("SystemRoot") or r"C:\Windows"
    return str(Path(windir) / "System32" / name)


def find_python() -> str | None:
    candidates = [
        str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "py.exe"),
        shutil.which("py.exe") or "",
        shutil.which("python.exe") or "",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python" / "Python311" / "python.exe"),
        str(Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Python311" / "python.exe"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def find_powershell() -> str | None:
    candidate = _system32(r"WindowsPowerShell\v1.0\powershell.exe")
    if Path(candidate).is_file():
        return candidate
    return shutil.which("powershell.exe")


def find_cmd() -> str:
    comspec = os.environ.get("ComSpec")
    if comspec and Path(comspec).is_file():
        return comspec
    return _system32("cmd.exe")


def find_cscript() -> str | None:
    candidate = _system32("cscript.exe")
    return candidate if Path(candidate).is_file() else shutil.which("cscript.exe")


def detect_launcher(target: str) -> str:
    suffix = Path(target).suffix.lower()
    if suffix in {".exe", ".com"}:
        return LAUNCHER_DIRECT
    if suffix in {".bat", ".cmd"}:
        return LAUNCHER_CMD
    if suffix == ".ps1":
        return LAUNCHER_POWERSHELL
    if suffix in {".py", ".pyw"}:
        return LAUNCHER_PYTHON
    if suffix in {".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh"}:
        return LAUNCHER_CSCRIPT
    return LAUNCHER_DIRECT


def effective_launcher(config: ServiceConfig) -> str:
    if config.launcher == LAUNCHER_AUTO:
        return detect_launcher(config.target)
    return config.launcher


def build_command(config: ServiceConfig) -> CommandSpec:
    target = expand_path(config.target)
    arguments = config.arguments.strip()
    if not target:
        return CommandSpec(error="Не указан файл для запуска.")

    launcher = effective_launcher(config)
    warning = ""
    if config.launcher == LAUNCHER_AUTO and launcher == LAUNCHER_DIRECT:
        suffix = Path(target).suffix.lower()
        if suffix not in {".exe", ".com", ""}:
            warning = (
                "Расширение файла не распознано: он будет запущен напрямую. "
                "Если это скрипт, выберите подходящий способ запуска."
            )

    if launcher == LAUNCHER_DIRECT:
        command = f"{quote(target)}" + (f" {arguments}" if arguments else "")
        return CommandSpec(command=command, warning=warning, launcher=launcher)

    if launcher == LAUNCHER_CMD:
        command = (
            f"{quote(find_cmd())} /d /s /c \"\"{target}\""
            + (f" {arguments}" if arguments else "")
            + "\""
        )
        return CommandSpec(command=command, warning=warning, launcher=launcher)

    if launcher == LAUNCHER_POWERSHELL:
        powershell = find_powershell()
        if not powershell:
            return CommandSpec(
                error="PowerShell не найден. Укажите способ запуска вручную.",
                launcher=launcher,
            )
        command = (
            f"{quote(powershell)} -NoLogo -NoProfile -NonInteractive "
            f"-ExecutionPolicy Bypass -File {quote(target)}"
            + (f" {arguments}" if arguments else "")
        )
        return CommandSpec(command=command, warning=warning, launcher=launcher)

    if launcher == LAUNCHER_PYTHON:
        python = find_python()
        if not python:
            return CommandSpec(
                error=(
                    "Python не найден на этом компьютере. Установите Python 3 "
                    "или укажите свой интерпретатор."
                ),
                warning=warning,
                launcher=launcher,
            )
        if Path(python).name.lower() == "py.exe":
            command = f"{quote(python)} -3 -u {quote(target)}"
        else:
            command = f"{quote(python)} -u {quote(target)}"
        if arguments:
            command += f" {arguments}"
        return CommandSpec(command=command, warning=warning, launcher=launcher)

    if launcher == LAUNCHER_CSCRIPT:
        cscript = find_cscript()
        if not cscript:
            return CommandSpec(error="cscript.exe не найден.", launcher=launcher)
        command = f"{quote(cscript)} //nologo //B {quote(target)}"
        if arguments:
            command += f" {arguments}"
        return CommandSpec(command=command, warning=warning, launcher=launcher)

    if launcher == LAUNCHER_CUSTOM:
        interpreter = config.interpreter.strip()
        if not interpreter:
            return CommandSpec(error="Укажите путь к интерпретатору.", launcher=launcher)
        if not Path(interpreter).is_file():
            return CommandSpec(
                error=f"Интерпретатор не найден: {interpreter}", launcher=launcher
            )
        command = f"{quote(interpreter)} {quote(target)}"
        if arguments:
            command += f" {arguments}"
        return CommandSpec(command=command, warning=warning, launcher=launcher)

    return CommandSpec(error="Неизвестный способ запуска.", launcher=launcher)


def effective_working_dir(config: ServiceConfig) -> str:
    value = expand_path(config.working_dir)
    if value:
        return value
    target = Path(expand_path(config.target))
    return str(target.parent) if target.name else ""


def child_environment(config: ServiceConfig) -> dict[str, str]:
    environment = dict(os.environ)
    for key, value in config.env.items():
        environment[key] = value
    environment["SERVICE_CREATOR_NAME"] = config.name
    environment["SERVICE_CREATOR_TARGET"] = config.target
    return environment


def test_command(config: ServiceConfig, timeout: float = 5.0) -> tuple[bool, str]:
    """Run the configured command briefly to catch obvious mistakes.

    Only intended for the editor's diagnostic button; the child process is
    terminated after ``timeout`` seconds.
    """
    spec = build_command(config)
    if not spec.ok:
        return False, spec.error
    try:
        process = subprocess.Popen(
            spec.command,
            cwd=effective_working_dir(config) or None,
            env=child_environment(config),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError as exc:
        return False, f"Не удалось запустить: {exc}"
    try:
        output, _ = process.communicate(timeout=timeout)
        text = (output or b"").decode("utf-8", errors="replace").strip()
        if process.returncode == 0:
            return True, text or "Процесс завершился с кодом 0."
        return True, (text + f"\n\nКод завершения: {process.returncode}").strip()
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            output, _ = process.communicate(timeout=2)
        except Exception:
            output = b""
        text = (output or b"").decode("utf-8", errors="replace").strip()
        return True, "Процесс успешно запущен и был остановлен через несколько секунд.\n\n" + text
