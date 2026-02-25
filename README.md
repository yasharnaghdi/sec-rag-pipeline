# sec-rag-pipeline

A production-grade Retrieval-Augmented Generation pipeline for SEC proxy filings (DEF 14A) and 10-K documents.

## Architecture

```
EDGAR (edgartools)
    └─► ingestion/       — SECProxyParser (BeautifulSoup + SEC section detection)
            └─► chunking/    — LangChain splitter, table-safe, 600t/100t overlap
                    └─► storage/     — PostgreSQL + pgvector (documents→sections→chunks→embeddings)
                            └─► indexing/    — Voyage Finance-2 embeddings → Qdrant
                                    └─► retrieval/   — Hybrid BM25 + vector, RRF merge, cross-encoder re-rank
                                            └─► generation/ — LLM prompt assembly + citation extraction
```

## Quickstart

```bash
cp .env.example .env
# Fill in API keys
docker compose up -d
poetry install
poetry run pytest tests/ -v
```

## Phase Plan

| Phase | Scope | Days |
|-------|-------|------|
| 0 | Repo init, TDD baseline, infra | 1 |
| 1 | Ingestion: SECProxyParser + edgartools | 1–2 |
| 2 | Chunking + Voyage embeddings | 2–3 |
| 3 | PostgreSQL schema + pgvector | 3 |
| 4 | Hybrid retrieval + LLM generation | 4–5 |
| 5 | FastAPI endpoints + query UI | 6+ |

## Reuse from stark-translate-agent

- `ingestion/html_parser.py` — ported from `file_parser.py`, BeautifulSoup base retained
- `core/config.py` — stripped of stark-* branding, re-parameterized for SEC/RAG env vars
- All stark-specific orchestration, Jinja2 templates, and task state machine are **not** ported
