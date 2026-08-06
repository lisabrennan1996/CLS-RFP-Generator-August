#!/usr/bin/env python3
"""Clinical Design Elements Parser — section-aware extraction from the structured
Clinical Design Elements markdown document.

Design documents use numbered sections (e.g. "38. Country Allocation / Sites",
"45. Timeline"). This module parses the document into a section tree and provides
typed extractors that target specific sections, eliminating false matches from
other parts of the document.

Usage:
    from engine.design_parser import DesignParser

    parser = DesignParser(design_text)
    timeline = parser.get_timeline()          # Timeline(protocol_approval, fpv, ...)
    countries = parser.get_country_allocation()  # list[CountryEntry]
    enrollment = parser.get_enrollment()      # Enrollment(planned, screen_fail_rate)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ======================================================================
# Data types
# ======================================================================

@dataclass
class Section:
    """One numbered section from the design document."""
    number: str          # e.g. "38", "38.1", "45"
    title: str           # e.g. "Country Allocation / Sites"
    text: str            # full section body (including subsections)
    subsections: list[Section] = field(default_factory=list)
    level: int = 1       # nesting depth (1 = top, 2 = subsection)

    @property
    def body(self) -> str:
        """Section text with any subsection text removed."""
        if not self.subsections:
            return self.text
        # Strip out subsection content
        body = self.text
        for sub in sorted(self.subsections, key=lambda s: len(s.text), reverse=True):
            body = body.replace(sub.text, '', 1) if sub.text in body else body
        return body.strip()


@dataclass
class TimelineEntry:
    name: str
    date: str              # raw date string from document


@dataclass
class Timeline:
    protocol_approval: Optional[str] = None
    fpv: Optional[str] = None
    lpv: Optional[str] = None
    fpet: Optional[str] = None
    lpet: Optional[str] = None
    protocol_content_lock: Optional[str] = None
    design_element_alignment: Optional[str] = None
    raw: list[TimelineEntry] = field(default_factory=list)


@dataclass
class CountryEntry:
    name: str
    abbreviation: str
    pct: Optional[float] = None          # target enrollment fraction (e.g. 0.60)
    consideration: bool = False          # IBU consideration only
    sites: Optional[int] = None          # planned site count (if given)


@dataclass
class CountryAllocation:
    countries: list[CountryEntry] = field(default_factory=list)
    total_screened: Optional[int] = None
    total_randomized: Optional[int] = None
    raw_text: str = ''


@dataclass
class Enrollment:
    planned: Optional[int] = None
    screen_fail_rate: float = 0.30
    early_discontinuation_rate: float = 0.10
    source: str = ''                     # 'design' or 'design_section'


@dataclass
class DesignFlags:
    """Boolean flags and short values extracted from design document."""
    is_immuno_oncology: Optional[bool] = None
    immunogenicity_needed: Optional[bool] = None
    genetics_pgx_collected: Optional[bool] = None
    includes_pediatric: Optional[bool] = None
    is_decentralized: Optional[bool] = None
    therapeutic_area: Optional[str] = None
    compound: Optional[str] = None
    phase: Optional[str] = None          # if specified separately in design


@dataclass
class DesignData:
    """Aggregate result from parsing a full design document."""
    timeline: Timeline = field(default_factory=Timeline)
    country_allocation: CountryAllocation = field(default_factory=CountryAllocation)
    enrollment: Enrollment = field(default_factory=Enrollment)
    flags: DesignFlags = field(default_factory=DesignFlags)
    sections: list[Section] = field(default_factory=list)
    raw_text: str = ''

    @property
    def enrolled(self) -> Optional[int]:
        return self.enrollment.planned

    @property
    def screened(self) -> Optional[int]:
        if self.enrollment.planned and self.enrollment.screen_fail_rate:
            return round(self.enrollment.planned / (1 - self.enrollment.screen_fail_rate))
        return None


# ======================================================================
# Section parser
# ======================================================================

# Pattern for top-level numbered sections: "38.  Country Allocation / Sites"
_SECTION_HEADER_RE = re.compile(
    r'^(\d+(?:\.\d+)*)\.\s+(.+?)(?:\s*\n|$)',
    re.MULTILINE
)

# Pattern for subsection: "38.1  Country Check"
_SUBSECTION_HEADER_RE = re.compile(
    r'^(\d+\.\d+)\.\s+(.+?)(?:\s*\n|$)',
    re.MULTILINE
)


def _dematerialize_table_row(line: str) -> tuple[str, bool]:
    """Strip table-row decoration from a line, returning (candidate, was_table_row).

    Design Elements content that arrives as a Field/Value table (rather than
    prose with headings) gets tag-stripped/pipe-table-rendered upstream
    (preparsed_schema_extractor.py's `_strip_tags`/`_html_tables_to_markdown`)
    into lines like `| 38. Country Allocation / Sites | <value> |` (a real
    Markdown pipe-table row) *and*, separately, ` 38. Country Allocation /
    Sites | <value> | ` (a stray leading space left by a stripped `<tr>`/
    `<td>` open tag, with `</td>` rendered as ` | `, no leading pipe at all) --
    both defeat `_SECTION_HEADER_RE`'s `^\\d+` anchor, so every numbered
    heading silently fails to match and every downstream extractor gets
    nothing. Confirmed directly against a real tabular design-elements
    document (zero sections, zero fields, until both shapes are normalized).
    `was_table_row` is true whenever a `|` was found at all (either the
    genuine pipe-table shape, or the stray-space shape which still carries
    `</td>`'s ` | ` between cells) -- both need the title/value split below.
    """
    stripped = line.strip()
    if stripped.startswith('|'):
        stripped = stripped[1:]
    if stripped.rstrip().endswith('|'):
        stripped = stripped.rstrip()[:-1]
    return stripped.strip(), '|' in line


def parse_sections(text: str) -> list[Section]:
    """Parse the design document into a tree of numbered sections.

    Handles:
      - Top-level sections: "38.  Country Allocation / Sites"
      - Subsections:        "38.1  Country Check"
      - Continuation lines after headings
      - The same headings rendered as a Markdown pipe-table row (a tabular
        design document), heading and value sharing one line/row
    """
    if not text:
        return []

    lines = text.split('\n')
    sections: list[Section] = []
    current_section: Optional[Section] = None
    current_sub: Optional[Section] = None
    current_body: list[str] = []

    def _flush():
        nonlocal current_body
        if current_section is not None and current_body:
            body = '\n'.join(current_body).strip()
            if current_sub is not None:
                current_sub.text = body
            else:
                current_section.text = body
        current_body = []

    for line in lines:
        candidate, was_table_row = _dematerialize_table_row(line)
        m = _SECTION_HEADER_RE.match(candidate)
        if m:
            _flush()
            num, title = m.group(1), m.group(2).strip()
            trailing = ''
            if was_table_row and '|' in title:
                # Row shape "NN. Title | value" -- the value sharing the
                # heading's own row/line is this section's actual content,
                # not part of its title.
                title, _, trailing = title.partition('|')
                title = title.strip()
                trailing = trailing.strip().rstrip('|').strip()
            parts = num.split('.')
            if len(parts) >= 2 and parts[1]:
                # Subsection
                if current_section is not None:
                    current_sub = Section(number=num, title=title, text='', level=2)
                    current_section.subsections.append(current_sub)
            else:
                # Top-level section
                current_sub = None
                current_section = Section(number=num, title=title, text='', level=1)
                sections.append(current_section)
            if trailing:
                current_body.append(trailing)
        else:
            if current_section is not None:
                current_body.append(line)
            # Skip lines before first heading

    _flush()
    return sections


def find_section(sections: list[Section], number: str) -> Optional[Section]:
    """Find a section by its number (e.g. '38', '45', '38.1')."""
    parts = number.split('.')
    for sec in sections:
        if sec.number == number:
            return sec
        if sec.number == parts[0]:
            for sub in sec.subsections:
                if sub.number == number:
                    return sub
    return None


def find_section_by_title(sections: list[Section], keyword: str,
                          case_sensitive: bool = False) -> Optional[Section]:
    """Find a section whose title contains *keyword*."""
    for sec in sections:
        kw = keyword if case_sensitive else keyword.lower()
        title = sec.title if case_sensitive else sec.title.lower()
        if kw in title:
            return sec
        for sub in sec.subsections:
            t = sub.title if case_sensitive else sub.title.lower()
            if kw in t:
                return sub
    return None


# ======================================================================
# Language list detection (for determining number of translation langs)
# ======================================================================

_LANGUAGE_NAMES = [
    'English', 'French', 'German', 'Italian', 'Japanese',
    'Portuguese', 'Chinese', 'Spanish', 'Afrikaans', 'Arabic',
    'Belarusian', 'Bulgarian', 'Croatian', 'Czech', 'Danish',
    'Dutch', 'Estonian', 'Finnish', 'Georgian', 'Greek', 'Hebrew',
    'Hindi', 'Hungarian', 'Indonesian', 'Korean', 'Latvian',
    'Lithuanian', 'Malay', 'Norwegian', 'Polish', 'Romanian',
    'Russian', 'Serbian', 'Slovakian', 'Slovenian', 'Swedish',
    'Thai', 'Taiwanese', 'Turkish', 'Ukrainian',
]


def detect_required_languages(text: str) -> list[str]:
    """Scan design text for a bulleted list of required translation languages."""
    t = _clean(text)
    # Look for "Translations" section or language list
    lang_block = None
    m = re.search(r'(?:Translations|Language)[^.]*(?:list|required|needed)[^.]*', t, re.I)
    if m:
        block_start = max(0, m.start() - 200)
        block_end = min(len(t), m.end() + 1500)
        lang_block = t[block_start:block_end]
    if not lang_block:
        m = re.search(r'(?:translations will be provided in|translate into|languages?:)\s*', t, re.I)
        if m:
            block_end = min(len(t), m.end() + 800)
            lang_block = t[m.start():block_end]

    if lang_block:
        found = []
        for lang in _LANGUAGE_NAMES:
            if re.search(r'\b' + re.escape(lang) + r'\b', lang_block, re.I):
                if lang not in found:
                    found.append(lang)
        if found:
            return found
    return []


# ======================================================================
# Therapeutic area keyword dictionary — shared, so it can act as a document-shape-independent
# fallback (via therapeutic_area_by_keyword) rather than being private to the Q&A-table parser.
# ======================================================================

_THERAPEUTIC_AREA_KEYWORDS = {
    'oncolog': 'Oncology', 'cancer': 'Oncology', 'tumor': 'Oncology', 'tumour': 'Oncology',
    'breast': 'Oncology - Breast Cancer', 'lung': 'Oncology - Lung Cancer',
    'melanoma': 'Oncology', 'lymphoma': 'Oncology', 'leukemia': 'Oncology',
    'pain': 'Neuroscience - Pain', 'neuropath': 'Neuroscience - Pain',
    'migraine': 'Neuroscience - Pain', 'fibromyalgia': 'Neuroscience - Pain',
    'alzheimer': 'Neuroscience', 'dementia': 'Neuroscience', 'parkinson': 'Neuroscience',
    'schizophreni': 'Neuroscience', 'depression': 'Neuroscience', 'bipolar': 'Neuroscience',
    'diabetes': 'Diabetes/Endocrinology', 'diabetic': 'Diabetes/Endocrinology',
    'obesity': 'Diabetes/Endocrinology', 'weight': 'Diabetes/Endocrinology',
    'cardiovascular': 'Cardiovascular', 'heart failure': 'Cardiovascular',
    'atherosclerosis': 'Cardiovascular',
    'rheumatoid': 'Immunology', 'psoriasis': 'Immunology', 'atopic dermatitis': 'Immunology',
    'lupus': 'Immunology', 'crohn': 'Immunology', 'colitis': 'Immunology',
    'asthma': 'Immunology', 'eczema': 'Immunology',
}


def therapeutic_area_by_keyword(text: str) -> Optional[str]:
    """Last-resort therapeutic-area fallback: scan arbitrary text for a disease/indication
    keyword, with no assumption about document structure at all. Used after both the explicit
    "Therapeutic Area:"-style label match and the section-scoped keyword scan (Q&A table's
    Title/Overview rows) have already been tried and found nothing."""
    if not text:
        return None
    t = text.lower()
    for keyword, ta_value in _THERAPEUTIC_AREA_KEYWORDS.items():
        if keyword in t:
            return ta_value
    return None


# ======================================================================
# Helpers
# ======================================================================

def _clean(text: str) -> str:
    """Normalize whitespace and remove encoding artifacts."""
    t = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return t


def _extract_number(text: str, pattern: str, group: int = 1) -> Optional[int]:
    """Extract an integer from a regex search."""
    m = re.search(pattern, text, re.I)
    if m:
        try:
            return int(m.group(group).replace(',', ''))
        except (ValueError, IndexError):
            pass
    return None


def _parse_date(text: str) -> Optional[str]:
    """Extract and normalize a date string (DD-Mon-YYYY or Mon-YYYY)."""
    m = re.search(
        r'(\d{1,2})\s*[- ]\s*(January|February|March|April|May|June|July|'
        r'August|September|October|November|December)\s*[- ]\s*(\d{4})',
        text, re.I
    )
    if m:
        day, mon, yr = m.group(1), m.group(2), m.group(3)
        mon_abbr = mon[:3]
        return f'{day}-{mon_abbr}-{yr}'
    m = re.search(
        r'(January|February|March|April|May|June|July|August|September|'
        r'October|November|December)\s*[- ]\s*(\d{4})', text, re.I
    )
    if m:
        mon, yr = m.group(1), m.group(2)
        return f'{mon[:3]}-{yr}'
    return None


def _crop(text: str, keyword: str, window: int = 3000) -> str:
    """Extract a window of text starting at *keyword*."""
    idx = text.lower().find(keyword.lower())
    if idx < 0:
        return ''
    return text[idx:idx + window]


# ======================================================================
# Section-specific extractors
# ======================================================================

def extract_timeline(section: Section) -> Timeline:
    """Extract milestone dates from Section 45 (Timeline).

    Supports formats:
        Protocol Approval: 15-Jan-2026
        FPV: December-2026
        FPV:  01-Jun-2026
        LPV: 01-Jun-2028
    """
    t = section.body if section.text else ''
    if not t:
        t = section.text
    result = Timeline()

    # Map milestone names to field names
    milestone_map = {
        'Protocol Approval': 'protocol_approval',
        'FPV': 'fpv',
        'LPV': 'lpv',
        'FPET': 'fpet',
        'LPET': 'lpet',
        'Protocol Content Lock': 'protocol_content_lock',
        'Design Element Alignment': 'design_element_alignment',
    }

    for label, field in milestone_map.items():
        # Match "Label: value" on same or next line
        m = re.search(
            re.escape(label) + r'\s*:\s*([^\n\r]+?)(?:\s*[•\n\r]|$)',
            t, re.I
        )
        if m:
            val = m.group(1).strip().rstrip('.')
            if val and val.lower() != 'tbd' and val.lower() != 'to be determined':
                setattr(result, field, val)
                result.raw.append(TimelineEntry(name=label, date=val))

    return result


def extract_country_allocation_section(section: Section) -> CountryAllocation:
    """Extract country allocation from Section 38 or similar.

    Handles:
      - "US at target 60%"
      - "Germany at target 10%"
      - "Other IBU: consideration can be given to Italy and Spain"
      - "Based on DSB countries in scope include US, Germany, UK, Canada"
    """
    t = section.body if section.text else ''
    if not t:
        t = section.text

    result = CountryAllocation(raw_text=t[:2000])
    seen_abbrs: dict[str, CountryEntry] = {}

    def _get_abbr(name: str) -> Optional[str]:
        """Try to resolve a name to a _COUNTRY_INFO abbreviation."""
        from engine.extractors import _COUNTRY_INFO, _COUNTRY_ALIASES
        name_s = name.strip().rstrip('.')
        # Direct lookup in aliases
        abbr = _COUNTRY_ALIASES.get(name_s)
        if abbr:
            return abbr
        # Try case-insensitive
        for alias, a in _COUNTRY_ALIASES.items():
            if alias.lower() == name_s.lower():
                return a
        return None

    def _add(abbr: str, *, pct: Optional[float] = None, consideration: bool = False):
        if abbr in seen_abbrs:
            entry = seen_abbrs[abbr]
            if pct is not None and entry.pct is None:
                entry.pct = pct
            if consideration:
                entry.consideration = True
            return
        from engine.extractors import _COUNTRY_INFO
        info = _COUNTRY_INFO.get(abbr, {})
        entry = CountryEntry(
            name=info.get('name', abbr),
            abbreviation=info.get('abbreviation', abbr),
            pct=pct,
            consideration=consideration,
        )
        seen_abbrs[abbr] = entry

    def _find_abbrs(fragment: str) -> list[str]:
        from engine.extractors import _COUNTRY_ALIASES
        found = []
        for alias, abbr in _COUNTRY_ALIASES.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', fragment):
                if abbr not in found:
                    found.append(abbr)
        return found

    # Phase 1: "X at target Y%" patterns
    for m in re.finditer(r'(\w+(?:\s+\w+)?)\s+at\s+target\s+(\d+)%', t, re.I):
        name = m.group(1).strip()
        pct = int(m.group(2)) / 100.0
        abbr = _get_abbr(name)
        if abbr:
            _add(abbr, pct=pct)

    # Phase 2: "X, Y, and Z will be in scope"
    m = re.search(r'(?:will be in scope|countries in scope include|'
                  r'in scope for country allocation)[^.]*\.', t, re.I)
    if m:
        for abbr in _find_abbrs(m.group(0)):
            if abbr not in seen_abbrs:
                _add(abbr)

    # Phase 3: IBU consideration
    m = re.search(r'(?:Other\s+)?IBU[^.]*', t, re.I)
    if m:
        ibu_text = m.group(0)
        for abbr in _find_abbrs(ibu_text):
            _add(abbr, consideration=True)

    # Phase 4: "Based on DSB countries in scope include ..."
    cc_start = re.search(r'Based on DSB countries in scope include', t, re.I)
    if cc_start:
        chunk = t[cc_start.start():cc_start.start() + 2000]
        for abbr in _find_abbrs(chunk):
            if abbr not in seen_abbrs:
                _add(abbr)

    # Phase 5: standalone country mentions with site counts
    for m in re.finditer(r'(\d+)\s*(?:sites?|patients?|subjects?)\s*(?:in|for)\s+'
                         r'(\w+(?:\s+\w+)?)', t, re.I):
        count, name = int(m.group(1)), m.group(2).strip()
        abbr = _get_abbr(name)
        if abbr:
            _add(abbr)
            if seen_abbrs[abbr].sites is None:
                seen_abbrs[abbr].sites = count

    # Phase 6: single-country prose with no percentage, e.g. "This trial will
    # be US-only" / "will be sited all in the United States". Every match is
    # validated against the known country alias table (_get_abbr) before
    # being accepted, so this can't invent a country from an unrelated
    # "X-only" phrase (e.g. "single-only").
    if not seen_abbrs:
        for m in re.finditer(r'\b([A-Za-z][\w\s]{0,25}?)-only\b', t):
            abbr = _get_abbr(m.group(1).strip())
            if abbr:
                _add(abbr)
        for m in re.finditer(
                r'(?:will be|is)\s+(?:sited\s+)?(?:all\s+)?(?:only\s+)?in\s+'
                r'(?:the\s+)?([A-Za-z][\w\s]{0,25}?)(?=[\.,;]|\s+and\b|$)', t, re.I):
            abbr = _get_abbr(m.group(1).strip())
            if abbr:
                _add(abbr)

    # Phase 7: broadest tier, absolute last resort -- any sentence mentioning both an
    # enrollment/site-relevant verb and one or more known country names, e.g. "Approximately 340
    # subjects will be enrolled across the United States and Japan." Still bounded to a single
    # sentence containing an explicit enrollment/conduct/site verb (not just any country mention
    # anywhere in the document), so this doesn't reopen the over-inclusion bug documented above
    # (site lists, translation notes, and amendment history routinely name countries without
    # being the actual allocation decision).
    if not seen_abbrs:
        enrollment_verb = re.compile(
            r'\b(?:enroll\w*|conduct\w*|sit(?:e|ed|ing)\w*|study\s+will|trial\s+will|take\s+place)',
            re.I)
        for sentence in re.split(r'(?<=[.!?])\s+', t):
            if not enrollment_verb.search(sentence):
                continue
            for abbr in _find_abbrs(sentence):
                _add(abbr)

    result.countries = sorted(seen_abbrs.values(),
                              key=lambda c: (c.consideration, c.name))
    return result


def extract_enrollment_section(section: Section) -> Enrollment:
    """Extract planned enrollment from Section 39 (Planned Enrollment).

    Handles:
      - "Approximately 300 patients will be enrolled."
      - "Randomized: 240"
      - "Screen failure rate: 20%"
    """
    t = section.body if section.text else ''
    if not t:
        t = section.text

    result = Enrollment(source='design_section')

    n = _extract_number(t, r'(?:Approximately|About|A total of|Total|Planned|Estimated)\s+'
                          r'([\d,]+)\s+(?:patients?|subjects?|participants?)', 1)
    if n:
        result.planned = n
    else:
        n = _extract_number(t, r'(?:Randomized|Enrolled|Sample\s+Size)\s*(?::|is|=)\s*([\d,]+)', 1)
        if n:
            result.planned = n
    if not result.planned:
        n = _extract_number(t, r'(\d{3,4})\s+(?:patients?|subjects?)', 1)
        if n:
            result.planned = n

    # Screen fail rate
    sf = _extract_number(t, r'(?:screen\s+fail(?:ure)?|screen\s+fail(?:ure)?\s+rate)\s*(?::|is|=)?\s*(\d{1,3})\s*%', 1)
    if sf:
        result.screen_fail_rate = sf / 100.0

    # Early discontinuation rate
    ed = _extract_number(t, r'(?:early\s+discontinuation|dropout|withdrawal)\s*(?:rate)?\s*(?::|is|=)?\s*(\d{1,3})\s*%', 1)
    if ed:
        result.early_discontinuation_rate = ed / 100.0

    return result


def extract_flags_section(sections: list[Section]) -> DesignFlags:
    """Extract boolean/short-value flags by scanning relevant sections."""
    result = DesignFlags()

    # Immuno-oncology: check design keywords
    for sec in sections:
        t = (sec.body or sec.text).lower()
        if any(kw in t for kw in ['immuno oncology', 'immuno-oncology',
                                   'io protocol', 'checkpoint inhibitor',
                                   'anti-pd1', 'anti-pdl1']):
            result.is_immuno_oncology = True
            break

    # Immunogenicity
    for sec in sections:
        m = re.search(r'immunogenicity\s*(?:testing)?\s*(?::|is|=)?\s*(Yes|No)',
                      sec.text, re.I)
        if m:
            result.immunogenicity_needed = m.group(1).lower() == 'yes'
            break

    # Genetics/PGx
    for sec in sections:
        m = re.search(r'(?:Genetics|PGx|pharmacogenomic)\s*(?:sample\s+)?collected\s*(?::|is|=)?\s*(Yes|No)',
                      sec.text, re.I)
        if m:
            result.genetics_pgx_collected = m.group(1).lower() == 'yes'
            break

    # Pediatric
    for sec in sections:
        t = (sec.body or sec.text).lower()
        if 'pediatric' in t or 'pediatric population' in t:
            m = re.search(r'pediatric\s*(?:population|study)?\s*(?::|is|=|includes?)?\s*(Yes|No)',
                          sec.text, re.I)
            if m:
                result.includes_pediatric = m.group(1).lower() == 'yes'
            elif 'no pediatric' in t:
                result.includes_pediatric = False
            break

    # Decentralized
    full_text = '\n'.join(s.text for s in sections).lower()
    if any(kw in full_text for kw in ['decentralized', 'mobile nursing',
                                       'home health', 'virtual trial',
                                       'direct to patient']):
        for sec in sections:
            m = re.search(r'decentralized\s*(?:trial|study|services)?\s*(?::|is|=|required)?\s*(Yes|No)',
                          sec.text, re.I)
            if m:
                result.is_decentralized = m.group(1).lower() == 'yes'
                break

    return result


def extract_therapeutic_area(text: str) -> Optional[str]:
    """Extract therapeutic area from design text."""
    patterns = [
        r'^\s*Therapeutic\s+Area\s*:\s*(.+)$',
        r'Therapeutic\s+Area\s*[\(\)]*\s*:\s*(.+?)(?:\n|$)',
        r'TA:\s*(.+?)(?:\n|$)',
    ]
    for p in patterns:
        m = re.search(p, text, re.I | re.MULTILINE)
        if m:
            v = m.group(1).strip().rstrip('.')
            if len(v) > 3:
                return v
    return None


# ======================================================================
# Q&A Table Format Parser (alternate CDE format)
# ======================================================================

_PIPE_QA_HEADER = re.compile(
    # Real CDE tables can render a stray leading character before "Q#"
    # (e.g. a literal backtick baked into the source docx's header cell).
    r'^\|\s*\S?\s*Q\s*#\s*\|\s*Sub\s*Section\s*\|\s*Design\s*Element\s*\|\s*Design\s*Detail',
    re.IGNORECASE | re.MULTILINE,
)


def _detect_qa_table(text: str) -> bool:
    """Return True if the text appears to be a Q&A table format CDE.

    Two source formats are supported: cells joined by newlines (older
    python-docx extraction convention), and a single-line pipe-delimited row
    per table row (the convention used when reading the .docx directly via
    python-docx, e.g. `"| " + " | ".join(cell.text for cell in row.cells)`).
    """
    return bool(
        re.search(r'(?:^|\n)\s*`?Q#', text) or
        re.search(r'Sub Section\nDesign Element\nDesign Detail', text) or
        re.search(r'\d+\nTitle\n', text) or
        _PIPE_QA_HEADER.search(text)
    )


def _split_pipe_qa_rows(text: str) -> list[dict]:
    """Split a pipe-delimited Q&A table (one row per line, e.g. from a direct
    python-docx read) into the same {q, sub_section, element, detail} shape
    `_split_qa_rows` produces for the newline-joined format."""
    from md_table import find_pipe_tables, parse_pipe_table

    rows = []
    for block in find_pipe_tables(text):
        headers, data_rows = parse_pipe_table(block)
        if not headers:
            continue
        col_idx = {}
        for name in ('q', 'sub section', 'design element', 'design detail'):
            for i, h in enumerate(headers):
                if name in h.lower():
                    col_idx[name] = i
                    break
        if 'sub section' not in col_idx or 'design detail' not in col_idx:
            continue
        for cells in data_rows:
            def _get(key):
                idx = col_idx.get(key)
                return cells[idx].strip() if idx is not None and idx < len(cells) else ''
            rows.append({
                'q': _get('q'),
                'sub_section': _get('sub section'),
                'element': _get('design element'),
                'detail': _get('design detail'),
            })
    return rows


def _split_qa_rows(text: str) -> list[dict]:
    """Split Q&A table text into structured rows.

    The python-docx extraction joins table cells with newlines, producing
    a repeating pattern of: Q_number, Sub_Section, Design_Element, Design_Detail.
    We detect row boundaries by looking for lines that are just a number (the Q#).
    """
    if _PIPE_QA_HEADER.search(text):
        pipe_rows = _split_pipe_qa_rows(text)
        if pipe_rows:
            return pipe_rows

    lines = text.split('\n')
    rows = []
    current_row = None

    # Strategy: find each Q# line (a line that's just a small integer),
    # then collect the following content into sub_section, element, detail.
    # However, the header row and other noise may be present.
    # Better approach: look for the structured pattern where Q# numbers
    # appear sequentially.

    # First, find all positions of Q# numbers
    q_positions = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.match(r'^\d{1,3}$', stripped):
            # Potential Q# — verify it's followed by a known sub-section keyword
            # within the next few lines
            following = ' '.join(lines[i+1:i+4])
            if any(kw in following for kw in [
                'Title', 'Overview', 'Objectives', 'Population', 'Peds',
                'Stats', 'Design', 'CT Material', 'Safety', 'Patient Burden',
                'Diversity', 'Risk Assessment', 'Timeline', 'Country Check',
                'Investigator Insights', 'Optionality', 'Design Check',
                'Design Alignment', 'Objectives / Stats', 'Design / CT Material',
            ]):
                q_positions.append(i)

    if not q_positions:
        return []

    # Extract each row
    for idx, pos in enumerate(q_positions):
        end = q_positions[idx + 1] if idx + 1 < len(q_positions) else len(lines)
        chunk_lines = lines[pos:end]

        q_num = chunk_lines[0].strip()
        # The rest is: sub_section, element, detail (may span many lines)
        rest = '\n'.join(chunk_lines[1:])

        # Try to split into sub_section | element | detail
        # Sub-section is typically a short label on its own line
        sub_section = ''
        element = ''
        detail = ''

        remaining_lines = chunk_lines[1:]
        if remaining_lines:
            sub_section = remaining_lines[0].strip()
            # Element is typically the next line (or lines until detail starts)
            # Detail is everything after the element description
            if len(remaining_lines) > 1:
                element = remaining_lines[1].strip()
                detail = '\n'.join(remaining_lines[2:]).strip()
            elif len(remaining_lines) > 0:
                element = remaining_lines[0].strip()

        rows.append({
            'q': q_num,
            'sub_section': sub_section,
            'element': element,
            'detail': detail,
        })

    return rows


def parse_qa_table(text: str) -> Optional[DesignData]:
    """Parse a Q&A table format CDE document.

    Returns DesignData if the format is detected and parsed, else None.
    """
    if not _detect_qa_table(text):
        return None

    rows = _split_qa_rows(text)
    if not rows:
        return None

    data = DesignData(raw_text=text)

    # Collect all detail text by sub-section for extraction
    by_section: dict[str, list[dict]] = {}
    for row in rows:
        sec = row['sub_section'].lower().strip()
        by_section.setdefault(sec, []).append(row)

    # ── Enrollment (from Stats section) ──
    for row in by_section.get('stats', []) + by_section.get('objectives / stats', []):
        detail = row['detail']
        # Try N=XXX pattern (may be a range like N=455-570)
        m = re.search(r'N\s*=\s*(\d+)', detail)
        if m and data.enrollment.planned is None:
            data.enrollment.planned = int(m.group(1))
            data.enrollment.source = 'design_qa_table'
        # Try "Approximately NNN" pattern
        if data.enrollment.planned is None:
            m = re.search(r'(?:Approximately|About|Total of)\s+(\d+)', detail, re.I)
            if m:
                data.enrollment.planned = int(m.group(1))
                data.enrollment.source = 'design_qa_table'
        # Early discontinuation rate
        m = re.search(r'discontinuation\s+rate\s+(?:of\s+)?(?:approximately\s+)?(\d+)\s*%', detail, re.I)
        if m:
            data.enrollment.early_discontinuation_rate = int(m.group(1)) / 100.0
        # Screen fail rate
        m = re.search(r'screen\s*fail(?:ure)?\s*(?:rate)?\s*[:=]?\s*(\d+)\s*%', detail, re.I)
        if m:
            data.enrollment.screen_fail_rate = int(m.group(1)) / 100.0

    # ── Timeline ──
    for row in by_section.get('timeline', []):
        detail = row['detail']
        _timeline_map = {
            'Protocol Approval': 'protocol_approval',
            'First Patient Visit': 'fpv',
            'First Patient Entered Treatment': 'fpet',
            'Last Patient Visit': 'lpv',
            'Last Patient Entered Treatment': 'lpet',
            'LPV All': 'lpv',
        }
        # Rendering collapses newlines to spaces (see mcp_server.py::parse_design_doc),
        # so multiple "Label: value" milestones sit on one line with no
        # newline to stop at — bound each capture at the next recognized
        # label instead, or end of string. Includes a few boundary-only
        # labels (not stored as their own field) that otherwise leak into
        # the preceding milestone's value.
        _boundary_only = ['Interim Timepoint(s)', 'Interim Timepoint', 'Last Patient Visit PO',
                          'Design Alignment', 'Design Element Alignment', 'Protocol Content Lock']
        _labels_pattern = '|'.join(re.escape(l) for l in list(_timeline_map) + _boundary_only)
        for label, field in _timeline_map.items():
            m = re.search(
                # Boundary label may have a trailing parenthetical before its
                # colon, e.g. "Interim Timepoint(s) (if applicable): June 2025".
                re.escape(label) + r'\s*:\s*(.+?)(?=\s*(?:' + _labels_pattern + r')(?:\s*\([^)]*\))?\s*:|$)',
                detail, re.I,
            )
            if m:
                val = m.group(1).strip().rstrip('.')
                if val and val.lower() not in ('tbd', 'to be determined', 'n/a'):
                    if getattr(data.timeline, field) is None:
                        setattr(data.timeline, field, val)
                        data.timeline.raw.append(TimelineEntry(name=label, date=val))

    # ── Country Allocation ──
    for row in by_section.get('country check', []):
        detail = row['detail']
        # Import country aliases for scanning
        try:
            from extractors import _COUNTRY_ALIASES, _COUNTRY_INFO
        except ImportError:
            break
        seen = {}
        for alias, abbr in _COUNTRY_ALIASES.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', detail):
                if abbr not in seen:
                    info = _COUNTRY_INFO.get(abbr, {})
                    entry = CountryEntry(
                        name=info.get('name', abbr),
                        abbreviation=info.get('abbreviation', abbr),
                    )
                    # Check for percentage
                    m_pct = re.search(re.escape(alias) + r'[^.]*?(\d+)\s*%', detail)
                    if m_pct:
                        entry.pct = int(m_pct.group(1)) / 100.0
                    # Check for consideration
                    ctx = detail[max(0, detail.lower().find(alias.lower()) - 20):
                                 detail.lower().find(alias.lower()) + len(alias) + 50]
                    if 'consideration' in ctx.lower() or 'low probability' in ctx.lower():
                        entry.consideration = True
                    seen[abbr] = entry
        data.country_allocation.countries = sorted(
            seen.values(), key=lambda c: (c.consideration, c.name)
        )
        data.country_allocation.raw_text = detail[:2000]

    # ── Flags ──
    # Immunogenicity
    for row in by_section.get('design', []) + by_section.get('design / ct material', []):
        elem_lower = row['element'].lower()
        detail_lower = row['detail'].lower()
        if 'immunogenicity' in elem_lower:
            if detail_lower.startswith('yes') or 'will be collected' in detail_lower:
                data.flags.immunogenicity_needed = True
            elif detail_lower.startswith('no'):
                data.flags.immunogenicity_needed = False
        if 'genetics' in elem_lower or 'pgx' in elem_lower:
            if detail_lower.startswith('yes') or 'collect' in detail_lower:
                data.flags.genetics_pgx_collected = True
            elif detail_lower.startswith('no') or detail_lower.startswith('n/a'):
                data.flags.genetics_pgx_collected = False

    # Pediatric — the actual age criteria is often filed under "Population"
    # or "Inclusion/Eligibility", not a dedicated "Peds" row, so read the real
    # age range with the same extractor populate_rfp.py's protocol-text
    # fallback already uses, rather than relying solely on a yes/no keyword.
    from extractors import min_age as _min_age
    _peds_rows = (by_section.get('peds', []) + by_section.get('population', []) +
                 by_section.get('inclusion', []) + by_section.get('eligibility', []))
    for row in _peds_rows:
        age = _min_age(row['detail'])
        if age is not None:
            data.flags.includes_pediatric = age < 18
            break
    if data.flags.includes_pediatric is None:
        for row in by_section.get('peds', []):
            detail_lower = row['detail'].lower()
            if 'n/a' in detail_lower or 'only participants over 18' in detail_lower or 'adults only' in detail_lower:
                data.flags.includes_pediatric = False
            elif 'yes' in detail_lower or 'adolescent' in detail_lower or 'pediatric' in detail_lower:
                data.flags.includes_pediatric = True

    # Phase (from Title rows)
    for row in by_section.get('title', []):
        if 'phase' in row['element'].lower():
            m = re.search(r'Phase\s+(\d+[a-z]?)', row['detail'], re.I)
            if m:
                data.flags.phase = m.group(1)

    # Therapeutic area — extract from title, overview, or explicit TA field
    if data.flags.therapeutic_area is None:
        # Search title + overview rows for TA keywords
        _ta_search_text = ''
        for row in by_section.get('title', []) + by_section.get('overview', []):
            _ta_search_text += ' ' + row.get('detail', '') + ' ' + row.get('element', '')
        ta_value = therapeutic_area_by_keyword(_ta_search_text)
        if ta_value:
            data.flags.therapeutic_area = ta_value

    # Also scan the Overview rows for region/country info as a fallback for countries
    if not data.country_allocation.countries:
        for row in by_section.get('overview', []):
            if 'regulatory' in row['element'].lower() or 'country' in row['element'].lower():
                detail = row['detail']
                try:
                    from extractors import _COUNTRY_ALIASES, _COUNTRY_INFO
                    seen = {}
                    for alias, abbr in _COUNTRY_ALIASES.items():
                        if re.search(r'\b' + re.escape(alias) + r'\b', detail):
                            if abbr not in seen:
                                info = _COUNTRY_INFO.get(abbr, {})
                                seen[abbr] = CountryEntry(
                                    name=info.get('name', abbr),
                                    abbreviation=info.get('abbreviation', abbr),
                                )
                    if seen:
                        data.country_allocation.countries = sorted(
                            seen.values(), key=lambda c: (c.consideration, c.name)
                        )
                except ImportError:
                    pass

    return data


# ======================================================================
# Main parser class
# ======================================================================

class DesignParser:
    """Parse a full Clinical Design Elements markdown document.

    Usage:
        parser = DesignParser(design_text)
        data = parser.parse()

        # Access specific sections:
        data.timeline.fpv        # First Patient Visit date
        data.enrollment.planned  # planned enrollment
        data.country_allocation.countries  # list of CountryEntry
    """

    def __init__(self, text: str):
        self._raw = text
        self._sections: list[Section] = []

    def parse(self) -> DesignData:
        """Parse the full document and return structured data."""
        self._sections = parse_sections(self._raw)

        # If no numbered sections found, try Q&A table format
        if not self._sections:
            qa_data = parse_qa_table(self._raw)
            if qa_data is not None:
                return qa_data

        data = DesignData(raw_text=self._raw, sections=self._sections)

        # Timeline
        sec = find_section(self._sections, '45') or find_section_by_title(self._sections, 'Timeline')
        if sec:
            data.timeline = extract_timeline(sec)
        elif self._sections:
            # Fallback: search entire text for Document Key Milestone Estimates
            t = _crop(self._raw, 'Document Key Milestone Estimates') or _crop(self._raw, 'Key Milestone')
            if t:
                dummy_sec = Section(number='', title='', text=t)
                data.timeline = extract_timeline(dummy_sec)

        # Country allocation
        sec = find_section(self._sections, '38') or find_section_by_title(self._sections, 'Country')
        if sec:
            data.country_allocation = extract_country_allocation_section(sec)
        elif self._sections:
            # Fallback: comprehensive search
            for s in self._sections:
                if re.search(r'countr|allocat|site', s.title, re.I):
                    data.country_allocation = extract_country_allocation_section(s)
                    break

        # Enrollment
        sec = find_section(self._sections, '39') or find_section_by_title(self._sections, 'Enrollment')
        if sec:
            data.enrollment = extract_enrollment_section(sec)
        else:
            # Fallback: scan all sections
            for s in self._sections:
                enr = extract_enrollment_section(s)
                if enr.planned is not None:
                    data.enrollment = enr
                    break

        # Flags from design
        data.flags = extract_flags_section(self._sections)

        # Therapeutic area from design (if found as plain field)
        ta = extract_therapeutic_area(self._raw)
        if ta:
            data.flags.therapeutic_area = ta

        return data

    @property
    def sections(self) -> list[Section]:
        return self._sections


# ======================================================================
# Convenience functions
# ======================================================================

def parse_design(text: str) -> DesignData:
    """One-shot: parse a design document and return structured data."""
    return DesignParser(text).parse()


def section_summary(sections: list[Section]) -> str:
    """Return a compact summary of all sections found."""
    lines = []
    for sec in sections:
        body_preview = (sec.body or sec.text)[:60].replace('\n', ' ').strip()
        lines.append(f"  {sec.number}. {sec.title}")
        if body_preview:
            lines[-1] += f" — {body_preview}..."
        for sub in sec.subsections:
            sub_preview = (sub.body or sub.text)[:50].replace('\n', ' ').strip()
            lines.append(f"    {sub.number}. {sub.title}")
            if sub_preview:
                lines[-1] += f" — {sub_preview}..."
    return '\n'.join(lines)


# ======================================================================
# Self-test
# ======================================================================

if __name__ == '__main__':
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path:
        with open(path, encoding='utf-8') as f:
            text = f.read()
        parser = DesignParser(text)
        data = parser.parse()
        print(f"Sections found: {len(parser.sections)}")
        if parser.sections:
            print(section_summary(parser.sections))
        print(f"\nTimeline: {data.timeline}")
        print(f"Country allocation: {len(data.country_allocation.countries)} countries")
        print(f"Enrollment: {data.enrollment}")
        print(f"Flags: {data.flags}")
    else:
        print("Usage: python design_parser.py <design_markdown.txt>")
