import sys

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from pdf417_tool.cli import main

        raise SystemExit(main())
    from pdf417_tool.ui import main

    main()
