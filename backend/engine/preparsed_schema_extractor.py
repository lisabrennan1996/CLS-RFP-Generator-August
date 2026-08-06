#!/usr/bin/env python3
"""Pre-parsed schema-driven extractor for RFP v4.

This module consumes already-parsed protocol/design content (markdown/json/html)
and resolves fields defined in extraction_schema_v4.json.

Current mode is text-first: table-derived fields (SoA and analytes appendix)
are optional and expected to come from separate inputs unless explicitly
enabled.

Key defaults requested for this implementation:
- Country where initial FPV planned: default to "US" when unresolved
- Screen fail rate: fallback to 30% when not present in source text
"""

from __future__ import annotations

import argparse
import datetime as dt
import html as html_lib
import json
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path

from design_parser import DesignParser, therapeutic_area_by_keyword
from extractors import (
    _COUNTRY_ALIASES,
    _COUNTRY_INFO,
    COUNTRY_LANG,
    compound,
    enrollment,
    extract_milestone_dates,
    genetics_pgx,
    immunogenicity,
    is_oncology,
    extract_country_check_row,
    min_age,
    phase,
    protocol_number,
    protocol_title,
    therapeutic_area,
)
from md_table import (
    extract_section,
    find_heading_by_number,
    find_pipe_tables,
    is_visit_column,
    parse_analytes_text,
    parse_pipe_table,
    parse_soa_text,
)


DEFAULT_SCHEMA_PATH = Path(r"C:\Users\L047081\OneDrive - Eli Lilly and Company\extraction_schema_v4.json")


def _strip_tags(text: str) -> str:
    text = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"(?i)</tr\s*>", "\n", text)
    text = re.sub(r"(?i)</t[dh]\s*>", " | ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_html(text: str) -> bool:
    sample = (text or "").lstrip()[:512].lower()
    return "<html" in sample or "<body" in sample or "<table" in sample or bool(re.search(r"<\w+[^>]*>", sample))


def _html_tables_to_markdown(html_text: str) -> list[str]:
    tables = []
    for table_block in re.findall(r"(?is)<table[^>]*>(.*?)</table>", html_text):
        rows = []
        for tr in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table_block):
            cells = re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)
            cleaned = []
            for cell in cells:
                cell_text = _strip_tags(cell)
                cell_text = re.sub(r"\s+", " ", cell_text).strip()
                cleaned.append(cell_text)
            if cleaned:
                rows.append(cleaned)
        if len(rows) < 2:
            continue
        width = max(len(r) for r in rows)
        padded = [r + [""] * (width - len(r)) for r in rows]
        header = "| " + " | ".join(padded[0]) + " |"
        sep = "| " + " | ".join(["---"] * width) + " |"
        body = ["| " + " | ".join(r) + " |" for r in padded[1:]]
        tables.append("\n".join([header, sep] + body))
    return tables


def _normalize_html_content(content: str) -> str:
    text = _strip_tags(content)
    tables = _html_tables_to_markdown(content)
    if tables:
        return text + "\n\n" + "\n\n".join(tables)
    return text


def _flatten_json_payload(payload) -> str:
    fragments: list[str] = []
    seen: set[str] = set()

    def _push(value: str, html_hint: bool = False) -> None:
        if not isinstance(value, str):
            return
        norm = _normalize_html_content(value) if html_hint or _looks_like_html(value) else value
        norm = norm.strip()
        if not norm or norm in seen:
            return
        seen.add(norm)
        fragments.append(norm)

    def _walk(node, parent_key: str = "") -> None:
        if isinstance(node, str):
            _push(node, html_hint=(parent_key.lower() in {"html", "html_content"}))
            return
        if isinstance(node, list):
            for item in node:
                _walk(item, parent_key)
            return
        if not isinstance(node, dict):
            return

        preferred = (
            "markdown",
            "md",
            "text",
            "content",
            "protocol_text",
            "design_text",
            "body",
            "html",
            "html_content",
            "pages",
        )
        for key in preferred:
            if key in node:
                _walk(node[key], key)
        for key, value in node.items():
            if key in preferred:
                continue
            _walk(value, key)

    _walk(payload)
    return "\n\n".join(fragments).strip()


