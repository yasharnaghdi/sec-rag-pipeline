# `extract_outstanding_equity.py` Workflow (Agent Reference)

Last updated: 2026-03-17

This document captures the execution path in `scripts/extract_outstanding_equity.py` — a standalone Outstanding Equity Awards extractor decoupled from the batch50 pipeline for fast iteration.

## 1. Purpose and Relationship to Batch50

The batch50 pipeline (`scripts/run_batch50_key_results.py`) includes equity award extraction as one of several table types (summary comp, grants, equity awards). That pipeline uses a two-tier approach: deterministic extraction first, LLM fallback with `gpt-4o-mini` second.

This standalone script replaces the LLM fallback path with a dedicated, higher-quality extraction using:
- **`gpt-5-mini`** (configurable) instead of `gpt-4o-mini`
- **Raw HTML tables** instead of linearized pipe-delimited text (when char offsets are valid)
- **OpenAI structured outputs** (`response_format` with `json_schema`, `strict: True`) instead of `json_object` mode — guarantees schema conformance without retry logic

The script is intentionally independent so extraction quality can be iterated without touching the batch50 pipeline.

## 2. Entry Point and CLI Contract

`main()` parses these user-facing controls:

- `--input` (required): CSV with `cik` column (or `folder_id` — same format as batch50)
- `--output`: output CSV path (default: `output/outstanding_equity_awards.csv`)
- `--model`: OpenAI model (default: `gpt-5-mini`)
- `--limit`: max CIKs to process (optional, for testing)
- `-v` / `--verbose`: enable debug logging

Example:
```bash
poetry run python scripts/extract_outstanding_equity.py \
    --input fixtures/client_input.csv \
    --output output/outstanding_equity_awards.csv \
    --model gpt-5-mini \
    --limit 5 \
    --verbose
```

## 3. Per-CIK Processing Flow

For each CIK, `process_cik()` executes these stages:

### 3.1 Acquire Filing

Calls `fetch_latest_def14a(cik)` from `ingestion/edgar_folder_fetcher.py`.
- Returns a `FetchedFiling` with: `raw_html`, `company_name`, `ticker`, `filing_date`, `accession_number`, `filing_url`, `cache_path`
- Uses the `data/raw/` cache: if `data/raw/{CIK_padded}_{accession}.html` exists, reads from disk; otherwise downloads from SEC EDGAR and caches it
- Metadata (company name, ticker, filing date) comes from the SEC EDGAR submissions API, same as the batch50 pipeline

### 3.2 Parse HTML

Builds `DocumentMetadata` from filing fields and parses via `SECHTMLParser().parse(raw_html, doc_meta)` → `list[BaseBlock]`.

### 3.3 Locate Table

Calls `_locate_outstanding_equity_awards_table(blocks)` — a copy of the same function from `run_batch50_key_results.py` (lines 1616–1794).

The locator works in three passes:
1. **Context scan**: Find headings/prose containing equity award signatures (e.g., "Outstanding Equity Awards at Fiscal Year-End")
2. **Proximity search**: Score nearby `TableBlock`s using column header matching, row/column counts, and schema-fit heuristics
3. **Global fallback**: If no context-based candidate scores ≥8, scan all tables for candidates scoring ≥10

Key constants (also copied from batch50):
- `_OUTSTANDING_EQUITY_SIGNATURES`: title/heading patterns
- `_OUTSTANDING_EQUITY_HEADER_HINTS`: expected column keywords
- `_COMP_REJECT_HEADER_HINTS`: negative signals (beneficial ownership, peer group, etc.)

### 3.4 Extract Table Content

`_extract_raw_html_table(raw_html, table_block)` attempts to slice raw HTML using `source_char_start`/`source_char_end` from the `TableBlock`.

**Known limitation**: The parser's `_find_tag_span()` sometimes produces degenerate offsets (`start == end`) when it cannot match the BeautifulSoup-serialized tag back to the raw HTML. When this happens, the function falls back to `table_block.linearized_text`.

Also falls back when the raw HTML snippet exceeds `MAX_RAW_HTML_BYTES` (50 KB).

### 3.5 Call LLM

`_call_llm()` sends the table content to OpenAI with:
- **System prompt** (`_SYSTEM_PROMPT`): instructions for HTML/linearized text parsing, strict field mapping, name propagation across rowspan'd rows, numeric normalization rules
- **Structured output schema** (`_RESPONSE_SCHEMA`): `json_schema` with `strict: True` — the model's response is guaranteed to match the schema
- **`max_completion_tokens=8192`**: equity tables can have 50+ rows (multiple grants per executive)
- **No `temperature` parameter**: `gpt-5-mini` only supports the default temperature

