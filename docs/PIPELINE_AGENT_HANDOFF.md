# SEC RAG Batch Pipeline Agent Handoff

Last updated: 2026-03-11  
Stable branch: `Separate-files`  
Primary scripts: `scripts/run_batch50_key_results.py`, `scripts/validate_key_results.py`

This document is the persistent handoff for agents working on the current DEF 14A batch extraction baseline. It records the behavior that the last week of iterations converged on so later branches do not reintroduce earlier regressions.

## 1. Stable extraction contract

Per CIK, the stable branch should do all of the following:

1. Fetch the latest `DEF 14A` using the CIK only
2. Parse the SEC HTML into typed blocks, preserving tables and heading lineage
3. Run deterministic Summary Compensation Table extraction first
4. Use LLM fallback only when deterministic extraction cannot produce a usable payload
5. Split executive name and title cleanly, even when they appear in one cell
6. Collapse role rows by choosing the most recent fiscal year for each selected executive
7. Extract CD&A narrative text, token counts, and pay-for-performance flag
8. Validate `key_results.csv` so silent empty-column regressions fail fast

## 2. Why this branch exists

`Separate-files` is the integration branch where the individual work streams from the recent IDE-agent cycle were reconciled into one release-quality path.

Those work streams were roughly:

- acquisition fixes for CIK-only latest-proxy fetches
- parser and chunking fixes for SEC table preservation
- deterministic extractor hardening for mixed years and combined name/title cells
- LLM fallback work for filings that remain too noisy for the deterministic path
- batch validation work to prevent "status=ok" rows with empty CEO outputs
- CD&A extraction work so narrative evidence is carried alongside compensation results

Use this branch as the baseline when you need the most complete end-to-end extraction contract.

## 3. Most relevant source files

When you need to understand or defend the current behavior, read these first:

- `ingestion/edgar_folder_fetcher.py`
- `ingestion/sec_html_parser.py`
- `ingestion/sec_chunker.py`
- `ingestion/comp_table_extractor.py`
- `ingestion/llm_comp_extractor.py`
- `ingestion/cda_extractor.py`
- `scripts/run_batch50_key_results.py`
- `scripts/validate_key_results.py`

These are the files that conclude the reasoning from acquisition through validation.

## 4. Key invariants that must not regress

### Acquisition

- Fetch by CIK, not ticker or ad hoc folder naming
- Choose the latest available `DEF 14A`
- Always carry `cik`, `company_name`, `accession_number`, and `filing_url` into outputs

### Deterministic summary compensation extraction

- Prefer deterministic extraction over LLM fallback
- Support XBRL-aware table parsing and wide-header detection
- Preserve row-level fiscal year correctly on multi-year tables
- When name cells are blank on continuation rows, carry forward the executive identity only when the table structure indicates the row belongs to the same person
- Normalize currency values to digit-only strings
- Fix extraction defects in `comp_table_extractor.py`, not in downstream CSV post-processing, unless the issue is purely output formatting

### Role assignment

- The CEO, CFO, COO, and "other" slots must come from the most recent fiscal year available for that executive
- `fiscal_year` written to `key_results.csv` must reflect the row actually selected for the role
- `ceo_title` must not just repeat `ceo_name`

### Validation

- A row with `status=ok` must not have both empty `ceo_name` and empty `ceo_total`
- Numeric compensation fields must remain plain digits where present
- `pay_for_performance_flag` must be explicitly present as a boolean-like field

### Chunking

- Do not split tables in a way that loses row semantics
- There should be at least one chunk retaining CEO identity plus total compensation when that information exists in the filing
- There should be at least one chunk retaining CD&A pay-for-performance language when it exists

## 5. Current stable metrics

For the current hardened 50-CIK reference run on `Separate-files`:

- expected rows: `50`
- written rows: `50`
- non-empty `ceo_total`: `42`
- `cda_token_count > 0`: `42`

Treat those as branch-level acceptance metrics, not as a universal guarantee for all issuers.

## 6. Local release gate

The branch is considered ready to merge only when all of these succeed:

```bash
poetry install --no-interaction
poetry run ruff check .
poetry run mypy . --ignore-missing-imports
poetry run pytest tests/ -v --tb=short
poetry run python scripts/run_batch50_key_results.py \
  --input fixtures/client_input.csv --batch-label b01 --limit 50
poetry run python scripts/validate_key_results.py \
  --input output/b01/key_results.csv --expected-rows 50
```

CI currently covers the static checks and tests. The batch run and CSV validator remain required release-level verification steps before merge.

## 7. Generated artifact policy

Never commit generated batch output. Specifically avoid staging:

- `output/<batch_label>/key_results.csv`
- `output/<batch_label>/batch_log.csv`
- any derived CSV exported from a local validation run

These files are evidence for review, not source-controlled inputs.

## 8. Debug order when the branch regresses

If a batch result fails validation, inspect stages in this order:

1. acquisition in `ingestion/edgar_folder_fetcher.py`
2. parsing in `ingestion/sec_html_parser.py`
3. deterministic extraction in `ingestion/comp_table_extractor.py`
4. role collapse and output serialization in `scripts/run_batch50_key_results.py`
5. LLM fallback in `ingestion/llm_comp_extractor.py`
6. CD&A extraction in `ingestion/cda_extractor.py`
7. validator expectations in `scripts/validate_key_results.py`

Check `output/<batch_label>/batch_log.csv` before changing extractor logic. It usually tells you whether the failure came from fetch, parse, deterministic extraction, or fallback.
