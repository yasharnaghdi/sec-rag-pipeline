#!/usr/bin/env python3
"""Build one-row-per-company compensation demo output for latest DEF 14A filings."""
from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from ingestion.edgar_folder_fetcher import FetchedFiling, fetch_latest_def14a
from ingestion.llm_comp_extractor import extract_company_comp_from_summary_table
from ingestion.metadata_model import BaseBlock, DocumentMetadata, HeadingBlock, TableBlock
from ingestion.sec_html_parser import SECHTMLParser

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

MANIFEST_PATH = Path("fixtures/sp500_manifest.csv")
CLIENT_INPUT_PATH = Path("fixtures/client_input.csv")
OUTPUT_ROOT = Path("output")
SUMMARY_HEADING_RE = re.compile(r"summary\s+compensation", re.IGNORECASE)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
NUMERIC_CLEAN_RE = re.compile(r"^-?\d+(\.\d+)?$")

KEY_RESULTS_FIELDS = [
    "cik",
    "company_name",
    "ticker",
    "filing_date",
    "accession_number",
    "filing_url",
    "fiscal_year",
    "ceo_name",
    "ceo_salary",
    "ceo_total",
    "cfo_name",
    "cfo_salary",
    "cfo_total",
    "coo_name",
    "coo_salary",
    "coo_total",
    "other1_name",
    "other1_title",
    "other1_salary",
    "other1_total",
    "other2_name",
    "other2_title",
    "other2_salary",
    "other2_total",
    "source_table_block_id",
    "source_section_id",
    "llm_model",
    "llm_confidence",
    "status",
    "error",
]

BATCH_LOG_FIELDS = [
    "cik",
    "company_name",
    "ticker",
    "filing_date",
    "accession_number",
    "status",
    "error",
    "source_table_block_id",
    "source_section_id",
    "llm_confidence",
    "timestamp",
]


@dataclass(frozen=True)
class CompanySeed:
    cik: str
    company_name: str
    ticker: str
    fiscal_year: str


def _digits_only(value: str) -> str:
    return re.sub(r"\D", "", value)


