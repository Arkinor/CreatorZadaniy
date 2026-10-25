"""Windows Service Control Manager operations."""

from __future__ import annotations

import logging
import time
import winreg
from dataclasses import dataclass, field

import pywintypes
import win32service

from . import config_store, paths, winapi
from .models import (
    ACCOUNT_CUSTOM,
    ACCOUNT_LOCAL_SERVICE,
    ACCOUNT_NETWORK_SERVICE,
    ACCOUNT_SYSTEM,
    START_AUTO,
    START_DELAYED,
    START_DISABLED,
    START_MANUAL,
    ServiceConfig,
    utc_now,
    validate_service_name,
)

log = logging.getLogger(__name__)

SERVICE_NAME = "ServiceCreatorHost"
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_DISABLED = 1058
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_CANNOT_ACCEPT_CTRL = 1061
ERROR_SERVICE_REQUEST_TIMEOUT = 1053
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_MARKED_FOR_DELETE = 1072
ERROR_SERVICE_EXISTS = 1073
ERROR_SERVICE_DIFFERENT_PROCESS = 1079
ERROR_FAILED_SERVICE_CONTROLLER_CONNECT = 1063

STATE_LABELS = {
    None: "Не зарегистрирована",
    win32service.SERVICE_STOPPED: "Остановлена",
    win32service.SERVICE_START_PENDING: "Запускается…",
    win32service.SERVICE_STOP_PENDING: "Останавливается…",
    win32service.SERVICE_RUNNING: "Работает",
    win32service.SERVICE_CONTINUE_PENDING: "Продолжается…",
    win32service.SERVICE_PAUSE_PENDING: "Приостанавливается…",
    win32service.SERVICE_PAUSED: "Приостановлена",
}

START_CONST = {
    START_AUTO: win32service.SERVICE_AUTO_START,
    START_DELAYED: win32service.SERVICE_AUTO_START,
    START_MANUAL: win32service.SERVICE_DEMAND_START,
    START_DISABLED: win32service.SERVICE_DISABLED,
}

START_CONST_TO_KIND = {
    win32service.SERVICE_AUTO_START: START_AUTO,
    win32service.SERVICE_DEMAND_START: START_MANUAL,
    win32service.SERVICE_DISABLED: START_DISABLED,
    win32service.SERVICE_BOOT_START: START_MANUAL,
    win32service.SERVICE_SYSTEM_START: START_MANUAL,
}

SYSTEM_ACCOUNTS = {
    ACCOUNT_SYSTEM: "LocalSystem",
    ACCOUNT_LOCAL_SERVICE: "NT AUTHORITY\\LocalService",
    ACCOUNT_NETWORK_SERVICE: "NT AUTHORITY\\NetworkService",
}

_ACTION_TO_CONST = {
    "none": win32service.SC_ACTION_NONE,
    "restart": win32service.SC_ACTION_RESTART,
    "reboot": win32service.SC_ACTION_REBOOT,
    "command": win32service.SC_ACTION_RUN_COMMAND,
}


@dataclass
class ServiceDetails:
    name: str
    display_name: str = ""
    description: str = ""
    image_path: str = ""
    start_type: str = START_MANUAL
    delayed: bool = False
    account: str = ""
    dependencies: list[str] = field(default_factory=list)
    state: int | None = None
    exit_code: int = 0
    service_exit_code: int = 0
    service_type: int = win32service.SERVICE_WIN32_OWN_PROCESS
    config_loaded: bool = False


