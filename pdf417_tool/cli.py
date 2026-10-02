"""Command line for scanning a PDF417 image and generating a new one."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .decode import Pdf417Symbol, decode_path
from .encode import GenerateOptions, generate_to_path


def main(argv: list[str] | None = None) -> int:
    _configure_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "scan":
            return _cmd_scan(args)
        if args.command == "generate":
            return _cmd_generate(args)
        if args.command == "rebuild":
            return _cmd_rebuild(args)
        return _interactive()
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdf417",
        description="Scan a PDF417 barcode image, then generate a new symbol from edited content.",
    )
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="Read every PDF417 symbol in an image")
    scan.add_argument("image", help="Path to an image that contains a PDF417 barcode")
    scan.add_argument("--json", dest="json_path", help="Also write the decoded data as JSON")
    scan.add_argument("--save-text", help="Write the first symbol text to this file")
    scan.add_argument("--all-formats", action="store_true", help="Also report non-PDF417 barcodes")

    generate = sub.add_parser("generate", help="Create a PDF417 image from text or a file")
    content = generate.add_mutually_exclusive_group(required=True)
    content.add_argument("--text", help="Text to encode")
    content.add_argument("--text-file", help="UTF-8 text file to encode")
    content.add_argument("--bytes-file", help="Raw bytes file to encode")
    generate.add_argument("-o", "--output", required=True, help="Output image (.png, .jpg, .bmp) or .svg")
    _add_render_args(generate)

    rebuild = sub.add_parser("rebuild", help="Scan an image and write a new PDF417 with changed content")
    rebuild.add_argument("image", help="Source barcode image")
    rebuild.add_argument("-o", "--output", required=True, help="Output image path")
    rebuild.add_argument("--index", type=int, default=0, help="Which symbol to use when the image has several")
    rebuild.add_argument("--text", help="Replace the whole payload with this text")
    rebuild.add_argument("--text-file", help="Replace the whole payload with this UTF-8 file")
    rebuild.add_argument(
        "--replace",
        nargs=2,
        action="append",
        metavar=("OLD", "NEW"),
        help="Replace OLD with NEW inside the decoded text. Repeat for several changes.",
    )
    rebuild.add_argument("--binary", action="store_true", help="Re-encode the original raw bytes (no text edit)")
    _add_render_args(rebuild)
    return parser


def _add_render_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--columns", type=int, help="Data columns, 1-30. Omit to let the encoder choose.")
    parser.add_argument("--rows", type=int, help="Rows, 3-90. Omit to let the encoder choose.")
    parser.add_argument("--security", type=int, help="Error correction level, 0-8. Default is 2, or the scanned level.")
    parser.add_argument("--scale", type=int, default=3, help="Module size in pixels (default: 3)")


def _cmd_scan(args) -> int:
    result = decode_path(args.image, pdf417_only=not args.all_formats)
    if not result.symbols:
        raise RuntimeError(f"No PDF417 barcode found in {args.image}")
    for symbol in result.symbols:
        print(_format_symbol(symbol))
        print()
    if args.json_path:
        payload = {
            "source": result.source,
            "image_size": {"width": result.image_size[0], "height": result.image_size[1]},
            "symbols": [symbol.to_dict() for symbol in result.symbols],
        }
        Path(args.json_path).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Wrote {args.json_path}")
    if args.save_text:
        Path(args.save_text).write_text(result.symbols[0].text, encoding="utf-8")
        print(f"Wrote {args.save_text}")
    return 0


def _cmd_generate(args) -> int:
    content: str | bytes
    if args.text is not None:
        content = args.text
    elif args.text_file:
        content = Path(args.text_file).read_text(encoding="utf-8")
    else:
        content = Path(args.bytes_file).read_bytes()
    options = _options_from_args(args, fallback_security=2)
    path = generate_to_path(content, args.output, options)
    print(f"Wrote {path}")
    _confirm_roundtrip(path, content)
    return 0


def _cmd_rebuild(args) -> int:
    result = decode_path(args.image)
    symbol = _pick(result.symbols, args.index)
    print("Decoded source:")
    print(_format_symbol(symbol))
    print()
    content = _changed_content(symbol, args)
    security = args.security if args.security is not None else _security_or_default(symbol.ec_level)
    options = _options_from_args(args, fallback_security=security)
    path = generate_to_path(content, args.output, options)
    print(f"Wrote {path}")
    _confirm_roundtrip(path, content)
    return 0


def _interactive() -> int:
    print("PDF417 scan and generate")
    print("Enter a barcode image to read it, then write a new symbol from changed text.")
    image_path = input("Barcode image path: ").strip().strip('"')
    if not image_path:
        raise ValueError("An image path is required")
    result = decode_path(image_path)
    if not result.symbols:
        raise RuntimeError(f"No PDF417 barcode found in {image_path}")
    for symbol in result.symbols:
        print()
        print(_format_symbol(symbol))
    print()
    index = 0
    if len(result.symbols) > 1:
        index = int(input(f"Symbol index [0-{len(result.symbols) - 1}]: ").strip() or "0")
    symbol = _pick(result.symbols, index)
    print()
    print("How should the new barcode content be built?")
    print("  1  Replace the entire text")
    print("  2  Find and replace inside the decoded text")
    print("  3  Keep the decoded text and only redraw the barcode")
    choice = input("Choice [1]: ").strip() or "1"
    content: str | bytes
    if choice == "2":
        if _is_binary(symbol):
            raise ValueError(
                "This payload is binary, so find-and-replace would edit the display text. "
                "Choose 1 to type a new payload, or 3 to keep the original bytes."
            )
        content = symbol.text
        print("Enter replacements as OLD => NEW. Empty line finishes.")
        while True:
            line = input("replace: ").strip()
            if not line:
                break
            if "=>" not in line:
                print("Use the form OLD => NEW")
                continue
            old, new = (part.strip() for part in line.split("=>", 1))
            if not old:
                print("The text to find is empty.")
                continue
            if old not in content:
                print(f"Not found: {old}")
                continue
            content = content.replace(old, new, 1)
            print(f"Replaced {old!r} with {new!r}")
    elif choice == "3":
        content = symbol.raw_bytes if _is_binary(symbol) else symbol.text
    else:
        print("Paste the new text. Finish with a line that contains only END.")
        lines: list[str] = []
        while True:
            line = input()
            if line == "END":
                break
            lines.append(line)
        content = "\n".join(lines)
    output = input("Output image path [barcode.png]: ").strip().strip('"') or "barcode.png"
    security_text = input(f"Error correction 0-8 [{_security_or_default(symbol.ec_level)}]: ").strip()
    security = int(security_text) if security_text else _security_or_default(symbol.ec_level)
    path = generate_to_path(content, output, GenerateOptions(ec_level=security, scale=3))
    print(f"Wrote {path}")
    _confirm_roundtrip(path, content)
    return 0


def _changed_content(symbol: Pdf417Symbol, args) -> str | bytes:
    has_text_edit = bool(args.text_file or args.text is not None or args.replace)
    if args.binary:
        if has_text_edit:
            raise ValueError("--binary keeps the original bytes, so do not combine it with text changes")
        return symbol.raw_bytes
    if _is_binary(symbol) and not has_text_edit:
        print("Payload is binary, so the new barcode keeps the original bytes.")
        return symbol.raw_bytes
    if _is_binary(symbol) and args.replace and args.text is None and not args.text_file:
        raise ValueError(
            "This payload is binary. --replace would edit the display text, not the bytes. "
            "Pass --text or --text-file to replace the whole payload."
        )
    if args.text_file:
        text = Path(args.text_file).read_text(encoding="utf-8")
    elif args.text is not None:
        text = args.text
    else:
        text = symbol.text
    for old, new in args.replace or []:
        if old not in text:
            raise ValueError(f"Text to replace was not found: {old!r}")
        text = text.replace(old, new, 1)
    return text


def _is_binary(symbol: Pdf417Symbol) -> bool:
    return symbol.content_type.lower() in {"binary", "mixed", "unknowneci"}


def _pick(symbols: list[Pdf417Symbol], index: int) -> Pdf417Symbol:
    if not symbols:
        raise RuntimeError("No PDF417 barcode found")
    if index < 0 or index >= len(symbols):
        raise ValueError(f"Symbol index {index} is out of range (0-{len(symbols) - 1})")
    return symbols[index]


def _options_from_args(args, fallback_security: int) -> GenerateOptions:
    security = args.security if args.security is not None else fallback_security
    return GenerateOptions(
        columns=args.columns,
        rows=args.rows,
        ec_level=security,
        scale=args.scale,
    )


def _security_label(symbol: Pdf417Symbol) -> str:
    if symbol.security_level is None:
        return symbol.ec_level or "(not reported)"
    words = f", {symbol.ec_codewords} codewords" if symbol.ec_codewords else ""
    percent = f", reader reported {symbol.ec_level}" if symbol.ec_level else ""
    return f"level {symbol.security_level}{words}{percent}"


def _security_or_default(ec_level: str) -> int:
    text = (ec_level or "").strip()
    if text.isdigit() and 0 <= int(text) <= 8:
        return int(text)
    return 2


def _confirm_roundtrip(path: Path, content: str | bytes) -> None:
    if path.suffix.lower() == ".svg":
        return
    try:
        checked = decode_path(path)
    except Exception as exc:
        print(f"Warning: could not re-read the new barcode: {exc}")
        return
    if not checked.symbols:
        print("Warning: the new image was written, but a scan of that image found no PDF417 symbol.")
        return
    decoded = checked.symbols[0]
    expected = content if isinstance(content, str) else content.decode("latin-1", errors="replace")
    if isinstance(content, str) and decoded.text != content:
        print("Warning: the new barcode does not decode to the exact text that was encoded.")
    elif isinstance(content, bytes) and decoded.raw_bytes != content and decoded.text != expected:
        print("Warning: the new barcode does not decode to the exact bytes that were encoded.")
    else:
        print("Checked: scanning the new image returns the encoded content.")


def _format_symbol(symbol: Pdf417Symbol) -> str:
    lines = [
        f"PDF417 #{symbol.index}",
        f"  format:        {symbol.format}",
        f"  valid:         {symbol.valid}",
        f"  content type:  {symbol.content_type}",
        f"  rows:          {symbol.rows if symbol.rows else '(unknown)'}",
        f"  columns:       {symbol.columns if symbol.columns else '(unknown)'}",
        f"  error correction: {_security_label(symbol)}",
        f"  symbology id:  {symbol.symbology_identifier or '(none)'}",
        f"  orientation:   {symbol.orientation}",
        f"  position:      {_position_line(symbol.position)}",
        f"  byte length:   {len(symbol.raw_bytes)}",
    ]
    if symbol.error:
        lines.append(f"  error:         {symbol.error}")
    if symbol.extra:
        lines.append(f"  extra:         {json.dumps(symbol.extra, ensure_ascii=False)}")
    lines.append("  text:")
    lines.append(_indent_block(symbol.text if symbol.text else "(empty)"))
    lines.append("  bytes:")
    lines.append(_indent_block(_hex_dump(symbol.raw_bytes) if symbol.raw_bytes else "(empty)"))
    return "\n".join(lines)


def _position_line(position: dict[str, dict[str, int]]) -> str:
    order = ("top_left", "top_right", "bottom_right", "bottom_left")
    return " -> ".join(f"{name}({position[name]['x']},{position[name]['y']})" for name in order)


def _indent_block(text: str) -> str:
    return "\n".join(f"    {line}" for line in text.splitlines() or [""])


def _hex_dump(data: bytes) -> str:
    rows: list[str] = []
    for offset in range(0, len(data), 16):
        chunk = data[offset : offset + 16]
        hex_part = " ".join(f"{byte:02x}" for byte in chunk)
        ascii_part = "".join(chr(byte) if 32 <= byte <= 126 else "." for byte in chunk)
        rows.append(f"{offset:04x}  {hex_part:<47}  {ascii_part}")
    return "\n".join(rows)


def _configure_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


if __name__ == "__main__":
    raise SystemExit(main())
