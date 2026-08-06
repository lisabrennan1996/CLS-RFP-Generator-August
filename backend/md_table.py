"""Markdown pipe-table extraction for clinical protocol SoA and appendix tables.

Liteparse with output_format='markdown' renders detected tables as standard
pipe-delimited markdown (| ... |). This module finds those tables by document
landmarks, parses them into structured (headers, rows), and provides helpers
for filtering lab rows vs non-lab assessments.
"""
import re

_NON_LAB = re.compile(r'(?i)'
    r'(?:ecg|ekg)(?:\s*\(\d+\s*-?\s*lead\))?|'
    r'electrocardiogram|holter(?:\s*monitor)?|telemetry|'
    r'echocardiogram|cardiac\s*echo|'
    r'vital\s*signs?|blood\s*pressure|'
    r'heart\s*rate|pulse(?:\s*rate|\s*oximetry)?|'
    r'temperature(?!.*(?:lab|serum|plasma|blood|sample|specimen))|'
    r'respirat(?:ory\s*rate|ion)|'
    r'(?:oxygen\s*)?saturation|spo2\b|pulse\s*oximetry|'
    r'orthostatic\s*(?:vital|bp|blood)|'
    r'\bheight\b(?!.*(?:lab|sample|specimen))|'
    r'body\s*weight|body\s*mass|bmi\b(?!.*(?:calc|measur))|'
    r'waist\s*circumference|body\s*surface\s*area|bsa\b|'
    r'body\s*composition|'
    r'physical\s*exam|clinical\s*exam|'
    r'pulmonary\s*function|spirometr|pft\b|lung\s*function|'
    r'neurologic\s*(?:exam|assessment|test|eval)|'
    r'ophthalmolog|visual\s*acuit|fundoscop|slit\s*lamp|'
    r'intraocular\s*pressure|iop\b|visual\s*field|'
    r'audiometr|hearing\s*(?:test|exam|assessment)|'
    r'bone\s*(?:density|scan|densitometry)|dexa\b|dxa\b|'
    r'ecog\b|karnofsky|performance\s*status|kps\b|'
    r'quality\s*of\s*life|qol\b|'
    r'eq[\s-]?5d|eq5d|sf[\s-]?(?:36|12|8)\b|'
    r'pain\s*(?:scale|score|questionnaire|inventory|diary|assess)|'
    r'visual\s*analog\s*(?:scale)?|vas\b(?!cular)|'
    r'likert\b|numeric\s*rating|nrs\b|'
    r'phq[\s-]?[0-9]|gad[\s-]?[0-9]|'
    r'hads\b|'
    r'c[\s-]?ssrs|columbia[\s-]*suicid|'
    r'ham[\s-]?[ad]\b|madrs\b|panss\b|ymrs\b|'
    r'beck\s*depression|bdi\b|'
    r'patient\s*health\s*questionnaire|'
    r'questionnaire|'
    r'patient[\s-]reported(?:.?outcome)?|'
    r'pro\b(?!.*(?:lactin|gester|tein|tease|lifera|phyla|lapse|nounce|secut))|'
    r'(?:patient|subject|symptom)\s*diary|'
    r'adverse\s*event|ae\b|serious\s*adverse|sae\b|'
    r'concomitant\s*med|con\s*med|prior\s*med|concomitant\s*therapy|'
    r'medical\s*history|past\s*medical|demographic|'
    r'smoking|tobacco|alcohol\s*use|substance\s*use|'
    r'x[\s-]?ray|cxr\b|chest\s*(?:xray|radiograph)|'
    r'ct\s*scan|cat\s*scan|computed\s*tomograph|'
    r'mri\b|magnetic\s*resonance|'
    r'ultrasound|sonogram|ultrasonograph|'
    r'pet\s*scan|positron\s*emission|mammogram|mammograph|'
    r'nuclear\s*(?:imaging|scan|medicine)|'
    r'tumor\s*(?:assess|evaluat|measur|response)|'
    r'disease\s*(?:assess|evaluat|measur|response)|'
    r'recist\b|lesion\s*(?:assess|measur|evaluat)|'
    r'target\s*lesion|non[\s-]*target\s*lesion|'
    r'endoscop|colonoscop|sigmoidoscop|bronchoscop|'
    r'cystoscop|laparoscop|arthroscop|hysteroscop|'
    r'egd\b|esophagogastroduodenoscop|'
    r'dosing|dose\s*(?:admin|level)|treatment\s*admin|'
    r'study\s*drug\s*(?:admin|dispens|accountab|prepar)|'
    r'ip\s*(?:admin|dispens|accountab|prepar)|'
    r'investigational\s*product|drug\s*accountability|'
    r'randomiz|random\s*assign|'
    r'informed\s*consent|consent\b|icf\b|'
    r'eligibility|screen\s*fail|inclusion|exclusion|i/e\s*criteria|'
    r'survival\s*(?:follow|status|contact)|'
    r'death\s*(?:confirm|report|assess)|'
    r'end\s*of\s*study|eos\b|early\s*termination|study\s*close[\s-]*out|'
    r'follow[\s-]*up(?!.*(?:sample|specimen|blood|urine|stool|tissue|csf))|'
    r'protocol\s*deviation|'
    r'(?:chest|breast|heart|lung)\s*auscult|'
    r'reflex\b|motor\s*(?:exam|function|strength|assess)|'
    r'dental\s*exam|oral\s*exam|'
    r'eeg\b|electroencephalogram|'
    r'emg\b|electromyogram|nerve\s*conduction|'
    r'cognitive\s*(?:assess|test|exam|eval|function)|'
    r'(?:mmse|moca|mini[\s-]*mental|montreal\s*cognitive)\b|'
    r'(?:exercise|cardiac)\s*stress\s*test|muga\b|'
    r'(?:elective|scheduled|unscheduled|interim)\s*(?:visit|contact|call|phone)|'
    r'safety\s*(?:monitor|report|update|assessment|review|follow[\s-]*up)|'
    r'\s(?:medication|treatment)\s*compliance|adherence\b|pill\s*count'
)

