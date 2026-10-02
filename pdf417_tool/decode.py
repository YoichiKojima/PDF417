"""Read PDF417 symbols from an image."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

import zxingcpp

from .layout import measure_layout


@dataclass
class Pdf417Symbol:
    """Everything recovered from one PDF417 symbol."""

    index: int
    text: str
    raw_bytes: bytes
    content_type: str
    ec_level: str
    symbology_identifier: str
    orientation: int
    valid: bool
    error: str | None
    position: dict[str, dict[str, int]]
    extra: dict | None = None
    format: str = "PDF417"
    rows: int | None = None
    columns: int | None = None
    security_level: int | None = None
    ec_codewords: int | None = None

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "format": self.format,
            "valid": self.valid,
            "text": self.text,
            "raw_hex": self.raw_bytes.hex(),
            "byte_length": len(self.raw_bytes),
            "content_type": self.content_type,
            "ec_level": self.ec_level,
            "security_level": self.security_level,
            "ec_codewords": self.ec_codewords,
            "rows": self.rows,
            "columns": self.columns,
            "symbology_identifier": self.symbology_identifier,
            "orientation": self.orientation,
            "error": self.error,
            "position": self.position,
            "extra": self.extra,
        }


@dataclass
class DecodeResult:
    symbols: list[Pdf417Symbol] = field(default_factory=list)
    image_size: tuple[int, int] = (0, 0)
    source: str = ""


def decode_path(path: str | Path, *, pdf417_only: bool = True) -> DecodeResult:
    image_path = Path(path)
    if not image_path.is_file():
        raise FileNotFoundError(f"Image not found: {image_path}")
    with Image.open(image_path) as image:
        loaded = image.convert("RGB")
    result = decode_image(loaded, pdf417_only=pdf417_only)
    result.source = str(image_path)
    return result


def decode_image(image: Image.Image, *, pdf417_only: bool = True) -> DecodeResult:
    """Decode PDF417 symbols, trying a few image variants when the first pass misses."""
    last_error: Exception | None = None
    for scale, candidate in _image_variants(image):
        try:
            symbols = _read(candidate, pdf417_only=pdf417_only)
        except Exception as exc:  # library can raise on unusual buffers
            last_error = exc
            continue
        if symbols:
            if scale != 1:
                for symbol in symbols:
                    symbol.position = _scale_position(symbol.position, 1 / scale)
            for symbol in symbols:
                measured = measure_layout(
                    image,
                    symbol.position,
                    symbol.ec_level,
                    compact="Compact" in symbol.format,
                )
                symbol.rows = measured.rows
                symbol.columns = measured.columns
                symbol.security_level = measured.security_level
                symbol.ec_codewords = measured.ec_codewords
            return DecodeResult(symbols=symbols, image_size=image.size)
    if last_error is not None:
        raise RuntimeError(f"Barcode reader failed: {last_error}") from last_error
    return DecodeResult(symbols=[], image_size=image.size)


def _image_variants(image: Image.Image):
    rgb = image.convert("RGB")
    yield 1.0, rgb
    gray = ImageOps.grayscale(rgb)
    yield 1.0, gray
    yield 1.0, ImageOps.autocontrast(gray)
    if max(rgb.size) < 900:
        yield 2.0, gray.resize((gray.width * 2, gray.height * 2), Image.Resampling.NEAREST)


def _read(image: Image.Image, *, pdf417_only: bool) -> list[Pdf417Symbol]:
    formats = zxingcpp.BarcodeFormat.PDF417 if pdf417_only else zxingcpp.BarcodeFormats()
    found = zxingcpp.read_barcodes(
        image,
        formats=formats,
        try_rotate=True,
        try_downscale=True,
        try_invert=True,
        text_mode=zxingcpp.TextMode.HRI,
    )
    symbols: list[Pdf417Symbol] = []
    for index, barcode in enumerate(found):
        if pdf417_only and str(barcode.format) not in {"PDF417", "PDF417Compact"}:
            continue
        position = barcode.position
        error = barcode.error
        symbols.append(
            Pdf417Symbol(
                index=index,
                format=_label(barcode.format),
                text=barcode.text or "",
                raw_bytes=bytes(barcode.bytes or b""),
                content_type=_label(barcode.content_type),
                ec_level=str(barcode.ec_level or ""),
                symbology_identifier=str(barcode.symbology_identifier or ""),
                orientation=int(barcode.orientation),
                valid=bool(barcode.valid),
                error=None if error is None else str(error),
                position={
                    "top_left": _point(position.top_left),
                    "top_right": _point(position.top_right),
                    "bottom_right": _point(position.bottom_right),
                    "bottom_left": _point(position.bottom_left),
                },
                extra=_extra(barcode),
            )
        )
    return symbols


def _label(value) -> str:
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    text = str(value)
    if "." in text:
        return text.rsplit(".", 1)[-1]
    return text


def _point(point) -> dict[str, int]:
    return {"x": int(point.x), "y": int(point.y)}


def _scale_position(position: dict[str, dict[str, int]], factor: float) -> dict[str, dict[str, int]]:
    return {
        name: {"x": int(round(point["x"] * factor)), "y": int(round(point["y"] * factor))}
        for name, point in position.items()
    }


def _extra(barcode) -> dict | None:
    extra = barcode.extra
    if not extra:
        return None
    if isinstance(extra, dict):
        return extra
    return {"value": str(extra)}
