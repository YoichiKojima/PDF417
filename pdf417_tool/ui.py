"""Single-window PDF417 upload, edit, generate, and save."""

from __future__ import annotations

import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageTk

from .decode import Pdf417Symbol, decode_image, decode_path
from .encode import GenerateOptions, generate_fitted, with_transparent_background

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


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("PDF417 Barcode")
        self.geometry("1020x760")
        self.minsize(880, 680)
        self.configure(bg="#f3f4f6")

        self.source_image: Image.Image | None = None
        self.generated_image: Image.Image | None = None
        self.original_text = ""
        self.original_bytes: bytes | None = None
        self.original_is_binary = False
        self.layout_rows: int | None = None
        self.layout_columns: int | None = None
        self.layout_security: int | None = None
        self.generated_dirty = False
        self._busy = False
        self._ignore_modified = False
        self._events: queue.Queue = queue.Queue()
        self._progress_job: str | None = None
        self._progress_value = 0
        self._zoom = 1.0
        self._views: dict[str, dict] = {}
        self._drag: tuple[str, int, int] | None = None
        self._wheel_target: str | None = None

        self._build()
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

        ttk.Label(
            root,
            text=(
                "Choose a barcode image. Scroll to zoom both pictures together. "
                "Drag to move both pictures. Hold Ctrl and drag to move one picture."
            ),
            style="Hint.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 12))

        self.source_frame = self._preview_card(root, "Uploaded barcode", "source", "Click to upload a barcode")
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
        self.status = ttk.Label(progress_row, text="Waiting for a barcode image.", style="Hint.TLabel")
        self.status.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.details = ttk.Label(
            progress_row,
            text="Rows, columns, and error correction appear after a barcode is read.",
            font=("Segoe UI", 11, "bold"),
        )
        self.details.grid(row=2, column=0, sticky="w", pady=(4, 0))

        content_frame = ttk.LabelFrame(root, text="Content", padding=8)
        content_frame.grid(row=4, column=0, columnspan=3, sticky="nsew", pady=(4, 12))
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
        return frame

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
        }
        commands = (
            ("plus", lambda: self._zoom_by(1.25)),
            ("reset", self._view_original),
            ("minus", lambda: self._zoom_by(1 / 1.25)),
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

    def _load_path(self, path: str) -> None:
        try:
            image = Image.open(path).convert("RGB")
        except Exception as exc:
            messagebox.showerror("Could not open image", str(exc))
            return
        self.source_image = image
        self.generated_image = None
        self.generated_dirty = False
        self.original_bytes = None
        self.original_text = ""
        self.original_is_binary = False
        self.layout_rows = None
        self.layout_columns = None
        self.layout_security = None
        self.details.configure(text="Rows, columns, and error correction appear after a barcode is read.")
        self._set_text("")
        self._reset_view()
        self._show_image("source", image)
        self._show_placeholder("generated", "Generate a barcode to preview it here")
        self._set_busy(True, f"Reading barcode from {Path(path).name}...", value=35)
        threading.Thread(target=self._decode_worker, args=(path,), daemon=True).start()

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self._events.get_nowait()
                if kind == "decoded":
                    self._decode_done(payload)
                elif kind == "decode-error":
                    self._decode_failed(payload)
                elif kind == "generated":
                    self._generate_done(payload)
                elif kind == "generate-error":
                    self._generate_failed(payload)
        except queue.Empty:
            pass
        self.after(50, self._poll_events)

    def _decode_worker(self, path: str) -> None:
        try:
            result = decode_path(path)
        except Exception as exc:
            self._events.put(("decode-error", exc))
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
        self.layout_rows = symbol.rows
        self.layout_columns = symbol.columns
        self.layout_security = symbol.security_level
        self._set_text(symbol.text)
        self.details.configure(text=f"{_layout_text(symbol)}    Payload: {len(symbol.raw_bytes)} bytes")
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
        self._set_busy(True, f"Generating barcode from {len(raw)} bytes...", value=45)
        threading.Thread(
            target=self._generate_original,
            args=(raw, self.layout_rows, self.layout_columns, self.layout_security),
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
            image, options = _generate_full_content(raw, rows, columns, security)
        except Exception as exc:
            self._events.put(("generate-error", exc))
            return
        self._events.put(("generated", (image, options, rows)))

    def _generate_done(self, payload) -> None:
        image, options, requested_rows = payload
        self.generated_image = image
        self.generated_dirty = False
        self._show_image("generated", image)
        drawn = _describe_generated(image, options)
        if drawn is not None:
            self.details.configure(text=drawn)
        rows = options.rows
        columns = options.columns
        level = options.ec_level
        if rows and columns:
            size = f"{rows} rows, {columns} columns, error correction {level}"
            if requested_rows and rows == requested_rows:
                message = f"Generated the full payload with the scanned layout: {size}. Click Save to write the image."
            else:
                message = f"Generated the full payload: {size}. Click Save to write the image."
        else:
            message = "Barcode generated. Click Save to write the image."
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
            messagebox.showinfo("Generate again", "The content changed. Click Generate before saving.")
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
        one_side = bool(event.state & 0x4)
        names = [key] if one_side else list(self._views)
        for name in names:
            pan = self._views[name]["pan"]
            pan[0] += delta_x
            pan[1] += delta_y
            self._redraw_view(name)

    def _on_pan_end(self, _event=None) -> None:
        self._drag = None
        self.unbind_all("<B1-Motion>")
        self.unbind_all("<ButtonRelease-1>")

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

    def _set_text(self, value: str) -> None:
        self._ignore_modified = True
        self.content.delete("1.0", "end")
        if value:
            self.content.insert("1.0", value)
        self.content.edit_modified(False)
        self._ignore_modified = False

    def _on_modified(self, _event=None) -> None:
        if self._ignore_modified or not self.content.edit_modified():
            return
        self.content.edit_modified(False)
        if self.generated_image is not None:
            self.generated_dirty = True
            self.status.configure(text="Content changed. Click Generate to update the barcode.")
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
        self.upload_button.configure(state="disabled" if self._busy else "normal")
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
    app = App()
    app.mainloop()


def _enable_windows_dpi() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        return
