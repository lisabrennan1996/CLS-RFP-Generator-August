# CLS RFP Generator Webapp

The updated CLS RFP Generator — a self-contained web app (browser frontend + FastAPI backend)
for native-PDF table extraction (Camelot) and Central Lab RFP `.docx` generation. Originally
ported from a Tauri desktop app; that desktop app and its Rust/Tauri dependency no longer exist
in this repo — this webapp now carries all of its own frontend and backend dependencies.

## Architecture

```
webapp/
  backend/
    main.py                FastAPI app: mounts frontend/ as static files, includes routers
    engine_paths.py         Points at backend/engine/ and ../template.docx
    sessions.py             Per-browser-tab file storage (see "Sessions" below)
    schemas.py              CamelModel -- accepts the same camelCase JSON keys app.js's
                            existing invoke() call sites send
    engine/                 The RFP-population engine (proprietary business logic) --
                            protocol/design extraction, populate_rfp.py, the CLIPS/Non-PKPD
                            column-mapping parser, and the specimen-column registry
    routers/
      documents.py           upload, upload-multi, convert-to-pdf (Word->PDF), rasterize
      tables.py               extract-tables (Camelot), master-schedule (clinical_mapper)
      rfp.py                  extract-schema, generate-rfp, clips-nonpkpd-preview,
                              fabric-design-fields, specimen-columns
      export.py               generic download endpoint
    services/
      pdf.py                  pypdfium2-based page rasterization
      office_convert.py       LibreOffice-headless Word->PDF conversion
      tables_extract.py       Camelot + pdfplumber extraction
      clinical_mapper.py      Schedule-of-Activities -> Master Schedule mapping
  frontend/
    index.html, styles.css, app.js, blocks-view.js, master-table.js, master-schedule.js
    -- vanilla HTML/CSS/JS, no build step, no framework
  run.bat                   Installs backend deps, starts uvicorn, opens the browser
```

## Sessions

A browser tab has no native file handles, so uploaded/converted/generated files live in a
per-tab temp directory on the server, keyed by a `crypto.randomUUID()` the frontend generates
once per page load and sends as the `X-Session-Id` header on every request. All *extracted
data* (page results, table JSON, the master-table grid's own state) lives in the frontend's
own in-memory `state` object -- this is purely about where file *bytes* live between requests.
Session directories older than 6 hours are swept automatically (see `backend/sessions.py`).

## Known limitations

- **No "paste a local file path" field.** A browser can't read an arbitrary local path outside
  a user-driven picker/drop gesture -- use Browse or drag-and-drop instead.
- **No native "Save As" dialog.** Generated/exported files download via the browser's normal
  download mechanism.
- **Word (.docx/.doc) upload requires LibreOffice installed on the machine running the
  backend** (see Setup below). PDF-only workflows don't need it at all.
- **Multi-user**: this is a real shared server -- concurrent sessions are isolated by the
  mechanism above, but there's no authentication layer yet. Add one before exposing this
  beyond trusted local/internal use.

## Setup

1. Python 3.12+ with the packages in `backend/requirements.txt`:
   ```bash
   pip install -r backend/requirements.txt
   ```
2. **LibreOffice** (system install, for Word-document upload only):
   - Windows: install LibreOffice normally; confirm `soffice.exe` ends up on PATH (or add its
     install directory, typically `C:\Program Files\LibreOffice\program`, to PATH).
   - Not required if you only ever work with PDFs.

## Run locally

Double-click `run.bat` -- it installs dependencies, starts the server, and opens
`http://127.0.0.1:8000` in your browser.

Or manually, from this directory (`webapp/`, so `backend` resolves as a top-level package):

```bash
uvicorn backend.main:app --reload
```
