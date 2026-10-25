"""System tray integration (pystray)."""

from __future__ import annotations

import logging
import threading
import tkinter as tk

import pystray

from ..icon import create_icon_image
from ..paths import APP_NAME

log = logging.getLogger(__name__)


class TrayController:
    def __init__(self, root: tk.Tk, on_open, on_create, on_refresh, on_exit):
        self.root = root
        self._on_open = on_open
        self._on_exit = on_exit
        self._thread: threading.Thread | None = None
        self._started = False

        menu = pystray.Menu(
            pystray.MenuItem("Открыть окно", self._wrap(on_open), default=True),
            pystray.MenuItem("Создать службу", self._wrap(on_create)),
            pystray.MenuItem("Обновить список", self._wrap(on_refresh)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self._wrap(on_exit)),
        )
        self.icon = pystray.Icon(
            "ServiceCreator",
            create_icon_image(64),
            f"{APP_NAME} — управление службами Windows",
            menu,
        )

    def _wrap(self, callback):
        def handler(_icon=None, _item=None):
            try:
                self.root.after(0, callback)
            except (tk.TclError, RuntimeError):
                pass

        return handler

    def start(self) -> bool:
        try:
            self._thread = threading.Thread(
                target=self.icon.run, name="service-creator-tray", daemon=True
            )
            self._thread.start()
            self._started = True
            return True
        except Exception:
            log.exception("Не удалось создать значок в системном трее")
            return False

    def notify(self, message: str, title: str = APP_NAME) -> None:
        if not self._started:
            return
        try:
            self.icon.notify(message, title)
        except Exception:
            pass

    def stop(self) -> None:
        if not self._started:
            return
        self._started = False
        try:
            self.icon.stop()
        except Exception:
            pass
