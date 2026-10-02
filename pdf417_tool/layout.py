"""Read PDF417 rows, columns, and error-correction level from a scanned symbol."""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from PIL import Image


@dataclass
class Pdf417Layout:
    rows: int | None = None
    columns: int | None = None
    security_level: int | None = None
    ec_codewords: int | None = None
    ec_percent: str = ""

    def summary(self) -> str:
        parts: list[str] = []
        parts.append(f"Rows: {self.rows}" if self.rows else "Rows: unknown")
        parts.append(f"Columns: {self.columns}" if self.columns else "Columns: unknown")
        if self.security_level is None:
            parts.append(f"Error correction: {self.ec_percent or 'unknown'}")
        else:
            words = f"{self.ec_codewords} codewords" if self.ec_codewords else "codewords unknown"
            percent = f", {self.ec_percent}" if self.ec_percent else ""
            parts.append(f"Error correction: level {self.security_level} ({words}{percent})")
        return "    ".join(parts)


def measure_layout(
    image: Image.Image,
    position: dict[str, dict[str, int]],
    ec_percent: str = "",
    *,
    compact: bool = False,
) -> Pdf417Layout:
    layout = Pdf417Layout(ec_percent=(ec_percent or "").strip())
    try:
        symbol = _isolate_symbol(image, position)
    except Exception:
        return layout
    module, width, height, _dark_is_bar, _threshold = _module_width(symbol)
    percent = _percent(layout.ec_percent)
    overhead = 35 if compact else 69
    if percent is not None and width > 0 and height > 0:
        matched = _match_reported_layout(width, height, percent, module, overhead)
        if matched is not None:
            layout.rows, layout.columns, layout.security_level = matched
            layout.ec_codewords = 2 << layout.security_level
            return layout
    if module is None:
        return layout
    columns = int(round((width / module - overhead) / 17))
    if 1 <= columns <= 30:
        layout.columns = columns
    layout.rows = _count_rows(symbol, module, _dark_is_bar, _threshold)
    if layout.rows and layout.columns and percent is not None:
        layout.security_level = _security_level(percent, layout.rows, layout.columns)
        if layout.security_level is not None:
            layout.ec_codewords = 2 << layout.security_level
    return layout


def _match_reported_layout(
    width: int,
    height: int,
    percent: int,
    module: float | None,
    overhead: int,
) -> tuple[int, int, int] | None:
    """Pick rows, columns, and error level that reproduce the reader's exact percentage and shape."""
    aspect = width / height
    column_hint = None
    if module and module > 0:
        column_hint = (width / module - overhead) / 17
    ranked: list[tuple[float, float, int, int, int]] = []
    for rows in range(3, 91):
        for columns in range(1, 31):
            total = rows * columns
            for level in range(9):
                codewords = 2 << level
                if codewords >= total or codewords * 100 // total != percent:
                    continue
                for ratio in (2, 3, 4, 5):
                    predicted = (17 * columns + overhead) / (rows * ratio)
                    aspect_error = abs(predicted - aspect) / aspect
                    hint_error = 0.0 if column_hint is None else abs(columns - column_hint)
                    ranked.append((aspect_error, hint_error, rows, columns, level))
    if not ranked:
        return None
    ranked.sort()
    best_aspect = ranked[0][0]
    if best_aspect > 0.45:
        return None
    close = [item for item in ranked if item[0] <= best_aspect + 0.03]
    close.sort(key=lambda item: (item[1], item[0]))
    _aspect_error, _hint_error, rows, columns, level = close[0]
    return rows, columns, level


def _isolate_symbol(image: Image.Image, position: dict[str, dict[str, int]]) -> Image.Image:
    names = ("top_left", "top_right", "bottom_right", "bottom_left")
    corners = [(float(position[name]["x"]), float(position[name]["y"])) for name in names]
    if _axis_aligned(corners):
        xs = [point[0] for point in corners]
        ys = [point[1] for point in corners]
        box = (
            int(max(0, min(xs))),
            int(max(0, min(ys))),
            int(min(image.width, max(xs) + 1)),
            int(min(image.height, max(ys) + 1)),
        )
        if box[2] - box[0] < 16 or box[3] - box[1] < 16:
            raise ValueError("Barcode region is too small")
        return image.crop(box).convert("L")
    return _warp(image.convert("RGB"), corners).convert("L")


