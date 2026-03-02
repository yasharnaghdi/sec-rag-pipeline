# Technical Audit Log — sec-rag-pipeline

## Last updated: 2026-03-02

## Purpose

This document is the authoritative technical record for the sec-rag-pipeline project.
It is updated as the final commit of every PR. It exists so a technical reviewer
can understand exactly what is implemented, what is deferred, and why specific
decisions were made, without reading the code.

---

## Architecture overview

The pipeline processes SEC DEF 14A proxy statement filings through five sequential layers:

1. **Ingestion** — Download raw HTML from EDGAR, cache locally, construct `DocumentMetadata`
2. **Parsing** — Convert raw HTML to a typed list of `BaseBlock` subclass instances
3. **Chunking** — Convert block list to `Chunk` objects with stable IDs, token counts, and citation strings
4. **Storage** — Write chunks to PostgreSQL (pgvector) and Qdrant (M1, not yet implemented)
5. **Retrieval + Generation** — Hybrid BM25 + vector search feeding an LLM with citations (M1/M2)

All layers in M0 are deterministic and require no LLM calls.

---

## What has been built

The storage layer is now implemented in `storage/writer.py` with idempotent Postgres upserts for chunks, including citation and table JSON persistence. The batch evidence workflow is captured in `notebooks/04_batch_ingest.ipynb`, which processes all five fixture filings from `fixtures/manifest.csv` end-to-end (download, parse, chunk, store) and exports `output/m0_batch_summary.csv` for M0 audit verification.

---

## Closed architectural decisions

| Decision | Resolution | Date |
|---|---|---|
| Section detection library | BeautifulSoup4 + heuristics. LangExtract rejected: requires Gemini API call per extraction. | 2026-02-27 |
| Chunking strategy | 600 tokens / 100 overlap via LangChain RecursiveCharacterTextSplitter. Tables always atomic (1 chunk). | 2026-02-27 |
| Token counting | tiktoken cl100k_base. Deterministic, no network call. | 2026-02-27 |
| Embeddings | Voyage Finance-2 (1024-dim). Stubbed in M0. Not called in any M0 code path. | 2026-02-27 |
| Document model | Eight Pydantic v2 classes: ProseBlock, HeadingBlock, TableBlock, ImageBlock, FootnoteBlock, XBRLTaggedBlock, XBRLAnnotation, DocumentMetadata. | 2026-02-27 |
| M0 fixture filings | ConnectOne Bancorp, Apple, Microsoft, Johnson & Johnson, Caterpillar. | 2026-02-27 |

---

## M0 implementation status

### Document block models (PR #1, merged 2026-02-27)
- `ingestion/metadata_model.py`: all eight Pydantic classes implemented
- SHA-256 deterministic block IDs
- `fiscal_year_end` optional with default `None` for backward compatibility
- 10 unit tests passing

### EDGAR downloader (PR #2, merged 2026-02-27)
- `ingestion/downloader.py`: manifest-driven downloader with cache-hit detection
- Runtime TBD accession resolution via edgartools for J&J slot
- `SEC_USER_AGENT` enforcement at instantiation
- Deterministic output filenames: `{cik}_{accession_normalized}.html`
- 6 unit tests passing, all edgartools calls mockable via dependency injection

### HTML parser, chunker, notebook (PR #3, complete)
- `ingestion/sec_html_parser.py`: `SECHTMLParser.parse()` returns `list[BaseBlock]`
  - Heading detection: tag-based, bold heuristic, all-caps heuristic, keyword match (SEC_SECTION_PATTERNS)
  - Table extraction: rows, header_row_count, linearized_text, has_merged_cells, colspan expansion
  - Footnote resolution: scans 3 siblings after table, links to parent table, populates `footnotes` dict
  - XBRL detection: `ix:nonFraction` / `ix:nonNumeric` produces `XBRLTaggedBlock` with `XBRLAnnotation` per concept
  - Image detection: `ImageBlock` with caption from following sibling
  - Section propagation: `section_id` propagates from last emitted HeadingBlock; `"preamble"` before first heading
- `ingestion/sec_chunker.py`: `SECChunker.chunk_blocks()` returns `list[Chunk]`
  - Tables: always 1 chunk, `table_json` populated
  - All other blocks: token-aware splitting, max 600 tokens, 100 overlap
  - Citation string format: `"{company_name} | {form_type} | {filing_date} | {section_id} | chunk {index}"`
- `notebooks/03_ingest_parse_chunk.ipynb`: 21-cell evidence notebook
  - Downloads and caches ConnectOne Bancorp DEF 14A from SEC EDGAR
  - Displays block type distribution, sample headings, first table, XBRL blocks
  - Exports `output/chunks_cnob_m0.csv`
  - Final cell asserts all M0 constraints (>50 blocks, >=5 tables, >=10 headings, >=50 chunks, all citation strings non-null)
- 20 unit tests passing (12 parser + 8 chunker)
- mypy --strict: no errors
- ruff: no errors
- `storage/writer.py`: PostgreSQL write layer implemented with idempotent chunk upserts and citation/table JSON persistence
- `notebooks/04_batch_ingest.ipynb`: batch M0 evidence notebook for all 5 fixture filings with gate assertions and CSV export

---

## Critical section labels (rule-based, no LLM)

The parser identifies these specific DEF 14A sections by regex pattern match on heading text:

| Section | Pattern |
|---|---|
| Compensation Discussion & Analysis | `COMPENSATION DISCUSSION AND ANALYSIS` |
| Summary Compensation Table | `SUMMARY COMPENSATION TABLE` |
| Grants of Plan-Based Awards | `GRANTS OF PLAN.BASED AWARDS` |
| Outstanding Equity Awards | `OUTSTANDING EQUITY AWARDS` |
| Option Exercises and Stock Vested | `OPTION EXERCISES` |
| Pension Benefits | `PENSION BENEFITS` |
| Director Compensation | `DIRECTOR COMPENSATION` |
| Corporate Governance | `CORPORATE GOVERNANCE` |
| Board of Directors | `BOARD OF DIRECTORS` |
| Security Ownership | `SECURITY OWNERSHIP` |

Sections not matching any pattern receive `section_id` inherited from the most recent matched heading above them in document order.

---

## Known limitations (M0)

- `source_char_start` / `source_char_end` are approximate (based on `raw_html.find(str(tag))`). Exact offsets deferred to M1.
- Embeddings stubbed. Qdrant not populated. Vector search not available.
- spaCy/NLTK NLP pipeline not yet integrated. Section detection relies on regex only in M0.

---

## Test coverage

| Module | Tests | Status |
|---|---|---|
| `ingestion/metadata_model.py` | 10 | Passing |
| `ingestion/downloader.py` | 6 | Passing |
| `ingestion/sec_html_parser.py` | 12 | Passing |
| `ingestion/sec_chunker.py` | 8 | Passing |
| `storage/writer.py` | 3 | Passing (DB-backed; requires `DB_URL`) |
| `ingestion/sec_proxy_parser.py` (legacy stub) | 8 | Passing |
| Total | 47 | All green |

---

## Deferred to M1

- Qdrant vector ingestion
- Voyage Finance-2 embedding calls
- spaCy section detection as fallback when regex fails
- Exact character offset tracking
