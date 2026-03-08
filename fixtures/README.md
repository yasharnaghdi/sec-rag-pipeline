# Fixtures

This directory contains the human-curated and generated inputs used by the SEC RAG pipeline.

## client_input.csv

`client_input.csv` is the smallest batch input used by `scripts/run_batch50_key_results.py`.

Current format:

| Column | Required | Example | Meaning |
| --- | --- | --- | --- |
| `folder_id` | yes | `320193` | Company identifier consumed by the batch runner. In the current implementation this is the target CIK for "latest DEF 14A" acquisition. |

How to find a CIK:

- SEC company search: `https://www.sec.gov/edgar/searchedgar/companysearch`
- EDGAR API docs: `https://www.sec.gov/search-filings/edgar-application-programming-interfaces`

## sp500_manifest.csv

`sp500_manifest.csv` is the larger batch manifest used for S&P 500-scale download and ingest workflows.

Current columns:

- `slot`
- `cik`
- `company_name`
- `ticker`
- `industry`
- `form_type`
- `filing_date`
- `accession_number`
- `edgar_url`
- `fiscal_year`
- `source_url`
- `raw_html_path`

To regenerate it:

```bash
poetry run python scripts/build_sp500_manifest.py
```

Requirements:

- `SEC_USER_AGENT` must be set in your environment or `.env`
- network access to Wikipedia and SEC EDGAR must be available

Related files:

- `sp500_download_log.csv` records HTML download outcomes
- `sp500_manifest_errors.csv` records tickers or filings that could not be resolved

## Evaluation JSON

The current repo file is `eval_queries.json`.

If you see older notes referring to `eval_qa.json`, use `eval_queries.json` in this repo.

JSON object schema per entry:

| Field | Type | Meaning |
| --- | --- | --- |
| `query_id` | string | Stable identifier used across evaluation outputs. |
| `query_text` | string | Natural-language question asked by an analyst or evaluator. |
| `target_section_keywords` | array of strings | Section hints used to localize the answer. |
| `required_keywords` | array of strings | Terms expected in relevant evidence chunks. |
| `cross_filing` | boolean | Whether the query requires evidence from more than one filing. |
| `notes` | string | Annotation guidance for the human reviewer. |
| `relevance_grades` | object | Human-populated mapping of `chunk_id -> relevance score`. |

Example shape:

```json
[
  {
    "query_id": "Q01",
    "query_text": "What was the CEO's total compensation in 2023?",
    "target_section_keywords": ["Summary Compensation Table"],
    "required_keywords": ["total", "CEO"],
    "cross_filing": false,
    "notes": "Answer should come from the latest proxy filing.",
    "relevance_grades": {}
  }
]
```
