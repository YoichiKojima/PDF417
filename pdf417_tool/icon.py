"""App icon drawn with Pillow, shared by the window, the tray, and the exe."""

from __future__ import annotations

from PIL import Image, ImageDraw

# Bar widths (in units) loosely following a PDF417 start pattern and a few codewords.
_BARS = (3, 1, 1, 1, 1, 1, 1, 2, 2, 1, 1, 2, 1, 1, 2, 2, 1, 3)


def app_icon(size: int = 64) -> Image.Image:
    scale = 4
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, big - 1, big - 1), radius=big // 6, fill="#1d4e89")
    margin = big // 7
    draw.rectangle((margin, margin * 2, big - margin, big - margin * 2), fill="#ffffff")

    inner_left, inner_right = margin * 1.5, big - margin * 1.5
    unit = (inner_right - inner_left) / sum(_BARS)
    x = inner_left
    for index, width in enumerate(_BARS):
        if index % 2 == 0:
            draw.rectangle((x, margin * 2.4, x + width * unit - 1, big - margin * 2.4), fill="#111827")
        x += width * unit
    return image.resize((size, size), Image.Resampling.LANCZOS)


def save_ico(path) -> None:
    app_icon(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
