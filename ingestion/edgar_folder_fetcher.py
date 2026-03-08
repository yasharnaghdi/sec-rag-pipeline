"""Fetch DEF 14A filing HTML from SEC EDGAR for folder-driven ingestion."""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, cast

import requests  # type: ignore[import-untyped]

log = logging.getLogger(__name__)

_SEC_DATA_BASE = "https://data.sec.gov"
_SEC_ARCHIVES_BASE = "https://www.sec.gov"
_USER_AGENT = os.getenv("SEC_USER_AGENT", "sec-rag-pipeline contact@yourorg.com")
_HEADERS = {"User-Agent": _USER_AGENT}
_RATE_LIMIT_SEC = 0.12

DATA_RAW_DIR = Path("data/raw")


@dataclass(frozen=True)
class FetchedFiling:
    """Resolved filing payload and metadata needed by downstream ingestion."""

    raw_html: str
    accession_number: str
    filing_date: date | None
    filing_url: str
    cache_path: Path
    company_name: str = ""
    ticker: str = ""


@dataclass(frozen=True)
class _FilingIndexEntry:
    accession_number: str
    form: str
    primary_document: str
    filing_date: date | None


def _normalize_form_type(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def _extract_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _folder_id_candidates(folder_id: str) -> set[str]:
    raw = folder_id.strip()
    digits = _extract_digits(raw)
    candidates = {raw, digits, digits.lstrip("0")}
    return {candidate for candidate in candidates if candidate}


def _accession_matches_folder(accession_number: str, folder_id: str) -> bool:
    candidates = _folder_id_candidates(folder_id)
    accession_raw = accession_number.strip()
    accession_digits = _extract_digits(accession_raw)

    for candidate in candidates:
        if candidate.isdigit():
            if accession_digits == candidate:
                return True
            if accession_digits.endswith(candidate):
                return True
            continue
        if accession_raw.lower() == candidate.lower():
            return True
    return False


def _parse_date(value: object) -> date | None:
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _throttle() -> None:
    time.sleep(_RATE_LIMIT_SEC)


def _get_json(url: str, session: requests.Session) -> dict[str, Any]:
    response = session.get(url, headers=_HEADERS, timeout=30)
    response.raise_for_status()
    _throttle()
    payload = response.json()
    if not isinstance(payload, dict):
        msg = f"Expected dict JSON payload from {url}"
        raise ValueError(msg)
    return payload


def _get_text(url: str, session: requests.Session) -> str:
    response = session.get(url, headers=_HEADERS, timeout=60)
    response.raise_for_status()
    _throttle()
    return cast(str, response.text)


def _extract_entries(submissions_payload: dict[str, Any]) -> list[_FilingIndexEntry]:
    filings = submissions_payload.get("filings")
    if not isinstance(filings, dict):
        return []
    recent = filings.get("recent")
    if not isinstance(recent, dict):
        return []

    accession_numbers = recent.get("accessionNumber")
    forms = recent.get("form")
    primary_docs = recent.get("primaryDocument")
    filing_dates = recent.get("filingDate")
    if not (
        isinstance(accession_numbers, list)
        and isinstance(forms, list)
        and isinstance(primary_docs, list)
    ):
        return []

    max_len = min(len(accession_numbers), len(forms), len(primary_docs))
    entries: list[_FilingIndexEntry] = []
    for index in range(max_len):
        accession_raw = accession_numbers[index]
        form_raw = forms[index]
        primary_doc_raw = primary_docs[index]
        filing_date_raw = filing_dates[index] if isinstance(filing_dates, list) and index < len(filing_dates) else ""
        if not isinstance(accession_raw, str):
            continue
        if not isinstance(form_raw, str):
            continue
        if not isinstance(primary_doc_raw, str):
            continue
        entries.append(
            _FilingIndexEntry(
                accession_number=accession_raw,
                form=form_raw,
                primary_document=primary_doc_raw,
                filing_date=_parse_date(filing_date_raw),
            )
        )
    return entries


def _extract_company_identity(submissions_payload: dict[str, Any]) -> tuple[str, str]:
    """Extract issuer company name and ticker from SEC submissions payload."""
    company_name = ""
    ticker = ""

    name_val = submissions_payload.get("name")
    if isinstance(name_val, str):
        company_name = name_val.strip()
    if not company_name:
        entity_val = submissions_payload.get("entityName")
        if isinstance(entity_val, str):
            company_name = entity_val.strip()

    tickers_val = submissions_payload.get("tickers")
    if isinstance(tickers_val, list):
        for item in tickers_val:
            if isinstance(item, str) and item.strip():
                ticker = item.strip()
                break
    if not ticker:
        ticker_val = submissions_payload.get("ticker")
        if isinstance(ticker_val, str):
            ticker = ticker_val.strip()

    return company_name, ticker


def _fetch_historical_submission_entries(
    submissions_payload: dict[str, Any],
    session: requests.Session,
) -> list[_FilingIndexEntry]:
    filings = submissions_payload.get("filings")
    if not isinstance(filings, dict):
        return []

    files = filings.get("files")
    if not isinstance(files, list):
        return []

    all_entries: list[_FilingIndexEntry] = []
    for file_entry in files:
        if not isinstance(file_entry, dict):
            continue
        name = file_entry.get("name")
        if not isinstance(name, str) or not name.endswith(".json"):
            continue
        url = f"{_SEC_DATA_BASE}/submissions/{name}"
        try:
            historical_payload = _get_json(url, session)
        except requests.HTTPError:
            log.warning("Unable to fetch historical submissions file: %s", url)
            continue
        all_entries.extend(_extract_entries(historical_payload))
    return all_entries


def _resolve_primary_document(
    cik: str,
    accession_clean: str,
    suggested_document: str,
    session: requests.Session,
) -> str:
    lowered = suggested_document.lower()
    if lowered.endswith(".htm") or lowered.endswith(".html"):
        return suggested_document

    index_url = (
        f"{_SEC_ARCHIVES_BASE}/Archives/edgar/data/{cik.lstrip('0')}/{accession_clean}/index.json"
    )
    payload = _get_json(index_url, session)
    directory = payload.get("directory")
    if not isinstance(directory, dict):
        if suggested_document:
            return suggested_document
        msg = f"Unable to resolve primary document for accession {accession_clean}"
        raise ValueError(msg)

    items = directory.get("item")
    if not isinstance(items, list):
        if suggested_document:
            return suggested_document
        msg = f"Unable to resolve primary document for accession {accession_clean}"
        raise ValueError(msg)

    for item in items:
        if not isinstance(item, dict):
            continue
        candidate_name = item.get("name")
        if not isinstance(candidate_name, str):
            continue
        lowered_name = candidate_name.lower()
        if lowered_name.endswith(".htm") or lowered_name.endswith(".html"):
            return candidate_name

    if suggested_document:
        return suggested_document

    msg = f"Unable to find .htm/.html primary document for accession {accession_clean}"
    raise ValueError(msg)


def _select_latest_form_entry(entries: list[_FilingIndexEntry], form_type: str) -> _FilingIndexEntry | None:
    """Pick the most recent filing entry for a target form."""
    target_form = _normalize_form_type(form_type)
    candidates = [entry for entry in entries if _normalize_form_type(entry.form) == target_form]
    if not candidates:
        return None

    def _sort_key(entry: _FilingIndexEntry) -> tuple[date, str]:
        return (entry.filing_date or date.min, entry.accession_number)

    return sorted(candidates, key=_sort_key, reverse=True)[0]


def _fetch_entry_filing(
    cik_digits: str,
    cik_padded: str,
    matched_entry: _FilingIndexEntry,
    session: requests.Session,
    company_name: str = "",
    ticker: str = "",
) -> FetchedFiling:
    accession_clean = matched_entry.accession_number.replace("-", "")
    cache_path = DATA_RAW_DIR / f"{cik_padded}_{accession_clean}.html"
    if cache_path.exists():
        log.info("Cache hit: %s", cache_path)
        raw_html = cache_path.read_text(encoding="utf-8", errors="replace")
        filing_url = (
            f"{_SEC_ARCHIVES_BASE}/Archives/edgar/data/{cik_digits.lstrip('0')}/"
            f"{accession_clean}/{matched_entry.primary_document}"
        )
        return FetchedFiling(
            raw_html=raw_html,
            accession_number=matched_entry.accession_number,
            filing_date=matched_entry.filing_date,
            filing_url=filing_url,
            cache_path=cache_path,
            company_name=company_name,
            ticker=ticker,
        )

    primary_document = _resolve_primary_document(
        cik_digits,
        accession_clean,
        matched_entry.primary_document,
        session,
    )
    filing_url = (
        f"{_SEC_ARCHIVES_BASE}/Archives/edgar/data/{cik_digits.lstrip('0')}/"
        f"{accession_clean}/{primary_document}"
    )
    log.info("Fetching filing: %s", filing_url)
    raw_html = _get_text(filing_url, session)

    DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(raw_html, encoding="utf-8")
    log.info("Cached -> %s (%s chars)", cache_path, len(raw_html))
    return FetchedFiling(
        raw_html=raw_html,
        accession_number=matched_entry.accession_number,
        filing_date=matched_entry.filing_date,
        filing_url=filing_url,
        cache_path=cache_path,
        company_name=company_name,
        ticker=ticker,
    )


def fetch_filing(
    cik: str,
    folder_id: str,
    form_type: str = "DEF 14A",
    *,
    session: requests.Session | None = None,
) -> FetchedFiling:
    """Fetch raw filing HTML and resolved filing metadata for a CIK + folder id."""
    cik_digits = _extract_digits(cik)
    if not cik_digits:
        raise ValueError("cik must include at least one digit")

    cik_padded = cik_digits.zfill(10)
    submissions_url = f"{_SEC_DATA_BASE}/submissions/CIK{cik_padded}.json"
    target_form = _normalize_form_type(form_type)

    owns_session = session is None
    http = session or requests.Session()
    try:
        log.info("Fetching submissions index: %s", submissions_url)
        submissions_payload = _get_json(submissions_url, http)
        company_name, ticker = _extract_company_identity(submissions_payload)
        entries = _extract_entries(submissions_payload)
        entries.extend(_fetch_historical_submission_entries(submissions_payload, http))

        matched_entry: _FilingIndexEntry | None = None
        for entry in entries:
            if _normalize_form_type(entry.form) != target_form:
                continue
            if _accession_matches_folder(entry.accession_number, folder_id):
                matched_entry = entry
                break

        if matched_entry is None:
            msg = f"No {form_type} found for CIK={cik_digits}, folder_id={folder_id}"
            raise ValueError(msg)

        return _fetch_entry_filing(
            cik_digits,
            cik_padded,
            matched_entry,
            http,
            company_name=company_name,
            ticker=ticker,
        )
    finally:
        if owns_session:
            http.close()


def fetch_latest_def14a(
    cik: str,
    *,
    session: requests.Session | None = None,
) -> FetchedFiling:
    """Fetch the latest DEF 14A for a CIK without requiring folder/accession input."""
    cik_digits = _extract_digits(cik)
    if not cik_digits:
        raise ValueError("cik must include at least one digit")

    cik_padded = cik_digits.zfill(10)
    submissions_url = f"{_SEC_DATA_BASE}/submissions/CIK{cik_padded}.json"

    owns_session = session is None
    http = session or requests.Session()
    try:
        log.info("Fetching submissions index: %s", submissions_url)
        submissions_payload = _get_json(submissions_url, http)
        company_name, ticker = _extract_company_identity(submissions_payload)
        entries = _extract_entries(submissions_payload)
        entries.extend(_fetch_historical_submission_entries(submissions_payload, http))

        matched_entry = _select_latest_form_entry(entries, "DEF 14A")
        if matched_entry is None:
            msg = f"No DEF 14A found for CIK={cik_digits}"
            raise ValueError(msg)

        return _fetch_entry_filing(
            cik_digits,
            cik_padded,
            matched_entry,
            http,
            company_name=company_name,
            ticker=ticker,
        )
    finally:
        if owns_session:
            http.close()


def fetch_filing_html(
    cik: str,
    folder_id: str,
    form_type: str = "DEF 14A",
) -> str:
    """Fetch raw filing HTML for compatibility with simple call sites."""
    return fetch_filing(cik=cik, folder_id=folder_id, form_type=form_type).raw_html
