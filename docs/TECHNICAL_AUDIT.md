# Technical Audit

## Task 3 Delivery (Notebook-First Ingestion Pipeline)
- Date: 2026-03-01
- Scope completed:
  - Added synthetic fixture: `tests/fixtures/sample_cnob.html`
  - Added parser-first unit tests: `tests/unit/test_sec_html_parser.py`
  - Added chunker-first unit tests: `tests/unit/test_sec_chunker.py`
  - Implemented SEC chunker: `ingestion/sec_chunker.py`
  - Aligned parser heading heuristic behavior in `ingestion/sec_html_parser.py`
  - Added evidence notebook: `notebooks/03_ingest_parse_chunk.ipynb`
  - Relaxed `DocumentMetadata` optional fields for notebook compatibility in `ingestion/metadata_model.py`

## Parser Behavior Verified
- `<h1>`-`<h6>` emit `HeadingBlock` with tag-based level detection.
- Bold all-caps paragraph heading heuristic emits `HeadingBlock(detection_method=\"bold_heuristic\")`.
- Tables emit dual-format `TableBlock` (`rows` + `linearized_text`) with `colspan` expansion.
- Table-adjacent footnotes are linked to table IDs and copied to `TableBlock.footnotes`.
- Inline XBRL (`ix:nonFraction` / `ix:nonNumeric`) emits `XBRLTaggedBlock`.
- Images emit `ImageBlock` with `position_token`.
- Block `order_index` monotonicity and section propagation (`preamble` then heading-derived section IDs) verified.

## Chunker Behavior Verified
- `TableBlock` always yields one atomic chunk.
- Table chunk carries serialized table payload in `table_json`.
- Non-table block text is split with `RecursiveCharacterTextSplitter` at 600-token budget with overlap 100.
- `chunk_index` is document-scoped and monotonically increasing from 0.
- Each chunk includes:
  - `document_id`
  - inherited `section_id`
  - token count
  - citation string in format:
    - `{company_name} | {form_type} | {filing_date} | {section_id} | chunk {index}`

## Validation Evidence
- `pytest tests/unit/test_sec_html_parser.py tests/unit/test_sec_chunker.py -v` -> 20 passed
- `poetry run mypy --strict ingestion/sec_html_parser.py ingestion/sec_chunker.py` -> success
- `poetry run ruff check ingestion/sec_html_parser.py ingestion/sec_chunker.py tests/unit/test_sec_html_parser.py tests/unit/test_sec_chunker.py` -> success
- Regression guard:
  - `pytest tests/test_html_parser_sec.py tests/test_table_dual_format.py -v` -> 8 passed
