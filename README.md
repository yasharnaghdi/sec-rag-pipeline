# sec-rag-pipeline

SEC filing RAG pipeline for DEF 14A and 10-K documents.

This repository implements a deterministic ingestion path from SEC EDGAR HTML to auditable chunk records in PostgreSQL, with citation-ready metadata for downstream retrieval and generation.

## Current milestone status

M0 is complete for fixture processing:
- Download + cache filings from `fixtures/manifest.csv`
- Parse SEC HTML into typed blocks (`SECHTMLParser`)
- Chunk blocks with citation strings (`SECChunker`)
- Persist chunks idempotently into Postgres (`ChunkWriter`)
- Produce batch evidence output in `output/m0_batch_summary.csv`

## Pipeline

1. `ingestion/downloader.py` resolves accession numbers (including TBD via edgartools) and caches raw HTML.
2. `ingestion/sec_html_parser.py` extracts headings, prose, tables, footnotes, images, and XBRL-tagged content.
3. `ingestion/sec_chunker.py` emits token-bounded chunks (tables are atomic).
4. `storage/writer.py` writes documents/sections/chunks into PostgreSQL with upsert-on-chunk-id behavior.

## Key notebooks

- `notebooks/03_ingest_parse_chunk.ipynb`: single-filing audit run and chunk export.
- `notebooks/04_batch_ingest.ipynb`: all 5 fixture filings in one session with M0 gate assertions.

## Local setup

```bash
cp .env.example .env
# Fill required env vars (SEC_USER_AGENT, DB_URL, API keys for later milestones)

docker compose up -d postgres qdrant
poetry install
```

## Validation commands

```bash
pytest tests/unit/ -v
mypy --strict ingestion/sec_html_parser.py ingestion/sec_chunker.py ingestion/downloader.py storage/writer.py
ruff check ingestion/ storage/ tests/unit/
```

## Repository structure

- `ingestion/` SEC download, parsing, and chunking
- `storage/` database schema, migrations, and write layer
- `retrieval/` hybrid ranking logic
- `generation/` prompt/citation assembly
- `notebooks/` reproducible audit artifacts
- `docs/` technical and researcher-facing project status
