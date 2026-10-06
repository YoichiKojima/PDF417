"""Build a single PDF417.exe with PyInstaller."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from pdf417_tool.icon import save_ico

    icon_path = ROOT / "build" / "PDF417.ico"
    icon_path.parent.mkdir(exist_ok=True)
    save_ico(icon_path)

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
        "--icon",
        str(icon_path),
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
        # pystray picks its platform backend at run time.
        "--hidden-import",
        "pystray._win32",
        str(ROOT / "pdf417.pyw"),
    ]
    raise SystemExit(subprocess.call(command, cwd=ROOT))


if __name__ == "__main__":
    main()
