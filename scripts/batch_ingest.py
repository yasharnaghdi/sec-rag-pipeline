#!/usr/bin/env python3
"""Batch ingest all S&P 500 DEF 14A filings: parse -> chunk -> store -> CSV.

Reads  : fixtures/sp500_manifest.csv
         data/raw/{cik}_{accession_clean}.html

Writes : output/sp500_ingest_log.csv   (one row per filing)
         output/sp500_chunks.csv        (one row per chunk)

Usage:
  poetry run python scripts/batch_ingest.py [--db] [--limit N]

Flags:
  --db      also write chunks to PostgreSQL via ChunkWriter (requires DB_URL env var)
  --limit N process only first N filings (useful for smoke-testing)

Safe to re-run: skips filings already in sp500_ingest_log.csv with status=success.
"""
from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Protocol

from ingestion.metadata_model import DocumentMetadata
from ingestion.sec_chunker import Chunk, SECChunker
from ingestion.sec_html_parser import SECHTMLParser

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

FIXTURES_DIR = Path("fixtures")
DATA_RAW_DIR = Path("data/raw")
OUTPUT_DIR = Path("output")

MANIFEST_PATH = FIXTURES_DIR / "sp500_manifest.csv"
INGEST_LOG_PATH = OUTPUT_DIR / "sp500_ingest_log.csv"
CHUNKS_CSV_PATH = OUTPUT_DIR / "sp500_chunks.csv"

INGEST_LOG_FIELDS = [
    "ticker",
    "cik",
    "accession_number",
    "fiscal_year",
    "status",
    "chunk_count",
    "block_count",
    "elapsed_seconds",
    "flag",
    "timestamp",
]
CHUNKS_FIELDS = [
    "chunk_id",
    "ticker",
    "cik",
    "fiscal_year",
    "filing_date",
    "section_id",
    "toc_page_range",
    "token_count",
    "citation_string",
    "text_preview",
]


class _ChunkWriterProtocol(Protocol):
    def write_chunks(self, chunks: list[Chunk], metadata: DocumentMetadata) -> int:
        ...


def _cache_path(cik: str, accession: str) -> Path:
    clean = accession.replace("-", "")
    return DATA_RAW_DIR / f"{cik.zfill(10)}_{clean}.html"


def _load_already_ingested() -> set[str]:
    if not INGEST_LOG_PATH.exists():
        return set()

    with INGEST_LOG_PATH.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return {
            row.get("accession_number", "")
            for row in reader
            if row.get("status") == "success" and row.get("accession_number")
        }


def _chunks_csv_exists() -> bool:
    return CHUNKS_CSV_PATH.exists() and CHUNKS_CSV_PATH.stat().st_size > 0


def _row_value(row: dict[str, str], key: str, default: str = "") -> str:
    value = row.get(key, default)
    return value if value is not None else default


def _parse_filing_date(value: str) -> date:
    return date.fromisoformat(value) if value else date.today()


def _build_metadata(row: dict[str, str], cache_path: Path) -> DocumentMetadata:
    cik = _row_value(row, "cik")
    accession = _row_value(row, "accession_number")
    filing_date = _parse_filing_date(_row_value(row, "filing_date"))
    source_url = _row_value(row, "source_url") or _row_value(row, "edgar_url")

    return DocumentMetadata(
        document_id=f"{cik}_{accession.replace('-', '_')}",
        cik=cik,
        company_name=_row_value(row, "company_name"),
        form_type=_row_value(row, "form_type", "DEF 14A"),
        filing_date=filing_date,
        accession_number=accession,
        source_url=source_url,
        fiscal_year_end=None,
        raw_html_path=str(cache_path),
    )


