"""Unit tests for ingestion.edgar_folder_fetcher.fetch_latest_def14a."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ingestion.edgar_folder_fetcher import fetch_latest_def14a


def _make_submissions(
    accession_numbers: list[str],
    forms: list[str],
    filing_dates: list[str],
    primary_documents: list[str],
    historical_files: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    """Build a minimal SEC submissions JSON payload for testing."""
    payload: dict[str, object] = {
        "filings": {
            "recent": {
                "accessionNumber": accession_numbers,
                "form": forms,
                "filingDate": filing_dates,
                "primaryDocument": primary_documents,
            },
        }
    }
    if historical_files:
        filings = payload["filings"]
        assert isinstance(filings, dict)
        filings["files"] = historical_files
    return payload


def _make_response(json_data: dict[str, object] | None = None, text: str = "") -> MagicMock:
    """Create a mock requests.Response object."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    if json_data is not None:
        resp.json.return_value = json_data
    resp.text = text
    return resp


class TestFetchLatestDef14aSelection:
    """Tests for correct filing selection logic."""

    def test_selects_most_recent_def14a_from_multiple(self, tmp_path: Path) -> None:
        """When multiple DEF 14A filings exist, return the most recent by date."""
        submissions = _make_submissions(
            accession_numbers=["0001234567-23-000001", "0001234567-22-000001"],
            forms=["DEF 14A", "DEF 14A"],
            filing_dates=["2023-04-15", "2022-04-20"],
            primary_documents=["proxy2023.htm", "proxy2022.htm"],
        )
        html_content = "<html><body>proxy 2023</body></html>"

        with (
            patch("ingestion.edgar_folder_fetcher.DATA_RAW_DIR", tmp_path),
            patch("ingestion.edgar_folder_fetcher._throttle"),
            patch("ingestion.edgar_folder_fetcher.requests.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.get.side_effect = [
                _make_response(json_data=submissions),
                _make_response(text=html_content),
            ]

            result = fetch_latest_def14a("1234567")

        assert result.accession_number == "0001234567-23-000001"
        assert result.filing_date == date(2023, 4, 15)
        assert result.raw_html == html_content

    def test_ignores_non_def14a_forms(self, tmp_path: Path) -> None:
        """10-K and other form types must be excluded from selection."""
        submissions = _make_submissions(
            accession_numbers=["0001234567-23-000099", "0001234567-23-000001"],
            forms=["10-K", "DEF 14A"],
            filing_dates=["2023-03-01", "2023-04-15"],
            primary_documents=["annual.htm", "proxy.htm"],
        )
        html_content = "<html><body>proxy</body></html>"

        with (
            patch("ingestion.edgar_folder_fetcher.DATA_RAW_DIR", tmp_path),
            patch("ingestion.edgar_folder_fetcher._throttle"),
            patch("ingestion.edgar_folder_fetcher.requests.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.get.side_effect = [
                _make_response(json_data=submissions),
                _make_response(text=html_content),
            ]

            result = fetch_latest_def14a("1234567")

        assert result.accession_number == "0001234567-23-000001"

    def test_reads_historical_submissions_when_recent_has_no_def14a(
        self,
        tmp_path: Path,
    ) -> None:
        """Historical submissions files are searched if recent lacks DEF 14A."""
        submissions = _make_submissions(
            accession_numbers=["0001234567-23-000099"],
            forms=["10-K"],
            filing_dates=["2023-03-01"],
            primary_documents=["annual.htm"],
            historical_files=[{"name": "CIK0001234567-submissions-001.json"}],
        )
        historical = _make_submissions(
            accession_numbers=["0001234567-22-000001"],
            forms=["DEF 14A"],
            filing_dates=["2022-04-20"],
            primary_documents=["proxy2022.htm"],
        )
        html_content = "<html><body>proxy 2022</body></html>"

        with (
            patch("ingestion.edgar_folder_fetcher.DATA_RAW_DIR", tmp_path),
            patch("ingestion.edgar_folder_fetcher._throttle"),
            patch("ingestion.edgar_folder_fetcher.requests.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.get.side_effect = [
                _make_response(json_data=submissions),
                _make_response(json_data=historical),
                _make_response(text=html_content),
            ]

            result = fetch_latest_def14a("1234567")

        assert result.accession_number == "0001234567-22-000001"
        assert result.raw_html == html_content

    def test_raises_when_no_def14a_found(self, tmp_path: Path) -> None:
        """ValueError is raised when no DEF 14A exists."""
        submissions = _make_submissions(
            accession_numbers=["0001234567-23-000099"],
            forms=["10-K"],
            filing_dates=["2023-03-01"],
            primary_documents=["annual.htm"],
        )

        with (
            patch("ingestion.edgar_folder_fetcher.DATA_RAW_DIR", tmp_path),
            patch("ingestion.edgar_folder_fetcher._throttle"),
            patch("ingestion.edgar_folder_fetcher.requests.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.get.return_value = _make_response(json_data=submissions)

            with pytest.raises(ValueError, match="No DEF 14A"):
                fetch_latest_def14a("1234567")

    def test_raises_for_invalid_cik(self) -> None:
        """ValueError is raised immediately for non-digit CIK."""
        with pytest.raises(ValueError, match="digit"):
            fetch_latest_def14a("not-a-cik")


class TestFetchLatestDef14aCaching:
    """Tests for local HTML cache behavior."""

    def test_cache_hit_skips_html_download(self, tmp_path: Path) -> None:
        """On cache hit, only the submissions index is fetched."""
        submissions = _make_submissions(
            accession_numbers=["0001234567-23-000001"],
            forms=["DEF 14A"],
            filing_dates=["2023-04-15"],
            primary_documents=["proxy.htm"],
        )
        cached_html = "<html><body>cached</body></html>"
        cik_padded = "0001234567"
        cache_file = tmp_path / f"{cik_padded}_000123456723000001.html"
        cache_file.write_text(cached_html, encoding="utf-8")

        with (
            patch("ingestion.edgar_folder_fetcher.DATA_RAW_DIR", tmp_path),
            patch("ingestion.edgar_folder_fetcher._throttle"),
            patch("ingestion.edgar_folder_fetcher.requests.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.get.return_value = _make_response(json_data=submissions)

            result = fetch_latest_def14a("1234567")

        assert result.raw_html == cached_html
        assert mock_session.get.call_count == 1

    def test_cache_miss_writes_file(self, tmp_path: Path) -> None:
        """On cache miss, the HTML file is written to DATA_RAW_DIR."""
        submissions = _make_submissions(
            accession_numbers=["0001234567-23-000001"],
            forms=["DEF 14A"],
            filing_dates=["2023-04-15"],
            primary_documents=["proxy.htm"],
        )
        html_content = "<html><body>fresh</body></html>"

        with (
            patch("ingestion.edgar_folder_fetcher.DATA_RAW_DIR", tmp_path),
            patch("ingestion.edgar_folder_fetcher._throttle"),
            patch("ingestion.edgar_folder_fetcher.requests.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.get.side_effect = [
                _make_response(json_data=submissions),
                _make_response(text=html_content),
            ]

            result = fetch_latest_def14a("1234567")

        assert result.cache_path.exists()
        assert result.cache_path.read_text(encoding="utf-8") == html_content


class TestFetchLatestDef14aThrottling:
    """Confirms SEC rate-limit compliance."""

    def test_throttle_called_per_http_request(self, tmp_path: Path) -> None:
        """_throttle() is called once per HTTP request."""
        submissions = _make_submissions(
            accession_numbers=["0001234567-23-000001"],
            forms=["DEF 14A"],
            filing_dates=["2023-04-15"],
            primary_documents=["proxy.htm"],
        )
        html_content = "<html><body>test</body></html>"

        with (
            patch("ingestion.edgar_folder_fetcher.DATA_RAW_DIR", tmp_path),
            patch("ingestion.edgar_folder_fetcher._throttle") as mock_throttle,
            patch("ingestion.edgar_folder_fetcher.requests.Session") as mock_session_cls,
        ):
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_session.get.side_effect = [
                _make_response(json_data=submissions),
                _make_response(text=html_content),
            ]

            fetch_latest_def14a("1234567")

        assert mock_throttle.call_count == 2
