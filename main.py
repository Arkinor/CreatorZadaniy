"""Service Creator entry point.

The same executable is used for the GUI and as the Windows service host:

* normal launch (optionally elevated)  -> Tkinter interface
* ``--svc-host``                       -> service control dispatcher
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import logging.handlers
import sys
import tempfile
import threading
import traceback
from pathlib import Path

from service_creator import paths

log = logging.getLogger("service_creator")

_MUTEX_NAME = "Local\\ServiceCreator.App.1"
_mutex_handle = None


# --------------------------------------------------------------------- helpers
def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ServiceCreator",
        description="Создание и управление службами Windows.",
        add_help=True,
    )
    parser.add_argument(
        "--svc-host",
        action="store_true",
        help="Запустить в режиме хост-службы (используется диспетчером служб).",
    )
    parser.add_argument(
        "--selftest",
        action="store_true",
        help="Проверить компоненты приложения и записать отчёт.",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help="Куда записать отчёт самопроверки.",
    )
    parser.add_argument(
        "--screenshot",
        nargs="?",
        const=str(Path(tempfile.gettempdir()) / "ServiceCreator_screens"),
        metavar="DIR",
        default=None,
        help="Сохранить снимки интерфейса в папку и выйти.",
    )
    parser.add_argument(
        "--allow-multi",
        action="store_true",
        help="Разрешить несколько экземпляров приложения.",
    )
    parser.add_argument("--version", action="store_true", help="Показать версию.")
    args, _unknown = parser.parse_known_args(argv)
    return args


def _enable_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def _set_app_user_model_id() -> None:
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ServiceCreator.App.1"
        )
    except Exception:
        pass


def _message_box(text: str, title: str = paths.APP_NAME, flags: int = 0x40) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(None, text, title, flags)
    except Exception:
        pass


def _acquire_mutex() -> bool:
    global _mutex_handle
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            kernel32.CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception:
        return True


def _release_mutex() -> None:
    global _mutex_handle
    if _mutex_handle:
        try:
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
        except Exception:
            pass
        _mutex_handle = None


def _setup_logging() -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    try:
        path = paths.app_log_path()
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(formatter)
        root_logger.addHandler(handler)
        log.info("Запуск %s %s (%s)", paths.APP_NAME, paths.APP_VERSION, path)
    except Exception:
        pass
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        root_logger.addHandler(console)


def _thread_excepthook(args) -> None:
    log.error(
        "Необработанная ошибка в потоке %s",
        getattr(args.thread, "name", "?"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


# ------------------------------------------------------------------------- gui
def _run_gui() -> int:
    import tkinter as tk

    from service_creator.ui import theme
    from service_creator.ui.main_window import MainWindow

    _enable_dpi_awareness()
    _set_app_user_model_id()
    _setup_logging()
    threading.excepthook = _thread_excepthook

    if not _acquire_mutex():
        _message_box(
            "Программа уже запущена.\n\n"
            "Окно можно открыть двойным щелчком по значку в системном трее.",
            paths.APP_NAME,
        )
        return 0

    root = None
    try:
        root = tk.Tk(className="ServiceCreator")
        theme.apply_theme(root)

        def report_callback_exception(exc_type, exc_value, exc_tb) -> None:
            log.error(
                "Необработанная ошибка интерфейса",
                exc_info=(exc_type, exc_value, exc_tb),
            )
            try:
                from tkinter import messagebox

                messagebox.showerror(
                    "Ошибка",
                    f"{exc_value}\n\nПодробности записаны в журнал:\n{paths.app_log_path()}",
                    parent=root,
                )
            except Exception:
                pass

        root.report_callback_exception = report_callback_exception
        MainWindow(root, enable_tray=True)
        root.mainloop()
        return 0
    except Exception:
        log.exception("Критическая ошибка при запуске интерфейса")
        _message_box(
            "Не удалось запустить программу:\n\n"
            + f"{traceback.format_exc(limit=3)}\n\nЖурнал: {paths.app_log_path()}",
            paths.APP_NAME,
            0x10,
        )
        return 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass
        _release_mutex()


# -------------------------------------------------------------------- selftest
def _selftest_lines() -> tuple[bool, list[str]]:
    import tkinter as tk

    from service_creator import icon, launcher, models, paths as p
    from service_creator.ui import theme
    from service_creator.ui.editor import ServiceEditor
    from service_creator.ui.main_window import MainWindow

    lines: list[str] = []
    ok = True

    def check(name: str, func) -> None:
        nonlocal ok
        try:
            value = func()
            lines.append(f"OK   {name}: {value}")
        except Exception as exc:  # noqa: BLE001 - report every failure
            ok = False
            lines.append(f"FAIL {name}: {exc}\n{traceback.format_exc()}")

    check("version", lambda: p.APP_VERSION)
    check("icon", lambda: icon.create_icon_image(32).size)
    check("host image path", lambda: p.host_image_path())
    check("data dir", lambda: str(p.data_dir()))
    check("log dir", lambda: str(p.log_dir()))

    def config_roundtrip():
        source = models.sample_config()
        source.failure_command = "cmd.exe /c echo failure"
        loaded = models.ServiceConfig.from_dict(source.to_dict())
        assert loaded.name == source.name
        assert loaded.failure_command == source.failure_command
        assert loaded.failure_actions[0].action == "restart"
        return "schema fields preserved"

    check("config roundtrip", config_roundtrip)

    def host_check():
        from service_creator.host_service import ServiceHost

        assert issubclass(ServiceHost, object)
        return ServiceHost.__name__

    check("service host", host_check)

    def launcher_check():
        config = models.ServiceConfig(
            name="TestService", target=r"C:\Windows\System32\notepad.exe", arguments="--x"
        )
        spec = launcher.build_command(config)
        assert spec.ok, spec.error
        assert spec.command.endswith('notepad.exe" --x'), spec.command
        assert launcher.effective_working_dir(config).endswith("System32")
        return spec.command

    check("launcher", launcher_check)

    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        theme.apply_theme(root)
        check("theme", lambda: str(theme.FONT_FAMILY))

        window = MainWindow(root, enable_tray=False)
        root.update_idletasks()
        check("main window", lambda: f"tree items: {len(window.tree.get_children())}")
        try:
            window.refresh(deep=True)
            root.update_idletasks()
            lines.append(f"OK   service list: {len(window.rows)} row(s)")
        except Exception as exc:  # noqa: BLE001 - non-admin environment
            lines.append(f"WARN service list unavailable: {exc}")

        editor = ServiceEditor(root, window, models.sample_config(), mode="create")
        editor.grab_release()
        root.update_idletasks()
        preview = editor.preview_text.get("1.0", "end-1c").strip()
        check("editor preview", lambda: preview or "<empty>")
        check("editor tabs", lambda: editor.notebook.index("end"))
        editor.destroy()

        cfg = window._selected_row() if window.rows else None
        lines.append(f"INFO selected: {cfg.name if cfg else '-'}")
    except Exception as exc:  # noqa: BLE001 - report GUI failures
        ok = False
        lines.append(f"FAIL gui: {exc}\n{traceback.format_exc()}")
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    lines.append(f"RESULT: {'PASS' if ok else 'FAIL'}")
    return ok, lines


def _run_selftest(report: str | None) -> int:
    _setup_logging()
    ok, lines = _selftest_lines()
    report_path = Path(report) if report else Path(tempfile.gettempdir()) / "ServiceCreator_selftest.txt"
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    if sys.stdout is not None:
        try:
            print("\n".join(lines))
            print(f"\nReport: {report_path}")
        except Exception:
            pass
    return 0 if ok else 1


# ------------------------------------------------------------------ screenshots
def _grab(widget, path: Path) -> None:
    from PIL import ImageGrab

    widget.update_idletasks()
    widget.update()
    x = widget.winfo_rootx()
    y = widget.winfo_rooty()
    width = max(1, widget.winfo_width())
    height = max(1, widget.winfo_height())
    ImageGrab.grab(bbox=(x, y, x + width, y + height)).save(path)


def _run_screenshot(directory: str) -> int:
    import tkinter as tk

    from service_creator.models import sample_config
    from service_creator.ui import theme
    from service_creator.ui.editor import ServiceEditor
    from service_creator.ui.main_window import MainWindow

    _enable_dpi_awareness()
    _setup_logging()
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)

    root = tk.Tk(className="ServiceCreator")
    theme.apply_theme(root)
    window = MainWindow(root, enable_tray=False)
    try:
        root.attributes("-topmost", True)
    except tk.TclError:
        pass

    result = {"code": 1}

    def capture() -> None:
        try:
            try:
                window.refresh(deep=True)
            except Exception:
                log.debug("Не удалось обновить список для снимка", exc_info=True)
            root.update_idletasks()
            root.update()
            _grab(root, output / "main_window.png")
            if window.rows and not window.selected_name:
                window.selected_name = window.rows[0].name
                window._update_detail()
            root.update()
            _grab(root, output / "main_window_selected.png")

            editor = ServiceEditor(root, window, sample_config(), mode="create")
            editor.grab_release()
            try:
                editor.attributes("-topmost", True)
            except tk.TclError:
                pass
            root.after(300, lambda: capture_editor(editor))
        except Exception:
            log.exception("Не удалось сохранить снимки интерфейса")
            try:
                root.destroy()
            except Exception:
                pass

    def capture_editor(editor: ServiceEditor) -> None:
        try:
            editor.update_idletasks()
            editor.update()
            names = [
                "editor_main.png",
                "editor_startup.png",
                "editor_recovery.png",
                "editor_log.png",
            ]
            for index, name in enumerate(names):
                editor.notebook.select(index)
                editor.update_idletasks()
                editor.update()
                _grab(editor, output / name)
            result["code"] = 0
        except Exception:
            log.exception("Не удалось сохранить снимки редактора")
        finally:
            try:
                editor.destroy()
            except Exception:
                pass
            try:
                root.destroy()
            except Exception:
                pass

    root.after(450, capture)
    root.mainloop()
    if result["code"] == 0 and sys.stdout is not None:
        try:
            print(f"Screenshots: {output}")
        except Exception:
            pass
    return result["code"]


# ------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.svc_host:
        from service_creator.host_service import run_service_host

        run_service_host()
        return 0

    if args.version:
        if sys.stdout is not None:
            try:
                print(f"{paths.APP_NAME} {paths.APP_VERSION}")
            except Exception:
                pass
        return 0

    if args.screenshot:
        return _run_screenshot(args.screenshot)

    if args.selftest:
        return _run_selftest(args.report)

    return _run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
