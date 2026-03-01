# Researcher Guide

## Task 3 Workflow
Use the notebook `notebooks/03_ingest_parse_chunk.ipynb` as the canonical walkthrough.

### Pipeline Steps
1. Download/cache live ConnectOne DEF 14A HTML from SEC EDGAR.
2. Build `DocumentMetadata` for attribution.
3. Parse HTML via `SECHTMLParser.parse(raw_html, metadata)` into typed blocks.
4. Chunk blocks via `SECChunker.chunk_blocks(blocks, metadata)`.
5. Export chunk manifest to `output/chunks_cnob_m0.csv`.
6. Run M0 assertions to confirm minimum evidence thresholds.

## Block Types Emitted
- `HeadingBlock`
- `ProseBlock`
- `TableBlock` (dual-format: `rows` + `linearized_text`)
- `FootnoteBlock`
- `XBRLTaggedBlock`
- `ImageBlock`

## Chunk Contract
Each emitted chunk includes:
- `chunk_index` (monotonic from 0)
- `document_id`
- `section_id`
- `token_count` (600 max target)
- `citation_string`:
  - `{company_name} | {form_type} | {filing_date} | {section_id} | chunk {index}`
- `table_json` populated for table-derived chunks

## Validation Commands
- `pytest tests/unit/test_sec_html_parser.py tests/unit/test_sec_chunker.py -v`
- `poetry run mypy --strict ingestion/sec_html_parser.py ingestion/sec_chunker.py`
- `poetry run ruff check ingestion/sec_html_parser.py ingestion/sec_chunker.py tests/unit/test_sec_html_parser.py tests/unit/test_sec_chunker.py`
