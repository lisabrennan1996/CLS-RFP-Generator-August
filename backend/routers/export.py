"""File download -- the HTTP equivalent of `pick_save_path`/`export_output`. There's no native
"Save As" location picker in a browser; the frontend triggers a download via a Blob response +
`<a download>`, and the browser saves to its own configured downloads location instead of a
user-chosen arbitrary path.
"""
from __future__ import annotations

from fastapi import APIRouter, Header
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..sessions import new_file_id, session_dir
from .documents import _find_session_file

router = APIRouter(prefix="/api", tags=["export"])

_CONTENT_TYPES = {
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pdf": "application/pdf",
    ".json": "application/json",
    ".md": "text/markdown",
    ".html": "text/html",
    ".csv": "text/csv",
    ".txt": "text/plain",
}


@router.get("/download/{file_id}")
async def download(file_id: str, name: str = "download", x_session_id: str = Header(...)):
    path = _find_session_file(x_session_id, file_id)
    media_type = _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=name)


class ExportTextRequest(BaseModel):
    content: str
    suggested_name: str


@router.post("/export-text")
async def export_text(body: ExportTextRequest, x_session_id: str = Header(...)):
    from pathlib import Path

    ext = Path(body.suggested_name).suffix or ".txt"
    file_id = new_file_id()
    dest = session_dir(x_session_id) / f"{file_id}{ext}"
    dest.write_text(body.content, encoding="utf-8")
    return {"file_id": file_id, "name": body.suggested_name}
