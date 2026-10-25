"""Visual theme for the Tkinter interface."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

from PIL import Image, ImageDraw, ImageTk

from ..icon import create_icon_image

BG = "#eef2f8"
CARD = "#ffffff"
TEXT = "#0f172a"
MUTED = "#64748b"
SUBTLE = "#94a3b8"
BORDER = "#dfe6f1"
BORDER_STRONG = "#cbd5e1"
ACCENT = "#2563eb"
ACCENT_DARK = "#1d4ed8"
ACCENT_SOFT = "#eff6ff"
SUCCESS = "#15803d"
SUCCESS_BG = "#dcfce7"
DANGER = "#b91c1c"
DANGER_BG = "#fee2e2"
WARN = "#b45309"
WARN_BG = "#fef3c7"

STATUS_COLORS = {
    "running": (SUCCESS, SUCCESS_BG),
    "stopped": ("#64748b", "#f1f5f9"),
    "pending": (WARN, WARN_BG),
    "paused": ("#0369a1", "#e0f2fe"),
    "error": (DANGER, DANGER_BG),
    "unregistered": ("#9a3412", "#ffedd5"),
}

FONT_FAMILY = "Segoe UI"
MONO_FAMILY = "Consolas"


def _pick_font(root: tk.Misc) -> str:
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return "Arial"
    return FONT_FAMILY if FONT_FAMILY in families else "Arial"


def apply_theme(root: tk.Tk) -> None:
    family = _pick_font(root)

    root.option_add("*Font", (family, 10))
    root.option_add("*TCombobox*Listbox.background", CARD)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.font", (family, 10))
    root.option_add("*Menu.background", CARD)
    root.option_add("*Menu.foreground", TEXT)
    root.option_add("*Menu.activeBackground", ACCENT_SOFT)
    root.option_add("*Menu.activeForeground", ACCENT_DARK)

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=BG, foreground=TEXT, font=(family, 10))
    style.configure("TFrame", background=BG)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Header.TFrame", background=CARD)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Card.TLabel", background=CARD, foreground=TEXT)
    style.configure("Muted.TLabel", background=CARD, foreground=MUTED)
    style.configure("BgMuted.TLabel", background=BG, foreground=MUTED)
    style.configure("Title.TLabel", background=CARD, foreground=TEXT, font=(family, 18, "bold"))
    style.configure("Subtitle.TLabel", background=CARD, foreground=MUTED, font=(family, 9))
    style.configure("Section.TLabel", background=CARD, foreground=TEXT, font=(family, 10, "bold"))
    style.configure("Hint.TLabel", background=CARD, foreground=MUTED, font=(family, 9))
    style.configure("Error.TLabel", background=CARD, foreground=DANGER, font=(family, 9))
    style.configure("Value.TLabel", background=CARD, foreground=TEXT, font=(family, 10))
    style.configure("Mono.TLabel", background=CARD, foreground=MUTED, font=(MONO_FAMILY, 9))

    style.configure(
        "Accent.TButton",
        background=ACCENT,
        foreground="#ffffff",
        borderwidth=0,
        focuscolor=ACCENT,
        focusthickness=0,
        padding=(16, 8),
        font=(family, 10, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("active", ACCENT_DARK), ("pressed", ACCENT_DARK), ("disabled", "#a9bce9")],
        foreground=[("disabled", "#f4f7ff")],
    )
    style.configure(
        "Secondary.TButton",
        background=CARD,
        foreground=TEXT,
        bordercolor=BORDER_STRONG,
        lightcolor=BORDER_STRONG,
        darkcolor=BORDER_STRONG,
        borderwidth=1,
        focusthickness=0,
        padding=(12, 7),
    )
    style.map(
        "Secondary.TButton",
        background=[("active", "#f4f7fc"), ("pressed", "#eaf0f9"), ("disabled", "#f8fafc")],
        foreground=[("disabled", SUBTLE)],
        bordercolor=[("disabled", BORDER)],
    )
    style.configure(
        "Ghost.TButton",
        background=CARD,
        foreground=ACCENT_DARK,
        borderwidth=0,
        focusthickness=0,
        padding=(8, 4),
        font=(family, 9),
    )
    style.map(
        "Ghost.TButton",
        background=[("active", ACCENT_SOFT), ("pressed", "#dbeafe")],
        foreground=[("disabled", SUBTLE)],
    )
    style.configure(
        "Success.TButton",
        background="#16a34a",
        foreground="#ffffff",
        borderwidth=0,
        focusthickness=0,
        padding=(14, 7),
        font=(family, 10, "bold"),
    )
    style.map(
        "Success.TButton",
        background=[("active", "#15803d"), ("pressed", "#166534"), ("disabled", "#a7d7b5")],
        foreground=[("disabled", "#f4fff7")],
    )
    style.configure(
        "Danger.TButton",
        background=CARD,
        foreground=DANGER,
        bordercolor="#fecaca",
        lightcolor="#fecaca",
        darkcolor="#fecaca",
        borderwidth=1,
        focusthickness=0,
        padding=(12, 7),
    )
    style.map(
        "Danger.TButton",
        background=[("active", "#fef2f2"), ("pressed", "#fee2e2")],
        foreground=[("disabled", SUBTLE)],
        bordercolor=[("disabled", BORDER)],
    )
    style.configure(
        "Tool.TButton",
        background=CARD,
        foreground=MUTED,
        borderwidth=0,
        focusthickness=0,
        padding=(9, 6),
        font=(family, 11),
    )
    style.map(
        "Tool.TButton",
        background=[("active", "#eef4ff")],
        foreground=[("active", ACCENT_DARK)],
    )

    style.configure(
        "TEntry",
        fieldbackground="#ffffff",
        foreground=TEXT,
        bordercolor=BORDER_STRONG,
        lightcolor=BORDER_STRONG,
        darkcolor=BORDER_STRONG,
        insertcolor=TEXT,
        borderwidth=1,
        padding=6,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", ACCENT)],
        lightcolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
        fieldbackground=[("disabled", "#eef2f7")],
        foreground=[("disabled", SUBTLE)],
    )
    style.configure(
        "TCombobox",
        fieldbackground="#ffffff",
        background=CARD,
        foreground=TEXT,
        arrowcolor=MUTED,
        bordercolor=BORDER_STRONG,
        lightcolor=BORDER_STRONG,
        darkcolor=BORDER_STRONG,
        borderwidth=1,
        padding=6,
    )
    style.map(
        "TCombobox",
        bordercolor=[("focus", ACCENT)],
        lightcolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
        fieldbackground=[("readonly", "#ffffff"), ("disabled", "#eef2f7")],
        foreground=[("disabled", SUBTLE)],
        arrowcolor=[("disabled", SUBTLE)],
    )
    style.configure(
        "TSpinbox",
        fieldbackground="#ffffff",
        foreground=TEXT,
        bordercolor=BORDER_STRONG,
        lightcolor=BORDER_STRONG,
        darkcolor=BORDER_STRONG,
        arrowcolor=MUTED,
        borderwidth=1,
        padding=5,
    )
    style.map(
        "TSpinbox",
        bordercolor=[("focus", ACCENT)],
        lightcolor=[("focus", ACCENT)],
        darkcolor=[("focus", ACCENT)],
        fieldbackground=[("disabled", "#eef2f7")],
    )

    style.configure(
        "Services.Treeview",
        background=CARD,
        fieldbackground=CARD,
        foreground=TEXT,
        rowheight=34,
        borderwidth=0,
        relief="flat",
        font=(family, 10),
    )
    style.configure(
        "Services.Treeview.Heading",
        background="#f5f8fd",
        foreground=MUTED,
        relief="flat",
        borderwidth=0,
        padding=(8, 8),
        font=(family, 9, "bold"),
    )
    style.map(
        "Services.Treeview",
        background=[("selected", "#e8f0ff")],
        foreground=[("selected", TEXT)],
    )
    style.map(
        "Services.Treeview.Heading",
        background=[("active", "#edf3fc")],
        foreground=[("active", TEXT)],
    )

    style.configure("Vertical.TScrollbar", width=12, arrowsize=12,
                    background="#c7d2e0", troughcolor="#f4f7fc",
                    bordercolor="#f4f7fc", lightcolor="#c7d2e0",
                    darkcolor="#c7d2e0", gripcount=0)
    style.map("Vertical.TScrollbar", background=[("active", "#aab8cc")])
    style.configure("Horizontal.TScrollbar", height=12, arrowsize=12,
                    background="#c7d2e0", troughcolor="#f4f7fc",
                    bordercolor="#f4f7fc", lightcolor="#c7d2e0",
                    darkcolor="#c7d2e0", gripcount=0)

    style.configure("TNotebook", background=CARD, borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "TNotebook.Tab",
        background="#f3f6fc",
        foreground=MUTED,
        padding=(16, 9),
        font=(family, 10),
        borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", CARD), ("active", "#eaf1fb")],
        foreground=[("selected", ACCENT_DARK), ("active", TEXT)],
        font=[("selected", (family, 10, "bold"))],
    )
    style.configure("TSeparator", background=BORDER)
    style.configure(
        "Accent.Horizontal.TProgressbar",
        background=ACCENT,
        troughcolor="#e2e8f0",
        borderwidth=0,
    )


def status_image(root: tk.Misc, kind: str, size: int = 12) -> ImageTk.PhotoImage:
    """Small anti-aliased status dot for the service list."""
    color, _bg = STATUS_COLORS.get(kind, STATUS_COLORS["stopped"])
    scale = 4
    canvas = Image.new("RGBA", (size * scale, size * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)
    margin = scale
    draw.ellipse(
        (margin, margin, size * scale - margin, size * scale - margin),
        fill=color,
    )
    # A subtle lighter core makes the dot feel less flat.
    light = tuple(min(255, int(channel * 1.25) + 20) for channel in _hex_to_rgb(color))
    inner = size * scale * 0.28
    draw.ellipse(
        (inner, inner, size * scale - inner, size * scale - inner),
        fill=light,
    )
    canvas = canvas.resize((size, size), Image.Resampling.LANCZOS)
    return ImageTk.PhotoImage(canvas, master=root)


def icon_photo(root: tk.Misc, size: int = 32) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(create_icon_image(size), master=root)


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))


def ellipsize(text: str, limit: int = 70) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
