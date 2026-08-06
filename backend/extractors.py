"""Flexible multi-pattern field extraction for clinical protocols.

Each extractor tries multiple regex patterns in order (most specific -> most generic)
to handle variations in protocol formatting across different studies.

Lilly-optimized: prioritizes Lilly protocol number format (XXXX-XX-XXXXX),
LY compound codes, and phase from title (Arabic + Roman numerals).
"""
import functools
import re
from dataclasses import dataclass
from typing import Optional, List


@dataclass
class Finding:
    """Structured finding for the RFP fill report."""
    field: str
    value: str = ''
    source: str = ''
    status: str = 'review'


@functools.lru_cache(maxsize=8)
def _normalize_for_extraction(text: str) -> str:
    """Fix PDF extraction artifacts that break code/identifier matching."""
    # Only apply aggressive space-removal when text contains hyphenated codes
    # (protocol numbers like I6T-MC-AMBX, not ALL-CAPS titles like "PHASE 3")
    if re.search(r'[A-Z0-9](?: [A-Z0-9])+-[A-Z0-9]', text):
        text = re.sub(r'(?<=[A-Z0-9]) (?=[A-Z0-9])', '', text)
    # Fix: long sequences of single-letter-spaced words from PDF extraction
    text = re.sub(
        r'\b([A-Za-z])(?: ([A-Za-z])){3,}\b',
        lambda m: ''.join(m.group(0).split()), text
    )
    return text


def _get_text(text: str) -> str:
    """Return normalized text. thin wrapper that handles None."""
    if text is None:
        return ''
    return _normalize_for_extraction(text)


def _try_patterns(text: str, patterns: list) -> Optional[str]:
    """Try multiple regex patterns, return first match group."""
    for p in patterns:
        m = re.search(p, text)
        if m:
            return m.group(1).strip() if m.lastindex else m.group(0).strip()
    return None


# ═══════════════════════════════════════════════════════════════
# PROTOCOL NUMBER
# ═══════════════════════════════════════════════════════════════

def protocol_number(text: str) -> Optional[str]:
    """Extract protocol number.
    
    Lilly-optimized: prioritizes XXXX-XX-XXXXX format (e.g. H8H-MC-LAHD, I6T-MC-AMBX).
    Falls back to general pharma codes and standard labels.
    """
    t = _get_text(text)
    return _try_patterns(t, [
        # -- Lilly format (labeled) --
        r'(?:Protocol\s*(?:Number|No\.?|#|ID)?\s*[:\.]\s*)'
        r'([A-Z0-9]{2,4}-[A-Z]{2}-[A-Z0-9]{3,6}[a-z]?(?:\([a-z]\))?)',
        r'(?:Protocol\s*(?:Number|No\.?|#|ID)?\s*[:\.]\s*)'
        r'([A-Z0-9][A-Z0-9\-\(\)\/\.]{3,25})',
        r'Protocol\s+(?:number|Number)\s+'
        r'([A-Z0-9]{2,4}-[A-Z]{2}-[A-Z0-9]{3,6}[a-z]?(?:\([a-z]\))?)',
        # -- Standalone Lilly code (appears in headers) --
        r'([A-Z0-9]{2,4}-[A-Z]{2}-[A-Z0-9]{3,6}[a-z]?(?:\([a-z]\))?)',
        # -- Trial ID patterns --
        r'(?:Trial\s*(?:ID|No|#|Number)\s*:?\s*)([A-Z0-9][A-Z0-9\-]{3,20})',
        r'(?:Study\s*(?:ID|No|#|Number)\s*:?\s*)([A-Z0-9][A-Z0-9\-]{3,20})',
        # -- Clinical Protocol header --
        r'Clinical\s+Protocol\s+([A-Z0-9]+\-[A-Z0-9]+\-[A-Z0-9]+[a-z]?)',
        r'Protocol\s+([A-Z0-9]{3,6}\-[A-Z0-9]{2}\-[A-Z0-9]{2,6}[a-z]?)',
        # -- General pharma codes (require alphanumeric start/end in each segment) --
        r'([A-Z]{2,4}\-[A-Z]{2}\-[A-Z0-9]{4,6}[a-z]?(?:\([a-z]\))?)',
        r'(?:^|)([A-Z0-9]{2,6}\-[A-Z0-9]{4,8})(?!\d)',
    ])


# ═══════════════════════════════════════════════════════════════
# COMPOUND
# ═══════════════════════════════════════════════════════════════

