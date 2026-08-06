# CLS RFP Generator Webapp

The updated CLS RFP Generator — a self-contained web app (browser frontend + FastAPI backend)
for native-PDF table extraction and Central Lab RFP `.docx` generation. 

## Architecture

```
webapp/
  backend/
    main.py                FastAPI app: mounts frontend/ as static files, includes routers
    engine_paths.py         Points at backend/ itself (see below) and ../template.docx
    sessions.py             Per-browser-tab file storage (see "Sessions" below)
    schemas.py              accepts the same JSON keys app.js's
                            existing invoke() call sites send
    populate_rfp.py, extractors.py, clips_nonpkpd_parser.py, specimen_columns.py,
    docx_table_ops.py, fabric_*.py, ...
                            The RFP-population engine (proprietary business logic) --
                            protocol/design extraction, the CLIPS/Non-PKPD column-mapping
                            parser, and the specimen-column registry. Flattened directly
                            into backend/ (not a separate engine/ subfolder) for a more
                            streamlined layout; these modules use plain top-level imports
                            internally, resolved via engine_paths.ensure_on_path().
    routers/
      documents.py           upload, upload-multi, rasterize (PDF only -- see "Known
                              limitations" below)
      tables.py               extract-tables (Camelot), master-schedule (clinical_mapper)
      rfp.py                  extract-schema, generate-rfp, clips-nonpkpd-preview,
                              fabric-design-fields, specimen-columns
      export.py               generic download endpoint
    services/
      pdf.py                  pypdfium2-based page rasterization
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
- **PDF only for the main protocol document / "Design Elements" attach.** Word (.docx/.doc)
  document conversion (previously via LibreOffice) is not supported on this deployment's
  servers -- upload a PDF instead. ("Previous RFP" attachment is unaffected: it reads a
  `.docx` directly via `python-docx`, and was never converted through LibreOffice at all.)
- **Multi-user**: this is a real shared server -- concurrent sessions are isolated by the
  mechanism above, but there's no authentication layer yet. Add one before exposing this
  beyond trusted local/internal use.

## Setup

Python 3.12+ with the packages in `backend/requirements.txt`:
```bash
pip install -r backend/requirements.txt
```

## Run locally

Double-click `run.bat` -- it installs dependencies, starts the server, and opens
`http://127.0.0.1:8000` in your browser.

Or manually, from this directory (`webapp/`, so `backend` resolves as a top-level package):

```bash
uvicorn backend.main:app --reload
```
