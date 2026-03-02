"""EDGAR filing downloader via edgartools.

TODO (Phase 1 implementation):
- Implement download_proxy_filing(cik, year) -> tuple[Path, FilingMetadata]
- Implement list_filings(cik, form_type, start_year, end_year)
"""
from __future__ import annotations

from pathlib import Path

from core.models import FilingMetadata
from core.config import get_settings


class EdgarDownloader:
    """Wraps edgartools to fetch SEC filings and return FilingMetadata."""

    def __init__(self) -> None:
        settings = get_settings()
        self.download_dir = settings.edgar_download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # TODO: set_identity(settings.edgar_user_agent)

    def download_proxy_filing(
        self, cik: str, year: int
    ) -> tuple[Path, FilingMetadata]:
        """Download DEF 14A for a given CIK and fiscal year.

        Returns:
            (local_html_path, FilingMetadata) tuple for injection into SECProxyParser.
        """
        raise NotImplementedError("Phase 1: implement with edgartools Company().get_filings()")
