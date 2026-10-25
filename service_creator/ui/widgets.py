"""Reusable Tkinter widgets and layout helpers."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from . import theme


class ScrollFrame(tk.Frame):
    """A vertically scrollable frame with a slim scrollbar."""

    def __init__(self, parent, background: str = theme.CARD, **kwargs):
        super().__init__(parent, bg=background, **kwargs)
        self.background = background
        self.scrollbar = ttk.Scrollbar(
            self, orient="vertical", style="Vertical.TScrollbar"
        )
        self.canvas = tk.Canvas(
            self,
            bg=background,
            highlightthickness=0,
            bd=0,
            takefocus=0,
            yscrollcommand=self.scrollbar.set,
        )
        self.scrollbar.configure(command=self.canvas.yview)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.inner = tk.Frame(self.canvas, bg=background)
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.inner.bind("<MouseWheel>", self._on_mousewheel)
        self.scrollbar.bind("<MouseWheel>", self._on_mousewheel)

    def _on_inner_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _on_mousewheel(self, event) -> None:
        bbox = self.canvas.bbox("all")
        if bbox is None:
            return
        _x1, y1, _x2, y2 = bbox
        if (y2 - y1) <= self.canvas.winfo_height():
            return
        delta = -int(event.delta / 120)
        self.canvas.yview_scroll(delta, "units")

    def bind_wheel_to_children(self) -> None:
        """Call after populating ``inner`` so the wheel works above form fields."""
        skip = (tk.Text, tk.Listbox, ttk.Treeview, ttk.Combobox)

        def visit(widget: tk.Misc) -> None:
            if not isinstance(widget, skip):
                widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
            for child in widget.winfo_children():
                visit(child)

        visit(self.inner)

    def scroll_to_top(self) -> None:
        self.canvas.yview_moveto(0)


class SearchBox(tk.Frame):
    """Entry with an overlay placeholder and a clear button."""

    def __init__(self, parent, placeholder: str, variable: tk.StringVar, width: int = 32):
        super().__init__(
            parent,
            bg=theme.CARD,
            highlightthickness=1,
            highlightbackground=theme.BORDER_STRONG,
            highlightcolor=theme.ACCENT,
        )
        self.variable = variable
        self.placeholder = placeholder

        self.entry = tk.Entry(
            self,
            textvariable=variable,
            bd=0,
            relief="flat",
            bg=theme.CARD,
            fg=theme.TEXT,
            insertbackground=theme.TEXT,
            font=(theme.FONT_FAMILY, 10),
            width=width,
        )
        self.entry.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=7)
        self.clear = tk.Label(
            self,
            text="×",
            bg=theme.CARD,
            fg=theme.SUBTLE,
            font=(theme.FONT_FAMILY, 12),
            cursor="hand2",
            padx=6,
        )
        self.clear.pack(side="right", padx=(0, 2))
        self.clear.bind("<Button-1>", self._clear)

        self.placeholder_label = tk.Label(
            self,
            text=placeholder,
            bg=theme.CARD,
            fg=theme.SUBTLE,
            font=(theme.FONT_FAMILY, 10),
            cursor="xterm",
        )
        self.placeholder_label.place(x=11, rely=0.5, anchor="w")
        self.placeholder_label.bind("<Button-1>", self._focus_entry)

        self.entry.bind("<FocusIn>", self._focus_in)
        self.entry.bind("<FocusOut>", self._focus_out)
        self.variable.trace_add("write", self._on_var_write)
        self._update_placeholder()

    def _focus_entry(self, _event=None) -> None:
        self.entry.focus_set()

    def _focus_in(self, _event=None) -> None:
        self.configure(highlightbackground=theme.ACCENT)
        self.placeholder_label.place_forget()

    def _focus_out(self, _event=None) -> None:
        self.configure(highlightbackground=theme.BORDER_STRONG)
        self._update_placeholder()

    def _on_var_write(self, *_args) -> None:
        self._update_placeholder()
        self.clear.configure(fg=theme.MUTED if self.variable.get() else theme.SUBTLE)

    def _update_placeholder(self) -> None:
        if self.variable.get() or self.entry.focus_get() == self.entry:
            self.placeholder_label.place_forget()
        else:
            self.placeholder_label.place(x=11, rely=0.5, anchor="w")

    def _clear(self, _event=None) -> None:
        self.variable.set("")
        self.entry.focus_set()

    def get(self) -> str:
        return self.variable.get().strip()


class Tooltip:
    def __init__(self, widget: tk.Widget, text: str, delay: int = 450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _show(self) -> None:
        if self._window is not None:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        window = tk.Toplevel(self.widget)
        window.wm_overrideredirect(True)
        window.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            window,
            text=self.text,
            bg="#111827",
            fg="#f8fafc",
            font=(theme.FONT_FAMILY, 9),
            padx=9,
            pady=5,
        )
        label.pack()
        self._window = window

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._window is not None:
            self._window.destroy()
            self._window = None

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None


def center_window(window: tk.Toplevel | tk.Tk, parent: tk.Misc | None, width: int, height: int) -> None:
    window.update_idletasks()
    screen_w = window.winfo_screenwidth()
    screen_h = window.winfo_screenheight()
    if parent is not None and parent.winfo_viewable():
        px, py = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = parent.winfo_width(), parent.winfo_height()
    else:
        px = py = 0
        pw, ph = screen_w, screen_h
    x = px + (pw - width) // 2
    y = py + (ph - height) // 2
    x = max(0, min(x, screen_w - width))
    y = max(0, min(y, screen_h - height - 40))
    window.geometry(f"{width}x{height}+{x}+{y}")


def make_text(parent, height: int = 3, **kwargs) -> tk.Text:
    return tk.Text(
        parent,
        height=height,
        wrap="word",
        bd=0,
        relief="flat",
        bg="#ffffff",
        fg=theme.TEXT,
        insertbackground=theme.TEXT,
        highlightthickness=1,
        highlightbackground=theme.BORDER_STRONG,
        highlightcolor=theme.ACCENT,
        padx=8,
        pady=7,
        font=(theme.FONT_FAMILY, 10),
        undo=True,
        **kwargs,
    )


def text_value(text: tk.Text) -> str:
    return text.get("1.0", "end-1c")


def set_text(text: tk.Text, value: str) -> None:
    text.delete("1.0", "end")
    text.insert("1.0", value)


def set_widget_state(widget, enabled: bool, readonly: bool = False) -> None:
    if isinstance(widget, ttk.Widget):
        try:
            widget.state(["!disabled", "!readonly"])
            if not enabled:
                widget.state(["disabled"])
            elif readonly:
                widget.state(["readonly"])
            return
        except tk.TclError:
            pass
    try:
        widget.configure(state="normal" if enabled else "disabled")
    except tk.TclError:
        pass


def checkbutton(parent, text: str, variable, **kwargs) -> tk.Checkbutton:
    """Card-friendly check button with the native check mark.

    The ttk ``clam`` theme draws a cross in its indicator, which is easy to
    mistake for an error. The classic Tk widget uses the familiar check mark
    and still supports the same variable/state API.
    """
    options = {
        "bg": theme.CARD,
        "fg": theme.TEXT,
        "activebackground": theme.CARD,
        "activeforeground": theme.TEXT,
        "disabledforeground": theme.SUBTLE,
        "selectcolor": "#ffffff",
        "font": (theme.FONT_FAMILY, 10),
        "bd": 0,
        "relief": "flat",
        "highlightthickness": 0,
        "anchor": "w",
        "justify": "left",
        "padx": 0,
        "pady": 2,
    }
    options.update(kwargs)
    return tk.Checkbutton(parent, text=text, variable=variable, **options)


def radiobutton(parent, text: str, variable, value, **kwargs) -> tk.Radiobutton:
    """Card-friendly radio button with the native indicator."""
    options = {
        "bg": theme.CARD,
        "fg": theme.TEXT,
        "activebackground": theme.CARD,
        "activeforeground": theme.TEXT,
        "disabledforeground": theme.SUBTLE,
        "selectcolor": "#ffffff",
        "font": (theme.FONT_FAMILY, 10),
        "bd": 0,
        "relief": "flat",
        "highlightthickness": 0,
        "anchor": "w",
        "justify": "left",
        "padx": 0,
        "pady": 2,
    }
    options.update(kwargs)
    return tk.Radiobutton(parent, text=text, variable=variable, value=value, **options)


def separator(parent, background: str = theme.BORDER) -> tk.Frame:
    return tk.Frame(parent, bg=background, height=1)


def section_title(parent, text: str, subtitle: str | None = None) -> tk.Frame:
    frame = tk.Frame(parent, bg=theme.CARD)
    label = tk.Label(
        frame,
        text=text,
        bg=theme.CARD,
        fg=theme.TEXT,
        font=(theme.FONT_FAMILY, 10, "bold"),
        anchor="w",
    )
    label.pack(fill="x")
    if subtitle:
        hint = tk.Label(
            frame,
            text=subtitle,
            bg=theme.CARD,
            fg=theme.MUTED,
            font=(theme.FONT_FAMILY, 9),
            anchor="w",
            justify="left",
        )
        hint.pack(fill="x", pady=(2, 0))
    return frame
