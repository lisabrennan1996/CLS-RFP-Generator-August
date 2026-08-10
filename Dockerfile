FROM python:3.11

WORKDIR /app

# System dependencies:
#   ghostscript           -- required by camelot-py's Lattice/Stream table extraction
#   libgl1, libglib2.0-0  -- runtime libs opencv-python-headless (a camelot-py dependency)
#                            needs even in headless mode
RUN apt-get update && apt-get install -y --no-install-recommends \
        ghostscript \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy into the container
COPY backend/ /app/backend/
COPY frontend/ /app/frontend/
COPY template.docx /app/template.docx

# Install backend dependencies (includes python-multipart, needed for the
# POST /api/admin/fabric-extract-upload endpoint's file-upload handling)
RUN pip install -r /app/backend/requirements.txt

# Matches deploy.yaml's FABRIC_EXTRACT_PATH -- the deployed server has no OneDrive
# client, so a desktop scheduled task pushes the Fabric extract file here instead
# (see backend/routers/admin.py); mounted onto a PersistentVolumeClaim in
# deploy.yaml so it survives pod restarts. Overridable at runtime via the env var
# of the same name -- this is just the default for local/non-K8s runs.
ENV FABRIC_EXTRACT_PATH=/data/fabric-extract/fabric_study_extract.xlsx
RUN mkdir -p /data/fabric-extract
VOLUME /data/fabric-extract

# Run uvicorn pointing at your main FastAPI app
# Port 7007 matches deploy.yaml's containerPort/service port -- keep these in sync.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7007", "--proxy-headers"]
