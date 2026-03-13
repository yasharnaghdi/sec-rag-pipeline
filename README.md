# sec-rag-pipeline

![CI](https://img.shields.io/badge/CI-GitHub_Actions-181717?logo=githubactions&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-Private-lightgrey)

Production-grade SEC filing RAG pipeline for DEF 14A proxy statements and 10-K documents. The current release is optimized for proxy acquisition, compensation extraction, offline-safe local development, and auditable CSV outputs.

## Stable Baseline

`main` is the current stable integration branch and single source of truth for the compensation extraction workflow. The baseline this branch is meant to preserve is:

1. CIK-only acquisition of the latest `DEF 14A`
2. SEC HTML parsing into typed blocks
3. Deterministic summary compensation extraction, with LLM fallback only when needed
4. Role assignment keyed to the most recent fiscal year per executive
5. CD&A extraction with token counts and pay-for-performance flagging
6. Rule-based critical-section labeling for compensation coverage signals
7. Batch outputs validated by `scripts/validate_key_results.py`

For the cached 50-CIK reference output validated on `main`, the current local acceptance result is:

- `50/50` rows written
- `43/50` rows with non-empty `ceo_total`
- `43/50` rows with `cda_token_count > 0`
- no empty key identifiers in `key_results.csv`
- numeric compensation fields normalized to plain digit strings
- rule-based critical section flags available in batch rows

## What This Does

1. Acquire filings from SEC EDGAR and cache raw HTML locally.
2. Parse filing HTML into typed SEC blocks, including headings, prose, tables, XBRL values, and footnotes.
3. Locate compensation-relevant sections and the Summary Compensation Table.
4. Extract structured executive compensation data with a deterministic parser first, then an LLM fallback when needed.
5. Write batch artifacts to `output/<batch_label>/` and optionally persist chunks to PostgreSQL.
6. Validate the resulting `key_results.csv` before downstream retrieval, embedding, or analyst review.

## Architecture

```text
fixtures/client_input.csv or fixtures/sp500_manifest.csv
                    |
                    v
      scripts/run_batch50_key_results.py
                    |
                    v
      ingestion/edgar_folder_fetcher.py
                    |
                    v
             data/raw/*.html
                    |
                    v
       ingestion/sec_html_parser.py
                    |
                    v
       typed SEC blocks + metadata
                    |
        +-----------+-----------+
        |                       |
        v                       v
deterministic extractor   LLM fallback extractor
comp_table_extractor.py   llm_comp_extractor.py
        |                       |
        +-----------+-----------+
                    |
                    v
     output/<batch>/key_results.csv
     output/<batch>/batch_log.csv
                    |
                    +--> scripts/validate_key_results.py
                    |
                    +--> chunking/indexing/storage phases
```

## Quick Start

1. Clone and enter the repo.
   ```bash
   git clone https://github.com/yasharnaghdi/sec-rag-pipeline.git
   cd sec-rag-pipeline
   ```
2. Create your local env file.
   ```bash
   cp .env.example .env
   ```
3. Fill in the minimum variables in `.env`.
   Required for local work: `EDGAR_USER_AGENT`, `SEC_USER_AGENT`, and either `OPENAI_API_KEY` or an Ollama setup.
4. Start infrastructure.
   ```bash
   docker compose up -d postgres qdrant
   ```
5. Install dependencies and run the test gate.
   ```bash
   poetry install
   poetry run pytest tests/ -v --tb=short
   ```
6. Run a smoke batch and validate it.
   ```bash
   poetry run python scripts/run_batch50_key_results.py \
     --input fixtures/client_input.csv --batch-label smoke --limit 3
   poetry run python scripts/validate_key_results.py \
     --input output/smoke/key_results.csv --expected-rows 3
   ```

## Branch Guide

The repo accumulated task-shaped branches during parser and extraction hardening. Treat them as historical intent markers, not as equally current release lines.

| Branch | Meaning |
| --- | --- |
| `main` | Current stable integration branch and the branch to base new work on. |
| `cleanup-finalize` | Stabilization branch merged through PR 22; keep only for audit traceability. |
| `Separate-files` | Historical audit baseline that matched pre-merge `main` during the stabilization review. |
| `fix/cik-only-acquisition` | Focused fix branch for latest-`DEF 14A` acquisition by CIK. |
| `feat/task4-hardening` | Hardening branch for summary compensation parsing, validation checks, and edge-case tests. |
| `feat/llm-comp-extractor-and-batch50` | LLM fallback and batch-runner development branch. |
| `feat/ollama-fallback-and-docker-compose` | Local runtime and model fallback branch. |

If you need the branch that reflects the current extraction contract, start from `main` and then inspect the files listed in [End-State Review Pointers](#end-state-review-pointers).

## One-Command Full Stack

Use the full Docker profile when you want PostgreSQL, Qdrant, Ollama, and the FastAPI app together:

```bash
docker compose --profile full up --build -d
```

The full profile exposes:

- `postgres` on `localhost:5432`
- `qdrant` on `localhost:6333`
- `ollama` on `localhost:11434`
- `app` on `localhost:8000`

## Input Format

`run_batch50_key_results.py` expects a one-column CSV, typically `fixtures/client_input.csv`.

| Column | Required | Example | Meaning |
| --- | --- | --- | --- |
| `folder_id` | yes | `320193` | Company identifier used by the batch runner. In the current workflow this is the CIK used to fetch the latest DEF 14A. |

CIK lookup reference: `https://www.sec.gov/edgar/searchedgar/companysearch`.

## Output Format

The current `output/<batch_label>/key_results.csv` schema contains 40 columns. They are grouped below by purpose.

| Columns | Meaning |
| --- | --- |
| `cik`, `company_name`, `ticker` | Company identifiers copied into each result row. |
| `filing_date`, `fiscal_year`, `accession_number`, `filing_url` | Filing lineage for the row that produced the extraction. |
| `ceo_name`, `ceo_title` | Selected CEO identity fields for the most recent fiscal year found in the filing. |
| `ceo_salary`, `ceo_bonus`, `ceo_stock_awards`, `ceo_option_awards`, `ceo_total` | CEO compensation values normalized to plain numeric strings. |
| `cfo_name`, `cfo_title`, `cfo_salary`, `cfo_total` | CFO identity and key compensation values. |
| `coo_name`, `coo_title`, `coo_salary`, `coo_total` | COO identity and key compensation values. |
| `other1_name`, `other1_title`, `other1_salary`, `other1_total` | First non-CEO/CFO/COO named executive selected by rank. |
| `other2_name`, `other2_title`, `other2_salary`, `other2_total` | Second non-CEO/CFO/COO named executive selected by rank. |
| `source_table_block_id`, `source_section_id` | The table block and section that supplied the extracted row. |
| `extraction_method`, `llm_model`, `llm_confidence` | Extraction provenance. `extraction_method` is `deterministic`, `llm`, or `failed`. |
| `has_exec_comp`, `exec_comp_token_count`, `has_cda`, `cda_token_count`, `pay_for_performance_flag`, `cda_section_found` | Executive-compensation and CD&A section coverage signals captured during the same filing pass. |
| `has_summary_comp`, `summary_comp_token_count`, `has_equity_awards`, `equity_awards_token_count`, `has_grants_plan_based`, `grants_plan_based_token_count`, `has_option_exercises`, `option_exercises_token_count`, `has_pension_benefits`, `pension_benefits_token_count`, `has_pay_vs_performance`, `pay_vs_performance_token_count` | Rule-based critical-section coverage flags and token counts for compensation-related tables and disclosure sections. |
| `status`, `error` | Final row status and any error message recorded by the batch runner. |

Companion file: `output/<batch_label>/batch_log.csv`

| Columns | Meaning |
| --- | --- |
| `cik`, `company_name`, `status`, `extraction_method` | Per-company execution result. |
| `block_count`, `table_count`, `comp_heading_found`, `comp_table_found` | Parsing and table-location diagnostics. |
| `det_rows`, `llm_confidence`, `cda_token_count`, `pay_for_performance_flag` | Extraction diagnostics. |
| `elapsed_seconds`, `error` | Runtime and failure detail. |

Generated artifacts under `output/` are local run products. Do not commit them, do not treat them as source of truth, and do not upload `key_results.csv` or related batch artifacts to the repository. The repo intentionally ignores `output/` in `.gitignore`.

## LLM Strategy

This release uses a two-tier extraction path:

1. Primary: deterministic parsing from the Summary Compensation Table when the table structure is clean enough to map roles directly.
2. Fallback: OpenAI by default, with Ollama as a local fallback when `OPENAI_API_KEY` is missing, empty, or intentionally set to a dummy value.

Operational notes:

- Default OpenAI model: `gpt-4o`
- Default batch script model: `gpt-4o-mini`
- Default Ollama model: `llama3.1`
- Docker full-stack profile points the app at `http://sec_ollama:11434`

## Project Structure

```text
api/                    FastAPI app and health endpoint
chunking/               SEC-aware chunk splitting
core/                   shared config and models
docs/                   decision log, audit docs, researcher-facing docs
fixtures/               client CSVs, manifests, evaluation inputs
generation/             prompt and citation assembly
indexing/               embedding layer
ingestion/              EDGAR fetch, HTML parse, extraction logic
notebooks/              milestone evidence notebooks
retrieval/              retrieval and fusion logic
scripts/                batch runners, validators, manifest builders
storage/                schema and PostgreSQL writer
tests/                  unit and integration-style tests
output/                 generated batch artifacts
```

## Phase Status

| Phase | Status | Scope |
| --- | --- | --- |
| 0 | Done | HTML parsing, SEC block modeling, fixture-based ingestion |
| 1 | Done | Batch extraction, compensation outputs, Ollama fallback, S&P 500 manifest workflow |
| 2 | Done | SEC-aware chunking and Voyage Finance-2 embedding pipeline |
| 3 | Done (schema) | PostgreSQL schema, chunk writer, and storage migration groundwork |
| 4 | Planned | Hybrid retrieval over PostgreSQL + Qdrant |
| 5 | Planned | API query layer and citation-grounded generation |

## Merge Gate

Before tagging a release from `main` or merging a release-critical change into `main`, the minimum gate is:

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

CI currently enforces the static and test portion of that gate. The batch run and validator remain the release-level smoke check to run before tagging or merging a stabilization branch.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) for branching rules, required local checks, and data-quality expectations for extraction-critical changes.

## Project Audit & Lessons

For a narrative of the branch audit, root causes, and the stabilization work merged from `cleanup-finalize`, see [docs/audit_and_lessons.md](docs/audit_and_lessons.md).

## End-State Review Pointers

When reviewing the current extraction contract or validating the reasoning behind the latest iterations, start with these files:

- `ingestion/comp_table_extractor.py` for deterministic summary compensation parsing, name/title split, year handling, and numeric normalization
- `ingestion/critical_section_labeler.py` for rule-based compensation section coverage signals
- `indexing/embedder.py` for the Voyage Finance-2 embedding path
- `scripts/run_batch50_key_results.py` for orchestration, role collapsing, fiscal-year selection, and output serialization
- `scripts/validate_key_results.py` for the safety checks that define acceptable `key_results.csv` outputs
- `ingestion/cda_extractor.py` for CD&A section capture, token counts, and pay-for-performance signals
- `ingestion/sec_chunker.py` for table-aware chunk preservation
- `chunking/splitter.py` for offline-safe token counting and chunk splitting behavior
- `docs/PIPELINE_AGENT_HANDOFF.md` for the cross-iteration reasoning and operational handoff context

For local inspection only, the most relevant generated evidence is:

- `output/b01/key_results.csv`
- `output/b01/batch_log.csv`

## Troubleshooting

| Problem | Likely Cause | Fix |
| --- | --- | --- |
| SEC fetch fails immediately | `EDGAR_USER_AGENT` or `SEC_USER_AGENT` is missing | Set both env vars to a real name plus email before hitting EDGAR. |
| `pytest` reaches chunking tests offline | No action needed | `chunking/splitter.py` now falls back to deterministic local token counting when `cl100k_base` is not cached. |
| `docker compose --profile full up` starts but `app` is unhealthy | Missing API keys or incorrect env values in `.env` | Confirm `.env` contains valid `EDGAR_USER_AGENT` and defaults for `VOYAGE_API_KEY` / `OPENAI_API_KEY`. |
| Ollama fallback does not trigger | `OPENAI_API_KEY` is set to a real key, so the OpenAI path is used | Set `OPENAI_API_KEY=dummy` or clear it to force the local Ollama path. |
| Ollama container is up but extraction still fails | The target model is not pulled or `OLLAMA_BASE_URL` points at the wrong host | Use the full Docker profile or run `ollama pull llama3.1`, then verify `OLLAMA_BASE_URL`. |
| Postgres connection errors | Port `5432` already in use or `DB_URL` points at the wrong host | Stop the conflicting service or update `DB_URL` to match your environment. |
| `validate_key_results.py` fails coverage checks | The smoke batch produced too many `failed` rows or sparse CEO/CD&A coverage | Inspect `output/<batch>/batch_log.csv`, the `error` column, and the chosen input CIKs before scaling up. |
