"""FastAPI application entry point."""
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core.config import get_settings

app = FastAPI(
    title="SEC RAG Pipeline",
    description="Retrieval-Augmented Generation over SEC proxy filings",
    version="0.1.0",
)


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({"status": "ok", "env": get_settings().app_env})


# TODO (Phase 5): mount routers
# from api.routers import ingest, query
# app.include_router(ingest.router, prefix="/ingest", tags=["Ingest"])
# app.include_router(query.router, prefix="/query", tags=["Query"])
