from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

from ingestion.metadata_model import ProseBlock
from ingestion.sec_chunker import Chunk
from scripts import batch_ingest


def _configure_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    fixtures_dir = tmp_path / "fixtures"
    data_raw_dir = tmp_path / "data" / "raw"
    output_dir = tmp_path / "output"
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    data_raw_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(batch_ingest, "FIXTURES_DIR", fixtures_dir)
    monkeypatch.setattr(batch_ingest, "DATA_RAW_DIR", data_raw_dir)
    monkeypatch.setattr(batch_ingest, "OUTPUT_DIR", output_dir)
    monkeypatch.setattr(batch_ingest, "MANIFEST_PATH", fixtures_dir / "sp500_manifest.csv")
    monkeypatch.setattr(batch_ingest, "INGEST_LOG_PATH", output_dir / "sp500_ingest_log.csv")
    monkeypatch.setattr(batch_ingest, "CHUNKS_CSV_PATH", output_dir / "sp500_chunks.csv")

    return fixtures_dir, data_raw_dir, output_dir


def _write_manifest(path: Path) -> None:
    fieldnames = [
        "slot",
        "cik",
        "company_name",
        "ticker",
        "form_type",
        "filing_date",
        "accession_number",
        "source_url",
        "fiscal_year",
        "raw_html_path",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow(
            {
                "slot": "1",
                "cik": "320193",
                "company_name": "Apple Inc.",
                "ticker": "AAPL",
                "form_type": "DEF 14A",
                "filing_date": "2024-01-11",
                "accession_number": "0001308179-24-000010",
                "source_url": "https://www.sec.gov/example.htm",
                "fiscal_year": "2023",
                "raw_html_path": "",
            }
        )


def _run_single_filing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    _configure_paths(monkeypatch, tmp_path)
    _write_manifest(batch_ingest.MANIFEST_PATH)

    cache = batch_ingest._cache_path("320193", "0001308179-24-000010")
    cache.write_text("<html><body>stub</body></html>", encoding="utf-8")

    class _FakeParser:
        def parse(self, raw_html: str, metadata) -> list[ProseBlock]:  # noqa: ANN001
            _ = raw_html
            block_text = "Sample paragraph"
            return [
                ProseBlock(
                    document_id=metadata.document_id,
                    section_id="preamble",
                    order_index=0,
                    source_char_start=0,
                    source_char_end=len(block_text),
                    toc_page_range=None,
                    text=block_text,
                    token_count=2,
                )
            ]

    class _FakeChunker:
        def chunk_blocks(self, blocks: list[ProseBlock], metadata) -> list[Chunk]:  # noqa: ANN001
            return [
                Chunk(
                    source_block_id=blocks[0].id,
                    document_id=metadata.document_id,
                    section_id="preamble",
                    text="chunk text\nline two",
                    token_count=4,
                    chunk_index=0,
                    citation_string="citation",
                    toc_page_range=(10, 11),
                )
            ]

    monkeypatch.setattr(batch_ingest, "SECHTMLParser", _FakeParser)
    monkeypatch.setattr(batch_ingest, "SECChunker", _FakeChunker)
    monkeypatch.setattr(sys, "argv", ["batch_ingest.py"])

    batch_ingest.main()

    with batch_ingest.INGEST_LOG_PATH.open("r", encoding="utf-8", newline="") as handle:
        ingest_rows = list(csv.DictReader(handle))
    with batch_ingest.CHUNKS_CSV_PATH.open("r", encoding="utf-8", newline="") as handle:
        chunk_rows = list(csv.DictReader(handle))

    return ingest_rows, chunk_rows


def test_cache_path_format() -> None:
    path = batch_ingest._cache_path("320193", "0001308179-24-000010")
    assert path == Path("data/raw/0000320193_000130817924000010.html")


def test_load_already_ingested_returns_empty_set_when_no_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_paths(monkeypatch, tmp_path)

    assert batch_ingest._load_already_ingested() == set()


def test_load_already_ingested_skips_failed_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_paths(monkeypatch, tmp_path)

    with batch_ingest.INGEST_LOG_PATH.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=batch_ingest.INGEST_LOG_FIELDS)
        writer.writeheader()
        writer.writerow(
            {
                "ticker": "AAPL",
                "cik": "320193",
                "accession_number": "ok-1",
                "fiscal_year": "2023",
                "status": "success",
                "chunk_count": "10",
                "block_count": "8",
                "elapsed_seconds": "1.2",
                "flag": "",
                "timestamp": "2026-01-01T00:00:00+00:00",
            }
        )
        writer.writerow(
            {
                "ticker": "MSFT",
                "cik": "789019",
                "accession_number": "bad-1",
                "fiscal_year": "2023",
                "status": "failed",
                "chunk_count": "0",
                "block_count": "0",
                "elapsed_seconds": "0.3",
                "flag": "error",
                "timestamp": "2026-01-01T00:00:01+00:00",
            }
        )

    assert batch_ingest._load_already_ingested() == {"ok-1"}


def test_chunks_csv_fields_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _, chunk_rows = _run_single_filing(monkeypatch, tmp_path)

    assert len(chunk_rows) == 1
    assert set(batch_ingest.CHUNKS_FIELDS).issubset(chunk_rows[0].keys())


def test_ingest_log_fields_present(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    ingest_rows, _ = _run_single_filing(monkeypatch, tmp_path)

    assert len(ingest_rows) == 1
    assert set(batch_ingest.INGEST_LOG_FIELDS).issubset(ingest_rows[0].keys())
