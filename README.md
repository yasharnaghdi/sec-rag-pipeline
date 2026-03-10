# sec-rag-pipeline

![CI](https://img.shields.io/badge/CI-GitHub_Actions-181717?logo=githubactions&logoColor=white)
![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1?logo=postgresql&logoColor=white)
![License](https://img.shields.io/badge/License-Private-lightgrey)

Production-grade SEC filing RAG pipeline for DEF 14A proxy statements and 10-K documents. The current release is optimized for proxy acquisition, compensation extraction, offline-safe local development, and auditable CSV outputs.

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
| `cda_token_count`, `pay_for_performance_flag`, `cda_section_found` | Companion CD&A metrics captured during the same filing pass. |
| `status`, `error` | Final row status and any error message recorded by the batch runner. |

Companion file: `output/<batch_label>/batch_log.csv`

| Columns | Meaning |
| --- | --- |
| `cik`, `company_name`, `status`, `extraction_method` | Per-company execution result. |
| `block_count`, `table_count`, `comp_heading_found`, `comp_table_found` | Parsing and table-location diagnostics. |
| `det_rows`, `llm_confidence`, `cda_token_count`, `pay_for_performance_flag` | Extraction diagnostics. |
| `elapsed_seconds`, `error` | Runtime and failure detail. |

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
| 2 | In progress | SEC-aware chunking and OpenAI embedding pipeline |
| 3 | Done (schema) | PostgreSQL schema, chunk writer, and storage migration groundwork |
| 4 | Planned | Hybrid retrieval over PostgreSQL + Qdrant |
| 5 | Planned | API query layer and citation-grounded generation |

## Troubleshooting

| Problem | Likely Cause | Fix |
| --- | --- | --- |
| SEC fetch fails immediately | `EDGAR_USER_AGENT` or `SEC_USER_AGENT` is missing | Set both env vars to a real name plus email before hitting EDGAR. |
| `pytest` fails on first run while importing `tiktoken` | Local tokenizer cache is empty | Re-run the suite once with network access so `cl100k_base` is cached locally. |
| `docker compose --profile full up` starts but `app` is unhealthy | Missing API keys or incorrect env values in `.env` | Confirm `.env` contains valid `EDGAR_USER_AGENT` and defaults for `VOYAGE_API_KEY` / `OPENAI_API_KEY`. |
| Ollama fallback does not trigger | `OPENAI_API_KEY` is set to a real key, so the OpenAI path is used | Set `OPENAI_API_KEY=dummy` or clear it to force the local Ollama path. |
| Ollama container is up but extraction still fails | The target model is not pulled or `OLLAMA_BASE_URL` points at the wrong host | Use the full Docker profile or run `ollama pull llama3.1`, then verify `OLLAMA_BASE_URL`. |
| Postgres connection errors | Port `5432` already in use or `DB_URL` points at the wrong host | Stop the conflicting service or update `DB_URL` to match your environment. |
| `validate_key_results.py` fails coverage checks | The smoke batch produced too many `failed` rows or sparse CEO/CD&A coverage | Inspect `output/<batch>/batch_log.csv`, the `error` column, and the chosen input CIKs before scaling up. |