def _write_chunk_row(
    writer: csv.DictWriter[str],
    chunk: Chunk,
    *,
    ticker: str,
    cik: str,
    fiscal_year: str,
    filing_date: str,
) -> None:
    toc_page_range = (
        f"{chunk.toc_page_range[0]}-{chunk.toc_page_range[1]}"
        if chunk.toc_page_range is not None
        else ""
    )
    writer.writerow(
        {
            "chunk_id": chunk.id,
            "ticker": ticker,
            "cik": cik,
            "fiscal_year": fiscal_year,
            "filing_date": filing_date,
            "section_id": chunk.section_id,
            "toc_page_range": toc_page_range,
            "token_count": chunk.token_count,
            "citation_string": chunk.citation_string,
            "text_preview": chunk.text[:150].replace("\n", " "),
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", action="store_true", help="Write to PostgreSQL")
    parser.add_argument("--limit", type=int, default=0, help="Process only N filings")
    args = parser.parse_args()

    if not MANIFEST_PATH.exists():
        sys.exit(f"ERROR: {MANIFEST_PATH} not found")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    chunk_writer: _ChunkWriterProtocol | None = None
    if args.db:
        db_url = os.getenv("DB_URL")
        if not db_url:
            sys.exit("ERROR: --db flag requires DB_URL environment variable")
        from storage.writer import ChunkWriter

        chunk_writer = ChunkWriter(db_url=db_url)

    already_ingested = _load_already_ingested()
    log.info("Already ingested: %s filings - will skip", len(already_ingested))

    with MANIFEST_PATH.open("r", encoding="utf-8", newline="") as handle:
        manifest_rows = list(csv.DictReader(handle))

    if args.limit > 0:
        manifest_rows = manifest_rows[: args.limit]
        log.info("--limit %s: processing first %s rows only", args.limit, args.limit)

    html_parser = SECHTMLParser()
    chunker = SECChunker()

    ingest_log_is_new = not INGEST_LOG_PATH.exists()
    chunks_is_new = not _chunks_csv_exists()

    success = 0
    failed = 0
    skipped = 0
    total_chunks = 0

    with INGEST_LOG_PATH.open("a", newline="", encoding="utf-8") as ingest_log_file, CHUNKS_CSV_PATH.open(
        "a",
        newline="",
        encoding="utf-8",
    ) as chunks_file:
        ingest_writer = csv.DictWriter(ingest_log_file, fieldnames=INGEST_LOG_FIELDS)
        chunks_writer = csv.DictWriter(chunks_file, fieldnames=CHUNKS_FIELDS)

        if ingest_log_is_new:
            ingest_writer.writeheader()
        if chunks_is_new:
            chunks_writer.writeheader()

        for index, row in enumerate(manifest_rows, start=1):
            ticker = _row_value(row, "ticker")
            cik = _row_value(row, "cik")
            accession = _row_value(row, "accession_number")
            fiscal_year = _row_value(row, "fiscal_year")
            filing_date = _row_value(row, "filing_date")

            if accession in already_ingested:
                skipped += 1
                continue

            cache_path = _cache_path(cik, accession)
            if not cache_path.exists():
                log.warning(
                    "[%s/%s] %s %s: cache file not found - %s",
                    index,
                    len(manifest_rows),
                    ticker,
                    fiscal_year,
                    cache_path,
                )
                ingest_writer.writerow(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "accession_number": accession,
                        "fiscal_year": fiscal_year,
                        "status": "skipped_no_file",
                        "chunk_count": 0,
                        "block_count": 0,
                        "elapsed_seconds": 0,
                        "flag": "missing_cache",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                ingest_log_file.flush()
                skipped += 1
                continue

            t0 = time.perf_counter()
            try:
                raw_html = cache_path.read_text(encoding="utf-8", errors="replace")
                metadata = _build_metadata(row, cache_path)

                blocks = html_parser.parse(raw_html, metadata)
                chunks = chunker.chunk_blocks(blocks, metadata)

                if chunk_writer is not None:
                    chunk_writer.write_chunks(chunks, metadata)

                for chunk in chunks:
                    _write_chunk_row(
                        chunks_writer,
                        chunk,
                        ticker=ticker,
                        cik=cik,
                        fiscal_year=fiscal_year,
                        filing_date=filing_date,
                    )
                chunks_file.flush()

                elapsed = time.perf_counter() - t0
                ingest_writer.writerow(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "accession_number": accession,
                        "fiscal_year": fiscal_year,
                        "status": "success",
                        "chunk_count": len(chunks),
                        "block_count": len(blocks),
                        "elapsed_seconds": round(elapsed, 2),
                        "flag": "",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                ingest_log_file.flush()

                total_chunks += len(chunks)
                success += 1
                already_ingested.add(accession)
                log.info(
                    "[%s/%s] %s %s: %s chunks, %s blocks (%.1fs)",
                    index,
                    len(manifest_rows),
                    ticker,
                    fiscal_year,
                    len(chunks),
                    len(blocks),
                    elapsed,
                )
            except Exception as exc:  # pragma: no cover - runtime error path
                elapsed = time.perf_counter() - t0
                log.error(
                    "[%s/%s] %s %s: FAILED - %s",
                    index,
                    len(manifest_rows),
                    ticker,
                    fiscal_year,
                    exc,
                )
                ingest_writer.writerow(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "accession_number": accession,
                        "fiscal_year": fiscal_year,
                        "status": "failed",
                        "chunk_count": 0,
                        "block_count": 0,
                        "elapsed_seconds": round(elapsed, 2),
                        "flag": str(exc)[:120],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                ingest_log_file.flush()
                failed += 1

    log.info("%s", "=" * 60)
    log.info("Batch ingest complete.")
    log.info("  Success  : %s", success)
    log.info("  Skipped  : %s", skipped)
    log.info("  Failed   : %s", failed)
    log.info("  Chunks   : %s", f"{total_chunks:,}")
    log.info("  Ingest log : %s", INGEST_LOG_PATH)
    log.info("  Chunks CSV : %s", CHUNKS_CSV_PATH)


if __name__ == "__main__":
    main()
