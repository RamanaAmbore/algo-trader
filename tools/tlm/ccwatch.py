#!/usr/bin/env python3
"""
CCWATCH — Cyclomatic Complexity snapshot + regression detector.

Runs radon cc over the core algo files and compares against the previous
day's snapshot to detect grade regressions and new F-grade functions.

Snapshot files: .log/cc_snapshot_YYYY-MM-DD.txt
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

# Make _base importable when invoked directly
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import TlmTool, TlmResult, TlmFinding, REPO_ROOT, LOG_DIR  # noqa: E402

# Files to analyse — keep in sync with CI if this list grows
CC_TARGETS = [
    "backend/api/algo/actions.py",
    "backend/api/algo/agent_engine.py",
    "backend/api/algo/template_attach.py",
    "backend/api/background.py",
]

# Radon grade ordering — higher index = worse
GRADE_ORDER = ["A", "B", "C", "D", "E", "F"]


def _grade_rank(g: str) -> int:
    try:
        return GRADE_ORDER.index(g.upper())
    except ValueError:
        return 99


def _parse_radon_output(text: str) -> dict[str, str]:
    """
    Parse radon -s text output.
    Returns {function_key: grade}, e.g. {"actions.py:run_preflight": "F"}.
    """
    result: dict[str, str] = {}
    current_file = ""
    # Function line pattern:  "    F 815:0 run_preflight - F (98)"
    func_re = re.compile(r"^\s+\w\s+\d+:\d+\s+(\S+)\s+-\s+([A-F])\s+\(\d+\)")
    for line in text.splitlines():
        stripped = line.rstrip()
        if stripped and not stripped[0].isspace():
            # File header line
            current_file = Path(stripped).name
        else:
            m = func_re.match(stripped)
            if m:
                func_name, grade = m.group(1), m.group(2)
                key = f"{current_file}:{func_name}"
                result[key] = grade
    return result


def _load_snapshot(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 2:
            data[parts[0]] = parts[1]
    return data


def _save_snapshot(path: Path, grades: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# CCWATCH snapshot — " + datetime.date.today().isoformat()]
    for key in sorted(grades):
        lines.append(f"{key}\t{grades[key]}")
    path.write_text("\n".join(lines) + "\n")


class CcWatch(TlmTool):
    name = "CCWATCH"
    description = "Cyclomatic complexity snapshot + grade regression detector."

    def _print_dry_run_plan(self, args: argparse.Namespace) -> None:
        print(f"  Would run: radon cc {' '.join(CC_TARGETS)} -s --min C")
        print("  Would compare against previous snapshot in .log/cc_snapshot_*.txt")
        print("  Would save today's snapshot to .log/cc_snapshot_YYYY-MM-DD.txt")

    def run(self, args: argparse.Namespace) -> TlmResult:
        # Resolve target paths relative to repo root
        target_paths = [str(REPO_ROOT / t) for t in CC_TARGETS]
        existing = [p for p in target_paths if Path(p).exists()]
        if not existing:
            return TlmResult.skip(self.name, "No target files found")

        # Run radon
        cmd = ["radon", "cc"] + existing + ["-s", "--min", "C"]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, cwd=str(REPO_ROOT)
            )
        except FileNotFoundError:
            return TlmResult.skip(self.name, "radon not installed — pip install radon")

        today_grades = _parse_radon_output(proc.stdout)

        # Load previous snapshot
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        prev_path = LOG_DIR / f"cc_snapshot_{yesterday.isoformat()}.txt"
        prev_grades = _load_snapshot(prev_path)
        first_run = not prev_grades

        # Save today's snapshot
        today_path = LOG_DIR / f"cc_snapshot_{today.isoformat()}.txt"
        _save_snapshot(today_path, today_grades)

        # Analyse findings
        findings: list[TlmFinding] = []

        if first_run:
            # First run: all F-grades are P3 "not previously tracked"
            for key, grade in today_grades.items():
                if grade == "F":
                    findings.append(
                        TlmFinding(
                            item=key,
                            detail=f"F-grade function (baseline run — no prior snapshot)",
                            severity="P3",
                        )
                    )
            f_count = len([g for g in today_grades.values() if g == "F"])
            ok_msg = f"Baseline established — {f_count} F-grade function(s) tracked"
            if findings:
                result = self.build_result(self.name, findings, ok_msg)
                result.summary = ok_msg + f" ({len(findings)} P3 on first run)"
                return result
            return self.build_result(self.name, findings, ok_msg)

        # Compare against previous snapshot
        for key, curr_grade in today_grades.items():
            prev_grade = prev_grades.get(key)

            if prev_grade is None:
                # New function
                if curr_grade == "F":
                    findings.append(
                        TlmFinding(
                            item=key,
                            detail=f"New F-grade function (grade: F)",
                            severity="P2",
                        )
                    )
            else:
                # Existing function — check for regression
                if _grade_rank(curr_grade) > _grade_rank(prev_grade):
                    sev = "P2" if curr_grade == "F" else "P2"
                    findings.append(
                        TlmFinding(
                            item=key,
                            detail=f"Grade regressed {prev_grade} -> {curr_grade}",
                            severity=sev,
                        )
                    )

        # Any existing F-grades that were ALREADY F in prior snapshot = P3
        for key, curr_grade in today_grades.items():
            if curr_grade == "F" and prev_grades.get(key) == "F":
                # Already tracked, no regression, still worth noting at P3
                # Only add if no P2 finding already for this key
                already = any(f.item == key for f in findings)
                if not already:
                    findings.append(
                        TlmFinding(
                            item=key,
                            detail="Existing F-grade function (unchanged, tracked)",
                            severity="P3",
                        )
                    )

        f_count = sum(1 for g in today_grades.values() if g == "F")
        ok_msg = f"No CC regressions ({f_count} F-grade function(s) tracked)"
        return self.build_result(self.name, findings, ok_msg)


if __name__ == "__main__":
    tool = CcWatch()
    sys.exit(tool.main())
