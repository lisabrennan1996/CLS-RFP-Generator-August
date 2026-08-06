"""RFP Webapp -- FastAPI entry point.

Serves the ported frontend (frontend/) as static files and mounts the API routers that
replace CLS Studio's Tauri commands. Run locally from the `rfp-webapp/` directory (so
`backend` resolves as a top-level package) with:

    uvicorn backend.main:app --reload

(see repo README for the exact working-directory/venv setup).
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .routers import documents, export, rfp, tables
from .sessions import sweep_old_sessions

FRONTEND_DIR = Path(__file__).resolve().parents[1] / "frontend"

# CATS routes traffic through an ingress with a path prefix (e.g. "/cls-rfp-generator") --
# FastAPI needs to know that prefix (via `root_path`) so it generates correct URLs for
# OpenAPI/docs and any redirect responses, even though the app itself is still mounted at
# "/" underneath it. Left empty for local `uvicorn --reload` runs (no ingress in front);
# set the ROOT_PATH env var to the actual ingress prefix in the Kubernetes deployment.
ROOT_PATH = os.environ.get("ROOT_PATH", "")


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(60 * 60)  # hourly
        sweep_old_sessions()


@asynccontextmanager
async def lifespan(app: FastAPI):
    sweep_old_sessions()  # clear anything stale from a previous run on startup
    task = asyncio.create_task(_sweep_loop())
    yield
    task.cancel()


app = FastAPI(title="RFP Webapp", lifespan=lifespan, root_path=ROOT_PATH)

app.include_router(documents.router)
app.include_router(tables.router)
app.include_router(rfp.router)
app.include_router(export.router)

# Mounted last so it doesn't shadow the /api/* routes above; serves index.html at "/" and
# every other frontend asset (styles.css, app.js, blocks-view.js, master-table.js,
# master-schedule.js) unchanged.
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
