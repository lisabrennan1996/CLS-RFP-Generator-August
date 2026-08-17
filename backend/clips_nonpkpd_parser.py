"""Alternative to reverse-parsing a previous RFP's own Referral/Storage tables
(`build_specimen.py`): reads the actual source documents instead —

  CLIPS forms ("Clinical Trial Specimen Collection, Processing and Shipping
  Instructions for Bioanalytical Samples") and
  Non-PK Data Management Services Worksheets (immunogenicity/biomarker assay
  setup, incl. referral lab contact info)

— and builds a `[{table_role, col_index, base_label, header_override, row_data}]`
write list (see build_from_assignments()) that populate_rfp.py writes directly into
the real Referral Lab (LTS PK / LTS Immunogenicity / Limited use bmkr x3) and
Storage Samples (LTS DNA/Serum/Plasma/Urine/CSF/RNA/Tissue) template columns --
every real column specimen_columns.py's registry knows about, addressed by
position rather than by a fixed name, so identically-labeled columns (the three
"Limited use bmkr" cells) and duplicate-assignment column insertion both work.

Column assignment for the *auto-detect preview* (column_for(), used by
parse_files()) is still rule-based off fields the forms themselves state
(confirmed directly against two real examples, one of each type) — never a guess,
and unchanged by the write-list rework above:
  - Any CLIPS file -> always "LTS PK" (the form's own title says "for
    Bioanalytical Samples" -- CLIPS forms only ever describe PK/bioanalytical
    handling).
  - Any Non-PK worksheet -> its own Assay Type field first ("ADA (Anti-drug
    antibody)" or a Test name containing "NAb"/"ADA") -> "LTS Immunogenicity";
    otherwise its own Specimen Type field (DNA/Serum/Plasma/RNA) -> the matching
    column. Anything else -> unmapped (surfaced to the caller, never silently
    guessed -- auto-detect still can't guess Urine/CSF/Tissue/bmkr from a form's
    contents; a user can still manually assign any of those via the dropdown,
    which drives real extraction via doc_type_override regardless of what
    auto-detect itself guessed).

Both document types extract cleanly as label/value table rows via pdfplumber
(already a dependency -- see oncology_biopsy_extractor.py), confirmed directly
against real files:
  CLIPS:     ['', 'Assay Type', '', 'ELISA']
  Non-PKPD:  ['Assay Type', 'ADA (Anti-drug antibody)', 'Assay Subtype', 'UFAT ACE']
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Optional

# The template's own Referral Lab Samples / Storage Samples attribute rows this
# parser actually writes into. Written in every table (Referral Lab AND Storage
# Samples):
ROW_ANALYTE_NAME = 'Analyte name'
ROW_SAMPLE_TYPE = 'Sample type'
ROW_POST_LPV_STORAGE = 'Post-LPV storage'
ROW_POST_LPV_ALIQUOTS = '# post-LPV storage aliquots'

# Written in the Referral Lab table ONLY, for BOTH CLIPS and Non-PKPD files --
# confirmed directly against the template that in the Storage Samples (LTS)
# tables these same-named rows are pre-filled "NA"-default dropdown/content-
# control cells and are intentionally left alone there, but in the Referral Lab
# table they're genuine unfilled placeholders ("Select", "List Assay Lab", "List
# special tube, or indicate N/A", not "NA") that this parser's own extracted
# fields actually answer.
ROW_VALIDATED_ASSAY = 'Validated assay'
ROW_LILLY_PROPRIETARY_ASSAY = 'Lilly proprietary assay'
ROW_ASSAY_LAB = 'Assay lab'
ROW_ASSAY_LAB_LOCATION = 'Assay lab city, state, country'
ROW_SPECIAL_TUBE_REQUIRED = 'Special collection tube required?'
ROW_SPECIAL_TUBE = 'Special collection tube'
ROW_REF_LAB_CONTRACT_OWNER = 'Ref Lab contract owner'
ROW_RESULTS_PER_SAMPLE = '# of results per expected sample'

# Written in every table, for both CLIPS and Non-PKPD files.
# ("Special processing requirements" is in both mapping spreadsheets too, but is
# explicitly ignored for now -- the field label(s) it should combine weren't
# confirmed against a real file, and the user asked to skip it.)
ROW_SAMPLE_VOL = 'Sample vol / collection'

# CLIPS-only hardcode -- the Non-PKPD mapping gives no instruction for this row
# at all (stays untouched for Non-PKPD files, same as any other unmapped row).
ROW_ALIQUOTS_PER_SAMPLE = '# aliquots/expected sample'

# Fully merged across every column in a table (one shared cell for the whole
# row, not per-column) -- written ONCE per table via a separate mechanism
# (build_from_assignments()'s `shared_writes`), not through the normal
# per-column `row_data` dict fill_spec_by_index() consumes. Hardcoded to
# "frozen" whenever a table receives at least one CLIPS/Non-PKPD assignment at
# all, regardless of which column(s).
SHARED_ROW_CONDITION_LABELS = (
    'Ship site to central lab',
    'Ship central lab to referral lab for testing',
    'Ship central lab to central lab biorepository for storage',
)
SHARED_ROW_CONDITION_VALUE = 'frozen'

# The narrow Storage Samples table (LTS RNA/Tissue) spells the middle condition row
# "Ship central lab to ref lab for testing" in the real template -- "ref lab", not
# "referral lab" like the other two specimen tables -- confirmed directly against
# template.docx. fill_shared_row() matches via the row's own label startswith() the
# given prefix, so sending the referral/storage_wide spelling for this table silently
# writes nothing. Only this one label differs, and only for this one table role.
_SHARED_ROW_LABEL_OVERRIDES = {
    'storage_narrow': {
        'Ship central lab to referral lab for testing': 'Ship central lab to ref lab for testing',
    },
}

IMMUNOGENICITY_ASSAY_RE = re.compile(r'\bADA\b|\bNAb\b|anti[- ]?drug antibody|neutralizing antibody', re.I)
SPECIMEN_COLUMN_MAP = {
    'dna': 'LTS DNA',
    'serum': 'LTS Serum',
    'plasma': 'LTS Plasma',
    'rna': 'LTS RNA',
}


def _extract_rows(path: str) -> list[list[Optional[str]]]:
    import pdfplumber

    rows: list[list[Optional[str]]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                rows.extend(table)
    return rows


def _cell_first_line(cell: Optional[str]) -> str:
    return (cell or '').split('\n', 1)[0].strip()


def _find_generic(rows: list[list[Optional[str]]], is_label) -> Optional[str]:
    """Shared label->value lookup, parameterized on a label-matching predicate (exact
    string for _find, regex for _find_re). Confirmed against two real documents that
    three distinct row shapes all occur and must each resolve to the right value:

      1. Non-PKPD's combined "label\\nvalue" cell (one cell holds both, e.g.
         "Special Training Required?\\nfalse") -- the value is the *same* cell's
         second line, not a neighboring cell.
      2. Non-PKPD's paired-columns row (label, value, label2, value2, ...), e.g.
         ['Assay Type', 'ADA (Anti-drug antibody)', 'Assay Subtype', 'UFAT ACE'] --
         the value is the very next cell after the matched label.
      3. CLIPS's blank-padded row, sometimes with the label cell duplicated (confirmed:
         ['Analyte(s) for Measurement', 'Analyte(s) for Measurement', None, 'LY3074828'])
         -- the value is the first later cell that ISN'T itself another copy of the
         label, which also correctly skips past shape 2's own label2/value2 pair when
         hunting for label1's value.
    """
    for row in rows:
        for i, cell in enumerate(row):
            if cell is None or not is_label(_cell_first_line(cell)):
                continue
            # Shape 1: label and value share one cell, split by the first newline. Only
            # the line right after the label is the value -- confirmed against a real
            # CLIPS cell where a multi-paragraph free-text block ("Please note: ...",
            # collection instructions, etc.) followed the actual Yes/No answer in the
            # very same cell; taking everything after the label would have returned
            # that whole paragraph instead of just "No".
            rest = cell.split('\n', 1)
            if len(rest) == 2 and rest[1].strip():
                return rest[1].strip().split('\n', 1)[0].strip()
            # Shapes 2/3: scan forward for the first later non-empty cell that isn't
            # itself another match for this same label (handles the duplicated-label
            # cell in shape 3 without misreading shape 2's next label as a value).
            for other in row[i + 1:]:
                if other and other.strip() and not is_label(_cell_first_line(other)):
                    return other.strip()
    return None


def _find_re(rows: list[list[Optional[str]]], pattern: re.Pattern) -> Optional[str]:
    """Like _find, but matches the label with a regex instead of an exact string --
    needed for the storage-duration labels, whose °/– characters were confirmed to come
    through pdfplumber as mangled replacement characters in a real Wuxi/ICON-exported
    PDF (fonts vary by originating lab, so an exact string match is too fragile here)."""
    return _find_generic(rows, lambda text: bool(pattern.search(text)))


def _find(rows: list[list[Optional[str]]], label: str) -> Optional[str]:
    """Generic label->value lookup across every extracted row -- see _find_generic for
    the three row shapes this handles."""
    label_lower = label.lower()
    return _find_generic(rows, lambda text: text.lower() == label_lower)


def detect_doc_type(rows: list[list[Optional[str]]]) -> Optional[str]:
    joined = ' '.join(_cell_first_line(c) for row in rows[:6] for c in row if c).lower()
    if 'non-pk data mgmt' in joined or 'non-pk data management' in joined:
        return 'non_pkpd'
    if 'specimen collection, processing and shipping' in joined or 'clinical processing instructions' in joined:
        return 'clips'
    # Fall back to a labeled field only CLIPS forms have vs. only Non-PKPD forms have,
    # in case the title text above doesn't appear on a page pdfplumber tabled cleanly.
    if _find(rows, 'Bioanalytical Lab Name') is not None:
        return 'clips'
    if _find(rows, 'Reference Laboratory Name') is not None:
        return 'non_pkpd'
    return None


_LONG_TERM_20_RE = re.compile(r'Long.?term at.{1,3}20.{1,3}C \(days\)', re.I)
_LONG_TERM_70_RE = re.compile(r'Long.?term at.{1,3}70.{1,3}C \(days\)', re.I)


def _storage_duration_text(rows: list[list[Optional[str]]], doc_type: str) -> Optional[str]:
    parts = []
    if doc_type == 'clips':
        for pattern, unit_suffix in (
            (_LONG_TERM_20_RE, 'days at -20°C'),
            (_LONG_TERM_70_RE, 'days at -70°C'),
        ):
            v = _find_re(rows, pattern)
            if v:
                parts.append(f'{v} {unit_suffix}')
    else:
        for label in ('AMB', 'REFR', 'FRZ -10 or colder', 'FRZ -60 or colder', 'Other FRZ'):
            v = _find(rows, label)
            if v:
                parts.append(f'{label}: {v}')
    return '; '.join(parts) if parts else None


def _yes_no(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    v = raw.strip().lower()
    if v in ('yes', 'true', 'y'):
        return 'Yes'
    if v in ('no', 'false', 'n'):
        return 'No'
    return raw.strip()


def parse_one(path: str, doc_type_override: Optional[str] = None) -> dict:
    """`doc_type_override` lets the caller nudge which label set (CLIPS vs. Non-PKPD)
    to search when detect_doc_type() itself can't classify a file (returns None) --
    used by build_from_assignments() so a user's manually-confirmed column assignment
    can still drive real extraction for a file auto-detection couldn't tell apart.

    Precedence is `detected_doc_type or doc_type_override` -- the file's own
    successfully-detected type ALWAYS wins over the column-derived override; the
    override is used ONLY when detection genuinely failed. This matters because a
    user can assign an uploaded file to ANY Referral Lab/Storage Samples column
    regardless of what the file actually is (per direct confirmation: "the chosen
    column is autopopulated regardless" of which column it is) -- if the override
    won unconditionally instead, a genuine CLIPS PDF assigned to e.g. "LTS Serum"
    would be extracted with the Non-PKPD label set (forced by that column), finding
    nothing, since a CLIPS PDF has no "Reference Laboratory Name" etc. field to find.
    (Historically this function's docstring described the override as always
    winning -- that was itself the "I picked LTS PK but nothing filled" bug's root
    cause for undetected files, but overshot by also overriding *successful*
    detections, which is the case fixed here.)"""
    rows = _extract_rows(path)
    detected_doc_type = detect_doc_type(rows)
    effective_doc_type = detected_doc_type or doc_type_override
    name = Path(path).name

    if effective_doc_type == 'clips':
        analyte = _find(rows, 'Analyte(s) for Measurement')
        specimen_type = _find(rows, 'Specimen Type')
        assay_type = _find(rows, 'Assay Type')
        validation_complete = _find(rows, 'Validation Complete')
        lilly_proprietary_assay = None  # No source on a CLIPS form -- left untouched.
        lab_name = _find(rows, 'Bioanalytical Lab Name')
        address_line1 = _find(rows, 'Address Line 1')
        lab_city = _find(rows, 'City, State  Zip Code') or _find(rows, 'City, State Zip Code')
        assay_lab_location = f'{address_line1}, {lab_city}' if address_line1 and lab_city else (address_line1 or lab_city)
        contact_name = _find(rows, 'Contact Name')
        special_training = _find(rows, 'Is special training required for personnel processing the samples?')
        tube_type = _find(rows, 'Type of tube')
        tube_size = _find(rows, 'Tube size')
        # Per the CLIPS-to-RFP mapping spreadsheet: both "Analyte name" and "Sample
        # type" are populated from Specimen Type, combined with the anticoagulant/
        # tube type when the form specifies one -- not the literal analyte/compound
        # name, and identical for both rows ("Sample type" = "Same as Analyte Name").
        analyte_name_value = f'{specimen_type} - {tube_type}' if specimen_type and tube_type else specimen_type
        sample_type_value = analyte_name_value
    else:
        analyte = _find(rows, 'Test name')
        specimen_type = _find(rows, 'Specimen Type')
        assay_type = _find(rows, 'Assay Type')
        validation_complete = _find(rows, 'Validation Complete')
        lilly_proprietary_assay = _find(rows, 'Lilly-developed or Lilly-assay transfer')
        lab_name = _find(rows, 'Reference Laboratory Name')
        assay_lab_location = _find(rows, 'Address')
        contact_name = _find(rows, 'Laboratory Contact')
        special_training = _find(rows, 'Special Training Required?')
        tube_type = _find(rows, 'Specimen Subtype')
        tube_size = None  # No equivalent field on a Non-PKPD worksheet.
        # Per the Non-PKPD-to-RFP mapping spreadsheet: unlike CLIPS, "Analyte name"
        # and "Sample type" have DIFFERENT sources here -- Analyte name is the
        # worksheet's own Test Name, verbatim; Sample type combines Specimen Type
        # and Specimen Subtype.
        analyte_name_value = analyte
        sample_type_value = f'{specimen_type} - {tube_type}' if specimen_type and tube_type else specimen_type

    fields = {
        'analyte': analyte,
        'analyte_name_value': analyte_name_value,
        'sample_type_value': sample_type_value,
        'specimen_type': specimen_type,
        'assay_type': assay_type,
        'validated_assay': _yes_no(validation_complete),
        'lilly_proprietary_assay': lilly_proprietary_assay,
        'assay_lab': lab_name,
        'assay_lab_location': assay_lab_location,
        'contact_name': contact_name,
        'special_tube_required': _yes_no(special_training),
        'special_tube': tube_type,
        'tube_size': tube_size,
        'storage_duration': _storage_duration_text(rows, effective_doc_type) if effective_doc_type else None,
    }

    column = column_for(detected_doc_type, fields)
    return {
        'path': path,
        'name': name,
        'doc_type': detected_doc_type,
        'effective_doc_type': effective_doc_type,
        'column': column,
        'fields': fields,
    }


def column_for(doc_type: Optional[str], fields: dict) -> Optional[str]:
    if doc_type == 'clips':
        return 'LTS PK'
    if doc_type != 'non_pkpd':
        return None
    assay_type = fields.get('assay_type') or ''
    analyte = fields.get('analyte') or ''
    if IMMUNOGENICITY_ASSAY_RE.search(assay_type) or IMMUNOGENICITY_ASSAY_RE.search(analyte):
        return 'LTS Immunogenicity'
    specimen_type = (fields.get('specimen_type') or '').strip().lower()
    return SPECIMEN_COLUMN_MAP.get(specimen_type)


def parse_files(paths: list[str]) -> dict:
    files = [parse_one(p) for p in paths]
    unmapped = [f['name'] for f in files if not f['column']]
    return {'files': files, 'unmapped': unmapped}


def _doc_type_for_column(base_label: str) -> str:
    """Generalizes the old fixed 6-entry _COLUMN_TO_DOC_TYPE dict to every real
    template column: "LTS PK" is the only column any CLIPS-shaped form ever maps to
    (see module docstring), so it's the only one that needs the CLIPS label set;
    every other column -- including the 6 that were previously unreachable through
    the dropdown (Urine/CSF/Tissue, the three "Limited use bmkr" slots) -- uses the
    Non-PKPD label set, exactly like Immunogenicity/DNA/Serum/Plasma/RNA already did."""
    return 'clips' if base_label == 'LTS PK' else 'non_pkpd'


def _row_data(fields: dict, table_role: str, effective_doc_type: Optional[str]) -> dict[str, str]:
    """Builds the {row_label: value} write set for one file's extracted fields, per
    the user's own CLIPS-to-RFP and Non-PKPD-to-RFP mapping spreadsheets -- both
    apply the same row set (the two spreadsheets are near-identical in structure,
    differing mainly in which field on each form supplies each row's value, see
    parse_one()'s own per-branch field extraction).

    Post-LPV storage / # post-LPV storage aliquots are hardcoded ("7" years, "all
    aliquots") in every table regardless of doc type -- unchanged from before either
    mapping existed.

    Every table, either doc type: Analyte name / Sample type (`analyte_name_value`/
    `sample_type_value` -- identical values for CLIPS, genuinely different sources
    for Non-PKPD, see parse_one()), Sample vol / collection (`tube_size` -- only
    CLIPS forms have this field, so it's a no-op for Non-PKPD files).

    Every table, CLIPS files ONLY: hardcoded # aliquots/expected sample ("1") --
    the Non-PKPD mapping gives no instruction for this row at all, so it stays
    untouched for Non-PKPD files exactly like any other unmapped row.

    Referral Lab table ONLY, either doc type (Storage Samples' same-named rows are
    pre-filled "NA"-default cells intentionally left untouched, per direct
    confirmation): Validated assay, Lilly proprietary assay (`lilly_proprietary_
    assay` -- only Non-PKPD forms have a source for this; stays untouched for
    CLIPS), Ref Lab contract owner (`contact_name`), Assay lab, Assay lab city/
    state/country, hardcoded Special collection tube required? ("Yes"), Special
    collection tube (`special_tube`), hardcoded # of results per expected sample
    ("1")."""
    row_data: dict[str, str] = {
        ROW_POST_LPV_STORAGE: '7',
        ROW_POST_LPV_ALIQUOTS: 'all aliquots',
    }

    if fields.get('analyte_name_value'):
        # The narrow Storage Samples table (LTS RNA/Tissue) spells this row's label
        # just "Analyte" in the real template, not "Analyte name" like the other two
        # specimen tables -- confirmed directly against template.docx. Using
        # ROW_ANALYTE_NAME there would never match (fill_spec_by_index() matches via
        # the row's own label startswith() this key, and "Analyte" doesn't start with
        # "Analyte name"), silently dropping this row for that table only.
        analyte_row_label = 'Analyte' if table_role == 'storage_narrow' else ROW_ANALYTE_NAME
        row_data[analyte_row_label] = fields['analyte_name_value']
    if fields.get('sample_type_value'):
        row_data[ROW_SAMPLE_TYPE] = fields['sample_type_value']
    if fields.get('tube_size'):
        row_data[ROW_SAMPLE_VOL] = fields['tube_size']
    # Special processing requirements: ignored for now, per direct instruction.
    if effective_doc_type == 'clips':
        row_data[ROW_ALIQUOTS_PER_SAMPLE] = '1'

    if table_role == 'referral':
        if fields.get('validated_assay'):
            row_data[ROW_VALIDATED_ASSAY] = fields['validated_assay']
        if fields.get('lilly_proprietary_assay'):
            row_data[ROW_LILLY_PROPRIETARY_ASSAY] = fields['lilly_proprietary_assay']
        if fields.get('contact_name'):
            row_data[ROW_REF_LAB_CONTRACT_OWNER] = fields['contact_name']
        if fields.get('assay_lab'):
            row_data[ROW_ASSAY_LAB] = fields['assay_lab']
        if fields.get('assay_lab_location'):
            row_data[ROW_ASSAY_LAB_LOCATION] = fields['assay_lab_location']
        row_data[ROW_SPECIAL_TUBE_REQUIRED] = 'Yes'
        if fields.get('special_tube'):
            row_data[ROW_SPECIAL_TUBE] = fields['special_tube']
        row_data[ROW_RESULTS_PER_SAMPLE] = '1'

    return row_data


def build_from_assignments(assignments: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """`assignments`: [{"path", "column"}, ...] where "column" is a
    specimen_columns.list_columns() registry `key` (e.g. "storage_wide:2"), NOT a
    bare column name -- unlike a name, the key is unique even for the three
    identically-labeled "Limited use bmkr" columns, and is what the frontend
    dropdown now sends (see specimen_columns.py's own docstring). Columns already
    confirmed by the caller (the UI's review list) are NOT re-derived here, so a
    manual correction the user made to an auto-detected column is respected rather
    than silently overridden. The confirmed column also drives *which label set*
    parse_one() searches with (see its own doc_type_override param) -- not just
    which table column the result lands in -- so a file detect_doc_type() couldn't
    classify still gets a real shot at extraction instead of always defaulting to
    (and likely failing) the Non-PKPD label set.

    Returns (writes, insertions, shared_writes):
      writes: [{"table_role", "col_index", "base_label", "header_override",
                "row_data": {row_label: value}}, ...] -- header_override is only
        set on the FIRST of two-or-more files sharing the same column key (renames
        the pre-existing column to "<base_label> (1)"); None otherwise (a solo
        assignment keeps the template's original header untouched, unchanged
        behavior from before this function supported duplicates at all).
      insertions: [{"table_role", "after_col_index", "header_text"}, ...] -- one
        entry per NEW column that must be inserted (docx_table_ops.insert_column_after)
        before ANY write in `writes` is applied, in the given order (later entries'
        after_col_index already account for earlier insertions in the same table,
        so they must be applied in this exact order, not reordered).
      shared_writes: [{"table_role", "row_label", "value"}, ...] -- one entry per
        SHARED_ROW_CONDITION_LABELS row, for each table that received at least
        one real assignment. Written ONCE per table (not per column/file) via a
        different mechanism (populate_rfp.fill_shared_row()) than `writes`,
        since these rows are fully merged across every column already.

    When two or more files are assigned the same column key, each gets a genuine,
    separate Word table column ("<base_label> (1)", "(2)", ...) instead of
    overwriting one another -- the first reuses the existing column, every
    subsequent one is a brand-new column inserted immediately after it. Two files
    assigned to two *different* pre-existing columns (e.g. one of the three
    distinct "Limited use bmkr" slots each) never trigger insertion at all -- each
    already has its own real column, so grouping is by column key, not base_label.
    """
    from specimen_columns import list_columns

    registry = list_columns()
    by_key = {c['key']: c for c in registry}

    groups: dict[str, list[tuple[dict, Optional[str]]]] = {}
    for a in assignments:
        key = a.get('column')
        if not key or key not in by_key:
            continue
        entry = by_key[key]
        parsed = parse_one(a['path'], doc_type_override=_doc_type_for_column(entry['base_label']))
        groups.setdefault(key, []).append((parsed['fields'], parsed['effective_doc_type']))

    keys_by_table: dict[str, list[str]] = {}
    for key, entry in by_key.items():
        keys_by_table.setdefault(entry['table_role'], []).append(key)

    writes: list[dict] = []
    insertions: list[dict] = []

    for table_role, keys in keys_by_table.items():
        keys_sorted = sorted(keys, key=lambda k: by_key[k]['col_index'])
        shift = 0
        for key in keys_sorted:
            items = groups.get(key)
            if not items:
                continue
            entry = by_key[key]
            base_col_index = entry['col_index'] + shift

            if len(items) == 1:
                fields, effective_doc_type = items[0]
                writes.append({
                    'table_role': table_role,
                    'col_index': base_col_index,
                    'base_label': entry['base_label'],
                    'header_override': None,
                    'row_data': _row_data(fields, table_role, effective_doc_type),
                })
                continue

            for i, (fields, effective_doc_type) in enumerate(items, start=1):
                label = f"{entry['base_label']} ({i})"
                if i == 1:
                    col_index = base_col_index
                    header_override = label
                else:
                    insertions.append({
                        'table_role': table_role,
                        'after_col_index': base_col_index + i - 2,
                        'header_text': label,
                    })
                    col_index = base_col_index + i - 1
                    shift += 1
                    header_override = None  # insertion above already set this header
                writes.append({
                    'table_role': table_role,
                    'col_index': col_index,
                    'base_label': entry['base_label'],
                    'header_override': header_override,
                    'row_data': _row_data(fields, table_role, effective_doc_type),
                })

    # Shared, once-per-table hardcodes for the fully-merged Condition rows (see
    # SHARED_ROW_CONDITION_LABELS' own docstring) -- one set per table that
    # actually received at least one real assignment (a `groups` entry with
    # items), regardless of which column(s)/how many files.
    shared_writes: list[dict] = []
    tables_with_data = {
        entry['table_role']
        for key, entry in by_key.items()
        if groups.get(key)
    }
    for table_role in tables_with_data:
        overrides = _SHARED_ROW_LABEL_OVERRIDES.get(table_role, {})
        for row_label in SHARED_ROW_CONDITION_LABELS:
            shared_writes.append({
                'table_role': table_role,
                'row_label': overrides.get(row_label, row_label),
                'value': SHARED_ROW_CONDITION_VALUE,
            })

    return writes, insertions, shared_writes


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Preview CLIPS/Non-PKPD worksheet column assignments.")
    parser.add_argument('--file', dest='files', action='append', default=[], required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(parse_files(args.files)))
    except Exception as e:  # noqa: BLE001 -- single JSON-line contract for the Rust caller
        print(json.dumps({'status': 'error', 'message': str(e)}))