_CATEGORY_HEADER_ENDING = re.compile(
    r'(?i)(?:assessments?|evaluations?|procedures?|'
    r'parameters?|measurements?|examinations?|tests?\b|'
    r'endpoints?|observations?|monitoring|collections?|'
    r'samples?|specimens?|determinations?|training|education|'
    r'activit(?:y|ies))\s*$'
)

# A single visit/timepoint token -- matched one or more of these, separated by whitespace/comma/
# slash, so a compound header naming both a visit AND a sub-visit timepoint (e.g. "Visit 3 Hour 2",
# "Visit 3 Day 1-3") is recognized as a visit column just like a bare "Visit 3" or "Day2" is today.
# Includes a day/week/etc range ("Day 1-3") as its own alternative, rather than relying on the
# separate short-header length fallback below to catch it by coincidence.
_VISIT_TOKEN = (
    r'\d+|[Vv](?:isit)?\.?\s*\d+[a-z]?|'
    r'scr(?:een(?:ing)?)?|'
    r'ed|edu|eos|et|fu|'
    r'(?:day|week|month|cycle|year)s?\s*\d+(?:\s*[-–]\s*\d+)?[a-z]?|'
    r'predose|pre[\s-]*dose|postdose|post[\s-]*dose|'
    r'(?:hour|hr|h)\s*\d+[a-z]?|'
    r'\d{1,2}\s*(?:h|hr|hour)s?|'
    r'(?:trough|peak|random|infusion|follow[\s-]*up|safety[\s-]*fu)'
)
_VISIT_LIKE = re.compile(
    r'^(?:' + _VISIT_TOKEN + r')(?:[\s,/&]+(?:' + _VISIT_TOKEN + r'))*$', re.I
)

_NON_VISIT_HEADERS = frozenset({
    'rt', 'retest', 'comments', 'comment', 'notes', 'note',
    'visit number', 'visit #', 'visit', 'study day', 'day',
    'visit type', 'visit label', 'study week', 'study month',
    'study period', 'period', 'time point', 'timepoint',
    'window', 'allowable window', 'assessment',
})


_HEADING_NUM = re.compile(r'^(#{1,6})?\s*(\d+(?:\.\d+)*)\.\s')

def _is_toc_line(s: str) -> bool:
    return bool(re.search(r'\.{4,}', s))

