# SEC RAG Batch Pipeline Agent Handoff

Last updated: 2026-03-09
Primary script: `scripts/run_batch50_key_results.py`

This document is a persistent context handoff for agents working on DEF 14A
extraction paths (summary compensation + grants of plan-based awards).

## 1. Pipeline Purpose

Per CIK, batch now writes:

- `output/<batch_label>/key_results.csv` (one row per CIK, role-keyed comp)
- `output/<batch_label>/batch_log.csv` (diagnostics)
- `output/<batch_label>/grants_plan_based_master.csv` (all grants rows)
- `output/<batch_label>/grants_plan_based_by_cik_year/<cik>_<fiscal_year>.csv`

## 2. End-to-End Flow

Per CIK, `process_cik(...)` does:

1. Acquire filing via `_fetch_latest_def14a(cik)`
2. Parse HTML into blocks with `SECHTMLParser().parse(...)`
3. Grants path:
   - locate grants table with `_locate_grants_table(...)`
   - deterministic extract via `extract_grants_plan_based(...)`
   - if deterministic has no payload and table exists, run grants LLM fallback
4. Summary comp path:
   - locate comp table with `_locate_comp_table(...)`
   - deterministic summary extraction
   - if deterministic has no payload and table exists, run LLM fallback
5. Write key results + logs + grants outputs

Important: grants and summary-comp are independent. Summary-comp failure can still
produce grants CSV rows for the same CIK.

## 3. Grants Table Locator (Deterministic Scoring)

File: `scripts/run_batch50_key_results.py`, function `_locate_grants_table(...)`.

Current grants locator behavior:

- Scores table candidates (heading-linked first, then nearby, then global fallback).
- Uses required positive terms in scoring:
  - `grant`
  - `incentive plan award`
- Uses grants structure/schema checks (grant date + payout triplets + grants-specific columns).
- Includes additional grant-type cues in body text (`AIA`, `PRSU`, `Time-Lapse RSU`,
  `Stock Option`, `Incentive Plan` variants).
- Rejects likely non-grants tables using ownership/peer-return hints.

Thresholds:

- heading-linked / nearby candidates require score `>= 8`
- global fallback candidates require score `>= 10`

## 4. Grants Deterministic Extraction

File: `ingestion/comp_table_extractor.py`, function `extract_grants_plan_based(...)`.

Key details:

- Supports explicit selected table extraction (`selected_table=...`) without requiring
  heading resolution.
- Merges heading-based rows and explicit-table rows, deduping by `(table_block_id, source_row_index)`.
- Handles `header_row_count=0` with grants-specific header inference from first rows.
- Preserves non-equity vs equity triplets via header group context propagation.
- Keeps duplicate mapped columns and chooses best value per canonical field at row mapping time.
- Normalizes split currency cells (`$` + adjacent numeric cell).
- Preserves row granularity; same name may appear on multiple rows.
- Row-shape normalization carries person name forward across grant-type-only rows.
- `grant_type` is preserved as source wording (no canonical remap).

## 5. Grants LLM Fallback

File: `ingestion/llm_comp_extractor.py`, function `extract_grants_from_plan_based_table(...)`.

- Invoked only when grants table is found but deterministic grants payload is empty.
- Uses same retry/validation routing pattern as summary-comp LLM extractor.
- Output model: `CompanyGrantsResult` with `GrantPlanAwardRecord` rows.
- Prompt explicitly requires preserving `grant_type` source wording and keeping non-equity/equity triplets separate.

## 6. Grants Output Schema

CSV columns are fixed in `GRANTS_OUTPUT_COLUMNS` and written in this exact order:

1. `Name`
2. `Grant Type`
3. `Grant Date`
4. `Estimated future payouts under non-equity incentive plan awards (Threshold)`
5. `Estimated future payouts under non-equity incentive plan awards (Target)`
6. `Estimated future payouts under non-equity incentive plan awards (Maximum)`
7. `Estimated future payouts under equity incentive plan awards (Threshold)`
8. `Estimated future payouts under equity incentive plan awards (Target)`
9. `Estimated future payouts under equity incentive plan awards (Maximum)`
10. `All other stock awards: Number of shares of stock or units`
11. `All other option awards: Number of securities underlying options`
12. `Exercise or base price of option awards`
13. `Grant date fair value of stock and option awards`

Notes:

- Missing values are blank.
- Deterministic `0.0` values are preserved (not dropped as blank).

## 7. Recent Grants Fixes (CIK 731802 + 4962)

1. Fixed 731802-style filings where parser reports `header_row_count=0` and heading gating
   previously zeroed deterministic rows.
2. Added robust explicit-table grants extraction path + multirow header inference.
3. Fixed 4962-style split-cell rows where currency symbol and numeric value are split across
   adjacent cells, which previously caused `$`/blank-heavy output.
4. Updated grants deterministic row serialization to preserve numeric zero values.

## 8. Current Known Issues

1. Summary compensation extraction still fails for some issuers even when grants extraction succeeds.
2. Batch exit code may be non-zero due to `MIN_CEO_COVERAGE`, even with valid grants outputs.
3. LLM paths can still return empty outputs on very noisy/large tables.

## 9. Operational Notes

Run command:

```bash
poetry run python scripts/run_batch50_key_results.py \
  --input fixtures/client_input.csv \
  --batch-label b01 \
  --limit 50 \
  --model gpt-4o-mini
```

Expected non-zero exit is common when CEO coverage threshold is unmet.
Always inspect all CSV outputs.

## 10. Quick Debug Commands

Check grants table selection and extracted rows for one CIK:

```bash
PYTHONPATH=. poetry run python - <<'PY'
from datetime import date
from ingestion.edgar_folder_fetcher import fetch_latest_def14a
from ingestion.metadata_model import DocumentMetadata
from ingestion.sec_html_parser import SECHTMLParser
from scripts.run_batch50_key_results import _locate_grants_table
from ingestion.comp_table_extractor import extract_grants_plan_based

cik='4962'
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
table, heading=_locate_grants_table(blocks)
print('heading:', heading.text if heading else None)
print('table found:', bool(table), 'header_row_count:', table.header_row_count if table else None)
rows=extract_grants_plan_based(blocks, {"cik": cik}, selected_table=table)
print('det rows:', len(rows))
print(rows[0] if rows else {})
PY
```

## 11. Handoff Rule of Thumb

When grants output is empty/mostly blanks:

1. Confirm `_locate_grants_table(...)` selected the expected table.
2. Inspect inferred header depth and column map quality on the selected table.
3. Check for split symbol/number cells (`$` in one column, number in the next).
4. Verify deterministic payload gate `_det_rows_have_grants_payload(...)`.
5. If deterministic has no payload but table is correct, inspect grants LLM fallback input/output.
