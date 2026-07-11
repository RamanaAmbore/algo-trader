#!/usr/bin/env python3
"""
RAMBOQ-TLM — Master orchestrator.

Runs all (or selected) TLM tools in sequence, prints a consolidated summary
table, and optionally writes a markdown audit report.

Usage:
  python tools/tlm/run_all.py [options]

Options:
  --tools all|ccwatch,pycheck,...   Which tools to run (default: all)
  --since N                         Days back for git-based tools (default: 1)
  --output PATH                     Write audit markdown to PATH
  --json                            Also write JSON summary to PATH.json
  --auto-commit                     Stage output file and commit after run
  --dry-run                         Pass --dry-run to all tools
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Make tools/tlm importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _base import TlmResult, REPO_ROOT  # noqa: E402
from ccwatch import CcWatch  # noqa: E402
from pycheck import PyCheck  # noqa: E402
from depscan import DepScan  # noqa: E402
from docdrift import DocDrift  # noqa: E402
from perfmon import PerfMon  # noqa: E402
from snapcheck import SnapCheck  # noqa: E402

# Ordered registry of all tools
ALL_TOOLS = [
    CcWatch(),
    PyCheck(),
    PerfMon(),
    SnapCheck(),
    DepScan(),
    DocDrift(),
]

TOOL_NAMES = {t.name.lower(): t for t in ALL_TOOLS}

# ------------------------------------------------------------------ #
# Display helpers                                                     #
# ------------------------------------------------------------------ #
_STATUS_ICON = {
    "ok":   "  ok  ",
    "warn": " warn ",
    "fail": " FAIL ",
    "skip": " skip ",
}

_STATUS_PREFIX = {
    "ok":   "v",
    "warn": "~",
    "fail": "!",
    "skip": "-",
}


def _now_ist() -> str:
    # IST = UTC+5:30
    utc = datetime.datetime.utcnow()
    ist = utc + datetime.timedelta(hours=5, minutes=30)
    return ist.strftime("%Y-%m-%d %H:%M IST")


def _print_summary_table(results: list[TlmResult]) -> None:
    name_w = max(len(r.tool) for r in results)
    print()
    print(f"RAMBOQ-TLM  {_now_ist()}")
    print("=" * 60)
    for r in results:
        icon = _STATUS_PREFIX[r.status]
        status_label = _STATUS_ICON[r.status].strip()
        sev = f"[{r.severity}]" if r.severity else "     "
        print(f"  {icon} {r.tool:<{name_w}}  {status_label:<4}  {sev}  {r.summary}")
    print()


def _overall_status(results: list[TlmResult]) -> tuple[str, int]:
    """Return (label, exit_code) for the overall run."""
    has_p1 = any(
        r.severity == "P1" and r.status in ("fail",)
        for r in results
    )
    has_p2 = any(
        r.severity in ("P1", "P2") and r.exit_code == 1
        for r in results
    )
    if has_p1 or has_p2:
        return "FAIL", 1
    has_warn = any(r.status == "warn" for r in results)
    if has_warn:
        return "WARN", 0
    has_skip = all(r.status == "skip" for r in results)
    if has_skip:
        return "SKIP", 0
    return "OK", 0


def _write_markdown(results: list[TlmResult], path: Path) -> None:
    lines = [
        f"# RAMBOQ-TLM Audit Report",
        f"",
        f"Generated: {_now_ist()}",
        f"",
        f"## Summary",
        f"",
        f"| Tool | Status | Severity | Summary |",
        f"|------|--------|----------|---------|",
    ]
    for r in results:
        lines.append(
            f"| {r.tool} | {r.status} | {r.severity or '-'} | {r.summary} |"
        )

    overall, _ = _overall_status(results)
    lines += ["", f"**Overall: {overall}**", ""]

    for r in results:
        lines += [
            f"## {r.tool}",
            f"",
            f"**Status**: {r.status}  ",
            f"**Summary**: {r.summary}",
            f"",
        ]
        if r.findings:
            lines += ["**Findings:**", ""]
            for f in r.findings:
                lines.append(f"- `{f.severity}` **{f.item}**: {f.detail}")
            lines.append("")
        else:
            lines += ["No findings.", ""]

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    print(f"[TLM] Audit report written to {path}")


def _auto_commit(output_path: Path) -> None:
    subprocess.run(
        ["git", "add", str(output_path)],
        cwd=str(REPO_ROOT),
    )
    today = datetime.date.today().isoformat()
    msg = f"chore(tlm): TLM audit report {today}"
    subprocess.run(
        ["git", "commit", "-m", msg],
        cwd=str(REPO_ROOT),
    )
    print(f"[TLM] Committed audit report with message: {msg}")


# ------------------------------------------------------------------ #
# Main                                                                #
# ------------------------------------------------------------------ #
def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="run_all",
        description="RAMBOQ-TLM master orchestrator — run all TLM quality tools.",
    )
    parser.add_argument(
        "--tools",
        default="all",
        help="Comma-separated tool names to run, or 'all' (default: all).",
    )
    parser.add_argument(
        "--since",
        type=int,
        default=1,
        metavar="DAYS",
        help="Days back for git-based tools (default: 1).",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        help="Write audit markdown to PATH.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Also write JSON summary to PATH.json (requires --output).",
    )
    parser.add_argument(
        "--auto-commit",
        action="store_true",
        help="Stage output file and commit after run.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Pass --dry-run to all tools — no side effects.",
    )

    args = parser.parse_args(argv)

    # Resolve tool list
    if args.tools.strip().lower() == "all":
        selected = ALL_TOOLS
    else:
        names = [n.strip().lower() for n in args.tools.split(",")]
        selected = []
        for n in names:
            if n not in TOOL_NAMES:
                print(f"[TLM] Unknown tool: {n!r}. Available: {', '.join(TOOL_NAMES)}")
                return 2
            selected.append(TOOL_NAMES[n])

    if args.dry_run:
        print(f"[TLM] --dry-run: would run {len(selected)} tool(s):")
        for t in selected:
            print(f"  - {t.name}: {t.description}")
            fake_args = argparse.Namespace(since=args.since, dry_run=True, json=False)
            t._print_dry_run_plan(fake_args)
        return 0

    # Run tools in sequence
    results: list[TlmResult] = []
    for tool in selected:
        tool_argv = ["--since", str(args.since)]
        if args.dry_run:
            tool_argv.append("--dry-run")
        tool_argv.append("--json")

        # Run each tool in-process using its main() but capture JSON from stdout
        # We redirect by calling run() directly so we can capture TlmResult cleanly.
        import argparse as _ap
        sub_parser = tool.build_parser()
        tool.add_args(sub_parser)
        sub_args = sub_parser.parse_args(["--since", str(args.since), "--json"])

        try:
            result = tool.run(sub_args)
        except Exception as exc:  # noqa: BLE001
            from _base import TlmResult as _TR
            result = _TR.tool_error(tool.name, str(exc))

        results.append(result)

    # Print consolidated table
    _print_summary_table(results)
    overall, exit_code = _overall_status(results)

    # P-level count for overall line
    p1 = sum(1 for r in results for f in r.findings if f.severity == "P1")
    p2 = sum(1 for r in results for f in r.findings if f.severity == "P2")
    p3 = sum(1 for r in results for f in r.findings if f.severity == "P3")
    parts = []
    if p1:
        parts.append(f"{p1} P1")
    if p2:
        parts.append(f"{p2} P2")
    if p3:
        parts.append(f"{p3} P3")
    detail = f" ({', '.join(parts)} finding(s))" if parts else ""
    print(f"Overall: {overall}{detail}")
    print()

    # Write output if requested
    if args.output:
        output_path = Path(args.output)
        _write_markdown(results, output_path)

        if args.json:
            json_path = output_path.with_suffix(".json")
            json_path.write_text(
                json.dumps([r.to_dict() for r in results], indent=2) + "\n"
            )
            print(f"[TLM] JSON summary written to {json_path}")

        if args.auto_commit:
            _auto_commit(output_path)

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