@dataclass
class ServiceRow:
    name: str
    display_name: str
    state: int | None
    config: ServiceConfig | None
    registered: bool = True
    missing_config: bool = False
    details: ServiceDetails | None = None

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, "Неизвестно")

    @property
    def start_label(self) -> str:
        if self.config is None:
            return ""
        if self.config.start_type == START_DELAYED and self.details:
            if not self.details.delayed:
                return "Автоматически"
        return {
            START_AUTO: "Автоматически",
            START_DELAYED: "Автоматически (задержка)",
            START_MANUAL: "Вручную",
            START_DISABLED: "Отключена",
        }.get(self.config.start_type, "")

    @property
    def account_label(self) -> str:
        if self.config is None:
            return ""
        if self.config.account_type == ACCOUNT_SYSTEM:
            return "LocalSystem"
        if self.config.account_type == ACCOUNT_LOCAL_SERVICE:
            return "LocalService"
        if self.config.account_type == ACCOUNT_NETWORK_SERVICE:
            return "NetworkService"
        return self.config.account or "Пользователь"


def _start_const(config: ServiceConfig) -> int:
    return START_CONST.get(config.start_type, win32service.SERVICE_AUTO_START)


def _is_delayed(config: ServiceConfig) -> bool:
    return config.start_type == START_DELAYED


def _account_name(config: ServiceConfig) -> str:
    if config.account_type in SYSTEM_ACCOUNTS:
        return SYSTEM_ACCOUNTS[config.account_type]
    return winapi.normalize_account(config.account)


def account_kind_and_name(service_account: str | None) -> tuple[str, str]:
    value = (service_account or "").strip()
    lowered = value.casefold()
    if not value or lowered in {"localsystem", "nt authority\\system", "system"}:
        return ACCOUNT_SYSTEM, ""
    if lowered.endswith("localservice"):
        return ACCOUNT_LOCAL_SERVICE, ""
    if lowered.endswith("networkservice"):
        return ACCOUNT_NETWORK_SERVICE, ""
    return ACCOUNT_CUSTOM, value


def _open_scm(access: int = win32service.SC_MANAGER_ALL_ACCESS):
    if not winapi.is_admin():
        raise RuntimeError(
            "Для управления службами нужны права администратора. "
            "Перезапустите программу от имени администратора."
        )
    return win32service.OpenSCManager(None, None, access)


def _open_service(hscm, name: str, access: int):
    return win32service.OpenService(hscm, name, access)


def _close(handle) -> None:
    try:
        win32service.CloseServiceHandle(handle)
    except Exception:
        pass


def service_exists(name: str) -> bool:
    try:
        hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    except pywintypes.error:
        return False
    try:
        try:
            hs = _open_service(hscm, name, win32service.SERVICE_QUERY_STATUS)
        except pywintypes.error as exc:
            if exc.winerror == ERROR_SERVICE_DOES_NOT_EXIST:
                return False
            raise
        _close(hs)
        return True
    finally:
        _close(hscm)


def _registry_image_path(name: str) -> str:
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{name}",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as key:
            value, _type = winreg.QueryValueEx(key, "ImagePath")
            return str(value)
    except OSError:
        return ""


def query_details(name: str) -> ServiceDetails:
    details = ServiceDetails(name=name)
    hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    hs = None
    try:
        hs = _open_service(
            hscm,
            name,
            win32service.SERVICE_QUERY_CONFIG | win32service.SERVICE_QUERY_STATUS,
        )
        try:
            raw = win32service.QueryServiceConfig(hs)
            details.service_type = raw[0]
            details.start_type = START_CONST_TO_KIND.get(raw[1], START_MANUAL)
            details.image_path = raw[3] or ""
            details.dependencies = list(raw[6] or [])
            details.account = raw[7] or ""
            details.display_name = raw[8] or name
            details.config_loaded = True
        except pywintypes.error:
            pass

        try:
            details.description = win32service.QueryServiceConfig2(
                hs, win32service.SERVICE_CONFIG_DESCRIPTION
            ) or ""
        except pywintypes.error:
            pass
        try:
            details.delayed = bool(
                win32service.QueryServiceConfig2(
                    hs, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO
                )
            )
        except pywintypes.error:
            details.delayed = False

        try:
            status = win32service.QueryServiceStatus(hs)
            details.state = status[1]
            details.exit_code = status[3]
            details.service_exit_code = status[4]
        except pywintypes.error:
            pass
    finally:
        if hs is not None:
            _close(hs)
        _close(hscm)
    return details


