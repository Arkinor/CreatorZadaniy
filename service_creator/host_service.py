"""Windows service host.

The application executable is also the service host: the SCM launches it with
``--svc-host``.  It reads the service settings from ProgramData, starts the
configured program as a child process and keeps the service alive.
"""

from __future__ import annotations

import ctypes
import locale
import os
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

import psutil
import servicemanager
import win32service
import win32serviceutil

from . import config_store, paths
from .launcher import build_command, child_environment, effective_working_dir, expand_path
from .models import ServiceConfig

_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_GLOBAL_LOCK = threading.Lock()
_CREATE_FLAGS = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP

_PRIORITY_MAP = {
    "idle": psutil.IDLE_PRIORITY_CLASS,
    "below": psutil.BELOW_NORMAL_PRIORITY_CLASS,
    "normal": psutil.NORMAL_PRIORITY_CLASS,
    "above": psutil.ABOVE_NORMAL_PRIORITY_CLASS,
    "high": psutil.HIGH_PRIORITY_CLASS,
}


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _console_encoding() -> str:
    """Best guess for output produced by console programs on this system."""
    try:
        return f"cp{int(ctypes.windll.kernel32.GetOEMCP())}"
    except Exception:
        return locale.getpreferredencoding(False) or "utf-8"


_CONSOLE_ENCODING = _console_encoding()