def _normalized_row(raw_row: dict[str, object]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for key, value in raw_row.items():
        normalized[key] = str(value).strip() if value is not None else ""
    return normalized


def _row_sort_key(row: dict[str, str]) -> tuple[str, str]:
    return (row.get("filing_date", ""), row.get("accession_number", ""))


def _load_company_seeds(limit: int) -> list[CompanySeed]:
    if MANIFEST_PATH.exists():
        with MANIFEST_PATH.open("r", encoding="utf-8", newline="") as handle:
            manifest_reader = csv.DictReader(handle)
            manifest_rows = [_normalized_row(dict(row)) for row in manifest_reader]

        ordered_ciks: list[str] = []
        latest_by_cik: dict[str, dict[str, str]] = {}
        for row in manifest_rows:
            if row.get("form_type", "").upper() != "DEF 14A":
                continue
            cik = _digits_only(row.get("cik", ""))
            if not cik:
                continue
            if cik not in latest_by_cik:
                ordered_ciks.append(cik)
                latest_by_cik[cik] = row
                continue
            if _row_sort_key(row) > _row_sort_key(latest_by_cik[cik]):
                latest_by_cik[cik] = row

        manifest_seeds: list[CompanySeed] = []
        for cik in ordered_ciks[:limit]:
            row = latest_by_cik[cik]
            manifest_seeds.append(
                CompanySeed(
                    cik=cik,
                    company_name=row.get("company_name", ""),
                    ticker=row.get("ticker", ""),
                    fiscal_year=row.get("fiscal_year", ""),
                )
            )
        return manifest_seeds

    if not CLIENT_INPUT_PATH.exists():
        raise FileNotFoundError(
            f"Could not find either {MANIFEST_PATH} or {CLIENT_INPUT_PATH} to source CIK list",
        )

    with CLIENT_INPUT_PATH.open("r", encoding="utf-8", newline="") as handle:
        client_reader = csv.reader(handle)
        rows: list[list[str]] = [row for row in client_reader if row]
    if not rows:
        return []

    first_value = rows[0][0].strip().lower()
    data_rows = rows[1:] if first_value == "folder_id" else rows
    client_seeds: list[CompanySeed] = []
    seen: set[str] = set()
    for raw_row in data_rows:
        cik = _digits_only(raw_row[0])
        if not cik or cik in seen:
            continue
        seen.add(cik)
        client_seeds.append(CompanySeed(cik=cik, company_name="", ticker="", fiscal_year=""))
        if len(client_seeds) >= limit:
            break
    return client_seeds


def _build_document_metadata(seed: CompanySeed, filing: FetchedFiling) -> DocumentMetadata:
    filing_date = filing.filing_date
    if filing_date is None:
        raise ValueError("Filing date is missing in fetched filing metadata")

    accession = filing.accession_number
    accession_slug = accession.replace("-", "_")
    return DocumentMetadata(
        document_id=f"{seed.cik}_{accession_slug}",
        cik=seed.cik,
        company_name=seed.company_name,
        form_type="DEF 14A",
        filing_date=filing_date,
        accession_number=accession,
        source_url=filing.filing_url,
        fiscal_year_end=None,
        raw_html_path=str(filing.cache_path),
    )


def _find_summary_comp_table(blocks: list[BaseBlock]) -> TableBlock | None:
    headings = [block for block in blocks if isinstance(block, HeadingBlock)]
    tables = [block for block in blocks if isinstance(block, TableBlock)]
    if not tables:
        return None

    def heading_quality(text: str) -> int:
        lowered = text.lower().strip()
        if "summary compensation table" in lowered and len(lowered) <= 160:
            return 3
        if SUMMARY_HEADING_RE.search(lowered) and len(lowered) <= 100:
            return 2
        if SUMMARY_HEADING_RE.search(lowered):
            return 1
        return 0

    def table_signature_score(table: TableBlock) -> int:
        preview_rows = table.rows[:10]
        preview_text = " ".join(" ".join(row).lower() for row in preview_rows)
        score = 0
        if "summary compensation table" in preview_text:
            score += 3
        if "name and principal position" in preview_text:
            score += 4
        elif (
            "name" in preview_text and "principal position" in preview_text
        ):
            score += 3
        elif "name" in preview_text and ("officer" in preview_text or "position" in preview_text):
            score += 2
        if "salary" in preview_text:
            score += 2
        if "total" in preview_text:
            score += 2
        if "chief executive officer" in preview_text or " ceo" in preview_text:
            score += 1
        if len(table.rows) < 5:
            score -= 2
        if YEAR_RE.search(preview_text):
            score += 1
        return score

    best_table: TableBlock | None = None
    best_score = -1

    matching_headings = [heading for heading in headings if heading_quality(heading.text) > 0]
    for heading in matching_headings:
        quality = heading_quality(heading.text)
        candidate_tables = [
            table for table in tables if table.order_index > heading.order_index and table.order_index - heading.order_index <= 40
        ]
        for table in candidate_tables:
            score = table_signature_score(table) + quality
            if table.section_id == heading.id:
                score += 2
            if score > best_score:
                best_score = score
                best_table = table

    if best_table is not None and best_score >= 4:
        return best_table

    for table in tables:
        score = table_signature_score(table)
        if score > best_score:
            best_score = score
            best_table = table
    if best_score >= 5:
        return best_table
    return None


def _extract_year_candidates(rows: list[list[str]]) -> list[int]:
    years: list[int] = []
    for row in rows[:8]:
        for cell in row:
            for match in YEAR_RE.finditer(cell):
                year = int(match.group(0))
                if 1990 <= year <= 2100:
                    years.append(year)
    return years


def _infer_fiscal_year(seed_fiscal_year: str, table: TableBlock | None) -> str:
    if table is not None:
        candidates = _extract_year_candidates(table.rows)
        if candidates:
            return str(max(candidates))
    return seed_fiscal_year


def _table_to_compact_tsv(rows: list[list[str]], *, max_rows: int = 240, max_cols: int = 64) -> str:
    tsv_lines: list[str] = []
    for row in rows[:max_rows]:
        normalized_cells = [cell.replace("\n", " ").strip() for cell in row[:max_cols]]
        tsv_lines.append("\t".join(normalized_cells))
    return "\n".join(tsv_lines)


def _clean_numeric(value: str | None) -> str:
    if value is None:
        return ""
    text = value.strip()
    if not text:
        return ""
    if text.lower() in {"none", "null", "n/a", "na", "unknown"}:
        return ""
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    text = text.replace("$", "").replace(",", "")
    text = text.replace(" ", "")
    text = text.replace("—", "").replace("–", "")
    if not NUMERIC_CLEAN_RE.fullmatch(text):
        text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in {"-", ".", "-."}:
        return ""
    if not NUMERIC_CLEAN_RE.fullmatch(text):
        return ""
    return text


def _role_fields(role_payload: object) -> tuple[str, str, str]:
    if not isinstance(role_payload, dict):
        return "", "", ""
    name = str(role_payload.get("name", "") or "").strip()
    salary = _clean_numeric(str(role_payload.get("salary", "") or ""))
    total = _clean_numeric(str(role_payload.get("total", "") or ""))
    return name, salary, total


def _other_fields(others_payload: object, index: int) -> tuple[str, str, str, str]:
    if not isinstance(others_payload, list) or index >= len(others_payload):
        return "", "", "", ""
    raw = others_payload[index]
    if not isinstance(raw, dict):
        return "", "", "", ""
    name = str(raw.get("name", "") or "").strip()
    title = str(raw.get("title", "") or "").strip()
    salary = _clean_numeric(str(raw.get("salary", "") or ""))
    total = _clean_numeric(str(raw.get("total", "") or ""))
    return name, title, salary, total


def _comp_table_prompt_payload(table_block: TableBlock) -> str:
    compact_tsv = _table_to_compact_tsv(table_block.rows)
    if compact_tsv:
        return f"{table_block.linearized_text}\n\nCompact TSV (first rows):\n{compact_tsv}"
    return table_block.linearized_text


def _safe_confidence(value: object) -> float:
    if isinstance(value, (int, float, str)):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _base_row(seed: CompanySeed) -> dict[str, str]:
    return {
        "cik": seed.cik,
        "company_name": seed.company_name,
        "ticker": seed.ticker,
        "filing_date": "",
        "accession_number": "",
        "filing_url": "",
        "fiscal_year": seed.fiscal_year,
        "ceo_name": "",
        "ceo_salary": "",
        "ceo_total": "",
        "cfo_name": "",
        "cfo_salary": "",
        "cfo_total": "",
        "coo_name": "",
        "coo_salary": "",
        "coo_total": "",
        "other1_name": "",
        "other1_title": "",
        "other1_salary": "",
        "other1_total": "",
        "other2_name": "",
        "other2_title": "",
        "other2_salary": "",
        "other2_total": "",
        "source_table_block_id": "",
        "source_section_id": "",
        "llm_model": "",
        "llm_confidence": "",
        "status": "",
        "error": "",
    }


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("--limit", type=int, default=50)
    cli.add_argument("--batch-label", default="b01")
    cli.add_argument("--model", default="gpt-4o-mini")
    args = cli.parse_args()

    load_dotenv()
    if not os.getenv("SEC_USER_AGENT"):
        raise SystemExit("SEC_USER_AGENT environment variable is required")
    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY environment variable is required")

    limit = max(1, args.limit)
    output_dir = OUTPUT_ROOT / f"batch_{args.batch_label}"
    output_dir.mkdir(parents=True, exist_ok=True)
    key_results_path = output_dir / "key_results.csv"
    batch_log_path = output_dir / "batch_log.csv"

    seeds = _load_company_seeds(limit)
    if not seeds:
        raise SystemExit("No CIK seeds found for batch run")
    seeds = seeds[:limit]

    parser = SECHTMLParser()
    key_rows: list[dict[str, str]] = []
    log_rows: list[dict[str, str]] = []
    ceo_total_populated = 0

    for index, seed in enumerate(seeds, start=1):
        row = _base_row(seed)
        status = "success"
        error = ""
        source_table_block_id = ""
        source_section_id = ""
        llm_confidence_value = ""
        filing_date_value = ""
        accession_number_value = ""
        try:
            filing = fetch_latest_def14a(seed.cik)
            if filing.filing_date is not None:
                filing_date_value = filing.filing_date.isoformat()
            accession_number_value = filing.accession_number
            row["filing_date"] = filing_date_value
            row["accession_number"] = accession_number_value
            row["filing_url"] = filing.filing_url

            metadata = _build_document_metadata(seed, filing)
            blocks = parser.parse(filing.raw_html, metadata)
            table_block = _find_summary_comp_table(blocks)
            if table_block is None:
                status = "summary_table_not_found"
                error = "Could not locate Summary Compensation Table from heading/table sequence"
            else:
                source_table_block_id = table_block.id
                source_section_id = table_block.section_id
                row["source_table_block_id"] = source_table_block_id
                row["source_section_id"] = source_section_id
                row["fiscal_year"] = _infer_fiscal_year(seed.fiscal_year, table_block)

                llm_payload = extract_company_comp_from_summary_table(
                    company_name=seed.company_name,
                    cik=seed.cik,
                    filing_date=filing_date_value,
                    accession_number=accession_number_value,
                    table_text=_comp_table_prompt_payload(table_block),
                    model=args.model,
                )
                ceo_name, ceo_salary, ceo_total = _role_fields(llm_payload.get("ceo"))
                cfo_name, cfo_salary, cfo_total = _role_fields(llm_payload.get("cfo"))
                coo_name, coo_salary, coo_total = _role_fields(llm_payload.get("coo"))
                other1_name, other1_title, other1_salary, other1_total = _other_fields(
                    llm_payload.get("others"),
                    0,
                )
                other2_name, other2_title, other2_salary, other2_total = _other_fields(
                    llm_payload.get("others"),
                    1,
                )

                confidence = _safe_confidence(llm_payload.get("confidence", 0.0))
                llm_confidence_value = f"{confidence:.4f}"

                row.update(
                    {
                        "ceo_name": ceo_name,
                        "ceo_salary": ceo_salary,
                        "ceo_total": ceo_total,
                        "cfo_name": cfo_name,
                        "cfo_salary": cfo_salary,
                        "cfo_total": cfo_total,
                        "coo_name": coo_name,
                        "coo_salary": coo_salary,
                        "coo_total": coo_total,
                        "other1_name": other1_name,
                        "other1_title": other1_title,
                        "other1_salary": other1_salary,
                        "other1_total": other1_total,
                        "other2_name": other2_name,
                        "other2_title": other2_title,
                        "other2_salary": other2_salary,
                        "other2_total": other2_total,
                        "llm_model": args.model,
                        "llm_confidence": llm_confidence_value,
                    }
                )
                if ceo_total:
                    ceo_total_populated += 1
        except Exception as exc:  # pragma: no cover - runtime path
            status = "failed"
            error = str(exc)
            log.exception("[%s/%s] CIK %s failed", index, len(seeds), seed.cik)

        row["status"] = status
        row["error"] = error
        key_rows.append(row)
        log_rows.append(
            {
                "cik": seed.cik,
                "company_name": seed.company_name,
                "ticker": seed.ticker,
                "filing_date": filing_date_value,
                "accession_number": accession_number_value,
                "status": status,
                "error": error,
                "source_table_block_id": source_table_block_id,
                "source_section_id": source_section_id,
                "llm_confidence": llm_confidence_value,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        log.info(
            "[%s/%s] CIK %s (%s) -> %s",
            index,
            len(seeds),
            seed.cik,
            seed.ticker,
            status,
        )

    with key_results_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=KEY_RESULTS_FIELDS)
        writer.writeheader()
        writer.writerows(key_rows)

    with batch_log_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BATCH_LOG_FIELDS)
        writer.writeheader()
        writer.writerows(log_rows)

    log.info("Wrote %s rows to %s", len(key_rows), key_results_path)
    log.info("Wrote %s rows to %s", len(log_rows), batch_log_path)
    log.info("CEO total populated for %s/%s companies", ceo_total_populated, len(seeds))

    if ceo_total_populated < 30:
        raise SystemExit(
            f"CEO total populated for {ceo_total_populated}/{len(seeds)} companies; required at least 30",
        )


if __name__ == "__main__":
    main()
