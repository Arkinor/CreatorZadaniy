"""Main application window."""

from __future__ import annotations

import logging
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import win32service

from .. import config_store, paths, scm, winapi
from ..launcher import effective_working_dir
from ..models import (
    ACCOUNT_LOCAL_SERVICE,
    ACCOUNT_NETWORK_SERVICE,
    ACCOUNT_SYSTEM,
    ServiceConfig,
)
from ..paths import APP_NAME, APP_VERSION
from . import theme, widgets
from .editor import ServiceEditor
from .tray import TrayController

log = logging.getLogger(__name__)


class DetailRow:
    """A label + wrapped value pair used in the service details card."""

    def __init__(self, parent, row: int, label: str, button_text: str | None = None):
        self.label = tk.Label(
            parent,
            text=label,
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="nw",
            justify="left",
        )
        self.label.grid(row=row, column=0, sticky="nw", padx=(0, 14), pady=6)

        holder = tk.Frame(parent, bg=theme.CARD)
        holder.grid(row=row, column=1, sticky="ew", pady=6)
        holder.grid_columnconfigure(0, weight=1)

        self.value = tk.Label(
            holder,
            text="—",
            bg=theme.CARD,
            fg=theme.TEXT,
            font=(theme.FONT_FAMILY, 10),
            anchor="w",
            justify="left",
            wraplength=420,
        )
        self.value.grid(row=0, column=0, sticky="w")

        self.button = None
        if button_text:
            self.button = ttk.Button(holder, text=button_text, style="Ghost.TButton")
            self.button.grid(row=0, column=1, sticky="e", padx=(8, 0))

    def set(self, text: str, color: str | None = None) -> None:
        self.value.configure(text=text or "—", fg=color or theme.TEXT)


