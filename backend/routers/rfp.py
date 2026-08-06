"""Schema preview + docx generation -- the HTTP equivalents of `extract_rfp_schema` and
`populate_rfp_docx`. Calls directly into the existing engine (referenced in place via
`engine_paths`, never copied) in-process, which is actually simpler than the desktop app's own
Rust->subprocess->Python chain: no CLI-argument/temp-file marshaling is needed since there's
no cross-language process boundary here at all.

Field names match the existing `invoke('extract_rfp_schema'|'populate_rfp_docx', {...})` call
sites in app.js exactly (via CamelModel's camelCase aliasing) so those call sites needed no
changes. Several fields (`soaTableOverride`, `labTableOverride`, `fieldOverrides`, `answers`)
arrive already JSON-encoded as strings -- app.js does `JSON.stringify(...)` on them before
calling `invoke`, since the original Tauri commands declared them as `Option<String>` -- so
they're `json.loads()`'d here rather than accepted as native nested objects.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Header, HTTPException

from .. import engine_paths
from ..schemas import CamelModel
from ..sessions import new_file_id, session_dir
from .documents import _find_session_file

engine_paths.ensure_on_path()

from preparsed_schema_extractor import extract_with_schema_v4  # noqa: E402
from populate_rfp import main as populate_rfp_main  # noqa: E402
from clips_nonpkpd_parser import parse_files as clips_nonpkpd_parse_files  # noqa: E402
from fabric_extract_lookup import lookup as fabric_lookup  # noqa: E402
from specimen_columns import list_columns as specimen_list_columns  # noqa: E402

router = APIRouter(prefix="/api", tags=["rfp"])

_SCHEMA_META_KEYS = {"schema_version", "protocol_fields", "design_fields", "rfp_engine_fields", "defaults_applied"}


def _parse_json_field(raw: Optional[str]) -> Optional[Any]:
    if not raw or not raw.strip():
        return None
    return json.loads(raw)


def _flatten_field_overrides(raw: Optional[dict]) -> Optional[dict]:
    """Same hybrid-shape flattening rfp_cli_bridge.py's --field-overrides-json handling does:
    accepts either preparsed_schema_extractor.py's own structured output
    ({"protocol_fields": [{"field","value"}, ...], ...}) or a flat {"field name": value} map --
    either way, wins over the engine's own protocol/design-element extraction for any field
    name it resolves a non-empty value for. A hybrid payload (the structured shape plus flat
    keys sitting alongside it, e.g. manual UI overrides merged in) layers the flat keys in last
    so they still win rather than silently vanishing."""
    if not raw:
        return None
    if isinstance(raw, dict) and any(k in raw for k in ("protocol_fields", "design_fields", "rfp_engine_fields")):
        flattened: dict[str, Any] = {}
        for key in ("protocol_fields", "design_fields", "rfp_engine_fields"):
            for spec in raw.get(key) or []:
                flattened[spec.get("field")] = spec.get("value")
        for key, value in raw.items():
            if key not in _SCHEMA_META_KEYS:
                flattened[key] = value
        return flattened
    return raw


class ExtractSchemaRequest(CamelModel):
    protocol_text: str
    design_text: str = ""
    previous_rfp_path: Optional[str] = None  # file id


@router.post("/extract-schema")
async def extract_schema(body: ExtractSchemaRequest, x_session_id: str = Header(...)):
    previous_rfp_path = ""
    if body.previous_rfp_path:
        previous_rfp_path = str(_find_session_file(x_session_id, body.previous_rfp_path))

    return extract_with_schema_v4(
        protocol_content=body.protocol_text,
        design_content=body.design_text,
        protocol_format="html",
        design_format="html",
        previous_rfp_path=previous_rfp_path,
    )


class GenerateRfpRequest(CamelModel):
    protocol_text: str
    design_text: str = ""
    soa_table_override: Optional[str] = None  # JSON-encoded {"headers","rows","footnotes"}
    lab_table_override: Optional[str] = None  # JSON-encoded [["Test","Comment"], ...]
    protocol_pdf_path: Optional[str] = None  # file id
    previous_rfp_path: Optional[str] = None  # file id
    field_overrides: Optional[str] = None  # JSON-encoded
    answers: Optional[str] = None  # JSON-encoded
    # JSON-encoded [{"path", "column"}, ...] -- same shape the Tauri command accepts, except
    # `path` here is a session file id (see /api/upload-multi) rather than a filesystem path,
    # resolved to a real path below the same way protocol_pdf_path/previous_rfp_path already are.
    clips_nonpkpd_assignments: Optional[str] = None


@router.post("/generate-rfp")
async def generate_rfp(body: GenerateRfpRequest, x_session_id: str = Header(...)):
    out_dir = session_dir(x_session_id)
    file_id = new_file_id()
    output_path = out_dir / f"{file_id}.docx"
    report_path = out_dir / f"{file_id}_report.md"

    protocol_pdf_path = ""
    if body.protocol_pdf_path:
        protocol_pdf_path = str(_find_session_file(x_session_id, body.protocol_pdf_path))
    previous_rfp_path = ""
    if body.previous_rfp_path:
        previous_rfp_path = str(_find_session_file(x_session_id, body.previous_rfp_path))

    clips_nonpkpd_assignments = None
    raw_assignments = _parse_json_field(body.clips_nonpkpd_assignments)
    if raw_assignments:
        clips_nonpkpd_assignments = [
            {
                "path": str(_find_session_file(x_session_id, a["path"])),
                "column": a.get("column"),
            }
            for a in raw_assignments
            if a.get("column")
        ]

    try:
        report = populate_rfp_main(
            protocol_text=body.protocol_text,
            design_text=body.design_text,
            template_path=str(engine_paths.TEMPLATE_PATH),
            output_path=str(output_path),
            report_path=str(report_path),
            answers=_parse_json_field(body.answers) or {},
            protocol_pdf_path=protocol_pdf_path,
            previous_rfp_path=previous_rfp_path,
            soa_include_indices=None,
            soa_table_override=_parse_json_field(body.soa_table_override),
            lab_table_override=_parse_json_field(body.lab_table_override),
            field_overrides=_flatten_field_overrides(_parse_json_field(body.field_overrides)),
            clips_nonpkpd_assignments=clips_nonpkpd_assignments,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a normal error response,
        # matching rfp.rs's own catch-all "RFP engine call failed" behavior.
        raise HTTPException(500, f"RFP generation failed: {exc}") from exc

    report_markdown = report_path.read_text(encoding="utf-8") if report_path.exists() else ""

    return {
        "status": "complete",
        "file_id": file_id,
        "output_path": f"RFP-{file_id}.docx",
        "report_path": f"{file_id}_report.md",
        "coverage": {
            "filled": report.filled,
            "computed": report.computed,
            "review": report.review_count,
            "total": len(report.findings),
        },
        "report_markdown": report_markdown,
    }


class ClipsNonPkpdPreviewRequest(CamelModel):
    paths: list[str]  # file ids (see /api/upload-multi)


@router.post("/clips-nonpkpd-preview")
async def clips_nonpkpd_preview(body: ClipsNonPkpdPreviewRequest, x_session_id: str = Header(...)):
    """HTTP equivalent of the Tauri desktop app's `preview_clips_nonpkpd_files` command --
    resolves each file id to its real session path and calls clips_nonpkpd_parser.parse_files()
    directly in-process (no subprocess needed at all, same reasoning as every other endpoint in
    this router). Returns `{"files": [...], "unmapped": [...]}` unchanged."""
    real_paths = [str(_find_session_file(x_session_id, p)) for p in body.paths]
    return clips_nonpkpd_parse_files(real_paths)


class FabricDesignFieldsRequest(CamelModel):
    protocol_alias: str


@router.post("/fabric-design-fields")
async def fabric_design_fields(body: FabricDesignFieldsRequest):
    """HTTP equivalent of the Tauri desktop app's `fetch_fabric_design_fields` command --
    calls fabric_extract_lookup.lookup() directly in-process (no subprocess, no auth of any
    kind -- it only ever reads the local daily-extract Excel file). Returns
    `{"status": "ok", "fields": {...}, "extracted_on": ...}` / `{"status": "not_found"}` /
    `{"status": "error", "message": ...}` unchanged."""
    try:
        return fabric_lookup(body.protocol_alias.strip())
    except Exception as exc:  # noqa: BLE001 - same silent-background-fill contract the
        # frontend's maybeAutoSearchFabricDesignFields already expects (a failure here is
        # swallowed client-side, not surfaced as a hard error)
        return {"status": "error", "message": str(exc)}


@router.get("/specimen-columns")
async def specimen_columns():
    """The single source of truth for the CLIPS/Non-PKPD column dropdown -- returns
    every real column in the template's Specimen Management / Referral Lab tables
    (specimen_columns.list_columns(), engine/specimen_columns.py), not a hardcoded
    subset. Each entry's `key` is what the frontend should send back as an
    assignment's `column` value; `display_label` is what to show in the dropdown."""
    return specimen_list_columns(str(engine_paths.TEMPLATE_PATH))
