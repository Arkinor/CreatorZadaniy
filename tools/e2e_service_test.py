"""End-to-end test for the built service host.

Must be run from an elevated prompt:

    .venv\\Scripts\\python.exe tools\\e2e_service_test.py

The test creates a real Windows service whose ImagePath points at the built
host copy in ProgramData, starts it, verifies that the target program runs and
writes both its heartbeat and stdout to the service log, restarts the service,
stops it and finally deletes it.  Nothing is left behind on success.
"""

from __future__ import annotations

import ctypes
import hashlib
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

import psutil
import win32service

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from service_creator import config_store, paths, scm  # noqa: E402
from service_creator.models import ServiceConfig  # noqa: E402

SERVICE_NAME = "SCTestE2E"
DISPLAY_NAME = "ServiceCreator E2E test"
LOG_NAME = "SCTestE2E.log"


class Failure(RuntimeError):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failure(message)
    print(f"  OK   {message}")


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def resolve_host_exe() -> Path:
    """Return the host copy that matches the current dist build."""
    dist = ROOT / "dist" / "ServiceCreator.exe"
    if not dist.is_file():
        raise Failure(f"Built executable not found: {dist}. Run build.bat first.")
    digest = hashlib.sha256(dist.read_bytes()).hexdigest()[:12]
    host = paths.host_dir() / f"{paths.HOST_PREFIX}{digest}.exe"
    host.parent.mkdir(parents=True, exist_ok=True)
    if not host.is_file() or host.stat().st_size != dist.stat().st_size:
        shutil.copy2(dist, host)
    paths.grant_everyone_read(host)
    return host


def override_image_path(name: str, image_path: str) -> None:
    """Point the service at the built host instead of the source interpreter."""
    hscm = win32service.OpenSCManager(
        None, None, win32service.SC_MANAGER_ALL_ACCESS
    )
    hs = None
    try:
        hs = win32service.OpenService(
            hscm, name, win32service.SERVICE_CHANGE_CONFIG
        )
        win32service.ChangeServiceConfig(
            hs,
            win32service.SERVICE_NO_CHANGE,
            win32service.SERVICE_NO_CHANGE,
            win32service.SERVICE_NO_CHANGE,
            image_path,
            None,
            0,
            None,
            None,
            None,
            None,
        )
    finally:
        if hs is not None:
            win32service.CloseServiceHandle(hs)
        win32service.CloseServiceHandle(hscm)


