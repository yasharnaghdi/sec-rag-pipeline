from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ingestion.downloader import FilingDownloader
from ingestion.metadata_model import FilingMetadata


class FakeEdgarClient:
    def __init__(self, *, resolved_accession: str = "000200406-24-000111") -> None:
        self.resolved_accession = resolved_accession
        self.identity_calls: list[str] = []
        self.resolve_calls: list[tuple[str, date]] = []
        self.fetch_calls: list[tuple[str, str, str]] = []

    def set_identity(self, user_agent: str) -> None:
        self.identity_calls.append(user_agent)

    def resolve_latest_def14a_accession_before(self, cik: str, cutoff_date: date) -> str:
        self.resolve_calls.append((cik, cutoff_date))
        return self.resolved_accession

    def fetch_filing_html(self, cik: str, accession_number: str, source_url: str) -> str:
        self.fetch_calls.append((cik, accession_number, source_url))
        return f"<html><body>{cik}-{accession_number}</body></html>"


@pytest.fixture(autouse=True)
def _sec_user_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEC_USER_AGENT", "sec-rag-tests/1.0 (unit@test.local)")


def _sample_metadata(*, accession_number: str = "0001308179-24-000010") -> FilingMetadata:
    return FilingMetadata(
        slot=2,
        cik="320193",
        company_name="Apple Inc",
        ticker="AAPL",
        industry="Large-Cap Technology",
        form_type="DEF 14A",
        filing_date=date(2024, 1, 11),
        accession_number=accession_number,
        edgar_url="https://www.sec.gov/Archives/edgar/data/320193/example.htm",
    )


def test_cache_hit_skips_edgartools_download(tmp_path: Path) -> None:
    edgar_client = FakeEdgarClient()
    downloader = FilingDownloader(raw_dir=tmp_path, edgar_client=edgar_client)
    metadata = _sample_metadata()

    cached_file = tmp_path / FilingDownloader.build_output_filename(
        metadata.cik,
        metadata.accession_number,
    )
    cached_file.write_text("<html>cached</html>", encoding="utf-8")

    downloaded = downloader.download([metadata])

    assert len(downloaded) == 1
    assert downloaded[0].raw_html_path == str(cached_file)
    assert edgar_client.resolve_calls == []
    assert edgar_client.fetch_calls == []


def test_tbd_accession_is_resolved_via_edgartools(tmp_path: Path) -> None:
    edgar_client = FakeEdgarClient(resolved_accession="000200406-24-000222")
    downloader = FilingDownloader(raw_dir=tmp_path, edgar_client=edgar_client)
    metadata = _sample_metadata(accession_number=FilingDownloader.TBD_ACCESSION)

    downloaded = downloader.download([metadata])

    assert len(downloaded) == 1
    assert downloaded[0].accession_number == "000200406-24-000222"
    assert len(edgar_client.resolve_calls) == 1


def test_missing_sec_user_agent_raises_environment_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(EnvironmentError):
        FilingDownloader(raw_dir=tmp_path, edgar_client=FakeEdgarClient())


def test_output_filename_is_deterministic() -> None:
    cik = "320193"
    accession = "0001308179-24-000010"
    assert FilingDownloader.build_output_filename(cik, accession) == "320193_0001308179_24_000010.html"
    assert FilingDownloader.build_output_filename(cik, accession) == "320193_0001308179_24_000010.html"


def test_returned_metadata_has_raw_html_path(tmp_path: Path) -> None:
    edgar_client = FakeEdgarClient()
    downloader = FilingDownloader(raw_dir=tmp_path, edgar_client=edgar_client)
    metadata = _sample_metadata()

    downloaded = downloader.download([metadata])

    expected_path = tmp_path / "320193_0001308179_24_000010.html"
    assert expected_path.exists()
    assert downloaded[0].raw_html_path == str(expected_path)

