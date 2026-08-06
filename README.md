# CLS RFP Generator Webapp

The updated CLS RFP Generator — a self-contained web app (browser frontend + FastAPI backend)
for native-PDF table extraction and Central Lab RFP `.docx` generation. 

## Architecture

```
webapp/
  backend/
    main.py                FastAPI app: mounts frontend/ as static files, includes routers
    engine_paths.py         Points at backend/engine/ and ../template.docx
    sessions.py             Per-browser-tab file storage (see "Sessions" below)
    schemas.py              accepts the same JSON keys app.js's
                            existing invoke() call sites send
    engine/                 The RFP-population engine (business logic) --
                            protocol/design extraction, populate_rfp.py, the CLIPS/Non-PKPD
                            column-mapping parser, and the specimen-column registry
    routers/
      documents.py           upload, upload-multi, convert-to-pdf (Word->PDF), rasterize
      tables.py               extract-tables, master-schedule (clinical_mapper)
      rfp.py                  extract-schema, generate-rfp, clips-nonpkpd-preview,
                              fabric-design-fields, specimen-columns
      export.py               generic download endpoint
    services/
      pdf.py                  pypdfium2-based page rasterization
      office_convert.py       LibreOffice-headless Word->PDF conversion
      tables_extract.py       python + pdfplumber extraction
      clinical_mapper.py      Schedule-of-Activities -> Master Schedule mapping
  frontend/
    index.html, styles.css, app.js, blocks-view.js, master-table.js, master-schedule.js
    -- vanilla HTML/CSS/JS, no build step, no framework
  run.bat                   Installs backend deps, starts uvicorn, opens the browser
```

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