def wait_for(predicate, timeout: float, description: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except OSError:
            pass
        time.sleep(0.4)
    raise Failure(f"Timed out after {timeout:.0f}s waiting for {description}")


def read_log(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def heartbeat_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def process_pids(target: Path) -> list[int]:
    pids: list[int] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if str(target).lower() in cmdline.lower():
            pids.append(proc.info["pid"])
    return pids


def write_target(path: Path, heartbeat: Path) -> None:
    path.write_text(
        "@echo off\r\n"
        ":loop\r\n"
        f'>>"{heartbeat}" echo tick\r\n'
        "echo output line from the service target\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto loop\r\n",
        encoding="ascii",
    )


def run() -> int:
    print(f"ServiceCreator end-to-end test ({SERVICE_NAME})")
    print(f"  host: {resolve_host_exe()}")

    work = paths.log_dir() / "e2e"
    work.mkdir(parents=True, exist_ok=True)
    paths.grant_everyone_modify(work)
    target = work / "target.cmd"
    heartbeat = work / "heartbeat.txt"
    log_path = paths.log_dir() / LOG_NAME

    for stale in (heartbeat, log_path, log_path.with_name(LOG_NAME + ".1")):
        stale.unlink(missing_ok=True)
    write_target(target, heartbeat)

    config = ServiceConfig(
        name=SERVICE_NAME,
        display_name=DISPLAY_NAME,
        target=str(target),
        launcher="cmd",
        auto_restart=True,
        restart_on_crash_only=True,
        restart_delay=1,
        log_enabled=True,
        log_path=str(log_path),
        log_append=True,
        log_max_mb=5,
        stop_timeout=5,
        kill_tree=True,
    )
    config.ensure_log_path()

    created = False
    try:
        print("\n[1/6] Creating the service through service_creator.scm")
        host = resolve_host_exe()
        if scm.service_exists(SERVICE_NAME):
            scm.delete_service(SERVICE_NAME, stop_if_running=True)
        config_store.delete_config(SERVICE_NAME)
        warnings = scm.create_service(config, None)
        created = True
        for warning in warnings:
            print(f"  WARN {warning}")
        check(scm.service_exists(SERVICE_NAME), "service registered")
        check(
            scm.query_state(SERVICE_NAME) == win32service.SERVICE_STOPPED,
            "service starts in the STOPPED state",
        )
        override_image_path(SERVICE_NAME, f'"{host}" {paths.HOST_ARG}')
        details = scm.query_details(SERVICE_NAME)
        check(
            paths.HOST_ARG in details.image_path and str(host) in details.image_path,
            "ImagePath points at the built host",
        )
        stored = config_store.load_config(SERVICE_NAME)
        check(stored is not None, "service configuration saved")
        check(stored.log_path == str(log_path), "log path persisted in the configuration")

        print("\n[2/6] Starting the service")
        scm.start_service(SERVICE_NAME, timeout=40)
        check(scm.query_state(SERVICE_NAME) == win32service.SERVICE_RUNNING, "service is RUNNING")
        wait_for(lambda: heartbeat_size(heartbeat) > 0, 20, "target heartbeat")
        check(heartbeat_size(heartbeat) > 0, "target program is writing its heartbeat")

        print("\n[3/6] Verifying logs and output capture")
        wait_for(
            lambda: "output line from the service target" in read_log(log_path),
            20,
            "target stdout in the service log",
        )
        text = read_log(log_path)
        check("Служба запущена." in text, "host start message in the log")
        check(
            "output line from the service target" in text,
            "target stdout captured in the service log",
        )
        match = re.search(r"PID=(\d+)", text)
        check(match is not None, "host logged the target PID")
        first_pid = int(match.group(1)) if match else 0
        check(psutil.pid_exists(first_pid), "target process exists")

        print("\n[4/6] Restarting the service twice")
        previous_pid = first_pid
        for index in (1, 2):
            restart_started = time.monotonic()
            scm.restart_service(SERVICE_NAME, timeout=45)
            elapsed = time.monotonic() - restart_started
            check(
                scm.query_state(SERVICE_NAME) == win32service.SERVICE_RUNNING,
                f"restart {index} finished in {elapsed:.1f}s, service RUNNING",
            )
            wait_for(
                lambda: read_log(log_path).count("Служба запущена.") >= index + 1,
                20,
                f"start #{index + 1} in the log",
            )
            pids = [
                int(value) for value in re.findall(r"PID=(\d+)", read_log(log_path))
            ]
            check(len(pids) >= index + 1, f"restart {index} logged a new PID")
            check(pids[-1] != previous_pid, f"restart {index} produced a new PID")
            previous_pid = pids[-1]
        check(scm.query_state(SERVICE_NAME) == win32service.SERVICE_RUNNING, "service still RUNNING")

        print("\n[5/6] Stopping the service")
        scm.stop_service(SERVICE_NAME, timeout=40)
        check(scm.query_state(SERVICE_NAME) == win32service.SERVICE_STOPPED, "service is STOPPED")
        size_before = heartbeat_size(heartbeat)
        time.sleep(3)
        check(heartbeat_size(heartbeat) == size_before, "heartbeat stopped growing")
        check("Процесс остановлен." in read_log(log_path), "host logged the target stop")
        leftover = process_pids(target)
        check(not leftover, "no target processes left behind")

        print("\n[6/6] Deleting the service")
        scm.delete_service(SERVICE_NAME)
        check(not scm.service_exists(SERVICE_NAME), "service removed from the SCM")
        config_store.delete_config(SERVICE_NAME)
        check(not config_store.config_exists(SERVICE_NAME), "service configuration removed")
    finally:
        if created:
            try:
                if scm.service_exists(SERVICE_NAME):
                    scm.delete_service(SERVICE_NAME, stop_if_running=True)
            except Exception as exc:  # noqa: BLE001
                print(f"  WARN cleanup: {exc}")
        config_store.delete_config(SERVICE_NAME)

    shutil.rmtree(work, ignore_errors=True)
    log_path.unlink(missing_ok=True)
    log_path.with_name(LOG_NAME + ".1").unlink(missing_ok=True)
    print("\nRESULT: PASS")
    return 0


def main() -> int:
    if not is_admin():
        print("ERROR: this test must be run from an elevated (Administrator) prompt.")
        return 2
    report = Path(tempfile.gettempdir()) / "sc_e2e_report.txt"
    handle = open(report, "w", encoding="utf-8", buffering=1)
    original_out, original_err = sys.stdout, sys.stderr

    class Tee:
        def __init__(self, *streams):
            self.streams = streams

        def write(self, text):
            for stream in self.streams:
                try:
                    stream.write(text)
                    stream.flush()
                except Exception:
                    pass
            return len(text)

        def flush(self):
            for stream in self.streams:
                try:
                    stream.flush()
                except Exception:
                    pass

    sys.stdout = Tee(original_out, handle)
    sys.stderr = Tee(original_err, handle)
    try:
        print(f"Report file: {report}")
        try:
            return run()
        except Failure as exc:
            print(f"\nRESULT: FAIL - {exc}")
            print("Artifacts kept in:", paths.log_dir() / "e2e")
            return 1
        except Exception:
            import traceback

            print("\nRESULT: FAIL - unexpected error")
            traceback.print_exc()
            print("Artifacts kept in:", paths.log_dir() / "e2e")
            return 1
    finally:
        sys.stdout, sys.stderr = original_out, original_err
        handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
