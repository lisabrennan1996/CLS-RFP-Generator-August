#!/usr/bin/env python3
"""Reconstruct the Specimen Management sections (Referral Lab Samples and
both Storage Samples / LTS tables) directly from a previous, completed
Central Lab RFP .docx -- which is built from the same template, so its data
tables are real Word tables (not OCR/positional reconstruction).

Generalized to specimen_columns.py's full 12-column registry (previously
this hardcoded a 6-column subset with no Immunogenicity/Urine/CSF/Tissue/
bmkr support at all) so the "Previous RFP" preview/selection UI can show --
and let the user choose from -- every real column, not just a fixed subset.

`build()`'s return shape is a preview/selection payload, not the
{row_label: {column: value}} shape `fill_spec()` consumes directly --
populate_rfp.py's own orchestration derives that from `rows` for whichever
columns the user actually selected.
"""
from __future__ import annotations

import re

import docx

from docx_table_ops import row_cells

# Boilerplate/placeholder text the template's own unfilled cells show (confirmed
# directly against the template: "NA"-default dropdowns, "Select" dropdowns never
# answered, and free-text placeholders like "List Assay Lab") -- treated as "no
# real data" so a column nobody actually filled in doesn't look populated in the
# preview/selection UI just because its cell isn't literally blank.
_PLACEHOLDER_EXACT = {'na', 'select', '#', '# yr or na', 'x ml or # fta cards'}
_PLACEHOLDER_DEFAULT_RE = re.compile(r'^\d+\s*\(default\)$', re.I)


def _is_placeholder(value: str) -> bool:
    v = value.strip().lower()
    if not v:
        return True
    if v in _PLACEHOLDER_EXACT:
        return True
    if v.startswith('list '):
        return True
    if v.startswith('x ml'):
        return True
    return bool(_PLACEHOLDER_DEFAULT_RE.match(v))


def _match_columns_for_table(header_cells, registry_cols_for_role: list[dict]) -> list[dict]:
    """Matches each registry column entry (from the pristine template) to a real
    column index in THIS previous RFP's own header row, by `base_label` text
    (case-insensitive substring) -- robust to a previous RFP whose columns may
    have shifted position (e.g. from an earlier duplicate-assignment run that
    inserted a real "LTS Serum (2)" column). Duplicate-labeled registry entries
    (the three "Limited use bmkr" slots, which all share the same base_label)
    are consumed against the previous RFP's own duplicate header cells in
    left-to-right order, so each still maps to a distinct real column despite
    the identical label text -- same convention specimen_columns.list_columns()
    already uses when building the registry itself.

    Returns registry column dicts with `col_index` replaced by this table's
    own real position (columns with no match at all are omitted)."""
    used: set[int] = set()
    matched = []
    for reg_col in registry_cols_for_role:
        label = reg_col['base_label'].lower()
        for i, cell in enumerate(header_cells):
            if i == 0 or i in used:
                continue
            if label in cell.text.lower():
                used.add(i)
                matched.append({**reg_col, 'col_index': i})
                break
    return matched


def build(previous_rfp_path: str) -> dict:
    """Returns, for each specimen table role it can locate in the previous RFP:

        {table_role: {"table_idx": int, "columns": [{"key", "table_role",
         "col_index", "base_label", "display_label", "tag", "has_data": bool},
         ...], "rows": {row_label: {col_key: value}}}}

    `has_data` is true when at least one row has a non-empty value for that
    column in THIS previous RFP -- lets the caller default the selection UI's
    checkboxes to what's actually there, without the user having to guess.
    A role is omitted entirely if its table couldn't be located at all.
    Returns `{}` if no path is given or the file can't be opened."""
    if not previous_rfp_path:
        return {}
    try:
        doc = docx.Document(previous_rfp_path)
    except Exception:
        return {}

    from populate_rfp import locate_specimen_tables
    from specimen_columns import list_columns

    roles = locate_specimen_tables(doc)
    registry_by_role: dict[str, list[dict]] = {}
    for col in list_columns():
        registry_by_role.setdefault(col['table_role'], []).append(col)

    result: dict[str, dict] = {}
    for table_role, tbl_idx in roles.items():
        if tbl_idx is None:
            continue
        try:
            table = doc.tables[tbl_idx]
            header_cells = row_cells(table.rows[0])
            matched_cols = _match_columns_for_table(header_cells, registry_by_role.get(table_role, []))
            if not matched_cols:
                continue

            rows: dict[str, dict[str, str]] = {}
            has_data = {c['key']: False for c in matched_cols}
            for r in table.rows[1:]:
                cells = row_cells(r)
                if not cells:
                    continue
                label = cells[0].text.strip()
                if not label:
                    continue
                row_values = {}
                for c in matched_cols:
                    idx = c['col_index']
                    if idx < len(cells):
                        val = cells[idx].text.strip()
                        if val:
                            row_values[c['key']] = val  # raw value -- the user's own choice to select
                            if not _is_placeholder(val):
                                has_data[c['key']] = True
                if row_values:
                    rows[label] = row_values

            result[table_role] = {
                'table_idx': tbl_idx,
                'columns': [{**c, 'has_data': has_data[c['key']]} for c in matched_cols],
                'rows': rows,
            }
        except Exception:
            # One table role's own unexpected shape (a previous RFP whose table structure
            # predates/postdates this template's -- e.g. a genuinely missing row/column
            # elsewhere in the doc that the header-keyword heuristic still located) shouldn't
            # take down the other two roles' previews with it; skip just this one; the
            # generate-time server-side auto-default (see populate_rfp.main()) already
            # tolerates a role being entirely absent here.
            continue
    return result


if __name__ == '__main__':
    import json
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else ''
    print(json.dumps(build(path), indent=2))
