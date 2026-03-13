# `run_batch50_key_results.py` Workflow (Agent Reference)

Last updated: 2026-03-13

This document captures the actual execution path in `scripts/run_batch50_key_results.py` so future agents can make targeted changes without re-deriving control flow.

## 1. Entry Point and CLI Contract

`main()` parses these user-facing controls:

- `--input`: CSV with `cik` (or fallback first column / `folder_id`)
- `--batch-label`: output subdirectory under `output/`
- `--limit`: max rows from input CIK list
- `--model`: LLM model for fallback extractors
- `--no-db`: disable Postgres chunk writes
- `--fiscal-year-start` + `--fiscal-year-end`: optional supplemental year sweep (must be provided together)

It then creates:

- `output/<batch>/key_results.csv`
- `output/<batch>/batch_log.csv`
- `output/<batch>/batch_log_failed.csv`
- `output/<batch>/grants_plan_based_master.csv`
- `output/<batch>/outstanding_equity_awards_master.csv`
- `output/<batch>/compensation_table_master.csv`
- per-`cik`/year split directories for grants, outstanding equity, and compensation.

## 2. Base Per-CIK Flow

For each CIK, `main()` calls `_call_process_cik(...)`, which forwards to `process_cik(...)` while preserving compatibility with monkeypatched legacy signatures.

`process_cik(...)` stages:

1. Acquire filing:
- Uses `filing_override` when provided (supplemental mode), else `_fetch_latest_def14a(cik)`.
2. Parse:
- Builds `DocumentMetadata`, then parses HTML through `SECHTMLParser().parse(...)` into typed `BaseBlock` objects.
3. CD&A extraction:
- Calls `cda_extractor.extract_cda(...)` (non-fatal; failures log warning and continue with defaults).
4. Grants table path:
- `_locate_grants_table(...)`
- Deterministic extraction via `det_extractor.extract_grants_plan_based(...)`
- LLM fallback via `extract_grants_from_plan_based_table(...)` only when deterministic payload is empty but table exists.
5. Outstanding equity awards path:
- `_locate_outstanding_equity_awards_table(...)`
- Deterministic extraction via `det_extractor.extract_equity_awards(...)`
- LLM fallback via `extract_outstanding_equity_awards_table(...)` only when deterministic payload is empty but table exists.
6. Summary compensation path:
- `_locate_comp_table(...)`
- Deterministic extraction via `extract_summary_compensation(...)` + `_normalize_det_comp_rows(...)`
- If deterministic payload is missing and table exists, LLM fallback via `extract_company_comp_from_summary_table(...)` with `_build_llm_comp_table_text(...)`.
7. Role collapse:
- `_collapse_to_roles(...)` maps to `ceo/cfo/coo/other1/other2` using title/name keyword matching and most-recent-year selection.
8. Row materialization:
- Builds one `key_results.csv` row + one batch log row + supplemental master rows (grants/comp/equity).
9. Optional DB write:
- If `DB_URL` is present and `skip_db=False`, runs `SECChunker().chunk_blocks(...)` and `ChunkWriter().write_chunks(...)`.
- DB write failures are warning-only (non-fatal).

Any per-CIK error returns `_failed(...)`, which still produces a complete failed row and log row instead of raising.

## 3. Supplemental Fiscal-Year Sweep

When `--fiscal-year-start/--fiscal-year-end` are provided:

- Base run still executes once per CIK for `key_results.csv`.
- For each year in range, `main()` fetches `fetcher.fetch_def14a_for_fiscal_year(cik, year)`.
- It then calls `_call_process_cik(...)` with:
  - `filing_override=<year filing>`
  - `fiscal_year_start=year`
  - `fiscal_year_end=year`
  - `skip_db=True` (explicitly avoids duplicate DB writes)
- Supplemental outputs feed the master/per-year grants/comp/equity CSVs.
- Supplemental status `failed` is remapped to `skipped` for selected non-actionable cases (`no_comp_table_located`, `target_year_not_in_comp_table`).

## 4. Post-Loop Finalization

After all CIKs:

1. Writes grouped per-`cik`/year CSV files from accumulated master rows.
2. Writes `batch_log_failed.csv` as subset where `status == failed`.
3. Logs run summary counters.
4. Enforces coverage gate:
- exits `1` when `ceo_total_populated < MIN_CEO_COVERAGE`
- exits `0` otherwise.

## 5. Helper Families Worth Knowing

- Signature compatibility wrappers:
  - `_call_process_cik(...)`
  - `_extract_summary_compensation_rows(...)`
- Compensation row cleaning/normalization:
  - `_clean_llm_comp_row(...)`
  - `_expand_multi_year_comp_row(...)`
  - `_normalize_det_comp_rows(...)`
- Table location heuristics:
  - `_locate_comp_table(...)`
  - `_locate_grants_table(...)`
  - `_locate_outstanding_equity_awards_table(...)`
- Fiscal-year filtering:
  - `_filter_det_rows_by_fiscal_year(...)`
  - `_is_within_fiscal_year_range(...)`
  - `_table_contains_fiscal_year(...)`

## 6. Observed Risks (Review Notes)

1. Coverage threshold is absolute, not scaled to `--limit`.
- `MIN_CEO_COVERAGE` is fixed at `30`.
- Final gate checks `ceo_total_populated < MIN_CEO_COVERAGE`.
- Small smoke runs (for example `--limit 3`) can still exit non-zero even with perfect extraction.
2. Fallback metadata date is non-deterministic when filing date is missing.
- `DocumentMetadata.filing_date` uses `filing.filing_date or date.today()`.
- Two runs on different days can produce different metadata when source date is absent.