def compound(text: str) -> Optional[str]:
    """Extract compound/drug name and identifier.
    
    Lilly-optimized: prioritizes LY###### codes, then labeled drug names,
    then capitalized drug names from title context.
    """
    t = _get_text(text)
    
    # Pattern 1: Explicit Compound: label (full name)
    m = re.search(r'Compound\s*:\s*([A-Za-z][A-Za-z0-9\s\-]+?)\s*\(\s*(?:LY\d{6,8}|[A-Z0-9]+)\s*\)', t, re.I)
    if m:
        return m.group(1).strip()
    # Pattern 1b: Just the LY code
    m = re.search(r'Compound\s*:\s*\w+\s*\(\s*(LY\d{6,8})\s*\)', t, re.I)
    if m:
        return m.group(1)
    # Also catch "Compound: LY3074828" directly (no drug name before)
    m = re.search(r'Compound\s*:\s*(LY\d{6,8})\b', t, re.I)
    if m:
        return m.group(1)
    
    m = re.search(r'Compound\s*:\s*([^;\n]{2,60})', t)
    if m:
        result = re.sub(r'\s+', ' ', m.group(1).strip())
        # Clean: remove trailing parenthetical context
        result = re.sub(r'\s*\(.*', '', result).strip()
        if len(result) > 2:
            return result
    
    # Pattern 2: Other drug labels
    result = _try_patterns(t, [
        r'Investigational\s+(?:Product|Agent|Drug)(?:\(s\))?:\s*([^;\n]+)',
        r'(?:Drug|Agent|Product)(?:\s+under\s+investigation)?:\s*([^;\n]+)',
    ])
    if result:
        result = re.sub(r'\s+', ' ', result).strip()
        if len(result) > 2:
            return result
    
    # Pattern 3: LY code anywhere in text (highest confidence match)
    m = re.search(r'LY\d{6,8}', t)
    if m:
        return m.group(0)
    
    # Pattern 4: Capitalized drug name from title context
    title_match = re.search(
        r'(?:Effect|Study|Trial|Comparison|Safety|Efficacy|Bioequivalence)'
        r'(?:\s+of\s+|\s+of\s+the\s+|\s+of\s+a\s+|\s+of\s+an\s+)'
        r'([A-Z][a-z]{4,}(?:\s*[A-Z][a-z]{2,})?)',
        t[:3000]
    )
    if title_match:
        drug = title_match.group(1).strip()
        common = {'Patients', 'Subjects', 'Participants', 'Treatment', 'Therapy',
                  'Healthy', 'Methodology', 'Approach', 'Method', 'New', 'Novel',
                  'Injectable', 'Oral', 'Intravenous', 'Subcutaneous', 'Topical',
                  'Human', 'Clinical', 'Single', 'Multiple', 'Following', 'Different',
                  'Various', 'Several', 'Alternative'}
        if drug not in common and len(drug) > 4:
            return drug
    
    return None


# ═══════════════════════════════════════════════════════════════
# PROTOCOL TITLE
# ═══════════════════════════════════════════════════════════════

def protocol_title(text: str) -> Optional[str]:
    """Extract the full protocol title."""
    t = _get_text(text)
    # Stop boundaries were `\n\s*(?:Investigational|...)` -- required a literal newline before
    # the next field's label. Real protocol cover pages routinely lose that newline in PDF text
    # extraction (the title and "Protocol Number:" end up flattened onto one line/run with just
    # whitespace between them), so the boundary never matched and the lazy capture ran on into
    # that field's own text. `\s*` (no `\n` required) stops correctly either way. Confirmed
    # directly against a real protocol where this was capturing well past "Protocol Number:".
    # Every capture group below is length-bounded (`.{1,300}?}` or similar) -- confirmed
    # directly against a real protocol that 3 of these previously used an *unbounded* lazy
    # `(.+?)` under `re.S` (DOTALL), stopped only by finding a boundary keyword
    # (Investigational/Sponsor/Protocol Number:/etc.) somewhere *later* in the text. When a
    # document's title isn't immediately followed by one of those words -- boundary is pages
    # away, or genuinely absent near the title -- the lazy match kept expanding across
    # paragraph/page breaks looking for it, capturing several pages of text as "the title"
    # instead of failing over to the next, safer pattern. A real title is never remotely close
    # to 300 chars, so bounding the quantifier can't lose a genuine match.
    patterns = [
        r'Protocol\s+[A-Z0-9\-]+\s*\n\s*(A\s+.{1,300}?)(?:\s*(?:Investigational|Sponsor|Protocol\s+Number\s*:|IND|EudraCT))',
        r'CLINICAL\s+PROTOCOL\s+[A-Z0-9\-]+\s*\n\s*(.{1,300}?)(?:\s*(?:Investigational|Confidential|Protocol\s+Number\s*:))',
        r'((?:A|An)\s+(?:Phase\s+\d|Open[\-\s]Label|Randomized|Multicenter|Single[\-\s]Arm).{20,200}?)'
        r'(?:\s*(?:Investigational|Protocol\s+Number\s*:|Sponsor|IND|EudraCT))',
        r'\n\s*([A-Z][A-Z\s,\(\)\d\/\-\:]{30,200}?)\s*(?:Investigational|Protocol\s+Number\s*:|Sponsor)',
        r'Protocol\s+Title\s*:\s*(.{1,300}?)(?:\s*(?:\n\s*\n|Protocol\s+Number\s*:))',
    ]
    for p in patterns:
        m = re.search(p, t, re.S)
        if m:
            v = m.group(1).strip()
            v = re.sub(r'Commented\s*\[[^\]]*\]', '', v)
            v = re.sub(r'\s+', ' ', v).strip()
            # Backstop against any future pattern with the same unbounded-capture shape: a
            # genuine title is never this long, so treat a runaway match as a non-match and
            # keep trying rather than returning several pages of text as "the title".
            if 20 < len(v) <= 400:
                return v
    return None


# ═══════════════════════════════════════════════════════════════
# PHASE
# ═══════════════════════════════════════════════════════════════

def phase(text: str) -> Optional[str]:
    """Extract study phase.
    
    Handles: Phase 3, Phase III, PHASE2, Study Phase: 1,
    and phase embedded in title: "A Phase 2, Randomized..."
    """
    t = _get_text(text)
    result = _try_patterns(t, [
        # Explicit labels
        r'(?:Development\s+)?Phase:\s*(\d+[A-Za-z]?)',
        r'Study\s+Phase\s*:\s*(\d+[A-Za-z]?)',
        # Phase from title (Arabic numerals)
        r'Phase\s+(\d+[A-Za-z]?)\s*(?:Study|Trial|Randomized|Open|Double|Multi|Single)',
        r'(?:A|An)\s+Phase\s+(\d+[A-Za-z]?\s*,)',
        # Roman numerals in title
        r'Phase\s+([IVXL]+)\s*(?:Study|Trial|Randomized)',
        r'(?:A|An)\s+Phase\s+([IVXL]+)\s*[,]?',
        # Generic phase mention
        r'Phase\s+(\d+[A-Za-z]?)\b',
        r'Phase\s+([IVXL]+)\b',
    ])
    if result:
        result = result.rstrip(',').strip()
        return result
    return None


