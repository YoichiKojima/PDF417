"""Open the PDF417 window, or run a command when arguments are given."""

import sys


def main() -> None:
    from pdf417_tool.startup import TRAY_ARGUMENT

    # The sign-in entry passes --tray so the window stays closed. Any other
    # argument is still a command-line request.
    if len(sys.argv) > 1 and sys.argv[1:] != [TRAY_ARGUMENT]:
        from pdf417_tool.cli import main as cli_main

        raise SystemExit(cli_main())
    from pdf417_tool.ui import main as ui_main

    ui_main()


if __name__ == "__main__":
    main()
