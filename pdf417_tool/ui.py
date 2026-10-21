"""Single-window PDF417 upload, edit, generate, and save."""

from __future__ import annotations

import math
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

from PIL import Image, ImageEnhance, ImageGrab, ImageTk

from .decode import Pdf417Symbol, common_code_text, decode_image, read_upload
from .encode import GenerateOptions, generate_fitted, legal_symbol_size, with_transparent_background
from .icon import app_icon
from . import startup

PREVIEW_WIDTH = 460
PREVIEW_HEIGHT = 250
IMAGE_TYPES = [
    ("Image files", "*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp"),
    ("All files", "*.*"),
]
SAVE_TYPES = [
    ("PNG image", "*.png"),
    ("JPEG image", "*.jpg"),
    ("BMP image", "*.bmp"),
]
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp", ".gif"}

# Driver-license elements. Each one is stored as <LF>CODE + value + <LF>.
_LICENSE_FIELDS: tuple[tuple[str, str], ...] = (
    ("DAC", "First name"),
    ("DAD", "Mid name"),
    ("DCS", "Last name"),
    ("DBD", "Issue date"),
    ("DBB", "Birthday"),
    ("DBA", "Expired date"),
    ("DAG", "Street"),
    ("DAI", "County"),
    ("DAJ", "State"),
    ("DAK", "Zip code"),
    ("DCG", "Country"),
    ("DCF", "DD"),
)
_FIELD_COLUMNS = 3
_FIELD_BOUNDARY = re.compile(r"\r\n|\n|\r|<LF>|<CR>", re.IGNORECASE)


def find_license_fields(text: str) -> dict[str, tuple[int, int, str]]:
    """Return each element found in ``text``.

    The code has to start a segment, right after a line break (or at the
    beginning). The value is everything after the code until the next break.
    Real newlines and ``<LF>`` / ``<CR>`` tags both count, since that is how
    the content box shows the payload. The result maps a code to
    ``(value start, value end, value)`` in ``text``.
    """

    found: dict[str, tuple[int, int, str]] = {}
    last = 0
    cuts = [(match.start(), match.end()) for match in _FIELD_BOUNDARY.finditer(text)]
    cuts.append((len(text), len(text)))
    for boundary, resume in cuts:
        segment = text[last:boundary]
        for code, _label in _LICENSE_FIELDS:
            if code in found or not segment.startswith(code):
                continue
            value_start = last + len(code)
            found[code] = (value_start, boundary, segment[len(code) :])
            break
        last = resume
    return found