def normalize_content(content: str, content_format: str = "markdown") -> str:
    fmt = (content_format or "markdown").strip().lower()
    raw = content or ""

    if fmt in {"markdown", "md", "text", "txt"}:
        return raw
    if fmt == "html":
        return _normalize_html_content(raw)
    if fmt == "json":
        try:
            parsed = json.loads(raw)
            return _flatten_json_payload(parsed) or raw
        except json.JSONDecodeError:
            return raw

    stripped = raw.lstrip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(raw)
            return _flatten_json_payload(parsed) or raw
        except json.JSONDecodeError:
            pass
    if _looks_like_html(raw):
        return _normalize_html_content(raw)
    return raw


def _strip_json_suffix(text: str) -> str:
    lines = (text or "").splitlines()
    for i in range(len(lines) - 1, max(5, len(lines) - 5000), -1):
        if lines[i].strip() == "{":
            remaining = "\n".join(lines[i : i + 3])
            if '"pages"' in remaining:
                return "\n".join(lines[:i])
    return text


def _best_pipe_table(section_text: str, table_type: str) -> tuple[list[str], list[list[str]]]:
    best_headers: list[str] = []
    best_rows: list[list[str]] = []
    best_score = -1

    for lines in find_pipe_tables(section_text or ""):
        headers, rows = parse_pipe_table(lines)
        if not headers or not rows:
            continue

        h0 = headers[0].strip().lower() if headers else ""
        h_all = [h.strip().lower() for h in headers]
        score = len(rows)

        if table_type == "soa":
            if any("visit" in h for h in h_all):
                score += 25
            if any(tok in h0 for tok in ("procedure", "assessment", "test", "laboratory")):
                score += 12
            if len(headers) >= 4:
                score += 8
        else:
            if any("clinical" in h and "lab" in h for h in h_all):
                score += 25
            if any(tok in h0 for tok in ("test", "analyte", "laboratory")):
                score += 12
            if any("comment" in h for h in h_all):
                score += 8

        if score > best_score:
            best_score = score
            best_headers, best_rows = headers, rows

    return best_headers, best_rows


def _extract_section(text: str, number: str | int) -> str:
    heading = find_heading_by_number(text, number)
    if not heading:
        return ""
    return extract_section(text, heading[0], heading[1], heading[2])


def _extract_soa_table(protocol_text: str) -> tuple[list[str], list[list[str]]]:
    section = _extract_section(protocol_text, "1.3")
    if not section:
        section = _extract_section(protocol_text, 1) or protocol_text

    headers, rows = _best_pipe_table(section, "soa")
    if headers and rows:
        return headers, rows

    fallback_headers, fallback_rows, _footnotes = parse_soa_text(section)
    return fallback_headers or [], fallback_rows or []


def _extract_lab_rows(protocol_text: str) -> list[tuple[str, str]]:
    section = _extract_section(protocol_text, "10.2")
    if not section:
        section = _extract_section(protocol_text, 10) or protocol_text

    headers, rows = _best_pipe_table(section, "lab")
    if headers and rows:
        out = []
        for row in rows:
            name = row[0].strip() if row else ""
            note = row[1].strip() if len(row) > 1 else ""
            if name:
                out.append((name, note))
        if out:
            return out

    return parse_analytes_text(section)


def _find_analytes_not_reported(lab_rows: list[tuple[str, str]], phrases: list[str]) -> list[str]:
    out = []
    for analyte, note in lab_rows:
        note_l = (note or "").lower()
        if any(p.lower() in note_l for p in phrases):
            if analyte not in out:
                out.append(analyte)
    return out


