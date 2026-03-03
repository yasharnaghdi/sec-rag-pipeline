#!/usr/bin/env python3
"""Build an S&P 500 DEF 14A manifest for fiscal years 2023 and 2024."""
from __future__ import annotations

import csv
import logging
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import requests  # type: ignore[import-untyped]
from bs4 import BeautifulSoup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

SP500_WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
EDGAR_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
RATE_SLEEP = 0.12
TARGET_FORM = "DEF 14A"
DATE_FROM = "2023-01-01"
DATE_TO = "2025-06-30"
TARGET_FISCAL_YEARS = {"2023", "2024"}
FIXTURES_DIR = Path("fixtures")


@dataclass(frozen=True)
class ManifestRow:
    """Single manifest row written to ``fixtures/sp500_manifest.csv``."""

    slot: int
    cik: str
    company_name: str
    ticker: str
    industry: str
    form_type: str
    filing_date: str
    accession_number: str
    edgar_url: str
    fiscal_year: str
    source_url: str
    raw_html_path: str = ""


@dataclass(frozen=True)
class ErrorRow:
    """Failure row written to ``fixtures/sp500_manifest_errors.csv``."""

    ticker: str
    company_name: str
    cik: str
    reason: str


MANIFEST_FIELDNAMES = [
    "slot",
    "cik",
    "company_name",
    "ticker",
    "industry",
    "form_type",
    "filing_date",
    "accession_number",
    "edgar_url",
    "fiscal_year",
    "source_url",
    "raw_html_path",
]
ERROR_FIELDNAMES = ["ticker", "company_name", "cik", "reason"]


def fetch_sp500_tickers() -> list[dict[str, str]]:
    """Scrape the S&P 500 table from Wikipedia."""
    log.info("Fetching S&P 500 list from Wikipedia...")
    response = requests.get(SP500_WIKI_URL, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")

    table = soup.find("table", {"id": "constituents"})
    if table is None:
        raise RuntimeError("Could not find S&P 500 constituents table on Wikipedia.")

    rows: list[dict[str, str]] = []
    for tr in table.find_all("tr")[1:]:
        cells = tr.find_all("td")
        if len(cells) < 5:
            continue
        ticker = cells[0].get_text(strip=True).replace(".", "-").upper()
        company_name = cells[1].get_text(strip=True)
        industry = cells[4].get_text(strip=True)
        rows.append(
            {
                "ticker": ticker,
                "company_name": company_name,
                "industry": industry,
            }
        )

    log.info("Found %s S&P 500 companies on Wikipedia", len(rows))
    return rows


def build_ticker_cik_map(session: requests.Session) -> dict[str, str]:
    """Download and parse EDGAR ticker -> CIK mappings."""
    log.info("Downloading EDGAR company tickers map...")
    response = session.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
    response.raise_for_status()
    payload = response.json()

    ticker_map: dict[str, str] = {}
    for entry in payload.values():
        ticker = str(entry.get("ticker", "")).upper().strip()
        cik_value = entry.get("cik_str", "")
        cik = str(cik_value).zfill(10)
        if ticker:
            ticker_map[ticker] = cik

    log.info("Loaded %s tickers from EDGAR", len(ticker_map))
    return ticker_map


def fetch_def14a_filings(cik: str, session: requests.Session) -> list[dict[str, str]]:
    """Fetch recent DEF 14A filings for ``cik`` within the target date window."""
    response = session.get(EDGAR_SUBMISSIONS_URL.format(cik=int(cik)), timeout=30)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    payload = response.json()

    filings_data = payload.get("filings", {}).get("recent", {})
    forms = filings_data.get("form", [])
    dates = filings_data.get("filingDate", [])
    accessions = filings_data.get("accessionNumber", [])
    primary_docs = filings_data.get("primaryDocument", [])

    results: list[dict[str, str]] = []
    for form, filing_date, accession, primary_doc in zip(forms, dates, accessions, primary_docs):
        if form != TARGET_FORM:
            continue
        if not (DATE_FROM <= filing_date <= DATE_TO):
            continue

        accession_clean = accession.replace("-", "")
        edgar_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{accession_clean}/{primary_doc}"
        )
        results.append(
            {
                "filing_date": filing_date,
                "accession_number": accession,
                "edgar_url": edgar_url,
                "source_url": edgar_url,
            }
        )

    return results


def infer_fiscal_year(filing_date: str) -> str:
    """Infer fiscal year from filing date using a Jan-Aug / Sep-Dec split."""
    year = int(filing_date[:4])
    month = int(filing_date[5:7])
    if month <= 8:
        return str(year - 1)
    return str(year)


def _manifest_has_data(path: Path) -> bool:
    """Return ``True`` if ``path`` exists and already contains many rows."""
    if not path.exists():
        return False

    with path.open("r", encoding="utf-8", newline="") as handle:
        row_count = sum(1 for _ in handle) - 1
    return row_count > 100


