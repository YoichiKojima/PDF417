"""Build a single PDF417.exe with PyInstaller."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        "PDF417",
        "--distpath",
        str(ROOT / "build"),
        "--collect-all",
        "zxingcpp",
        "--collect-all",
        "tkinterdnd2",
        "--hidden-import",
        "json",
        "--hidden-import",
        "PIL._tkinter_finder",
        str(ROOT / "pdf417.pyw"),
    ]
    raise SystemExit(subprocess.call(command, cwd=ROOT))


if __name__ == "__main__":
    main()