def _compute_reflex_optional(protocol_text: str) -> dict:
    text_l = (protocol_text or "").lower()
    rowmap = {
        "pregnancy": ("50% of pts", ["additional pregnancy tests", "clinical suspicion of pregnancy"]),
        "ck-mb": (None, ["ck-mb"]),
        "uds": ("Yes - assume 2%", ["urine drug confirmation", "drug screen is positive", "drug confirmation to"]),
        "hbv dna": ("Yes - 1%", ["hbv dna"]),
        "hcv rna": ("Yes - 1%", ["hcv rna"]),
        "hiv": ("Yes - 1%", ["reflex to viral load"]),
        "fsh": ("Yes - 50%", ["postmenopausal", "follicle-stimulating hormone"]),
    }
    result = {}
    for key, (default_value, keywords) in rowmap.items():
        if default_value and any(k in text_l for k in keywords):
            result[key] = default_value
        else:
            result[key] = "No"
    result["other"] = (
        "MMA reflexed if B12 below central lab reference range; "
        "ANA reflex to titre and pattern if positive"
    )
    return result


def _extract_screen_fail_rate(design_text: str) -> float | None:
    m = re.search(
        r"(?:screen[\s-]*fail(?:ure)?(?:\s*rate)?|screening[\s-]*failure(?:\s*rate)?)\s*(?::|=|of)?\s*(\d{1,3})\s*%",
        design_text,
        re.I,
    )
    if not m:
        return None
    val = int(m.group(1))
    if 0 <= val <= 95:
        return val / 100.0
    return None


def _extract_enrollment_highest(design_text: str, design_planned: int | None) -> int | None:
    strong_candidates = []
    weak_candidates = []

    def _push(values: list[int], out: list[int]) -> None:
        for v in values:
            if 10 <= v <= 100000 and not (1900 <= v <= 2100):
                out.append(v)

    if isinstance(design_planned, int) and design_planned > 0:
        strong_candidates.append(design_planned)

    relevant_line = re.compile(
        r"\benroll(?:ed|ment|ing)?\b|\brandomiz(?:ed|ation|e)?\b|"
        r"sample\s*size|participants?\b|subjects?\b|patients?\b|"
        r"\bn\s*=|ceiling|up to|max(?:imum)?",
        re.I,
    )
    standalone_num = re.compile(r"(?<![A-Za-z0-9-])(\d{2,5})(?![A-Za-z0-9-])")

    for line in design_text.splitlines():
        if not relevant_line.search(line):
            continue

        vals = []
        for m in re.finditer(r"(?<![A-Za-z0-9])(\d{2,5})\s*(?:-|to|–)\s*(\d{2,5})(?![A-Za-z0-9])", line, re.I):
            vals.extend([int(m.group(1)), int(m.group(2))])
        _push(vals, strong_candidates)

        vals = [int(m.group(1)) for m in re.finditer(r"(?:up to|ceiling|maximum(?:\s+of)?|max(?:\s+of)?)\s*(\d{2,5})", line, re.I)]
        _push(vals, strong_candidates)

        vals = [int(m.group(1)) for m in re.finditer(r"\bN\s*=\s*(\d{2,5})\b", line, re.I)]
        _push(vals, strong_candidates)

        vals = [int(m.group(1)) for m in re.finditer(r"(?<![A-Za-z0-9-])(\d{2,5})(?![A-Za-z0-9-])\s+(?:patients?|subjects?|participants?)\b", line, re.I)]
        _push(vals, strong_candidates)

        vals = [int(m.group(1)) for m in re.finditer(r"(?:patients?|subjects?|participants?)\s*(?:of|=|:|~|approximately|about)?\s*(\d{2,5})\b", line, re.I)]
        _push(vals, strong_candidates)

        vals = [int(m.group(1)) for m in standalone_num.finditer(line)]
        _push(vals, weak_candidates)

    if strong_candidates:
        return max(strong_candidates)
    if weak_candidates:
        return max(weak_candidates)
    return None


