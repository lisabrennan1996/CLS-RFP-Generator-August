import re
import json
import argparse
from dataclasses import dataclass, field, asdict
from typing import Optional
from pathlib import Path


@dataclass
class Visit:
    number: int
    week: Optional[str] = None
    is_clinic: bool = True
    is_telehealth: bool = False


@dataclass
class TestPanel:
    name: str
    is_standalone: bool
    is_lilly_lab: bool = True
    tests: list[str] = field(default_factory=list)
    raw_name: str = ""


@dataclass
class ScheduleTest:
    name: str
    visits: dict[int, bool] = field(default_factory=dict)
    is_panel: bool = True
    source: str = "schedule"


@dataclass
class MasterScheduleEntry:
    visit_number: int
    week: Optional[str]
    visit_type: str
    tests: list[str]


class ClinicalProtocolMapper:
    LILLY_LAB_PATTERN = re.compile(
        r"assayed\s+by\s+lilly[\-\s]?designated\s+laboratory",
        re.IGNORECASE
    )

    VISIT_LINE_PATTERN = re.compile(r"Visit\s+Number", re.IGNORECASE)
    WEEKS_PATTERN = re.compile(r"Weeks?\s+([\d\-,]+)", re.IGNORECASE)
    X_PATTERN = re.compile(r"\bX\b")
    CLINIC_TELEHEALTH_PATTERN = re.compile(r"C\s*=\s*Clinic.*T\s*=\s*Telehealth", re.IGNORECASE)
    PERIOD_PATTERN = re.compile(r"Period\s+[IVX]+", re.IGNORECASE)

    def __init__(self):
        self.schedule_tests: dict[str, ScheduleTest] = {}
        self.lab_panels: list[TestPanel] = []
        self.visits: list[Visit] = []
        self.master_schedule: list[MasterScheduleEntry] = []

    def parse_lab_appendix(self, lines: list[str]) -> list[TestPanel]:
        panels = []
        i = 0

        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            if not stripped:
                i += 1
                continue

            lilly_match = self.LILLY_LAB_PATTERN.search(stripped)

            if lilly_match:
                panel_name = self._extract_panel_name_from_comment(stripped, lilly_match)

                is_standalone = False
                if i + 1 < len(lines):
                    next_line = lines[i + 1].strip()
                    if self.LILLY_LAB_PATTERN.search(next_line):
                        is_standalone = True

                panel = TestPanel(
                    name=panel_name,
                    is_standalone=is_standalone,
                    is_lilly_lab=True,
                    raw_name=stripped
                )
                panels.append(panel)
                i += 1
                continue

            if panels:
                current_panel = panels[-1]
                if not current_panel.is_standalone:
                    test_name = self._extract_test_name(stripped)
                    if test_name:
                        current_panel.tests.append(test_name)

            i += 1

        self.lab_panels = panels
        return panels

    def _extract_panel_name_from_comment(self, line: str, match) -> str:
        name = line[:match.start()].strip()
        name = re.sub(r"\s+", " ", name).strip()
        name = name.rstrip(":").rstrip()
        return name

    def _extract_test_name(self, line: str) -> Optional[str]:
        stripped = line.strip()
        if not stripped:
            return None
        if stripped.startswith("If ") or stripped.startswith("Performed"):
            return None
        if stripped.startswith("-"):
            return None
        if self.LILLY_LAB_PATTERN.search(stripped):
            return None
        if "CONFIDENTIAL" in stripped.upper():
            return None
        if "APPROVED ON" in stripped.upper():
            return None
        if re.match(r"^\d+\s+\w", stripped):
            return None
        if "Clinical Laboratory Tests" in stripped and "Comments" in stripped:
            return None
        if "J4F-MC-CYAB" in stripped:
            return None

        if re.match(r"^[A-Z][a-zA-Z\-\s]+$", stripped):
            clean = stripped.split(",")[0].split("(")[0].strip()
            if len(clean) > 2:
                return clean

        parts = stripped.split()
        if parts:
            first = parts[0]
            if re.match(r"^[A-Z][a-zA-Z\-\.]+$", first) and len(first) > 2:
                skip_words = {"The", "And", "For", "Not", "See", "With", "That", "This", "Evaluated", "Collected"}
                if first not in skip_words:
                    clean = stripped.split("(")[0].split(",")[0].split("  ")[0].strip()
                    clean = re.sub(r"\s+If\s+.*$", "", clean)
                    clean = re.sub(r"\s+Evaluated\s+.*$", "", clean)
                    clean = re.sub(r"\s+Performed\s+.*$", "", clean)
                    return clean

        return None

    def parse_schedule(self, lines: list[str]) -> dict[str, ScheduleTest]:
        visit_numbers = []
        tests = {}
        collecting_visits = False
        first_visit_row = None

        for i, line in enumerate(lines):
            line_upper = line.upper()
            if "VISIT NUMBER" in line_upper:
                collecting_visits = True
                first_visit_row = i
                parts = re.split(r"\s{2,}", line.strip())
                for p in parts:
                    p = p.strip()
                    if re.match(r"^\d+$", p):
                        visit_numbers.append(int(p))

            if "PHARMACOKINETICS" in line_upper or "PK SAMPLES" in line_upper:
                break

        for i, line in enumerate(lines):
            if first_visit_row is not None and i > first_visit_row + 15:
                break

            parts = re.split(r"\s{2,}", line.strip())
            clean_parts = [p.strip() for p in parts if p.strip()]

            if len(clean_parts) < 2:
                continue

            test_name = clean_parts[0]

            if self.PERIOD_PATTERN.match(test_name):
                continue
            if re.match(r"Visit\s*Number", test_name, re.I):
                continue
            if re.match(r"Weeks?", test_name, re.I):
                continue
            if re.match(r"Visit\s*Interval", test_name, re.I):
                continue
            if re.match(r"Visit\s*Detail", test_name, re.I):
                continue
            if re.match(r"Period\s*[IVX]+", test_name, re.I):
                continue
            if "CONFIDENTIAL" in test_name.upper():
                continue
            if re.match(r"Table\s*\d+", test_name, re.I):
                continue
            if "Laboratory tests and sample" in test_name:
                continue

            x_positions = []
            for j, p in enumerate(clean_parts[1:], 1):
                if self.X_PATTERN.search(p):
                    x_positions.append(j)

            if x_positions and test_name and len(test_name) > 2:
                is_panel = self._is_likely_panel(test_name)
                tests[test_name] = ScheduleTest(
                    name=test_name,
                    is_panel=is_panel,
                    source="schedule"
                )
                for pos in x_positions:
                    if pos - 1 < len(visit_numbers):
                        visit_num = visit_numbers[pos - 1]
                        tests[test_name].visits[visit_num] = True

        self.schedule_tests = tests
        return tests

    def _is_likely_panel(self, name: str) -> bool:
        panel_indicators = [
            "Hematology", "Chemistry", "Lipids", "Urinalysis", "Panel",
            "Serology", "Hormones", "Antibody", "testing"
        ]
        name_lower = name.lower()
        for indicator in panel_indicators:
            if indicator.lower() in name_lower:
                return True
        return False

    def resolve_standalone_tests(self) -> None:
        standalone_from_lab = {p.name: p for p in self.lab_panels if p.is_standalone}

        for test_name, test in self.schedule_tests.items():
            if test_name in standalone_from_lab:
                lab_panel = standalone_from_lab[test_name]
                if not test.is_panel:
                    lab_panel.tests = [test_name]

    def build_master_schedule(self) -> list[MasterScheduleEntry]:
        visit_test_map: dict[int, list[str]] = {}

        for test_name, test in self.schedule_tests.items():
            for visit_num in test.visits:
                if visit_num not in visit_test_map:
                    visit_test_map[visit_num] = []
                visit_test_map[visit_num].append(test_name)

        for visit_num in sorted(visit_test_map.keys()):
            self.master_schedule.append(MasterScheduleEntry(
                visit_number=visit_num,
                week=None,
                visit_type="Clinic",
                tests=sorted(visit_test_map[visit_num])
            ))

        return self.master_schedule

    def to_json(self) -> str:
        output = {
            "protocol_info": {
                "source": "Clinical Protocol Mapper"
            },
            "schedule_of_activities": {
                "tests": [
                    {
                        "name": name,
                        "is_panel": test.is_panel,
                        "visits": list(test.visits.keys()),
                        "source": test.source
                    }
                    for name, test in sorted(self.schedule_tests.items())
                ],
                "visits": [
                    {
                        "number": v.number,
                        "week": v.week,
                        "is_clinic": v.is_clinic,
                        "is_telehealth": v.is_telehealth
                    }
                    for v in self.visits
                ]
            },
            "lab_appendix": {
                "panels": [
                    {
                        "name": p.name,
                        "is_standalone": p.is_standalone,
                        "is_lilly_designated_lab": p.is_lilly_lab,
                        "tests": p.tests
                    }
                    for p in self.lab_panels
                ]
            },
            "master_schedule": [
                {
                    "visit_number": entry.visit_number,
                    "week": entry.week,
                    "visit_type": entry.visit_type,
                    "tests": entry.tests
                }
                for entry in self.master_schedule
            ]
        }

        return json.dumps(output, indent=2)


