"""Path to the RFP-population engine and the template it fills -- both now live INSIDE this
webapp so the whole app is self-contained in one repo/folder.

Layout: the engine's modules were flattened directly into `webapp\\backend\\` (this file's own
directory) rather than a separate `backend\\engine\\` subfolder, for a more streamlined
structure -- `ENGINE_DIR` is just `_BACKEND_DIR` itself. Those modules use plain top-level
imports internally (`import extractors`, etc., not package-relative ones), so
`ensure_on_path()` still adds this directory to `sys.path` for them to resolve, exactly as
before the flattening -- only where that directory points changed.

`template.docx` lives at `webapp\\template.docx` (a direct sibling of `backend\\`) -- kept
inside `webapp\\` so this folder can stand alone as its own repo with no dependency on
anything outside it.
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent  # webapp/backend/
ENGINE_DIR = _BACKEND_DIR
TEMPLATE_PATH = _BACKEND_DIR.parent / "template.docx"  # webapp/template.docx

if not TEMPLATE_PATH.exists():  # pragma: no cover - defensive fallback for an unexpected layout
    ENGINE_DIR = Path(r"C:\Users\L047081\clinlab-retriever\CLS-Studio\webapp\backend")
    TEMPLATE_PATH = Path(r"C:\Users\L047081\clinlab-retriever\CLS-Studio\webapp\template.docx")


def ensure_on_path() -> None:
    engine_dir_str = str(ENGINE_DIR)
    if engine_dir_str not in sys.path:
        sys.path.insert(0, engine_dir_str)
