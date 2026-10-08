"""Generate a PDF417 barcode image from text or bytes."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

import zxingcpp


@dataclass
class GenerateOptions:
    columns: int | None = None
    rows: int | None = None
    ec_level: int | None = 2
    scale: int = 3
    quiet_zone: bool = True
    eci: str | None = "UTF-8"
    shortened: bool = False
    encoded: str | bytes | None = None


PDF417_MAX_CODEWORDS = 928
PDF417_MAX_COLUMNS = 30


def legal_symbol_size(
    columns: int | None,
    rows: int | None,
) -> tuple[int | None, int | None]:
    """Keep a requested PDF417 size inside the 928 codeword limit."""
    if not columns or not rows or columns * rows <= PDF417_MAX_CODEWORDS:
        return columns, rows
    fitted_rows = PDF417_MAX_CODEWORDS // columns
    if fitted_rows < 3:
        return columns, None
    return columns, fitted_rows


def generate_image(content: str | bytes, options: GenerateOptions | None = None) -> Image.Image:
    """Encode content as PDF417 and return a grayscale Pillow image."""
    opts = options or GenerateOptions()
    _validate(opts)
    barcode = zxingcpp.create_barcode(
        content,
        zxingcpp.BarcodeFormat.PDF417,
        **_creator_kwargs(content, opts),
    )
    bitmap = barcode.to_image(scale=opts.scale, add_quiet_zones=opts.quiet_zone)
    return _bitmap_to_image(bitmap)


def generate_fitted(
    content: str | bytes,
    *,
    columns: int | None,
    rows: int | None,
    ec_level: int | None,
    scale: int = 4,
    eci: str | None = "UTF-8",
) -> tuple[Image.Image, GenerateOptions]:
    """Draw a PDF417 using the scanned layout, relaxing the size only if the data will not fit.

    A payload past the 928-codeword limit is drawn at the widest symbol and the
    lowest error correction that can hold it. If even that is too small, the
    payload is cut to the longest prefix that fits.
    """
    level = 2 if ec_level is None else ec_level
    columns, rows = legal_symbol_size(columns, rows)
    plans: list[GenerateOptions] = []
    if columns and rows:
        plans.append(GenerateOptions(columns=columns, rows=rows, ec_level=level, scale=scale, eci=eci))
    if columns:
        plans.append(GenerateOptions(columns=columns, ec_level=level, scale=scale, eci=eci))
    plans.append(GenerateOptions(ec_level=level, scale=scale, eci=eci))
    if columns != PDF417_MAX_COLUMNS:
        plans.append(GenerateOptions(columns=PDF417_MAX_COLUMNS, ec_level=level, scale=scale, eci=eci))
    for lower in range(level - 1, -1, -1):
        plans.append(GenerateOptions(columns=PDF417_MAX_COLUMNS, ec_level=lower, scale=scale, eci=eci))
    last_error: Exception | None = None
    saw_capacity = False
    for options in plans:
        try:
            return generate_image(content, options), options
        except Exception as exc:
            last_error = exc
            saw_capacity = saw_capacity or _is_capacity_error(exc)
    if saw_capacity:
        fitted = _fit_maximum(content, scale=scale, eci=eci)
        if fitted is not None:
            return fitted
    raise last_error or RuntimeError("Could not generate a barcode")


def generate_to_path(
    content: str | bytes,
    output: str | Path,
    options: GenerateOptions | None = None,
) -> Path:
    destination = Path(output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.suffix.lower() == ".svg":
        opts = options or GenerateOptions()
        _validate(opts)
        barcode = zxingcpp.create_barcode(
            content,
            zxingcpp.BarcodeFormat.PDF417,
            **_creator_kwargs(content, opts),
        )
        destination.write_text(
            barcode.to_svg(scale=opts.scale, add_quiet_zones=opts.quiet_zone),
            encoding="utf-8",
        )
        return destination

    image = generate_image(content, options)
    if destination.suffix.lower() == ".png":
        image = with_transparent_background(image)
    image.save(destination)
    return destination


def _is_capacity_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "too long" in text or "too many codewords" in text


def _fit_maximum(
    content: str | bytes,
    *,
    scale: int,
    eci: str | None,
) -> tuple[Image.Image, GenerateOptions] | None:
    """Longest prefix that fits in the largest PDF417, at the lowest error correction."""
    if not content:
        return None
    stored_eci = eci if isinstance(content, str) else None
    probe = GenerateOptions(columns=PDF417_MAX_COLUMNS, ec_level=0, scale=1, eci=stored_eci)

    def fits(length: int) -> bool:
        if length <= 0:
            return False
        try:
            generate_image(content[:length], probe)
        except Exception as exc:
            if _is_capacity_error(exc):
                return False
            raise
        return True

    if not fits(1):
        return None
    low, high = 1, len(content)
    while low < high:
        mid = (low + high + 1) // 2
        if fits(mid):
            low = mid
        else:
            high = mid - 1
    encoded = content[:low]
    options = GenerateOptions(
        columns=PDF417_MAX_COLUMNS,
        ec_level=0,
        scale=scale,
        eci=stored_eci,
        shortened=len(encoded) < len(content),
        encoded=encoded,
    )
    return generate_image(encoded, options), options


def _creator_kwargs(content: str | bytes, options: GenerateOptions) -> dict:
    kwargs: dict = {}
    if options.columns is not None:
        kwargs["columns"] = options.columns
    if options.rows is not None:
        kwargs["rows"] = options.rows
    if options.ec_level is not None:
        kwargs["ec_level"] = str(options.ec_level)
    if isinstance(content, str) and options.eci:
        # Store text as UTF-8 so a later scan returns the same characters.
        kwargs["eci"] = options.eci
    return kwargs


def _validate(options: GenerateOptions) -> None:
    if options.columns is not None and not 1 <= options.columns <= 30:
        raise ValueError("columns must be from 1 to 30")
    if options.rows is not None and not 3 <= options.rows <= 90:
        raise ValueError("rows must be from 3 to 90")
    if options.ec_level is not None and not 0 <= options.ec_level <= 8:
        raise ValueError("ec_level (error correction) must be from 0 to 8")
    if options.scale < 1:
        raise ValueError("scale must be at least 1")


def with_transparent_background(image: Image.Image) -> Image.Image:
    """Keep the black bars and make the white background transparent."""
    gray = image.convert("L")
    alpha = gray.point(lambda value: 0 if value >= 128 else 255)
    transparent = Image.new("RGBA", gray.size, (0, 0, 0, 0))
    transparent.paste(Image.new("RGBA", gray.size, (0, 0, 0, 255)), mask=alpha)
    return transparent


def _bitmap_to_image(bitmap) -> Image.Image:
    height, width = bitmap.shape
    try:
        return Image.fromarray(bitmap).convert("L")
    except Exception:
        raw = bytes(memoryview(bitmap).cast("B"))
        return Image.frombytes("L", (width, height), raw)
