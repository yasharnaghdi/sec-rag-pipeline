# Executive Summary — sec-rag-pipeline

## Last updated: 2026-03-01

## What this system does

This system enables academic researchers to query SEC proxy statement filings (DEF 14A)
in plain English and receive verifiable, citable answers drawn directly from the source documents.
Every answer includes a citation string that identifies the exact company, filing date, section,
and chunk position the answer was drawn from. No answer is generated without a traceable source.

The system is designed for use in academic compensation research, corporate governance studies,
and executive pay analysis. Outputs are suitable for citation in peer-reviewed work.

---

## Current status (M0 — 2026-03-01)

**What researchers can do right now:**

Run `notebooks/03_ingest_parse_chunk.ipynb` to:
- Download the ConnectOne Bancorp DEF 14A proxy statement directly from SEC EDGAR
- See every section of the filing broken into typed, labelled blocks
- Export a fully-cited chunk manifest to `output/chunks_cnob_m0.csv`
- Verify that all data is traceable back to the source HTML character-for-character

**What is not yet available:**

- Natural language query interface (M1)
- Search across multiple filings simultaneously (M1)
- Embedding-based semantic search (M1)
- LLM-generated answers with citations (M2)
- Apple, Microsoft, J&J, and Caterpillar filings processed (M1)

---

## Milestone plan

| Milestone | Scope | Status |
|---|---|---|
| M0 | Parse, chunk, and audit 5 filings locally. No LLM, no embeddings. | In progress |
| M1 | Embed chunks, store in pgvector + Qdrant, BM25 + vector hybrid retrieval | Not started |
| M2 | LLM generation with citations, evaluation against 7 benchmark queries | Not started |

M0 sign-off criteria: `notebooks/03_ingest_parse_chunk.ipynb` runs to completion with all assertions passing for all 5 fixture filings, and `output/chunks_cnob_m0.csv` is committed to the repository.

---

## Governance

All architectural decisions are logged in `docs/TECHNICAL_AUDIT.md`.
No decision is reversed without a written rationale in the PR description.
The system never calls an LLM during parsing or chunking.
All chunk outputs are verifiable against the original HTML source.