# ═══════════════════════════════════════════════════════════════
# THERAPEUTIC AREA
# ═══════════════════════════════════════════════════════════════

def therapeutic_area(text: str) -> Optional[str]:
    """Extract therapeutic area."""
    t = _get_text(text)
    result = _try_patterns(t, [
        r'Therapeutic\s+Area\s*:\s*([^\n]+)',
        r'Therapeutic\s+Area\s*[\(\)]*\s*:\s*([^\n]+)',
        r'TA:\s*([^\n]+)',
    ])
    if result:
        result = re.sub(r'\s*\([^)]*\)', '', result).strip().rstrip('.')
        return result if len(result) > 3 else None
    
    # Fallback: check for condition/disease labels
    for p in [
        r'(?:Condition|Disease|Indication|Target\s+Condition)\s*(?:under\s+study)?\s*:\s*([^\n]{3,80})',
        r'(?:Primary\s+)?Diagnosis\s*:\s*([^\n]{3,80})',
    ]:
        m = re.search(p, t, re.I)
        if m:
            v = m.group(1).strip().strip('.').strip()
            if len(v) > 3:
                return v
    return None


# ═══════════════════════════════════════════════════════════════
# INDICATION
# ═══════════════════════════════════════════════════════════════

def indication(text: str) -> Optional[str]:
    """Extract disease indication."""
    t = _get_text(text)
    result = _try_patterns(t, [
        r'Indication:\s*(.+?)(?:\n|$)',
        r'Indication\s*[\(\)]*\s*:\s*(.+?)(?:\n|$)',
        r'(?:In|for)\s+Patients\s+With\s+(.+?)(?:\n\s*(?:Protocol|Investigational|Sponsor))',
    ])
    if not result:
        title = protocol_title(t)
        if title:
            m = re.search(r'in\s+Patients\s+with\s+(.+?)(?:\.|$)', title, re.I)
            if m:
                result = m.group(1).strip()
    if not result:
        m = re.search(r'Protocol\s+Title\s*:\s*(.+?)(?:\n|$)', t[:3000])
        if m:
            m2 = re.search(r'\bin\s+(.+?)\s*$', m.group(1))
            if m2:
                result = m2.group(1).strip().rstrip('.')
    if result:
        result = re.sub(r'\s+', ' ', result).strip().rstrip('.')
        return result
    return None


# ═══════════════════════════════════════════════════════════════
# ENROLLMENT
# ═══════════════════════════════════════════════════════════════

def enrollment(text: str) -> Optional[int]:
    """Extract total enrolled/randomized participants."""
    t = _get_text(text)
    text_window = t[:80000]  # 80K covers synopsis + sample size for most protocols
    patterns = [
        # "Approximately 240 patients" (with or without trailing verb)
        r'(?:Approximately|About|A total of|Estimated|Target|Planned|Total)\s+'
        r'([\d,]+)\s+(?:participants|patients|subjects)(?:\s+(?:will\s+be\s+)?(?:enrolled|randomized))?',
        # "N = 240" standalone
        r'(?:sample\s+size|enrollment|number\s+of\s+(?:participants|patients|subjects))\s*'
        r'(?::|is|will\s+be|=\s*approximately|of)?\s*([\d,]+)',
        r'(?:total\s+)?(?:sample\s+size|enrollment)\s*(?::|is|=|of)\s*'
        r'(?:approximately|about|of)?\s*([\d,]+)',
        r'\b[Nn]\s*[=:]\s*(\d{2,4})\b',
        r'(?:^|\s)(\d{3,4})\s+(?:patients|subjects|participants)\s+will\s+be\s+(?:randomized|enrolled)',
        r'total\s+of\s+(\d{3,4})\s+(?:patients|subjects|participants)',
    ]
    for p in patterns:
        m = re.search(p, text_window, re.I)
        if m:
            try:
                val = int(m.group(1).replace(',', ''))
                if 2 <= val <= 100000:
                    return val
            except ValueError:
                pass
    return None


# ═══════════════════════════════════════════════════════════════
# COUNTRIES — structured extraction from clinical design elements
# ═══════════════════════════════════════════════════════════════

