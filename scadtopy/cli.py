"""Command-line entry point for scadtopy."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .translator import TranslationError, translate_file


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="scadtopy",
        description="Translate an OpenSCAD file to Python code using Piecad.",
    )
    parser.add_argument("source", type=Path, help="input .scad file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="output .py file (defaults to the input path with a .py extension)",
    )
    args = parser.parse_args()
    output = args.output or args.source.with_suffix(".py")

    try:
        python_source = translate_file(args.source)
        output.write_text(python_source, encoding="utf-8")
    except (OSError, TranslationError) as error:
        print(f"scadtopy: {error}", file=sys.stderr)
        return 1

    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
