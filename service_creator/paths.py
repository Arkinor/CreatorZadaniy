"""Filesystem locations and helpers for the application and its service host."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "ServiceCreator"
APP_NAME = "Создатель служб"
APP_VERSION = "1.0.0"
HOST_ARG = "--svc-host"
HOST_PREFIX = "ServiceCreatorHost_"

_CREATE_NO_WINDOW = 0x08000000


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _program_data() -> Path:
    base = os.environ.get("PROGRAMDATA") or r"C:\ProgramData"
    return Path(base) / APP_ID


def _local_app_data() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_ID


def data_dir() -> Path:
    return _program_data()


def host_dir() -> Path:
    return data_dir() / "host"


def config_dir() -> Path:
    return data_dir() / "configs"


def log_dir() -> Path:
    return data_dir() / "logs"


def fallback_log_dir() -> Path:
    return _local_app_data() / "logs"


def default_log_path(service_name: str) -> str:
    return str(log_dir() / f"{service_name}.log")


def app_log_path() -> Path:
    primary = log_dir()
    try:
        primary.mkdir(parents=True, exist_ok=True)
        probe = primary / ".write_test"
        probe.touch(exist_ok=True)
        probe.unlink(missing_ok=True)
        return primary / "app.log"
    except OSError:
        fallback = fallback_log_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / "app.log"


def main_script_path() -> Path:
    candidate = Path(sys.argv[0])
    try:
        return candidate.resolve()
    except OSError:
        return candidate.absolute()


def _exe_digest() -> str:
    hasher = hashlib.sha256()
    with open(sys.executable, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


_HOST_HASH: str | None = None


def _host_hash() -> str:
    global _HOST_HASH
    if _HOST_HASH is None:
        _HOST_HASH = _exe_digest()[:12]
    return _HOST_HASH


def host_exe_path() -> Path:
    return host_dir() / f"{HOST_PREFIX}{_host_hash()}.exe"


def ensure_host_exe() -> Path:
    """Copy the current executable to a stable location used by services.

    A single-file build doubles as the service host: the SCM starts the same
    executable with ``--svc-host`` and it responds to service control messages.
    """
    if not is_frozen():
        return Path(sys.executable)
    destination = host_exe_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = Path(sys.executable)
    if destination.exists() and destination.stat().st_size == source.stat().st_size:
        return destination
    temporary = destination.with_suffix(".tmp")
    shutil.copy2(source, temporary)
    os.replace(temporary, destination)
    grant_everyone_read(destination)
    return destination


def host_image_path() -> str:
    """Command line stored in the service's ImagePath registry value."""
    if is_frozen():
        host = ensure_host_exe()
        return f'"{host}" {HOST_ARG}'
    script = main_script_path()
    return f'"{sys.executable}" "{script}" {HOST_ARG}'


def is_our_image_path(image_path: str) -> bool:
    if not image_path:
        return False
    lowered = image_path.lower()
    if HOST_ARG not in lowered:
        return False
    return HOST_PREFIX.lower() in lowered or "main.py" in lowered or "servicecreator" in lowered


def grant_everyone_read(path: Path) -> None:
    """Best-effort read/execute ACL so any service account can start the host."""
    _icacls(path, "*S-1-1-0:(OI)(CI)RX" if path.is_dir() else "*S-1-1-0:RX")


def grant_everyone_modify(path: Path) -> None:
    _icacls(path, "*S-1-1-0:(OI)(CI)M" if path.is_dir() else "*S-1-1-0:M")


def _icacls(path: Path, grant: str) -> None:
    try:
        subprocess.run(
            [
                "icacls",
                str(path),
                "/grant",
                grant,
                "/C",
                "/Q",
            ],
            check=False,
            capture_output=True,
            creationflags=_CREATE_NO_WINDOW,
        )
    except OSError:
        pass


def prepare_runtime_directories() -> None:
    for directory in (data_dir(), host_dir(), config_dir(), log_dir()):
        directory.mkdir(parents=True, exist_ok=True)
    # Host and configuration are read-only for services; logs are writable.
    grant_everyone_read(host_dir())
    grant_everyone_read(config_dir())
    grant_everyone_modify(log_dir())
