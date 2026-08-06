"""Table/text extraction and the "Master SoA" consolidated-schedule mapper -- the HTTP
equivalents of `extract_tables` and `extract_master_schedule`."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from ..schemas import CamelModel
from ..sessions import session_dir
from ..services.tables_extract import extract_tables as extract_tables_service
from .documents import _find_session_file

router = APIRouter(prefix="/api", tags=["tables"])


class ExtractTablesRequest(CamelModel):
    path: str  # file id
    flavor: str = "lattice"
    table_areas_by_page: dict[str, list[str]] = {}
    flavor_by_page: dict[str, str] = {}
    # 0-indexed page subset (e.g. a Combine-Tables merge region) -- restricts extraction to
    # just these pages instead of re-scanning the whole document. Omit for the main workspace
    # parse and "Tables Only" autodetect, which both need every page.
    pages: Optional[list[int]] = None


@router.post("/extract-tables")
async def extract_tables(body: ExtractTablesRequest, x_session_id: str = Header(...)):
    src = _find_session_file(x_session_id, body.path)
    if src.suffix.lower() != ".pdf":
        raise HTTPException(400, "extract-tables requires a PDF file id (convert first)")
    return extract_tables_service(
        str(src),
        flavor=body.flavor,
        table_areas_by_page=body.table_areas_by_page,
        flavor_by_page=body.flavor_by_page,
        pages=body.pages,
    )


class MasterScheduleRequest(CamelModel):
    protocol_text: str


@router.post("/master-schedule")
async def master_schedule(body: MasterScheduleRequest, x_session_id: str = Header(...)):
    from ..services import clinical_mapper

    text_file = session_dir(x_session_id) / "schedule-text.txt"
    text_file.write_text(body.protocol_text, encoding="utf-8")
    try:
        return clinical_mapper.process_file(str(text_file))
    finally:
        text_file.unlink(missing_ok=True)
