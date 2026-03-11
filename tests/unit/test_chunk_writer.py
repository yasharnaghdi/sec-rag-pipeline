from __future__ import annotations

import json
import os
import re
from collections.abc import AsyncGenerator
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from ingestion.metadata_model import DocumentMetadata
from ingestion.sec_chunker import Chunk
from storage.writer import ChunkWriter, _to_async_db_url


def _strip_sql_comments(sql: str) -> str:
    """Remove -- line comments before splitting on semicolons."""
    return re.sub(r"--[^\n]*", "", sql)


def _async_db_url(raw_db_url: str) -> str:
    if raw_db_url.startswith("postgresql+asyncpg://"):
        return raw_db_url
    if raw_db_url.startswith("postgresql://"):
        return raw_db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if raw_db_url.startswith("postgres://"):
        return raw_db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return raw_db_url


def _metadata() -> DocumentMetadata:
    return DocumentMetadata(
        document_id="320193_000130817924000010",
        cik="320193",
        company_name="Apple Inc",
        form_type="DEF 14A",
        filing_date=date(2024, 1, 11),
        accession_number="0001308179-24-000010",
        source_url="https://www.sec.gov/Archives/edgar/data/320193/example.htm",
        fiscal_year_end=None,
        raw_html_path="data/raw/320193_0001308179_24_000010.html",
    )


def _chunks(metadata: DocumentMetadata) -> list[Chunk]:
    table_payload = {
        "rows": [["Year", "Salary"], ["2024", "1000"]],
        "linearized_text": "Year | Salary | 2024 | 1000",
    }
    return [
        Chunk(
            id="00000000-0000-0000-0000-000000000001",
            source_block_id="block-1",
            document_id=metadata.document_id,
            section_id="preamble",
            text="Intro paragraph for governance context.",
            token_count=6,
            chunk_index=0,
            citation_string="Apple Inc | DEF 14A | 2024-01-11 | preamble | chunk 0",
            table_json=None,
        ),
        Chunk(
            id="00000000-0000-0000-0000-000000000002",
            source_block_id="block-2",
            document_id=metadata.document_id,
            section_id="section_summary_comp",
            text="Year | Salary | 2024 | 1000",
            token_count=6,
            chunk_index=1,
            citation_string="Apple Inc | DEF 14A | 2024-01-11 | section_summary_comp | chunk 1",
            table_json=json.dumps(table_payload),
        ),
        Chunk(
            id="00000000-0000-0000-0000-000000000003",
            source_block_id="block-3",
            document_id=metadata.document_id,
            section_id="section_summary_comp",
            text="Additional paragraph after table.",
            token_count=4,
            chunk_index=2,
            citation_string="Apple Inc | DEF 14A | 2024-01-11 | section_summary_comp | chunk 2",
            table_json=None,
        ),
    ]


def test_to_async_db_url_converts_psycopg_urls() -> None:
    assert _to_async_db_url("postgresql+psycopg2://user:pw@host/db") == "postgresql+asyncpg://user:pw@host/db"
    assert _to_async_db_url("postgresql+psycopg://user:pw@host/db") == "postgresql+asyncpg://user:pw@host/db"


def test_to_async_db_url_preserves_asyncpg_url() -> None:
    url = "postgresql+asyncpg://user:pw@host/db"
    assert _to_async_db_url(url) == url


@pytest_asyncio.fixture()
async def db_engine() -> AsyncGenerator[AsyncEngine, None]:
    raw_db_url = os.getenv("DB_URL")
    if not raw_db_url:
        pytest.skip("DB_URL is required for integration tests.")

    engine = create_async_engine(_async_db_url(raw_db_url))
    schema_sql = Path("storage/schema.sql").read_text(encoding="utf-8")
    migration_sql = Path("storage/migrations/001_add_citation_and_table_json.sql").read_text(
        encoding="utf-8"
    )

    try:
        async with engine.begin() as conn:
            for statement in _strip_sql_comments(schema_sql).split(";"):
                cleaned = statement.strip()
                if cleaned:
                    await conn.execute(text(cleaned))
            for statement in _strip_sql_comments(migration_sql).split(";"):
                cleaned = statement.strip()
                if cleaned:
                    await conn.execute(text(cleaned))
    except DBAPIError as exc:
        await engine.dispose()
        pytest.skip(f"DB integration tests require a PostgreSQL instance with pgvector: {exc}")

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture()
async def _truncate_tables(db_engine: AsyncEngine) -> None:
    async with db_engine.begin() as conn:
        await conn.execute(text("TRUNCATE TABLE embeddings, chunks, sections, documents CASCADE"))


@pytest.mark.asyncio
async def test_write_chunks_and_read_back_fields(
    db_engine: AsyncEngine,
    _truncate_tables: None,
) -> None:
    metadata = _metadata()
    chunks = _chunks(metadata)
    writer = ChunkWriter(db_url=os.environ["DB_URL"])

    written = writer.write_chunks(chunks, metadata)

    assert written == 3
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT id::text AS chunk_id, citation_string, token_count
                FROM chunks
                ORDER BY chunk_index
                """
            )
        )
        rows = result.mappings().all()

    assert [row["chunk_id"] for row in rows] == [chunk.id for chunk in chunks]
    assert [row["citation_string"] for row in rows] == [chunk.citation_string for chunk in chunks]
    assert [row["token_count"] for row in rows] == [chunk.token_count for chunk in chunks]


@pytest.mark.asyncio
async def test_write_chunks_is_idempotent_on_chunk_id(
    db_engine: AsyncEngine,
    _truncate_tables: None,
) -> None:
    metadata = _metadata()
    chunks = _chunks(metadata)
    writer = ChunkWriter(db_url=os.environ["DB_URL"])

    writer.write_chunks(chunks, metadata)
    writer.write_chunks(chunks, metadata)

    async with db_engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM chunks"))
        row_count = result.scalar_one()

    assert row_count == len(chunks)


@pytest.mark.asyncio
async def test_table_json_is_stored_and_retrievable(
    db_engine: AsyncEngine,
    _truncate_tables: None,
) -> None:
    metadata = _metadata()
    chunks = _chunks(metadata)
    writer = ChunkWriter(db_url=os.environ["DB_URL"])

    writer.write_chunks(chunks, metadata)

    async with db_engine.connect() as conn:
        result = await conn.execute(
            text(
                """
                SELECT table_json
                FROM chunks
                WHERE id = '00000000-0000-0000-0000-000000000002'::uuid
                """
            )
        )
        table_json = result.scalar_one()

    assert table_json is not None
    assert table_json["rows"][0] == ["Year", "Salary"]
