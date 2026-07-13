#!/usr/bin/env python3
"""
PERFMON — Performance regression monitor.

Full implementation lives in tools/perf_regression.py (generated separately).
This stub delegates to that script when present; emits status=skip when absent.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import TlmTool, TlmResult, REPO_ROOT, VENV_PYTHON  # noqa: E402

DELEGATE = REPO_ROOT / "tools" / "perf_regression.py"


class PerfMon(TlmTool):
    name = "PERFMON"
    description = "Performance regression monitor (delegates to tools/perf_regression.py)."

    def _print_dry_run_plan(self, args) -> None:
        if DELEGATE.exists():
            print(f"  Would delegate to: {DELEGATE} --dry-run")
        else:
            print(f"  Would skip: {DELEGATE} not yet installed")

    def run(self, args) -> TlmResult:
        if not DELEGATE.exists():
            return TlmResult.skip(
                self.name,
                f"perf_regression.py not yet installed (expected at {DELEGATE.relative_to(REPO_ROOT)})",
            )

        extra = []
        if args.since != 1:
            extra += ["--since", str(args.since)]

        r = subprocess.run(
            [VENV_PYTHON, str(DELEGATE)] + extra,
            cwd=str(REPO_ROOT),
        )
        # Delegate's exit code maps to TlmResult exit_code directly
        if r.returncode == 0:
            return TlmResult(
                tool=self.name,
                status="ok",
                severity="",
                summary="perf_regression.py completed cleanly",
                findings=[],
                exit_code=0,
            )
        elif r.returncode == 1:
            return TlmResult(
                tool=self.name,
                status="warn",
                severity="P2",
                summary="perf_regression.py reported regressions (see output above)",
                findings=[],
                exit_code=1,
            )
        else:
            return TlmResult.tool_error(
                self.name, f"perf_regression.py exited with code {r.returncode}"
            )


if __name__ == "__main__":
    tool = PerfMon()
    sys.exit(tool.main())
