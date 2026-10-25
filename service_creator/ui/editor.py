"""Service configuration editor dialog."""

from __future__ import annotations

import logging
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .. import config_store, paths, scm, winapi
from ..launcher import build_command, effective_launcher, expand_path, test_command
from ..models import (
    ACCOUNT_CUSTOM,
    ACCOUNT_LOCAL_SERVICE,
    ACCOUNT_NETWORK_SERVICE,
    ACCOUNT_SYSTEM,
    ACCOUNT_TYPE_BY_LABEL,
    ACCOUNT_TYPE_LABELS,
    FAILURE_BY_LABEL,
    FAILURE_COMMAND,
    FAILURE_LABELS,
    FAILURE_NONE,
    FAILURE_REBOOT,
    LAUNCHER_AUTO,
    LAUNCHER_BY_LABEL,
    LAUNCHER_CUSTOM,
    LAUNCHER_LABELS,
    PRIORITY_BY_LABEL,
    PRIORITY_LABELS,
    START_TYPE_BY_LABEL,
    START_TYPE_LABELS,
    FailureAction,
    ServiceConfig,
    format_dependencies,
    format_env_text,
    parse_dependencies,
    parse_env_text,
    validate_service_name,
)
from . import theme, widgets

log = logging.getLogger(__name__)

TAB_MAIN = 0
TAB_STARTUP = 1
TAB_RECOVERY = 2
TAB_LOG = 3

FILE_TYPES = [
    ("Программы и скрипты", "*.exe *.com *.bat *.cmd *.ps1 *.py *.pyw *.vbs *.js *.wsf"),
    ("Исполняемые файлы", "*.exe *.com"),
    ("Все файлы", "*.*"),
]

ACCOUNT_HINTS = {
    ACCOUNT_SYSTEM: "Полный доступ к компьютеру. Пароль не требуется.",
    ACCOUNT_LOCAL_SERVICE: "Ограниченные права; сетевой доступ как у анонимного пользователя.",
    ACCOUNT_NETWORK_SERVICE: "Ограниченные права; сеть доступна от имени компьютера.",
    ACCOUNT_CUSTOM: "Укажите DOMAIN\\Пользователь или .\\Пользователь. Пароль не сохраняется в настройках.",
}

START_HINTS = {
    "auto": "Служба запускается автоматически при старте Windows.",
    "delayed": "Запускается при старте Windows с задержкой. Рекомендуется для сетевых программ.",
    "manual": "Служба запускается только вручную из этой программы или оснастки «Службы».",
    "disabled": "Служба зарегистрирована, но Windows не будет её запускать.",
}


class ValidationError(Exception):
    def __init__(self, message: str, tab: int = TAB_MAIN, widget=None):
        super().__init__(message)
        self.message = message
        self.tab = tab
        self.widget = widget


class _Form:
    """Small grid-based form builder used inside the scrollable tabs."""

    def __init__(self, parent: tk.Frame):
        self.parent = parent
        self.row = 0
        parent.grid_columnconfigure(1, weight=1)

    def section(self, title: str, subtitle: str | None = None) -> None:
        frame = widgets.section_title(self.parent, title, subtitle)
        frame.grid(
            row=self.row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(16 if self.row else 0, 8),
        )
        self.row += 1

    def field(self, label: str, hint: str | None = None) -> tk.Frame:
        tk.Label(
            self.parent,
            text=label,
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="nw",
            justify="left",
            width=23,
            wraplength=175,
        ).grid(row=self.row, column=0, sticky="nw", padx=(0, 18), pady=(7, 0))

        holder = tk.Frame(self.parent, bg=theme.CARD)
        holder.grid(row=self.row, column=1, sticky="ew", pady=(7, 0))
        holder.grid_columnconfigure(0, weight=1)
        self.row += 1

        if hint:
            tk.Label(
                self.parent,
                text=hint,
                bg=theme.CARD,
                fg=theme.SUBTLE,
                font=(theme.FONT_FAMILY, 9),
                anchor="w",
                justify="left",
                wraplength=620,
            ).grid(row=self.row, column=1, sticky="ew", pady=(2, 0))
            self.row += 1
        return holder

    def note(self, text: str, kind: str = "warn") -> tk.Label:
        colors = {
            "warn": (theme.WARN, theme.WARN_BG),
            "info": (theme.MUTED, "#f8fafc"),
        }
        fg, bg = colors.get(kind, colors["warn"])
        label = tk.Label(
            self.parent,
            text=text,
            bg=bg,
            fg=fg,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
            padx=10,
            pady=7,
            wraplength=640,
        )
        label.grid(
            row=self.row,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(10, 0),
        )
        self.row += 1
        return label


def _suggest_name(path: str) -> str:
    stem = Path(path).stem
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")
    if not candidate or not candidate[0].isalnum():
        candidate = "Service" + candidate
    candidate = candidate[:48] or "MyService"
    if candidate.upper() in {"CON", "PRN", "AUX", "NUL"}:
        candidate += "Service"
    return candidate


def _set_text_value(widget: tk.Text, value: str) -> None:
    widget.configure(state="normal")
    widgets.set_text(widget, value)
    widget.configure(state="disabled")


def _compact(holder: tk.Frame) -> None:
    """Keep a control group left-aligned instead of stretching to the row width."""
    holder.grid_columnconfigure(0, weight=0)


