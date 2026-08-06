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

router = APIRouter(prefix="/api", tags=["documents"])

# LibreOffice-based Word->PDF conversion was removed (not supported on this deployment's
# servers) -- PDFs upload and work as before. ".docx" is still accepted (not ".doc", which
# python-docx can't read at all) purely for "Previous RFP" attachment, which reads a .docx
# directly via python-docx/build_specimen.py and never goes through /api/convert-to-pdf at
# all -- see that endpoint's own docstring for the one path that still needs a real PDF.
SUPPORTED_EXTENSIONS = {".pdf", ".docx"}


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
    """Word (.docx) -> PDF conversion via LibreOffice was removed -- not supported on this
    deployment's servers. This endpoint is still called by the frontend's `ensure_pdf_path`
    (the main protocol upload and the "Design Elements" attach both call it unconditionally,
    since it's a safe no-op passthrough for an already-PDF file) -- so a PDF input still
    passes straight through unchanged; a non-PDF input now fails with a clear, actionable
    error instead of shelling out to a LibreOffice binary that may not exist on this server.
    ("Previous RFP" attachment reads a .docx directly via python-docx/build_specimen.py and
    never calls this endpoint at all, so it's unaffected.)"""
    src = _find_session_file(x_session_id, body.path)
    if src.suffix.lower() == ".pdf":
        return {"file_id": body.path}

    raise HTTPException(
        400,
        "Word document upload isn't supported here -- please upload a PDF instead "
        f"(got {src.suffix.lower() or '(no extension)'}).",
    )


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