def query_state(name: str) -> int:
    hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    hs = None
    try:
        hs = _open_service(hscm, name, win32service.SERVICE_QUERY_STATUS)
        return win32service.QueryServiceStatus(hs)[1]
    finally:
        if hs is not None:
            _close(hs)
        _close(hscm)


def poll_states() -> dict[str, int]:
    """Cheap status refresh for the UI timer."""
    hscm = win32service.OpenSCManager(
        None, None, win32service.SC_MANAGER_ENUMERATE_SERVICE
    )
    try:
        raw = win32service.EnumServicesStatus(
            hscm, win32service.SERVICE_WIN32, win32service.SERVICE_STATE_ALL
        )
        return {name: status[1] for name, _display, status in raw}
    finally:
        _close(hscm)


def apply_config_from_details(
    config: ServiceConfig, details: ServiceDetails, missing_config: bool = False
) -> ServiceConfig:
    """Merge values that are authoritative in the SCM into a config object."""
    if not config.display_name:
        config.display_name = details.display_name or config.name
    if details.start_type == START_AUTO:
        config.start_type = START_DELAYED if details.delayed else START_AUTO
    else:
        config.start_type = details.start_type
    if details.account:
        kind, account = account_kind_and_name(details.account)
        config.account_type = kind
        config.account = account
    if details.config_loaded:
        config.dependencies = list(details.dependencies)
    if missing_config:
        config.description = details.description or ""
    return config


def list_services() -> list[ServiceRow]:
    """All services managed by this app, registered or only stored on disk."""
    try:
        hscm = win32service.OpenSCManager(
            None, None, win32service.SC_MANAGER_ENUMERATE_SERVICE
        )
    except pywintypes.error as exc:
        raise RuntimeError(winapi.format_error(exc, "Не удалось открыть диспетчер служб")) from exc

    registered: dict[str, tuple[str, tuple]] = {}
    try:
        raw = win32service.EnumServicesStatus(
            hscm, win32service.SERVICE_WIN32, win32service.SERVICE_STATE_ALL
        )
        for name, display_name, status in raw:
            registered[name] = (display_name, status)
    finally:
        _close(hscm)

    configs = config_store.load_all()
    rows: list[ServiceRow] = []

    for name, (scm_display, status) in registered.items():
        config = configs.pop(name, None)
        missing_config = False
        if config is None:
            image_path = _registry_image_path(name)
            if not paths.is_our_image_path(image_path):
                continue
            missing_config = True
            config = ServiceConfig(name=name, display_name=scm_display or name)
            config.ensure_log_path()
        try:
            details = query_details(name)
        except pywintypes.error:
            details = ServiceDetails(name=name, display_name=scm_display)
            details.state = status[1]
        apply_config_from_details(config, details, missing_config=missing_config)
        details.state = status[1]
        rows.append(
            ServiceRow(
                name=name,
                display_name=details.display_name or config.display_name or name,
                state=status[1],
                config=config,
                registered=True,
                missing_config=missing_config,
                details=details,
            )
        )

    for name, config in configs.items():
        rows.append(
            ServiceRow(
                name=name,
                display_name=config.effective_display_name,
                state=None,
                config=config,
                registered=False,
                missing_config=False,
                details=None,
            )
        )

    rows.sort(key=lambda row: (row.display_name.casefold(), row.name.casefold()))
    return rows


def load_effective_config(name: str) -> ServiceConfig:
    config = config_store.load_config(name)
    if config is None:
        details = query_details(name)
        config = ServiceConfig(name=name, display_name=details.display_name or name)
        config.description = details.description
        config.ensure_log_path()
        apply_config_from_details(config, details)
        return config
    try:
        details = query_details(name)
        apply_config_from_details(config, details)
    except pywintypes.error:
        pass
    return config


