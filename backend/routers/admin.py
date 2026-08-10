"""Server-side landing spot for the Fabric daily extract file.

The container has no OneDrive client, so it can't read the SharePoint-synced
copy `fabric_daily_extract.py` writes on the desktop. Instead, the desktop
scheduled task pushes the same file here over HTTP after each run, and this
just saves it to FABRIC_EXTRACT_PATH (the persistent-volume path
`fabric_daily_extract.py`/`fabric_extract_lookup.py` already read/write via
that env var) -- no SharePoint/Graph credentials of any kind live in the
container.

Token-gated rather than open, since this is a write endpoint reachable
through the same ingress as everything else: the caller must send the
FABRIC_UPLOAD_TOKEN value (set as a Kubernetes secret) as `X-Upload-Token`.
If the env var isn't set at all (e.g. local dev), the check is skipped.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, File, Header, HTTPException, UploadFile

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _extract_path() -> Path:
    import fabric_daily_extract

    return Path(fabric_daily_extract.DEFAULT_OUTPUT_PATH)


@router.post("/fabric-extract-upload")
async def fabric_extract_upload(
    file: UploadFile = File(...),
    x_upload_token: str = Header(default=""),
):
    expected_token = os.environ.get("FABRIC_UPLOAD_TOKEN", "")
    if expected_token and x_upload_token != expected_token:
        raise HTTPException(401, "invalid upload token")

    dest = _extract_path()
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Write to a temp file alongside the target, then atomically replace --
    # avoids a reader (fabric_extract_lookup.py, mid-request) ever seeing a
    # half-written file.
    tmp_path = dest.with_suffix(dest.suffix + ".tmp")
    with tmp_path.open("wb") as out:
        while chunk := await file.read(1024 * 1024):
            out.write(chunk)
    tmp_path.replace(dest)

    return {"status": "ok", "bytes": dest.stat().st_size}