_COUNTRY_INFO = {
    'US':              {'name': 'United States',   'abbreviation': 'US'},
    'Japan':           {'name': 'Japan',            'abbreviation': 'Japan'},
    'China':           {'name': 'China',            'abbreviation': 'China'},
    'Germany':         {'name': 'Germany',          'abbreviation': 'Germany'},
    'United Kingdom':  {'name': 'United Kingdom',   'abbreviation': 'UK'},
    'Italy':           {'name': 'Italy',            'abbreviation': 'Italy'},
    'Spain':           {'name': 'Spain',            'abbreviation': 'Spain'},
    'Korea':           {'name': 'South Korea',      'abbreviation': 'South Korea'},
    'Canada':          {'name': 'Canada',           'abbreviation': 'Canada'},
    'Mexico':          {'name': 'Mexico',           'abbreviation': 'Mexico'},
    'Poland':          {'name': 'Poland',           'abbreviation': 'Poland'},
    'Austria':         {'name': 'Austria',          'abbreviation': 'Austria'},
    'Belgium':         {'name': 'Belgium',          'abbreviation': 'Belgium'},
    'Bulgaria':        {'name': 'Bulgaria',         'abbreviation': 'Bulgaria'},
    'Croatia':         {'name': 'Croatia',          'abbreviation': 'Croatia'},
    'Cyprus':          {'name': 'Cyprus',           'abbreviation': 'Cyprus'},
    'Czech Republic':  {'name': 'Czech Republic',   'abbreviation': 'Czech Republic'},
    'Denmark':         {'name': 'Denmark',          'abbreviation': 'Denmark'},
    'Estonia':         {'name': 'Estonia',          'abbreviation': 'Estonia'},
    'Finland':         {'name': 'Finland',          'abbreviation': 'Finland'},
    'France':          {'name': 'France',           'abbreviation': 'France'},
    'Greece':          {'name': 'Greece',           'abbreviation': 'Greece'},
    'Hungary':         {'name': 'Hungary',          'abbreviation': 'Hungary'},
    'Ireland':         {'name': 'Ireland',          'abbreviation': 'Ireland'},
    'Latvia':          {'name': 'Latvia',           'abbreviation': 'Latvia'},
    'Lithuania':       {'name': 'Lithuania',        'abbreviation': 'Lithuania'},
    'Luxembourg':      {'name': 'Luxembourg',       'abbreviation': 'Luxembourg'},
    'Malta':           {'name': 'Malta',            'abbreviation': 'Malta'},
    'Netherlands':     {'name': 'Netherlands',      'abbreviation': 'Netherlands'},
    'Portugal':        {'name': 'Portugal',         'abbreviation': 'Portugal'},
    'Romania':         {'name': 'Romania',          'abbreviation': 'Romania'},
    'Slovakia':        {'name': 'Slovakia',         'abbreviation': 'Slovakia'},
    'Slovenia':        {'name': 'Slovenia',         'abbreviation': 'Slovenia'},
    'Sweden':          {'name': 'Sweden',           'abbreviation': 'Sweden'},
    'Norway':          {'name': 'Norway',           'abbreviation': 'Norway'},
    'Australia':       {'name': 'Australia',        'abbreviation': 'Australia'},
    'Brazil':          {'name': 'Brazil',           'abbreviation': 'Brazil'},
    'India':           {'name': 'India',            'abbreviation': 'India'},
    'Switzerland':     {'name': 'Switzerland',      'abbreviation': 'Switzerland'},
    'Russia':          {'name': 'Russia',           'abbreviation': 'Russia'},
    'Turkey':          {'name': 'Turkey',           'abbreviation': 'Turkey'},
    'Argentina':       {'name': 'Argentina',        'abbreviation': 'Argentina'},
    'South Africa':    {'name': 'South Africa',     'abbreviation': 'South Africa'},
}

_COUNTRY_ALIASES = {
    'US': 'US', 'USA': 'US', 'U.S.': 'US', 'United States': 'US',
    'Japan': 'Japan', 'JP': 'Japan',
    'China': 'China', 'CN': 'China',
    'Germany': 'Germany', 'DE': 'Germany',
    'United Kingdom': 'United Kingdom', 'UK': 'United Kingdom', 'GB': 'United Kingdom',
    'Italy': 'Italy', 'IT': 'Italy',
    'Spain': 'Spain', 'ES': 'Spain',
    'Korea': 'Korea', 'South Korea': 'Korea', 'S. Korea': 'Korea', 'KR': 'Korea',
    'Canada': 'Canada', 'CA': 'Canada',
    'Mexico': 'Mexico', 'MX': 'Mexico',
    'Poland': 'Poland', 'PL': 'Poland',
    'Austria': 'Austria', 'AT': 'Austria',
    'Belgium': 'Belgium', 'BE': 'Belgium',
    'Bulgaria': 'Bulgaria', 'BG': 'Bulgaria',
    'Croatia': 'Croatia', 'HR': 'Croatia',
    'Cyprus': 'Cyprus', 'CY': 'Cyprus',
    'Czech Republic': 'Czech Republic', 'CZ': 'Czech Republic', 'Czechia': 'Czech Republic',
    'Denmark': 'Denmark', 'DK': 'Denmark',
    'Estonia': 'Estonia', 'EE': 'Estonia',
    'Finland': 'Finland', 'FI': 'Finland',
    'France': 'France', 'FR': 'France',
    'Greece': 'Greece', 'GR': 'Greece',
    'Hungary': 'Hungary', 'HU': 'Hungary',
    'Ireland': 'Ireland', 'IE': 'Ireland',
    'Latvia': 'Latvia', 'LV': 'Latvia',
    'Lithuania': 'Lithuania', 'LT': 'Lithuania',
    'Luxembourg': 'Luxembourg', 'LU': 'Luxembourg',
    'Malta': 'Malta', 'MT': 'Malta',
    'Netherlands': 'Netherlands', 'NL': 'Netherlands', 'Holland': 'Netherlands',
    'Portugal': 'Portugal', 'PT': 'Portugal',
    'Romania': 'Romania', 'RO': 'Romania',
    'Slovakia': 'Slovakia', 'SK': 'Slovakia',
    'Slovenia': 'Slovenia', 'SI': 'Slovenia',
    'Sweden': 'Sweden', 'SE': 'Sweden',
    'Norway': 'Norway', 'NO': 'Norway',
    'Australia': 'Australia', 'AU': 'Australia',
    'Brazil': 'Brazil', 'BR': 'Brazil',
    'India': 'India', 'IN': 'India',
    'Switzerland': 'Switzerland', 'CH': 'Switzerland',
    'Russia': 'Russia', 'RU': 'Russia',
    'Turkey': 'Turkey', 'TR': 'Turkey',
    'Argentina': 'Argentina', 'AR': 'Argentina',
    'South Africa': 'South Africa', 'ZA': 'South Africa',
}



# ═══════════════════════════════════════════════════════════════
# COUNTRY_LANG — maps country abbreviation → native language(s)
# Used by Rule 3 to tick translation checkboxes.
# All values must match the 44-language schema enum in
# extract-config.json → data_schema.properties.translations.items.enum
# ═══════════════════════════════════════════════════════════════

