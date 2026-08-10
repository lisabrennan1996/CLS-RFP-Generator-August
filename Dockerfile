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

# Install backend dependencies
RUN pip install -r /app/backend/requirements.txt

# Run uvicorn pointing at your main FastAPI app
# Port 7007 matches deploy.yaml's containerPort/service port -- keep these in sync.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "7007", "--proxy-headers"]
