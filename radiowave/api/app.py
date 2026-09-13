"""FastAPI application for the Radiowave Observatory.

Run locally with ``pnpm run dev:api`` (uvicorn on port 8765). Everything is
in-process and synthetic: no persistence, no authentication, no hardware.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from radiowave import __version__
from radiowave.api.routes import runs, scenarios
from radiowave.api.runs import RunManager

DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def create_app() -> FastAPI:
    app = FastAPI(
        title="Radiowave Observatory API",
        version=__version__,
        description="Local engineering bridge over the deterministic Foundation v0 engine.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.state.runs = RunManager()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "engine": "foundation-v0", "version": __version__}

    app.include_router(scenarios.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    return app


app = create_app()