class App(TkinterDnD.Tk):
    def __init__(self, *, start_in_tray: bool = False) -> None:
        super().__init__()
        # Withdraw before the window is mapped so a sign-in launch does not flash the UI.
        if start_in_tray:
            self.withdraw()
        self.title("PDF417 Barcode")
        self.geometry("1020x760")
        self.minsize(880, 680)
        self.configure(bg="#f3f4f6")

        self.source_image: Image.Image | None = None
        self.generated_image: Image.Image | None = None
        self.original_text = ""
        self.original_bytes: bytes | None = None
        self.original_is_binary = False
        self.generated_dirty = False
        self._ignore_layout = False
        self._syncing_fields = False
        self._fields_shown = False
        self._fields_dialog: tk.Toplevel | None = None
        self._dialog_content: tk.Text | None = None
        self._code_dialog: tk.Toplevel | None = None
        self.field_vars: dict[str, tk.StringVar] = {}
        self._field_widgets: dict[str, tuple[ttk.Label, ttk.Entry]] = {}
        self._shown_fields: tuple[str, ...] = ()
        self.columns_var = tk.StringVar()
        self.rows_var = tk.StringVar()
        self.security_var = tk.StringVar()
        self._busy = False
        self._ignore_modified = False
        self._events: queue.Queue = queue.Queue()
        self._progress_job: str | None = None
        self._progress_value = 0
        self._zoom = 1.0
        self._views: dict[str, dict] = {}
        self._drag: tuple[str, int, int] | None = None
        self._wheel_target: str | None = None
        self._space_down = False
        self._snipping = False
        self._tray = None
        self._in_tray = start_in_tray

        self._window_icon = ImageTk.PhotoImage(app_icon(64))
        self.iconphoto(True, self._window_icon)
        self._build()
        self._bind_space()
        self._enable_file_drop()
        self._start_screenshot_hotkey()
        self._start_tray()
        if start_in_tray and self._tray is None:
            self._in_tray = False
            self.deiconify()
        self._listen_for_activate()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._refresh_buttons()
        self.after(50, self._poll_events)

    def _build(self) -> None:
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10), background="#f3f4f6")
        style.configure("TFrame", background="#f3f4f6")
        style.configure("TLabel", background="#f3f4f6", foreground="#1f2933")
        style.configure("Hint.TLabel", foreground="#52606d")
        style.configure("Card.TLabelframe", background="#ffffff", relief="solid")
        style.configure("Card.TLabelframe.Label", background="#f3f4f6", font=("Segoe UI", 10, "bold"))
        style.configure("Upload.TButton", font=("Segoe UI", 12, "bold"), padding=(18, 10))
        style.configure("Action.TButton", font=("Segoe UI", 11), padding=(16, 8))
        style.configure("Label.TButton", padding=(10, 1))
        style.configure("Preview.TLabel", background="#eef1f4", foreground="#52606d")

        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(2, weight=1)
        root.rowconfigure(2, weight=1, minsize=PREVIEW_HEIGHT + 36)
        root.rowconfigure(4, weight=1)

        header = ttk.Frame(root)
        header.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Label(header, text="PDF417 Barcode", font=("Segoe UI", 18, "bold")).pack(side="left")
        self.upload_button = ttk.Button(
            header,
            text="Upload Your Bar code",
            style="Upload.TButton",
            command=self._choose_file,
        )
        self.upload_button.pack(side="right")
        self.screenshot_button = ttk.Button(
            header,
            text="Screenshot",
            style="Upload.TButton",
            command=self._take_screenshot,
        )
        self.screenshot_button.pack(side="right", padx=(0, 8))

        ttk.Label(
            root,
            text=(
                "Choose a barcode image, drop one from Explorer, or take a screenshot. "
                "Scroll to zoom both pictures together. "
                "Drag to move both pictures. Hold Space and drag to move one picture."
            ),
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 12))

        self.source_frame = self._preview_card(root, "Uploaded Image", "source", "Click to upload a barcode")
        self.source_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 6))
        self._build_zoom_controls(root)
        self.generated_frame = self._preview_card(
            root, "Generated barcode", "generated", "Generate a barcode to preview it here"
        )
        self.generated_frame.grid(row=2, column=2, sticky="nsew", padx=(6, 0))

        progress_row = ttk.Frame(root)
        progress_row.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 8))
        progress_row.columnconfigure(0, weight=1)
        self.progress = tk.Canvas(
            progress_row,
            height=14,
            bg="#e6e8eb",
            highlightthickness=0,
            bd=0,
        )
        self._bar = self.progress.create_rectangle(0, 0, 0, 14, fill="#1d4e89", width=0)
        self.progress.grid(row=0, column=0, sticky="ew")
        self.progress.bind("<Configure>", self._redraw_progress)
        self.result = ttk.Label(progress_row, text="", font=("Segoe UI", 11, "bold"))
        self.result.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.status = ttk.Label(progress_row, text="Waiting for a barcode image.", style="Hint.TLabel")
        self.status.grid(row=2, column=0, sticky="w", pady=(4, 0))
        layout = ttk.Frame(progress_row)
        layout.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self._layout_spins = [
            self._layout_spin(layout, "Columns", self.columns_var, 1, 30),
            self._layout_spin(layout, "Rows", self.rows_var, 3, 90),
            self._layout_spin(layout, "Error correction", self.security_var, 0, 8),
        ]
        for variable in (self.columns_var, self.rows_var, self.security_var):
            variable.trace_add("write", self._on_layout_changed)

        content_frame = ttk.LabelFrame(root, padding=8)
        content_frame.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(4, 12))
        content_label = ttk.Frame(content_frame, width=400, height=28)
        content_label.grid_propagate(False)
        content_label.columnconfigure(0, weight=1)
        content_label.rowconfigure(0, weight=1)
        ttk.Label(content_label, text="Content").grid(row=0, column=0, sticky="w")
        self.details_button = ttk.Button(
            content_label,
            text="Details",
            style="Label.TButton",
            command=self._open_fields_dialog,
        )
        self.details_button.grid(row=0, column=1, sticky="e")
        content_frame.configure(labelwidget=content_label)
        content_frame.bind("<Configure>", lambda _event: self._fit_content_label(content_frame, content_label), add="+")
        content_frame.rowconfigure(0, weight=1)
        content_frame.columnconfigure(0, weight=1)
        self.content = tk.Text(
            content_frame,
            wrap="char",
            font=("Consolas", 11),
            relief="flat",
            undo=True,
            height=8,
            padx=8,
            pady=8,
        )
        scroll = ttk.Scrollbar(content_frame, command=self.content.yview)
        self.content.configure(yscrollcommand=scroll.set)
        self.content.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.content.bind("<<Modified>>", self._on_modified)

        actions = ttk.Frame(root)
        actions.grid(row=5, column=0, columnspan=3, sticky="e")
        self.generate_button = ttk.Button(actions, text="Generate", style="Action.TButton", command=self._generate)
        self.generate_button.pack(side="left", padx=(0, 8))
        self.save_button = ttk.Button(actions, text="Save", style="Action.TButton", command=self._save)
        self.save_button.pack(side="left")

    def _open_fields_dialog(self) -> None:
        if self._dialog_alive() and self._fields_dialog is not None:
            self._fields_dialog.lift()
            self._fields_dialog.focus_force()
            return
        dialog = tk.Toplevel(self)
        dialog.title("Details")
        dialog.configure(bg="#f3f4f6")
        dialog.geometry("960x680")
        dialog.minsize(760, 520)
        dialog.transient(self)
        dialog.iconphoto(False, self._window_icon)
        self._fields_dialog = dialog

        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(1, weight=1)
        self.fields_frame = self._build_fields(body)

        content_frame = ttk.LabelFrame(body, text="Content", padding=8)
        content_frame.grid(row=1, column=0, sticky="nsew")
        content_frame.rowconfigure(0, weight=1)
        content_frame.columnconfigure(0, weight=1)
        text = tk.Text(
            content_frame,
            wrap="char",
            font=("Consolas", 11),
            relief="flat",
            undo=True,
            height=8,
            padx=8,
            pady=8,
        )
        scroll = ttk.Scrollbar(content_frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        text.bind("<<Modified>>", self._on_dialog_modified)
        self._dialog_content = text
        self._ignore_modified = True
        self._write_text(text, self.content.get("1.0", "end-1c"))
        self._ignore_modified = False
        self._sync_fields_from_content()

        actions = ttk.Frame(body)
        actions.grid(row=2, column=0, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="Save", style="Action.TButton", command=self._save_fields_dialog).pack(
            side="left", padx=(0, 8)
        )
        ttk.Button(actions, text="Cancel", style="Action.TButton", command=self._close_fields_dialog).pack(side="left")

        dialog.protocol("WM_DELETE_WINDOW", self._close_fields_dialog)
        dialog.bind("<Escape>", lambda _event: self._close_fields_dialog())
        dialog.bind("<Control-s>", self._save_fields_shortcut)
        dialog.bind("<Control-S>", self._save_fields_shortcut)
        dialog.update_idletasks()
        width = dialog.winfo_width()
        height = dialog.winfo_height()
        left = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        top = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{left}+{top}")
        dialog.grab_set()
        dialog.focus_force()

    def _apply_fields_dialog(self) -> None:
        """Write the open draft into Content and leave the window open."""
        if self._dialog_content is None:
            return
        updated = self._dialog_content.get("1.0", "end-1c")
        if updated == self.content.get("1.0", "end-1c"):
            return
        self._ignore_modified = True
        self._write_text(self.content, updated)
        self._ignore_modified = False
        self._note_content_edited()

    def _save_fields_shortcut(self, _event=None) -> str:
        self._apply_fields_dialog()
        return "break"

    def _save_fields_dialog(self) -> None:
        self._apply_fields_dialog()
        self._close_fields_dialog()

    def _fit_content_label(self, frame: ttk.LabelFrame, label: ttk.Frame) -> None:
        width = frame.winfo_width()
        if width <= 1:
            return
        target = max(width - 24, 160)
        if str(label.cget("width")) != str(target):
            label.configure(width=target)

    def _close_fields_dialog(self) -> None:
        dialog = self._fields_dialog
        self._fields_dialog = None
        self._dialog_content = None
        self.field_vars = {}
        self._field_widgets = {}
        self._shown_fields = ()
        self._fields_shown = False
        if dialog is not None and dialog.winfo_exists():
            try:
                dialog.grab_release()
            except tk.TclError:
                pass
            dialog.destroy()

    def _dialog_alive(self) -> bool:
        return self._fields_dialog is not None and self._fields_dialog.winfo_exists()

    def _build_fields(self, parent: ttk.Frame) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text="Details", padding=(8, 6))
        self.field_vars = {}
        self._field_widgets = {}
        self._shown_fields = ()
        self._fields_shown = False
        for column in range(1, _FIELD_COLUMNS * 2, 2):
            frame.columnconfigure(column, weight=1)
        for code, label in _LICENSE_FIELDS:
            variable = tk.StringVar()
            variable.trace_add("write", lambda *_args, key=code: self._on_field_write(key))
            name = ttk.Label(frame, text=label)
            entry = ttk.Entry(frame, textvariable=variable)
            self.field_vars[code] = variable
            self._field_widgets[code] = (name, entry)
        return frame

    def _layout_license_fields(self, visible: tuple[str, ...]) -> None:
        for code, _label in _LICENSE_FIELDS:
            name, entry = self._field_widgets[code]
            if name.grid_info():
                name.grid_remove()
            if entry.grid_info():
                entry.grid_remove()
        if not visible:
            if self._fields_shown:
                self.fields_frame.grid_remove()
                self._fields_shown = False
            return
        for index, code in enumerate(visible):
            row, column = divmod(index, _FIELD_COLUMNS)
            name, entry = self._field_widgets[code]
            name.grid(row=row, column=column * 2, sticky="w", padx=(0, 6), pady=3)
            entry.grid(row=row, column=column * 2 + 1, sticky="ew", padx=(0, 12), pady=3)
        self.fields_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self._fields_shown = True

    def _sync_fields_from_content(self) -> None:
        if self._syncing_fields or not self._dialog_alive() or self._dialog_content is None:
            return
        text = self._dialog_content.get("1.0", "end-1c")
        found = find_license_fields(text)
        visible = tuple(code for code, _label in _LICENSE_FIELDS if code in found)
        self._syncing_fields = True
        try:
            for code in visible:
                value = found[code][2]
                if self.field_vars[code].get() != value:
                    self.field_vars[code].set(value)
            if visible != self._shown_fields:
                self._shown_fields = visible
                self._layout_license_fields(visible)
        finally:
            self._syncing_fields = False

    def _on_field_write(self, code: str) -> None:
        if self._syncing_fields:
            return
        if self._dialog_content is None:
            return
        text = self._dialog_content.get("1.0", "end-1c")
        found = find_license_fields(text)
        if code not in found:
            return
        start, end, current = found[code]
        updated = self.field_vars[code].get()
        if updated == current:
            return
        self._replace_span(start, end, updated)

    def _replace_span(self, start: int, end: int, value: str) -> None:
        if self._dialog_content is None:
            return
        self._ignore_modified = True
        self._dialog_content.delete(f"1.0 + {start} chars", f"1.0 + {end} chars")
        if value:
            self._dialog_content.insert(f"1.0 + {start} chars", value)
        self._dialog_content.edit_modified(False)
        self._ignore_modified = False

    def _note_content_edited(self) -> None:
        if self.generated_image is not None:
            self.generated_dirty = True
            self.status.configure(text="Content changed. Click Generate to update the barcode.")
        self._refresh_buttons()

    def _preview_card(self, parent: ttk.Frame, title: str, key: str, placeholder: str) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, text=title, padding=8)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        canvas = tk.Canvas(
            frame,
            bg="#eef1f4",
            height=PREVIEW_HEIGHT,
            highlightthickness=0,
            cursor="hand2",
        )
        canvas.grid(row=0, column=0, sticky="nsew")
        self._views[key] = {
            "canvas": canvas,
            "image": None,
            "pan": [0.0, 0.0],
            "placeholder": placeholder,
            "photo": None,
        }
        canvas.bind("<Configure>", lambda _event, name=key: self._redraw_view(name))
        canvas.bind("<Enter>", lambda _event, name=key: self._arm_wheel(name))
        canvas.bind("<Leave>", lambda _event: self._disarm_wheel())
        canvas.bind("<ButtonPress-1>", lambda event, name=key: self._on_pan_start(event, name))
        canvas.bind("<Double-Button-1>", lambda _event, name=key: self._on_double_click(name))
        return frame

    def _layout_spin(
        self,
        parent: ttk.Frame,
        label: str,
        variable: tk.StringVar,
        low: int,
        high: int,
    ) -> ttk.Spinbox:
        ttk.Label(parent, text=label).pack(side="left")
        spin = ttk.Spinbox(
            parent,
            from_=low,
            to=high,
            textvariable=variable,
            width=5,
            justify="center",
        )
        spin.pack(side="left", padx=(6, 18))
        return spin

    def _build_zoom_controls(self, parent: ttk.Frame) -> None:
        column = ttk.Frame(parent)
        column.grid(row=2, column=1, sticky="ns")
        column.rowconfigure(0, weight=1)
        column.rowconfigure(2, weight=1)
        stack = ttk.Frame(column)
        stack.grid(row=1, column=0)
        self._zoom_icons = {
            "plus": _zoom_icon("plus"),
            "reset": _zoom_icon("reset"),
            "minus": _zoom_icon("minus"),
            "rescan": _zoom_icon("rescan"),
        }
        commands = (
            ("plus", lambda: self._zoom_by(1.25)),
            ("reset", self._view_original),
            ("minus", lambda: self._zoom_by(1 / 1.25)),
            ("rescan", self._rescan),
        )
        for name, command in commands:
            tk.Button(
                stack,
                image=self._zoom_icons[name],
                command=command,
                relief="flat",
                bg="#ffffff",
                activebackground="#e5e7eb",
                bd=1,
                highlightthickness=0,
                cursor="hand2",
                width=32,
                height=32,
            ).pack(pady=3)

    def _choose_file(self) -> None:
        if self._busy:
            return
        path = filedialog.askopenfilename(
            title="Upload Your Bar code",
            filetypes=IMAGE_TYPES,
        )
        if path:
            self._load_path(path)

    def _enable_file_drop(self) -> None:
        self._drop_hover = False
        self._drop_overlay = tk.Frame(self, bg="#e8f1fb")
        card = tk.Frame(
            self._drop_overlay,
            bg="#e8f1fb",
            highlightbackground="#1d4e89",
            highlightthickness=3,
        )
        card.place(relx=0.03, rely=0.03, relwidth=0.94, relheight=0.94)
        tk.Label(
            card,
            text="Drop barcode image",
            bg="#e8f1fb",
            fg="#1d4e89",
            font=("Segoe UI", 22, "bold"),
        ).place(relx=0.5, rely=0.46, anchor="center")
        tk.Label(
            card,
            text="Release to upload",
            bg="#e8f1fb",
            fg="#52606d",
            font=("Segoe UI", 12),
        ).place(relx=0.5, rely=0.56, anchor="center")
        self._register_drop_target(self)

    def _register_drop_target(self, widget: tk.Misc) -> None:
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", self._on_drop_enter)
            widget.dnd_bind("<<DropPosition>>", self._on_drop_position)
            widget.dnd_bind("<<DropLeave>>", self._on_drop_leave)
            widget.dnd_bind("<<Drop>>", self._on_drop)
        except tk.TclError:
            return
        for child in widget.winfo_children():
            self._register_drop_target(child)

    def _on_drop_enter(self, event):
        self._drop_hover = True
        self._drop_overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._drop_overlay.lift()
        return getattr(event, "action", None) or "copy"

    def _on_drop_position(self, event):
        self._drop_hover = True
        return getattr(event, "action", None) or "copy"

    def _on_drop_leave(self, _event):
        self._drop_hover = False
        self.after(120, self._hide_drop_overlay)

    def _hide_drop_overlay(self) -> None:
        if not self._drop_hover:
            self._drop_overlay.place_forget()

    def _on_drop(self, event):
        self._drop_hover = False
        self._drop_overlay.place_forget()
        self._on_files_dropped(list(self.tk.splitlist(event.data)))
        return getattr(event, "action", None) or "copy"

    def _on_files_dropped(self, paths: list[str]) -> None:
        if self._busy:
            return
        images = [path for path in paths if Path(path).suffix.lower() in _IMAGE_SUFFIXES]
        if not images:
            messagebox.showinfo("Not an image", "Drop a barcode image, such as a PNG or JPEG.")
            return
        self._load_path(images[0])

    def _start_screenshot_hotkey(self) -> None:
        if sys.platform == "win32":
            threading.Thread(target=self._screenshot_hotkey_loop, daemon=True).start()

    def _screenshot_hotkey_loop(self) -> None:
        # Windows delivers the hotkey to the thread that registered it, so this
        # thread owns the registration and only hands the press to the Tk loop.
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        mod_alt, mod_norepeat, vk_s, wm_hotkey = 0x0001, 0x4000, 0x53, 0x0312
        if not user32.RegisterHotKey(None, 1, mod_alt | mod_norepeat, vk_s):
            self._events.put(("hotkey-unavailable", None))
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == wm_hotkey:
                self._events.put(("screenshot-hotkey", None))
        user32.UnregisterHotKey(None, 1)

    def _listen_for_activate(self) -> None:
        if sys.platform != "win32" or len(_instance_handles) < 2:
            return
        event = _instance_handles[1]

        def watch() -> None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            while kernel32.WaitForSingleObject(event, 0xFFFFFFFF) == 0:
                self._events.put(("tray-show", None))

        threading.Thread(target=watch, daemon=True).start()

    def _start_tray(self) -> None:
        try:
            import pystray
        except Exception:
            return

        def send(kind: str):
            return lambda _icon, _item: self._events.put((kind, None))

        items: list = [
            pystray.MenuItem("Screenshot\tAlt+S", send("screenshot-hotkey")),
            pystray.MenuItem("Show window", send("tray-show"), default=True),
        ]
        if startup.supported():
            items.append(
                pystray.MenuItem(
                    "Start with Windows",
                    self._toggle_startup,
                    checked=lambda _item: startup.enabled(),
                )
            )
        items.extend(
            (
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", send("tray-exit")),
            )
        )
        menu = pystray.Menu(*items)
        try:
            self._tray = pystray.Icon("PDF417", app_icon(64), "PDF417 Barcode", menu)
            # Callbacks arrive on the tray thread and only queue events for Tk.
            threading.Thread(target=self._tray.run, daemon=True).start()
        except Exception:
            self._tray = None

    def _toggle_startup(self, icon, _item) -> None:
        try:
            startup.set_enabled(not startup.enabled())
        except OSError as exc:
            self._events.put(("startup-error", str(exc)))
            return
        update = getattr(icon, "update_menu", None)
        if not callable(update):
            return
        try:
            update()
        except Exception:
            pass

    def _on_close(self) -> None:
        if self._tray is None:
            self._quit()
            return
        self.withdraw()
        self._in_tray = True

    def _quit(self) -> None:
        if self._tray is not None:
            try:
                # stop() hides the icon from the tray thread, which may not run
                # before the process exits and leaves a ghost icon behind.
                self._tray.visible = False
                self._tray.stop()
            except Exception:
                pass
        self.destroy()

    def _take_screenshot(self) -> None:
        if self._busy or self._snipping:
            return
        self._snipping = True
        self.withdraw()
        self.after(300, self._capture_screen)

    def _capture_screen(self) -> None:
        try:
            shot = ImageGrab.grab(all_screens=True).convert("RGB")
        except Exception as exc:
            self._snipping = False
            self._restore_window()
            messagebox.showerror("Screenshot failed", str(exc))
            return
        left, top = _virtual_screen_origin()
        _ScreenSnip(self, shot, left, top, self._on_snip_done)

    def _on_snip_done(self, image: Image.Image | None) -> None:
        self._snipping = False
        if image is None:
            self.status.configure(text="Screenshot canceled.")
            if not self._in_tray:
                self._restore_window()
            return
        self._restore_window()
        self._load_image(image, "screenshot")

    def _report_startup_error(self, message: str) -> None:
        text = f"Could not change Start with Windows.\n\n{message}"
        if self._in_tray and sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, text, "PDF417 Barcode", 0x10)
            return
        messagebox.showerror("Start with Windows", text)

    def _restore_window(self) -> None:
        self._in_tray = False
        self.deiconify()
        self.lift()
        self.focus_force()

    def _load_path(self, path: str) -> None:
        try:
            image = Image.open(path).convert("RGB")
        except Exception as exc:
            messagebox.showerror("Could not open image", str(exc))
            return
        self._load_image(image, Path(path).name)

    def _rescan(self) -> None:
        if self._busy:
            return
        if self.source_image is None:
            self.status.configure(text="Upload a barcode first, then rescan it.")
            return
        self._load_image(self.source_image, "the current image", keep_view=True)

    def _load_image(self, image: Image.Image, label: str, *, keep_view: bool = False) -> None:
        self._close_code_dialog()
        self.source_image = image
        self.generated_image = None
        self.generated_dirty = False
        self.original_bytes = None
        self.original_text = ""
        self.original_is_binary = False
        self._set_layout_fields(None, None, None)
        self._show_result(None, None, None)
        self._set_text("")
        if not keep_view:
            self._reset_view()
        self._show_image("source", image)
        self._show_placeholder("generated", "Generate a barcode to preview it here")
        self._set_busy(True, f"Reading barcode from {label}...", value=35)
        threading.Thread(target=self._decode_worker, args=(image.copy(),), daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "decoded":
                    self._decode_done(payload)
                elif kind == "common-code":
                    self._common_code_done(payload)
                elif kind == "decode-error":
                    self._decode_failed(payload)
                elif kind == "generated":
                    self._generate_done(payload)
                elif kind == "generate-error":
                    self._generate_failed(payload)
                elif kind == "screenshot-hotkey":
                    self._take_screenshot()
                elif kind == "tray-show":
                    self._restore_window()
                elif kind == "tray-exit":
                    self._quit()
                    return
                elif kind == "startup-error":
                    self._report_startup_error(str(payload))
                elif kind == "hotkey-unavailable":
                    self.status.configure(text="Alt+S is taken by another app, so the screenshot hotkey is off.")
        except queue.Empty:
            pass
        self.after(50, self._poll_events)

    def _decode_worker(self, image: Image.Image) -> None:
        try:
            result, common = read_upload(image)
        except Exception as exc:
            self._events.put(("decode-error", exc))
            return
        if common and not result.symbols:
            self._events.put(("common-code", common))
            return
        self._events.put(("decoded", result))

    def _decode_done(self, result) -> None:
        self._set_busy(False, "", value=100)
        if self.source_image is None:
            self.status.configure(text="Uploaded image cleared.")
            self._refresh_buttons()
            return
        if not result.symbols:
            self.status.configure(text="No PDF417 barcode was found in that image.")
            messagebox.showwarning("No barcode", "No PDF417 barcode was found in that image.")
            self._refresh_buttons()
            return
        symbol = result.symbols[0]
        self.original_text = symbol.text
        self.original_bytes = symbol.raw_bytes
        self.original_is_binary = symbol.content_type.lower() in {"binary", "mixed", "unknowneci"}
        self._set_layout_fields(symbol.columns, symbol.rows, symbol.security_level)
        self._show_result(symbol.rows, symbol.columns, symbol.security_level)
        self._set_text(symbol.text)
        extra = ""
        if len(result.symbols) > 1:
            extra = f" Found {len(result.symbols)} barcodes; showing the first."
        kind = "binary" if self.original_is_binary else "text"
        self.status.configure(
            text=(
                f"Ready. {symbol.format} {kind}, {len(symbol.raw_bytes)} bytes, "
                f"error correction {symbol.ec_level or 'not reported'}.{extra} "
                "Click Generate to draw a barcode with this row count and error correction."
            )
        )
        self._refresh_buttons()

    def _common_code_done(self, symbols: list[Pdf417Symbol]) -> None:
        self._set_busy(False, "", value=100)
        if self.source_image is None:
            self.status.configure(text="Uploaded image cleared.")
            self._refresh_buttons()
            return
        if len(symbols) == 1:
            self.status.configure(text=f"{symbols[0].format} read. Copy the content from the dialog.")
        else:
            self.status.configure(text=f"Found {len(symbols)} barcodes. Copy the content from the dialog.")
        self._show_code_dialog(symbols)
        self._refresh_buttons()

    def _show_code_dialog(self, symbols: list[Pdf417Symbol]) -> None:
        self._close_code_dialog()
        dialog = tk.Toplevel(self)
        self._code_dialog = dialog
        single = len(symbols) == 1
        dialog.title(symbols[0].format if single else "Barcodes")
        dialog.configure(bg="#f3f4f6")
        dialog.transient(self)
        dialog.iconphoto(False, self._window_icon)
        dialog.minsize(480, 120)

        body = ttk.Frame(dialog, padding=16)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        first_field: ttk.Entry | None = None

        for index, symbol in enumerate(symbols):
            block = ttk.Frame(body)
            block.grid(row=index, column=0, sticky="ew", pady=(0 if index == 0 else 12, 0))
            block.columnconfigure(0, weight=1)
            row = 0
            if not single:
                ttk.Label(block, text=symbol.format, font=("Segoe UI", 10, "bold")).grid(
                    row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
                )
                row = 1
            field = self._code_value(block, row, common_code_text(symbol))
            if first_field is None:
                first_field = field

        actions = ttk.Frame(body)
        actions.grid(row=len(symbols), column=0, sticky="e", pady=(12, 0))
        ttk.Button(actions, text="Close", style="Action.TButton", command=self._close_code_dialog).pack(side="left")

        dialog.protocol("WM_DELETE_WINDOW", self._close_code_dialog)
        dialog.bind("<Escape>", lambda _event: self._close_code_dialog())
        dialog.update_idletasks()
        width = max(dialog.winfo_reqwidth(), 520)
        height = max(dialog.winfo_reqheight(), 120)
        left = self.winfo_rootx() + max(0, (self.winfo_width() - width) // 2)
        top = self.winfo_rooty() + max(0, (self.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{left}+{top}")
        dialog.grab_set()
        dialog.focus_force()
        if first_field is not None:
            first_field.focus_set()
            first_field.selection_range(0, "end")

    def _code_value(self, parent: ttk.Frame, row: int, text: str) -> ttk.Entry | None:
        line = ttk.Frame(parent)
        line.grid(row=row, column=0, columnspan=2, sticky="ew")
        line.columnconfigure(0, weight=1)
        button = ttk.Button(line, text="Copy", style="Action.TButton")
        button.configure(command=lambda value=text, widget=button: self._copy_code(value, widget))
        button.grid(row=0, column=1, padx=(8, 0), sticky="ne")
        if "\n" in text:
            box = tk.Text(
                line,
                height=min(8, text.count("\n") + 1),
                width=48,
                wrap="word",
                font=("Consolas", 11),
                relief="solid",
                bd=1,
                padx=8,
                pady=6,
            )
            box.insert("1.0", text)
            box.configure(state="disabled")
            box.grid(row=0, column=0, sticky="ew")
            return None
        entry = ttk.Entry(line, width=48, font=("Consolas", 11))
        entry.insert(0, text)
        entry.configure(state="readonly")
        entry.grid(row=0, column=0, sticky="ew")
        return entry

    def _copy_code(self, text: str, button: ttk.Button) -> None:
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update_idletasks()
        button.configure(text="Copied")

        def restore() -> None:
            if button.winfo_exists():
                button.configure(text="Copy")

        self.after(1200, restore)

    def _close_code_dialog(self) -> None:
        dialog = self._code_dialog
        self._code_dialog = None
        if dialog is None or not dialog.winfo_exists():
            return
        try:
            dialog.grab_release()
        except tk.TclError:
            pass
        dialog.destroy()

    def _decode_failed(self, exc: Exception) -> None:
        self._set_busy(False, "Could not read that image.", value=0)
        messagebox.showerror("Scan failed", str(exc))
        self._refresh_buttons()

    def _generate(self) -> None:
        if self._busy:
            return
        text = self.content.get("1.0", "end-1c")
        if text == "" and not (self.original_is_binary and self.original_bytes):
            messagebox.showinfo("Nothing to encode", "Upload a barcode or type the content first.")
            return
        if text == self.original_text and self.original_bytes is not None:
            raw = self.original_bytes
        else:
            try:
                raw = payload_from_text(text)
            except UnicodeEncodeError:
                messagebox.showerror(
                    "Cannot encode",
                    "The content has characters outside the original payload encoding.",
                )
                return
        try:
            columns = self._read_layout_field(self.columns_var, 1, 30, "Columns")
            rows = self._read_layout_field(self.rows_var, 3, 90, "Rows")
            security = self._read_layout_field(self.security_var, 0, 8, "Error correction")
        except ValueError as exc:
            messagebox.showerror("Check the layout", str(exc))
            return
        self._set_busy(True, f"Generating barcode from {len(raw)} bytes...", value=45)
        threading.Thread(
            target=self._generate_original,
            args=(raw, rows, columns, security),
            daemon=True,
        ).start()

    def _generate_original(
        self,
        raw: bytes,
        rows: int | None,
        columns: int | None,
        security: int | None,
    ) -> None:
        try:
            columns, rows = legal_symbol_size(columns, rows)
            image, options = generate_fitted(
                raw.decode("latin-1"),
                columns=columns,
                rows=rows,
                ec_level=2 if security is None else security,
                scale=4,
                eci=None,
            )
        except Exception as exc:
            self._events.put(("generate-error", exc))
            return
        self._events.put(("generated", (image, options, rows)))

    def _generate_done(self, payload) -> None:
        image, options, _requested_rows = payload
        self.generated_image = image
        self.generated_dirty = False
        self._show_image("generated", image)
        measured = _measured_symbol(image)
        self._fill_blank_layout(measured)
        rows = measured.rows if measured and measured.rows else options.rows
        columns = measured.columns if measured and measured.columns else options.columns
        level = measured.security_level if measured and measured.security_level is not None else options.ec_level
        if rows and columns:
            self._set_layout_fields(columns, rows, level if level is not None else options.ec_level)
        self._show_result(rows, columns, level)
        if options.shortened and isinstance(options.encoded, str):
            display = measured.text if measured and measured.text else options.encoded
            self.original_text = display
            self.original_bytes = options.encoded.encode("latin-1")
            self.original_is_binary = False
            self._set_text(display)
            message = "Cut the content to the maximum a PDF417 can hold. Click Save to write the image."
        else:
            message = "Generated the full payload. Click Save to write the image."
        self._set_busy(False, message, value=100)
        self._refresh_buttons()

    def _generate_failed(self, exc: Exception) -> None:
        self._set_busy(False, "Could not generate a barcode.", value=0)
        messagebox.showerror("Generate failed", str(exc))
        self._refresh_buttons()

    def _save(self) -> None:
        if self.generated_image is None or self._busy:
            return
        if self.generated_dirty:
            messagebox.showinfo("Generate again", "The content or layout changed. Click Generate before saving.")
            return
        path = filedialog.asksaveasfilename(
            title="Save barcode",
            defaultextension=".png",
            filetypes=SAVE_TYPES,
            initialfile="barcode.png",
        )
        if not path:
            return
        destination = Path(path)
        image = self.generated_image
        if destination.suffix.lower() == ".png":
            image = with_transparent_background(image)
        elif destination.suffix.lower() in {".jpg", ".jpeg"}:
            image = image.convert("RGB")
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination)
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return
        self.status.configure(text=f"Saved {destination}")

    def _reset_view(self) -> None:
        self._zoom = 1.0
        for view in self._views.values():
            view["pan"] = [0.0, 0.0]

    def _view_original(self) -> None:
        self._reset_view()
        for name in self._views:
            self._redraw_view(name)

    def _zoom_by(self, factor: float) -> None:
        new_zoom = min(30.0, max(0.25, self._zoom * factor))
        applied = new_zoom / self._zoom
        if applied == 1:
            return
        self._zoom = new_zoom
        for name, view in self._views.items():
            pan_x, pan_y = view["pan"]
            view["pan"] = [pan_x * applied, pan_y * applied]
            self._redraw_view(name)

    def _show_image(self, key: str, image: Image.Image) -> None:
        view = self._views[key]
        view["image"] = image
        view["placeholder"] = ""
        self._redraw_view(key)

    def _show_placeholder(self, key: str, text: str) -> None:
        view = self._views[key]
        view["image"] = None
        view["photo"] = None
        view["placeholder"] = text
        self._redraw_view(key)

    def _arm_wheel(self, key: str) -> None:
        self._wheel_target = key
        self.bind_all("<MouseWheel>", self._on_wheel)

    def _disarm_wheel(self) -> None:
        self._wheel_target = None
        self.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        if self._wheel_target is None:
            return
        self._on_zoom(event, self._wheel_target)

    def _on_zoom(self, event, key: str) -> None:
        if event.delta:
            factor = 1.1 ** (event.delta / 120)
        elif getattr(event, "num", None) == 4:
            factor = 1.1
        else:
            factor = 1 / 1.1
        new_zoom = min(30.0, max(0.25, self._zoom * factor))
        applied = new_zoom / self._zoom
        if applied == 1:
            return
        origin = self._views[key]["canvas"]
        origin_w = max(origin.winfo_width(), 1)
        origin_h = max(origin.winfo_height(), 1)
        fraction_x = event.x / origin_w
        fraction_y = event.y / origin_h
        self._zoom = new_zoom
        for name, view in self._views.items():
            canvas = view["canvas"]
            width = max(canvas.winfo_width(), 1)
            height = max(canvas.winfo_height(), 1)
            cursor_x = fraction_x * width
            cursor_y = fraction_y * height
            pan_x, pan_y = view["pan"]
            view["pan"] = [
                (cursor_x - width / 2) * (1 - applied) + pan_x * applied,
                (cursor_y - height / 2) * (1 - applied) + pan_y * applied,
            ]
            self._redraw_view(name)

    def _is_original_view(self) -> bool:
        if abs(self._zoom - 1.0) > 0.001:
            return False
        return all(
            abs(view["pan"][0]) < 0.5 and abs(view["pan"][1]) < 0.5
            for view in self._views.values()
        )

    def _on_double_click(self, key: str) -> None:
        if self._views[key]["image"] is None:
            return
        self._on_pan_end()
        if self._is_original_view():
            self._zoom = 2.0
            for name in self._views:
                self._redraw_view(name)
            return
        self._view_original()

    def _on_pan_start(self, event, key: str) -> None:
        if key == "source" and self._views[key]["image"] is None:
            self._choose_file()
            return
        if key == "generated" and self._views[key]["image"] is None:
            self.content.focus_set()
            self.content.mark_set("insert", "end")
            self.content.see("insert")
            return
        self._drag = (key, event.x_root, event.y_root)
        self.bind_all("<B1-Motion>", self._on_pan_move)
        self.bind_all("<ButtonRelease-1>", self._on_pan_end)

    def _on_pan_move(self, event) -> None:
        if self._drag is None:
            return
        key, origin_x, origin_y = self._drag
        delta_x = event.x_root - origin_x
        delta_y = event.y_root - origin_y
        self._drag = (key, event.x_root, event.y_root)
        names = [key] if self._space_down else list(self._views)
        for name in names:
            pan = self._views[name]["pan"]
            pan[0] += delta_x
            pan[1] += delta_y
            self._redraw_view(name)

    def _on_pan_end(self, _event=None) -> None:
        self._drag = None
        self.unbind_all("<B1-Motion>")
        self.unbind_all("<ButtonRelease-1>")

    def _bind_space(self) -> None:
        """Space pans one preview. It must not activate the focused button."""
        for widget_class in ("Button", "TButton"):
            self.bind_class(widget_class, "<KeyPress-space>", self._on_space_press)
            self.bind_class(widget_class, "<KeyRelease-space>", self._on_space_release)
        self.bind_all("<KeyPress-space>", self._on_space_press)
        self.bind_all("<KeyRelease-space>", self._on_space_release)
        self.bind("<FocusOut>", self._on_space_release)

    def _on_space_press(self, event) -> str | None:
        self._space_down = True
        if event.widget.winfo_class() in {"Button", "TButton"}:
            return "break"
        return None

    def _on_space_release(self, event=None) -> str | None:
        self._space_down = False
        widget = getattr(event, "widget", None)
        if widget is not None and widget.winfo_class() in {"Button", "TButton"}:
            return "break"
        return None

    def _redraw_view(self, key: str) -> None:
        view = self._views.get(key)
        if view is None:
            return
        canvas: tk.Canvas = view["canvas"]
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        canvas.delete("all")
        image: Image.Image | None = view["image"]
        if image is None:
            canvas.create_text(
                width / 2,
                height / 2,
                text=view["placeholder"],
                fill="#52606d",
                font=("Segoe UI", 10),
                width=width - 24,
            )
            return
        image_w, image_h = image.size
        fit = min(width / image_w, height / image_h)
        scale = fit * self._zoom
        pan_x, pan_y = view["pan"]
        left = width / 2 + pan_x - (image_w * scale) / 2
        top = height / 2 + pan_y - (image_h * scale) / 2
        src_x0 = max(0, int((0 - left) / scale))
        src_y0 = max(0, int((0 - top) / scale))
        src_x1 = min(image_w, int((width - left) / scale) + 1)
        src_y1 = min(image_h, int((height - top) / scale) + 1)
        if src_x1 <= src_x0 or src_y1 <= src_y0:
            view["photo"] = None
            return
        crop = image.crop((src_x0, src_y0, src_x1, src_y1))
        draw_w = max(1, min(width + 4, int(round((src_x1 - src_x0) * scale))))
        draw_h = max(1, min(height + 4, int(round((src_y1 - src_y0) * scale))))
        resized = crop.resize((draw_w, draw_h), Image.Resampling.NEAREST)
        photo = ImageTk.PhotoImage(resized)
        view["photo"] = photo
        canvas.create_image(left + src_x0 * scale, top + src_y0 * scale, image=photo, anchor="nw")

    def _write_text(self, widget: tk.Text, value: str) -> None:
        widget.delete("1.0", "end")
        if value:
            widget.insert("1.0", value)
        widget.edit_modified(False)

    def _set_text(self, value: str) -> None:
        self._ignore_modified = True
        self._write_text(self.content, value)
        self._ignore_modified = False
        if self._dialog_alive():
            self._close_fields_dialog()

    def _on_modified(self, _event=None) -> None:
        if self._ignore_modified or not self.content.edit_modified():
            return
        self.content.edit_modified(False)
        self._note_content_edited()

    def _on_dialog_modified(self, _event=None) -> None:
        if self._ignore_modified or self._dialog_content is None or not self._dialog_content.edit_modified():
            return
        self._dialog_content.edit_modified(False)
        self._sync_fields_from_content()

    def _show_result(self, rows: int | None, columns: int | None, level: int | None) -> None:
        if rows and columns and level is not None:
            text = f"{rows} rows, {columns} columns, error correction {level}"
        elif rows and columns:
            text = f"{rows} rows, {columns} columns"
        else:
            text = ""
        self.result.configure(text=text)

    def _set_layout_fields(self, columns: int | None, rows: int | None, security: int | None) -> None:
        self._ignore_layout = True
        self.columns_var.set("" if columns is None else str(columns))
        self.rows_var.set("" if rows is None else str(rows))
        self.security_var.set("" if security is None else str(security))
        self._ignore_layout = False

    def _fill_blank_layout(self, symbol: Pdf417Symbol | None) -> None:
        if symbol is None:
            return
        self._ignore_layout = True
        if not self.columns_var.get().strip() and symbol.columns:
            self.columns_var.set(str(symbol.columns))
        if not self.rows_var.get().strip() and symbol.rows:
            self.rows_var.set(str(symbol.rows))
        if not self.security_var.get().strip() and symbol.security_level is not None:
            self.security_var.set(str(symbol.security_level))
        self._ignore_layout = False

    def _read_layout_field(self, variable: tk.StringVar, low: int, high: int, name: str) -> int | None:
        text = variable.get().strip()
        if not text:
            return None
        try:
            value = int(text)
        except ValueError:
            raise ValueError(f"{name} must be a whole number.") from None
        if not low <= value <= high:
            raise ValueError(f"{name} must be from {low} to {high}.")
        return value

    def _on_layout_changed(self, *_args) -> None:
        if self._ignore_layout:
            return
        if self.generated_image is not None:
            self.generated_dirty = True
            self.status.configure(text="Layout changed. Click Generate to update the barcode.")
        self._refresh_buttons()

    def _set_busy(self, busy: bool, message: str, value: int = 0) -> None:
        self._busy = busy
        self._stop_progress()
        self._progress_value = 0 if busy else value
        if busy:
            self._pulse = -120
            self._animate_progress()
        else:
            self._redraw_progress()
        if message:
            self.status.configure(text=message)
        self._refresh_buttons()

    def _stop_progress(self) -> None:
        if self._progress_job is not None:
            self.after_cancel(self._progress_job)
            self._progress_job = None

    def _animate_progress(self) -> None:
        if not self._busy:
            return
        width = max(self.progress.winfo_width(), 1)
        height = max(self.progress.winfo_height(), 14)
        chunk = max(90, width // 4)
        self._pulse = getattr(self, "_pulse", -chunk) + 16
        if self._pulse > width:
            self._pulse = -chunk
        self.progress.coords(self._bar, self._pulse, 0, self._pulse + chunk, height)
        self._progress_job = self.after(30, self._animate_progress)

    def _redraw_progress(self, _event=None) -> None:
        if self._busy:
            return
        width = max(self.progress.winfo_width(), 1)
        height = max(self.progress.winfo_height(), 14)
        fill = int(width * max(0, min(self._progress_value, 100)) / 100)
        self.progress.coords(self._bar, 0, 0, fill, height)

    def _refresh_buttons(self) -> None:
        text = self.content.get("1.0", "end-1c").strip()
        can_generate = not self._busy and (bool(text) or self.original_is_binary)
        can_save = not self._busy and self.generated_image is not None and not self.generated_dirty
        state = "disabled" if self._busy else "normal"
        self.upload_button.configure(state=state)
        self.screenshot_button.configure(state=state)
        self.details_button.configure(state=state)
        for spin in self._layout_spins:
            spin.configure(state=state)
        self.generate_button.configure(state="normal" if can_generate else "disabled")
        self.save_button.configure(state="normal" if can_save else "disabled")


_CONTROL_NAMES = (
    "NUL", "SOH", "STX", "ETX", "EOT", "ENQ", "ACK", "BEL",
    "BS", "HT", "LF", "VT", "FF", "CR", "SO", "SI",
    "DLE", "DC1", "DC2", "DC3", "DC4", "NAK", "SYN", "ETB",
    "CAN", "EM", "SUB", "ESC", "FS", "GS", "RS", "US",
)
_CONTROL_BYTES = {name: chr(index) for index, name in enumerate(_CONTROL_NAMES)}
_CONTROL_BYTES["DEL"] = chr(127)
_CONTROL_TAG = re.compile(r"<([A-Za-z][A-Za-z0-9]*)>")


def payload_from_text(text: str) -> bytes:
    """Turn the on-screen text back into payload bytes.

    The box shows controls as tags such as ``<LF>``. Those tags are four characters
    on screen and one byte in the barcode.
    """

    def replace(match: re.Match[str]) -> str:
        control = _CONTROL_BYTES.get(match.group(1).upper())
        if control is None:
            return match.group(0)
        return control

    return _CONTROL_TAG.sub(replace, text).encode("latin-1")


class _ScreenSnip(tk.Toplevel):
    """Full-screen overlay for dragging a box around part of a screenshot."""

    MIN_SIZE = 8

    def __init__(self, master: tk.Tk, shot: Image.Image, left: int, top: int, on_done) -> None:
        super().__init__(master)
        self._shot = shot
        self._on_done = on_done
        self._start: tuple[int, int] | None = None
        self._box: int | None = None
        self._finished = False

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.geometry(f"{shot.width}x{shot.height}+{left}+{top}")
        self.configure(cursor="crosshair")

        self._canvas = tk.Canvas(self, highlightthickness=0, bd=0, cursor="crosshair")
        self._canvas.pack(fill="both", expand=True)
        dimmed = ImageEnhance.Brightness(shot).enhance(0.6)
        self._photo = ImageTk.PhotoImage(dimmed)
        self._canvas.create_image(0, 0, image=self._photo, anchor="nw")
        # The selection shows the undimmed screenshot, copied in by Tk on each move.
        self._bright = ImageTk.PhotoImage(shot)
        self._selection = tk.PhotoImage(master=self, width=1, height=1)
        self._selection_item = self._canvas.create_image(0, 0, image=self._selection, anchor="nw", state="hidden")

        self._canvas.bind("<ButtonPress-1>", self._on_press)
        self._canvas.bind("<B1-Motion>", self._on_move)
        self._canvas.bind("<ButtonRelease-1>", self._on_release)
        self._canvas.bind("<ButtonPress-3>", lambda _event: self._finish(None))
        self.bind("<Escape>", lambda _event: self._finish(None))
        self.after(50, self.focus_force)

    def _on_press(self, event) -> None:
        self._start = (event.x, event.y)
        if self._box is not None:
            self._canvas.delete(self._box)
        self._canvas.itemconfigure(self._selection_item, state="hidden")
        self._box = self._canvas.create_rectangle(
            event.x, event.y, event.x, event.y, outline="#4da3ff", width=2
        )

    def _on_move(self, event) -> None:
        if self._start is None or self._box is None:
            return
        self._canvas.coords(self._box, self._start[0], self._start[1], event.x, event.y)
        left, right = sorted((max(0, self._start[0]), max(0, min(self._shot.width, event.x))))
        top, bottom = sorted((max(0, self._start[1]), max(0, min(self._shot.height, event.y))))
        if right - left < 1 or bottom - top < 1:
            self._canvas.itemconfigure(self._selection_item, state="hidden")
            return
        self._selection.configure(width=right - left, height=bottom - top)
        self._selection.tk.call(
            str(self._selection), "copy", str(self._bright), "-from", left, top, right, bottom, "-to", 0, 0
        )
        self._canvas.coords(self._selection_item, left, top)
        self._canvas.itemconfigure(self._selection_item, state="normal")
        self._canvas.tag_raise(self._box)

    def _on_release(self, event) -> None:
        if self._start is None:
            return
        x0, y0 = self._start
        left, right = sorted((max(0, x0), min(self._shot.width, event.x)))
        top, bottom = sorted((max(0, y0), min(self._shot.height, event.y)))
        if right - left < self.MIN_SIZE or bottom - top < self.MIN_SIZE:
            self._finish(self._shot)
            return
        self._finish(self._shot.crop((left, top, right, bottom)))

    def _finish(self, image: Image.Image | None) -> None:
        if self._finished:
            return
        self._finished = True
        self.destroy()
        self._on_done(image)


def _virtual_screen_origin() -> tuple[int, int]:
    """Top-left of the area that ImageGrab.grab(all_screens=True) covers."""
    try:
        import ctypes

        metrics = ctypes.windll.user32.GetSystemMetrics
        return metrics(76), metrics(77)
    except Exception:
        return 0, 0


def _zoom_icon(kind: str) -> ImageTk.PhotoImage:
    from PIL import ImageDraw

    size = 22
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    ink = (31, 41, 51, 255)
    if kind == "plus":
        draw.rectangle((9, 4, 12, 17), fill=ink)
        draw.rectangle((4, 9, 17, 12), fill=ink)
    elif kind == "minus":
        draw.rectangle((4, 9, 17, 12), fill=ink)
    elif kind == "rescan":
        return ImageTk.PhotoImage(_rescan_icon(size, ink))
    else:
        draw.rectangle((3, 3, 9, 5), fill=ink)
        draw.rectangle((3, 3, 5, 9), fill=ink)
        draw.rectangle((12, 3, 18, 5), fill=ink)
        draw.rectangle((16, 3, 18, 9), fill=ink)
        draw.rectangle((3, 16, 9, 18), fill=ink)
        draw.rectangle((3, 12, 5, 18), fill=ink)
        draw.rectangle((12, 16, 18, 18), fill=ink)
        draw.rectangle((16, 12, 18, 18), fill=ink)
    return ImageTk.PhotoImage(image)


def _rescan_icon(size: int, ink: tuple[int, int, int, int]) -> Image.Image:
    """Circular arrow, drawn large and scaled down so the curve stays smooth."""
    from PIL import ImageDraw

    big = size * 4
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    center = big / 2
    radius = big * 0.3
    draw.arc(
        (center - radius, center - radius, center + radius, center + radius),
        start=30,
        end=300,
        fill=ink,
        width=int(big * 0.1),
    )
    end = math.radians(300)
    tip_x = center + radius * math.cos(end)
    tip_y = center + radius * math.sin(end)
    forward = (-math.sin(end), math.cos(end))
    outward = (math.cos(end), math.sin(end))
    head = big * 0.17
    draw.polygon(
        [
            (tip_x + forward[0] * head, tip_y + forward[1] * head),
            (tip_x + outward[0] * head, tip_y + outward[1] * head),
            (tip_x - outward[0] * head, tip_y - outward[1] * head),
        ],
        fill=ink,
    )
    return image.resize((size, size), Image.Resampling.LANCZOS)


def _layout_text(symbol: Pdf417Symbol) -> str:
    rows = str(symbol.rows) if symbol.rows else "unknown"
    columns = str(symbol.columns) if symbol.columns else "unknown"
    if symbol.security_level is None:
        security = symbol.ec_level or "unknown"
    else:
        words = f", {symbol.ec_codewords} codewords" if symbol.ec_codewords else ""
        percent = f", {symbol.ec_level}" if symbol.ec_level else ""
        security = f"level {symbol.security_level}{words}{percent}"
    return f"Rows: {rows}    Columns: {columns}    Error correction: {security}"


def _generate_full_content(
    raw: bytes,
    rows: int | None,
    columns: int | None,
    security: int | None,
) -> tuple[Image.Image, GenerateOptions]:
    """Encode every original byte, and skip row counts that only add padding."""
    content = raw.decode("latin-1")
    natural, natural_options = generate_fitted(
        content,
        columns=columns,
        rows=None,
        ec_level=security,
        scale=4,
        eci=None,
    )
    natural_rows = _measured_rows(natural)
    if not rows:
        return natural, _with_measured_rows(natural_options, natural_rows)
    try:
        forced, forced_options = generate_fitted(
            content,
            columns=columns,
            rows=rows,
            ec_level=security,
            scale=4,
            eci=None,
        )
    except Exception:
        return natural, _with_measured_rows(natural_options, natural_rows)
    forced_rows = _measured_rows(forced)
    # A taller symbol than the data needs is filled with the same pad codeword on every extra row.
    if forced_rows and natural_rows and forced_rows > natural_rows:
        return natural, _with_measured_rows(natural_options, natural_rows)
    if _measured_bytes(forced) == raw and forced_rows == natural_rows:
        return forced, _with_measured_rows(forced_options, forced_rows)
    if _measured_bytes(natural) == raw:
        return natural, _with_measured_rows(natural_options, natural_rows)
    return forced, _with_measured_rows(forced_options, forced_rows)


def _with_measured_rows(options: GenerateOptions, rows: int | None) -> GenerateOptions:
    if not rows or options.rows == rows:
        return options
    return GenerateOptions(
        columns=options.columns,
        rows=rows,
        ec_level=options.ec_level,
        scale=options.scale,
        quiet_zone=options.quiet_zone,
        eci=options.eci,
    )


def _describe_generated(image: Image.Image, options: GenerateOptions) -> str | None:
    try:
        found = decode_image(image)
    except Exception:
        return None
    if not found.symbols:
        return None
    symbol = found.symbols[0]
    if symbol.rows is None:
        symbol.rows = options.rows
    if symbol.columns is None:
        symbol.columns = options.columns
    if symbol.security_level is None:
        symbol.security_level = options.ec_level
    text = _layout_text(symbol)
    return f"{text}    Payload: {len(symbol.raw_bytes)} bytes"


def _measured_symbol(image: Image.Image) -> Pdf417Symbol | None:
    try:
        found = decode_image(image)
    except Exception:
        return None
    if not found.symbols:
        return None
    return found.symbols[0]


def _measured_bytes(image: Image.Image) -> bytes | None:
    try:
        found = decode_image(image)
    except Exception:
        return None
    if not found.symbols:
        return None
    return found.symbols[0].raw_bytes


def _measured_rows(image: Image.Image) -> int | None:
    try:
        found = decode_image(image)
    except Exception:
        return None
    if not found.symbols:
        return None
    return found.symbols[0].rows


def main() -> None:
    _enable_windows_dpi()
    start_in_tray = startup.hidden_on_launch()
    if not _claim_single_instance(show_existing=not start_in_tray):
        return
    startup.use_existing_hidden_launch()
    app = App(start_in_tray=start_in_tray)
    app.mainloop()


_INSTANCE_MUTEX = "Local\\PDF417Barcode"
_INSTANCE_EVENT = "Local\\PDF417BarcodeShow"
_instance_handles: list[int] = []


def _claim_single_instance(*, show_existing: bool = True) -> bool:
    """Open one window. A second launch shows the window that is already running."""
    if sys.platform != "win32":
        return True
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetLastError.restype = wintypes.DWORD

    mutex = kernel32.CreateMutexW(None, False, _INSTANCE_MUTEX)
    if not mutex:
        return True
    already_running = kernel32.GetLastError() == 183
    if already_running:
        kernel32.CloseHandle(mutex)
        if show_existing:
            _signal_running_instance()
        return False
    event = kernel32.CreateEventW(None, False, False, _INSTANCE_EVENT)
    _instance_handles.extend([mutex, event])
    return True


def _signal_running_instance() -> None:
    import ctypes
    import time
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    ctypes.windll.user32.AllowSetForegroundWindow(0xFFFFFFFF)
    for _ in range(20):
        event = kernel32.OpenEventW(0x0002, False, _INSTANCE_EVENT)
        if event:
            kernel32.SetEvent(event)
            kernel32.CloseHandle(event)
            return
        time.sleep(0.05)


def _enable_windows_dpi() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        return