COUNTRY_LANG = {
    # Americas
    'US': ['English'],
    'Canada': ['English', 'French'],
    'Mexico': ['Spanish (Latin America)'],
    'Argentina': ['Spanish (Latin America)'],
    'Brazil': ['Portuguese (Brazil)'],
    # Europe — Western
    'UK': ['English'],
    'Ireland': ['English'],
    'Germany': ['German'],
    'Austria': ['German'],
    'Switzerland': ['Swiss German', 'French', 'Italian'],
    'France': ['French'],
    'Belgium': ['Dutch', 'French'],
    'Luxembourg': ['French', 'German'],
    'Netherlands': ['Dutch'],
    # Europe — Southern
    'Italy': ['Italian'],
    'Spain': ['Spanish (EU)'],
    'Portugal': ['Portuguese (EU)'],
    'Greece': ['Greek'],
    'Malta': ['English'],
    'Cyprus': ['Greek'],
    # Europe — Nordic
    'Denmark': ['Danish'],
    'Sweden': ['Swedish'],
    'Finland': ['Finnish'],
    'Norway': ['Norwegian'],
    'Estonia': ['Estonian'],
    'Latvia': ['Latvian'],
    'Lithuania': ['Lithuanian'],
    # Europe — Central & Eastern
    'Poland': ['Polish'],
    'Czech Republic': ['Czech'],
    'Slovakia': ['Slovakian'],
    'Hungary': ['Hungarian'],
    'Romania': ['Romanian'],
    'Bulgaria': ['Bulgarian'],
    'Croatia': ['Croatian'],
    'Slovenia': ['Slovenian'],
    'Turkey': ['Turkish'],
    'Russia': ['Russian'],
    # Asia-Pacific
    'Japan': ['Japanese'],
    'China': ['Chinese'],
    'South Korea': ['Korean'],
    'India': ['Hindi', 'English'],
    'Australia': ['English'],
    # Africa
    'South Africa': ['Afrikaans', 'English'],
}


def parse_country_allocation(text: str) -> List[dict]:
    """Extract structured country allocation from clinical design elements.

    Four content-based phases (no line-number assumptions):

    Phase 1 — Find in-scope countries from the sentence
              "... X, Y, and Z will be in scope for country allocation"
    Phase 2 — Find consideration countries from
              "consideration can be given to A, B, C, and D"
    Phase 3 — Find the Country Check section ("Based on DSB countries in scope
              include") with per-country target percentages and IBU lines.
    Phase 4 — Fallback: keyword scan of design text only.

    Returns a list of dicts sorted in-scope first, then consideration:
        name: str          canonical country name (e.g. 'United States')
        abbreviation: str  short form for table headers / COUNTRY_LANG key
        pct: float | None  target enrollment fraction (e.g. 0.10 for 10 %)
        consideration: bool True if IBU consideration only
    """
    t = _get_text(text)
    results: dict[str, dict] = {}       # abbreviation -> entry

    def _set(abbr, *, pct=None, consideration=False):
        info = _COUNTRY_INFO.get(abbr, {})
        entry = results.get(abbr)
        if entry is None:
            results[abbr] = {
                'name': info.get('name', abbr),
                'abbreviation': info.get('abbreviation', abbr),
                'pct': pct,
                'consideration': consideration,
            }
        else:
            if pct is not None and entry['pct'] is None:
                entry['pct'] = pct
            if consideration:
                entry['consideration'] = True

    def _find_abbrs(fragment: str) -> list[str]:
        found = []
        for alias, abbr in _COUNTRY_ALIASES.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', fragment):
                if abbr not in found:
                    found.append(abbr)
        return found

    # ---- Phase 1: in-scope sentence ----
    m = re.search(
        r'(?:will be in scope|countries in scope include|in scope for country allocation)[^.]*\.',
        t, re.I)
    if m:
        for abbr in _find_abbrs(m.group(0)):
            _set(abbr)

    # ---- Phase 2: consideration sentence ----
    m = re.search(r'consideration can be given to ([^.;]+)', t, re.I)
    if m:
        for abbr in _find_abbrs(m.group(1)):
            _set(abbr, consideration=True)

    # ---- Phase 3: Country Check section (percentages + IBU) ----
    cc_start = re.search(r'Based on DSB countries in scope include', t, re.I)
    if cc_start:
        chunk = t[cc_start.start():cc_start.start() + 3000]
        for m in re.finditer(r'target\s*(\d+)%', chunk, re.I):
            line_start = chunk.rfind('\n', 0, m.start())
            line = chunk[line_start if line_start != -1 else 0:m.end()]
            for abbr in _find_abbrs(line):
                _set(abbr, pct=int(m.group(1)) / 100.0)
        # Add countries not already found (e.g. US without percentage)
        for alias, abbr in _COUNTRY_ALIASES.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', chunk):
                if abbr not in results:
                    _set(abbr)
        # Mark IBU-line countries as consideration
        ibu = re.search(r'Other IBU[^.]*', chunk, re.I)
        if ibu:
            for abbr in _find_abbrs(ibu.group(0)):
                if abbr in results:
                    results[abbr]['consideration'] = True

    # ---- Phase 4: fallback keyword scan (only if nothing found above) ----
    if not results:
        for alias, abbr in _COUNTRY_ALIASES.items():
            if re.search(r'\b' + re.escape(alias) + r'\b', t):
                _set(abbr)

    return sorted(results.values(), key=lambda r: (r['consideration'], r['name']))


def countries(text: str) -> List[str]:
    """Backward-compat: return flat list of canonical country names."""
    return [c['name'] for c in parse_country_allocation(text)]


