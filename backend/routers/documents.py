"""Upload, Word->PDF conversion, and page rasterization -- the HTTP equivalents of
file_io.rs's `pick_document`, `ensure_pdf_path`, and `rasterize_document` Tauri commands.

There is no local "path" concept on the client side anymore: the browser uploads file bytes
directly (`pick_document`'s native file dialog -> `<input type="file">`), and every
subsequent step is addressed by a file id scoped to the caller's session. Request fields are
still named `path` (matching every existing `invoke(cmd, { path: ... })` call site in app.js
exactly) even though the value is now a file id, not a filesystem path -- this is what lets
those call sites stay completely untouched.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Header, HTTPException, UploadFile

from ..schemas import CamelModel
from ..sessions import new_file_id, session_dir
from ..services import pdf as pdf_service
from ..services.office_convert import OfficeConversionError, convert_to_pdf

router = APIRouter(prefix="/api", tags=["documents"])

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".doc"}


def _strip_surrounding_quotes(name: str) -> str:
    """Defense-in-depth against a filename arriving with surrounding quote characters still
    attached (see file_io.rs's identically-named helper and its docstring for why this
    matters for Windows "Copy as path" pastes)."""
    trimmed = name.strip()
    for quote in ('"', "'"):
        if len(trimmed) >= 2 and trimmed.startswith(quote) and trimmed.endswith(quote):
            return trimmed[1:-1].strip()
    return trimmed


async def _save_upload(file: UploadFile, x_session_id: str) -> dict:
    name = _strip_surrounding_quotes(file.filename or "document")
    ext = Path(name).suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise HTTPException(400, f"unsupported file type: {ext or '(none)'}")

    file_id = new_file_id()
    dest = session_dir(x_session_id) / f"{file_id}{ext}"
    contents = await file.read()
    dest.write_bytes(contents)

    return {"file_id": file_id, "name": name, "ext": ext}


@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    x_session_id: str = Header(...),
):
    return await _save_upload(file, x_session_id)


@router.post("/upload-multi")
async def upload_multi(
    files: list[UploadFile] = File(...),
    x_session_id: str = Header(...),
):
    """HTTP equivalent of the Tauri desktop app's `pick_documents` command (multi-select) --
    used for attaching several CLIPS forms/Non-PK Data Mgmt Worksheets at once. Returns a list
    of {file_id, name, ext}, one per uploaded file, same shape /api/upload returns for a single
    file."""
    return [await _save_upload(f, x_session_id) for f in files]


def _find_session_file(session_id: str, file_id: str) -> Path:
    matches = list(session_dir(session_id).glob(f"{file_id}.*"))
    if not matches:
        raise HTTPException(404, f"file not found for this session: {file_id}")
    return matches[0]


class ConvertRequest(CamelModel):
    path: str  # file id


@router.post("/convert-to-pdf")
async def convert_to_pdf_endpoint(body: ConvertRequest, x_session_id: str = Header(...)):
    src = _find_session_file(x_session_id, body.path)
    if src.suffix.lower() == ".pdf":
        return {"file_id": body.path}

    out_dir = session_dir(x_session_id)
    try:
        converted_path = convert_to_pdf(str(src), str(out_dir))
    except OfficeConversionError as exc:
        raise HTTPException(500, str(exc)) from exc

    new_id = new_file_id()
    final_path = out_dir / f"{new_id}.pdf"
    Path(converted_path).replace(final_path)
    return {"file_id": new_id}


class RasterizeRequest(CamelModel):
    path: str  # file id
    dpi: int = 150
    pages: Optional[list[int]] = None


@router.post("/rasterize")
async def rasterize(body: RasterizeRequest, x_session_id: str = Header(...)):
    src = _find_session_file(x_session_id, body.path)
    if src.suffix.lower() != ".pdf":
        raise HTTPException(400, "rasterize requires a PDF file id (convert first)")
    return pdf_service.rasterize_pdf(str(src), dpi=body.dpi, only_pages=body.pages)
