"""Shared test fixtures for the SEC RAG Pipeline test suite."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from core.models import FilingMetadata

# ── Fixtures ────────────────────────────────────────────────────────────────

APPLE_META = FilingMetadata(
    cik="0000320193",
    company_name="Apple Inc.",
    filing_date=date(2023, 1, 19),
    document_type="DEF14A",
    accession_number="0000320193-23-000006",
)


@pytest.fixture
def apple_meta() -> FilingMetadata:
    return APPLE_META


@pytest.fixture
def proxy_html_path(tmp_path: Path) -> Path:
    """Minimal synthetic SEC proxy HTML for parser tests."""
    html = """
    <!DOCTYPE html>
    <html>
    <body>
      <h2>Executive Compensation</h2>
      <p>The following table sets forth compensation paid to named executive officers.</p>
      <table>
        <tr><th>Name</th><th>Title</th><th>Salary</th><th>Bonus</th></tr>
        <tr><td>Tim Cook</td><td>CEO</td><td>$3,000,000</td><td>$82,348,753</td></tr>
        <tr><td>Luca Maestri</td><td>CFO</td><td>$1,000,000</td><td>$10,500,000</td></tr>
      </table>
      <p>Compensation decisions are made by the Compensation Committee of the Board of Directors.</p>
      <h2>Board of Directors</h2>
      <p>The Board consists of eight independent directors.</p>
      <p style="font-size:8pt"><sup>1</sup> Amounts reflect total compensation as reported in the Summary Compensation Table.</p>
    </body>
    </html>
    """
    p = tmp_path / "def14a.htm"
    p.write_text(html, encoding="utf-8")
    return p
