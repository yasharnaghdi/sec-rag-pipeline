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

## What researchers can do today

Researchers can run `notebooks/03_ingest_parse_chunk.ipynb` to download a DEF 14A filing from SEC EDGAR, parse it into typed blocks, and export a citation-ready chunk manifest CSV. The current workflow provides a transparent path from source HTML to structured outputs that can be inspected and validated manually.

Researchers can review section-labelled chunks and citation strings directly in `output/chunks_cnob_m0.csv`. Each chunk is traceable to the underlying filing text, which supports reproducible evidence collection for compensation and governance studies.

---

## What is coming next

- Ask questions in plain English across multiple filings simultaneously
- Receive answers with verifiable citations to the source document
- Search filings using semantic similarity, not only exact keyword matches
- Retrieve evidence from both lexical and vector search in a single ranked result set
- Process the remaining fixture filings (Apple, Microsoft, Johnson & Johnson, and Caterpillar) through the same pipeline

---

## Governance

All architectural decisions are logged in `docs/TECHNICAL_AUDIT.md`.
No decision is reversed without a written rationale in the PR description.
The system never calls an LLM during parsing or chunking.
All chunk outputs are verifiable against the original HTML source.