def _apply_config2(hs, config: ServiceConfig) -> list[str]:
    warnings: list[str] = []
    try:
        win32service.ChangeServiceConfig2(
            hs, win32service.SERVICE_CONFIG_DESCRIPTION, config.description or ""
        )
    except pywintypes.error as exc:
        warnings.append(winapi.format_error(exc, "Не удалось сохранить описание"))

    if _is_delayed(config):
        try:
            win32service.ChangeServiceConfig2(
                hs, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO, True
            )
        except pywintypes.error as exc:
            warnings.append(winapi.format_error(exc, "Не удалось задать задержку запуска"))
    else:
        try:
            win32service.ChangeServiceConfig2(
                hs, win32service.SERVICE_CONFIG_DELAYED_AUTO_START_INFO, False
            )
        except pywintypes.error:
            pass

    actions = []
    for item in config.failure_actions:
        actions.append((_ACTION_TO_CONST.get(item.action, win32service.SC_ACTION_NONE), max(0, item.delay_seconds) * 1000))
    if not actions:
        actions = [(win32service.SC_ACTION_NONE, 0)]
    reboot_msg = "Служба была перезапущена после сбоя." if any(a[0] == win32service.SC_ACTION_REBOOT for a in actions) else ""
    command = (
        config.failure_command.strip()
        if any(a[0] == win32service.SC_ACTION_RUN_COMMAND for a in actions)
        else ""
    )
    try:
        win32service.ChangeServiceConfig2(
            hs,
            win32service.SERVICE_CONFIG_FAILURE_ACTIONS,
            {
                "ResetPeriod": max(0, config.failure_reset_days) * 86400,
                "RebootMsg": reboot_msg,
                "Command": command,
                "Actions": actions,
            },
        )
    except pywintypes.error as exc:
        warnings.append(winapi.format_error(exc, "Не удалось задать действия при сбое"))

    try:
        win32service.ChangeServiceConfig2(
            hs, win32service.SERVICE_CONFIG_FAILURE_ACTIONS_FLAG, bool(config.failure_flag)
        )
    except pywintypes.error:
        pass
    return warnings


def _prepare_account(config: ServiceConfig, password: str | None) -> tuple[str, str | None]:
    name = _account_name(config)
    if config.account_type == ACCOUNT_CUSTOM:
        if not config.account.strip():
            raise ValueError("Укажите учётную запись пользователя.")
        if config.grant_logon_right:
            try:
                winapi.grant_logon_as_service(config.account)
            except Exception as exc:  # noqa: BLE001 - friendly message for any failure
                raise RuntimeError(
                    winapi.format_error(
                        exc,
                        "Не удалось разрешить учётной записи вход в качестве службы",
                    )
                ) from exc
        return name, password
    return name, None


def create_service(config: ServiceConfig, password: str | None) -> list[str]:
    error = validate_service_name(config.name)
    if error:
        raise ValueError(error)
    if service_exists(config.name):
        raise RuntimeError(f"Служба «{config.name}» уже существует.")
    if not config.target.strip():
        raise ValueError("Укажите файл, который должна запускать служба.")
    if config.account_type == ACCOUNT_CUSTOM and not password:
        raise ValueError("Для пользовательской учётной записи нужен пароль.")

    paths.prepare_runtime_directories()
    image_path = paths.host_image_path()
    account, account_password = _prepare_account(config, password)
    config.ensure_log_path()

    hscm = _open_scm(win32service.SC_MANAGER_ALL_ACCESS)
    hs = None
    created = False
    try:
        try:
            hs = win32service.CreateService(
                hscm,
                config.name,
                config.effective_display_name,
                win32service.SERVICE_ALL_ACCESS,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                _start_const(config),
                win32service.SERVICE_ERROR_NORMAL,
                image_path,
                None,
                0,
                tuple(config.dependencies) or None,
                account,
                account_password,
            )
            created = True
        except pywintypes.error as exc:
            raise RuntimeError(
                winapi.format_error(exc, "Не удалось создать службу")
            ) from exc

        warnings = _apply_config2(hs, config)
        _close(hs)
        hs = None
        config.created_at = config.created_at or utc_now()
        try:
            config_store.save_config(config)
        except OSError as exc:
            raise RuntimeError(
                f"Служба создана, но не удалось сохранить её настройки: {exc}"
            ) from exc
        return warnings
    except Exception:
        if hs is not None:
            _close(hs)
        # Do not leave a half-configured service behind. Only remove a service
        # this call has actually created.
        if created:
            try:
                _remove_service_raw(hscm, config.name)
            except Exception:
                pass
        raise
    finally:
        _close(hscm)


