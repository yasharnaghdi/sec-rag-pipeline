# Executive Summary — sec-rag-pipeline

## Last updated: 2026-03-02

## What this system does

This system enables academic researchers to query SEC proxy statement filings (DEF 14A)
in plain English and receive verifiable, citable answers drawn directly from the source documents.
Every answer includes a citation string that identifies the exact company, filing date, section,
and chunk position the answer was drawn from. No answer is generated without a traceable source.

The system is designed for use in academic compensation research, corporate governance studies,
and executive pay analysis. Outputs are suitable for citation in peer-reviewed work.

---

## What researchers can do today

Researchers can run `notebooks/03_ingest_parse_chunk.ipynb` to download a DEF 14A filing from SEC EDGAR, parse it into typed blocks, and export a citation-ready chunk manifest CSV. The workflow provides a transparent path from source HTML to structured outputs that can be inspected and validated manually.

Researchers can also run `notebooks/04_batch_ingest.ipynb` to download, parse, chunk, and store all five fixture proxy statements in a single session, then review row-level storage counts in `output/m0_batch_summary.csv`.

---

## What is not yet available

- Natural language query interface (M1)
- Search across multiple filings simultaneously (M1)
- Embedding-based semantic search (M1)
- LLM-generated answers with citations (M2)

---

## What is coming next

- Ask questions in plain English across multiple filings simultaneously
- Receive answers with verifiable citations to the source document
- Search filings using semantic similarity, not only exact keyword matches
- Retrieve evidence from both lexical and vector search in a single ranked result set
- Expand from deterministic ingest/chunk/storage to embedding and hybrid retrieval workflows

M0 sign-off evidence: `notebooks/03_ingest_parse_chunk.ipynb` and `notebooks/04_batch_ingest.ipynb` run to completion with assertions passing, and outputs are exported to `output/chunks_cnob_m0.csv` and `output/m0_batch_summary.csv`.

---

## Governance

All architectural decisions are logged in `docs/TECHNICAL_AUDIT.md`.
No decision is reversed without a written rationale in the PR description.
The system never calls an LLM during parsing or chunking.
All chunk outputs are verifiable against the original HTML source.
