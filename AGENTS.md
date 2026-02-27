# Agent Handoff Instructions — sec-rag-pipeline

This file is the single source of truth for AI coding agents (Codex, Copilot, Cursor, etc.).
Read this before touching any file.

## Project Purpose

A production-grade RAG pipeline for SEC proxy filings (DEF 14A) and 10-K documents.
Ingests from EDGAR → parses HTML → chunks → embeds (Voyage Finance-2) → stores in PostgreSQL + Qdrant → hybrid retrieval → LLM generation with citations.

## Stack

| Layer | Technology |
|-------|------------|
| Language | Python 3.11, strict mypy |
| API | FastAPI 0.111 |
| ORM | SQLAlchemy 2.0 async |
| DB | PostgreSQL 16 + pgvector |
| Vector store | Qdrant |
| Embeddings | Voyage Finance-2 (1024-dim) |
| Chunking | LangChain RecursiveCharacterTextSplitter |
| HTML parsing | BeautifulSoup4 (lxml) |
| EDGAR ingestion | edgartools |
| Package manager | Poetry |
| Testing | pytest + pytest-asyncio |

## Current State (Phase 0 complete)

- [x] Repo scaffolded with full directory structure
- [x] 5 TDD test files written (CI gate) — **tests currently fail by design**
- [x] docker-compose with postgres:pgvector16 + qdrant + app service
- [x] PostgreSQL schema (schema.sql) — 4 tables with FK chain
- [x] Core models (Pydantic): FilingMetadata, ContentBlock, SECBlock, Chunk
- [x] HTMLParser base (BeautifulSoup) — ported from V1
- [x] SECProxyParser stub — extends HTMLParser, section detection regex ready
- [x] SECChunker stub — table-atomic rule + LangChain splitter wired
- [x] VoyageEmbedder stub
- [x] RRF merge function (retrieval/hybrid.py) — implemented, testable
- [x] PromptBuilder — implemented, citation tests pass
- [x] GitHub Actions CI (ruff + mypy + pytest)

## Phase 1 Task for Agent

**Your job: Make all tests in `tests/test_html_parser_sec.py` and `tests/test_table_dual_format.py` pass.**

### Acceptance Criteria
1. `SECProxyParser.parse_with_metadata(file_path, metadata)` returns `list[SECBlock]`
2. Every block has `cik`, `company_name`, `filing_date`, `document_type` from `FilingMetadata`
3. Heading blocks matching SEC_SECTION_PATTERNS update `section_header` on all subsequent blocks
4. Table blocks carry `rows: list[list[str]]` AND `linearized_text: str`
5. `pytest tests/test_html_parser_sec.py tests/test_table_dual_format.py -v` → all green
6. `mypy ingestion/sec_proxy_parser.py` → no errors
7. `ruff check ingestion/sec_proxy_parser.py` → no errors

### Files to Modify
- `ingestion/sec_proxy_parser.py` — primary implementation target
- `ingestion/html_parser.py` — extend `_extract_table` if needed (keep backward-compat)
- `ingestion/metadata_model.py` — add fields only if strictly required by tests

### Do NOT Touch
- `tests/` — tests are the spec; never modify test files
- `storage/schema.sql` — DB schema is finalized
- `docker-compose.yml`
- `.github/workflows/ci.yml`

## Phase 2 Task (after Phase 1 passes)

**Make `tests/test_chunk_boundaries.py` pass.**

### Acceptance Criteria
1. `SECChunker.chunk_blocks(blocks)` returns `list[Chunk]`
2. Each `TABLE` block produces exactly 1 chunk (atomic)
3. Every table chunk has `table_json` populated
4. Chunk indices are monotonically increasing from 0
5. `pytest tests/test_chunk_boundaries.py -v` → all green

### Files to Modify
- `chunking/splitter.py` — `SECChunker.chunk_blocks()` implementation

## Coding Standards

- All new functions/classes require type annotations (mypy strict)
- Docstrings on every public method
- No `print()` — use `logging.getLogger(__name__)`
- Tests are the spec — if a test is wrong, raise it as a comment, never silently skip
- Commit message format: `<type>(<scope>): <description>` — e.g. `feat(ingestion): implement SECProxyParser section detection`

## Environment Setup

```bash
cp .env.example .env
# Fill in VOYAGE_API_KEY, OPENAI_API_KEY
docker compose up -d postgres qdrant
poetry install
poetry run pytest tests/ -v
```