def find_heading_by_number(text: str, num: int | str) -> tuple[str, int, int] | None:
    """Find heading for a numbered section.

    num can be an int (1, 10) or a subsection string ('1.3', '10.2').
    TOC entries (lines with 4+ consecutive dots) are skipped.

    Returns (heading_line, heading_level, char_offset). heading_level is the
    section depth: for markdown headings it's the '#' count; for plain-text
    numbered headings (the common case in Lilly protocols/design docs) it's
    the number of dot-separated segments (e.g. '1.3' -> 2, '10.2' -> 2,
    '2' -> 1) so that extract_section stops only at a same-or-shallower
    heading, not at every subsection.

    char_offset is the position of the *line* in `text`. Numbered headings
    are duplicated verbatim in the Table of Contents, so callers must pass
    this offset into extract_section rather than re-searching from the start
    of the document (which would just re-find the ToC entry).
    """
    target = str(num)
    lines = text.split('\n')
    offset = 0
    prefix_candidates = []  # (offset, line, level, full_num) for fallback pass
    for line in lines:
        s = line.strip()
        if s and not _is_toc_line(s):
            m = _HEADING_NUM.match(s)
            if m:
                full_prefix = m.group(0).rstrip(' \t.')
                full_num = m.group(2)
                level = len(m.group(1)) if m.group(1) else full_num.count('.') + 1
                if full_prefix == target:
                    return s, level, offset
                prefix_candidates.append((offset, s, level, full_num))
        offset += len(line) + 1
    # Fallback: match on the first number segment (e.g., target='1' matches '1.3')
    for off, s, level, full_num in prefix_candidates:
        if full_num.split('.')[0] == target:
            return s, level, off
    return None


def extract_section(text: str, heading: str, level: int, start: int | None = None) -> str:
    """Extract a section from heading to the next heading at the same or
    shallower depth. TOC entries (lines with 4+ consecutive dots) are skipped.

    `start` should be the char_offset returned by find_heading_by_number.
    Numbered headings are duplicated in the Table of Contents, so without
    `start` a plain text.find(heading) can match the ToC entry instead of
    the real section — always pass `start` when it's available.
    """
    idx = text.find(heading, start) if start is not None else text.find(heading)
    if idx == -1:
        return ''
    after = text[idx + len(heading):]
    lines = after.split('\n')
    end_offset = len(after)
    if re.match(r'^#{1,6}\s', heading.strip()):
        pat = re.compile(r'^#{1,' + str(level) + r'}\s+\d')
    else:
        # Match headings with up to `level` dot-separated segments — i.e.
        # same depth or shallower. Deeper subsections (more segments) don't end the section.
        pat = re.compile(r'^\d+(?:\.\d+){0,' + str(max(level - 1, 0)) + r'}\.\s')
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or _is_toc_line(s):
            continue
        if pat.match(s):
            end_offset = sum(len(l) + 1 for l in lines[:i])
            break
    return text[idx:idx + len(heading) + end_offset]


def find_table_by_header(text: str, header_matcher) -> tuple:
    """Find first pipe table whose first-column header satisfies header_matcher.

    Returns (headers, rows, table_lines) or (None, None, None).
    """
    for table_lines in find_pipe_tables(text):
        headers, rows = parse_pipe_table(table_lines)
        if headers and header_matcher(headers[0].strip()):
            return headers, rows, table_lines
    return None, None, None


def find_section(text: str, start_marker: str, end_marker: str | None = None) -> str | None:
    """Extract text between two landmark markers (case-insensitive)."""
    s = text.lower().find(start_marker.lower())
    if s == -1:
        return None
    text_from = text[s:]
    if end_marker:
        e = text_from.lower().find(end_marker.lower())
        if e == -1:
            return text_from
        return text_from[:e]
    return text_from


def find_pipe_tables(text: str) -> list:
    """Find all contiguous pipe-table blocks in markdown text."""
    lines = text.split('\n')
    tables = []
    current = None
    for line in lines:
        s = line.strip()
        if s and s[0] == '|':
            if current is None:
                current = []
            current.append(s)
        else:
            if current is not None:
                if len(current) >= 2:
                    tables.append(current)
                current = None
    if current is not None and len(current) >= 2:
        tables.append(current)
    return tables


def _split_row(line: str) -> list:
    cells = [c.strip() for c in line.split('|')]
    if cells and cells[0] == '':
        cells = cells[1:]
    if cells and cells[-1] == '':
        cells = cells[:-1]
    return cells


