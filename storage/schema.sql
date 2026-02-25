-- SEC RAG Pipeline — PostgreSQL + pgvector schema
-- Run once at init; also mounted in docker-compose for auto-creation

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;  -- for GIN full-text / BM25-style search

-- ─────────────────────────────────────────────────────────────────
-- 1. Documents
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS documents (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cik           TEXT NOT NULL,
    company_name  TEXT NOT NULL,
    filing_date   DATE NOT NULL,
    doc_type      TEXT NOT NULL,  -- DEF14A, 10-K, etc.
    accession_no  TEXT UNIQUE,
    s3_path       TEXT,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_documents_cik ON documents(cik);
CREATE INDEX IF NOT EXISTS idx_documents_filing_date ON documents(filing_date);

-- ─────────────────────────────────────────────────────────────────
-- 2. Sections
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sections (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id    UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    section_header TEXT,
    order_index    INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sections_document_id ON sections(document_id);

-- ─────────────────────────────────────────────────────────────────
-- 3. Chunks
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS chunks (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    section_id       UUID NOT NULL REFERENCES sections(id) ON DELETE CASCADE,
    text             TEXT NOT NULL,
    chunk_type       TEXT NOT NULL,  -- paragraph, table, heading, footnote
    token_count      INTEGER,
    chunk_index      INTEGER,
    table_json       JSONB,          -- rows: list[list[str]] for structured queries
    linearized_text  TEXT,           -- flattened table text for embedding
    fts_vector       tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
    created_at       TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_section_id ON chunks(section_id);
CREATE INDEX IF NOT EXISTS idx_chunks_fts ON chunks USING GIN(fts_vector);
CREATE INDEX IF NOT EXISTS idx_chunks_chunk_type ON chunks(chunk_type);

-- ─────────────────────────────────────────────────────────────────
-- 4. Embeddings
-- ─────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS embeddings (
    id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    chunk_id  UUID NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
    model     TEXT NOT NULL,   -- voyage-finance-2
    vector    vector(1024),    -- Voyage Finance-2 output dimension
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_embeddings_chunk_id ON embeddings(chunk_id);
-- IVFFlat index — rebuild after bulk ingest with VACUUM ANALYZE
CREATE INDEX IF NOT EXISTS idx_embeddings_vector
    ON embeddings USING ivfflat (vector vector_cosine_ops)
    WITH (lists = 100);
