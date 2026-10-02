# PDF417 scan and generate

Read a PDF417 barcode from an image, print the full payload, then write a new barcode after you change that content.

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

Double-click `pdf417.pyw`, or run:

```bash
python pdf417.py
```

1. Click **Upload Your Bar code** and choose an image.
2. The image appears while a progress bar runs, then the barcode text fills the content box.
3. Edit the text if you want, then click **Generate**. The new barcode appears on the right.
4. Click **Save** and choose where to store the image.

Command-line scan and generate still work when you pass a command, for example `python pdf417.py scan barcode.png`.

Scan an image and print the text, raw bytes, position, error-correction level, and any extra PDF417 metadata:

```bash
python pdf417.py scan barcode.png
python pdf417.py scan barcode.png --json decoded.json --save-text payload.txt
```

Generate a barcode from text you type or from a file you edited:

```bash
python pdf417.py generate --text "Hello PDF417" -o out.png
python pdf417.py generate --text-file payload.txt -o out.png --security 5 --columns 6
```

Scan, change part of the text, and write a new image. The new image is scanned again to confirm the payload.

```bash
python pdf417.py rebuild barcode.png -o changed.png --replace "OLD" "NEW"
python pdf417.py rebuild barcode.png -o changed.png --text "entirely new payload"
```

`--security` is the PDF417 error-correction level from 0 to 8. Higher levels make a larger symbol that survives more damage. `--columns` is optional (1-30). Leave it off and the encoder picks a size that fits the data.

SVG output works the same way when the output path ends in `.svg`.