def detect_sections(lines: list[str]) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    schedule_start = schedule_end = lab_start = lab_end = None

    for i, line in enumerate(lines):
        upper = line.upper()
        if "LABORATORY" in upper and "SAMPLE" in upper:
            if schedule_start is None:
                schedule_start = i
        if schedule_start and i > schedule_start and ("PHARMACOKINETICS" in upper or "PK SAMPLES" in upper):
            schedule_end = i
            break

    for i, line in enumerate(lines):
        upper = line.upper()
        if "APPENDIX" in upper and "LABORATORY" in upper:
            lab_start = i
            break
        if "CLINICAL LABORATORY TESTS" in line and lab_start is None:
            lab_start = i

    if lab_start is not None:
        lab_end = min(lab_start + 600, len(lines))

    return schedule_start, schedule_end, lab_start, lab_end


def process_file(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()

    lines = content.split("\n")
    schedule_start, schedule_end, lab_start, lab_end = detect_sections(lines)

    # `if schedule_start and schedule_end` treats a section starting at line 0 as "not found"
    # (0 is falsy in Python) - fixed to explicit None checks so a schedule/appendix section that
    # happens to start on the very first line of the extracted text isn't silently dropped.
    schedule_lines = lines[schedule_start:schedule_end] if schedule_start is not None and schedule_end is not None else []
    lab_lines = lines[lab_start:lab_end] if lab_start is not None and lab_end is not None else []

    mapper = ClinicalProtocolMapper()
    mapper.parse_schedule(schedule_lines)
    mapper.parse_lab_appendix(lab_lines)
    mapper.resolve_standalone_tests()
    mapper.build_master_schedule()

    return json.loads(mapper.to_json())


def main():
    parser = argparse.ArgumentParser(description="Clinical Protocol Mapper - Schedule of Activities to Lab Appendix")
    parser.add_argument("file", help="Path to the protocol markdown/text file")
    parser.add_argument("-o", "--output", help="Output JSON file path")
    parser.add_argument("--pretty", action="store_true", help="Pretty print JSON")

    args = parser.parse_args()

    result = process_file(args.file)

    if args.pretty or args.output:
        output = json.dumps(result, indent=2)
    else:
        output = json.dumps(result)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"Output written to {args.output}")
    else:
        print(output)


if __name__ == "__main__":
    main()