def update_service(
    config: ServiceConfig, password: str | None, account_changed: bool = False
) -> list[str]:
    error = validate_service_name(config.name)
    if error:
        raise ValueError(error)
    if not config.target.strip():
        raise ValueError("Укажите файл, который должна запускать служба.")
    if config.account_type == ACCOUNT_CUSTOM and account_changed and not password:
        raise ValueError("При смене учётной записи укажите пароль.")

    paths.prepare_runtime_directories()
    image_path = paths.host_image_path()
    account, account_password = _prepare_account(config, password)
    config.ensure_log_path()

    hscm = _open_scm()
    hs = None
    try:
        try:
            hs = _open_service(hscm, config.name, win32service.SERVICE_ALL_ACCESS)
        except pywintypes.error as exc:
            if exc.winerror == ERROR_SERVICE_DOES_NOT_EXIST:
                _close(hscm)
                hscm = None
                return create_service(config, password)
            raise RuntimeError(winapi.format_error(exc, "Не удалось открыть службу")) from exc

        try:
            win32service.ChangeServiceConfig(
                hs,
                win32service.SERVICE_WIN32_OWN_PROCESS,
                _start_const(config),
                win32service.SERVICE_ERROR_NORMAL,
                image_path,
                None,
                0,
                tuple(config.dependencies) if config.dependencies else ("",),
                account,
                account_password,
                config.effective_display_name,
            )
        except pywintypes.error as exc:
            raise RuntimeError(
                winapi.format_error(exc, "Не удалось применить настройки службы")
            ) from exc

        warnings = _apply_config2(hs, config)
        config_store.save_config(config)
        return warnings
    finally:
        if hs is not None:
            _close(hs)
        if hscm is not None:
            _close(hscm)


def _remove_service_raw(hscm, name: str) -> None:
    hs = None
    try:
        hs = _open_service(hscm, name, win32service.SERVICE_ALL_ACCESS)
        win32service.DeleteService(hs)
    finally:
        if hs is not None:
            _close(hs)


def delete_service(name: str, stop_if_running: bool = True) -> None:
    hscm = _open_scm()
    hs = None
    try:
        try:
            hs = _open_service(
                hscm,
                name,
                win32service.SERVICE_ALL_ACCESS | win32service.SERVICE_STOP,
            )
        except pywintypes.error as exc:
            if exc.winerror == ERROR_SERVICE_DOES_NOT_EXIST:
                config_store.delete_config(name)
                return
            raise RuntimeError(winapi.format_error(exc, "Не удалось открыть службу")) from exc

        try:
            status = win32service.QueryServiceStatus(hs)
            if status[1] in (
                win32service.SERVICE_RUNNING,
                win32service.SERVICE_START_PENDING,
                win32service.SERVICE_STOP_PENDING,
                win32service.SERVICE_PAUSED,
            ):
                if not stop_if_running:
                    raise RuntimeError("Сначала остановите службу.")
                _stop_handle(hs, name, timeout=30)
            win32service.DeleteService(hs)
        except pywintypes.error as exc:
            if exc.winerror == ERROR_SERVICE_MARKED_FOR_DELETE:
                config_store.delete_config(name)
                return
            raise RuntimeError(
                winapi.format_error(exc, "Не удалось удалить службу")
            ) from exc
    finally:
        if hs is not None:
            _close(hs)
        if hscm is not None:
            _close(hscm)
    config_store.delete_config(name)


