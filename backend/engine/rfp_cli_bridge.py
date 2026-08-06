#!/usr/bin/env python3
"""Additive CLI wrapper around populate_rfp.main() for the CLS Studio desktop app.

populate_rfp.py itself has no argparse CLI — its __main__ block reads file paths
from environment variables, wired for a specific containerized pipeline. This
script does not touch that block or populate_rfp.main()'s own logic at all; it
just gives an external process (the Tauri app, via a plain subprocess call) a
stable, explicit-file-path way to invoke the exact same pipeline, and prints a
small JSON coverage summary to stdout (populate_rfp.main() already returns this
directly on its Report object — no need to re-derive it from findings).

Usage:
  python rfp_cli_bridge.py --protocol-file P.txt --template T.docx --output O.docx
"""
import argparse
import json
from pathlib import Path

from populate_rfp import main as populate_rfp_main


def main():
    parser = argparse.ArgumentParser(description="Populate a Central Lab RFP .docx from protocol/design text.")
    parser.add_argument("--protocol-file", required=True, help="Path to a UTF-8 text file with protocol markdown/text.")
    parser.add_argument("--design-file", default="", help="Path to a UTF-8 text file with design-element markdown/text.")
    parser.add_argument("--template", required=True, help="Path to the .docx template.")
    parser.add_argument("--output", required=True, help="Path to write the populated .docx.")
    parser.add_argument("--report", default="", help="Path to write the fill report .md (defaults next to --output).")
    parser.add_argument("--protocol-pdf", default="", help="Optional path to the original protocol PDF (SoA/Lab Appendix table layout).")
    parser.add_argument("--previous-rfp", default="", help="Optional path to a previous, same-template RFP .docx.")
    parser.add_argument(
        "--soa-table-json", default="",
        help='Optional path to a JSON file {"headers": [...], "rows": [[...]], "footnotes": ""} — '
             "when given, inserted as-is as the Schedule of Activities table instead of running "
             "populate_rfp.py's own edgeparse/regex extraction.",
    )
    parser.add_argument(
        "--lab-table-json", default="",
        help='Optional path to a JSON file [["Test", "Comment"], ...] — same idea as '
             "--soa-table-json, for the Lab Appendix / Clinical Laboratory Tests table.",
    )
    parser.add_argument(
        "--field-overrides-json", default="",
        help="Optional path to a JSON file: either preparsed_schema_extractor.py's own output "
             '({"protocol_fields": [{"field","value"},...], "design_fields": [...], '
             '"rfp_engine_fields": [...]}) or a flat {"field name": value} map -- either way, '
             "wins over this script's own protocol/design-element extraction for any field name "
             "it resolves a non-empty value for (extraction_schema_v4.json is the authoritative "
             "field-resolution path; this script's own extraction is now just the fallback).",
    )
    parser.add_argument(
        "--clips-nonpkpd-assignments-json", default="",
        help='Optional path to a JSON file [{"path", "column"}, ...] -- CLIPS forms / '
             "Non-PK Data Mgmt Worksheets read directly (via clips_nonpkpd_parser.py) instead "
             "of (or alongside) --previous-rfp, each already assigned to its target Referral/"
             'Storage column ("LTS PK", "LTS Immunogenicity", "LTS DNA", "LTS Serum", '
             '"LTS Plasma", "LTS RNA").',
    )
    parser.add_argument(
        "--answers-json", default="",
        help="Optional path to a JSON file: flat intake-question overrides matching "
             "populate_rfp.main()'s own `answers` dict shape -- oncology_override "
             "('' | 'yes' | 'no'), decentralized/penalties_incentives/anatomic_pathology "
             "(bool), hepatic_calc ('Non-oncology' | 'Oncology' | 'Oncology w/ ICI'). These gate "
             "whether whole optional template sections are kept or deleted, not just a field's "
             "value. Missing keys fall back to populate_rfp.py's own defaults.",
    )
    args = parser.parse_args()

    protocol_text = Path(args.protocol_file).read_text(encoding="utf-8", errors="replace")
    design_text = (
        Path(args.design_file).read_text(encoding="utf-8", errors="replace") if args.design_file else ""
    )
    report_path = args.report or (str(Path(args.output).with_suffix("")) + "_report.md")

    soa_table_override = (
        json.loads(Path(args.soa_table_json).read_text(encoding="utf-8")) if args.soa_table_json else None
    )
    lab_table_override = (
        json.loads(Path(args.lab_table_json).read_text(encoding="utf-8")) if args.lab_table_json else None
    )

    field_overrides = None
    if args.field_overrides_json:
        raw = json.loads(Path(args.field_overrides_json).read_text(encoding="utf-8"))
        if isinstance(raw, dict) and any(
            k in raw for k in ("protocol_fields", "design_fields", "rfp_engine_fields")
        ):
            field_overrides = {}
            for key in ("protocol_fields", "design_fields", "rfp_engine_fields"):
                for spec in raw.get(key) or []:
                    field_overrides[spec.get("field")] = spec.get("value")
            # A caller may hand back a hybrid payload -- the structured shape above plus flat
            # "field name": value keys sitting alongside it (e.g. manual UI overrides merged in
            # without being routed into protocol_fields/design_fields/rfp_engine_fields). Layer
            # those in last so they still win, instead of silently vanishing.
            schema_keys = {"schema_version", "protocol_fields", "design_fields", "rfp_engine_fields", "defaults_applied"}
            for key, value in raw.items():
                if key not in schema_keys:
                    field_overrides[key] = value
        else:
            field_overrides = raw

    answers = {}
    if args.answers_json:
        answers = json.loads(Path(args.answers_json).read_text(encoding="utf-8")) or {}

    clips_nonpkpd_assignments = None
    if args.clips_nonpkpd_assignments_json:
        clips_nonpkpd_assignments = json.loads(
            Path(args.clips_nonpkpd_assignments_json).read_text(encoding="utf-8")
        )

    report = populate_rfp_main(
        protocol_text=protocol_text,
        design_text=design_text,
        template_path=args.template,
        output_path=args.output,
        report_path=report_path,
        answers=answers,
        protocol_pdf_path=args.protocol_pdf,
        previous_rfp_path=args.previous_rfp,
        soa_include_indices=None,
        soa_table_override=soa_table_override,
        lab_table_override=lab_table_override,
        field_overrides=field_overrides,
        clips_nonpkpd_assignments=clips_nonpkpd_assignments,
    )

    print(json.dumps({
        "status": "complete",
        "output_path": args.output,
        "report_path": report_path,
        "coverage": {
            "filled": report.filled,
            "computed": report.computed,
            "review": report.review_count,
            "total": len(report.findings),
        },
    }))


if __name__ == "__main__":
    main()