def _module_width(symbol: Image.Image) -> tuple[float | None, int, int, bool, int]:
    width, height = symbol.size
    threshold = _otsu(symbol)
    pixels = symbol.load()
    for dark_is_bar in (True, False):
        samples: list[float] = []
        for frac in (0.25, 0.5, 0.75):
            y = min(height - 1, max(0, int(height * frac)))
            for runs in (
                _runs(pixels, width, y, threshold, dark_is_bar, from_left=True),
                _runs(pixels, width, y, threshold, dark_is_bar, from_left=False),
            ):
                module = _module_from_runs(runs)
                if module is not None:
                    samples.append(module)
        if samples:
            return median(samples), width, height, dark_is_bar, threshold
    return None, width, height, True, threshold


def _count_rows(symbol: Image.Image, module: float, dark_is_bar: bool, threshold: int) -> int | None:
    width, height = symbol.size
    pixels = symbol.load()
    x0 = int(round(18 * module))
    x1 = int(round(33 * module))
    step = max(1, int(round(module)))
    if x1 <= x0 or x0 >= width:
        x0, x1 = 0, min(width, max(step * 4, int(width * 0.2)))
    signatures: list[tuple[int, ...]] = []
    for y in range(height):
        bits: list[int] = []
        x = x0
        while x < min(width, x1):
            dark = pixels[min(x, width - 1), y] <= threshold
            bits.append(1 if dark == dark_is_bar else 0)
            x += step
        signatures.append(tuple(bits))
    if not signatures or not signatures[0]:
        return None

    ranked: list[tuple[float, float, int]] = []
    for ratio in (2, 3, 4, 5, 6):
        row_height = ratio * module
        rows = int(round(height / row_height))
        if not 3 <= rows <= 90:
            continue
        remainder = abs(height - rows * row_height) / height
        ranked.append((_inconsistency(signatures, rows), remainder, rows))
    if not ranked:
        return None
    ranked.sort()
    return ranked[0][2]