def extract_country_check_row(design_text: str, existing_countries: Optional[list] = None) -> List[dict]:
    """Countries come ONLY from column 4 (0-indexed 3) of the design document's own "Country
    Check" row -- not from any other line that merely mentions a country name (site lists,
    language/translation notes, amendment history, etc. routinely name countries without being
    the actual allocation decision), and not from that row's OWN other columns either (confirmed
    directly against a real design-elements document: columns 1-3 of that row hold the field
    label plus instructional/example text, e.g. "e.g. United States, Canada" -- scanning the
    whole row after the label, not just column 4 specifically, picked up that example text as if
    it were the real answer).

    This is the single source of truth for country allocation across both
    preparsed_schema_extractor.py and populate_rfp.py's own DESIGN_DATA fallback -- do NOT seed
    `existing_countries` from parse_country_allocation() or DesignParser's country_allocation
    (both scan much wider spans of text and over-include); leave it empty (the default) unless
    there's a genuinely trusted prior source to merge with."""
    out = [dict(c) for c in (existing_countries or []) if isinstance(c, dict)]
    by_abbr = {}
    for c in out:
        key = str(c.get("abbreviation") or c.get("name") or "").strip().lower()
        if key:
            by_abbr[key] = c

    if not design_text:
        return out

    alias_items = sorted(_COUNTRY_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)

    def _alias_matches(fragment: str, alias: str) -> bool:
        pattern = r"\b" + re.escape(alias) + r"\b"
        if len(alias) <= 3 and alias.upper() == alias:
            return bool(re.search(pattern, fragment))
        return bool(re.search(pattern, fragment, re.I))

    trigger = re.compile(r"country\s*check", re.I)
    # Second, narrower trigger: some real Design Elements documents (a "Design Element" / "Design
    # Detail" two-column form, not a "Country Check" row at all -- confirmed directly against a
    # real document with zero "Country Check" occurrences anywhere) instead state the allocation
    # decision as a plain sentence, e.g. "For this trial, US, Japan, and China will be in scope
    # for country allocation." Deliberately kept as specific a phrase as "Country Check" itself
    # (not just any country mention) so this doesn't reopen the earlier over-inclusion bug (see
    # this function's own docstring) -- it only fires on this exact "in scope for ... country
    # allocation" wording, not e.g. a site list or a translation/amendment note that happens to
    # name a country.
    prose_trigger = re.compile(r"in scope for (?:the )?country allocation", re.I)

    def _record(value_text: str, status: str) -> None:
        for alias, abbr in alias_items:
            if not _alias_matches(value_text, alias):
                continue

            info = _COUNTRY_INFO.get(abbr, {})
            name = str(info.get("name") or abbr).strip()
            canonical_abbr = str(info.get("abbreviation") or abbr).strip()
            key = canonical_abbr.lower()

            existing = by_abbr.get(key)
            if existing:
                if existing.get("status") != "confirmed" and status == "confirmed":
                    existing["status"] = "confirmed"
                continue

            entry = {
                "name": name,
                "abbreviation": canonical_abbr,
                "pct": None,
                "status": status,
            }
            out.append(entry)
            by_abbr[key] = entry

    for line in design_text.splitlines():
        m = trigger.search(line)
        if m:
            # This row's cells, table-row-flattened to "| a | b | c | d |" (a real pipe-table row)
            # or " a | b | c | d " (the plain-text-mangled shape -- see design_parser.py's own
            # _dematerialize_table_row for why both shapes exist) -- either way, split on "|" and
            # take column 4 specifically. Columns 1-3 hold the label plus instructional/example
            # text for this field, not the real answer.
            cells = [c.strip() for c in line.split("|")]
            if cells and cells[0] == "":
                cells = cells[1:]
            if cells and cells[-1] == "":
                cells = cells[:-1]
            value_text = cells[3] if len(cells) >= 4 else line[m.end():]

            status = "under_consideration"
            if re.search(r"will be in scope|countries in scope include|in scope for country allocation|\bin scope\b", value_text, re.I):
                status = "confirmed"
            if re.search(r"consideration|recommended to consider|for consideration|not required|utilized", value_text, re.I):
                status = "under_consideration"

            _record(value_text, status)
            continue

        if prose_trigger.search(line):
            # No column structure here -- the country names sit in the same plain sentence as
            # the trigger phrase itself, so the whole line is the value text. Finding this exact
            # phrase already means "in scope", so status is always confirmed (matches how the
            # Country Check row's own "will be in scope"/"in scope for country allocation"
            # wording is classified above).
            _record(line, "confirmed")

    if out:
        return out

    # Fallback tier: the strict "Country Check" row / "in scope for country allocation" phrase
    # found nothing at all, which most often means this design document isn't the standard CDE
    # template. Rather than leave country allocation empty, reuse design_parser's already-tested,
    # broader prose-based detection (percent targets, "will be in scope", IBU consideration,
    # "-only"/"sited in X" phrasing, etc.) applied to the whole document as one pseudo-section.
    # This only ever engages when the strict path above returned nothing -- it never overrides or
    # loosens the strict path's own results. Built directly from the already-resolved
    # name/abbreviation/pct on each CountryEntry rather than round-tripping through _record's
    # own alias re-matching, since these values are already canonical.
    from engine.design_parser import Section, extract_country_allocation_section
    fallback_section = Section(number='', title='', text=design_text)
    fallback = extract_country_allocation_section(fallback_section)
    for entry in fallback.countries:
        key = entry.abbreviation.lower()
        if key in by_abbr:
            continue
        record = {
            "name": entry.name,
            "abbreviation": entry.abbreviation,
            "pct": entry.pct,
            "status": "under_consideration" if entry.consideration else "confirmed",
        }
        out.append(record)
        by_abbr[key] = record

    return out


# ═══════════════════════════════════════════════════════════════
# MILESTONE DATES — from clinical design elements Section 45 Timeline
# ═══════════════════════════════════════════════════════════════

