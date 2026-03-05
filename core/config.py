"""Application configuration — ported from stark-translate-agent config.py, re-parameterised for SEC RAG."""
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # EDGAR
    edgar_user_agent: str = Field(..., description="EDGAR HTTP User-Agent string")
    edgar_download_dir: Path = Path("./data/raw")

    # Embeddings
    voyage_api_key: str = Field(...)
    voyage_model: str = "voyage-finance-2"

    # LLM
    openai_api_key: str = Field(...)
    openai_model: str = "gpt-4o"

    # Database
    db_url: str = Field(...)
    db_pool_size: int = 10

    # Vector Store
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "sec_chunks"

    # Chunking
    chunk_size_tokens: int = 600
    chunk_overlap_tokens: int = 100

    # Retrieval
    retrieval_top_k: int = 20
    rerank_top_n: int = 5

    # App
    app_env: str = "development"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
