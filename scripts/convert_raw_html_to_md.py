#!/usr/bin/env python3
"""Convert cached SEC filing HTML files into Markdown files.

Usage:
    .venv311/bin/poetry run python scripts/convert_raw_html_to_md.py \
      --input-dir data/raw \
      --output-dir output/html_markdown \
      --limit 10
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import sec2md

log = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert cached SEC HTML files to Markdown.")
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("data/raw"),
        help="Directory with cached SEC filing HTML files.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/html_markdown"),
        help="Directory where Markdown files will be written.",
    )
    parser.add_argument(
        "--pattern",
        default="*.html",
        help="Glob pattern for input files inside --input-dir.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum number of HTML files to convert (sorted by filename).",
    )
    return parser.parse_args()


def _pages_to_markdown(raw_html: str) -> str:
    pages = sec2md.parse_filing(raw_html)
    rendered: list[str] = []
    for page in pages:
        content = getattr(page, "content", "")
        page_number = getattr(page, "number", None)
        if not isinstance(content, str):
            continue
        content_clean = content.strip()
        if not content_clean:
            continue
        if page_number is not None:
            rendered.append(f"## Page {page_number}")
            rendered.append("")
        rendered.append(content_clean)
        rendered.append("")
    markdown = "\n".join(rendered).strip()
    return f"{markdown}\n" if markdown else ""


def main() -> int:
    args = parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    if args.limit <= 0:
        log.error("--limit must be > 0")
        return 1
    if not args.input_dir.exists():
        log.error("Input directory does not exist: %s", args.input_dir)
        return 1

    html_files = sorted(args.input_dir.glob(args.pattern))
    if not html_files:
        log.error("No files matched %r in %s", args.pattern, args.input_dir)
        return 1

    selected_files = html_files[: args.limit]
    args.output_dir.mkdir(parents=True, exist_ok=True)

    converted_count = 0
    for html_path in selected_files:
        try:
            raw_html = html_path.read_text(encoding="utf-8", errors="replace")
            markdown = _pages_to_markdown(raw_html)
            output_path = args.output_dir / f"{html_path.stem}.md"
            output_path.write_text(markdown, encoding="utf-8")
            log.info("Wrote %s (%d chars)", output_path, len(markdown))
            converted_count += 1
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to convert %s: %s", html_path, exc)

    log.info("Converted %d/%d files.", converted_count, len(selected_files))
    return 0 if converted_count == len(selected_files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
