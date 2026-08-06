#!/usr/bin/env python3
"""Structured table extraction for Schedule of Activities and the Clinical
Laboratory Tests appendix, using edgeparse's bounding-box-aware PDF table
detection instead of an LLM.

No network calls, no LLM. Page ranges are found dynamically by scanning the
PDF's own text for the section headings (skipping Table-of-Contents entries),
never hardcoded — the same content-based approach already used elsewhere in
this codebase for section-finding.

Known limitation (proven against a real protocol, not theoretical): edgeparse's
table detector depends on the PDF page having visible grid borders. Sections of
the Lab Appendix without visible borders come back as flat prose with the
test-name/comment association lost. Rather than guess at reconstructing those,
`extract_lab_appendix_table` surfaces them as a single visible review row per
unparsed stretch (see extractors.REVIEW) — never silently dropped, never
invented.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Optional

import edgeparse
import pypdfium2

from extractors import REVIEW
from md_table import find_pipe_tables, parse_pipe_table, is_visit_column, is_category_header

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Dynamic, content-based page-range detection (mirrors the proven
# ToC-skipping approach in plugin/scripts/rfp_pipeline.py, reimplemented
# locally so this module has no dependency on plugin/scripts/).
# ═══════════════════════════════════════════════════════════════════

_SOA_START_PAT = re.compile(r'1\.3\.?\s+Schedule\s+of\s+Activities', re.IGNORECASE)
_SOA_END_PAT = re.compile(r'^\s*(?:1\.4\.|2\.\s)', re.IGNORECASE | re.MULTILINE)

_APPX_START_PAT = re.compile(r'10\.2\.?\s+Appendix\s+\d+:?\s*Clinical\s+Lab', re.IGNORECASE)
_APPX_END_PAT = re.compile(r'^\s*10\.3\.?\s', re.IGNORECASE | re.MULTILINE)

# Numbered headings are duplicated verbatim in two other places besides the
# real section: the Table of Contents (dot-leader lines) and the amendment /
# summary-of-changes table (e.g. "10.2. Appendix 2: Clinical Laboratory
# Tests" followed by "Added 'Magnesium' to the Clinical Chemistry panel").
# ToC entries are filtered by the dot-leader check. Amendment entries don't
# have dot leaders, so instead of requiring one specific phrase from a real
# section's body (fragile — different protocols word their SoA intro
# differently, confirmed on a second real document), reject candidates that
# read like a change-log entry — checking for what a real section *isn't*
# generalizes far better than requiring one fixed phrasing of what it *is*.
_AMENDMENT_PAT = re.compile(
    r'(?:Revised|Added|Modified|Updated|Clarified)\s+(?:to\s+)?(?:the\s+)?'
    r'(?:requir|language|text|criterion|protocol|section|instruction|panel)',
    re.IGNORECASE,
)


def _find_page_range(pdf_path: str, start_pat: re.Pattern, end_pat: re.Pattern) -> Optional[tuple[int, int]]:
    """Return (start_idx, end_idx), 0-indexed inclusive, or None if not found."""
    doc = pypdfium2.PdfDocument(pdf_path)
    n = len(doc)
    start_idx = None
    end_idx = None
    try:
        page_texts = []
        for i in range(n):
            page = doc[i]
            tp = page.get_textpage()
            page_texts.append(tp.get_text_bounded())
            tp.close()
            page.close()

        for i in range(n):
            text = page_texts[i]
            is_toc = '........' in text
            if start_idx is None:
                if start_pat.search(text) and not is_toc:
                    window = text + (page_texts[i + 1] if i + 1 < n else '')
                    if not _AMENDMENT_PAT.search(window):
                        start_idx = i
                continue
            if i > start_idx and end_pat.search(text) and not is_toc:
                end_idx = i - 1
                break
    finally:
        doc.close()
    if start_idx is None:
        return None
    if end_idx is None or end_idx < start_idx:
        end_idx = n - 1
    return start_idx, end_idx


def _page_range_str(rng: tuple[int, int]) -> str:
    start_idx, end_idx = rng
    return f'{start_idx + 1}-{end_idx + 1}'


def _edgeparse_convert(pdf_path: str, **kwargs) -> str:
    """edgeparse.convert(), falling back to a pypdfium2-resaved copy of the
    PDF on structural load failures. Confirmed against a real protocol PDF
    with a non-standard cross-reference table: pypdfium2 opens it fine
    (already required for page-range detection above), but edgeparse's own
    PDF parser rejects it outright. Re-saving through pypdfium2 first
    normalizes the file structure and edgeparse then reads it cleanly."""
    try:
        return edgeparse.convert(pdf_path, **kwargs)
    except RuntimeError as e:
        if 'PDF loading error' not in str(e):
            raise
        import tempfile
        fixed_path = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False).name
        doc = pypdfium2.PdfDocument(pdf_path)
        doc.save(fixed_path)
        doc.close()
        return edgeparse.convert(fixed_path, **kwargs)


# ═══════════════════════════════════════════════════════════════════
# Schedule of Activities
# ═══════════════════════════════════════════════════════════════════

def _split_row(line: str) -> list[str]:
    cells = [c.strip() for c in line.split('|')]
    if cells and cells[0] == '':
        cells = cells[1:]
    if cells and cells[-1] == '':
        cells = cells[:-1]
    return cells


def _is_separator_row(cells: list[str]) -> bool:
    return all(re.match(r'^[-:\s]*$', c) for c in cells)


def _is_continuation_label(label: str) -> bool:
    """A wrapped-label fragment (e.g. 'randomization' continuing 'Weeks from')
    rather than a genuine new procedure with no marks at any visit."""
    return bool(label) and (label[0].islower() or len(label) < 20)


_PAGE_DECORATION = re.compile(
    r'^(?:Table\s+\d+|[A-Z0-9]{2,4}-[A-Z]{2}-[A-Z0-9]{3,6})'
)

# A repeated page/column-group header row (e.g. the protocol nickname
# "GZQD" paired with cells "Period I", "Study Period II Dose Escalation
# Treatment", "Comments" — the table's *own* column-group caption, recurring
# verbatim atop every page) — recognized by its cells being built entirely
# from a small set of generic period/column words, never a real procedure
# name or an X/blank visit mark.
_DECORATION_WORDS = (
    'period', 'treatment', 'comments', 'screening', 'washout',
    'maintenance', 'lead-in', 'dose escalation', 'study',
)


def _looks_like_repeated_header_row(label: str, data_cells: list[str]) -> bool:
    if not label or not re.match(r'^[A-Za-z0-9]{2,8}$', label):
        return False
    nonblank = [c.strip() for c in data_cells if c.strip()]
    if not nonblank:
        return False
    def _is_decor_word(c: str) -> bool:
        core = re.sub(r'[/\s]+$', '', c).strip().lower()
        return any(core == w or core.startswith(w) for w in _DECORATION_WORDS)
    return all(_is_decor_word(c) for c in nonblank)


def _is_page_decoration(label: str, data_cells: list[str] = ()) -> bool:
    """Repeated page-header artifacts (e.g. 'Table 1 ...', the protocol
    number/nickname paired with the table's own period-column caption) that
    appear as spurious rows at page boundaries — never a real procedure."""
    stripped = label.strip()
    if _PAGE_DECORATION.match(stripped):
        return True
    return _looks_like_repeated_header_row(stripped, data_cells)


# Some protocols label their column-index row "Visit number"; others use
# "Study day", "Day", "Assessment Day", etc. Rather than enumerate every
# wording, also detect the row structurally: a row whose non-blank cells are
# mostly day-numbers/tolerance-windows/visit-type abbreviations, regardless
# of what its own label says.
_DAY_HEADER_LABELS = {'visit number', 'study day', 'visit', 'day',
                      'assessment day', 'cycle day'}
_DAY_TOKEN = re.compile(r'^-?\d+(\s*to\s*-?\d+)?$|^\(?[±·±]\d+\)?$|^(SCR|ET|UV|ED|EOI|EOS)$', re.IGNORECASE)


def _is_day_header_label(label: str) -> bool:
    return label.strip().lower() in _DAY_HEADER_LABELS


def _looks_like_day_header_structurally(label: str, data_cells: list[str]) -> bool:
    """Structural fallback for protocols using wording outside
    `_DAY_HEADER_LABELS` — deliberately bootstrap-only (see call site): a
    sequential-looking numeric row can also legitimately be a *continuation*
    of an already-active table's own header (confirmed on a real protocol —
    "Weeks from randomization" is ~93% bare integers), so this must never
    fire once a table is already open, or it mistakes a continuation row
    for a brand new one."""
    if not label.strip():
        return False
    nonblank = [c.strip() for c in data_cells if c.strip()]
    if len(nonblank) < 3:
        return False
    return sum(bool(_DAY_TOKEN.match(c)) for c in nonblank) / len(nonblank) > 0.5


# Some SoAs wrap a day-range across two physical rows: an unlabeled fragment
# row (e.g. "-35 to") directly above the labeled day-header row, column-
# aligned with its continuation ("-2"). Detected and merged by
# _merge_day_header_fragment before the header row is used.
def _merge_day_header_fragment(cells: list[str], pending_row: Optional[tuple]) -> list[str]:
    if not pending_row or pending_row[0]:
        return cells
    prev_cells = pending_row[1]
    if not any(c.strip() for c in prev_cells[1:]):
        return cells
    merged = list(cells)
    width = max(len(merged), len(prev_cells))
    for i in range(1, width):
        prev_val = prev_cells[i].strip() if i < len(prev_cells) else ''
        if not prev_val:
            continue
        cur_val = merged[i].strip() if i < len(merged) else ''
        combined = f'{prev_val} {cur_val}'.strip() if cur_val else prev_val
        if i >= len(merged):
            merged.extend([''] * (i - len(merged) + 1))
        merged[i] = combined
    return merged


# A "Part"/"Arm"/"Wave"/"Cohort" title row (e.g. "Parts A1 and C", "Part A2",
# "Part B – Cohort 3") — protocols with parallel treatment groups repeat
# day numbers across groups (both Part A1 and Part A2 have a "Day 1"), so
# each group's visit columns are namespaced by its title to avoid
# conflating unrelated arms' schedules just because they share a day number.
_GROUP_TITLE_PAT = re.compile(r'^(Parts?|Arms?|Waves?|Groups?|Cohorts?)\b', re.IGNORECASE)


def _rejoin_fragmented_label(cells: list[str]) -> list[str]:
    """A long category-header/title label can get split across multiple pipe
    cells by edgeparse's column-boundary detection on the same row (e.g.
    "Laboratory tests and sa" | "mple co" | "llect" | "ions" — confirmed on a
    real protocol, distinct from the row-wrapped label case
    `_is_continuation_label` already handles). Rejoin leading fragments —
    non-blank, short, not an "X" mark or day-token — into cells[0], blanking
    the consumed cells so positional indexing of the real data columns isn't
    thrown off."""
    if len(cells) < 2:
        return cells
    label = cells[0].strip()
    consumed = 0
    # A genuine mid-word split spans a handful of narrow cells and the whole
    # reconstructed label stays short (real example: "Laboratory tests and
    # sample collections", 40 chars). Comment/footnote overflow sitting in
    # later columns (e.g. "Washout/PDEP: The time interval of...") is made of
    # full words/sentences and runs much longer — cap both the per-fragment
    # length and the cumulative total so that text isn't mistaken for a
    # split label.
    for c in cells[1:]:
        if consumed >= 4:
            break
        c_strip = c.strip()
        if not c_strip or c_strip.upper() == 'X' or _DAY_TOKEN.match(c_strip) or len(c_strip) > 12:
            break
        if len(label) + len(c_strip) > 45:
            break
        label += c_strip
        consumed += 1
    if consumed == 0:
        return cells
    return [label] + [''] * consumed + cells[1 + consumed:]


def _is_group_title_row(label: str, data_cells: list[str]) -> bool:
    # The title row often shares space with trailing period-name fragments
    # (e.g. "Parts A1 and C | SCR | | Treatm | ent Per..."), so — unlike a
    # category-divider row — it can't require blank data cells. The
    # Part/Arm/Wave/Cohort label prefix is distinctive enough on its own;
    # no real procedure name starts with one of these words.
    return bool(label) and bool(_GROUP_TITLE_PAT.match(label.strip()))



def extract_soa_table(pdf_path: str) -> tuple[list[str], list[list[str]]]:
    """Extract the full Schedule of Activities as (headers, rows).

    Handles protocols where the SoA is split across multiple physically
    separate tables with different visit-column sets (e.g. a screening/dose-
    escalation table and a separate maintenance/follow-up table) by unioning
    all distinct visit columns into one table and merging rows by procedure
    label — a procedure assessed in both tables lands on one row with marks
    in the correct columns from each source table.

    No filtering, no whitelist, no model call: every row from every
    physical table in section 1.3 is returned as-is. Deciding which rows
    are actually relevant to the central lab is left entirely to the
    interactive crop tool in the UI (rfp-ui) — simpler and more reliable
    than any automatic lab-vs-not classification attempted here previously
    (structural dividers, a CDISC whitelist, a vision-model rescue tier —
    all removed; each had real gaps or was unreliable in practice).

    Confirmed directly on 3 real protocols (OIAH, OIAF, GZPR): edgeparse's
    table detector returns nothing usable when a PDF's SoA pages lack
    visible grid borders — neither 'default' nor 'cluster' table_method
    helps (tested identically empty on all three). No fallback for that
    case currently; it returns ([], []).
    """
    rng = _find_page_range(pdf_path, _SOA_START_PAT, _SOA_END_PAT)
    if rng is None:
        return [], []

    md = _edgeparse_convert(pdf_path, format='markdown', pages=_page_range_str(rng), table_method='default')
    return _parse_soa_from_markdown(md)


def _parse_soa_from_markdown(md: str) -> tuple[list[str], list[list[str]]]:
    """Parse an already-rendered SoA markdown blob into (headers, rows).
    Shared by the edgeparse path and the vision-rescue path (which
    synthesizes an equivalent markdown blob from model-reconstructed page
    tables) — every day-header/divider/signal rule below applies to both,
    with no duplicated logic."""
    pipe_lines = [line for line in md.split('\n') if line.strip().startswith('|')]

    # A running page/column-group header repeats on every page of a
    # paginated table (confirmed on a real protocol: the same "<nickname> |
    # Period I | ... | Comments" caption recurs ~15 times, occasionally with
    # one incidental extra blank cell on some pages) — a genuine procedure
    # row essentially never does, since its marks/comment differ per page.
    # Keying on non-blank cell content (not the raw line) makes this
    # invariant to that incidental blank-cell drift. This catches the whole
    # artifact class — including cases where mid-word cell-splitting defeats
    # word-level decoration matching — without needing to recognize its
    # wording at all. Checked below only after day-header/group-title/
    # divider recognition has had first chance at a repeating row, so
    # genuinely-repeating *structural* rows (e.g. a day-header re-shown per
    # page) are still classified correctly.
    def _row_signature(l: str) -> tuple:
        return tuple(c for c in _split_row(l) if c.strip())

    _line_counts = Counter(_row_signature(l) for l in pipe_lines)

    # tables: (namespaced) visit_labels tuple -> {'end_idx': int, 'rows': [...]}
    tables: dict[tuple, dict] = {}
    table_order: list[tuple] = []
    current_key: Optional[tuple] = None
    current_group_title: Optional[str] = None
    pending_row: Optional[tuple] = None  # (label, cells) of the previous non-blank row

    n_lines = len(pipe_lines)
    idx = 0
    while idx < n_lines:
        line = pipe_lines[idx]
        cells = _split_row(line)
        if not cells or _is_separator_row(cells):
            pending_row = None
            idx += 1
            continue

        # Checked on the *raw*, pre-rejoin cells: a repeated page/column-
        # group header (e.g. "GZQD | Period I | ... | Comments") reads as a
        # short bare label with generic-word data cells, but
        # _rejoin_fragmented_label would otherwise eagerly absorb the first
        # such cell into the label (e.g. "GZQDPeriod I"), destroying the
        # very shape this check looks for.
        if _is_page_decoration(cells[0].strip(), cells[1:]):
            pending_row = None
            idx += 1
            continue

        cells = _rejoin_fragmented_label(cells)
        label = cells[0].strip()
        data_cells = cells[1:]

        if _is_group_title_row(label, data_cells):
            current_group_title = label
            pending_row = None
            idx += 1
            continue

        is_trigger = _is_day_header_label(label) or (
            current_key is None and _looks_like_day_header_structurally(label, data_cells))
        if is_trigger:
            merged_cells = _merge_day_header_fragment(cells, pending_row)
            nonblank_idx = [i for i, c in enumerate(merged_cells) if c.strip()]
            pending_row = None
            if not nonblank_idx:
                idx += 1
                continue
            visit_end = max(nonblank_idx)
            raw_labels = tuple(merged_cells[i].strip() for i in range(1, visit_end + 1))
            if not raw_labels:
                idx += 1
                continue
            visit_labels = (tuple(f'{current_group_title}: {v}' for v in raw_labels)
                            if current_group_title else raw_labels)
            if visit_labels not in tables:
                tables[visit_labels] = {'end_idx': visit_end, 'rows': []}
                table_order.append(visit_labels)
            else:
                tables[visit_labels]['end_idx'] = max(tables[visit_labels]['end_idx'], visit_end)
            current_key = visit_labels
            idx += 1
            continue

        # Section-divider row (e.g. "Laboratory tests and sample
        # collections", "Vital Signs Assessments") — never a data row itself,
        # just skipped so it doesn't get treated as a procedure. A divider's
        # label can itself wrap across *several* physical rows
        # (confirmed on a real protocol: "Randomization and" / "dosing
        # -related" / "activities" — three fragments), so greedily consume a
        # run of blank-celled continuation-looking rows before deciding —
        # otherwise a partial combination (e.g. just the first two
        # fragments) fails the category-ending check and the lead fragment
        # falls through to get swallowed by an unrelated *previous* data
        # row's continuation-merge below.
        if current_key is not None and not any(c.strip() for c in data_cells) and label:
            combined_label = label
            consumed = 0
            j = idx + 1
            while consumed < 3 and j < n_lines:
                nxt_cells = _split_row(pipe_lines[j])
                if not nxt_cells or _is_separator_row(nxt_cells):
                    break
                nxt_label = nxt_cells[0].strip()
                nxt_data = nxt_cells[1:]
                if (not nxt_label or any(c.strip() for c in nxt_data)
                        or not _is_continuation_label(nxt_label)):
                    break
                combined_label = f'{combined_label} {nxt_label}'.strip()
                consumed += 1
                j += 1
            if is_category_header(combined_label, data_cells):
                pending_row = None
                idx += consumed + 1
                continue

        if (current_key is None or not label or _is_page_decoration(label, data_cells)
                or _line_counts[_row_signature(line)] >= 3):
            pending_row = (label, cells)
            idx += 1
            continue

        meta = tables[current_key]
        end_idx = meta['end_idx']
        padded = cells + [''] * max(0, end_idx + 1 - len(cells))
        visits_vals = [padded[i].strip() for i in range(1, end_idx + 1)]
        comment_cells = cells[end_idx + 1:] if len(cells) > end_idx + 1 else []
        comment = ' '.join(c.strip() for c in comment_cells if c.strip())

        # Pass 1: merge wrapped-label continuation fragments only. Whitelist/
        # citation checks happen in a second pass below, once labels
        # (including multi-line continuations) are whole — checking
        # per-fragment misclassifies both halves.
        rows = meta['rows']
        if rows and all(not v for v in visits_vals) and _is_continuation_label(label):
            rows[-1]['label'] = (rows[-1]['label'] + ' ' + label).strip()
            if comment:
                rows[-1]['comment'] = (rows[-1]['comment'] + ' ' + comment).strip()
            pending_row = (label, cells)
            idx += 1
            continue

        rows.append({'label': label, 'visits': dict(zip(current_key, visits_vals)),
                     'comment': comment})
        pending_row = (label, cells)
        idx += 1

    if not table_order:
        return [], []

    # No inclusion filtering: every row from every table in section 1.3
    # survives, regardless of divider wording or content. Deciding what's
    # actually lab-relevant is left to the interactive crop tool in the UI
    # instead of any automatic classification here (structural dividers, a
    # CDISC whitelist, and a vision-model rescue tier were all tried and
    # removed — simpler and more reliable to show everything and let the
    # user remove what they don't want). Column-structure rows (day/visit
    # header rows, Part/Cohort titles, category dividers) never reach this
    # point at all — they're consumed in Pass 1 above, before any row is
    # added to meta['rows'].

    # Union all visit columns across tables, in first-seen order.
    all_visits: list[str] = []
    seen_visits = set()
    for key in table_order:
        for v in key:
            if v not in seen_visits:
                seen_visits.add(v)
                all_visits.append(v)

    # Merge rows by procedure label across tables (same procedure assessed in
    # more than one table lands on one row).
    merged: dict[str, dict] = {}
    merge_order: list[str] = []
    for key in table_order:
        for row in tables[key]['rows']:
            label = row['label']
            if label not in merged:
                merged[label] = {'visits': {}, 'comments': []}
                merge_order.append(label)
            for v, val in row['visits'].items():
                if val and not merged[label]['visits'].get(v):
                    merged[label]['visits'][v] = val
            if row['comment'] and row['comment'] not in merged[label]['comments']:
                merged[label]['comments'].append(row['comment'])

    headers = ['Procedures'] + all_visits + ['Comments']
    rows_out = []
    for label in merge_order:
        entry = merged[label]
        row_vals = [label] + [entry['visits'].get(v, '') for v in all_visits] + [' '.join(entry['comments'])]
        rows_out.append(row_vals)

    return headers, rows_out


# ═══════════════════════════════════════════════════════════════════
# Clinical Laboratory Tests appendix
# ═══════════════════════════════════════════════════════════════════

_NOISE_LINE = re.compile(
    r'^(?:#\s*)?(?:CONFIDENTIAL|Approved\s+on|---\s*PAGE\s+\d+|Page\s+\d+\s+of\s+\d+)',
    re.IGNORECASE,
)
_APPENDIX_HEADER_ROW = re.compile(r'^clinical\s+laboratory\s+tests$', re.IGNORECASE)


def extract_lab_appendix_table(pdf_path: str) -> tuple[list[str], list[list[str]]]:
    """Extract the Clinical Laboratory Tests appendix as (headers, rows).

    Table sections with visible grid borders come back as real
    (test name, comment) rows. Sections edgeparse can't structure (no visible
    borders on that PDF page) are surfaced as a single visible review row per
    unparsed stretch — never guessed, never silently dropped.
    """
    rng = _find_page_range(pdf_path, _APPX_START_PAT, _APPX_END_PAT)
    if rng is None:
        return [], []

    md = _edgeparse_convert(pdf_path, format='markdown', pages=_page_range_str(rng), table_method='cluster')

    rows_out: list[list[str]] = []
    prose_buf: list[str] = []

    def _flush_prose():
        text = ' '.join(prose_buf).strip()
        prose_buf.clear()
        if len(text) < 60:
            return
        rows_out.append([REVIEW('Lab Appendix section', 'could not parse table structure'), text])

    for line in md.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('|'):
            cells = _split_row(stripped)
            if not cells or _is_separator_row(cells):
                continue
            label = cells[0].strip()
            if not label or _APPENDIX_HEADER_ROW.match(label):
                continue
            _flush_prose()
            comment = ' '.join(c.strip() for c in cells[1:] if c.strip())
            rows_out.append([label, comment])
        else:
            if _NOISE_LINE.match(stripped):
                continue
            prose_buf.append(re.sub(r'^#+\s*', '', stripped))

    _flush_prose()
    return ['Clinical Laboratory Tests', 'Comments'], rows_out
