# CLS RFP Generator -- containerized build.
#
# The build CONTEXT must be the repo root (not this containerized/ directory), since the
# COPY instructions below are relative to backend/, frontend/, and template.docx at the
# repo's top level -- pass this Dockerfile's path explicitly with -f while keeping the
# context as ".":
#
#   docker build -f containerized/Dockerfile -t cls-rfp-generator .
#   docker run -p 8000:8000 cls-rfp-generator
#
# Then open http://localhost:8000

FROM python:3.12-slim

# System dependencies:
#   ghostscript      -- required by camelot-py's Lattice/Stream table extraction
#   libreoffice      -- headless Word (.docx/.doc) -> PDF conversion
#                       (backend/services/office_convert.py)
#   libgl1, libglib2.0-0 -- runtime libs opencv-python-headless (a camelot-py
#                       dependency) needs even in headless mode
#   fonts-liberation -- avoids missing-glyph warnings when LibreOffice renders
#                       a Word document that uses common Office fonts
RUN apt-get update && apt-get install -y --no-install-recommends \
        ghostscript \
        libreoffice \
        libgl1 \
        libglib2.0-0 \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/
COPY template.docx template.docx

EXPOSE 8000

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