_MILESTONE_KEYS = [
    'Protocol Approval', 'FPV', 'LPV', 'FPET', 'LPET',
    'Protocol Content Lock', 'Design Element Alignment',
]

# Additional phrasings for the same milestone, used only once the strict pass (exact label,
# anchored to a "Timeline"/"Document Key Milestone Estimates" section) has already run and come
# up empty for a given key -- a document that describes the same facts without ever using the
# word "Timeline", or that spells FPV/LPV out in full, still resolves to the same dict key.
_MILESTONE_DATE_RE = re.compile(
    r'\d{1,2}[-\s](?:January|February|March|April|May|June|July|August|September|October|'
    r'November|December)[-\s]\d{4}'
    r'|(?:January|February|March|April|May|June|July|August|September|October|November|'
    r'December)\s+\d{4}'
    r'|\d{1,2}/\d{1,2}/\d{2,4}'
    r'|\d{4}-\d{2}-\d{2}',
    re.I,
)

_MILESTONE_SYNONYMS = {
    'Protocol Approval': ['Protocol Approval Date', 'PA Date'],
    'FPV': ['First Patient Visit', 'First Patient Enrolled', 'First Patient In',
             'First Patient Dosed', 'First Subject In', 'FPI', 'FSI'],
    'LPV': ['Last Patient Visit', 'Last Patient Completed', 'Last Patient Out',
             'Last Subject Out', 'LPO', 'LSO'],
    'FPET': ['First Patient Entered Treatment', 'First Patient Treated'],
    'LPET': ['Last Patient Entered Treatment', 'Last Patient Treated'],
}


def extract_milestone_dates(text: str) -> dict[str, str]:
    """Extract milestone -> date mappings from clinical design elements.

    Strict pass first: exact label, anchored inside a "Timeline"/"Document Key Milestone
    Estimates" section -- this is the proven, regression-tested path for the standard CDE
    template and always wins when it finds something. Any milestone still missing after that
    falls back to a whole-document scan using broader label synonyms (_MILESTONE_SYNONYMS), so a
    differently-worded design document isn't left empty just because it doesn't use this
    template's exact section header or abbreviations.

    Returns a dict like:
        {'Protocol Approval': '31-July-2026', 'FPV': 'December-2026', ...}
    """
    t = _get_text(text)
    milestones: dict[str, str] = {}

    start = t.lower().find('document key milestone estimates')
    if start < 0:
        start = t.lower().find('timeline')
    if start >= 0:
        chunk = t[start:start + 2000]
        for key in _MILESTONE_KEYS:
            m = re.search(re.escape(key) + r':\s*([^\n•]+)', chunk)
            if m:
                val = m.group(1).strip()
                if val:
                    milestones[key] = val

    for key in _MILESTONE_KEYS:
        if key in milestones:
            continue
        for label in [key] + _MILESTONE_SYNONYMS.get(key, []):
            m = re.search(re.escape(label) + r'\s*:\s*([^\n•]+)', t, re.I)
            if m:
                val = m.group(1).strip()
                if val:
                    milestones[key] = val
                break

    # Third tier: plain prose that states the date first and names the milestone parenthetically
    # afterward, e.g. "the first patient is expected to be dosed in March 2027 (FPV)" -- no
    # "label: value" shape at all. Only tried for milestones still missing after both tiers above.
    # Only the short abbreviation (the actual key, not a spelled-out synonym) is searched for in
    # parentheses, since that's the realistic parenthetical-gloss shape; the date is taken from
    # the nearest date-like expression in the sentence immediately preceding the parenthetical.
    for key in _MILESTONE_KEYS:
        if key in milestones:
            continue
        for m in re.finditer(r'\(\s*' + re.escape(key) + r'\s*\)', t, re.I):
            window_start = max(0, m.start() - 120)
            window = t[window_start:m.start()]
            date_m = None
            for date_m in re.finditer(_MILESTONE_DATE_RE, window):
                pass  # keep the last (closest-to-the-parenthetical) match
            if date_m:
                milestones[key] = date_m.group(0).strip()
                break

    return milestones


# ═══════════════════════════════════════════════════════════════
# MIN AGE
# ═══════════════════════════════════════════════════════════════

def min_age(text: str, section_text: str | None = None) -> Optional[int]:
    """Extract minimum participant age.

    If section_text is provided, search only in that scoped section
    (e.g., Section 1.1 Study Population) rather than the full text.
    """
    t = _get_text(section_text if section_text else text)
    patterns = [
        r'(?:must\s+be|are|aged?)\s+(?:at\s+least|>=|≥)\s*(\d{1,2})\s+years?\s+of\s+age',
        r'(?:must\s+be|are|aged?)\s+(?:at\s+least|>=|≥)\s*(\d{1,2})\s+years?',
        r'aged?\s+(\d{1,2})\s*(?:to|through|\-)\s*\d{1,2}\s*years?',
        r'(\d{1,2})\s+years?\s+of\s+age\s+(?:or\s+)?older',
        r'Age\s*(?:at\s+)?(?:screening|enrollment|inclusion)\s*(?:>=|is|:)?\s*(\d{1,2})',
        r'inclusion\s+criterion\s*(?::|is|#)\s*(\d{1,2})\s*years',
        r'(?:>=|≥)\s*(\d{1,2})\s+years',
        r'(?:Age|aged?)\s*(?:>=|≥|=)\s*(\d{1,2})',
        r'(?:Inclusion\s+Criteria|Criteria|Eligibility).{0,200}?'
        r'(?:(?:at\s+least|>|=)\s*(\d{1,2})\s*(?:years?|yrs?))',
        # "Age X to Y" format in inclusion criteria
        r'(?:Inclusion|Criteria|Age).{0,50}?[Aa]ge[\s:]+(\d{1,2})\s*(?:to|through|-)\s*\d{1,2}\s*(?:years?|yrs?)?',
    ]
    for p in patterns:
        m = re.search(p, t, re.I)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None


