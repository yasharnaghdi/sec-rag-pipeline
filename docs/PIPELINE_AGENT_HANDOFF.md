# SEC RAG Batch Pipeline Agent Handoff

Last updated: 2026-03-08
Primary script: `scripts/run_batch50_key_results.py`

This document is a persistent context handoff for future agents working on
the DEF 14A compensation extraction pipeline.

## 1. Pipeline Purpose

Generate one output row per CIK in `output/<batch_label>/key_results.csv`,
with role-keyed executive compensation fields (CEO/CFO/COO/other1/other2),
plus batch diagnostics in `output/<batch_label>/batch_log.csv`.

## 2. End-to-End Flow

Per CIK, `process_cik(...)` does:

1. Acquire filing: `_fetch_latest_def14a(cik)`
2. Parse HTML into blocks: `SECHTMLParser().parse(...)`
3. Locate candidate compensation table: `_locate_comp_table(...)`
4. Deterministic extraction: `comp_table_extractor.extract_summary_compensation(...)`
5. If deterministic is not trustworthy, fallback to LLM
6. Collapse rows into roles and write `key_results` + `batch_log`

Key decision point:

- Deterministic is used only if:
  - extractor returns rows, and
  - `_det_rows_have_comp_payload(det_rows)` is true
- Else, if a table was located, LLM fallback runs.
- If no table was located, row is marked failed (`no_comp_table_located`).

## 3. Deterministic Extractor Constraints

File: `ingestion/comp_table_extractor.py`

Important behavior:

- `extract_summary_compensation(...)` first gets candidate rows via `_extract_table(...)`.
- It then keeps only table IDs whose mapped columns include at least one required
  numeric comp column (`salary`, `bonus`, `stock_awards`, `option_awards`, etc.).
- This reduces false positives but can zero out many rows if header mapping fails.

## 4. Table Locator Heuristics

File: `scripts/run_batch50_key_results.py`, function `_locate_comp_table(...)`.

Current logic:

- Uses heading signatures (`summary compensation`, etc.).
- Scores nearby/section-linked `TableBlock`s with:
  - header hints (`name and principal position`, `salary`, `total`, etc.)
  - structural checks (min rows/cols, data rows present)
  - reject hints (ownership/peer group patterns)
- If no heading-linked candidate qualifies, tries a global high-score table fallback.

This was tightened to avoid selecting footnote/ownership tables.

## 5. LLM Fallback Behavior

File: `ingestion/llm_comp_extractor.py`

- Uses OpenAI when `OPENAI_API_KEY` is present and non-dummy.
- Falls back to Ollama only when key is missing/dummy or on OpenAI auth failure.
- Returns structured `CompanyCompResult`.
- If result is empty, pipeline marks failure as `llm_extract_failed_empty_result`.

## 6. Recent Fixes (Critical Context)

Applied in this workspace:

1. Added true CIK-based latest DEF 14A fetch (`fetch_latest_def14a`) instead of
   overloading `folder_id=cik`.
2. Added `company_name` and `ticker` propagation in `FetchedFiling`.
3. Ensured `.env` is loaded for script + extractor paths, so OpenAI key is used.
4. Fixed CSV writer mismatch from extra fields leaking into `key_results`.
5. Hardened `_locate_comp_table` scoring to reduce wrong table selection.
6. Added failure guards:
   - `llm_extract_failed_empty_result`
   - `extraction_empty_after_mapping`
   so empty rows are not silently marked `ok`.

## 7. Why Many Rows Still Fall Back to LLM

Observed pattern:

- Deterministic often returns zero usable rows after filtering, or rows with
  placeholders (`$`, `($)`, empty strings) that fail payload checks.
- DEF 14A table formatting varies heavily across issuers.
- Header mapping in deterministic extractor is strict and brittle for merged/multirow headers.

Net effect:

- Most CIKs route to LLM fallback by design in current quality gate setup.

## 8. Known Remaining Issues

1. Some `ok` rows may still be partial (role mapping not ideal, CEO missing).
2. Deterministic role collapse can pick non-exec rows in edge cases.
3. Very large linearized tables can produce invalid/empty LLM outputs.
4. Batch exits non-zero when `ceo_total_populated < MIN_CEO_COVERAGE` even if many
   rows are otherwise valid.

## 9. Operational Notes

Run command (project root):

```bash
poetry run python scripts/run_batch50_key_results.py \
  --input fixtures/client_input.csv \
  --batch-label b01 \
  --limit 50 \
  --model gpt-4o-mini
```

Expected non-zero exit is common when coverage threshold is not met.
Always inspect output CSVs rather than relying only on exit code.

## 10. Quick Debug Commands

Status/method distribution:

```bash
python3 - <<'PY'
import csv
from collections import Counter
rows=list(csv.DictReader(open('output/b01/key_results.csv', newline='', encoding='utf-8')))
print('status', Counter(r['status'] for r in rows))
print('method', Counter(r['extraction_method'] for r in rows))
print('errors', Counter(r['error'] for r in rows if r['error']))
PY
```

Inspect selected comp table for one CIK:

```bash
PYTHONPATH=. poetry run python - <<'PY'
from datetime import date
from ingestion.edgar_folder_fetcher import fetch_latest_def14a
from ingestion.metadata_model import DocumentMetadata
from ingestion.sec_html_parser import SECHTMLParser
from scripts.run_batch50_key_results import _locate_comp_table

cik='1518621'
filing=fetch_latest_def14a(cik)
meta=DocumentMetadata(
    document_id=f"{cik}_{filing.accession_number.replace('-','')}",
    cik=cik,
    company_name=filing.company_name,
    form_type='DEF 14A',
    filing_date=filing.filing_date or date.today(),
    accession_number=filing.accession_number,
    source_url=filing.filing_url,
)
blocks=SECHTMLParser().parse(filing.raw_html, meta)
table, heading=_locate_comp_table(blocks)
print('heading:', heading.text if heading else None)
print('table head:', ' '.join(table.linearized_text.split())[:500] if table else None)
PY
```

## 11. Handoff Rule of Thumb

When debugging empty cells:

1. Check `key_results.status` and `error`.
2. Cross-check `batch_log` for `comp_heading_found`, `comp_table_found`, `det_rows`.
3. If `det_rows == 0` and `comp_table_found == True`, inspect table selection quality.
4. If LLM confidence is `0.0` with empty output, inspect `table_text` relevance/size.

