from __future__ import annotations

from types import SimpleNamespace

from scripts.batch_download import _accession_to_filename
from scripts.build_sp500_manifest import (
    build_ticker_cik_map,
    fetch_def14a_filings,
    fetch_sp500_tickers,
    infer_fiscal_year,
)


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, text: str = "", payload: object | None = None) -> None:
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> object:
        if self._payload is None:
            raise RuntimeError("No JSON payload")
        return self._payload


def test_infer_fiscal_year_jan_filing_returns_prior_year() -> None:
    assert infer_fiscal_year("2024-03-15") == "2023"


def test_infer_fiscal_year_sep_filing_returns_same_year() -> None:
    assert infer_fiscal_year("2024-09-01") == "2024"


def test_accession_to_filename_format() -> None:
    assert _accession_to_filename("320193", "0001308179-24-000010") == "0000320193_000130817924000010.html"


def test_fetch_sp500_tickers_returns_list_of_dicts(monkeypatch) -> None:
    html = """
    <html><body>
      <table id="constituents">
        <tr><th>Symbol</th><th>Security</th><th>SEC filings</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr>
        <tr><td>BRK.B</td><td>Berkshire Hathaway</td><td></td><td>Financials</td><td>Multi-Sector Holdings</td></tr>
      </table>
    </body></html>
    """

    def _fake_get(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeResponse(text=html)

    monkeypatch.setattr("scripts.build_sp500_manifest.requests.get", _fake_get)

    rows = fetch_sp500_tickers()

    assert isinstance(rows, list)
    assert rows == [
        {
            "ticker": "BRK-B",
            "company_name": "Berkshire Hathaway",
            "industry": "Multi-Sector Holdings",
        }
    ]


def test_build_ticker_cik_map_parses_edgar_json() -> None:
    payload = {
        "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
        "1": {"cik_str": 789019, "ticker": "MSFT", "title": "Microsoft Corp"},
    }

    session = SimpleNamespace(get=lambda *args, **kwargs: _FakeResponse(payload=payload))  # noqa: ARG005

    ticker_map = build_ticker_cik_map(session)

    assert ticker_map["AAPL"] == "0000320193"
    assert ticker_map["MSFT"] == "0000789019"


def test_fetch_def14a_filters_by_date_range() -> None:
    payload = {
        "filings": {
            "recent": {
                "form": ["DEF 14A", "DEF 14A", "10-K", "DEF 14A"],
                "filingDate": ["2024-03-01", "2022-12-31", "2024-02-01", "2025-07-01"],
                "accessionNumber": [
                    "0001308179-24-000010",
                    "0001308179-22-000010",
                    "0001308179-24-000011",
                    "0001308179-25-000012",
                ],
                "primaryDocument": ["a.htm", "b.htm", "c.htm", "d.htm"],
            }
        }
    }

    session = SimpleNamespace(get=lambda *args, **kwargs: _FakeResponse(payload=payload))  # noqa: ARG005

    filings = fetch_def14a_filings("0000320193", session)

    assert len(filings) == 1
    assert filings[0]["filing_date"] == "2024-03-01"
    assert filings[0]["accession_number"] == "0001308179-24-000010"
    assert filings[0]["edgar_url"].endswith("/a.htm")


def test_fetch_def14a_returns_empty_on_404() -> None:
    session = SimpleNamespace(get=lambda *args, **kwargs: _FakeResponse(status_code=404))  # noqa: ARG005

    assert fetch_def14a_filings("0000320193", session) == []
