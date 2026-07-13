#!/usr/bin/env python3
"""
SNAPCHECK — Stale closed-hours snapshot detector.

Full implementation lives in scripts/check_stale_snapshots.py (generated separately).
This stub delegates to that script when present; emits status=skip when absent.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import TlmTool, TlmResult, REPO_ROOT, VENV_PYTHON  # noqa: E402

DELEGATE = REPO_ROOT / "scripts" / "check_stale_snapshots.py"


class SnapCheck(TlmTool):
    name = "SNAPCHECK"
    description = "Stale closed-hours snapshot detector (delegates to scripts/check_stale_snapshots.py)."

    def _print_dry_run_plan(self, args) -> None:
        if DELEGATE.exists():
            print(f"  Would delegate to: {DELEGATE} --dry-run")
        else:
            print(f"  Would skip: {DELEGATE} not yet installed")

    def run(self, args) -> TlmResult:
        if not DELEGATE.exists():
            return TlmResult.skip(
                self.name,
                f"check_stale_snapshots.py not yet installed (expected at {DELEGATE.relative_to(REPO_ROOT)})",
            )

        extra = []
        if args.since != 1:
            extra += ["--since", str(args.since)]

        r = subprocess.run(
            [VENV_PYTHON, str(DELEGATE)] + extra,
            cwd=str(REPO_ROOT),
        )
        if r.returncode == 0:
            return TlmResult(
                tool=self.name,
                status="ok",
                severity="",
                summary="check_stale_snapshots.py completed cleanly",
                findings=[],
                exit_code=0,
            )
        elif r.returncode == 1:
            return TlmResult(
                tool=self.name,
                status="fail",
                severity="P1",
                summary="check_stale_snapshots.py found stale snapshot(s) — see output above",
                findings=[],
                exit_code=1,
            )
        elif r.returncode == 2:
            return TlmResult.skip(
                self.name,
                "asyncpg not installed (local dev) — skipping stale snapshot check",
            )
        elif r.returncode == 3:
            return TlmResult.skip(
                self.name,
                "DB unreachable (local dev / no DB role) — skipping stale snapshot check",
            )
        else:
            return TlmResult.tool_error(
                self.name, f"check_stale_snapshots.py exited with code {r.returncode}"
            )


if __name__ == "__main__":
    tool = SnapCheck()
    sys.exit(tool.main())
