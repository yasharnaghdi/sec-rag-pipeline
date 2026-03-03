"""PostgreSQL persistence for SEC chunks."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Coroutine
from threading import Thread
from typing import Any, TypeVar, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ingestion.metadata_model import DocumentMetadata
from ingestion.sec_chunker import Chunk

logger = logging.getLogger(__name__)
T = TypeVar("T")


class ChunkWriter:
    """Write parsed chunks into PostgreSQL with idempotent upsert semantics."""

    def __init__(self, db_url: str | None = None) -> None:
        configured_db_url = db_url or os.getenv("DB_URL") or os.getenv("db_url")
        if not configured_db_url:
            raise EnvironmentError("DB_URL environment variable is required.")
        self._db_url = _to_async_db_url(configured_db_url)

    def write_chunks(self, chunks: list[Chunk], metadata: DocumentMetadata) -> int:
        """Insert chunks into Postgres and upsert on chunk id."""
        if not chunks:
            return 0
        return _run_coroutine_sync(self._write_chunks_async(chunks, metadata))

    async def _write_chunks_async(self, chunks: list[Chunk], metadata: DocumentMetadata) -> int:
        engine = create_async_engine(self._db_url)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    text("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS citation_string TEXT")
                )
                await conn.execute(
                    text("ALTER TABLE chunks ADD COLUMN IF NOT EXISTS table_json JSONB")
                )

                document_uuid = _stable_uuid(f"document:{metadata.document_id}")
                await conn.execute(
                    text(
                        """
                        INSERT INTO documents (id, cik, company_name, filing_date, doc_type, accession_no, s3_path)
                        VALUES (:id, :cik, :company_name, :filing_date, :doc_type, :accession_no, :s3_path)
                        ON CONFLICT (accession_no)
                        DO UPDATE SET
                            cik = EXCLUDED.cik,
                            company_name = EXCLUDED.company_name,
                            filing_date = EXCLUDED.filing_date,
                            doc_type = EXCLUDED.doc_type,
                            s3_path = EXCLUDED.s3_path
                        """
                    ),
                    {
                        "id": document_uuid,
                        "cik": metadata.cik,
                        "company_name": metadata.company_name,
                        "filing_date": metadata.filing_date,
                        "doc_type": metadata.form_type,
                        "accession_no": metadata.accession_number,
                        "s3_path": metadata.raw_html_path,
                    },
                )

                section_id_map = _build_section_id_map(chunks, metadata.document_id)
                for order_index, (source_section_id, section_uuid) in enumerate(section_id_map.items()):
                    await conn.execute(
                        text(
                            """
                            INSERT INTO sections (id, document_id, section_header, order_index)
                            VALUES (:id, :document_id, :section_header, :order_index)
                            ON CONFLICT (id)
                            DO UPDATE SET
                                section_header = EXCLUDED.section_header,
                                order_index = EXCLUDED.order_index
                            """
                        ),
                        {
                            "id": section_uuid,
                            "document_id": document_uuid,
                            "section_header": source_section_id,
                            "order_index": order_index,
                        },
                    )

                insert_chunk_stmt = text(
                    """
                    INSERT INTO chunks (
                        id,
                        section_id,
                        text,
                        chunk_type,
                        token_count,
                        chunk_index,
                        table_json,
                        linearized_text,
                        citation_string
                    )
                    VALUES (
                        :id,
                        :section_id,
                        :text,
                        :chunk_type,
                        :token_count,
                        :chunk_index,
                        :table_json,
                        :linearized_text,
                        :citation_string
                    )
                    ON CONFLICT (id)
                    DO UPDATE SET
                        section_id = EXCLUDED.section_id,
                        text = EXCLUDED.text,
                        chunk_type = EXCLUDED.chunk_type,
                        token_count = EXCLUDED.token_count,
                        chunk_index = EXCLUDED.chunk_index,
                        table_json = EXCLUDED.table_json,
                        linearized_text = EXCLUDED.linearized_text,
                        citation_string = EXCLUDED.citation_string
                    """
                )

                for chunk in chunks:
                    section_uuid = section_id_map[chunk.section_id]
                    table_payload = _json_payload(chunk.table_json)
                    await conn.execute(
                        insert_chunk_stmt,
                        {
                            "id": _coerce_uuid(chunk.id),
                            "section_id": section_uuid,
                            "text": chunk.text,
                            "chunk_type": "table" if table_payload is not None else "paragraph",
                            "token_count": chunk.token_count,
                            "chunk_index": chunk.chunk_index,
                            "table_json": table_payload,
                            "linearized_text": chunk.text if table_payload is not None else None,
                            "citation_string": chunk.citation_string,
                        },
                    )
        finally:
            await engine.dispose()

        logger.info(
            "wrote_chunks rows=%s document_id=%s accession=%s",
            len(chunks),
            metadata.document_id,
            metadata.accession_number,
        )
        return len(chunks)


def _to_async_db_url(db_url: str) -> str:
    if db_url.startswith("postgresql+asyncpg://"):
        return db_url
    if db_url.startswith("postgresql+psycopg2://"):
        return db_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://", 1)
    if db_url.startswith("postgresql+psycopg://"):
        return db_url.replace("postgresql+psycopg://", "postgresql+asyncpg://", 1)
    if db_url.startswith("postgresql://"):
        return db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    if db_url.startswith("postgres://"):
        return db_url.replace("postgres://", "postgresql+asyncpg://", 1)
    return db_url


def _stable_uuid(key: str) -> UUID:
    return uuid5(NAMESPACE_URL, key)


def _coerce_uuid(raw_value: str) -> UUID:
    try:
        return UUID(raw_value)
    except ValueError:
        return _stable_uuid(raw_value)


def _build_section_id_map(chunks: list[Chunk], document_id: str) -> OrderedDict[str, UUID]:
    section_ids: OrderedDict[str, UUID] = OrderedDict()
    for chunk in chunks:
        source_section_id = chunk.section_id
        if source_section_id not in section_ids:
            section_ids[source_section_id] = _stable_uuid(f"section:{document_id}:{source_section_id}")
    return section_ids


def _json_payload(raw_json: str | None) -> object | None:
    if raw_json is None:
        return None
    try:
        return cast(object, json.loads(raw_json))
    except json.JSONDecodeError:
        return raw_json


def _run_coroutine_sync(coroutine: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)

    result: list[T] = []
    raised: list[BaseException] = []

    def _target() -> None:
        try:
            result.append(asyncio.run(coroutine))
        except BaseException as exc:  # pragma: no cover - defensive boundary
            raised.append(exc)

    worker = Thread(target=_target, daemon=True)
    worker.start()
    worker.join()

    if raised:
        raise RuntimeError("ChunkWriter failed in background event loop.") from raised[0]
    if not result:
        raise RuntimeError("ChunkWriter completed without a result.")
    return result[0]
