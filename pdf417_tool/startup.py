"""Launch this app when the current user signs in to Windows."""

from __future__ import annotations

import sys
from pathlib import Path

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "PDF417"
TRAY_ARGUMENT = "--tray"


def supported() -> bool:
    return sys.platform == "win32"


def enabled() -> bool:
    """True when Windows is set to open this app at sign-in.

    Nothing is written until the user turns it on, so a new install stays off.
    """

    if not supported():
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, _RUN_VALUE)
    except OSError:
        return False
    return kind == winreg.REG_SZ and bool(str(value).strip())


def command() -> str:
    """Command line Windows should run at sign-in for this process."""

    if getattr(sys, "frozen", False):
        return f"{_quote(Path(sys.executable))} {TRAY_ARGUMENT}"
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        windowed = executable.with_name("pythonw.exe")
        if windowed.is_file():
            executable = windowed
    script = Path(sys.argv[0]).resolve()
    return f"{_quote(executable)} {_quote(script)} {TRAY_ARGUMENT}"


def hidden_on_launch(argv: list[str] | None = None) -> bool:
    """True when this process was started by the Windows sign-in entry."""

    args = sys.argv[1:] if argv is None else argv
    return TRAY_ARGUMENT in args


def use_existing_hidden_launch() -> None:
    """Keep an already enabled sign-in entry from opening the window.

    Entries saved before tray startup stay as they are until the app runs
    again. This adds the tray flag without changing the program path.
    """

    if not supported():
        return
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _RUN_KEY,
            0,
            winreg.KEY_READ | winreg.KEY_SET_VALUE,
        ) as key:
            try:
                value, kind = winreg.QueryValueEx(key, _RUN_VALUE)
            except FileNotFoundError:
                return
            if kind != winreg.REG_SZ:
                return
            text = str(value).strip()
            if not text or TRAY_ARGUMENT in text.split():
                return
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, f"{text} {TRAY_ARGUMENT}")
    except OSError:
        return


def set_enabled(turn_on: bool) -> None:
    if not supported():
        raise OSError("Start with Windows is only available on Windows.")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if turn_on:
            winreg.SetValueEx(key, _RUN_VALUE, 0, winreg.REG_SZ, command())
            return
        try:
            winreg.DeleteValue(key, _RUN_VALUE)
        except FileNotFoundError:
            pass


def _quote(path: Path) -> str:
    return '"' + str(path).replace('"', "") + '"'
