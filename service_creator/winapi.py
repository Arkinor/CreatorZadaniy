"""Small Windows helpers: elevation, friendly errors and account rights."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from pathlib import Path

import pywintypes
import win32security

_CREATE_NO_WINDOW = 0x08000000


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_windows() -> bool:
    return os.name == "nt"


def normalize_account(account: str) -> str:
    account = (account or "").strip()
    if not account:
        return account
    if "@" in account and "\\" not in account:
        return account
    if "\\" not in account:
        return f".\\{account}"
    return account


def _current_python_args() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *sys.argv[1:]]
    return [sys.executable, str(Path(sys.argv[0]).resolve()), *sys.argv[1:]]


def restart_as_admin() -> bool:
    """Relaunch the app elevated. Returns False when the user cancels UAC."""
    try:
        params = subprocess.list2cmdline(_current_python_args()[1:])
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, params, None, 1
        )
        return int(result) > 32
    except Exception:
        return False


def grant_logon_as_service(account: str) -> None:
    """Grant SeServiceLogonRight to a custom account (requires elevation)."""
    normalized = normalize_account(account)
    sid, _domain, _type = win32security.LookupAccountName(None, normalized)
    policy = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
    try:
        win32security.LsaAddAccountRights(policy, sid, ("SeServiceLogonRight",))
    finally:
        try:
            win32security.LsaClose(policy)
        except Exception:
            pass


def account_has_logon_right(account: str) -> bool:
    try:
        normalized = normalize_account(account)
        sid, _domain, _type = win32security.LookupAccountName(None, normalized)
        policy = win32security.LsaOpenPolicy(None, win32security.POLICY_READ)
        try:
            rights = win32security.LsaEnumerateAccountRights(policy, sid)
            return "SeServiceLogonRight" in rights
        finally:
            try:
                win32security.LsaClose(policy)
            except Exception:
                pass
    except Exception:
        return False


_ERROR_HINTS = {
    2: "Файл не найден.",
    3: "Не найден указанный путь.",
    5: "Отказано в доступе. Запустите программу от имени администратора.",
    87: "Неверный параметр.",
    183: "Файл уже существует.",
    193: "Файл не является приложением Windows.",
    216: "Файл не является исполняемым файлом Windows.",
    267: "Указан неверный каталог.",
    740: "Для выполнения операции требуются права администратора.",
    1051: "Есть запущенные службы, зависящие от этой.",
    1052: "Запрошенное действие не поддерживается этой службой.",
    1053: "Служба не ответила службе управления вовремя (ошибка 1053). Проверьте журнал службы.",
    1058: "Служба отключена — включите её, чтобы запустить.",
    1060: "Служба не найдена. Возможно, она уже удалена.",
    1062: "Служба не запущена.",
    1069: "Не удалось войти в систему под указанной учётной записью. Проверьте имя и пароль.",
    1072: "Служба помечена для удаления. Перезагрузите компьютер и повторите попытку.",
    1073: "Служба с таким именем уже существует.",
    1075: "Зависимость не существует или помечена для удаления.",
    1083: "Этот файл не является службой Windows.",
    1314: "Необходимая привилегия не предоставлена.",
    1326: "Неверное имя пользователя или пароль.",
    1332: "Учётная запись не найдена.",
    1385: "Учётной записи не разрешён вход в качестве службы.",
    2209: "Служба помечена для удаления. Перезагрузите компьютер.",
}


def format_error(exc: BaseException, context: str = "") -> str:
    code = None
    text = ""
    if isinstance(exc, pywintypes.error):
        try:
            code = int(exc.winerror)
        except Exception:
            try:
                code = int(exc.args[0])
            except Exception:
                code = None
        text = getattr(exc, "strerror", None) or (exc.args[2] if len(exc.args) > 2 else "")
    elif isinstance(exc, OSError):
        code = getattr(exc, "winerror", None) or exc.errno
        text = str(exc)
    else:
        text = str(exc)

    hint = _ERROR_HINTS.get(code or -1, "")
    pieces: list[str] = []
    if context:
        pieces.append(context.rstrip(" .") + ".")
    if hint:
        pieces.append(hint)
    windows_text = (text or "").strip()
    if windows_text and windows_text.casefold() not in hint.casefold():
        pieces.append(f"Система: {windows_text}")
    if code and not hint:
        pieces.append(f"Код ошибки: {code}.")
    return " ".join(pieces) or "Неизвестная ошибка."


def open_path(path: str | Path) -> None:
    target = Path(path)
    if target.is_file():
        os.startfile(str(target))  # type: ignore[attr-defined]
    else:
        os.startfile(str(target))  # type: ignore[attr-defined]


def open_folder(path: str | Path) -> None:
    os.startfile(str(path))  # type: ignore[attr-defined]


def reveal_path(path: str | Path) -> None:
    target = str(path)
    if Path(target).is_file():
        subprocess.Popen(["explorer", "/select,", target])
    else:
        subprocess.Popen(["explorer", target])


def run_hidden(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_CREATE_NO_WINDOW,
    )


def quote(path: str) -> str:
    return f'"{path}"'