def _extract_explicit_initial_fpv_country(protocol_text: str, design_text: str) -> str | None:
    combined = "\n".join([design_text or "", protocol_text or ""])
    alias_items = sorted(_COUNTRY_ALIASES.items(), key=lambda kv: len(kv[0]), reverse=True)

    def _alias_matches(fragment: str, alias: str) -> bool:
        pattern = r"\b" + re.escape(alias) + r"\b"
        if len(alias) <= 3 and alias.upper() == alias:
            return bool(re.search(pattern, fragment))
        return bool(re.search(pattern, fragment, re.I))

    def _country_from_fragment(fragment: str) -> str | None:
        for alias, abbr in alias_items:
            if _alias_matches(fragment, alias):
                info = _COUNTRY_INFO.get(abbr, {})
                return str(info.get("name") or abbr)
        return None

    patterns = [
        r"country\s+where\s+initial\s+fpv\s+planned\s*[:=\-]\s*([^\n.;]+)",
        r"initial\s+(?:fpv|first\s+patient\s+visit)\s*(?:is|=|planned|planned\s+in|planned\s+for|in)?\s*([^\n.;]+)",
        r"first\s+patient\s+visit\s*(?:is|=|planned|planned\s+in|planned\s+for|in)\s*([^\n.;]+)",
        r"first\s+country\s*(?:for\s*)?(?:fpv|first\s+patient\s+visit)\s*[:=\-]?\s*([^\n.;]+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, combined, re.I):
            country = _country_from_fragment(match.group(1))
            if country:
                return country
    return None


def _as_plain(obj):
    if obj is None:
        return None
    if is_dataclass(obj):
        return asdict(obj)
    if isinstance(obj, dict):
        return {k: _as_plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_as_plain(v) for v in obj]
    return obj


def _parse_flexible_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    s = value.strip()
    if not s:
        return None

    fmts = [
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%d %b %Y",
        "%d %B %Y",
        "%Y-%m-%d",
        "%b-%Y",
        "%B-%Y",
        "%b %Y",
        "%B %Y",
    ]
    for fmt in fmts:
        try:
            parsed = dt.datetime.strptime(s, fmt)
            if "%d" not in fmt:
                parsed = parsed.replace(day=1)
            return parsed.date()
        except ValueError:
            continue
    return None


def _months_between(start: dt.date, end: dt.date) -> float:
    return round((end - start).days / 30.4375, 1)


def _add_business_days(start: dt.date, days: int) -> dt.date:
    cur = start
    left = days
    while left > 0:
        cur += dt.timedelta(days=1)
        if cur.weekday() < 5:
            left -= 1
    return cur


def _resolve_path(context: dict, path: str):
    if not path:
        return None
    obj = context
    for token in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(token)
        else:
            obj = getattr(obj, token, None)
        if obj is None:
            return None
    return obj


def _fallback_from_spec(spec: dict, context: dict):
    stype = spec.get("source_type")
    if stype == "direct":
        return _resolve_path(context, spec.get("source", ""))
    if stype == "conditional_fallback":
        primary = _resolve_path(context, spec.get("primary", ""))
        if primary:
            if spec.get("primary_value_if_truthy"):
                return spec.get("primary_value_if_truthy")
            return primary
        return _resolve_path(context, spec.get("fallback", ""))
    if stype in {"mixed_list", "object_passthrough_with_validation", "range_or_scenario"}:
        return _resolve_path(context, spec.get("source", ""))
    return None


def extract_with_schema_v4(
    protocol_content: str,
    design_content: str = "",
    protocol_format: str = "markdown",
    design_format: str = "markdown",
    schema_path: str | None = None,
    user_inputs: dict | None = None,
    previous_rfp_path: str = "",
) -> dict:
    schema_file = Path(schema_path) if schema_path else DEFAULT_SCHEMA_PATH
    schema = json.loads(schema_file.read_text(encoding="utf-8"))

    protocol_text = _strip_json_suffix(normalize_content(protocol_content, protocol_format))
    design_text = _strip_json_suffix(normalize_content(design_content, design_format))
    user_inputs = user_inputs or {}

    design_data = DesignParser(design_text).parse() if design_text.strip() else None

    soa_headers, soa_rows = _extract_soa_table(protocol_text)
    visit_count = sum(1 for h in (soa_headers[1:] if soa_headers else []) if is_visit_column(h))

    lab_rows = _extract_lab_rows(protocol_text)
    phrases = []
    for pf in schema.get("protocol_fields", []):
        if pf.get("field") == "Analytes — not reported to sites":
            phrases = pf.get("match_phrases", [])
            break
    analytes_not_reported = _find_analytes_not_reported(lab_rows, phrases)
    reflex_map = _compute_reflex_optional(protocol_text)

    ta_primary = getattr(getattr(design_data, "flags", None), "therapeutic_area", None) if design_data else None
    ta_fallback = therapeutic_area(design_text) if design_text else None
    # Final fallback: a disease/indication keyword scan with no structural assumption at all --
    # only engages once both the structured (CDE-section/Q&A-table) and label-based ("Therapeutic
    # Area:") paths above have already been tried and found nothing. Design-derived text is
    # checked before protocol text, matching this file's existing precedence convention.
    ta_keyword = therapeutic_area_by_keyword(design_text) or therapeutic_area_by_keyword(protocol_text)
    ta_value = ta_primary or ta_fallback or ta_keyword

    immuno_primary = getattr(getattr(design_data, "flags", None), "immunogenicity_needed", None) if design_data else None
    immuno_value = "Yes" if immuno_primary else immunogenicity(design_text or protocol_text)

    pgx_primary = getattr(getattr(design_data, "flags", None), "genetics_pgx_collected", None) if design_data else None
    pgx_value = "Yes" if pgx_primary else genetics_pgx(design_text or protocol_text)

    planned_raw = getattr(getattr(design_data, "enrollment", None), "planned", None) if design_data else None
    enrolled_value = _extract_enrollment_highest(design_text, planned_raw) if design_text else None

    explicit_screen_fail = _extract_screen_fail_rate(design_text) if design_text else None
    used_screen_fail_fallback = explicit_screen_fail is None
    screen_fail_rate = explicit_screen_fail if explicit_screen_fail is not None else 0.30
    screened_value = round(enrolled_value / (1 - screen_fail_rate)) if enrolled_value else None

    # Countries come ONLY from the design document's own "Country Check" row, column 4 (see
    # extract_country_check_row's docstring in extractors.py) -- NOT from design_data.
    # country_allocation or parse_country_allocation(), both of which scan much wider spans of
    # text (a whole matched sentence, or a 3000-char window after a trigger phrase) and pick up
    # example/instructional text and unrelated country mentions (site lists, translation notes,
    # amendment history). Previously this list was *seeded* from those broad sources and the
    # column-4 scan only added to / upgraded it, which meant the broad, over-inclusive list
    # always won (confirmed directly: the user saw every mentioned country, not just column 4's).
    countries = extract_country_check_row(design_text, [])

    timeline = getattr(design_data, "timeline", None) if design_data else None
    fallback_milestones = extract_milestone_dates(design_text) if design_text else {}
    pa = getattr(timeline, "protocol_approval", None) if timeline else None
    fpv = getattr(timeline, "fpv", None) if timeline else None
    lpv = getattr(timeline, "lpv", None) if timeline else None
    fpet = getattr(timeline, "fpet", None) if timeline else None
    lpet = getattr(timeline, "lpet", None) if timeline else None

    pa = pa or fallback_milestones.get("Protocol Approval")
    fpv = fpv or fallback_milestones.get("FPV")
    lpv = lpv or fallback_milestones.get("LPV")
    fpet = fpet or fallback_milestones.get("FPET")
    lpet = lpet or fallback_milestones.get("LPET")

    fpv_date = _parse_flexible_date(fpv)
    lpv_date = _parse_flexible_date(lpv)
    dbl_date = (lpv_date + dt.timedelta(days=28)) if lpv_date else None
    protocol_duration = _months_between(fpv_date, dbl_date) if fpv_date and dbl_date else None
    initial_siv = (fpv_date - dt.timedelta(days=14)) if fpv_date else None

    is_pediatric = getattr(getattr(design_data, "flags", None), "includes_pediatric", None) if design_data else None
    if is_pediatric is None:
        m_age = min_age(protocol_text)
        is_pediatric = (m_age < 18) if m_age is not None else None

    today = dt.date.today()
    date_submitted = today.strftime("%d-%b-%Y")
    date_budget_required = _add_business_days(today, 10).strftime("%d-%b-%Y")

    explicit_initial_country = _extract_explicit_initial_fpv_country(protocol_text, design_text)
    if explicit_initial_country:
        country_initial_fpv = explicit_initial_country
        used_us_default = False
    else:
        country_initial_fpv = "US"
        used_us_default = True

    io_input = str(
        user_inputs.get("is_immuno_oncology_protocol")
        or user_inputs.get("immuno_oncology")
        or ""
    ).strip().lower()
    if io_input in {"yes", "true", "1"}:
        immuno_oncology_flag = "Yes"
    elif io_input in {"no", "false", "0"}:
        immuno_oncology_flag = "No"
    else:
        immuno_oncology_flag = "Yes" if is_oncology(protocol_text, ta_value) else "No"

    country_table_cells = []
    for c in countries:
        pct = c.get("pct")
        screened = round(screened_value * pct) if screened_value and pct is not None else None
        randomized = round(enrolled_value * pct) if enrolled_value and pct is not None else None
        country_table_cells.append(
            {
                "country": c.get("name"),
                "abbreviation": c.get("abbreviation"),
                "pct": pct,
                "status": c.get("status"),
                "planned_screened_patients": screened,
                "randomized_patients": randomized,
            }
        )

    hypersensitivity_hepatic_visits = round(enrolled_value * visit_count * 0.02) if enrolled_value and visit_count else None

    translation_languages = set()
    for c in countries:
        translation_languages.update(COUNTRY_LANG.get(c.get("abbreviation"), []))

    protocol_values = {
        "Protocol alias": protocol_number(protocol_text),
        "Protocol title": protocol_title(protocol_text),
        "Compound": compound(protocol_text),
        "Phase": phase(protocol_text),
        "Analytes — not reported to sites": analytes_not_reported,
        "Reflex/Optional testing": reflex_map,
        "Total number of visits (Schedule of Activities)": visit_count,
    }

    design_values = {
        "Therapeutic Area": ta_value,
        "Immunogenicity testing needed": immuno_value,
        "Genetics/PGx sample collected": pgx_value,
        "Patients enrolled (randomized)": enrolled_value,
        "Patients screened": screened_value,
        "Countries in scope": countries,
        "Country Allocation table": countries,
        "Trial Milestones (PA/FPV/LPV/FPET/LPET)": {
            "protocol_approval": pa,
            "fpv": fpv,
            "lpv": lpv,
            "fpet": fpet,
            "lpet": lpet,
        },
        "Protocol Approval (PA) date": pa,
        "Planned First Patient Visit (FPV) date": fpv,
        "Planned Last Patient Visit (LPV) date": lpv,
        "Protocol duration (FPV-DBL)": protocol_duration,
        "Pediatric population?": ("Yes" if is_pediatric else "No") if is_pediatric is not None else None,
    }

    engine_values = {
        "Date RFP submitted": date_submitted,
        "Date budget required": date_budget_required,
        "Country where initial FPV planned": country_initial_fpv,
        "Initial SIV date": initial_siv.strftime("%d-%b-%Y") if initial_siv else None,
        "Immuno Oncology protocol": immuno_oncology_flag,
        "Country Allocation table cells": country_table_cells,
        "Reflex/Optional rows": reflex_map,
        "Hypersensitivity + Hepatic # patient visits": hypersensitivity_hepatic_visits,
        "Translations checkboxes": sorted(translation_languages),
        "Referral/Storage specimen tables": previous_rfp_path or None,
    }

    context = {
        "PT": {
            "protocol_number": protocol_values["Protocol alias"],
            "protocol_title": protocol_values["Protocol title"],
            "compound": protocol_values["Compound"],
            "phase": protocol_values["Phase"],
            "full_text_lowercased": protocol_text.lower(),
            "clinical_laboratory_tests_appendix": lab_rows,
            "schedule_of_activities": {"headers": soa_headers, "rows": soa_rows},
        },
        "DT": {
            "therapeutic_area": ta_fallback,
            "immunogenicity": immunogenicity(design_text or protocol_text),
            "genetics_pgx": genetics_pgx(design_text or protocol_text),
        },
        "DESIGN_DATA": _as_plain(design_data) if design_data else None,
        "USER": user_inputs,
    }

    def _render_fields(spec_list: list[dict], values: dict) -> list[dict]:
        rendered = []
        for spec in spec_list:
            field_name = spec.get("field")
            value = values.get(field_name)
            if value is None:
                value = _fallback_from_spec(spec, context)
            rendered.append(
                {
                    "field": field_name,
                    "source_type": spec.get("source_type"),
                    "template_label": spec.get("template_label"),
                    "value": value,
                }
            )
        return rendered

    return {
        "schema_version": schema.get("schema_version", "4.0"),
        "protocol_fields": _render_fields(schema.get("protocol_fields", []), protocol_values),
        "design_fields": _render_fields(schema.get("design_fields", []), design_values),
        "rfp_engine_fields": _render_fields(schema.get("rfp_engine_fields", {}).get("fields", []), engine_values),
        "defaults_applied": {
            "country_initial_fpv_default": "US" if used_us_default else None,
            "screen_fail_rate_default": "30%" if used_screen_fail_fallback else None,
        },
        "intermediate": {
            "visit_count": visit_count,
            "screen_fail_rate_used": screen_fail_rate,
            "enrolled_value": enrolled_value,
            "screened_value": screened_value,
        },
    }


def _read_content_arg(path: str, inline: str) -> str:
    if inline:
        return inline
    if path:
        return Path(path).read_text(encoding="utf-8")
    return ""


def main() -> None:
    parser = argparse.ArgumentParser(description="Pre-parsed schema-driven extractor for extraction_schema_v4.json")
    parser.add_argument("--protocol-file", default="", help="Path to pre-parsed protocol content file")
    parser.add_argument("--protocol-content", default="", help="Inline protocol content")
    parser.add_argument("--protocol-format", default="markdown", choices=["markdown", "json", "html", "text"])
    parser.add_argument("--design-file", default="", help="Path to pre-parsed design content file")
    parser.add_argument("--design-content", default="", help="Inline design content")
    parser.add_argument("--design-format", default="markdown", choices=["markdown", "json", "html", "text"])
    parser.add_argument("--schema-path", default=str(DEFAULT_SCHEMA_PATH), help="Path to extraction_schema_v4.json")
    parser.add_argument("--previous-rfp-path", default="", help="Optional previous RFP path for carry-forward fields")
    parser.add_argument("--immuno-oncology", default="", help="Optional user override: Yes/No")
    parser.add_argument("--out", default="", help="Output JSON path (defaults to stdout)")
    args = parser.parse_args()

    protocol_content = _read_content_arg(args.protocol_file, args.protocol_content)
    design_content = _read_content_arg(args.design_file, args.design_content)
    user_inputs = {}
    if args.immuno_oncology:
        user_inputs["immuno_oncology"] = args.immuno_oncology

    result = extract_with_schema_v4(
        protocol_content=protocol_content,
        design_content=design_content,
        protocol_format=args.protocol_format,
        design_format=args.design_format,
        schema_path=args.schema_path,
        user_inputs=user_inputs,
        previous_rfp_path=args.previous_rfp_path,
    )

    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        # Compact, single-line -- callers that invoke this as a subprocess and read stdout
        # (CLS Studio's extract_rfp_schema) take the last non-empty line as the JSON payload;
        # indent=2's pretty-printed output left just a trailing "}" on that line, which failed
        # to parse ("expected value at line 1 column 1") on every real run.
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