def _inconsistency(signatures: list[tuple[int, ...]], rows: int) -> float:
    height = len(signatures)
    errors = 0.0
    samples = 0
    for row in range(rows):
        y0 = int(round(row * height / rows + (height / rows) * 0.3))
        y1 = int(round(row * height / rows + (height / rows) * 0.7))
        y0 = min(height - 1, max(0, y0))
        y1 = min(height, max(y0 + 1, y1))
        reference = signatures[(y0 + y1 - 1) // 2]
        for y in range(y0, y1):
            current = signatures[y]
            errors += sum(bit_a != bit_b for bit_a, bit_b in zip(reference, current)) / len(reference)
            samples += 1
    return errors / max(samples, 1)


def _security_level(percent: int, rows: int, columns: int) -> int | None:
    total = rows * columns
    exact: list[int] = []
    closest: tuple[int, int] | None = None
    for level in range(9):
        codewords = 2 << level
        if codewords >= total:
            continue
        reported = codewords * 100 // total
        if reported == percent:
            exact.append(level)
        distance = abs(reported - percent)
        if closest is None or distance < closest[0]:
            closest = (distance, level)
    if len(exact) == 1:
        return exact[0]
    if exact:
        return exact[-1]
    return None if closest is None else closest[1]


def _percent(text: str) -> int | None:
    digits = "".join(char for char in text if char.isdigit())
    if not digits:
        return None
    value = int(digits)
    if 0 <= value <= 100:
        return value
    return None


def _runs(pixels, width: int, y: int, threshold: int, dark_is_bar: bool, *, from_left: bool) -> list[int]:
    xs = range(width) if from_left else range(width - 1, -1, -1)
    started = False
    current: bool | None = None
    length = 0
    runs: list[int] = []
    for x in xs:
        dark = pixels[x, y] <= threshold
        bar = dark if dark_is_bar else not dark
        if not started:
            if not bar:
                continue
            started = True
            current = True
            length = 1
            continue
        if bar == current:
            length += 1
            continue
        runs.append(length)
        if len(runs) == 8:
            return runs
        current = bar
        length = 1
    if started and len(runs) < 8:
        runs.append(length)
    return runs


def _module_from_runs(runs: list[int]) -> float | None:
    if len(runs) < 8:
        return None
    unit = sum(runs[1:7]) / 6
    if unit < 0.8:
        return None
    if not 6.2 <= runs[0] / unit <= 10.5:
        return None
    if not 2.0 <= runs[7] / unit <= 4.8:
        return None
    return runs[0] / 8


def _axis_aligned(corners: list[tuple[float, float]]) -> bool:
    top_left, top_right, bottom_right, bottom_left = corners
    width = max(_distance(top_left, top_right), 1.0)
    height = max(_distance(top_left, bottom_left), 1.0)
    return (
        abs(top_left[1] - top_right[1]) <= max(3.0, width * 0.02)
        and abs(bottom_left[1] - bottom_right[1]) <= max(3.0, width * 0.02)
        and abs(top_left[0] - bottom_left[0]) <= max(3.0, height * 0.02)
        and abs(top_right[0] - bottom_right[0]) <= max(3.0, height * 0.02)
    )


def _warp(image: Image.Image, corners: list[tuple[float, float]]) -> Image.Image:
    top_left, top_right, bottom_right, bottom_left = corners
    width = max(16, int(round(max(_distance(top_left, top_right), _distance(bottom_left, bottom_right)))))
    height = max(16, int(round(max(_distance(top_left, bottom_left), _distance(top_right, bottom_right)))))
    target = [(0, 0), (width, 0), (width, height), (0, height)]
    return image.transform(
        (width, height),
        Image.Transform.PERSPECTIVE,
        _perspective_coeffs(corners, target),
        Image.Resampling.BICUBIC,
    )


def _distance(a: tuple[float, float], b: tuple[float, float]) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _perspective_coeffs(source: list[tuple[float, float]], target: list[tuple[float, float]]) -> list[float]:
    matrix: list[list[float]] = []
    values: list[float] = []
    for (sx, sy), (tx, ty) in zip(source, target):
        matrix.append([tx, ty, 1, 0, 0, 0, -sx * tx, -sx * ty])
        matrix.append([0, 0, 0, tx, ty, 1, -sy * tx, -sy * ty])
        values.extend((sx, sy))
    return _solve(matrix, values)


def _solve(rows: list[list[float]], values: list[float]) -> list[float]:
    size = len(values)
    working = [row[:] + [values[index]] for index, row in enumerate(rows)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(working[row][col]))
        working[col], working[pivot] = working[pivot], working[col]
        scale = working[col][col]
        if abs(scale) < 1e-8:
            raise ValueError("Could not straighten the barcode")
        for index in range(col, size + 1):
            working[col][index] /= scale
        for row in range(size):
            if row == col:
                continue
            factor = working[row][col]
            for index in range(col, size + 1):
                working[row][index] -= factor * working[col][index]
    return [working[row][size] for row in range(size)]


def _otsu(image: Image.Image) -> int:
    histogram = [0] * 256
    for value in image.getdata():
        histogram[value] += 1
    total = sum(histogram)
    total_sum = sum(index * count for index, count in enumerate(histogram))
    background_sum = 0
    background_weight = 0
    best_variance = -1.0
    threshold = 128
    for level in range(256):
        background_weight += histogram[level]
        if background_weight == 0:
            continue
        foreground_weight = total - background_weight
        if foreground_weight == 0:
            break
        background_sum += level * histogram[level]
        background_mean = background_sum / background_weight
        foreground_mean = (total_sum - background_sum) / foreground_weight
        variance = background_weight * foreground_weight * (background_mean - foreground_mean) ** 2
        if variance > best_variance:
            best_variance = variance
            threshold = level
    return threshold