def _decode_line(raw: bytes) -> str:
    """Decode one line of child output, preferring UTF-8 over legacy codepages."""
    for encoding in ("utf-8", _CONSOLE_ENCODING, "cp1251"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _global_log_path() -> Path:
    primary = paths.log_dir()
    try:
        primary.mkdir(parents=True, exist_ok=True)
        return primary / "host.log"
    except OSError:
        fallback = paths.fallback_log_dir()
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback / "host.log"


def write_global(service_name: str, message: str) -> None:
    """Always-on diagnostics in ProgramData (survives a disabled log)."""
    line = f"[{_timestamp()}] [{service_name or 'host'}] {message}\n"
    with _GLOBAL_LOCK:
        path = _global_log_path()
        try:
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                backup = path.with_suffix(".old.log")
                try:
                    backup.unlink(missing_ok=True)
                    path.replace(backup)
                except OSError:
                    pass
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError:
            pass


class ServiceLogger:
    """Per-service log file plus an always-on global host log.

    Both the host messages and the child program output are written through a
    single persistent handle guarded by a lock.  Sharing one writer avoids the
    interleaved/truncated lines that two independent append handles produce.
    """

    def __init__(self, service_name: str, config: ServiceConfig):
        self.service_name = service_name
        self.enabled = bool(config.log_enabled)
        self.append = bool(config.log_append)
        self.max_bytes = max(0, int(config.log_max_mb)) * 1024 * 1024
        self.path = self._resolve_path(config)
        self._lock = threading.RLock()
        self._path_ok = self.enabled
        self._handle = None
        if self.enabled:
            self._prepare_file()
            self._open_file()

    def _resolve_path(self, config: ServiceConfig) -> Path:
        raw = (config.log_path or "").strip() or paths.default_log_path(self.service_name)
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            base = Path(effective_working_dir(config) or paths.data_dir())
            path = base / path
        return path

    def _prepare_file(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.max_bytes:
                try:
                    if self.path.stat().st_size > self.max_bytes:
                        backup = self.path.with_name(self.path.name + ".1")
                        backup.unlink(missing_ok=True)
                        self.path.replace(backup)
                except OSError:
                    pass
            if not self.append:
                try:
                    self.path.write_bytes(b"")
                except OSError:
                    pass
        except OSError as exc:
            self._path_ok = False
            write_global(self.service_name, f"Не удалось подготовить журнал {self.path}: {exc}")

    def _open_file(self) -> None:
        if not self._path_ok:
            return
        try:
            # The file is truncated in _prepare_file when append is off.
            self._handle = open(
                self.path, "a", encoding="utf-8", newline="\n", buffering=1
            )
        except OSError as exc:
            self._path_ok = False
            self._handle = None
            write_global(self.service_name, f"Не удалось открыть журнал {self.path}: {exc}")

    def _close_handle_locked(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def _write_locked(self, line: str) -> None:
        if not self._path_ok or self._handle is None:
            return
        try:
            self._handle.write(line + "\n")
        except OSError:
            self._path_ok = False
            self._close_handle_locked()
            write_global(self.service_name, f"Запись в журнал {self.path} недоступна.")

    def write(self, message: str) -> None:
        write_global(self.service_name, message)
        if not self.enabled or not self._path_ok:
            return
        line = f"[{_timestamp()}] [{self.service_name}] {message}"
        with self._lock:
            self._write_locked(line)

    def write_output(self, text: str) -> None:
        """Write one line produced by the child process (service log only)."""
        if not self.enabled or not self._path_ok:
            return
        line = f"[{_timestamp()}] [{self.service_name}] | {text}"
        with self._lock:
            self._write_locked(line)

    def pump_stream(self, stream) -> None:
        """Read child output until EOF, writing complete lines to the log."""
        # read1() returns as soon as some data is available; read(n) would
        # block until the full n-byte buffer is filled, delaying small output.
        read = getattr(stream, "read1", None) or stream.read
        pending = b""
        try:
            while True:
                try:
                    chunk = read(4096)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    raw, pending = pending.split(b"\n", 1)
                    self._emit_output(raw)
        finally:
            if pending:
                self._emit_output(pending)

    def _emit_output(self, raw: bytes) -> None:
        line = _decode_line(raw).rstrip("\r")
        if line.strip():
            self.write_output(line)

    def close(self) -> None:
        with self._lock:
            self._close_handle_locked()


class TargetRunner:
    """Starts the target program, watches it and restarts it when needed."""

    def __init__(self, service_name: str, stop_event: threading.Event, on_stop_progress=None):
        self.service_name = service_name
        self.stop_event = stop_event
        self.on_stop_progress = on_stop_progress
        self.config = config_store.load_config(service_name)
        self.logger = ServiceLogger(service_name, self.config) if self.config else None
        self.process: subprocess.Popen | None = None
        self._pump_thread: threading.Thread | None = None

    def run(self) -> None:
        try:
            self._run()
        finally:
            try:
                self._stop_child()
            except Exception:
                pass
            self._finish_pump()
            if self.logger is not None:
                self.logger.close()

    def _run(self) -> None:
        if self.config is None:
            message = (
                "Конфигурация службы не найдена. Откройте «Создатель служб» "
                "и сохраните настройки службы повторно."
            )
            write_global(self.service_name, message)
            raise RuntimeError(message)

        config = self.config
        spec = build_command(config)
        if not spec.ok:
            self.logger.write(f"Ошибка конфигурации: {spec.error}")
            raise RuntimeError(spec.error)
        if spec.warning:
            self.logger.write(f"Предупреждение: {spec.warning}")
        if not Path(expand_path(config.target)).is_file():
            message = f"Целевой файл не найден: {config.target}"
            self.logger.write(message)
            raise RuntimeError(message)

        self.logger.write("Служба запущена.")
        self.logger.write(f"Команда: {spec.command}")
        self.logger.write(f"Рабочая папка: {effective_working_dir(config) or '(по умолчанию)'}")
        self.logger.write(
            f"Автоперезапуск: {'включён' if config.auto_restart else 'выключен'}"
        )

        restart_marks: list[float] = []
        attempts = 0
        window_seconds = max(1, config.restart_window_minutes) * 60
        started_once = False

        while not self.stop_event.is_set():
            if started_once:
                # This iteration is a restart.
                if not config.auto_restart:
                    break
                now = time.monotonic()
                restart_marks = [mark for mark in restart_marks if now - mark <= window_seconds]
                if config.max_restarts and len(restart_marks) >= config.max_restarts:
                    self.logger.write(
                        f"Достигнут лимит перезапусков ({config.max_restarts} за "
                        f"{config.restart_window_minutes} мин). Служба останавливается."
                    )
                    break
                restart_marks.append(now)
                attempts += 1
                delay = max(0, config.restart_delay)
                if delay:
                    self.logger.write(f"Перезапуск через {delay} с (попытка {attempts}).")
                    if self.stop_event.wait(delay):
                        break
                if self.stop_event.is_set():
                    break

            self._spawn(spec)
            started_once = True
            while not self.stop_event.is_set():
                if self.process is None or self.process.poll() is not None:
                    break
                self.stop_event.wait(0.5)

            if self.stop_event.is_set():
                break

            code = self.process.returncode if self.process else None
            self.logger.write(f"Процесс завершился с кодом {code}.")
            if not config.auto_restart:
                break
            if config.restart_on_crash_only and code == 0:
                self.logger.write(
                    "Обычное завершение процесса: автоперезапуск не требуется."
                )
                break

        self.logger.write("Служба останавливается.")
        self._stop_child()

    def _spawn(self, spec) -> None:
        config = self.config
        assert config is not None
        cwd = effective_working_dir(config)
        if cwd and not Path(cwd).is_dir():
            self.logger.write(f"Рабочая папка не найдена, используется системная: {cwd}")
            cwd = ""
        try:
            self.process = subprocess.Popen(
                spec.command,
                cwd=cwd or None,
                env=child_environment(config),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=_CREATE_FLAGS,
                close_fds=True,
            )
        except (OSError, ValueError) as exc:
            self.logger.write(f"Не удалось запустить процесс: {exc}")
            raise RuntimeError(f"Не удалось запустить процесс: {exc}") from exc

        self._start_pump()

        try:
            priority = _PRIORITY_MAP.get(config.priority)
            if priority is not None:
                psutil.Process(self.process.pid).nice(priority)
        except Exception:
            pass

        self.logger.write(f"Процесс запущен, PID={self.process.pid}")

    def _start_pump(self) -> None:
        self._finish_pump()
        stream = self.process.stdout if self.process else None
        if stream is None or self.logger is None:
            return
        thread = threading.Thread(
            target=self._pump_output,
            args=(stream,),
            name="svc-output",
            daemon=True,
        )
        self._pump_thread = thread
        thread.start()

    def _pump_output(self, stream) -> None:
        try:
            self.logger.pump_stream(stream)
        except Exception as exc:
            self.logger.write(f"Ошибка чтения вывода процесса: {exc}")
        finally:
            try:
                stream.close()
            except (OSError, ValueError):
                pass

    def _finish_pump(self, timeout: float = 5.0) -> None:
        thread = self._pump_thread
        if thread is None:
            return
        thread.join(timeout)
        if thread.is_alive():
            stream = self.process.stdout if self.process else None
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
            thread.join(1.0)
        self._pump_thread = None

    def _stop_child(self) -> None:
        process = self.process
        if process is None:
            self._finish_pump()
            return
        if process.poll() is None:
            config = self.config
            timeout = max(1, config.stop_timeout) if config else 10
            self.logger.write("Останавливаю процесс…")
            children: list[psutil.Process] = []
            if config and config.kill_tree:
                try:
                    children = psutil.Process(process.pid).children(recursive=True)
                except Exception:
                    children = []
            try:
                process.terminate()
            except OSError:
                pass

            deadline = time.monotonic() + timeout
            while process.poll() is None and time.monotonic() < deadline:
                if self.on_stop_progress:
                    try:
                        self.on_stop_progress()
                    except Exception:
                        pass
                time.sleep(0.3)

            if process.poll() is None:
                self.logger.write("Процесс не завершился вовремя, принудительное завершение.")
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=5)
                except Exception:
                    pass

            if config and config.kill_tree and children:
                for child in children:
                    try:
                        if child.is_running():
                            child.terminate()
                    except Exception:
                        pass
                _gone, alive = psutil.wait_procs(children, timeout=3)
                for child in alive:
                    try:
                        child.kill()
                    except Exception:
                        pass

            self.logger.write("Процесс остановлен.")
        self._finish_pump()
        self.process = None


class ServiceHost(win32serviceutil.ServiceFramework):
    _svc_name_ = "ServiceCreatorHost"
    _svc_display_name_ = "ServiceCreator Host"
    _svc_description_ = "Запускает приложение как службу Windows."

    def __init__(self, args):
        if args:
            self._svc_name_ = args[0]
        super().__init__(args)
        self._stop_event = threading.Event()

    def SvcStop(self):
        timeout = 10
        try:
            config = config_store.load_config(self._svc_name_)
            if config is not None:
                timeout = max(1, int(config.stop_timeout))
        except Exception:
            pass
        self.ReportServiceStatus(
            win32service.SERVICE_STOP_PENDING,
            waitHint=min(120000, (timeout + 15) * 1000),
        )
        self._stop_event.set()

    def SvcShutdown(self):
        self.SvcStop()

    def SvcDoRun(self):
        runner = None
        try:
            runner = TargetRunner(
                self._svc_name_,
                self._stop_event,
                on_stop_progress=lambda: self.ReportServiceStatus(
                    win32service.SERVICE_STOP_PENDING, waitHint=15000
                ),
            )
            runner.run()
        except Exception:
            detail = traceback.format_exc()
            write_global(self._svc_name_, "Критическая ошибка службы:\n" + detail)
            try:
                if runner and runner.logger:
                    runner.logger.write("Критическая ошибка:\n" + detail)
            except Exception:
                pass
            raise
        finally:
            if runner is not None:
                try:
                    runner.logger.close()
                except Exception:
                    pass


def run_service_host() -> None:
    try:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ServiceHost)
        servicemanager.StartServiceCtrlDispatcher()
    except Exception:
        write_global("host", "Не удалось запустить хост службы:\n" + traceback.format_exc())
        raise
