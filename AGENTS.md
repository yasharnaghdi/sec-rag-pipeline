# Agent Handoff Instructions - sec-rag-pipeline

This file is the single source of truth for AI coding agents working in this repository.
Read it before touching any file.

## Project Purpose

This repo builds a retrieval-augmented generation pipeline over SEC proxy filings and related disclosure artifacts.

Current production path:

EDGAR -> HTML cache -> SEC block parsing -> compensation extraction -> batch CSV outputs

Planned end state:

EDGAR -> parse -> chunk -> embed -> store in PostgreSQL + Qdrant -> retrieve -> generate with citations

## Stack

| Layer | Technology |
| --- | --- |
| Language | Python 3.11 |
| API | FastAPI 0.111 |
| Parsing | BeautifulSoup4 + lxml |
| EDGAR ingestion | requests + edgartools |
| Chunking | LangChain RecursiveCharacterTextSplitter + tiktoken |
| Embeddings | Voyage Finance-2 |
| LLM fallback | OpenAI primary, Ollama fallback |
| Database | PostgreSQL 16 + pgvector |
| Vector store | Qdrant |
| Package manager | Poetry |
| Testing | pytest + pytest-asyncio |
| Static checks | mypy strict + ruff |

## Current State

Phase 0 and Phase 1 are complete on this branch.

- [x] SEC HTML acquisition via `ingestion/edgar_folder_fetcher.py`
- [x] HTML parsing via `ingestion/sec_html_parser.py`
- [x] Table-aware chunking via `ingestion/sec_chunker.py` and `chunking/splitter.py`
- [x] Deterministic compensation extraction via `ingestion/comp_table_extractor.py`
- [x] LLM compensation fallback via `ingestion/llm_comp_extractor.py`
- [x] Ollama routing when OpenAI credentials are missing or dummy
- [x] Batch runner via `scripts/run_batch50_key_results.py`
- [x] Batch validator via `scripts/validate_key_results.py`
- [x] S&P 500 manifest tooling via `scripts/build_sp500_manifest.py`
- [x] Postgres schema and chunk writer groundwork in `storage/`
- [x] Docker local stack with postgres, qdrant, app, and optional ollama profile

## Phase 2 Task (active)

Your job is to finish the chunking and embedding path so chunks can be embedded with Voyage Finance-2 and prepared for vector storage.

### Acceptance Criteria

1. `SECChunker.chunk_blocks(blocks)` preserves atomic tables and monotonic chunk indices.
2. `count_tokens()` remains deterministic and safe for repeated local test runs.
3. `VoyageEmbedder.embed(texts)` returns `list[list[float]]`.
4. Returned embeddings preserve the input text order.
5. Empty input batches return an empty list without making an API call.
6. Missing `VOYAGE_API_KEY` fails clearly rather than silently.
7. Voyage Finance-2 remains the default embedding model.
8. `pytest tests/test_chunk_boundaries.py tests/unit/test_sec_chunker.py -v` is green.
9. `mypy chunking/splitter.py indexing/embedder.py` and `ruff check chunking/splitter.py indexing/embedder.py` are clean.

### Files to Modify

- `chunking/splitter.py`
- `indexing/embedder.py`
- supporting code only if required by tests or strict typing

## Phase 3 Task (queued)

Wire PostgreSQL persistence into the batch runner behind a dedicated `--store-pg` flag.

### Acceptance Criteria

1. The batch runner writes chunks to PostgreSQL only when `--store-pg` is passed.
2. Runs without `--store-pg` stay side-effect free with respect to the database.
3. DB write failures are logged per filing and do not crash the whole batch.
4. Stored chunk rows preserve citation strings, section lineage, and table JSON.

### Likely Files

- `scripts/run_batch50_key_results.py`
- `storage/writer.py`
- `storage/database.py`

## Do Not Touch Without Explicit Need

- `tests/`
- `storage/schema.sql`
- `.github/workflows/`
- generated files under `output/`

If you think one of these must change, stop and explain why before editing it.

## Coding Standards

- Add type annotations to all new code.
- Keep public methods documented.
- Use `logging.getLogger(__name__)`; do not use `print()`.
- Keep tests as the spec. Do not weaken assertions to make a task pass.
- Preserve offline-safe behavior where practical.
- Commit messages must follow `<type>(<scope>): <description>`.

## Working Rules

- Prefer `rg` for repo search.
- Use `apply_patch` for manual file edits.
- Do not revert unrelated working tree changes.
- Stage only the files you intentionally changed.
- If a verification step fails because the sandbox blocks network, retry with escalation instead of assuming the code is broken.

## Verification Commands

```bash
poetry run pytest tests/ -v --tb=short
poetry run ruff check .
poetry run mypy . --ignore-missing-imports
poetry run python scripts/run_batch50_key_results.py \
  --input fixtures/client_input.csv --batch-label smoke --limit 3
poetry run python scripts/validate_key_results.py \
  --input output/smoke/key_results.csv --expected-rows 3
```