def _assign_slots(rows: list[ManifestRow]) -> list[ManifestRow]:
    """Return rows with deterministic, 1-based slot values."""
    sorted_rows = sorted(rows, key=lambda row: (row.ticker, row.fiscal_year, row.accession_number), reverse=False)
    slotted: list[ManifestRow] = []
    for index, row in enumerate(sorted_rows, start=1):
        slotted.append(
            ManifestRow(
                slot=index,
                cik=row.cik,
                company_name=row.company_name,
                ticker=row.ticker,
                industry=row.industry,
                form_type=row.form_type,
                filing_date=row.filing_date,
                accession_number=row.accession_number,
                edgar_url=row.edgar_url,
                fiscal_year=row.fiscal_year,
                source_url=row.source_url,
                raw_html_path=row.raw_html_path,
            )
        )
    return slotted


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    """Write rows to CSV with deterministic header ordering."""
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    """Build ``sp500_manifest.csv`` and ``sp500_manifest_errors.csv`` in fixtures."""
    sec_user_agent = os.getenv("SEC_USER_AGENT")
    if not sec_user_agent:
        sys.exit(
            "ERROR: SEC_USER_AGENT environment variable not set.\n"
            "Set it to 'Your Name your@email.com' before running this script."
        )

    FIXTURES_DIR.mkdir(exist_ok=True)
    manifest_path = FIXTURES_DIR / "sp500_manifest.csv"
    errors_path = FIXTURES_DIR / "sp500_manifest_errors.csv"

    if _manifest_has_data(manifest_path):
        with manifest_path.open("r", encoding="utf-8", newline="") as handle:
            row_count = max(0, sum(1 for _ in handle) - 1)
        log.info("Manifest already exists with %s rows; skipping rebuild.", row_count)
        log.info("Delete fixtures/sp500_manifest.csv to force a rebuild.")
        return

    session = requests.Session()
    session.headers.update({"User-Agent": sec_user_agent})

    sp500_companies = fetch_sp500_tickers()
    time.sleep(RATE_SLEEP)
    ticker_cik_map = build_ticker_cik_map(session)

    manifest_rows: list[ManifestRow] = []
    error_rows: list[ErrorRow] = []

    for index, company in enumerate(sp500_companies, start=1):
        ticker = company["ticker"].upper()
        company_name = company["company_name"]
        industry = company["industry"]

        cik = ticker_cik_map.get(ticker)
        if cik is None:
            log.warning("[%s/%s] %s: CIK not found", index, len(sp500_companies), ticker)
            error_rows.append(
                ErrorRow(
                    ticker=ticker,
                    company_name=company_name,
                    cik="",
                    reason="CIK not found in EDGAR ticker map",
                )
            )
            continue

        time.sleep(RATE_SLEEP)
        try:
            filings = fetch_def14a_filings(cik, session)
        except Exception as exc:  # pragma: no cover - network/runtime path
            log.warning(
                "[%s/%s] %s (CIK %s): EDGAR fetch failed - %s",
                index,
                len(sp500_companies),
                ticker,
                cik,
                exc,
            )
            error_rows.append(
                ErrorRow(
                    ticker=ticker,
                    company_name=company_name,
                    cik=cik,
                    reason=str(exc),
                )
            )
            continue

        selected_by_year: dict[str, dict[str, str]] = {}
        for filing in filings:
            fiscal_year = infer_fiscal_year(filing["filing_date"])
            if fiscal_year not in TARGET_FISCAL_YEARS:
                continue
            if fiscal_year in selected_by_year:
                continue
            selected_by_year[fiscal_year] = filing
            if len(selected_by_year) >= 2:
                break

        if not selected_by_year:
            log.warning("[%s/%s] %s: No DEF 14A found for FY2023/FY2024", index, len(sp500_companies), ticker)
            error_rows.append(
                ErrorRow(
                    ticker=ticker,
                    company_name=company_name,
                    cik=cik,
                    reason="No DEF 14A in date range for FY2023/FY2024",
                )
            )
            continue

        for fiscal_year in sorted(selected_by_year.keys()):
            filing = selected_by_year[fiscal_year]
            manifest_rows.append(
                ManifestRow(
                    slot=0,
                    cik=str(int(cik)),
                    company_name=company_name,
                    ticker=ticker,
                    industry=industry,
                    form_type=TARGET_FORM,
                    filing_date=filing["filing_date"],
                    accession_number=filing["accession_number"],
                    edgar_url=filing["edgar_url"],
                    fiscal_year=fiscal_year,
                    source_url=filing["source_url"],
                    raw_html_path="",
                )
            )

        log.info(
            "[%s/%s] %s: %s filing(s) selected",
            index,
            len(sp500_companies),
            ticker,
            len(selected_by_year),
        )

    final_rows = _assign_slots(manifest_rows)
    final_error_rows = sorted(error_rows, key=lambda row: (row.ticker, row.reason))

    _write_csv(
        manifest_path,
        MANIFEST_FIELDNAMES,
        [asdict(row) for row in final_rows],
    )
    _write_csv(
        errors_path,
        ERROR_FIELDNAMES,
        [asdict(row) for row in final_error_rows],
    )

    tickers_with_two_years = 0
    per_ticker_counts: dict[str, int] = {}
    for row in final_rows:
        per_ticker_counts[row.ticker] = per_ticker_counts.get(row.ticker, 0) + 1
    tickers_with_two_years = sum(1 for count in per_ticker_counts.values() if count == 2)

    log.info("Done. %s manifest rows -> %s", len(final_rows), manifest_path)
    log.info("      %s error rows   -> %s", len(final_error_rows), errors_path)
    log.info("      Companies with FY2023 + FY2024: %s", tickers_with_two_years)


if __name__ == "__main__":
    main()
