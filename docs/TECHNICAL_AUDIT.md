# Technical Audit Log — sec-rag-pipeline

## Last updated: 2026-03-01

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

## What has been built

The system can download SEC DEF 14A filings from EDGAR and cache each source HTML document locally with deterministic filenames. It enforces a valid SEC user agent and supports reproducible ingestion from manifest inputs so the same filing can be reprocessed consistently.

The parser can convert filing HTML into typed document blocks, including headings, prose, tables, images, footnotes, and inline XBRL-tagged content. Section labels are assigned through deterministic heading detection rules, and section context is propagated through downstream blocks in document order.

The chunking layer can turn parsed blocks into stable, citable chunks with deterministic IDs, token counts, chunk indices, and citation strings. Tables are preserved as atomic chunks so structured compensation data remains intact for audit and downstream analysis.

The notebook workflow can run an end-to-end ingestion, parsing, and chunking pass for a live filing and export a manifest CSV in `output/`. This provides a transparent evidence path from raw SEC source HTML to analyzable, citation-ready chunks.

The codebase includes automated validation across core modules with passing unit tests, static type checks, and linting checks for implemented components. This establishes a reproducible baseline for extending storage, retrieval, and generation layers.

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
- Storage layer (PostgreSQL write) not yet wired. Chunks exist only in memory and in the exported CSV.
- Only ConnectOne Bancorp has been run through the live notebook. Apple, Microsoft, J&J, Caterpillar filings are in `fixtures/manifest.csv` but not yet parsed.
- Embeddings stubbed. Qdrant not populated. Vector search not available.
- spaCy/NLTK NLP pipeline not yet integrated. Section detection relies on regex only in M0.

---

## Test coverage

| Module | Tests | Status |
|---|---|---|
| `ingestion/metadata_model.py` | 10 | Passing |
| `ingestion/downloader.py` | 5 | Passing |
| `ingestion/sec_html_parser.py` | 12 | Passing |
| `ingestion/sec_chunker.py` | 8 | Passing |
| `ingestion/sec_proxy_parser.py` (legacy stub) | 8 | Passing |
| Total | 43 | All green |

---

## Deferred to M1

- PostgreSQL write layer (`storage/writer.py`)
- Qdrant vector ingestion
- Voyage Finance-2 embedding calls
- spaCy section detection as fallback when regex fails
- Exact character offset tracking
- Remaining four filings through full notebook pipeline
