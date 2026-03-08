"""Application configuration — ported from stark-translate-agent config.py, re-parameterised for SEC RAG.

Change log:
  v0.1.0 — added llm_provider, ollama_*, edgar_rate_limit_sleep, sec_user_agent fields
           to match .env.example; extra='ignore' prevents ValidationError on unknown env vars.
"""
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # silently ignore env vars not declared as fields
    )

    # ── EDGAR / SEC ────────────────────────────────────────────────────────────
    # SEC mandates a real User-Agent: "First Last email@domain.com"
    # Accepted as both EDGAR_USER_AGENT and SEC_USER_AGENT for back-compat.
    edgar_user_agent: str = Field(
        default="anonymous user@example.com",
        description="SEC EDGAR HTTP User-Agent (First Last email)",
    )
    edgar_download_dir: Path = Path("./data/raw")
    edgar_rate_limit_sleep: float = Field(
        default=0.1,
        description="Seconds to sleep between EDGAR requests (max 10 req/s)",
    )

    # ── LLM Provider ───────────────────────────────────────────────────────────
    llm_provider: Literal["openai", "ollama"] = Field(
        default="openai",
        description="Primary LLM provider. Set to 'ollama' for local fallback.",
    )

    # OpenAI
    openai_api_key: str = Field(default="dummy")
    openai_model: str = "gpt-4o"

    # Ollama (local fallback)
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama API base URL. Use http://sec_ollama:11434 inside Docker.",
    )
    ollama_model: str = Field(default="llama3.1")

    # ── Embeddings ─────────────────────────────────────────────────────────────
    # Leave blank until Phase 2 (Voyage Finance-2) is active.
    voyage_api_key: str = Field(default="")
    voyage_model: str = "voyage-finance-2"

    # ── Database ───────────────────────────────────────────────────────────────
    # Default matches docker-compose postgres service.
    db_url: str = Field(
        default="postgresql+asyncpg://postgres:password@localhost:5432/sec_rag"
    )
    db_pool_size: int = 10

    # ── Vector Store ───────────────────────────────────────────────────────────
    qdrant_url: str = "http://localhost:6333"
    qdrant_collection: str = "sec_chunks"

    # ── Chunking ───────────────────────────────────────────────────────────────
    chunk_size_tokens: int = 600
    chunk_overlap_tokens: int = 100

    # ── Retrieval ──────────────────────────────────────────────────────────────
    retrieval_top_k: int = 20
    rerank_top_n: int = 5

    # ── App ────────────────────────────────────────────────────────────────────
    app_env: str = "development"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
