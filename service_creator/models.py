"""Service configuration model and small validation helpers."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from .paths import APP_VERSION, default_log_path

SCHEMA_VERSION = 1

START_AUTO = "auto"
START_DELAYED = "delayed"
START_MANUAL = "manual"
START_DISABLED = "disabled"

START_TYPE_LABELS = {
    START_AUTO: "Автоматически",
    START_DELAYED: "Автоматически (с задержкой)",
    START_MANUAL: "Вручную",
    START_DISABLED: "Отключена",
}
START_TYPE_BY_LABEL = {value: key for key, value in START_TYPE_LABELS.items()}

ACCOUNT_SYSTEM = "system"
ACCOUNT_LOCAL_SERVICE = "local_service"
ACCOUNT_NETWORK_SERVICE = "network_service"
ACCOUNT_CUSTOM = "custom"

ACCOUNT_TYPE_LABELS = {
    ACCOUNT_SYSTEM: "Локальная система (LocalSystem)",
    ACCOUNT_LOCAL_SERVICE: "Локальная служба (LocalService)",
    ACCOUNT_NETWORK_SERVICE: "Сетевая служба (NetworkService)",
    ACCOUNT_CUSTOM: "Учётная запись пользователя",
}
ACCOUNT_TYPE_BY_LABEL = {value: key for key, value in ACCOUNT_TYPE_LABELS.items()}

LAUNCHER_AUTO = "auto"
LAUNCHER_DIRECT = "direct"
LAUNCHER_CMD = "cmd"
LAUNCHER_POWERSHELL = "powershell"
LAUNCHER_PYTHON = "python"
LAUNCHER_CSCRIPT = "cscript"
LAUNCHER_CUSTOM = "custom"

LAUNCHER_LABELS = {
    LAUNCHER_AUTO: "Автоматически (по типу файла)",
    LAUNCHER_DIRECT: "Запуск файла напрямую",
    LAUNCHER_CMD: "cmd.exe /c (пакетные файлы)",
    LAUNCHER_POWERSHELL: "PowerShell -File (скрипты .ps1)",
    LAUNCHER_PYTHON: "Python (скрипты .py)",
    LAUNCHER_CSCRIPT: "Windows Script Host (cscript)",
    LAUNCHER_CUSTOM: "Свой интерпретатор",
}
LAUNCHER_BY_LABEL = {value: key for key, value in LAUNCHER_LABELS.items()}

PRIORITY_LABELS = {
    "idle": "Низкий",
    "below": "Ниже обычного",
    "normal": "Обычный",
    "above": "Выше обычного",
    "high": "Высокий",
}
PRIORITY_BY_LABEL = {value: key for key, value in PRIORITY_LABELS.items()}

FAILURE_NONE = "none"
FAILURE_RESTART = "restart"
FAILURE_REBOOT = "reboot"
FAILURE_COMMAND = "command"

FAILURE_LABELS = {
    FAILURE_NONE: "Ничего не делать",
    FAILURE_RESTART: "Перезапустить службу",
    FAILURE_REBOOT: "Перезапустить компьютер",
    FAILURE_COMMAND: "Выполнить команду",
}
FAILURE_BY_LABEL = {value: key for key, value in FAILURE_LABELS.items()}

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
ENV_KEY_RE = re.compile(r"^[^=\s][^=]*$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, result))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "да"}
    return default


def validate_service_name(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return "Укажите внутреннее имя службы."
    if not NAME_RE.match(name):
        return (
            "Имя службы: 1–64 символа, только латинские буквы, цифры, точка, дефис "
            "и подчёркивание; начинаться должно с буквы или цифры."
        )
    if name.upper() in {"CON", "PRN", "AUX", "NUL"} or name.startswith("_"):
        return "Это имя зарезервировано системой."
    return None


def parse_env_text(text: str) -> tuple[dict[str, str], str | None]:
    result: dict[str, str] = {}
    for number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            return {}, f"Строка {number}: ожидается формат ПЕРЕМЕННАЯ=значение."
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or not ENV_KEY_RE.match(key):
            return {}, f"Строка {number}: некорректное имя переменной."
        result[key] = value.strip()
    return result, None


def format_env_text(env: dict[str, str]) -> str:
    return "\n".join(f"{key}={value}" for key, value in sorted(env.items(), key=lambda item: item[0].lower()))


def parse_dependencies(text: str) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[,;\n]+", text):
        name = chunk.strip()
        if name and name.casefold() not in seen:
            seen.add(name.casefold())
            result.append(name)
    return result


def format_dependencies(deps: Iterable[str]) -> str:
    return ", ".join(deps)


@dataclass
class FailureAction:
    action: str = FAILURE_RESTART
    delay_seconds: int = 60

    def to_dict(self) -> dict[str, Any]:
        return {"action": self.action, "delay_seconds": int(self.delay_seconds)}

    @classmethod
    def from_dict(cls, data: Any) -> "FailureAction":
        if not isinstance(data, dict):
            return cls()
        action = str(data.get("action", FAILURE_RESTART))
        if action not in FAILURE_LABELS:
            action = FAILURE_RESTART
        return cls(action=action, delay_seconds=_as_int(data.get("delay_seconds"), 60, 0, 86400))


@dataclass
class ServiceConfig:
    name: str = ""
    display_name: str = ""
    description: str = ""
    target: str = ""
    arguments: str = ""
    working_dir: str = ""
    launcher: str = LAUNCHER_AUTO
    interpreter: str = ""
    env: dict[str, str] = field(default_factory=dict)
    start_type: str = START_AUTO
    account_type: str = ACCOUNT_SYSTEM
    account: str = ""
    grant_logon_right: bool = True
    dependencies: list[str] = field(default_factory=list)
    priority: str = "normal"
    auto_restart: bool = True
    restart_on_crash_only: bool = True
    restart_delay: int = 5
    max_restarts: int = 0
    restart_window_minutes: int = 60
    failure_actions: list[FailureAction] = field(
        default_factory=lambda: [
            FailureAction(FAILURE_RESTART, 60),
            FailureAction(FAILURE_RESTART, 120),
            FailureAction(FAILURE_RESTART, 300),
        ]
    )
    failure_reset_days: int = 1
    failure_flag: bool = True
    failure_command: str = ""
    log_enabled: bool = True
    log_path: str = ""
    log_append: bool = True
    log_max_mb: int = 10
    stop_timeout: int = 10
    kill_tree: bool = True
    created_at: str = ""
    updated_at: str = ""
    app_version: str = APP_VERSION

    def __post_init__(self) -> None:
        if self.start_type not in START_TYPE_LABELS:
            self.start_type = START_AUTO
        if self.account_type not in ACCOUNT_TYPE_LABELS:
            self.account_type = ACCOUNT_SYSTEM
        if self.launcher not in LAUNCHER_LABELS:
            self.launcher = LAUNCHER_AUTO
        if self.priority not in PRIORITY_LABELS:
            self.priority = "normal"

    @property
    def effective_display_name(self) -> str:
        return self.display_name.strip() or self.name

    def ensure_log_path(self) -> None:
        if not self.log_path.strip() and self.name:
            self.log_path = default_log_path(self.name)

    def clone(self) -> "ServiceConfig":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "app_version": self.app_version,
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "target": self.target,
            "arguments": self.arguments,
            "working_dir": self.working_dir,
            "launcher": self.launcher,
            "interpreter": self.interpreter,
            "env": dict(self.env),
            "start_type": self.start_type,
            "account_type": self.account_type,
            "account": self.account,
            "grant_logon_right": bool(self.grant_logon_right),
            "dependencies": list(self.dependencies),
            "priority": self.priority,
            "auto_restart": bool(self.auto_restart),
            "restart_on_crash_only": bool(self.restart_on_crash_only),
            "restart_delay": self.restart_delay,
            "max_restarts": self.max_restarts,
            "restart_window_minutes": self.restart_window_minutes,
            "failure_actions": [item.to_dict() for item in self.failure_actions],
            "failure_reset_days": self.failure_reset_days,
            "failure_flag": bool(self.failure_flag),
            "failure_command": self.failure_command,
            "log_enabled": bool(self.log_enabled),
            "log_path": self.log_path,
            "log_append": bool(self.log_append),
            "log_max_mb": self.log_max_mb,
            "stop_timeout": self.stop_timeout,
            "kill_tree": bool(self.kill_tree),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any], name: str | None = None) -> "ServiceConfig":
        config = cls()
        if not isinstance(data, dict):
            if name:
                config.name = name
            return config

        config.name = str(name or data.get("name") or "").strip()
        config.display_name = str(data.get("display_name") or "").strip()
        config.description = str(data.get("description") or "")
        config.target = str(data.get("target") or "").strip()
        config.arguments = str(data.get("arguments") or "")
        config.working_dir = str(data.get("working_dir") or "").strip()
        config.launcher = str(data.get("launcher") or LAUNCHER_AUTO)
        config.interpreter = str(data.get("interpreter") or "").strip()

        env = data.get("env")
        if isinstance(env, dict):
            config.env = {str(key): str(value) for key, value in env.items() if str(key).strip()}
        elif isinstance(env, str):
            parsed, _ = parse_env_text(env)
            config.env = parsed

        config.start_type = str(data.get("start_type") or START_AUTO)
        config.account_type = str(data.get("account_type") or ACCOUNT_SYSTEM)
        config.account = str(data.get("account") or "").strip()
        config.grant_logon_right = _as_bool(data.get("grant_logon_right"), True)

        deps = data.get("dependencies")
        if isinstance(deps, list):
            config.dependencies = [
                str(item).strip() for item in deps if str(item).strip()
            ]
        elif isinstance(deps, str):
            config.dependencies = parse_dependencies(deps)

        config.priority = str(data.get("priority") or "normal")
        config.auto_restart = _as_bool(data.get("auto_restart"), True)
        config.restart_on_crash_only = _as_bool(data.get("restart_on_crash_only"), True)
        config.restart_delay = _as_int(data.get("restart_delay"), 5, 0, 3600)
        config.max_restarts = _as_int(data.get("max_restarts"), 0, 0, 9999)
        config.restart_window_minutes = _as_int(
            data.get("restart_window_minutes"), 60, 1, 1440
        )

        raw_actions = data.get("failure_actions")
        if isinstance(raw_actions, list) and raw_actions:
            config.failure_actions = [
                FailureAction.from_dict(item) for item in raw_actions[:5]
            ]
        config.failure_reset_days = _as_int(data.get("failure_reset_days"), 1, 0, 365)
        config.failure_flag = _as_bool(data.get("failure_flag"), True)
        config.failure_command = str(data.get("failure_command") or "").strip()

        config.log_enabled = _as_bool(data.get("log_enabled"), True)
        config.log_path = str(data.get("log_path") or "").strip()
        config.log_append = _as_bool(data.get("log_append"), True)
        config.log_max_mb = _as_int(data.get("log_max_mb"), 10, 0, 1024)
        config.stop_timeout = _as_int(data.get("stop_timeout"), 10, 1, 120)
        config.kill_tree = _as_bool(data.get("kill_tree"), True)
        config.created_at = str(data.get("created_at") or "")
        config.updated_at = str(data.get("updated_at") or "")
        config.app_version = str(data.get("app_version") or APP_VERSION)
        config.__post_init__()
        return config


def sample_config() -> ServiceConfig:
    """Used by the UI preview tooling and documentation."""
    config = ServiceConfig(
        name="MyAppService",
        display_name="Моё приложение",
        description="Запускает приложение в фоне при старте Windows.",
        target=r"C:\Program Files\MyApp\MyApp.exe",
        arguments="--background",
        start_type=START_DELAYED,
        auto_restart=True,
        restart_delay=10,
    )
    config.log_path = default_log_path(config.name)
    return config
