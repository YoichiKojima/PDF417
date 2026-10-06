# PDF417 Barcode

Upload a PDF417 image, edit the payload, and generate a new barcode. The window reads the symbol’s text, rows, columns, and error correction, then draws a replacement from the content you keep or change.

## Setup

```bash
python -m venv .venv
```

Windows PowerShell:

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Windows Git Bash:

```bash
source .venv/Scripts/activate
pip install -r requirements.txt
```

## Window

Open the app with either of these:

```bash
python pdf417.py
```

```bash
python pdf417.pyw
```

1. Click **Upload Your Bar code** and choose an image. Clicking the empty left preview does the same thing. You can also drop an image from Explorer onto the window, or click **Screenshot**: the window hides, and you drag a box around a barcode anywhere on screen. A single click uses the whole screen, and Esc cancels.
2. The original appears on the left while the progress bar runs. The decoded payload fills **Content**, with the measured rows, columns, and error correction above it. A soft or blurry photo is sharpened and enlarged automatically when the first read misses. If the bars have smeared into each other, the scan can still fail.
3. Edit the content if you want, then click **Generate**. The new barcode appears on the right. Clicking the empty right preview moves the cursor into **Content**.
4. Click **Save**. A PNG keeps the bars and makes the background transparent. JPEG and BMP are saved as normal images.

Changing the content after a generate requires another **Generate** before **Save**.

### Previews

The left preview is the uploaded barcode. The right preview is the generated barcode. The three buttons between them apply to both sides.

| Control | Action |
| --- | --- |
| Plus | Zoom in |
| Center icon | Fit both previews |
| Minus | Zoom out |
| Refresh | Scan the original again and restore its content and layout |
| Mouse wheel | Zoom both previews around the pointer |
| Drag | Move both previews together |
| Space + drag | Move only the preview under the pointer |
| Double-click | Fit both previews. If they are already fitted, zoom to 200% |

Space still types a space in **Content**. It does not press the focused button.

## One executable

```bash
python build.py
```

This writes `build/PDF417.exe`, a single windowed file. Run that file to open the same window without Python.

## Command line

Passing a command to `python pdf417.py` uses the command line instead of the window.

Scan an image and print the payload, raw bytes, position, and error correction:

```bash
python pdf417.py scan barcode.png
python pdf417.py scan barcode.png --json decoded.json --save-text payload.txt
```

Create a barcode from text or a file:

```bash
python pdf417.py generate --text "Hello PDF417" -o out.png
python pdf417.py generate --text-file payload.txt -o out.png --security 5 --columns 6
```

Scan an image, change the payload, and write a new image:

```bash
python pdf417.py rebuild barcode.png -o changed.png --replace "OLD" "NEW"
python pdf417.py rebuild barcode.png -o changed.png --text "entirely new payload"
```

`--security` is the error-correction level from 0 to 8. `--columns` is optional, from 1 to 30, and `--rows` is optional, from 3 to 90. Leave them off and the encoder chooses a size that fits. An output path ending in `.svg` writes SVG instead of an image.