# ═══════════════════════════════════════════════════════════════
# ONCOLOGY / IMMUNOGENICITY / GENETICS / ETC
# ═══════════════════════════════════════════════════════════════

def is_oncology(text: str, ta: Optional[str] = None) -> bool:
    """Determine if study is oncology based on keywords."""
    t = _get_text(text)
    combined = f"{ta or ''} {t}".lower()
    keywords = ['oncolog', 'cancer', 'tumou?r', 'malignan', 'carcinoma',
                'metastatic', 'neoplasm', 'sarcoma', 'lymphoma', 'leukemia',
                'myeloma', 'glioblastoma', 'melanoma']
    return any(re.search(k, combined) for k in keywords)


def has_ici(text: str) -> bool:
    """Detect if study involves immune checkpoint inhibitors."""
    t = _get_text(text)
    ici_keywords = [
        'immune checkpoint inhibitor', 'checkpoint inhibitor', 'anti-?PD-?L?1',
        'anti-?PD-?1', 'anti-?CTLA-?4', 'pembrolizumab', 'nivolumab',
        'atezolizumab', 'durvalumab', 'ipilimumab', 'avelumab', 'cemiplimab',
        'tremelimumab', 'dostarlimab',
    ]
    return any(re.search(k, t, re.I) for k in ici_keywords)


def immunogenicity(text: str) -> Optional[str]:
    """Extract immunogenicity testing requirement."""
    t = _get_text(text)
    patterns = [
        r'Is?\s+immunogenicity\s+testing\s+needed\s*\??\s*(Yes|No)',
        r'immunogenicity\s*(?:testing|assessment)?\s*(?::|is|=)\s*(Yes|No)',
        r'immunogenicity\s*(?:testing|assessment)?\s*(?::|will\s+be\s+)?(performed|required|needed)',
    ]
    for p in patterns:
        m = re.search(p, t, re.I)
        if m:
            v = m.group(1).strip().capitalize()
            if v in ('Yes', 'No', 'Performed', 'Required', 'Needed'):
                return 'Yes' if v != 'No' else 'No'
    return None


def genetics_pgx(text: str) -> Optional[str]:
    """Extract genetics/PGx sample collection requirement."""
    t = _get_text(text)
    patterns = [
        r'(collect|will\s+collect)\s+(?:Genetics|PGx|pharmacogenomic)\s*(Yes|No)?',
        r'(?:Genetics|PGx|pharmacogenomic)\s+(?:samples?\s+)?(?:collected|required)?\s*(?::|is|=)\s*(Yes|No)',
        r'Genetics/PGx\s*sample\s*collected\s*(Yes|No)',
    ]
    for p in patterns:
        m = re.search(p, t, re.I)
        if m:
            v = m.group(m.lastindex).strip().capitalize() if m.lastindex else 'Yes'
            if v in ('Yes', 'No'):
                return v
    if re.search(r'genetics|pgx|pharmacogenom', t, re.I):
        if re.search(r'no\s+(genetics|pgx)', t, re.I):
            return 'No'
        return 'Yes'
    return None


def screen_fail_rate(text: str) -> float:
    """Extract or estimate screen failure rate."""
    m = re.search(r'(\d{1,3})\s*%\s*(?:screen\s+)?(?:fail(?:ure)?)?\s*(?:rate)?', _get_text(text), re.I)
    if m:
        try:
            return int(m.group(1)) / 100
        except ValueError:
            pass
    return 0.30


def ed_rate() -> float:
    """Early discontinuation rate (default assumption)."""
    return 0.10


def analyze_appendix_2(text: str) -> Optional[list]:
    """Find and extract the clinical laboratory tests appendix."""
    patterns = [
        r'Appendix\s*2\s*:?\s*Clinical\s+Laboratory\s+Tests\s*\n\s*(?:The\s+tests\s+detailed|The\s+following)',
        r'Clinical\s+Laboratory\s+Tests\s*\n\s*(?:The\s+tests\s+detailed|The\s+following)',
        r'Appendix\s*\d+\s*:?\s*Clinical\s+Laboratory',
    ]
    for p in patterns:
        m = re.search(p, _get_text(text))
        if m:
            start = m.start()
            nxt = re.search(
                r'\n\s*(?:Appendix\s+[3-9]|\d+\.\d+\s+|References?\s*\n)',
                _get_text(text)[start+50:]
            )
            block = _get_text(text)[start:start+50+nxt.start()] if nxt else _get_text(text)[start:start+5000]
            lines = [l.strip() for l in block.split('\n') if l.strip()]
            lines = [l for l in lines if not re.search(
                r'^(CONFIDENTIAL|Approved on|Page\s+\d|Commented\s*\[|Author and Content|\d{1,3}\s*$)', l
            )]
            if lines:
                return lines
    return None


# ═══════════════════════════════════════════════════════════════
# General helpers (shared by populate_rfp and tests)
# ═══════════════════════════════════════════════════════════════

REVIEW = lambda field, why='not found': f'‹REVIEW:{field} — {why}›'


def parse_manual_countries(raw: str) -> list[dict] | None:
    if not raw:
        return None
    results = []
    for part in raw.split(','):
        part = part.strip()
        if not part:
            continue
        pct = None
        consideration = False
        m_pct = re.search(r'\((\d+(?:\.\d+)?)\s*%\)', part)
        if m_pct:
            pct = float(m_pct.group(1)) / 100.0
        if re.search(r'consideration', part, re.I):
            consideration = True
        name = re.sub(r'\s*\(.*\)\s*', '', part).strip()
        if name:
            results.append({
                'name': name,
                'abbreviation': name,
                'pct': pct,
                'consideration': consideration,
            })
    return results if results else None
