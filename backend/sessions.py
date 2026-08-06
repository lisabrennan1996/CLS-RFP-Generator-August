"""Per-browser-tab session isolation for uploaded/converted/generated files.

A browser tab has no native file handles the way a desktop app does, so the server holds
files on disk between requests, keyed by a session id the frontend generates once per page
load (`crypto.randomUUID()`) and sends as the `X-Session-Id` header on every request. All
*extracted data* (page results, table JSON, master-table state) stays exactly where it lives
today -- in the frontend's own `state` object in memory -- this module only manages file
bytes, keeping the backend itself stateless about app logic.

Old session directories are swept periodically since there's no explicit "app closed" signal
from a browser tab the way there is from a desktop process exiting.
"""
from __future__ import annotations

import shutil
import tempfile
import time
import uuid
from pathlib import Path

SESSIONS_ROOT = Path(tempfile.gettempdir()) / "rfp-webapp-sessions"
SESSION_MAX_AGE_SECONDS = 6 * 60 * 60  # 6 hours


def session_dir(session_id: str) -> Path:
    """Return (creating if needed) the temp directory for a session id.

    `session_id` is validated to a safe filename-shape (hex/hyphen only, from
    `crypto.randomUUID()`) so it can never be used to escape SESSIONS_ROOT via a
    path-traversal string.
    """
    safe_id = "".join(c for c in session_id if c.isalnum() or c == "-") or "default"
    d = SESSIONS_ROOT / safe_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_file_id() -> str:
    """A random id for one file within a session (upload, converted PDF, generated docx)."""
    return uuid.uuid4().hex


def sweep_old_sessions(max_age_seconds: int = SESSION_MAX_AGE_SECONDS) -> int:
    """Delete session directories whose most recent file mtime is older than max_age_seconds.

    Returns the number of session directories removed. Safe to call repeatedly (e.g. from a
    background task on an interval, or once at startup) -- never touches a directory that's
    still within its age window.
    """
    if not SESSIONS_ROOT.exists():
        return 0
    now = time.time()
    removed = 0
    for entry in SESSIONS_ROOT.iterdir():
        if not entry.is_dir():
            continue
        try:
            newest = max((p.stat().st_mtime for p in entry.rglob("*") if p.is_file()), default=entry.stat().st_mtime)
        except OSError:
            continue
        if now - newest > max_age_seconds:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed
