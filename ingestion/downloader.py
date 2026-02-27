"""EDGAR downloader with manifest support and local HTML cache."""
from __future__ import annotations

import csv
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Protocol

from ingestion.metadata_model import FilingMetadata

logger = logging.getLogger(__name__)


def _configure_stdout_logger() -> None:
    if any(
        isinstance(handler, logging.StreamHandler) and getattr(handler, "stream", None) is sys.stdout
        for handler in logger.handlers
    ):
        return
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(stream_handler)
    logger.propagate = False
    logger.setLevel(logging.INFO)


def _coerce_date(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value[:10])
    return None


class EdgarClientProtocol(Protocol):
    """Dependency-injection boundary for all edgartools calls."""

    def set_identity(self, user_agent: str) -> None:
        """Set SEC identity string for outbound requests."""

    def resolve_latest_def14a_accession_before(self, cik: str, cutoff_date: date) -> str:
        """Return the accession number for the most recent DEF 14A before cutoff."""

    def fetch_filing_html(self, cik: str, accession_number: str, source_url: str) -> str:
        """Return HTML payload for a filing accession."""


class EdgartoolsClient:
    """Thin adapter around edgartools calls used by FilingDownloader."""

    def __init__(self) -> None:
        try:
            from edgar import Company, set_identity  # type: ignore[import-untyped]
        except Exception as exc:  # pragma: no cover - import/runtime dependency
            raise ImportError(
                "edgartools is required for downloader runtime operations."
            ) from exc
        self._company_cls = Company
        self._set_identity = set_identity

    def set_identity(self, user_agent: str) -> None:
        self._set_identity(user_agent)

    def resolve_latest_def14a_accession_before(self, cik: str, cutoff_date: date) -> str:
        company = self._company_cls(cik)
        filings = company.get_filings(form="DEF 14A")

        selected_filing: object | None = None
        selected_date: date | None = None
        for filing in filings:
            filing_date = _coerce_date(getattr(filing, "filing_date", None))
            if filing_date is None or filing_date >= cutoff_date:
                continue
            if selected_date is None or filing_date > selected_date:
                selected_filing = filing
                selected_date = filing_date

        if selected_filing is None:
            raise LookupError(f"No DEF 14A filing found for CIK {cik} before {cutoff_date.isoformat()}.")

        accession = getattr(selected_filing, "accession_number", None)
        if accession is None:
            raise LookupError(f"Resolved filing for CIK {cik} is missing accession_number.")
        return str(accession)

    def fetch_filing_html(self, cik: str, accession_number: str, source_url: str) -> str:
        company = self._company_cls(cik)
        filings = company.get_filings(accession_number=accession_number)
        filing = next(iter(filings), None)
        if filing is None:
            raise LookupError(
                f"Unable to find filing accession {accession_number} for CIK {cik}."
            )

        html_callable = getattr(filing, "html", None)
        if callable(html_callable):
            html = html_callable()
            if isinstance(html, str):
                return html
        raise LookupError(
            f"Unable to fetch HTML for accession {accession_number} (source_url={source_url})."
        )


class FilingDownloader:
    """Download SEC filing HTML into `data/raw` with cache support."""

    TBD_ACCESSION = "TBD_RETRIEVE_VIA_EDGARTOOLS"
    JNJ_CUTOFF_DATE = date(2025, 1, 1)

    def __init__(
        self,
        *,
        raw_dir: Path | str = Path("data/raw"),
        manifest_path: Path | str = Path("fixtures/manifest.csv"),
        edgar_client: EdgarClientProtocol | None = None,
        sec_user_agent: str | None = None,
    ) -> None:
        _configure_stdout_logger()

        user_agent = sec_user_agent or os.getenv("SEC_USER_AGENT")
        if not user_agent:
            raise EnvironmentError("SEC_USER_AGENT environment variable is required.")

        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = Path(manifest_path)
        self.edgar_client = edgar_client or EdgartoolsClient()
        self.edgar_client.set_identity(user_agent)

    def download(self, filings_or_manifest: Sequence[FilingMetadata] | Path | str) -> list[FilingMetadata]:
        """Download filings from explicit metadata list or a manifest CSV path."""
        filings = (
            self.load_manifest(filings_or_manifest)
            if isinstance(filings_or_manifest, (Path, str))
            else list(filings_or_manifest)
        )

        downloaded: list[FilingMetadata] = []
        for filing in filings:
            resolved = filing.model_copy(deep=True)
            # Keep manifest fixtures stable while resolving the J&J placeholder at runtime.
            if resolved.accession_number == self.TBD_ACCESSION:
                resolved.accession_number = self.edgar_client.resolve_latest_def14a_accession_before(
                    resolved.cik,
                    self.JNJ_CUTOFF_DATE,
                )

            output_path = self.raw_dir / self.build_output_filename(
                resolved.cik,
                resolved.accession_number,
            )
            if output_path.exists():
                # Cache hit: do not call the network path, only backfill raw_html_path.
                resolved.raw_html_path = str(output_path)
                self._log_event("cache_hit", resolved, output_path)
                downloaded.append(resolved)
                continue

            html = self.edgar_client.fetch_filing_html(
                cik=resolved.cik,
                accession_number=resolved.accession_number,
                source_url=resolved.edgar_url,
            )
            output_path.write_text(html, encoding="utf-8")
            resolved.raw_html_path = str(output_path)
            self._log_event("download", resolved, output_path, payload_size_bytes=len(html.encode("utf-8")))
            downloaded.append(resolved)

        return downloaded

    def download_from_default_manifest(self) -> list[FilingMetadata]:
        """Download filings listed in the default `fixtures/manifest.csv`."""
        return self.download(self.manifest_path)

    def load_manifest(self, manifest_path: Path | str) -> list[FilingMetadata]:
        """Load FilingMetadata entries from a manifest CSV file."""
        manifest = Path(manifest_path)
        filings: list[FilingMetadata] = []
        with manifest.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            for row_number, row in enumerate(reader, start=2):
                filing_date_raw = self._required_field(row, "filing_date", row_number)
                slot_raw = row.get("slot")
                slot = int(slot_raw) if slot_raw else None
                filings.append(
                    FilingMetadata(
                        slot=slot,
                        cik=self._required_field(row, "cik", row_number),
                        company_name=self._required_field(row, "company_name", row_number),
                        ticker=row.get("ticker"),
                        industry=row.get("industry"),
                        form_type=self._required_field(row, "form_type", row_number),
                        filing_date=date.fromisoformat(filing_date_raw),
                        accession_number=self._required_field(row, "accession_number", row_number),
                        edgar_url=self._required_field(row, "edgar_url", row_number),
                    )
                )
        return filings

    @staticmethod
    def build_output_filename(cik: str, accession_number: str) -> str:
        """Return deterministic local filename for raw filing HTML."""
        accession_normalized = accession_number.replace("-", "_")
        return f"{cik}_{accession_normalized}.html"

    @staticmethod
    def _required_field(row: Mapping[str, str | None], key: str, row_number: int) -> str:
        value = row.get(key)
        if value is None or value == "":
            raise ValueError(f"Manifest row {row_number} missing required field '{key}'.")
        return value

    def _log_event(
        self,
        event: str,
        filing: FilingMetadata,
        output_path: Path,
        *,
        payload_size_bytes: int | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "event": event,
            "cik": filing.cik,
            "accession_number": filing.accession_number,
            "raw_html_path": str(output_path),
        }
        if payload_size_bytes is not None:
            payload["payload_size_bytes"] = payload_size_bytes
        logger.info(json.dumps(payload, sort_keys=True))
