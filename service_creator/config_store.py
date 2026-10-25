"""Persistent service configuration storage (JSON files in ProgramData)."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .models import ServiceConfig, utc_now, validate_service_name
from .paths import config_dir

log = logging.getLogger(__name__)


def _safe_name(name: str) -> str:
    if validate_service_name(name):
        raise ValueError(f"Некорректное имя службы: {name!r}")
    return name


def config_path(name: str) -> Path:
    return config_dir() / f"{_safe_name(name)}.json"


def config_exists(name: str) -> bool:
    try:
        return config_path(name).is_file()
    except ValueError:
        return False


def save_config(config: ServiceConfig) -> Path:
    """Atomically write the service settings and return the file path."""
    _safe_name(config.name)
    config.ensure_log_path()
    now = utc_now()
    if not config.created_at:
        config.created_at = now
    config.updated_at = now

    path = config_path(config.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    payload = json.dumps(config.to_dict(), ensure_ascii=False, indent=2)
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return path


def load_config(name: str) -> ServiceConfig | None:
    try:
        path = config_path(name)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        config = ServiceConfig.from_dict(data, name=name)
        config.name = name
        return config
    except (OSError, ValueError, TypeError) as exc:
        log.error("Не удалось прочитать конфигурацию %s: %s", path, exc)
        return None


def delete_config(name: str) -> bool:
    try:
        path = config_path(name)
    except ValueError:
        return False
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError as exc:
        log.error("Не удалось удалить конфигурацию %s: %s", path, exc)
        return False


def load_all() -> dict[str, ServiceConfig]:
    result: dict[str, ServiceConfig] = {}
    directory = config_dir()
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("*.json")):
        name = path.stem
        if validate_service_name(name):
            continue
        config = load_config(name)
        if config is not None:
            result[name] = config
    return result


def export_config(config: ServiceConfig, path: str | Path) -> None:
    payload = config.to_dict()
    payload["exported_at"] = utc_now()
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def import_config(path: str | Path) -> ServiceConfig:
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("Файл не содержит конфигурацию службы.")
    config = ServiceConfig.from_dict(data)
    if not config.name:
        config.name = Path(path).stem
    config.name = config.name.strip()
    error = validate_service_name(config.name)
    if error:
        raise ValueError(error)
    if not config.target:
        raise ValueError("В файле не указан целевой файл (target).")
    return config