class ServiceEditor(tk.Toplevel):
    """Modal window that creates or edits a single service."""

    def __init__(self, parent, app, config: ServiceConfig, mode: str = "create", row=None):
        super().__init__(parent)
        self.parent = parent
        self.app = app
        self.mode = mode
        self.is_edit = mode == "edit"
        self.service_row = row
        self.original = config.clone()
        self.saved_config: ServiceConfig | None = None
        self.start_after = False
        self._alive = True
        self._saving = False
        self._scroll_frames: list[widgets.ScrollFrame] = []
        self._results: queue.Queue = queue.Queue()

        self.title("Изменить службу" if self.is_edit else "Новая служба")
        self.configure(bg=theme.BG)
        self.transient(parent)
        self.resizable(True, True)
        self.minsize(940, 680)
        self._icon = theme.icon_photo(self, 32)
        try:
            self.iconphoto(False, self._icon)
        except tk.TclError:
            pass

        self._create_variables()
        self._build()
        widgets.center_window(self, parent, 1020, 820)
        self._bind_state()
        self._load_config(config)
        self._sync_all()
        self._update_preview()

        self.protocol("WM_DELETE_WINDOW", self._on_cancel)
        self.bind("<Escape>", lambda _event: self._on_cancel())
        self.bind("<Control-s>", lambda _event: self._save())
        self.bind("<Control-S>", lambda _event: self._save())
        self.grab_set()
        self.after(60, self._initial_focus)
        self.after(100, self._drain_results)

    # ------------------------------------------------------------- construction
    def _create_variables(self) -> None:
        self.name_var = tk.StringVar()
        self.display_var = tk.StringVar()
        self.target_var = tk.StringVar()
        self.args_var = tk.StringVar()
        self.workdir_var = tk.StringVar()
        self.launcher_var = tk.StringVar(value=LAUNCHER_LABELS[LAUNCHER_AUTO])
        self.interpreter_var = tk.StringVar()
        self.start_var = tk.StringVar(value=START_TYPE_LABELS["auto"])
        self.account_type_var = tk.StringVar(value=ACCOUNT_TYPE_LABELS[ACCOUNT_SYSTEM])
        self.account_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.password2_var = tk.StringVar()
        self.grant_var = tk.BooleanVar(value=True)
        self.deps_var = tk.StringVar()
        self.priority_var = tk.StringVar(value=PRIORITY_LABELS["normal"])
        self.auto_restart_var = tk.BooleanVar(value=True)
        self.crash_only_var = tk.BooleanVar(value=True)
        self.restart_delay_var = tk.StringVar(value="5")
        self.max_restarts_var = tk.StringVar(value="0")
        self.restart_window_var = tk.StringVar(value="60")
        self.failure_action_vars = [
            tk.StringVar(value=FAILURE_LABELS[FAILURE_NONE]) for _ in range(3)
        ]
        self.failure_delay_vars = [tk.StringVar(value="60") for _ in range(3)]
        self.failure_command_var = tk.StringVar()
        self.failure_reset_var = tk.StringVar(value="1")
        self.failure_flag_var = tk.BooleanVar(value=True)
        self.stop_timeout_var = tk.StringVar(value="10")
        self.kill_tree_var = tk.BooleanVar(value=True)
        self.log_enabled_var = tk.BooleanVar(value=True)
        self.log_path_var = tk.StringVar()
        self.log_append_var = tk.IntVar(value=1)
        self.log_max_var = tk.StringVar(value="10")
        self.start_after_var = tk.BooleanVar(value=not self.is_edit)

    def _build(self) -> None:
        self._build_header()
        self._build_footer()
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(4, 8))
        self._build_main_tab()
        self._build_startup_tab()
        self._build_recovery_tab()
        self._build_log_tab()

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=theme.CARD, highlightthickness=1, highlightbackground=theme.BORDER)
        header.pack(fill="x")
        inner = tk.Frame(header, bg=theme.CARD)
        inner.pack(fill="x", padx=18, pady=12)

        tk.Label(inner, image=self._icon, bg=theme.CARD).pack(side="left", padx=(0, 12))
        block = tk.Frame(inner, bg=theme.CARD)
        block.pack(side="left", fill="y")
        tk.Label(
            block,
            text="Изменить службу" if self.is_edit else "Новая служба",
            bg=theme.CARD,
            fg=theme.TEXT,
            font=(theme.FONT_FAMILY, 15, "bold"),
            anchor="w",
        ).pack(anchor="w")
        self.header_hint = tk.Label(
            block,
            text="Настройки запуска приложения как службы Windows",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
        )
        self.header_hint.pack(anchor="w")

        if not winapi.is_admin():
            tk.Label(
                inner,
                text="Без прав администратора сохранение недоступно",
                bg=theme.DANGER_BG,
                fg=theme.DANGER,
                font=(theme.FONT_FAMILY, 9, "bold"),
                padx=10,
                pady=4,
            ).pack(side="right", pady=8)

    def _add_tab(self, title: str) -> tuple[tk.Frame, widgets.ScrollFrame]:
        page = tk.Frame(self.notebook, bg=theme.CARD)
        self.notebook.add(page, text=f"  {title}  ")
        scroll = widgets.ScrollFrame(page, background=theme.CARD)
        scroll.pack(fill="both", expand=True)
        body = tk.Frame(scroll.inner, bg=theme.CARD)
        body.pack(fill="both", expand=True, padx=20, pady=(16, 20))
        self._scroll_frames.append(scroll)
        return body, scroll

    def _build_main_tab(self) -> None:
        body, scroll = self._add_tab("Главное")
        form = _Form(body)

        form.section("Основное", "Название службы в Windows и её описание.")
        holder = form.field("Отображаемое имя")
        self.display_entry = ttk.Entry(holder, textvariable=self.display_var)
        self.display_entry.grid(row=0, column=0, sticky="ew")

        name_hint = (
            "Внутреннее имя нельзя изменить после создания службы."
            if self.is_edit
            else "Латиница, цифры, точка, дефис и подчёркивание. Используется в командах и файлах."
        )
        holder = form.field("Внутреннее имя", hint=name_hint)
        self.name_entry = ttk.Entry(holder, textvariable=self.name_var)
        self.name_entry.grid(row=0, column=0, sticky="ew")
        if self.is_edit:
            self.name_entry.state(["readonly"])

        holder = form.field("Описание")
        self.description_text = widgets.make_text(holder, height=3)
        self.description_text.grid(row=0, column=0, sticky="ew")

        form.section("Что запускать", "Файл, аргументы и способ запуска.")
        holder = form.field("Файл программы")
        self.target_entry = ttk.Entry(holder, textvariable=self.target_var)
        self.target_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            holder, text="Выбрать…", style="Secondary.TButton", command=self._browse_target
        ).grid(row=0, column=1, padx=(8, 0))

        holder = form.field("Аргументы", hint="Передаются программе как есть.")
        self.args_entry = ttk.Entry(holder, textvariable=self.args_var, font=(theme.MONO_FAMILY, 10))
        self.args_entry.grid(row=0, column=0, sticky="ew")

        holder = form.field(
            "Рабочая папка", hint="Если пусто — используется папка выбранного файла."
        )
        self.workdir_entry = ttk.Entry(holder, textvariable=self.workdir_var)
        self.workdir_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(
            holder, text="Выбрать…", style="Secondary.TButton", command=self._browse_workdir
        ).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(
            holder, text="Сброс", style="Ghost.TButton", command=lambda: self.workdir_var.set("")
        ).grid(row=0, column=2, padx=(4, 0))

        holder = form.field("Способ запуска")
        self.launcher_combo = ttk.Combobox(
            holder,
            textvariable=self.launcher_var,
            values=list(LAUNCHER_LABELS.values()),
            state="readonly",
            width=42,
        )
        self.launcher_combo.grid(row=0, column=0, sticky="w")
        self.launcher_hint = tk.Label(
            holder,
            text="",
            bg=theme.CARD,
            fg=theme.SUBTLE,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
        )
        self.launcher_hint.grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

        holder = form.field(
            "Интерпретатор", hint="Используется только для «Свой интерпретатор»."
        )
        self.interpreter_entry = ttk.Entry(holder, textvariable=self.interpreter_var)
        self.interpreter_entry.grid(row=0, column=0, sticky="ew")
        self.interpreter_button = ttk.Button(
            holder, text="Выбрать…", style="Secondary.TButton", command=self._browse_interpreter
        )
        self.interpreter_button.grid(row=0, column=1, padx=(8, 0))

        holder = form.field("Команда запуска", hint="Итоговая командная строка, которую получит служба.")
        self.preview_text = widgets.make_text(holder, height=3)
        self.preview_text.configure(font=(theme.MONO_FAMILY, 9), bg="#f8fafc")
        self.preview_text.grid(row=0, column=0, sticky="ew")
        self.preview_text.configure(state="disabled")

        preview_actions = tk.Frame(holder, bg=theme.CARD)
        preview_actions.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.copy_button = ttk.Button(
            preview_actions, text="Копировать", style="Ghost.TButton", command=self._copy_command
        )
        self.copy_button.pack(side="left")
        self.test_button = ttk.Button(
            preview_actions,
            text="Проверить запуск",
            style="Ghost.TButton",
            command=self._test_command,
        )
        self.test_button.pack(side="left", padx=(4, 0))
        self.preview_hint = tk.Label(
            holder,
            text="",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
            wraplength=600,
        )
        self.preview_hint.grid(row=2, column=0, sticky="ew", pady=(4, 0))

        form.section("Переменные среды", "Дополнительные переменные только для этой службы.")
        holder = form.field(
            "Переменные",
            hint="По одной на строку: ИМЯ=значение. Строки с # или ; игнорируются.",
        )
        env_frame = tk.Frame(holder, bg=theme.CARD)
        env_frame.grid(row=0, column=0, sticky="ew")
        env_frame.grid_columnconfigure(0, weight=1)
        self.env_text = widgets.make_text(env_frame, height=5)
        self.env_text.grid(row=0, column=0, sticky="ew")
        env_scroll = ttk.Scrollbar(
            env_frame, orient="vertical", command=self.env_text.yview, style="Vertical.TScrollbar"
        )
        env_scroll.grid(row=0, column=1, sticky="ns")
        self.env_text.configure(yscrollcommand=env_scroll.set)

        scroll.bind_wheel_to_children()

    def _build_startup_tab(self) -> None:
        body, scroll = self._add_tab("Запуск")
        form = _Form(body)

        form.section("Тип запуска", "Когда Windows должна запускать службу.")
        holder = form.field("Тип запуска")
        self.start_combo = ttk.Combobox(
            holder,
            textvariable=self.start_var,
            values=list(START_TYPE_LABELS.values()),
            state="readonly",
            width=42,
        )
        self.start_combo.grid(row=0, column=0, sticky="w")
        self.start_hint = tk.Label(
            holder,
            text="",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
            wraplength=600,
        )
        self.start_hint.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(3, 0))

        form.section("Учётная запись", "От чьего имени работает служба и её дочерний процесс.")
        holder = form.field("Учётная запись")
        self.account_combo = ttk.Combobox(
            holder,
            textvariable=self.account_type_var,
            values=list(ACCOUNT_TYPE_LABELS.values()),
            state="readonly",
            width=42,
        )
        self.account_combo.grid(row=0, column=0, sticky="w")
        self.account_hint = tk.Label(
            holder,
            text="",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
            wraplength=600,
        )
        self.account_hint.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(3, 0))

        holder = form.field("Пользователь", hint="Например: .\\Имя или DOMAIN\\Имя.")
        self.account_entry = ttk.Entry(holder, textvariable=self.account_var)
        self.account_entry.grid(row=0, column=0, sticky="ew")

        holder = form.field("Пароль")
        self.password_entry = ttk.Entry(
            holder, textvariable=self.password_var, show="*", width=34
        )
        self.password_entry.grid(row=0, column=0, sticky="w")

        holder = form.field("Повтор пароля")
        self.password2_entry = ttk.Entry(
            holder, textvariable=self.password2_var, show="*", width=34
        )
        self.password2_entry.grid(row=0, column=0, sticky="w")

        holder = form.field(
            "Вход как служба",
            hint="Право SeServiceLogonRight. Обычно нужно выдавать пользовательской учётной записи.",
        )
        self.grant_check = widgets.checkbutton(
            holder,
            "Разрешить вход в качестве службы",
            self.grant_var,
        )
        self.grant_check.grid(row=0, column=0, sticky="w")
        self.check_right_button = ttk.Button(
            holder, text="Проверить право", style="Ghost.TButton", command=self._check_logon_right
        )
        self.check_right_button.grid(row=0, column=1, padx=(8, 0))
        _compact(holder)

        form.section("Зависимости и приоритет", "Необязательные дополнительные параметры.")
        holder = form.field(
            "Зависимости",
            hint="Имена служб через запятую, например: Tcpip, Dnscache, LanmanWorkstation.",
        )
        self.deps_entry = ttk.Entry(holder, textvariable=self.deps_var)
        self.deps_entry.grid(row=0, column=0, sticky="ew")

        holder = form.field("Приоритет процесса")
        self.priority_combo = ttk.Combobox(
            holder,
            textvariable=self.priority_var,
            values=list(PRIORITY_LABELS.values()),
            state="readonly",
            width=30,
        )
        self.priority_combo.grid(row=0, column=0, sticky="w")

        scroll.bind_wheel_to_children()

    def _build_recovery_tab(self) -> None:
        body, scroll = self._add_tab("Восстановление")
        form = _Form(body)

        form.section(
            "Автоперезапуск процесса",
            "Служба следит за программой и запускает её снова после завершения.",
        )
        holder = form.field("Перезапуск")
        self.auto_restart_check = widgets.checkbutton(
            holder,
            "Перезапускать программу автоматически",
            self.auto_restart_var,
        )
        self.auto_restart_check.grid(row=0, column=0, sticky="w")
        self.crash_only_check = widgets.checkbutton(
            holder,
            "Только при аварийном завершении (код выхода ≠ 0)",
            self.crash_only_var,
        )
        self.crash_only_check.grid(row=1, column=0, sticky="w", pady=(4, 0))

        holder = form.field("Задержка", hint="Пауза перед повторным запуском процесса.")
        self.restart_delay_spin = ttk.Spinbox(
            holder, from_=0, to=3600, width=8, textvariable=self.restart_delay_var
        )
        self.restart_delay_spin.grid(row=0, column=0, sticky="w")
        tk.Label(holder, text="сек.", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT_FAMILY, 9)).grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        _compact(holder)

        holder = form.field("Максимум попыток", hint="0 — без ограничений.")
        self.max_restarts_spin = ttk.Spinbox(
            holder, from_=0, to=9999, width=8, textvariable=self.max_restarts_var
        )
        self.max_restarts_spin.grid(row=0, column=0, sticky="w")
        tk.Label(holder, text="раз", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT_FAMILY, 9)).grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        _compact(holder)

        holder = form.field("Окно подсчёта")
        self.restart_window_spin = ttk.Spinbox(
            holder, from_=1, to=1440, width=8, textvariable=self.restart_window_var
        )
        self.restart_window_spin.grid(row=0, column=0, sticky="w")
        tk.Label(holder, text="мин.", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT_FAMILY, 9)).grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        _compact(holder)

        form.section(
            "Действия Windows при сбое службы",
            "Применяются службой SCM, если хост-процесс завершился с ошибкой.",
        )
        for index in range(3):
            holder = form.field(f"{index + 1}-й сбой")
            combo = ttk.Combobox(
                holder,
                textvariable=self.failure_action_vars[index],
                values=list(FAILURE_LABELS.values()),
                state="readonly",
                width=32,
            )
            combo.grid(row=0, column=0, sticky="w")
            spin = ttk.Spinbox(
                holder,
                from_=0,
                to=86400,
                width=8,
                textvariable=self.failure_delay_vars[index],
            )
            spin.grid(row=0, column=1, sticky="w", padx=(10, 4))
            tk.Label(
                holder, text="сек.", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT_FAMILY, 9)
            ).grid(row=0, column=2, sticky="w")
            _compact(holder)
            if index == 0:
                self.failure_action_combos = [combo]
                self.failure_delay_spins = [spin]
            else:
                self.failure_action_combos.append(combo)
                self.failure_delay_spins.append(spin)

        holder = form.field(
            "Команда при сбое",
            hint="Выполняется Windows, если хотя бы одно действие — «Выполнить команду».",
        )
        self.failure_command_entry = ttk.Entry(
            holder, textvariable=self.failure_command_var, font=(theme.MONO_FAMILY, 9)
        )
        self.failure_command_entry.grid(row=0, column=0, sticky="ew")

        holder = form.field("Сброс счётчика", hint="0 — не сбрасывать счётчик сбоев.")
        self.failure_reset_spin = ttk.Spinbox(
            holder, from_=0, to=365, width=8, textvariable=self.failure_reset_var
        )
        self.failure_reset_spin.grid(row=0, column=0, sticky="w")
        tk.Label(holder, text="дн.", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT_FAMILY, 9)).grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        _compact(holder)

        holder = form.field("Флаг сбоя")
        self.failure_flag_check = widgets.checkbutton(
            holder,
            "Применять действия и для служб без интерфейса",
            self.failure_flag_var,
        )
        self.failure_flag_check.grid(row=0, column=0, sticky="w")
        _compact(holder)

        self.reboot_note = form.note(
            "Внимание: действие «Перезапустить компьютер» действительно перезагрузит систему "
            "при сбое службы. Используйте его только осознанно."
        )
        self.reboot_note.grid_remove()

        form.section("Остановка", "Как служба завершает дочерний процесс.")
        holder = form.field(
            "Таймаут остановки",
            hint="Сколько ждать завершения после запроса остановки, прежде чем завершить принудительно.",
        )
        self.stop_timeout_spin = ttk.Spinbox(
            holder, from_=1, to=120, width=8, textvariable=self.stop_timeout_var
        )
        self.stop_timeout_spin.grid(row=0, column=0, sticky="w")
        tk.Label(holder, text="сек.", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT_FAMILY, 9)).grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        _compact(holder)

        holder = form.field("Дерево процессов")
        self.kill_tree_check = widgets.checkbutton(
            holder,
            "Завершать также дочерние процессы программы",
            self.kill_tree_var,
        )
        self.kill_tree_check.grid(row=0, column=0, sticky="w")

        scroll.bind_wheel_to_children()

    def _build_log_tab(self) -> None:
        body, scroll = self._add_tab("Журнал")
        form = _Form(body)

        form.section("Журнал службы", "Вывод программы и события запуска сохраняются в файл.")
        holder = form.field("Ведение журнала")
        self.log_enabled_check = widgets.checkbutton(
            holder,
            "Записывать журнал",
            self.log_enabled_var,
        )
        self.log_enabled_check.grid(row=0, column=0, sticky="w")

        holder = form.field("Файл журнала")
        self.log_path_entry = ttk.Entry(holder, textvariable=self.log_path_var)
        self.log_path_entry.grid(row=0, column=0, sticky="ew")
        self.log_browse_button = ttk.Button(
            holder, text="Выбрать…", style="Secondary.TButton", command=self._browse_log
        )
        self.log_browse_button.grid(row=0, column=1, padx=(8, 0))
        self.log_default_button = ttk.Button(
            holder, text="По умолчанию", style="Ghost.TButton", command=self._default_log_path
        )
        self.log_default_button.grid(row=0, column=2, padx=(4, 0))

        self.log_default_label = tk.Label(
            body,
            text="",
            bg=theme.CARD,
            fg=theme.SUBTLE,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
            wraplength=640,
        )
        self.log_default_label.grid(
            row=form.row, column=0, columnspan=2, sticky="ew", pady=(4, 0)
        )
        form.row += 1

        holder = form.field("Режим записи")
        self.log_append_radio = widgets.radiobutton(
            holder,
            "Дописывать в конец файла",
            self.log_append_var,
            1,
        )
        self.log_append_radio.grid(row=0, column=0, sticky="w")
        self.log_truncate_radio = widgets.radiobutton(
            holder,
            "Очищать при каждом запуске службы",
            self.log_append_var,
            0,
        )
        self.log_truncate_radio.grid(row=1, column=0, sticky="w", pady=(4, 0))
        _compact(holder)

        holder = form.field(
            "Максимальный размер",
            hint="При превышении файл переименовывается в .1. 0 — без ограничения.",
        )
        self.log_max_spin = ttk.Spinbox(
            holder, from_=0, to=1024, width=8, textvariable=self.log_max_var
        )
        self.log_max_spin.grid(row=0, column=0, sticky="w")
        tk.Label(holder, text="МБ", bg=theme.CARD, fg=theme.MUTED, font=(theme.FONT_FAMILY, 9)).grid(
            row=0, column=1, sticky="w", padx=(6, 0)
        )
        _compact(holder)

        holder = form.field("Служебный журнал", hint="Даже при отключённом журнале хост пишет диагностику.")
        ttk.Button(
            holder, text="Открыть папку журналов", style="Secondary.TButton", command=self._open_logs_dir
        ).grid(row=0, column=0, sticky="w")
        _compact(holder)

        form.note(
            "Журнал службы находится в папке данных программы. Файл host.log содержит "
            "диагностику хост-процесса и помогает понять причину, если служба не запускается.",
            kind="info",
        )

        scroll.bind_wheel_to_children()

    def _build_footer(self) -> None:
        footer = tk.Frame(
            self, bg=theme.CARD, highlightthickness=1, highlightbackground=theme.BORDER
        )
        footer.pack(fill="x", side="bottom")
        inner = tk.Frame(footer, bg=theme.CARD)
        inner.pack(fill="x", padx=18, pady=12)

        self.message_label = tk.Label(
            inner,
            text="Проверьте параметры и сохраните службу.",
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
            wraplength=780,
        )
        self.message_label.pack(fill="x", pady=(0, 8))

        bottom = tk.Frame(inner, bg=theme.CARD)
        bottom.pack(fill="x")
        left = tk.Frame(bottom, bg=theme.CARD)
        left.pack(side="left", fill="x", expand=True)

        start_text = (
            "Перезапустить службу после сохранения"
            if self.is_edit
            else "Запустить службу после создания"
        )
        self.start_after_check = widgets.checkbutton(
            left, start_text, self.start_after_var
        )
        self.start_after_check.pack(anchor="w")
        self.start_after_hint = tk.Label(
            left,
            text=(
                "Изменения вступят в силу после перезапуска службы."
                if self.is_edit
                else "Служба сразу начнёт работать в фоне."
            ),
            bg=theme.CARD,
            fg=theme.SUBTLE,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
        )
        self.start_after_hint.pack(anchor="w", padx=(24, 0))

        right = tk.Frame(bottom, bg=theme.CARD)
        right.pack(side="right", anchor="s")
        self.save_button = ttk.Button(
            right, text="Сохранить", style="Accent.TButton", command=self._save
        )
        self.save_button.pack(side="right")
        self.cancel_button = ttk.Button(
            right, text="Отмена", style="Secondary.TButton", command=self._on_cancel
        )
        self.cancel_button.pack(side="right", padx=(0, 8))
        self.progress = ttk.Progressbar(
            right, mode="indeterminate", length=90, style="Accent.Horizontal.TProgressbar"
        )

    # ------------------------------------------------------------------- state
    def _bind_state(self) -> None:
        for var in (
            self.target_var,
            self.args_var,
            self.workdir_var,
            self.launcher_var,
            self.interpreter_var,
        ):
            var.trace_add("write", lambda *_args: self._update_preview())
        self.launcher_var.trace_add("write", lambda *_args: self._sync_launcher())
        self.account_type_var.trace_add("write", lambda *_args: self._sync_account())
        self.auto_restart_var.trace_add("write", lambda *_args: self._sync_restart())
        self.log_enabled_var.trace_add("write", lambda *_args: self._sync_log())
        self.start_var.trace_add("write", lambda *_args: self._sync_start())
        self.name_var.trace_add("write", lambda *_args: self._update_log_hint())
        for var in self.failure_action_vars:
            var.trace_add("write", lambda *_args: self._sync_failure())

    def _load_config(self, config: ServiceConfig) -> None:
        self.name_var.set(config.name)
        self.display_var.set(config.display_name)
        widgets.set_text(self.description_text, config.description)
        self.target_var.set(config.target)
        self.args_var.set(config.arguments)
        self.workdir_var.set(config.working_dir)
        self.launcher_var.set(LAUNCHER_LABELS.get(config.launcher, LAUNCHER_LABELS[LAUNCHER_AUTO]))
        self.interpreter_var.set(config.interpreter)
        widgets.set_text(self.env_text, format_env_text(config.env))
        self.start_var.set(START_TYPE_LABELS.get(config.start_type, START_TYPE_LABELS["auto"]))
        self.account_type_var.set(
            ACCOUNT_TYPE_LABELS.get(config.account_type, ACCOUNT_TYPE_LABELS[ACCOUNT_SYSTEM])
        )
        self.account_var.set(config.account)
        self.grant_var.set(bool(config.grant_logon_right))
        self.deps_var.set(format_dependencies(config.dependencies))
        self.priority_var.set(PRIORITY_LABELS.get(config.priority, PRIORITY_LABELS["normal"]))
        self.auto_restart_var.set(bool(config.auto_restart))
        self.crash_only_var.set(bool(config.restart_on_crash_only))
        self.restart_delay_var.set(str(config.restart_delay))
        self.max_restarts_var.set(str(config.max_restarts))
        self.restart_window_var.set(str(config.restart_window_minutes))

        actions = list(config.failure_actions)
        while len(actions) < 3:
            actions.append(FailureAction(FAILURE_NONE, 0))
        for index, action in enumerate(actions[:3]):
            self.failure_action_vars[index].set(
                FAILURE_LABELS.get(action.action, FAILURE_LABELS[FAILURE_NONE])
            )
            self.failure_delay_vars[index].set(str(action.delay_seconds))
        self.failure_command_var.set(config.failure_command)
        self.failure_reset_var.set(str(config.failure_reset_days))
        self.failure_flag_var.set(bool(config.failure_flag))

        self.stop_timeout_var.set(str(config.stop_timeout))
        self.kill_tree_var.set(bool(config.kill_tree))
        self.log_enabled_var.set(bool(config.log_enabled))
        self.log_path_var.set(config.log_path)
        self.log_append_var.set(1 if config.log_append else 0)
        self.log_max_var.set(str(config.log_max_mb))

        self.password_var.set("")
        self.password2_var.set("")

    def _sync_all(self) -> None:
        self._sync_start()
        self._sync_launcher()
        self._sync_account()
        self._sync_restart()
        self._sync_failure()
        self._sync_log()
        self._update_log_hint()

    def _initial_focus(self) -> None:
        if not self._alive:
            return
        if not self.target_var.get().strip():
            target = self.target_entry
        elif not self.display_var.get().strip():
            target = self.display_entry
        else:
            target = self.name_entry
        try:
            target.focus_set()
            if isinstance(target, ttk.Entry) and not self.is_edit:
                target.select_range(0, "end")
        except tk.TclError:
            pass

    def _sync_start(self) -> None:
        key = START_TYPE_BY_LABEL.get(self.start_var.get(), "auto")
        self.start_hint.configure(text=START_HINTS.get(key, ""))

    def _sync_launcher(self) -> None:
        custom = self._launcher_key() == LAUNCHER_CUSTOM
        widgets.set_widget_state(self.interpreter_entry, custom)
        widgets.set_widget_state(self.interpreter_button, custom)
        cfg = self._collect(False)
        if cfg.target:
            detected = effective_launcher(cfg)
            self.launcher_hint.configure(
                text=f"Будет использовано: {LAUNCHER_LABELS.get(detected, detected)}"
            )
        else:
            self.launcher_hint.configure(text="Выберите файл, чтобы определить способ запуска.")

    def _sync_account(self) -> None:
        key = self._account_type_key()
        custom = key == ACCOUNT_CUSTOM
        for widget in (
            self.account_entry,
            self.password_entry,
            self.password2_entry,
            self.grant_check,
            self.check_right_button,
        ):
            widgets.set_widget_state(widget, custom)
        hint = ACCOUNT_HINTS.get(key, "")
        if custom and self.is_edit:
            try:
                unchanged = not self._account_changed(self._collect(False))
            except Exception:
                unchanged = False
            if unchanged:
                hint += "\nПароль можно не вводить — текущий пароль службы сохранится."
        self.account_hint.configure(text=hint)

    def _sync_restart(self) -> None:
        enabled = bool(self.auto_restart_var.get())
        for widget in (
            self.crash_only_check,
            self.restart_delay_spin,
            self.max_restarts_spin,
            self.restart_window_spin,
        ):
            widgets.set_widget_state(widget, enabled)

    def _sync_failure(self) -> None:
        command_needed = False
        reboot_needed = False
        for index, var in enumerate(self.failure_action_vars):
            action = FAILURE_BY_LABEL.get(var.get(), FAILURE_NONE)
            widgets.set_widget_state(self.failure_delay_spins[index], action != FAILURE_NONE)
            command_needed = command_needed or action == FAILURE_COMMAND
            reboot_needed = reboot_needed or action == FAILURE_REBOOT
        widgets.set_widget_state(self.failure_command_entry, command_needed)
        if reboot_needed:
            self.reboot_note.grid()
        else:
            self.reboot_note.grid_remove()

    def _sync_log(self) -> None:
        enabled = bool(self.log_enabled_var.get())
        for widget in (
            self.log_path_entry,
            self.log_browse_button,
            self.log_default_button,
            self.log_append_radio,
            self.log_truncate_radio,
            self.log_max_spin,
        ):
            widgets.set_widget_state(widget, enabled)

    def _update_log_hint(self) -> None:
        name = self.name_var.get().strip() or "Service"
        self.log_default_label.configure(
            text=f"Путь по умолчанию: {paths.default_log_path(name)}"
        )

    # ------------------------------------------------------------------ fields
    def _launcher_key(self) -> str:
        return LAUNCHER_BY_LABEL.get(self.launcher_var.get(), LAUNCHER_AUTO)

    def _account_type_key(self) -> str:
        return ACCOUNT_TYPE_BY_LABEL.get(self.account_type_var.get(), ACCOUNT_SYSTEM)

    def _read_int(self, var, default, minimum, maximum, label, tab, widget, strict):
        raw = var.get().strip().replace(" ", "")
        if not raw:
            if strict:
                raise ValidationError(f"Укажите значение: {label.lower()}.", tab, widget)
            return default
        try:
            value = int(raw)
        except ValueError:
            if strict:
                raise ValidationError(
                    f"«{label}»: нужно целое число.", tab, widget
                ) from None
            return default
        if value < minimum or value > maximum:
            if strict:
                raise ValidationError(
                    f"«{label}»: допустимое значение от {minimum} до {maximum}.",
                    tab,
                    widget,
                )
            return max(minimum, min(maximum, value))
        return value

    def _collect(self, strict: bool) -> ServiceConfig:
        cfg = ServiceConfig()
        cfg.name = self.name_var.get().strip()
        cfg.display_name = self.display_var.get().strip()
        cfg.description = widgets.text_value(self.description_text).strip()

        target = self.target_var.get().strip()
        if len(target) >= 2 and target[0] == target[-1] == '"':
            target = target[1:-1].strip()
        cfg.target = target

        workdir = self.workdir_var.get().strip()
        if len(workdir) >= 2 and workdir[0] == workdir[-1] == '"':
            workdir = workdir[1:-1].strip()
        cfg.working_dir = workdir

        cfg.arguments = self.args_var.get().strip()
        cfg.launcher = self._launcher_key()
        cfg.interpreter = self.interpreter_var.get().strip()

        env, env_error = parse_env_text(widgets.text_value(self.env_text))
        if strict and env_error:
            raise ValidationError(env_error, TAB_MAIN, self.env_text)
        cfg.env = env

        cfg.start_type = START_TYPE_BY_LABEL.get(self.start_var.get(), "auto")
        cfg.account_type = self._account_type_key()
        cfg.account = self.account_var.get().strip()
        cfg.grant_logon_right = bool(self.grant_var.get())
        cfg.dependencies = parse_dependencies(self.deps_var.get())
        cfg.priority = PRIORITY_BY_LABEL.get(self.priority_var.get(), "normal")

        cfg.auto_restart = bool(self.auto_restart_var.get())
        cfg.restart_on_crash_only = bool(self.crash_only_var.get())
        cfg.restart_delay = self._read_int(
            self.restart_delay_var, 5, 0, 3600, "Задержка", TAB_RECOVERY,
            self.restart_delay_spin, strict,
        )
        cfg.max_restarts = self._read_int(
            self.max_restarts_var, 0, 0, 9999, "Максимум попыток", TAB_RECOVERY,
            self.max_restarts_spin, strict,
        )
        cfg.restart_window_minutes = self._read_int(
            self.restart_window_var, 60, 1, 1440, "Окно подсчёта", TAB_RECOVERY,
            self.restart_window_spin, strict,
        )

        actions: list[FailureAction] = []
        for index in range(3):
            action = FAILURE_BY_LABEL.get(self.failure_action_vars[index].get(), FAILURE_NONE)
            delay = self._read_int(
                self.failure_delay_vars[index],
                0,
                0,
                86400,
                f"Задержка для {index + 1}-го сбоя",
                TAB_RECOVERY,
                self.failure_delay_spins[index],
                strict,
            )
            actions.append(FailureAction(action=action, delay_seconds=delay))
        cfg.failure_actions = actions
        cfg.failure_command = self.failure_command_var.get().strip()
        cfg.failure_reset_days = self._read_int(
            self.failure_reset_var, 1, 0, 365, "Сброс счётчика", TAB_RECOVERY,
            self.failure_reset_spin, strict,
        )
        cfg.failure_flag = bool(self.failure_flag_var.get())

        cfg.stop_timeout = self._read_int(
            self.stop_timeout_var, 10, 1, 120, "Таймаут остановки", TAB_RECOVERY,
            self.stop_timeout_spin, strict,
        )
        cfg.kill_tree = bool(self.kill_tree_var.get())

        cfg.log_enabled = bool(self.log_enabled_var.get())
        cfg.log_path = self.log_path_var.get().strip()
        cfg.log_append = bool(self.log_append_var.get())
        cfg.log_max_mb = self._read_int(
            self.log_max_var, 10, 0, 1024, "Максимальный размер журнала", TAB_LOG,
            self.log_max_spin, strict,
        )

        cfg.created_at = self.original.created_at
        cfg.updated_at = self.original.updated_at
        return cfg

    def _password(self, cfg: ServiceConfig) -> str | None:
        typed = self.password_var.get()
        if cfg.account_type == ACCOUNT_CUSTOM and typed:
            return typed
        return None

    def _account_changed(self, cfg: ServiceConfig) -> bool:
        if not self.is_edit:
            return True
        if cfg.account_type != self.original.account_type:
            return True
        if cfg.account_type == ACCOUNT_CUSTOM:
            old = winapi.normalize_account(self.original.account).casefold()
            new = winapi.normalize_account(cfg.account).casefold()
            return old != new
        return False

    def _validate(self, cfg: ServiceConfig) -> None:
        error = validate_service_name(cfg.name)
        if error:
            raise ValidationError(error, TAB_MAIN, self.name_entry)
        if len(cfg.display_name) > 250:
            raise ValidationError(
                "Отображаемое имя слишком длинное (максимум 250 символов).",
                TAB_MAIN,
                self.display_entry,
            )
        if not cfg.target:
            raise ValidationError(
                "Выберите файл, который должна запускать служба.",
                TAB_MAIN,
                self.target_entry,
            )
        target_path = Path(expand_path(cfg.target))
        if not target_path.is_file():
            raise ValidationError(
                f"Файл не найден:\n{expand_path(cfg.target)}\n\n"
                "Укажите существующий файл или исправьте путь.",
                TAB_MAIN,
                self.target_entry,
            )
        if cfg.working_dir:
            workdir = Path(expand_path(cfg.working_dir))
            if not workdir.is_dir():
                raise ValidationError(
                    f"Рабочая папка не найдена:\n{expand_path(cfg.working_dir)}",
                    TAB_MAIN,
                    self.workdir_entry,
                )
        spec = build_command(cfg)
        if not spec.ok:
            raise ValidationError(spec.error, TAB_MAIN, self.target_entry)

        if not self.is_edit:
            if config_store.config_exists(cfg.name):
                raise ValidationError(
                    f"Настройки службы «{cfg.name}» уже существуют. "
                    "Выберите другое внутреннее имя.",
                    TAB_MAIN,
                    self.name_entry,
                )
            try:
                registered = scm.service_exists(cfg.name)
            except Exception:
                registered = False
            if registered:
                raise ValidationError(
                    f"Служба «{cfg.name}» уже зарегистрирована в Windows. "
                    "Выберите другое внутреннее имя.",
                    TAB_MAIN,
                    self.name_entry,
                )

        if cfg.account_type == ACCOUNT_CUSTOM:
            if not cfg.account:
                raise ValidationError(
                    "Укажите учётную запись пользователя для службы.",
                    TAB_STARTUP,
                    self.account_entry,
                )
            password = self.password_var.get()
            confirm = self.password2_var.get()
            if password != confirm:
                raise ValidationError(
                    "Пароль и его повтор не совпадают.",
                    TAB_STARTUP,
                    self.password2_entry,
                )
            if not password and (not self.is_edit or self._account_changed(cfg)):
                raise ValidationError(
                    "Для пользовательской учётной записи укажите пароль.",
                    TAB_STARTUP,
                    self.password_entry,
                )

        for dependency in cfg.dependencies:
            if any(char in dependency for char in '/\\"'):
                raise ValidationError(
                    f"Недопустимое имя зависимости: {dependency}",
                    TAB_STARTUP,
                    self.deps_entry,
                )

        if cfg.log_enabled:
            if cfg.log_path:
                if cfg.log_path.endswith(("\\", "/")):
                    raise ValidationError(
                        "Путь журнала должен указывать на файл, а не на папку.",
                        TAB_LOG,
                        self.log_path_entry,
                    )
                probe = Path(expand_path(cfg.log_path))
                if probe.is_dir():
                    raise ValidationError(
                        "Путь журнала указывает на папку. Укажите имя файла.",
                        TAB_LOG,
                        self.log_path_entry,
                    )
                if re.search(r'[<>:"|?*]', probe.name):
                    raise ValidationError(
                        "Имя файла журнала содержит недопустимые символы.",
                        TAB_LOG,
                        self.log_path_entry,
                    )

        if any(action.action == FAILURE_COMMAND for action in cfg.failure_actions):
            if not cfg.failure_command:
                raise ValidationError(
                    "Укажите команду, которая должна выполняться при сбое.",
                    TAB_RECOVERY,
                    self.failure_command_entry,
                )

    # ----------------------------------------------------------------- actions
    def _show_validation(self, error: ValidationError) -> None:
        self.notebook.select(error.tab)
        self._set_message(error.message, "error")
        if error.widget is not None:
            try:
                error.widget.focus_set()
            except tk.TclError:
                pass
        self.bell()

    def _set_message(self, text: str, kind: str = "info") -> None:
        color = {
            "info": theme.MUTED,
            "success": theme.SUCCESS,
            "error": theme.DANGER,
            "warn": theme.WARN,
        }.get(kind, theme.MUTED)
        self.message_label.configure(text=text, fg=color)

    def _set_saving(self, saving: bool, text: str = "") -> None:
        self._saving = saving
        if saving:
            self._set_message(text or "Сохранение настроек…", "info")
            self.progress.pack(side="right", padx=(0, 12))
            self.progress.start(12)
            self.save_button.state(["disabled"])
            self.cancel_button.state(["disabled"])
            self.configure(cursor="watch")
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.save_button.state(["!disabled"])
            self.cancel_button.state(["!disabled"])
            self.configure(cursor="")

    def _save(self) -> None:
        if self._saving or not self._alive:
            return
        try:
            cfg = self._collect(True)
            self._validate(cfg)
        except ValidationError as error:
            self._show_validation(error)
            return

        if any(action.action == FAILURE_REBOOT for action in cfg.failure_actions):
            confirmed = messagebox.askyesno(
                "Перезагрузка компьютера",
                "Среди действий при сбое выбрана перезагрузка компьютера.\n\n"
                "Windows действительно перезагрузит систему, если служба завершится с ошибкой. "
                "Продолжить сохранение?",
                parent=self,
                icon="warning",
                default="no",
            )
            if not confirmed:
                return

        if not winapi.is_admin():
            messagebox.showerror(
                "Требуются права администратора",
                "Создание и изменение служб доступно только от имени администратора.\n\n"
                "Закройте редактор и перезапустите программу с повышенными правами.",
                parent=self,
            )
            return

        password = self._password(cfg)
        account_changed = self._account_changed(cfg)
        start_after = bool(self.start_after_var.get())
        self._set_saving(True)

        def worker() -> None:
            warnings: list[str] = []
            error: BaseException | None = None
            try:
                if self.is_edit:
                    warnings = scm.update_service(cfg, password, account_changed=account_changed)
                else:
                    warnings = scm.create_service(cfg, password)
            except BaseException as exc:  # noqa: BLE001 - surfaced in the dialog
                log.exception("Не удалось сохранить настройки службы %s", cfg.name)
                error = exc
            self._results.put(("save", cfg, start_after, warnings, error))

        threading.Thread(target=worker, name="service-save", daemon=True).start()

    def _drain_results(self) -> None:
        if not self._alive:
            return
        try:
            while True:
                item = self._results.get_nowait()
                kind = item[0]
                if kind == "save":
                    self._finish_save(*item[1:])
                elif kind == "test":
                    self._test_done(*item[1:])
                if not self._alive:
                    return
        except queue.Empty:
            pass
        self.after(100, self._drain_results)

    def _finish_save(
        self,
        cfg: ServiceConfig,
        start_after: bool,
        warnings: list[str],
        error: BaseException | None,
    ) -> None:
        if not self._alive:
            return
        self._set_saving(False)
        if error is not None:
            self._set_message(winapi.format_error(error, "Не удалось сохранить службу"), "error")
            messagebox.showerror(
                "Не удалось сохранить службу",
                winapi.format_error(error),
                parent=self,
            )
            return

        self.saved_config = cfg
        self.start_after = start_after
        if warnings:
            messagebox.showwarning(
                "Служба сохранена с предупреждениями",
                "Служба сохранена, но некоторые дополнительные параметры применить не удалось:\n\n"
                + "\n\n".join(f"• {item}" for item in warnings),
                parent=self,
            )
        self._close_dialog()

    def _on_cancel(self) -> None:
        if self._saving or not self._alive:
            return
        if self._is_dirty():
            if not messagebox.askyesno(
                "Закрыть без сохранения?",
                "Изменения не сохранены. Закрыть редактор и потерять их?",
                parent=self,
            ):
                return
        self._close_dialog()

    def _is_dirty(self) -> bool:
        if self.password_var.get() or self.password2_var.get():
            return True
        try:
            current = self._collect(False)
        except Exception:
            return True
        return current.to_dict() != self.original.to_dict()

    def _close_dialog(self) -> None:
        self._alive = False
        try:
            self.grab_release()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    # ----------------------------------------------------------------- browsers
    def _browse_target(self) -> None:
        current = self.target_var.get().strip()
        initial = None
        if current:
            parent = Path(expand_path(current)).parent
            if parent.is_dir():
                initial = str(parent)
        path = filedialog.askopenfilename(
            parent=self,
            title="Файл для службы",
            initialdir=initial,
            filetypes=FILE_TYPES,
        )
        if not path:
            return
        self.target_var.set(path)
        if not self.is_edit and not self.name_var.get().strip():
            self.name_var.set(_suggest_name(path))
        self._update_preview()

    def _browse_workdir(self) -> None:
        current = self.workdir_var.get().strip()
        initial = expand_path(current) if current else expand_path(self.target_var.get().strip())
        if initial and not Path(initial).is_dir():
            initial = str(Path(initial).parent) if Path(initial).parent.is_dir() else ""
        path = filedialog.askdirectory(
            parent=self,
            title="Рабочая папка",
            initialdir=initial or None,
            mustexist=True,
        )
        if path:
            self.workdir_var.set(path)

    def _browse_interpreter(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Интерпретатор",
            filetypes=[("Программы", "*.exe"), ("Все файлы", "*.*")],
        )
        if path:
            self.interpreter_var.set(path)

    def _browse_log(self) -> None:
        name = self.name_var.get().strip() or "Service"
        current = self.log_path_var.get().strip()
        initial = expand_path(current) if current else paths.default_log_path(name)
        path = filedialog.asksaveasfilename(
            parent=self,
            title="Файл журнала службы",
            defaultextension=".log",
            initialfile=Path(initial).name,
            initialdir=str(Path(initial).parent),
            filetypes=[("Журнал", "*.log"), ("Все файлы", "*.*")],
        )
        if path:
            self.log_path_var.set(path)

    def _default_log_path(self) -> None:
        self.log_path_var.set(paths.default_log_path(self.name_var.get().strip() or "Service"))

    def _open_logs_dir(self) -> None:
        folder = paths.log_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            winapi.open_folder(folder)
        except OSError as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self)

    # ------------------------------------------------------------------ preview
    def _update_preview(self) -> None:
        if not hasattr(self, "preview_text"):
            return
        try:
            cfg = self._collect(False)
            spec = build_command(cfg) if cfg.target else None
        except Exception:
            return
        if spec is None:
            command = ""
            hint = "Выберите файл, чтобы увидеть команду запуска."
            color = theme.SUBTLE
        elif spec.error:
            command = ""
            hint = spec.error
            color = theme.DANGER
        elif spec.warning:
            command = spec.command
            hint = spec.warning
            color = theme.WARN
        else:
            command = spec.command
            hint = "Команда сформирована."
            color = theme.MUTED
        _set_text_value(self.preview_text, command or "—")
        self.preview_hint.configure(text=hint, fg=color)

    def _copy_command(self) -> None:
        command = self.preview_text.get("1.0", "end-1c").strip()
        if not command or command == "—":
            self._set_message("Команда пока не сформирована.", "warn")
            return
        self.clipboard_clear()
        self.clipboard_append(command)
        self._set_message("Команда скопирована в буфер обмена.", "success")

    def _test_command(self) -> None:
        try:
            cfg = self._collect(False)
        except Exception:
            return
        if not cfg.target:
            self._set_message("Сначала выберите файл для запуска.", "error")
            return
        spec = build_command(cfg)
        if not spec.ok:
            self._set_message(spec.error, "error")
            return
        if not messagebox.askyesno(
            "Проверить запуск",
            "Программа будет запущена на несколько секунд и затем остановлена.\n\n"
            f"{spec.command}\n\nПродолжить?",
            parent=self,
        ):
            return

        self.test_button.state(["disabled"])
        self._set_message("Проверка запуска… Программа будет остановлена через 5 секунд.", "info")

        def worker() -> None:
            try:
                ok, output = test_command(cfg, timeout=5)
            except Exception as exc:  # noqa: BLE001 - diagnostic helper
                ok, output = False, winapi.format_error(exc)
            self._results.put(("test", ok, output))

        threading.Thread(target=worker, name="service-test-run", daemon=True).start()

    def _test_done(self, ok: bool, output: str) -> None:
        if not self._alive:
            return
        self.test_button.state(["!disabled"])
        if ok:
            self._set_message("Проверка запуска завершена.", "success")
            title = "Проверка запуска"
        else:
            self._set_message(output, "error")
            title = "Не удалось запустить"
        text = output.strip() or "Программа запустилась без вывода."
        if len(text) > 1600:
            text = text[:1590].rstrip() + "\n…"
        if ok:
            messagebox.showinfo(title, text, parent=self)
        else:
            messagebox.showerror(title, text, parent=self)

    def _check_logon_right(self) -> None:
        account = self.account_var.get().strip()
        if not account:
            messagebox.showinfo(
                "Учётная запись не указана",
                "Сначала введите имя пользователя.",
                parent=self,
            )
            return
        try:
            has_right = winapi.account_has_logon_right(account)
        except Exception as exc:
            messagebox.showerror(
                "Не удалось проверить право",
                winapi.format_error(exc),
                parent=self,
            )
            return
        if has_right:
            messagebox.showinfo(
                "Право уже есть",
                f"Учётная запись {account} может входить в систему как служба.",
                parent=self,
            )
            return
        if not messagebox.askyesno(
            "Право не предоставлено",
            f"Учётная запись {account} не имеет права «Вход в качестве службы».\n\n"
            "Предоставить это право сейчас?",
            parent=self,
        ):
            return
        try:
            winapi.grant_logon_as_service(account)
        except Exception as exc:
            messagebox.showerror(
                "Не удалось предоставить право",
                winapi.format_error(exc),
                parent=self,
            )
            return
        messagebox.showinfo(
            "Право предоставлено",
            f"Учётная запись {account} теперь может входить как служба.",
            parent=self,
        )