def parse_pipe_table(lines: list) -> tuple:
    """Parse pipe-table lines into (headers, rows).

    Returns:
        headers: list of column header strings
        rows: list of lists — each inner list is [row_label, cell1, cell2, ...]
    """
    if not lines:
        return [], []
    headers = _split_row(lines[0])

    data_start = 1
    if len(lines) > 1:
        second = lines[1].strip()
        if second and all(c in '| :-' for c in second):
            data_start = 2

    rows = []
    for line in lines[data_start:]:
        cells = _split_row(line)
        if cells and any(c for c in cells):
            while len(cells) < len(headers):
                cells.append('')
            rows.append(cells[:len(headers)])
    return headers, rows


def extract_footnotes(text: str, table_lines: list) -> str:
    """Extract footnote text immediately after a pipe table."""
    table_str = '\n'.join(table_lines)
    idx = text.find(table_str)
    if idx == -1:
        return ''
    after = text[idx + len(table_str):].strip()
    footnotes = []
    for line in after.split('\n'):
        s = line.strip()
        if not s:
            if footnotes:
                break
            continue
        if re.match(r'^[\d\*†‡§¶#]\s+', s) or (
            footnotes and s[0].islower()
        ):
            footnotes.append(s)
        elif not footnotes:
            continue
        else:
            break
    return '\n'.join(footnotes)


def is_non_lab(label: str) -> bool:
    """True if the row label is a non-lab assessment."""
    return bool(_NON_LAB.search(label.strip()))


def is_category_header(label: str, cells: list) -> bool:
    """True if the row is a section divider (e.g. 'Laboratory Assessments')."""
    lb = label.strip()
    if not lb:
        return False
    has_content = any(c.strip() for c in cells)
    if has_content:
        return False
    # Strip a trailing parenthetical (e.g. "Clinician-administered assessments
    # (paper)") before checking the category-noun ending.
    lb_core = re.sub(r'\s*\([^)]*\)\s*$', '', lb).strip()
    # A real divider is a descriptive phrase ("Laboratory tests and sample
    # collections", "Randomization and dosing-related activities") — a bare
    # single word ending in a category noun ("samples", "tests" alone) is
    # indistinguishable from a genuine procedure-name fragment continuing a
    # wrapped label from the row above (confirmed directly: a lone "samples"
    # row completing "Exploratory biomarker" was misclassified as a
    # divider). Require at least two words before this check can fire.
    if ' ' in lb_core and _CATEGORY_HEADER_ENDING.search(lb_core):
        return True
    # Multi-word requirement excludes bare acronyms (IWRS, ECG, MRI...) that
    # are otherwise indistinguishable from a genuine all-caps divider
    # ("LABORATORY ASSESSMENTS") by case and length alone — confirmed
    # directly: a lone all-caps "IWRS" row (a wrapped label fragment, not a
    # divider) was being misclassified as a section header.
    if lb.isupper() and ' ' in lb and len(lb) >= 4 and len(lb) <= 50 and not _NON_LAB.search(lb):
        return True
    return False


def is_visit_column(header: str) -> bool:
    """True if a column header represents a study visit (not Comments/RT/etc.)."""
    h = header.strip().lower()
    if not h:
        return True
    if h in _NON_VISIT_HEADERS:
        return False
    if _VISIT_LIKE.match(header.strip()):
        return True
    if len(h) <= 8:
        return True
    if re.match(r'^\d{1,2}\s*(?:h|hr|hour)s?$', h):
        return True
    return False


# ── Text-based table parsers (for liteparse markdown without pipe tables) ──

_VISIT_NUMBER_RE = re.compile(r'^\s*Visit\s+Number', re.I)
_X_MARK = re.compile(r'\bX\b')


