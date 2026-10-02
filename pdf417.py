"""Open the PDF417 window, or run a command when arguments are given."""

import sys


def main() -> None:
    if len(sys.argv) > 1:
        from pdf417_tool.cli import main as cli_main

        raise SystemExit(cli_main())
    from pdf417_tool.ui import main as ui_main

    ui_main()


if __name__ == "__main__":
    main()
