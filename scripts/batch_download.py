#!/usr/bin/env python3
"""Batch-download DEF 14A HTML files listed in ``fixtures/sp500_manifest.csv``."""
from __future__ import annotations

import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests  # type: ignore[import-untyped]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

FIXTURES_DIR = Path("fixtures")
DATA_RAW_DIR = Path("data/raw")
RATE_SLEEP = 0.12
MIN_FILE_SIZE = 50_000

MANIFEST_PATH = FIXTURES_DIR / "sp500_manifest.csv"
LOG_PATH = FIXTURES_DIR / "sp500_download_log.csv"
LOG_FIELDNAMES = [
    "ticker",
    "cik",
    "accession_number",
    "fiscal_year",
    "status",
    "file_size_bytes",
    "flag",
    "timestamp",
]


def _accession_to_filename(cik: str, accession: str) -> str:
    """Convert CIK/accession into a deterministic HTML cache filename."""
    accession_clean = accession.replace("-", "")
    return f"{cik.zfill(10)}_{accession_clean}.html"


def _load_existing_log() -> set[str]:
    """Return accession numbers already present in ``sp500_download_log.csv``."""
    if not LOG_PATH.exists():
        return set()

    with LOG_PATH.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return {row["accession_number"] for row in reader if row.get("accession_number")}


def _manifest_url(row: dict[str, str]) -> str:
    """Resolve source URL from a manifest row with backward compatibility."""
    source_url = row.get("source_url", "").strip()
    if source_url:
        return source_url

    edgar_url = row.get("edgar_url", "").strip()
    if edgar_url:
        return edgar_url

    raise ValueError("Manifest row missing both source_url and edgar_url")


def main() -> None:
    """Download all filings in the S&P 500 manifest with cache-aware behavior."""
    sec_user_agent = os.getenv("SEC_USER_AGENT")
    if not sec_user_agent:
        sys.exit(
            "ERROR: SEC_USER_AGENT not set.\n"
            "Example: export SEC_USER_AGENT='Jane Smith jane@university.edu'"
        )

    if not MANIFEST_PATH.exists():
        sys.exit(f"ERROR: {MANIFEST_PATH} not found. Run build_sp500_manifest.py first.")

    DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    already_logged = _load_existing_log()

    with MANIFEST_PATH.open("r", encoding="utf-8", newline="") as handle:
        manifest_rows = list(csv.DictReader(handle))

    log.info("Manifest: %s filings to process", len(manifest_rows))
    log.info("Already logged: %s - these will be skipped", len(already_logged))

    session = requests.Session()
    session.headers.update({"User-Agent": sec_user_agent})

    downloaded = 0
    cached = 0
    failed = 0
    suspect = 0

    with LOG_PATH.open("a", newline="", encoding="utf-8") as log_handle:
        log_writer = csv.DictWriter(log_handle, fieldnames=LOG_FIELDNAMES)
        if LOG_PATH.stat().st_size == 0:
            log_writer.writeheader()

        for index, row in enumerate(manifest_rows, start=1):
            ticker = row.get("ticker", "")
            cik = row.get("cik", "")
            accession = row.get("accession_number", "")
            fiscal_year = row.get("fiscal_year", "")

            if not accession:
                failed += 1
                log.warning(
                    "[%s/%s] %s %s: FAILED - missing accession_number",
                    index,
                    len(manifest_rows),
                    ticker,
                    fiscal_year,
                )
                continue

            if accession in already_logged:
                cached += 1
                continue

            filename = _accession_to_filename(cik, accession)
            cache_path = DATA_RAW_DIR / filename

            if cache_path.exists() and cache_path.stat().st_size > MIN_FILE_SIZE:
                log_writer.writerow(
                    {
                        "ticker": ticker,
                        "cik": cik,
                        "accession_number": accession,
                        "fiscal_year": fiscal_year,
                        "status": "cached",
                        "file_size_bytes": cache_path.stat().st_size,
                        "flag": "",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                )
                log_handle.flush()
                cached += 1
                already_logged.add(accession)
                continue

            time.sleep(RATE_SLEEP)
            try:
                response = session.get(_manifest_url(row), timeout=45)
                response.raise_for_status()
                cache_path.write_text(response.text, encoding="utf-8", errors="replace")
                size = cache_path.stat().st_size
                flag = "suspect_file" if size < MIN_FILE_SIZE else ""
                status = "downloaded"
                downloaded += 1
                if flag:
                    suspect += 1
                log.info(
                    "[%s/%s] %s %s: %s bytes %s",
                    index,
                    len(manifest_rows),
                    ticker,
                    fiscal_year,
                    f"{size:,}",
                    "SUSPECT" if flag else "OK",
                )
            except Exception as exc:  # pragma: no cover - network/runtime path
                status = "failed"
                size = 0
                flag = ""
                failed += 1
                log.warning(
                    "[%s/%s] %s %s: FAILED - %s",
                    index,
                    len(manifest_rows),
                    ticker,
                    fiscal_year,
                    exc,
                )

            log_writer.writerow(
                {
                    "ticker": ticker,
                    "cik": cik,
                    "accession_number": accession,
                    "fiscal_year": fiscal_year,
                    "status": status,
                    "file_size_bytes": size,
                    "flag": flag,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            log_handle.flush()
            already_logged.add(accession)

    total = downloaded + cached + failed
    log.info("=" * 60)
    log.info("Complete. %s filings processed.", total)
    log.info("  Downloaded : %s", downloaded)
    log.info("  Cached     : %s", cached)
    log.info("  Failed     : %s", failed)
    log.info("  Suspect    : %s (file < %s bytes)", suspect, f"{MIN_FILE_SIZE:,}")
    log.info("Download log : %s", LOG_PATH)

    if failed > 100:
        log.warning(
            "WARNING: %s failures is above threshold. "
            "Check your SEC_USER_AGENT and network before proceeding.",
            failed,
        )


if __name__ == "__main__":
    main()