def _stop_handle(hs, name: str, timeout: float = 30) -> None:
    try:
        win32service.ControlService(hs, win32service.SERVICE_CONTROL_STOP)
    except pywintypes.error as exc:
        if exc.winerror in (
            ERROR_SERVICE_NOT_ACTIVE,
            ERROR_SERVICE_CANNOT_ACCEPT_CTRL,
            ERROR_SERVICE_REQUEST_TIMEOUT,
        ):
            pass
        else:
            raise
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = win32service.QueryServiceStatus(hs)
        if status[1] == win32service.SERVICE_STOPPED:
            return
        time.sleep(0.3)
    raise RuntimeError(
        f"Служба «{name}» не остановилась за {int(timeout)} секунд."
    )


def stop_service(name: str, timeout: float = 30) -> None:
    hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    hs = None
    try:
        hs = _open_service(
            hscm,
            name,
            win32service.SERVICE_STOP | win32service.SERVICE_QUERY_STATUS,
        )
        status = win32service.QueryServiceStatus(hs)
        if status[1] == win32service.SERVICE_STOPPED:
            return
        _stop_handle(hs, name, timeout=timeout)
    except pywintypes.error as exc:
        if exc.winerror == ERROR_SERVICE_NOT_ACTIVE:
            return
        raise RuntimeError(winapi.format_error(exc, "Не удалось остановить службу")) from exc
    finally:
        if hs is not None:
            _close(hs)
        _close(hscm)


def start_service(name: str, timeout: float = 25) -> None:
    hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    hs = None
    try:
        hs = _open_service(
            hscm,
            name,
            win32service.SERVICE_START
            | win32service.SERVICE_QUERY_STATUS
            | win32service.SERVICE_STOP,
        )
        status = win32service.QueryServiceStatus(hs)
        if status[1] == win32service.SERVICE_RUNNING:
            return
        if status[1] == win32service.SERVICE_STOP_PENDING:
            _wait_state(hs, win32service.SERVICE_STOPPED, 30)
        try:
            win32service.StartService(hs, None)
        except pywintypes.error as exc:
            if exc.winerror == ERROR_SERVICE_ALREADY_RUNNING:
                return
            raise RuntimeError(winapi.format_error(exc, "Не удалось запустить службу")) from exc

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = win32service.QueryServiceStatus(hs)
            state = status[1]
            if state == win32service.SERVICE_RUNNING:
                return
            if state == win32service.SERVICE_STOPPED:
                code = status[4] or status[3]
                hint = ""
                if code:
                    hint = f" Код завершения: {code}."
                raise RuntimeError(
                    "Служба остановилась сразу после запуска."
                    + hint
                    + " Откройте журнал службы, чтобы узнать причину."
                )
            time.sleep(0.25)
        raise RuntimeError(
            f"Служба не перешла в состояние «Работает» за {int(timeout)} секунд. "
            "Проверьте журнал службы."
        )
    finally:
        if hs is not None:
            _close(hs)
        _close(hscm)


def _wait_state(hs, desired: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if win32service.QueryServiceStatus(hs)[1] == desired:
            return
        time.sleep(0.25)


def restart_service(name: str, timeout: float = 30) -> None:
    state = query_state(name)
    if state in (
        win32service.SERVICE_RUNNING,
        win32service.SERVICE_START_PENDING,
        win32service.SERVICE_STOP_PENDING,
        win32service.SERVICE_PAUSED,
    ):
        stop_service(name, timeout=timeout)
    start_service(name)
