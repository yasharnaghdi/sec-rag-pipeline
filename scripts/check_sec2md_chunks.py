"""Quick CLI check for sec2md page-aware chunking on cached SEC filings.

Usage:
    .venv311/bin/python scripts/check_sec2md_chunks.py \
        --file data/raw/0001518621_000143774925013209.html \
        --chunk-size 512 \
        --limit 10
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import sec2md


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run sec2md on a cached filing and print page-tagged chunks."
    )
    parser.add_argument(
        "--file",
        required=True,
        help="Path to cached filing HTML/TXT (e.g. data/raw/<cik>_<accession>.html).",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=512,
        help="Target chunk size passed to sec2md.chunk_pages (default: 512).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Number of chunks to print (default: 10).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    file_path = Path(args.file)
    if not file_path.exists():
        print(f"error: file not found: {file_path}", file=sys.stderr)
        return 1

    filing_text = file_path.read_text(encoding="utf-8", errors="ignore")
    pages = sec2md.parse_filing(filing_text)
    chunks = sec2md.chunk_pages(pages, chunk_size=args.chunk_size)

    print(f"file={file_path}")
    print(f"pages={len(pages)} chunks={len(chunks)} chunk_size={args.chunk_size}")
    print()

    for chunk in chunks[: args.limit]:
        preview = chunk.content[:100].replace("\n", " ")
        print(f"Page {chunk.page}: {preview}...")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
