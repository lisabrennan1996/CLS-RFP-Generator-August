"""Path to the RFP-population engine and the template it fills -- both now live INSIDE this
webapp so the whole app is self-contained in one repo/folder.

Layout: the engine lives at `webapp\\backend\\engine\\` (a direct sibling of this file), and
`template.docx` lives at `webapp\\template.docx` (a direct sibling of `backend\\`) -- both kept
inside `webapp\\` specifically so this folder can stand alone as its own repo with no
dependency on anything outside it (the earlier layout had `template.docx` one level further up,
at `CLS-Studio\\template.docx`, a sibling of `webapp\\` rather than inside it).
"""
from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent  # webapp/backend/
ENGINE_DIR = _BACKEND_DIR / "engine"
TEMPLATE_PATH = _BACKEND_DIR.parent / "template.docx"  # webapp/template.docx

if not ENGINE_DIR.exists():  # pragma: no cover - defensive fallback for an unexpected layout
    ENGINE_DIR = Path(r"C:\Users\L047081\clinlab-retriever\CLS-Studio\webapp\backend\engine")
    TEMPLATE_PATH = Path(r"C:\Users\L047081\clinlab-retriever\CLS-Studio\webapp\template.docx")


def ensure_on_path() -> None:
    engine_dir_str = str(ENGINE_DIR)
    if engine_dir_str not in sys.path:
        sys.path.insert(0, engine_dir_str)
