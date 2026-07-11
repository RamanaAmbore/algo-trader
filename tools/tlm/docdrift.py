#!/usr/bin/env python3
"""
DOCDRIFT — DESIGN_GUIDE.md vs recent commit drift detector.

Scans git commits over the last N days for changes to backend/ and frontend/
that touch architectural concepts. Cross-references whether docs/DESIGN_GUIDE.md
was also updated within the same window. Missing doc update = P3 finding.

Architectural keywords (commit message match):
  routes, background, broker, agent, engine, options, F&O, nav,
  lifecycle, middleware, auth, schema, migration, component, store
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import TlmTool, TlmResult, TlmFinding, REPO_ROOT  # noqa: E402

# Keywords that indicate an architectural change in a commit message
ARCH_KEYWORDS = [
    "route", "routes",
    "background", "background task",
    "broker",
    "agent", "agent engine",
    "options", "f&o", "fno",
    "nav", "navstrip",
    "lifecycle", "market lifecycle",
    "middleware",
    "auth", "authentication",
    "schema", "migration",
    "component", "store", "svelte",
    "algo",
    "template",
    "simulator",
    "ticker",
    "persistence",
    "ohlcv",
]

_KW_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in ARCH_KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def _git_log(since_days: int, path_filter: str) -> list[str]:
    """Return list of oneline log entries for path_filter over last N days."""
    cmd = [
        "git", "log",
        f"--since={since_days} days ago",
        "--oneline",
        "--no-merges",
        "--", path_filter,
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _is_arch_commit(message: str) -> bool:
    return bool(_KW_RE.search(message))


class DocDrift(TlmTool):
    name = "DOCDRIFT"
    description = "Detect architectural commits not reflected in DESIGN_GUIDE.md within the same window."

    def _print_dry_run_plan(self, args: argparse.Namespace) -> None:
        print(f"  Would scan git log --since={args.since} days ago -- backend/ frontend/")
        print("  Would look for architectural keywords in commit messages")
        print("  Would check git log for docs/DESIGN_GUIDE.md updates in same window")

    def run(self, args: argparse.Namespace) -> TlmResult:
        since = args.since

        # Commits touching backend/ or frontend/
        backend_commits = _git_log(since, "backend/")
        frontend_commits = _git_log(since, "frontend/")
        code_commits = backend_commits + frontend_commits

        # Commits touching the DESIGN_GUIDE
        doc_commits = _git_log(since, "docs/DESIGN_GUIDE.md")

        arch_commits = [c for c in code_commits if _is_arch_commit(c)]

        findings: list[TlmFinding] = []

        if arch_commits and not doc_commits:
            # Architectural changes with zero doc updates in the window
            for commit in arch_commits:
                sha = commit[:9]
                msg = commit[10:] if len(commit) > 10 else commit
                findings.append(
                    TlmFinding(
                        item=sha,
                        detail=f"Architectural commit without DESIGN_GUIDE update: \"{msg[:100]}\"",
                        severity="P3",
                    )
                )
        elif arch_commits and doc_commits:
            # Doc was updated but maybe not for every arch commit — single P3 note
            # We consider the window covered if doc_commits is non-empty
            pass

        if not code_commits:
            ok_msg = f"No code commits in the past {since} day(s)"
        elif not arch_commits:
            ok_msg = f"{len(code_commits)} commit(s) in window, none architectural"
        elif doc_commits:
            ok_msg = (
                f"{len(arch_commits)} architectural commit(s) — "
                f"DESIGN_GUIDE updated ({len(doc_commits)} doc commit(s) in window)"
            )
        else:
            ok_msg = f"{len(arch_commits)} architectural commit(s) without DESIGN_GUIDE update"

        return self.build_result(self.name, findings, ok_msg)


if __name__ == "__main__":
    tool = DocDrift()
    sys.exit(tool.main())
