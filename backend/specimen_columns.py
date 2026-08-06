"""Canonical registry of the template's real Specimen Management / Referral Lab
table columns -- the single source of truth for what a CLIPS/Non-PKPD file can be
assigned to, used by both clips_nonpkpd_parser.py (extraction/writing) and the
webapp's GET /api/specimen-columns endpoint (the frontend dropdown).

Exists because the dropdown used to hardcode a fixed 6-name list
(CLIPS_NONPKPD_COLUMNS / _COLUMN_TO_DOC_TYPE) that had silently drifted from the
template's actual 12 real data columns -- `Limited use bmkr` (×3, all identically
labeled), `LTS Urine`, `LTS CSF`, and `LTS Tissue, etc.` were all unreachable.
Reading the template directly here means the dropdown and the extraction/writing
logic can never drift apart again.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

# This file lives at webapp/backend/specimen_columns.py; template.docx lives at
# webapp/template.docx (a sibling of backend/, so the whole app is self-contained in one
# folder) -- parents[1] from here is webapp/.
_DEFAULT_TEMPLATE_PATH = Path(__file__).resolve().parents[1] / 'template.docx'


def _clean_header(text: str) -> str:
    """Strips the template's footnote-marker asterisk (e.g. "LTS PK *" -> "LTS PK")
    and collapses whitespace/newlines so header text compares and displays cleanly."""
    return ' '.join(text.replace('*', '').split())


def list_columns(template_path: Optional[str] = None) -> list[dict]:
    """Returns, in template left-to-right order across all three tables:

        [{"key": "referral:1", "table_role": "referral", "col_index": 1,
          "base_label": "LTS PK", "display_label": "LTS PK (referral)",
          "tag": "referral"}, ...]

    `key` is the stable identifier a caller (the frontend dropdown, an
    assignment's "column" field) should actually store/send -- unlike
    `base_label`, it is unique even for the three identically-labeled
    "Limited use bmkr" columns (each gets its own "referral:N" key; only
    `display_label` gets a "(1)"/"(2)"/"(3)" suffix to tell them apart visually).

    `table_role` is one of "referral" | "storage_wide" | "storage_narrow" --
    matches populate_rfp.locate_specimen_tables()'s own keys, so a caller can go
    straight from an entry to the real docx table without re-deriving which one
    it is. `col_index` is 0-indexed, the column's position within that table's
    row-0 cells (column 0, the row-label column, is never a real data column and
    is always excluded here).
    """
    import docx
    from populate_rfp import locate_specimen_tables
    from docx_table_ops import row_cells

    doc = docx.Document(str(template_path or _DEFAULT_TEMPLATE_PATH))
    roles = locate_specimen_tables(doc)

    out: list[dict] = []
    for table_role, tag in (
        ('referral', 'referral'),
        ('storage_wide', 'lts'),
        ('storage_narrow', 'lts'),
    ):
        tbl_idx = roles.get(table_role)
        if tbl_idx is None:
            continue
        # row_cells(), not the header row's own `.cells` -- correctly counts a row even
        # if some of its cells are wrapped in a Word content control (dropdown); the
        # header row in these tables happens to use plain cells today, but staying
        # consistent with fill_spec_by_index()'s own accessor avoids silent drift.
        header_cells = row_cells(doc.tables[tbl_idx].rows[0])
        raw_labels = [_clean_header(c.text) for c in header_cells[1:]]

        totals: dict[str, int] = {}
        for lbl in raw_labels:
            totals[lbl] = totals.get(lbl, 0) + 1

        seen_counts: dict[str, int] = {}
        tag_suffix = 'referral' if tag == 'referral' else 'LTS'
        for offset, lbl in enumerate(raw_labels):
            col_index = offset + 1  # +1 to skip the row-label column (index 0)
            if totals[lbl] > 1:
                seen_counts[lbl] = seen_counts.get(lbl, 0) + 1
                display = f'{lbl} ({seen_counts[lbl]}) ({tag_suffix})'
            else:
                display = f'{lbl} ({tag_suffix})'
            out.append({
                'key': f'{table_role}:{col_index}',
                'table_role': table_role,
                'col_index': col_index,
                'base_label': lbl,
                'display_label': display,
                'tag': tag,
            })
    return out