Response is parsed with `json.loads()` then validated through `CompanyOutstandingEquityAwardsResult.model_validate()` (Pydantic). A `null` notes field is coerced to empty string for Pydantic compatibility.

### 3.6 Map to CSV Rows

`_row_to_csv()` maps each `OutstandingEquityAwardRecord` to the output column schema, prepending `CIK`, `Company Name`, and `Filing URL`.

## 4. Output Schema

CSV columns (14 total):

| # | Column |
|---|--------|
| 1 | CIK |
| 2 | Company Name |
| 3 | Filing URL |
| 4 | Name |
| 5 | Grant Date |
| 6 | Option Award (Number of Securities Underlying Unexercised Options Exercisable (#)) |
| 7 | Option Award (Number of Securities Underlying Unexercised Options Unexercisable (#)) |
| 8 | Option Award (Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#)) |
| 9 | Option Exercise Price ($) |
| 10 | Option Expiration Date |
| 11 | Stock Awards (Number of Shares or Units of Stock that Have Not Vested (#)) |
| 12 | Stock Awards (Market Value of Shares or Units of Stock that Have Not Vested ($)) |
| 13 | Stock Awards (Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#)) |
| 14 | Stock Awards (Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($)) |

Numeric values are plain digit strings (no `$`, no commas). Dates are kept as source text. Missing values are empty strings in the CSV.

## 5. Reused Components

| Component | Source | How used |
|-----------|--------|----------|
| `fetch_latest_def14a()` | `ingestion/edgar_folder_fetcher.py` | Filing acquisition + metadata |
| `SECHTMLParser` | `ingestion/sec_html_parser.py` | HTML → typed blocks |
| `OutstandingEquityAwardRecord` | `ingestion/llm_comp_extractor.py:311` | Pydantic model for per-row data |
| `CompanyOutstandingEquityAwardsResult` | `ingestion/llm_comp_extractor.py:369` | Pydantic model for full extraction result |
| `DocumentMetadata`, `TableBlock`, etc. | `ingestion/metadata_model.py` | Block types |
| Table locator + constants | `run_batch50_key_results.py:225–294, 1616–1794` | **Copied** into the script (not imported) |

The table locator was copied rather than imported to avoid pulling in the entire batch50 module and its heavy dependencies. If the locator logic is updated in batch50, this copy should be synced.

## 6. Error Handling

- **Filing acquisition failure**: exception is caught per-CIK, logged, and processing continues with the next CIK
- **Table not found**: logged as warning, no CSV rows emitted for that CIK
- **LLM API error**: caught per-CIK, logged as error, continues to next CIK
- **No retry logic**: structured outputs eliminate most parse/validation failures; if the API call fails, it is not retried

Final summary is logged: `processed=N tables_found=N no_table=N errors=N total_rows=N`.

## 7. Known Limitations and Iteration Notes

1. **Raw HTML offset reliability**: The `sec_html_parser._find_tag_span()` function often produces degenerate char offsets for large filings, causing fallback to linearized text. Improving the parser's tag-span logic would unlock raw HTML input for more filings.

2. **Single filing per CIK**: Currently fetches only the latest DEF 14A. To extract across multiple fiscal years, either run with different input CSVs or extend with a `--fiscal-year-start/--fiscal-year-end` sweep (like batch50).

3. **Table locator drift**: The locator is a snapshot copy from batch50. If heuristics are improved upstream, this script needs manual syncing.

4. **Model compatibility**: `gpt-5-mini` does not support `temperature` or `max_tokens` (use `max_completion_tokens`). If switching to a different model, these parameters may need adjustment.

## 8. Key File Paths

| File | Purpose |
|------|---------|
| `scripts/extract_outstanding_equity.py` | This script |
| `ingestion/edgar_folder_fetcher.py` | Filing fetcher (EDGAR API + `data/raw/` cache) |
| `ingestion/sec_html_parser.py` | HTML parser |
| `ingestion/llm_comp_extractor.py` | Pydantic models + legacy LLM extraction |
| `ingestion/metadata_model.py` | Block type definitions |
| `fixtures/client_input.csv` | Default input CSV (4 CIKs with cached filings) |
| `data/raw/` | Cached HTML filings |
| `output/outstanding_equity_awards.csv` | Default output path |
