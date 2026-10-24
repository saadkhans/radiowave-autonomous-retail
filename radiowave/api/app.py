"""FastAPI application for the Radiowave Observatory.

Run locally with ``pnpm run dev:api`` (uvicorn on port 8765). Everything is
in-process and synthetic by default: no persistence, no authentication, no hardware.

Setting ``RADIOWAVE_TI_CONFIG`` to a ``TiLiveConfig`` JSON file enables LIVE runs
backed by one TI mmWave radar (``POST /api/runs/live``). A missing or invalid file,
or a missing serial dependency, never stops the application: replay keeps working
and ``GET /api/live/status`` explains why live mode is unavailable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from radiowave import __version__
from radiowave.api.live import LiveRuntime
from radiowave.api.routes import live, runs, scenarios, sim
from radiowave.api.runs import RunManager
from radiowave.api.sim import SimRuntime

DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]
LIVE_CONFIG_ENV = "RADIOWAVE_TI_CONFIG"

log = logging.getLogger(__name__)


def live_runtime_from_env() -> LiveRuntime | None:
    """Load the live sensor configuration named by ``RADIOWAVE_TI_CONFIG``, if any."""
    path = os.environ.get(LIVE_CONFIG_ENV)
    if not path:
        return None
    from radiowave.adapters.mmwave.ti.config import TiLiveConfig

    try:
        config = TiLiveConfig.load(path)
    except (OSError, ValueError) as exc:
        log.warning("ignoring %s=%r: %s", LIVE_CONFIG_ENV, path, exc)
        return None
    return LiveRuntime(config=config, config_path=path)


def create_app(live_runtime: LiveRuntime | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        manager: RunManager = app.state.runs
        manager.close_all()  # stop live sessions so shutdown never leaves a reader thread

    app = FastAPI(
        title="Radiowave Observatory API",
        version=__version__,
        description="Local engineering bridge over the deterministic Foundation v0 engine.",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=DEV_ORIGINS,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.state.runs = RunManager()
    app.state.live = live_runtime
    # The virtual store lab is always available: it needs no hardware, no config
    # file and no serial port, so it is wired unconditionally.
    app.state.sim = SimRuntime()

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "engine": "foundation-v0", "version": __version__}

    app.include_router(scenarios.router, prefix="/api")
    app.include_router(runs.router, prefix="/api")
    app.include_router(live.router, prefix="/api")
    app.include_router(sim.router, prefix="/api")
    return app


app = create_app(live_runtime_from_env())