class MainWindow:
    def __init__(self, root: tk.Tk, enable_tray: bool = True):
        self.root = root
        self.enable_tray = enable_tray
        self.admin = winapi.is_admin()
        self.rows: list[scm.ServiceRow] = []
        self.row_map: dict[str, scm.ServiceRow] = {}
        self.selected_name: str | None = None
        self.registered_names: set[str] = set()
        self.busy = False
        self._closing = False
        self._populating = False
        self._poll_after: str | None = None
        self._tray_hint_shown = False
        self._status_images = {
            "running": theme.status_image(root, "running"),
            "stopped": theme.status_image(root, "stopped"),
            "pending": theme.status_image(root, "pending"),
            "paused": theme.status_image(root, "paused"),
            "error": theme.status_image(root, "error"),
            "unregistered": theme.status_image(root, "unregistered"),
        }
        self._icon64 = theme.icon_photo(root, 64)
        self._icon32 = theme.icon_photo(root, 32)
        self._icon96 = theme.icon_photo(root, 96)

        root.title(APP_NAME)
        root.configure(bg=theme.BG)
        root.minsize(1180, 740)
        root.geometry("1280x820")
        root.iconphoto(True, self._icon64)
        try:
            root.wm_iconbitmap(default=None)
        except tk.TclError:
            pass

        self.search_var = tk.StringVar()
        self._build_menu()
        self._build_layout()
        self._bind_events()
        self._setup_tray()

        self.root.after(80, lambda: self.refresh(deep=True))
        self._schedule_poll()
        if not self.admin:
            self.show_status(
                "Ограниченный режим: для создания и изменения служб нужны права администратора.",
                "error",
            )

    # ------------------------------------------------------------------ layout
    def _build_menu(self) -> None:
        menubar = tk.Menu(self.root, tearoff=0)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Создать службу…", accelerator="Ctrl+N", command=self.create_service)
        file_menu.add_command(label="Импорт из файла…", command=self.import_service)
        file_menu.add_command(label="Экспорт выбранной службы…", command=self.export_selected)
        file_menu.add_separator()
        file_menu.add_command(label="Выход", accelerator="Ctrl+Q", command=self.quit_app)
        menubar.add_cascade(label="Файл", menu=file_menu)

        service_menu = tk.Menu(menubar, tearoff=0)
        service_menu.add_command(label="Изменить…", accelerator="Ctrl+E", command=self.edit_selected)
        service_menu.add_separator()
        service_menu.add_command(label="Запустить", command=self.start_selected)
        service_menu.add_command(label="Остановить", command=self.stop_selected)
        service_menu.add_command(label="Перезапустить", command=self.restart_selected)
        service_menu.add_separator()
        service_menu.add_command(label="Удалить…", accelerator="Delete", command=self.delete_selected)
        menubar.add_cascade(label="Служба", menu=service_menu)

        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Обновить список", accelerator="F5", command=lambda: self.refresh(deep=True))
        tools_menu.add_command(label="Открыть папку данных", command=self.open_data_folder)
        tools_menu.add_command(label="Открыть журнал приложения", command=self.open_app_log)
        if not self.admin:
            tools_menu.add_separator()
            tools_menu.add_command(label="Перезапустить с правами администратора", command=self.elevate)
        menubar.add_cascade(label="Сервис", menu=tools_menu)

        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="Как это работает", command=self.show_help)
        help_menu.add_command(label="О программе", command=self.show_about)
        menubar.add_cascade(label="Справка", menu=help_menu)
        self.root.configure(menu=menubar)

    def _build_layout(self) -> None:
        header = tk.Frame(self.root, bg=theme.CARD, height=78)
        header.pack(fill="x")
        header.pack_propagate(False)
        header_inner = tk.Frame(header, bg=theme.CARD)
        header_inner.pack(fill="both", expand=True, padx=18, pady=(10, 8))

        icon_label = tk.Label(header_inner, image=self._icon64, bg=theme.CARD)
        icon_label.pack(side="left", padx=(0, 12))
        title_block = tk.Frame(header_inner, bg=theme.CARD)
        title_block.pack(side="left", fill="y")
        tk.Label(
            title_block,
            text=APP_NAME,
            bg=theme.CARD,
            fg=theme.TEXT,
            font=(theme.FONT_FAMILY, 17, "bold"),
            anchor="w",
        ).pack(anchor="w", pady=(2, 0))
        tk.Label(
            title_block,
            text="Создание и управление службами Windows",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
        ).pack(anchor="w")

        right = tk.Frame(header_inner, bg=theme.CARD)
        right.pack(side="right", fill="y")
        badge_bg = theme.SUCCESS_BG if self.admin else theme.DANGER_BG
        badge_fg = theme.SUCCESS if self.admin else theme.DANGER
        self.admin_badge = tk.Label(
            right,
            text="Администратор" if self.admin else "Без прав администратора",
            bg=badge_bg,
            fg=badge_fg,
            font=(theme.FONT_FAMILY, 9, "bold"),
            padx=10,
            pady=4,
        )
        self.admin_badge.pack(side="right", padx=(12, 0), pady=14)
        self.refresh_button = ttk.Button(
            right, text="Обновить", style="Secondary.TButton", command=lambda: self.refresh(deep=True)
        )
        self.refresh_button.pack(side="right", padx=(8, 0), pady=9)
        self.create_button = ttk.Button(
            right, text="+  Создать службу", style="Accent.TButton", command=self.create_service
        )
        self.create_button.pack(side="right", pady=9)

        if not self.admin:
            banner = tk.Frame(self.root, bg="#fff7ed")
            banner.pack(fill="x")
            tk.Label(
                banner,
                text="Для создания, изменения и запуска служб требуются права администратора.",
                bg="#fff7ed",
                fg="#9a3412",
                font=(theme.FONT_FAMILY, 9),
                anchor="w",
            ).pack(side="left", padx=18, pady=8)
            ttk.Button(
                banner, text="Перезапустить с правами", style="Secondary.TButton", command=self.elevate
            ).pack(side="right", padx=18, pady=6)

        body = tk.PanedWindow(
            self.root,
            orient="horizontal",
            bg=theme.BG,
            sashwidth=6,
            sashrelief="flat",
            borderwidth=0,
            opaqueresize=True,
        )
        body.pack(fill="both", expand=True, padx=16, pady=(14, 10))

        left_card = tk.Frame(body, bg=theme.CARD, highlightthickness=1, highlightbackground=theme.BORDER)
        right_card = tk.Frame(body, bg=theme.CARD, highlightthickness=1, highlightbackground=theme.BORDER)
        body.add(left_card, minsize=520, width=575, stretch="never")
        body.add(right_card, minsize=520, stretch="always")

        self._build_list(left_card)
        self._build_details(right_card)
        self._build_statusbar()

    def _build_list(self, parent: tk.Frame) -> None:
        inner = tk.Frame(parent, bg=theme.CARD)
        inner.pack(fill="both", expand=True, padx=12, pady=12)

        search_row = tk.Frame(inner, bg=theme.CARD)
        search_row.pack(fill="x", pady=(0, 10))
        self.search = widgets.SearchBox(
            search_row,
            "Поиск службы…",
            self.search_var,
            width=30,
        )
        self.search.pack(side="left", fill="x", expand=True)
        refresh_btn = ttk.Button(
            search_row,
            text="⟳",
            style="Tool.TButton",
            command=lambda: self.refresh(deep=True),
        )
        refresh_btn.pack(side="right", padx=(6, 0))
        widgets.Tooltip(refresh_btn, "Обновить список (F5)")

        tree_frame = tk.Frame(inner, bg=theme.CARD)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            tree_frame,
            style="Services.Treeview",
            columns=("display", "name", "state", "start"),
            show="tree headings",
            selectmode="browse",
        )
        self.tree.heading("#0", text="")
        self.tree.heading("display", text="Служба", anchor="w")
        self.tree.heading("name", text="Имя", anchor="w")
        self.tree.heading("state", text="Состояние", anchor="w")
        self.tree.heading("start", text="Запуск", anchor="w")
        self.tree.column("#0", width=32, minwidth=32, stretch=False, anchor="center")
        self.tree.column("display", width=185, minwidth=120, stretch=True, anchor="w")
        self.tree.column("name", width=122, minwidth=100, stretch=False, anchor="w")
        self.tree.column("state", width=108, minwidth=100, stretch=False, anchor="w")
        self.tree.column("start", width=82, minwidth=70, stretch=False, anchor="w")
        tree_scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview, style="Vertical.TScrollbar"
        )
        self.tree.configure(yscrollcommand=tree_scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        footer = tk.Frame(inner, bg=theme.CARD)
        footer.pack(fill="x", pady=(10, 0))
        self.count_label = tk.Label(
            footer,
            text="Служб: 0",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
        )
        self.count_label.pack(side="left")
        self.filtered_label = tk.Label(
            footer,
            text="",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
        )
        self.filtered_label.pack(side="right")

    def _build_details(self, parent: tk.Frame) -> None:
        self.detail_stack = tk.Frame(parent, bg=theme.CARD)
        self.detail_stack.pack(fill="both", expand=True)
        self.detail_stack.grid_rowconfigure(0, weight=1)
        self.detail_stack.grid_columnconfigure(0, weight=1)

        self.empty_frame = tk.Frame(self.detail_stack, bg=theme.CARD)
        self.detail_frame = tk.Frame(self.detail_stack, bg=theme.CARD)
        for frame in (self.empty_frame, self.detail_frame):
            frame.grid(row=0, column=0, sticky="nsew")

        self._build_empty_state(self.empty_frame)
        self._build_detail_content(self.detail_frame)
        self.empty_frame.tkraise()

    def _build_empty_state(self, parent: tk.Frame) -> None:
        center = tk.Frame(parent, bg=theme.CARD)
        center.place(relx=0.5, rely=0.45, anchor="center")
        tk.Label(center, image=self._icon96, bg=theme.CARD).pack(pady=(0, 14))
        self.empty_title = tk.Label(
            center,
            text="Служба не выбрана",
            bg=theme.CARD,
            fg=theme.TEXT,
            font=(theme.FONT_FAMILY, 15, "bold"),
        )
        self.empty_title.pack()
        self.empty_text = tk.Label(
            center,
            text=(
                "Выберите службу в списке слева, чтобы увидеть её состояние,\n"
                "запустить, остановить или изменить настройки."
            ),
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 10),
            justify="center",
            wraplength=430,
        )
        self.empty_text.pack(pady=(8, 18))
        self.empty_button = ttk.Button(
            center,
            text="+  Создать службу",
            style="Accent.TButton",
            command=self.create_service,
        )
        self.empty_button.pack()

    def _build_detail_content(self, parent: tk.Frame) -> None:
        content = tk.Frame(parent, bg=theme.CARD)
        content.pack(fill="both", expand=True, padx=20, pady=(16, 14))
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(3, weight=1)

        top = tk.Frame(content, bg=theme.CARD)
        top.grid(row=0, column=0, sticky="ew")
        top.grid_columnconfigure(0, weight=1)
        title_row = tk.Frame(top, bg=theme.CARD)
        title_row.grid(row=0, column=0, sticky="ew")
        title_row.grid_columnconfigure(0, weight=1)

        self.detail_title = tk.Label(
            title_row,
            text="",
            bg=theme.CARD,
            fg=theme.TEXT,
            font=(theme.FONT_FAMILY, 16, "bold"),
            anchor="w",
        )
        self.detail_title.grid(row=0, column=0, sticky="w")
        self.status_pill = tk.Label(
            title_row,
            text="",
            bg=theme.SUCCESS_BG,
            fg=theme.SUCCESS,
            font=(theme.FONT_FAMILY, 9, "bold"),
            padx=10,
            pady=3,
        )
        self.status_pill.grid(row=0, column=1, sticky="e", padx=(10, 0))

        self.detail_sub = tk.Label(
            top,
            text="",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.MONO_FAMILY, 9),
            anchor="w",
        )
        self.detail_sub.grid(row=1, column=0, sticky="w", pady=(2, 0))

        self.detail_desc = tk.Label(
            top,
            text="",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
            wraplength=580,
        )
        self.detail_desc.grid(row=2, column=0, sticky="w", pady=(6, 0))

        self.detail_warning = tk.Label(
            content,
            text="",
            bg=theme.WARN_BG,
            fg=theme.WARN,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
            wraplength=600,
            padx=10,
            pady=7,
        )
        self.detail_warning.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.detail_warning.grid_remove()

        actions = tk.Frame(content, bg=theme.CARD)
        actions.grid(row=2, column=0, sticky="ew", pady=(12, 10))
        self.btn_start = ttk.Button(
            actions, text="Запустить", style="Success.TButton", command=self.start_selected
        )
        self.btn_stop = ttk.Button(
            actions, text="Остановить", style="Secondary.TButton", command=self.stop_selected
        )
        self.btn_restart = ttk.Button(
            actions, text="Перезапустить", style="Secondary.TButton", command=self.restart_selected
        )
        self.btn_edit = ttk.Button(
            actions, text="Изменить", style="Accent.TButton", command=self.edit_selected
        )
        self.btn_delete = ttk.Button(
            actions, text="Удалить", style="Danger.TButton", command=self.delete_selected
        )
        self.btn_log = ttk.Button(
            actions, text="Журнал", style="Ghost.TButton", command=self.open_selected_log
        )
        self.btn_folder = ttk.Button(
            actions, text="Папка файла", style="Ghost.TButton", command=self.reveal_selected_target
        )
        for button in (
            self.btn_start,
            self.btn_stop,
            self.btn_restart,
            self.btn_edit,
            self.btn_delete,
            self.btn_log,
            self.btn_folder,
        ):
            button.pack(side="left", padx=(0, 6))

        widgets.separator(content).grid(row=4, column=0, sticky="ew")

        self.info_scroll = widgets.ScrollFrame(content, background=theme.CARD)
        self.info_scroll.grid(row=3, column=0, sticky="nsew", pady=(10, 0))
        grid = self.info_scroll.inner
        grid.grid_columnconfigure(1, weight=1)

        self.detail_rows: dict[str, DetailRow] = {}
        definitions = [
            ("target", "Целевой файл", "Открыть"),
            ("arguments", "Аргументы", None),
            ("workdir", "Рабочая папка", None),
            ("start", "Тип запуска", None),
            ("account", "Учётная запись", None),
            ("dependencies", "Зависимости", None),
            ("restart", "Автоперезапуск", None),
            ("log", "Журнал", "Открыть"),
            ("name", "Имя службы", None),
        ]
        for index, (key, label, button_text) in enumerate(definitions):
            self.detail_rows[key] = DetailRow(grid, index, label, button_text=button_text)
        self.detail_rows["target"].button.configure(command=self.reveal_selected_target)
        self.detail_rows["log"].button.configure(command=self.open_selected_log)
        self.info_scroll.inner.bind("<Configure>", self._resize_detail_rows)
        self.detail_frame.bind("<Configure>", self._resize_detail_rows)

    def _build_statusbar(self) -> None:
        bar = tk.Frame(self.root, bg=theme.CARD, height=32, highlightthickness=1, highlightbackground=theme.BORDER)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        left = tk.Frame(bar, bg=theme.CARD)
        left.pack(side="left", fill="y", padx=16)
        self.status_label = tk.Label(
            left,
            text="Готово",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
        )
        self.status_label.pack(side="left", pady=7)
        self.progress = ttk.Progressbar(
            left,
            mode="indeterminate",
            length=90,
            style="Accent.Horizontal.TProgressbar",
        )

        right = tk.Frame(bar, bg=theme.CARD)
        right.pack(side="right", fill="y", padx=16)
        version = tk.Label(
            right,
            text=f"v{APP_VERSION}",
            bg=theme.CARD,
            fg=theme.SUBTLE,
            font=(theme.FONT_FAMILY, 9),
        )
        version.pack(side="right", pady=7)
        data_link = tk.Label(
            right,
            text="Папка данных",
            bg=theme.CARD,
            fg=theme.ACCENT_DARK,
            font=(theme.FONT_FAMILY, 9),
            cursor="hand2",
        )
        data_link.pack(side="right", padx=(0, 14), pady=7)
        data_link.bind("<Button-1>", lambda _e: self.open_data_folder())

    def _bind_events(self) -> None:
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Delete>", lambda _e: self.delete_selected())
        self.tree.bind("<Return>", lambda _e: self.edit_selected())
        self.search_var.trace_add("write", lambda *_args: self._populate())
        self.root.bind("<Control-n>", lambda _e: self.create_service())
        self.root.bind("<Control-N>", lambda _e: self.create_service())
        self.root.bind("<Control-e>", lambda _e: self.edit_selected())
        self.root.bind("<Control-E>", lambda _e: self.edit_selected())
        self.root.bind("<F5>", lambda _e: self.refresh(deep=True))
        self.root.bind("<Control-q>", lambda _e: self.quit_app())
        self.root.bind("<Control-Q>", lambda _e: self.quit_app())
        self.root.protocol("WM_DELETE_WINDOW", self.close_requested)
        self.root.bind("<Unmap>", self._on_unmap)

    # -------------------------------------------------------------------- tray
    def _setup_tray(self) -> None:
        if not self.enable_tray:
            self.tray = None
            self.tray_available = False
            return
        self.tray = TrayController(
            self.root,
            on_open=self.show_window,
            on_create=self._tray_create,
            on_refresh=lambda: self.refresh(deep=True),
            on_exit=self.quit_app,
        )
        self.tray_available = self.tray.start()

    def _tray_create(self) -> None:
        self.show_window()
        self.create_service()

    def _on_unmap(self, event) -> None:
        if event.widget is not self.root:
            return
        try:
            if self.root.state() == "iconic":
                self.root.after(1, self.hide_to_tray)
        except tk.TclError:
            pass

    def close_requested(self) -> None:
        if self.tray_available:
            self.hide_to_tray()
        else:
            self.quit_app()

    def hide_to_tray(self) -> None:
        if self._closing:
            return
        if not self.tray_available:
            self.quit_app()
            return
        try:
            self.root.withdraw()
        except tk.TclError:
            return
        if not self._tray_hint_shown:
            self._tray_hint_shown = True
            self.tray.notify(
                "Приложение свёрнуто в трей. Двойной щелчок по значку откроет окно."
            )

    def show_window(self) -> None:
        if self._closing:
            return
        try:
            self.root.deiconify()
            self.root.state("normal")
            self.root.lift()
            self.root.focus_force()
        except tk.TclError:
            pass

    def quit_app(self) -> None:
        if self._closing:
            return
        if self.busy:
            if not messagebox.askyesno(
                "Идёт операция",
                "Операция со службой ещё выполняется. Выйти из программы?",
            ):
                return
        self._closing = True
        if self._poll_after is not None:
            try:
                self.root.after_cancel(self._poll_after)
            except tk.TclError:
                pass
            self._poll_after = None
        try:
            if self.tray is not None:
                self.tray.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ data
    def _status_kind(self, row: scm.ServiceRow) -> str:
        if not row.registered:
            return "unregistered"
        state = row.state
        if state == win32service.SERVICE_RUNNING:
            return "running"
        if state in (
            win32service.SERVICE_START_PENDING,
            win32service.SERVICE_STOP_PENDING,
            win32service.SERVICE_CONTINUE_PENDING,
            win32service.SERVICE_PAUSE_PENDING,
        ):
            return "pending"
        if state == win32service.SERVICE_PAUSED:
            return "paused"
        if state == win32service.SERVICE_STOPPED:
            code = row.details.exit_code if row.details else 0
            if code and code not in (1077, 0):
                return "error"
        return "stopped"

    def refresh(self, deep: bool = True, select: str | None = None) -> None:
        if self._closing:
            return
        if not deep:
            self._poll_once()
            return
        try:
            rows = scm.list_services()
        except Exception as exc:
            log.exception("Не удалось получить список служб")
            self.show_status(winapi.format_error(exc, "Не удалось получить список служб"), "error")
            return
        self.rows = rows
        self.row_map = {row.name: row for row in rows}
        self.registered_names = {row.name for row in rows if row.registered}
        if select:
            self.selected_name = select
        self._populate()
        self._update_detail()
        self._update_counts()

    def _filtered_rows(self) -> list[scm.ServiceRow]:
        query = self.search.get().casefold()
        if not query:
            return self.rows
        terms = [part for part in query.split() if part]
        result = []
        for row in self.rows:
            target = row.config.target if row.config else ""
            haystack = f"{row.display_name} {row.name} {target} {row.state_label}".casefold()
            if all(term in haystack for term in terms):
                result.append(row)
        return result

    def _populate(self) -> None:
        if not hasattr(self, "tree"):
            return
        self._populating = True
        visible = self._filtered_rows()
        try:
            self.tree.delete(*self.tree.get_children())
            for row in visible:
                kind = self._status_kind(row)
                state_text = row.state_label
                if kind == "error":
                    state_text += " (сбой)"
                self.tree.insert(
                    "",
                    "end",
                    iid=row.name,
                    image=self._status_images[kind],
                    values=(
                        row.display_name,
                        row.name,
                        state_text,
                        row.start_label or "—",
                    ),
                )
            if self.selected_name and self.selected_name in self.row_map:
                if self.tree.exists(self.selected_name):
                    self.tree.selection_set(self.selected_name)
                    self.tree.focus(self.selected_name)
                    self.tree.see(self.selected_name)
                else:
                    self.selected_name = None
            elif visible:
                self.selected_name = visible[0].name
                self.tree.selection_set(self.selected_name)
                self.tree.focus(self.selected_name)
        finally:
            self._populating = False

        total = len(self.rows)
        shown = len(visible)
        self.count_label.configure(text=f"Служб: {total}")
        self.filtered_label.configure(
            text=f"Показано: {shown}" if shown != total else ""
        )
        if not total:
            self.empty_title.configure(text="Служб пока нет")
            self.empty_text.configure(
                text=(
                    "Создайте первую службу: выберите .exe или другой файл,\n"
                    "задайте параметры запуска — и приложение будет работать\n"
                    "в фоне и стартовать вместе с Windows."
                )
            )
            self.empty_button.configure(text="+  Создать первую службу")
        else:
            self.empty_title.configure(text="Служба не выбрана")
            self.empty_text.configure(
                text=(
                    "Выберите службу в списке слева, чтобы увидеть её состояние,\n"
                    "запустить, остановить или изменить настройки."
                )
            )
            self.empty_button.configure(text="+  Создать службу")
        if self.selected_name:
            self._update_detail()
        else:
            self.empty_frame.tkraise()

    def _update_counts(self) -> None:
        running = sum(
            1 for row in self.rows if row.state == win32service.SERVICE_RUNNING
        )
        self.count_label.configure(
            text=f"Служб: {len(self.rows)}    ·    работают: {running}"
        )

    def _on_select(self, _event=None) -> None:
        if self._populating:
            return
        selection = self.tree.selection()
        self.selected_name = selection[0] if selection else None
        self._update_detail()

    def _on_double_click(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self.selected_name = row_id
            self._update_detail()
            self.edit_selected()

    def _selected_row(self) -> scm.ServiceRow | None:
        if not self.selected_name:
            return None
        return self.row_map.get(self.selected_name)

    # ---------------------------------------------------------------- details
    def _update_detail(self) -> None:
        row = self._selected_row()
        if row is None:
            self.empty_frame.tkraise()
            return
        self.detail_frame.tkraise()
        self.detail_title.configure(text=row.display_name)
        self.detail_sub.configure(text=row.name)
        config = row.config or ServiceConfig(name=row.name, display_name=row.display_name)

        description = config.description.strip()
        if description:
            self.detail_desc.configure(text=description, fg=theme.MUTED)
        else:
            self.detail_desc.configure(text="Описание не задано.", fg=theme.SUBTLE)

        kind = self._status_kind(row)
        fg, bg = theme.STATUS_COLORS[kind]
        self.status_pill.configure(text=row.state_label, bg=bg, fg=fg)

        if row.missing_config:
            self.detail_warning.configure(
                text=(
                    "Файл настроек этой службы не найден. Она была создана программой, "
                    "но конфигурация потеряна. Откройте «Изменить», проверьте параметры и сохраните."
                )
            )
            self.detail_warning.grid()
        elif not row.registered:
            self.detail_warning.configure(
                text=(
                    "Служба не зарегистрирована в Windows. Настройки сохранены — "
                    "нажмите «Изменить» и сохраните, чтобы создать её снова."
                )
            )
            self.detail_warning.grid()
        else:
            self.detail_warning.grid_remove()

        target = config.target or ""
        self.detail_rows["target"].set(target or "—", theme.DANGER if target and not Path(target).exists() else None)
        self.detail_rows["arguments"].set(config.arguments.strip() or "—")
        workdir = effective_working_dir(config)
        self.detail_rows["workdir"].set(workdir or "—")
        self.detail_rows["start"].set(row.start_label or "—")
        account_text = row.account_label
        if config.account_type not in (ACCOUNT_SYSTEM, ACCOUNT_LOCAL_SERVICE, ACCOUNT_NETWORK_SERVICE):
            account_text = config.account or "—"
        self.detail_rows["account"].set(account_text or "—")
        self.detail_rows["dependencies"].set(", ".join(config.dependencies) or "—")
        self.detail_rows["restart"].set(self._restart_summary(config))
        if config.log_enabled:
            self.detail_rows["log"].set(config.log_path or paths.default_log_path(row.name))
        else:
            self.detail_rows["log"].set("Отключён в настройках")
        self.detail_rows["name"].set(row.name)

        self.detail_rows["target"].button.configure(
            state="normal" if target and Path(target).exists() else "disabled"
        )
        self.detail_rows["log"].button.configure(
            state="normal" if config.log_enabled else "disabled"
        )
        self._update_action_buttons(row)
        self._resize_detail_rows()
        self._update_counts()

    def _restart_summary(self, config: ServiceConfig) -> str:
        if not config.auto_restart:
            return "Выключен"
        parts = [f"Включён, задержка {config.restart_delay} с"]
        if config.max_restarts:
            parts.append(
                f"до {config.max_restarts} попыток за {config.restart_window_minutes} мин"
            )
        else:
            parts.append("число попыток не ограничено")
        if config.restart_on_crash_only:
            parts.append("только при аварийном завершении")
        return "; ".join(parts)

    def _update_action_buttons(self, row: scm.ServiceRow) -> None:
        can_manage = self.admin and not self.busy
        state = row.state
        registered = row.registered
        running = state == win32service.SERVICE_RUNNING
        pending = state in (
            win32service.SERVICE_START_PENDING,
            win32service.SERVICE_STOP_PENDING,
        )
        self.btn_start.configure(
            state="normal" if can_manage and registered and state == win32service.SERVICE_STOPPED else "disabled"
        )
        self.btn_stop.configure(
            state="normal" if can_manage and registered and (running or pending or state == win32service.SERVICE_PAUSED) else "disabled"
        )
        self.btn_restart.configure(
            state="normal" if can_manage and registered and not pending else "disabled"
        )
        self.btn_edit.configure(state="normal" if can_manage else "disabled")
        self.btn_delete.configure(
            state="normal" if can_manage and (registered or row.config is not None) else "disabled"
        )
        log_enabled = bool(row.config and row.config.log_enabled)
        self.btn_log.configure(state="normal" if log_enabled and not self.busy else "disabled")
        target = row.config.target if row.config else ""
        self.btn_folder.configure(
            state="normal" if target and Path(target).exists() and not self.busy else "disabled"
        )

    def _resize_detail_rows(self, event=None) -> None:
        if not hasattr(self, "info_scroll"):
            return
        width = max(220, self.info_scroll.inner.winfo_width() - 150)
        for detail in self.detail_rows.values():
            detail.value.configure(wraplength=width)
        self.detail_desc.configure(wraplength=max(300, self.detail_stack.winfo_width() - 40))

    def _show_context_menu(self, event) -> None:
        row_id = self.tree.identify_row(event.y)
        if row_id:
            self.tree.selection_set(row_id)
            self.selected_name = row_id
            self._update_detail()
        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="Изменить…", command=self.edit_selected)
        menu.add_separator()
        menu.add_command(label="Запустить", command=self.start_selected)
        menu.add_command(label="Остановить", command=self.stop_selected)
        menu.add_command(label="Перезапустить", command=self.restart_selected)
        menu.add_separator()
        menu.add_command(label="Открыть журнал", command=self.open_selected_log)
        menu.add_command(label="Показать файл", command=self.reveal_selected_target)
        menu.add_separator()
        menu.add_command(label="Копировать имя", command=self.copy_selected_name)
        menu.add_command(label="Удалить…", command=self.delete_selected)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ----------------------------------------------------------------- actions
    def show_status(self, text: str, kind: str = "info") -> None:
        color = {
            "info": theme.MUTED,
            "success": theme.SUCCESS,
            "error": theme.DANGER,
            "busy": theme.ACCENT_DARK,
        }.get(kind, theme.MUTED)
        self.status_label.configure(text=text, fg=color)

    def _set_busy(self, busy: bool, text: str = "") -> None:
        self.busy = busy
        if busy:
            self.show_status(text, "busy")
            self.progress.pack(side="left", padx=(10, 0), pady=9)
            self.progress.start(12)
            self.root.configure(cursor="watch")
            self.create_button.configure(state="disabled")
            row = self._selected_row()
            if row:
                self._update_action_buttons(row)
            else:
                for button in (self.btn_start, self.btn_stop, self.btn_restart, self.btn_edit, self.btn_delete):
                    button.configure(state="disabled")
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.root.configure(cursor="")
            self.create_button.configure(state="normal" if self.admin else "disabled")
            row = self._selected_row()
            if row:
                self._update_action_buttons(row)

    def _run_operation(
        self,
        busy_text: str,
        action: Callable[[], None],
        success_text: str,
        select: str | None = None,
        clear_selection: bool = False,
    ) -> None:
        if self.busy or self._closing:
            return
        if not self.admin:
            if self._offer_elevation():
                return
            return
        self._set_busy(True, busy_text)

        def worker() -> None:
            error: BaseException | None = None
            try:
                action()
            except BaseException as exc:  # noqa: BLE001 - shown to the user
                log.exception("Операция со службой не выполнена")
                error = exc
            self.root.after(0, lambda: self._finish_operation(error, success_text, select, clear_selection))

        threading.Thread(target=worker, name="service-operation", daemon=True).start()

    def _finish_operation(
        self,
        error: BaseException | None,
        success_text: str,
        select: str | None,
        clear_selection: bool,
    ) -> None:
        self._set_busy(False)
        if clear_selection:
            self.selected_name = None
        if error is not None:
            message = winapi.format_error(error)
            self.show_status(message, "error")
            messagebox.showerror("Операция не выполнена", message, parent=self.root)
        else:
            self.show_status(success_text, "success")
        self.refresh(deep=True, select=select if not clear_selection else None)

    def _require_admin(self) -> bool:
        if self.admin:
            return True
        return self._offer_elevation()

    def _offer_elevation(self) -> bool:
        if messagebox.askyesno(
            "Требуются права администратора",
            "Для управления службами нужно запустить программу от имени администратора.\n\n"
            "Перезапустить сейчас?",
            parent=self.root,
        ):
            self.elevate()
        return False

    def elevate(self) -> None:
        if winapi.restart_as_admin():
            self.quit_app()
        else:
            self.show_status("Не удалось получить права администратора.", "error")

    def create_service(self) -> None:
        if self.busy:
            return
        if not self._require_admin():
            return
        config = ServiceConfig()
        editor = ServiceEditor(self.root, self, config, mode="create")
        self.root.wait_window(editor)
        if editor.saved_config is None:
            return
        name = editor.saved_config.name
        self.refresh(deep=True, select=name)
        if editor.start_after:
            self._run_operation(
                "Запуск службы…",
                lambda: scm.start_service(name),
                "Служба создана и запущена.",
                select=name,
            )
        else:
            self.show_status("Служба создана.", "success")

    def edit_selected(self) -> None:
        row = self._selected_row()
        if row is None or self.busy:
            return
        if not self._require_admin():
            return
        try:
            config = scm.load_effective_config(row.name)
        except Exception as exc:
            messagebox.showerror(
                "Не удалось прочитать настройки",
                winapi.format_error(exc),
                parent=self.root,
            )
            return
        editor = ServiceEditor(self.root, self, config, mode="edit", row=row)
        self.root.wait_window(editor)
        if editor.saved_config is None:
            return
        name = editor.saved_config.name
        self.refresh(deep=True, select=name)
        if editor.start_after:
            self._run_operation(
                "Перезапуск службы…",
                lambda: scm.restart_service(name),
                "Служба перезапущена.",
                select=name,
            )
        else:
            self.show_status("Настройки сохранены.", "success")

    def start_selected(self) -> None:
        row = self._selected_row()
        if row is None or not row.registered:
            return
        self._run_operation(
            f"Запуск службы «{row.name}»…",
            lambda: scm.start_service(row.name),
            "Служба запущена.",
            select=row.name,
        )

    def stop_selected(self) -> None:
        row = self._selected_row()
        if row is None or not row.registered:
            return
        self._run_operation(
            f"Остановка службы «{row.name}»…",
            lambda: scm.stop_service(row.name),
            "Служба остановлена.",
            select=row.name,
        )

    def restart_selected(self) -> None:
        row = self._selected_row()
        if row is None or not row.registered:
            return
        self._run_operation(
            f"Перезапуск службы «{row.name}»…",
            lambda: scm.restart_service(row.name),
            "Служба перезапущена.",
            select=row.name,
        )

    def delete_selected(self) -> None:
        row = self._selected_row()
        if row is None or self.busy:
            return
        if not self._require_admin():
            return
        if row.registered:
            title = "Удалить службу?"
            question = (
                f"Служба «{row.display_name}» будет остановлена и удалена из Windows.\n\n"
                "Файл программы и журнал останутся на диске. Настройки службы будут удалены.\n\n"
                "Продолжить?"
            )
        else:
            title = "Удалить настройки?"
            question = (
                f"Служба «{row.display_name}» не зарегистрирована в Windows.\n"
                "Удалить сохранённые настройки этой службы?"
            )
        if not messagebox.askyesno(title, question, parent=self.root, icon="warning"):
            return

        def action() -> None:
            if row.registered:
                scm.delete_service(row.name)
            else:
                config_store.delete_config(row.name)

        self.selected_name = None
        self._run_operation(
            f"Удаление службы «{row.name}»…",
            action,
            "Служба удалена.",
            select=None,
            clear_selection=True,
        )

    def open_selected_log(self) -> None:
        row = self._selected_row()
        if row is None or row.config is None:
            return
        config = row.config
        path = config.log_path or paths.default_log_path(row.name)
        if path and Path(path).is_file():
            try:
                winapi.open_path(path)
            except OSError as exc:
                messagebox.showerror("Не удалось открыть журнал", str(exc), parent=self.root)
        else:
            folder = Path(path).parent if path else paths.log_dir()
            if folder.is_dir():
                winapi.open_folder(folder)
            else:
                messagebox.showinfo(
                    "Журнал пуст",
                    "Файл журнала ещё не создан. Запустите службу — журнал появится автоматически.",
                    parent=self.root,
                )

    def reveal_selected_target(self) -> None:
        row = self._selected_row()
        if row is None or row.config is None:
            return
        target = row.config.target
        if target and Path(target).exists():
            winapi.reveal_path(target)
        else:
            messagebox.showinfo(
                "Файл не найден",
                f"Целевой файл недоступен:\n{target or '—'}",
                parent=self.root,
            )

    def copy_selected_name(self) -> None:
        row = self._selected_row()
        if row is not None:
            self.root.clipboard_clear()
            self.root.clipboard_append(row.name)
            self.show_status("Имя службы скопировано в буфер обмена.", "success")

    def open_data_folder(self) -> None:
        folder = paths.data_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            winapi.open_folder(folder)
        except OSError as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self.root)

    def open_app_log(self) -> None:
        path = paths.app_log_path()
        if path.is_file():
            try:
                winapi.open_path(path)
            except OSError as exc:
                messagebox.showerror("Не удалось открыть журнал", str(exc), parent=self.root)
        else:
            messagebox.showinfo("Журнал пуст", "Журнал приложения пока не создан.", parent=self.root)

    def import_service(self) -> None:
        if self.busy or not self._require_admin():
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Импорт конфигурации службы",
            filetypes=[("Конфигурация службы", "*.json"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            config = config_store.import_config(path)
        except Exception as exc:
            messagebox.showerror("Не удалось импортировать", str(exc), parent=self.root)
            return
        if scm.service_exists(config.name) or config_store.config_exists(config.name):
            messagebox.showerror(
                "Имя уже используется",
                f"Служба или конфигурация с именем «{config.name}» уже существует.\n"
                "Измените внутреннее имя в файле и повторите импорт.",
                parent=self.root,
            )
            return
        editor = ServiceEditor(self.root, self, config, mode="create")
        self.root.wait_window(editor)
        if editor.saved_config:
            self.refresh(deep=True, select=editor.saved_config.name)
            if editor.start_after:
                self._run_operation(
                    "Запуск службы…",
                    lambda: scm.start_service(editor.saved_config.name),
                    "Служба создана и запущена.",
                    select=editor.saved_config.name,
                )
            else:
                self.show_status("Служба создана из файла.", "success")

    def export_selected(self) -> None:
        row = self._selected_row()
        if row is None or row.config is None:
            messagebox.showinfo("Нет выбранной службы", "Сначала выберите службу в списке.", parent=self.root)
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Экспорт конфигурации",
            defaultextension=".json",
            initialfile=f"{row.name}.json",
            filetypes=[("Конфигурация службы", "*.json")],
        )
        if not path:
            return
        try:
            config_store.export_config(row.config, path)
            self.show_status("Конфигурация экспортирована.", "success")
        except OSError as exc:
            messagebox.showerror("Не удалось экспортировать", str(exc), parent=self.root)

    def show_help(self) -> None:
        messagebox.showinfo(
            "Как это работает",
            "Программа регистрирует в Windows службу, которая запускает выбранный файл "
            "в фоне и при старте системы.\n\n"
            "• Целевой файл выполняется как дочерний процесс службы.\n"
            "• Автоперезапуск следит за завершением программы и запускает её снова.\n"
            "• Для .bat, .ps1, .py и других скриптов автоматически подбирается интерпретатор.\n"
            "• Служба может работать от LocalSystem, LocalService, NetworkService или "
            "пользовательской учётной записи.\n"
            "• Журнал службы: папка данных → logs.\n"
            "• Службы работают независимо от окна программы, в том числе после её закрытия.\n\n"
            "Изменения вступают в силу после перезапуска службы.",
            parent=self.root,
        )

    def show_about(self) -> None:
        messagebox.showinfo(
            "О программе",
            f"{APP_NAME}\nВерсия {APP_VERSION}\n\n"
            "Создание и управление службами Windows из одного окна.\n"
            "Работает без установки Python на целевой машине.\n\n"
            f"Папка данных:\n{paths.data_dir()}",
            parent=self.root,
        )

    # ------------------------------------------------------------------- poll
    def _schedule_poll(self) -> None:
        if self._closing:
            return
        self._poll_after = self.root.after(2500, self._poll_tick)

    def _poll_tick(self) -> None:
        if self._closing:
            return
        try:
            self._poll_once()
        except Exception:
            log.debug("Не удалось обновить состояния служб", exc_info=True)
        self._schedule_poll()

    def _poll_once(self) -> None:
        if not self.rows:
            return
        try:
            states = scm.poll_states()
        except Exception:
            return
        removed = [name for name in self.registered_names if name not in states]
        if removed:
            self.refresh(deep=True, select=self.selected_name)
            return
        changed = False
        need_deep = False
        for row in self.rows:
            if not row.registered:
                continue
            new_state = states.get(row.name)
            if new_state is None:
                continue
            if new_state != row.state:
                if row.state == win32service.SERVICE_RUNNING and new_state == win32service.SERVICE_STOPPED:
                    need_deep = True
                row.state = new_state
                changed = True
            if self.tree.exists(row.name):
                kind = self._status_kind(row)
                state_text = row.state_label + (" (сбой)" if kind == "error" else "")
                self.tree.item(
                    row.name,
                    image=self._status_images[kind],
                    values=(
                        row.display_name,
                        row.name,
                        state_text,
                        row.start_label or "—",
                    ),
                )
        if changed:
            self._update_counts()
            row = self._selected_row()
            if row is not None:
                fg, bg = theme.STATUS_COLORS[self._status_kind(row)]
                self.status_pill.configure(text=row.state_label, bg=bg, fg=fg)
                self._update_action_buttons(row)
        if need_deep:
            self.root.after(500, lambda: self.refresh(deep=True, select=self.selected_name))
