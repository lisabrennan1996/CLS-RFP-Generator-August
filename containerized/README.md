# Containerized build

A `Dockerfile` for running the CLS RFP Generator as a container. This folder holds only the
Dockerfile (and this README) -- the app itself is not duplicated here, it's built directly
from `backend/`, `frontend/`, and `template.docx` at the repo root.

## Build

Run from the **repo root** (not from inside `containerized/`), since the build context needs
to see `backend/`, `frontend/`, and `template.docx`:

```bash
docker build -f containerized/Dockerfile -t cls-rfp-generator .
```

## Run

```bash
docker run -p 8000:8000 cls-rfp-generator
```

Open `http://localhost:8000` in a browser.

## What's inside the image

- Python 3.12 + the packages in `backend/requirements.txt`
- **Ghostscript** -- required by `camelot-py`'s table extraction (Lattice/Stream)
- **LibreOffice** (headless) -- Word (`.docx`/`.doc`) upload conversion
  (`backend/services/office_convert.py`); PDF-only workflows don't need it, but it's included
  so the image works out of the box either way
- A handful of shared libraries (`libgl1`, `libglib2.0-0`) that `opencv-python-headless` (a
  `camelot-py` dependency) needs even in headless mode

## Notes

- Uploaded/converted/generated files are session-scoped and stored under the system temp
  directory inside the container (`backend/sessions.py`) -- they do **not** persist across
  container restarts. That's expected: this app has no database, and generated `.docx` files
  are meant to be downloaded by the user, not stored server-side long-term.
- There's no authentication layer built in (see the main `README.md`'s "Known limitations") --
  put this behind your own auth/reverse-proxy layer before exposing it beyond trusted use.
