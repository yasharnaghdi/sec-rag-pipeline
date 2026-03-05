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
            )

        primary_document = _resolve_primary_document(
            cik_digits,
            accession_clean,
            matched_entry.primary_document,
            http,
        )
        filing_url = (
            f"{_SEC_ARCHIVES_BASE}/Archives/edgar/data/{cik_digits.lstrip('0')}/"
            f"{accession_clean}/{primary_document}"
        )
        log.info("Fetching filing: %s", filing_url)
        raw_html = _get_text(filing_url, http)

        DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(raw_html, encoding="utf-8")
        log.info("Cached -> %s (%s chars)", cache_path, len(raw_html))
        return FetchedFiling(
            raw_html=raw_html,
            accession_number=matched_entry.accession_number,
            filing_date=matched_entry.filing_date,
            filing_url=filing_url,
            cache_path=cache_path,
        )
    finally:
        if owns_session:
            http.close()


def fetch_latest_def14a(
    cik: str,
    *,
    session: requests.Session | None = None,
) -> FetchedFiling:
    """Return the most recent DEF 14A filing for a given CIK.

    This is the correct acquisition entry point when only CIK codes are
    available (for example `client_input.csv`). It performs no folder_id
    or accession-suffix matching.

    Args:
        cik: SEC Central Index Key. May be zero-padded or bare digits.
        session: Optional shared requests.Session for connection reuse.
            If None, a new session is created and closed on exit.

    Returns:
        FetchedFiling with raw_html, accession_number, filing_date,
        filing_url, and cache_path populated.

    Raises:
        ValueError: If CIK contains no digits, or no DEF 14A is found.
        requests.HTTPError: On non-2xx HTTP responses.
    """
    cik_digits = _extract_digits(cik)
    if not cik_digits:
        raise ValueError(f"cik must contain at least one digit, got: {cik!r}")

    cik_padded = cik_digits.zfill(10)
    submissions_url = f"{_SEC_DATA_BASE}/submissions/CIK{cik_padded}.json"

    owns_session = session is None
    http = session or requests.Session()

    try:
        log.info("submissions index | cik=%s url=%s", cik_digits, submissions_url)
        submissions_payload = _get_json(submissions_url, http)

        entries = _extract_entries(submissions_payload)
        entries.extend(_fetch_historical_submission_entries(submissions_payload, http))

        def14a_entries = [
            entry
            for entry in entries
            if _normalize_form_type(entry.form) == "DEF14A"
        ]
        if not def14a_entries:
            raise ValueError(
                f"No DEF 14A filing found for CIK={cik_digits}. "
                f"Checked {len(entries)} total filings in submissions index."
            )

        def _sort_key(entry: _FilingIndexEntry) -> tuple[int, str]:
            return (1 if entry.filing_date is not None else 0, str(entry.filing_date or ""))

        matched_entry = sorted(def14a_entries, key=_sort_key, reverse=True)[0]
        log.info(
            "latest DEF 14A | cik=%s accession=%s date=%s",
            cik_digits,
            matched_entry.accession_number,
            matched_entry.filing_date,
        )

        accession_clean = matched_entry.accession_number.replace("-", "")
        cache_path = DATA_RAW_DIR / f"{cik_padded}_{accession_clean}.html"

        cik_no_leading_zeros = cik_digits.lstrip("0") or cik_digits
        filing_url_base = (
            f"{_SEC_ARCHIVES_BASE}/Archives/edgar/data/"
            f"{cik_no_leading_zeros}/{accession_clean}"
        )

        if cache_path.exists():
            log.info("cache hit | path=%s", cache_path)
            raw_html = cache_path.read_text(encoding="utf-8", errors="replace")
            return FetchedFiling(
                raw_html=raw_html,
                accession_number=matched_entry.accession_number,
                filing_date=matched_entry.filing_date,
                filing_url=f"{filing_url_base}/{matched_entry.primary_document}",
                cache_path=cache_path,
            )

        primary_document = _resolve_primary_document(
            cik_digits,
            accession_clean,
            matched_entry.primary_document,
            http,
        )
        filing_url = f"{filing_url_base}/{primary_document}"

        log.info("downloading | url=%s", filing_url)
        raw_html = _get_text(filing_url, http)

        DATA_RAW_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(raw_html, encoding="utf-8")
        log.info("cached | path=%s chars=%s", cache_path, len(raw_html))

        return FetchedFiling(
            raw_html=raw_html,
            accession_number=matched_entry.accession_number,
            filing_date=matched_entry.filing_date,
            filing_url=filing_url,
            cache_path=cache_path,
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
