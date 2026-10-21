"""Read PDF417 symbols from an image."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageMath, ImageOps, ImageStat

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


def read_upload(image: Image.Image) -> tuple[DecodeResult, list[Pdf417Symbol]]:
    """Read a PDF417 symbol, or a QR code / ordinary barcode when that is what was uploaded.

    A QR code or linear barcode on the original image is returned immediately.
    PDF417 still wins when the first pass can read it, including tilted photos
    that need the longer search.
    """

    rgb = image.convert("RGB")
    try:
        pdf417_ready = bool(_read(rgb, pdf417_only=True))
    except Exception:
        pdf417_ready = False
    if pdf417_ready:
        return decode_image(image), []
    common = read_common_codes(rgb)
    if common:
        return DecodeResult(image_size=rgb.size), common
    result = decode_image(image)
    if result.symbols:
        return result, []
    return result, read_common_codes(image, thorough=True)


def common_code_name(fmt: str) -> str:
    """Short label for a QR code or linear barcode format."""

    names = {
        "QRCode": "QR Code",
        "MicroQRCode": "Micro QR Code",
        "RMQRCode": "rMQR Code",
        "Code128": "Code 128",
        "Code39": "Code 39",
        "Code39Ext": "Code 39",
        "Code39Std": "Code 39",
        "Code93": "Code 93",
        "Code32": "Code 32",
        "Codabar": "Codabar",
        "EAN13": "EAN-13",
        "EAN8": "EAN-8",
        "EAN2": "EAN-2",
        "EAN5": "EAN-5",
        "UPCA": "UPC-A",
        "UPCE": "UPC-E",
        "ITF": "ITF",
        "ITF14": "ITF-14",
        "ISBN": "ISBN",
        "PZN": "PZN",
        "Telepen": "Telepen",
        "TelepenAlpha": "Telepen",
        "TelepenNumeric": "Telepen",
        "DataBar": "GS1 DataBar",
        "DataBarOmni": "GS1 DataBar",
        "DataBarLtd": "GS1 DataBar Limited",
        "DataBarExp": "GS1 DataBar Expanded",
        "DataBarStk": "GS1 DataBar Stacked",
        "DataBarStkOmni": "GS1 DataBar Stacked",
        "DataBarExpStk": "GS1 DataBar Expanded Stacked",
    }
    return names.get(fmt, fmt)


def common_code_text(symbol: Pdf417Symbol) -> str:
    """Text to show and copy for a non-PDF417 symbol."""

    if symbol.text:
        return symbol.text
    if not symbol.raw_bytes:
        return ""
    try:
        return symbol.raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return symbol.raw_bytes.hex()


_COMMON_FORMATS = (
    zxingcpp.BarcodeFormat.QRCode,
    zxingcpp.BarcodeFormat.MicroQRCode,
    zxingcpp.BarcodeFormat.RMQRCode,
    zxingcpp.BarcodeFormat.AllLinear,
)


def read_common_codes(image: Image.Image, *, thorough: bool = False) -> list[Pdf417Symbol]:
    """Read QR and ordinary linear barcodes, without the tilted-PDF417 search."""

    rgb = image.convert("RGB")
    if thorough:
        gray = ImageOps.grayscale(rgb)
        stretched = ImageOps.autocontrast(gray)
        candidates = [gray, stretched, ImageOps.invert(stretched)]
    else:
        candidates = [rgb]
        if max(rgb.size) < _SMALL_SIDE:
            gray = ImageOps.grayscale(rgb)
            for factor in (2, 3):
                if max(gray.size) * factor > _UPSCALE_MAX_SIDE:
                    break
                candidates.append(
                    gray.resize((gray.width * factor, gray.height * factor), Image.Resampling.LANCZOS)
                )
    return _first_common(candidates)


def _first_common(candidates: list[Image.Image]) -> list[Pdf417Symbol]:
    seen: set[tuple[str, bytes, str]] = set()
    for candidate in candidates:
        try:
            symbols = _read(candidate, pdf417_only=False, formats=_COMMON_FORMATS)
        except Exception:
            continue
        found: list[Pdf417Symbol] = []
        for symbol in symbols:
            if "PDF417" in symbol.format.upper() or not symbol.valid:
                continue
            if not symbol.text and not symbol.raw_bytes:
                continue
            key = (symbol.format, symbol.raw_bytes, symbol.text)
            if key in seen:
                continue
            seen.add(key)
            symbol.format = common_code_name(symbol.format)
            found.append(symbol)
        if found:
            return found
    return []


def decode_image(image: Image.Image, *, pdf417_only: bool = True) -> DecodeResult:
    """Decode PDF417 symbols, trying a few image variants when the first pass misses."""
    last_error: Exception | None = None
    for candidate, to_original, options in _image_variants(image):
        try:
            symbols = _read(candidate, pdf417_only=pdf417_only, **options)
        except Exception as exc:  # library can raise on unusual buffers
            last_error = exc
            continue
        if symbols:
            for symbol in symbols:
                symbol.position = {name: to_original(point) for name, point in symbol.position.items()}
                source, position = _level_view(image, symbol)
                measured = measure_layout(
                    source,
                    position,
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


# zxing's PDF417 detector only accepts symbols within about 2 degrees of level
# (try_rotate covers the 90 degree turns). Tilted images are straightened at
# the measured tilt first, then swept in steps small enough that one of them
# always lands inside that window.
_TILT_STEP = 3
_TILT_SWEEP = [angle for angle in range(-45 + _TILT_STEP, 46, _TILT_STEP) if angle]
_SWEEP_MAX_SIDE = 2000
# Modules about a pixel wide (small screenshots, distant photos) misread unless
# the image is enlarged smoothly and thresholded as a whole.
_SMALL_SIDE = 1200
_UPSCALE_MAX_SIDE = 3600

_THOROUGH: dict = {}
_QUICK = {"thorough": False}
_GLOBAL = {"binarizer": zxingcpp.Binarizer.GlobalHistogram}


def _image_variants(image: Image.Image):
    """Yield (image, map back to original coordinates, _read options) in the order to try."""
    rgb = image.convert("RGB")
    yield rgb, _unchanged, _THOROUGH
    gray = ImageOps.grayscale(rgb)
    yield gray, _unchanged, _THOROUGH
    stretched = ImageOps.autocontrast(gray)
    yield stretched, _unchanged, _THOROUGH
    yield gray, _unchanged, _GLOBAL
    yield from _upscaled(gray, (0, 0))
    yield ImageOps.invert(stretched), _unchanged, _THOROUGH

    tilt, region = _analyse_edges(stretched)
    # (source image, its top-left in the original, rotation that levels it)
    targets: list[tuple[Image.Image, tuple[int, int], float]] = []
    if region is not None:
        # A busy background around a small symbol can hide it from the reader
        # even when it is level, so the edge-dense area is also tried alone.
        crop = stretched.crop(region)
        yield crop, _shifted(region[:2]), _THOROUGH
        yield from _upscaled(gray.crop(region), region[:2])
        # The crop holds mostly the symbol, so its own tilt is the sharper estimate.
        targets.append((crop, region[:2], -_analyse_edges(crop)[0]))
    targets.append((stretched, (0, 0), -tilt))

    for source, offset, correction in targets:
        if abs(correction) > _TILT_STEP / 2:
            base, scale = _enlarged(source)
            levelled, to_original = _rotated(base, scale, offset, correction, Image.Resampling.BICUBIC)
            yield levelled, to_original, _THOROUGH
            yield levelled, to_original, _GLOBAL
            # The reader's own inversion pass does not find light-on-dark PDF417.
            yield ImageOps.invert(levelled), to_original, _QUICK

    # Wide symbols tolerate less leftover tilt, so try close to the estimate first.
    source, offset, correction = targets[0]
    base, scale = _enlarged(source)
    for nudge in (1, -1, 2, -2):
        yield *_rotated(base, scale, offset, correction + nudge, Image.Resampling.BICUBIC), _QUICK

    if max(base.size) > _SWEEP_MAX_SIDE:
        factor = _SWEEP_MAX_SIDE / max(base.size)
        size = (max(1, round(base.width * factor)), max(1, round(base.height * factor)))
        base, scale = base.resize(size, Image.Resampling.LANCZOS), scale * factor
    for angle in sorted(_TILT_SWEEP, key=lambda angle: _angle_gap(angle, correction)):
        yield *_rotated(base, scale, offset, angle, Image.Resampling.BILINEAR), _QUICK


def _upscaled(gray: Image.Image, offset: tuple[int, int]):
    if max(gray.size) >= _SMALL_SIDE:
        return
    for factor in (2, 3):
        if max(gray.size) * factor > _UPSCALE_MAX_SIDE:
            break
        enlarged = gray.resize((gray.width * factor, gray.height * factor), Image.Resampling.LANCZOS)
        to_original = _scaled(factor, offset)
        yield enlarged, to_original, _GLOBAL
        yield enlarged, to_original, _THOROUGH


def _enlarged(image: Image.Image) -> tuple[Image.Image, float]:
    # Rotation resampling smears modules that are only a pixel or two wide.
    if max(image.size) < 1000:
        return image.resize((image.width * 2, image.height * 2), Image.Resampling.BICUBIC), 2.0
    return image, 1.0


def _rotated(base: Image.Image, scale: float, offset: tuple[int, int], angle: float, resample):
    background = int(ImageStat.Stat(base).median[0])
    rotated = base.rotate(angle, resample=resample, expand=True, fillcolor=background)
    return rotated, _unrotated(angle, base.size, rotated.size, scale, offset)


def _level_view(image: Image.Image, symbol: Pdf417Symbol) -> tuple[Image.Image, dict[str, dict[str, int]]]:
    """Image and corners to measure the layout on.

    Corners the reader reports for a tilted symbol are a few pixels off, enough
    to skew the module width, so a tilted symbol is cut out, levelled, and read
    again to get exact corners.
    """
    corners = symbol.position
    tilt = _corner_angle(corners)
    if abs(tilt) <= 0.5:
        return image, corners
    quad = [(corners[name]["x"], corners[name]["y"]) for name in ("top_left", "top_right", "bottom_right", "bottom_left")]
    across = (quad[1][0] - quad[0][0], quad[1][1] - quad[0][1])
    down = (quad[3][0] - quad[0][0], quad[3][1] - quad[0][1])
    pad = 0.03 * math.hypot(*across) + 4
    unit_across = _unit(across)
    unit_down = _unit(down)
    padded = [
        (x + sx * pad * unit_across[0] + sy * pad * unit_down[0], y + sx * pad * unit_across[1] + sy * pad * unit_down[1])
        for (x, y), (sx, sy) in zip(quad, ((-1, -1), (1, -1), (1, 1), (-1, 1)))
    ]
    xs = [x for x, _y in padded]
    ys = [y for _x, y in padded]
    box = (
        max(0, int(min(xs)) - 2),
        max(0, int(min(ys)) - 2),
        min(image.width, int(max(xs)) + 3),
        min(image.height, int(max(ys)) + 3),
    )
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        return image, corners

    # Clutter around the symbol can stop the second read, so everything past
    # its quiet zone is painted with the quiet zone's own shade.
    gray = ImageOps.grayscale(image.crop(box))

    def local(points):
        return [(x - box[0], y - box[1]) for x, y in points]

    outer = Image.new("L", gray.size, 0)
    ImageDraw.Draw(outer).polygon(local(padded), fill=255)
    inner = Image.new("L", gray.size, 0)
    ImageDraw.Draw(inner).polygon(local(quad), fill=255)
    ring = ImageChops.subtract(outer, inner)
    shade = int(ImageStat.Stat(gray, ring).median[0]) if ring.getbbox() else 255
    cleaned = Image.new("L", gray.size, shade)
    cleaned.paste(gray, mask=outer)

    base, scale = _enlarged(cleaned)
    level, _to_original = _rotated(base, scale, box[:2], tilt, Image.Resampling.BICUBIC)
    try:
        again = _read(level, pdf417_only=True, thorough=False)
        if not any(found.raw_bytes == symbol.raw_bytes for found in again):
            again = _read(level, pdf417_only=True, thorough=False, binarizer=zxingcpp.Binarizer.GlobalHistogram)
    except Exception:
        return image, corners
    for found in again:
        if found.raw_bytes == symbol.raw_bytes:
            return level, found.position
    return image, corners


def _unit(vector: tuple[float, float]) -> tuple[float, float]:
    length = math.hypot(*vector) or 1.0
    return vector[0] / length, vector[1] / length


def _corner_angle(corners: dict[str, dict[str, int]]) -> float:
    """Rotation (Image.rotate's sense) that turns the symbol upright and horizontal."""
    tl, tr, br, bl = (corners[name] for name in ("top_left", "top_right", "bottom_right", "bottom_left"))
    dx = (tr["x"] - tl["x"]) + (br["x"] - bl["x"])
    dy = (tr["y"] - tl["y"]) + (br["y"] - bl["y"])
    return math.degrees(math.atan2(dy, dx))


def _angle_gap(a: float, b: float) -> float:
    gap = abs(a - b) % 90
    return min(gap, 90 - gap)


_EDGE_CELL = 16
_REGION_THRESHOLD = 0.25
_REGION_MAX_AREA = 0.6


def _analyse_edges(gray: Image.Image) -> tuple[float, tuple[int, int, int, int] | None]:
    """Return the image's tilt and the box around its densest aligned edges.

    Bars, row boundaries, and card edges all run parallel or perpendicular to
    the symbol, so the gradient orientation folded to 90 degrees tracks its
    tilt. Angles are in degrees (-45..45) in Image.rotate's sense. The box is
    None when it would cover most of the image anyway.
    """
    small = gray.copy()
    small.thumbnail((1200, 1200))
    factor = small.width / gray.width
    small = small.filter(ImageFilter.GaussianBlur(1)).convert("F")
    w, h = small.size
    if w < 8 or h < 8:
        return 0.0, None
    gx = ImageMath.lambda_eval(lambda a: a["r"] - a["l"], r=small.crop((2, 1, w, h - 1)), l=small.crop((0, 1, w - 2, h - 1)))
    gy = ImageMath.lambda_eval(lambda a: a["d"] - a["u"], d=small.crop((1, 2, w - 1, h)), u=small.crop((1, 0, w - 1, h - 2)))
    # Gradient angle multiplied by four, weighted by edge strength.
    cos4 = ImageMath.lambda_eval(
        lambda a: ((a["x"] * a["x"] - a["y"] * a["y"]) ** 2 - 4 * a["x"] * a["x"] * a["y"] * a["y"])
        / (a["x"] * a["x"] + a["y"] * a["y"] + 1),
        x=gx,
        y=gy,
    )
    sin4 = ImageMath.lambda_eval(
        lambda a: 4 * a["x"] * a["y"] * (a["x"] * a["x"] - a["y"] * a["y"]) / (a["x"] * a["x"] + a["y"] * a["y"] + 1),
        x=gx,
        y=gy,
    )
    tilt = _tilt(_mean(sin4), _mean(cos4))

    grid_w, grid_h = max(1, cos4.width // _EDGE_CELL), max(1, cos4.height // _EDGE_CELL)
    if grid_w * grid_h < 4:
        return tilt, None
    cos_cells = list(cos4.resize((grid_w, grid_h), Image.Resampling.BOX).get_flattened_data())
    sin_cells = list(sin4.resize((grid_w, grid_h), Image.Resampling.BOX).get_flattened_data())
    strength = [math.hypot(c, s) for c, s in zip(cos_cells, sin_cells)]
    peak = max(range(len(strength)), key=strength.__getitem__)
    if strength[peak] <= 0:
        return tilt, None
    unit_c, unit_s = cos_cells[peak] / strength[peak], sin_cells[peak] / strength[peak]
    aligned = [c * unit_c + s * unit_s for c, s in zip(cos_cells, sin_cells)]
    limit = aligned[peak] * _REGION_THRESHOLD

    member = {peak}
    pending = [peak]
    while pending:
        index = pending.pop()
        x, y = index % grid_w, index // grid_w
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < grid_w and 0 <= ny < grid_h:
                neighbour = ny * grid_w + nx
                if neighbour not in member and aligned[neighbour] >= limit:
                    member.add(neighbour)
                    pending.append(neighbour)

    cell_w, cell_h = cos4.width / grid_w, cos4.height / grid_h
    xs = [i % grid_w for i in member]
    ys = [i // grid_w for i in member]
    left, right = min(xs) * cell_w, (max(xs) + 1) * cell_w
    top, bottom = min(ys) * cell_h, (max(ys) + 1) * cell_h
    pad_x = max(2 * cell_w, (right - left) * 0.15)
    pad_y = max(2 * cell_h, (bottom - top) * 0.15)
    box = (
        max(0, int((left + 1 - pad_x) / factor)),
        max(0, int((top + 1 - pad_y) / factor)),
        min(gray.width, int(math.ceil((right + 1 + pad_x) / factor))),
        min(gray.height, int(math.ceil((bottom + 1 + pad_y) / factor))),
    )
    if (box[2] - box[0]) * (box[3] - box[1]) > _REGION_MAX_AREA * gray.width * gray.height:
        return tilt, None
    return tilt, box


def _tilt(sin4: float, cos4: float) -> float:
    return -math.degrees(math.atan2(sin4, cos4)) / 4


def _mean(image: Image.Image) -> float:
    # ImageStat bins float images into a 256-step histogram; a box resize is exact.
    return image.resize((1, 1), Image.Resampling.BOX).getpixel((0, 0))


def _unchanged(point: dict[str, int]) -> dict[str, int]:
    return point


def _scaled(scale: float, offset: tuple[int, int] = (0, 0)):
    def to_original(point: dict[str, int]) -> dict[str, int]:
        return {"x": int(round(point["x"] / scale + offset[0])), "y": int(round(point["y"] / scale + offset[1]))}

    return to_original


def _shifted(offset: tuple[int, int]):
    def to_original(point: dict[str, int]) -> dict[str, int]:
        return {"x": point["x"] + offset[0], "y": point["y"] + offset[1]}

    return to_original


def _unrotated(
    angle: float,
    base_size: tuple[int, int],
    rotated_size: tuple[int, int],
    scale: float,
    offset: tuple[int, int],
):
    radians = math.radians(angle)
    cos, sin = math.cos(radians), math.sin(radians)
    base_cx, base_cy = base_size[0] / 2, base_size[1] / 2
    rotated_cx, rotated_cy = rotated_size[0] / 2, rotated_size[1] / 2

    def to_original(point: dict[str, int]) -> dict[str, int]:
        dx, dy = point["x"] - rotated_cx, point["y"] - rotated_cy
        x = base_cx + dx * cos - dy * sin
        y = base_cy + dx * sin + dy * cos
        return {"x": int(round(x / scale + offset[0])), "y": int(round(y / scale + offset[1]))}

    return to_original


def _read(
    image: Image.Image,
    *,
    pdf417_only: bool,
    thorough: bool = True,
    binarizer=zxingcpp.Binarizer.LocalAverage,
    formats=None,
) -> list[Pdf417Symbol]:
    if formats is None:
        formats = (
            zxingcpp.BarcodeFormat.PDF417 | zxingcpp.BarcodeFormat.CompactPDF417 | zxingcpp.BarcodeFormat.MicroPDF417
            if pdf417_only
            else zxingcpp.BarcodeFormats()
        )
    found = zxingcpp.read_barcodes(
        image,
        formats=formats,
        try_rotate=True,
        try_downscale=True,
        try_invert=thorough,
        binarizer=binarizer,
        text_mode=zxingcpp.TextMode.HRI,
    )
    symbols: list[Pdf417Symbol] = []
    for index, barcode in enumerate(found):
        if pdf417_only and "PDF417" not in str(barcode.format):
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


def _extra(barcode) -> dict | None:
    # The packaged app used to abort the whole scan here. Optional metadata is
    # parsed with the standard json module, which a one-file build can omit.
    try:
        extra = barcode.extra
    except Exception:
        return None
    if not extra:
        return None
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            return {"value": extra}
    if isinstance(extra, dict):
        return extra
    return {"value": str(extra)}