def parse_soa_text(section_text: str) -> tuple:
    """Parse a visual whitespace-aligned SoA table from plain text.

    The SoA repeats across pages with the same header structure.
    Returns (headers, rows, footnotes) matching the pipe-table format.
    """
    lines = section_text.split('\n')

    # Find all "Visit Number" lines — they define column positions
    visit_def_lines = []
    for i, line in enumerate(lines):
        if _VISIT_NUMBER_RE.match(line):
            visit_def_lines.append(i)

    if not visit_def_lines:
        return [], [], ''

    # Use the first Visit Number line to define visit columns
    vn_idx = visit_def_lines[0]
    vn_line = lines[vn_idx]

    # Parse visit labels from the Visit Number line
    # Find positions of each non-empty token after "Visit Number"
    visit_labels = []
    visit_positions = []
    tokens = vn_line.split()
    if tokens[:2] == ['Visit', 'Number']:
        rest = vn_line[vn_line.index('Number') + len('Number'):]
        pos = vn_line.index('Number') + len('Number')
        for tok in tokens[2:]:
            idx = rest.find(tok)
            if idx >= 0:
                visit_labels.append(tok)
                visit_positions.append(pos + idx)
                rest = rest[idx + len(tok):]
                pos += idx + len(tok)

    if not visit_labels:
        return [], [], ''

    headers = ['Procedures'] + visit_labels + ['Comments']

    # Collect data rows between consecutive Visit Number lines
    data_lines = []
    for pi in range(len(visit_def_lines)):
        start = visit_def_lines[pi] + 1
        end = visit_def_lines[pi + 1] if pi + 1 < len(visit_def_lines) else len(lines)
        for i in range(start, end):
            s = lines[i].strip()
            if not s:
                continue
            # Skip known header rows
            if _VISIT_NUMBER_RE.match(lines[i]):
                continue
            if re.match(r'^\s*Period\s', lines[i]):
                continue
            if re.match(r'^\s*Weeks?\s+from\s+Randomiz', lines[i], re.I):
                continue
            if re.match(r'^\s*Visit\s+Interval', lines[i], re.I):
                continue
            if re.match(r'^\s*Visit\s+Detail', lines[i], re.I):
                continue
            if re.match(r'^\s*(?:Initial|Screening|Washout|Treatment|Period)', lines[i]):
                continue
            if 'Author and Content Review' in lines[i]:
                continue
            if 'CONFIDENTIAL' in lines[i]:
                continue
            if re.match(r'^\s*\d+\s*$', s):
                continue  # page numbers
            data_lines.append(lines[i])

    # Parse each data row
    rows = []
    seen_labels = set()
    for line in data_lines:
        # Extract label (text before first visit position)
        first_pos = visit_positions[0] if visit_positions else len(line)
        label = line[:first_pos].strip()
        if not label:
            continue
        if label in seen_labels:
            continue
        seen_labels.add(label)

        # Extract X marks for each visit position
        cells = []
        for vp in visit_positions:
            # Check character at visit position
            if vp < len(line):
                token = line[vp:vp + 3].strip()
                cells.append('X' if token == 'X' else ('\u2713' if '\u2713' in token else ''))
            else:
                cells.append('')

        # Extract comments (text after last visit position)
        if visit_positions:
            last_vp = visit_positions[-1] + len(visit_labels[-1])
            comments = line[last_vp:].strip()
        else:
            comments = ''

        row = [label] + cells + ([comments] if comments else [])
        rows.append(row)

    return headers, rows, ''


def parse_analytes_text(section_text: str) -> list:
    """Parse a 2-column visual text table (Clinical Laboratory Tests | Comments).

    Returns list of (test_name, comment) tuples.
    """
    lines = section_text.split('\n')
    in_table = False
    results = []

    for line in lines:
        s = line.rstrip()
        # Detect the table header
        if re.match(r'^\s*Clinical Laboratory Tests', s, re.I) and 'Comments' in s:
            in_table = True
            continue
        if not in_table:
            continue
        # Stop at subsection heading or blank line after content
        if re.match(r'^\d{1,2}\.\d{1,2}\.\s', s):
            break
        if re.match(r'^10\.\s', s):
            break
        if not s.strip():
            continue
        # Skip page headers
        if re.match(r'^\s*Author and Content Review', s) or 'CONFIDENTIAL' in s:
            continue
        if re.match(r'^\s*\d+\s*$', s.strip()):
            continue

        # Split on 3+ spaces to separate test name from comment
        parts = re.split(r' {3,}', s, maxsplit=1)
        test_name = parts[0].strip()
        comment = parts[1].strip() if len(parts) > 1 else ''
        if test_name:
            results.append((test_name, comment))

    return results
