# Decision Log

This file records the architectural decisions that define `sec-rag-pipeline` as of release `v0.1.0`.

## ADR-001: New Repo, Not a Fork

- Status: Accepted
- Date: 2026-03-08
- Decision: Build this project as a clean repository rather than extending `stark-translate-agent`.
- Reason: The SEC ingestion, parsing, storage, and audit requirements are materially different from translation workflows.
- Consequence: The repo can keep a focused dependency set, milestone plan, and storage model without inheriting unrelated abstractions.

## ADR-002: PostgreSQL + pgvector, Not Pinecone

- Status: Accepted
- Date: 2026-03-08
- Decision: Use PostgreSQL 16 with `pgvector` as the primary durable store for chunk metadata and local vector-capable storage.
- Reason: The project needs relational joins, idempotent writes, and local reproducibility in addition to vector search.
- Consequence: Storage stays self-hostable and auditable, with Qdrant layered in for retrieval-oriented indexing rather than replacing the system of record.

## ADR-003: Dual-Format Table Storage

- Status: Accepted
- Date: 2026-03-08
- Decision: Preserve SEC tables both as structured rows and as linearized text.
- Reason: Structured rows support deterministic extraction and downstream validation, while linearized text works better for chunking, LLM prompts, and citation context.
- Consequence: Table blocks and chunk records carry more metadata, but later retrieval and audit steps become simpler and more reliable.

## ADR-004: Ollama Fallback Over Deterministic-Only Parsing

- Status: Accepted
- Date: 2026-03-08
- Decision: Keep deterministic extraction as the first path, but add an Ollama-backed fallback when the table parser cannot recover clean rows or OpenAI is unavailable.
- Reason: Real-world DEF 14A tables vary enough that a deterministic-only pipeline leaves too many gaps for batch coverage targets.
- Consequence: The system can run locally with `OPENAI_API_KEY=dummy`, while still preferring the more structured and cheaper deterministic route when possible.

## ADR-005: Table-Atomic Chunking

- Status: Accepted
- Date: 2026-03-08
- Decision: Never split SEC tables across multiple chunks.
- Reason: Compensation tables and governance tables lose meaning when row context is separated across chunk boundaries.
- Consequence: Some chunks are larger than prose chunks, but retrieval quality and auditability are better preserved.

## ADR-006: edgartools for EDGAR Ingestion

- Status: Accepted
- Date: 2026-03-08
- Decision: Use `edgartools` where it improves accession discovery and EDGAR lookup workflows, while keeping direct HTTP requests for explicit SEC endpoints.
- Reason: Some filing resolution paths are easier through `edgartools`, especially for latest-filing discovery and incomplete manifest rows.
- Consequence: Ingestion remains pragmatic rather than ideologically pure, with both direct requests and library-assisted fetch paths in the repo.

## ADR-007: S&P 500 as Batch Scope

- Status: Accepted
- Date: 2026-03-08
- Decision: Use the S&P 500 as the default batch universe for the first large-scale manifest and extraction workflow.
- Reason: It is a practical, high-signal benchmark set for executive compensation coverage and repeatable evaluation.
- Consequence: The repo ships manifest tooling, download logs, and validation expectations shaped around S&P 500 scale rather than ad hoc one-off filings.

## ADR-008: TOC-Bounded CD&A Extraction with Heading Fallback

- Status: Accepted
- Date: 2026-03-09
- Decision: Bound CD&A narrative extraction by TOC-derived page ranges when available, while preserving heading and text-pattern boundaries as fallback.
- Reason: Cached SEC HTML frequently includes strong pagination markers and TOC page ranges, but heading detection coverage is not universal across filers and templates.
- Consequence: CD&A extraction is more deterministic for long sections and less likely to run into downstream compensation tables, while still remaining robust when TOC or heading signals are incomplete.
