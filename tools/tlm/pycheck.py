#!/usr/bin/env python3
"""
PYCHECK — Pytest baseline tracker.

Runs the backend test suite and compares pass/fail counts against the
previous snapshot. Any test that was passing yesterday and is failing
today is a P1 finding with the test name included.

Snapshot files: .log/pytest_snapshot_YYYY-MM-DD.txt
Format:
  passed=N
  skipped=N
  failed=N
  failed_tests=test_foo,test_bar,...
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import TlmTool, TlmResult, TlmFinding, REPO_ROOT, LOG_DIR  # noqa: E402

TEST_DIR = "backend/tests/"


def _parse_snapshot(path: Path) -> dict:
    if not path.exists():
        return {}
    data: dict = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if "=" in line:
            k, _, v = line.partition("=")
            data[k.strip()] = v.strip()
    return data


def _save_snapshot(
    path: Path,
    passed: int,
    skipped: int,
    failed: int,
    failed_tests: list[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# PYCHECK snapshot — {datetime.date.today().isoformat()}",
        f"passed={passed}",
        f"skipped={skipped}",
        f"failed={failed}",
        f"failed_tests={','.join(failed_tests)}",
    ]
    path.write_text("\n".join(lines) + "\n")


def _parse_pytest_output(stdout: str, stderr: str) -> tuple[int, int, int, list[str]]:
    """
    Parse pytest -q output.
    Returns (passed, skipped, failed, list_of_failed_test_ids).
    """
    passed = skipped = failed = 0
    failed_tests: list[str] = []

    # Summary line e.g.: "2264 passed, 13 skipped, 2 failed in 45.32s"
    summary_re = re.compile(
        r"(\d+) passed(?:.*?(\d+) skipped)?(?:.*?(\d+) failed)?", re.IGNORECASE
    )
    for line in (stdout + "\n" + stderr).splitlines():
        m = summary_re.search(line)
        if m:
            passed = int(m.group(1) or 0)
            skipped = int(m.group(2) or 0)
            failed = int(m.group(3) or 0)

    # Collect failed test IDs from FAILED lines:
    # "FAILED backend/tests/test_foo.py::test_bar - AssertionError"
    failed_line_re = re.compile(r"^FAILED\s+([\w/:.]+)", re.MULTILINE)
    for m in failed_line_re.finditer(stdout):
        failed_tests.append(m.group(1))

    return passed, skipped, failed, failed_tests


class PyCheck(TlmTool):
    name = "PYCHECK"
    description = "Pytest baseline tracker — detects new test failures vs prior snapshot."

    def add_args(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--quick",
            metavar="MODULE",
            default=None,
            help=(
                "Quick mode: run only tests matching backend/tests/test_<MODULE>*.py "
                "instead of the full suite. Snapshot is NOT updated in quick mode."
            ),
        )

    def _print_dry_run_plan(self, args: argparse.Namespace) -> None:
        if getattr(args, "quick", None):
            print(f"  Would run quick mode tests for module: {args.quick}")
            print(f"  Would match: backend/tests/test_{args.quick}*.py")
            print("  Would NOT update snapshot (quick mode).")
        else:
            print(f"  Would run: python -m pytest {TEST_DIR} -q --tb=no")
            print("  Would compare against previous snapshot in .log/pytest_snapshot_*.txt")
            print("  Would save today's snapshot to .log/pytest_snapshot_YYYY-MM-DD.txt")

    def _run_quick(self, args: argparse.Namespace) -> TlmResult:
        """Quick mode: run only tests for a specific module, no snapshot update."""
        module = args.quick
        test_dir = REPO_ROOT / TEST_DIR
        if not test_dir.exists():
            return TlmResult.skip(self.name, f"Test directory not found: {TEST_DIR}")

        matched = sorted(test_dir.glob(f"test_{module}*.py"))
        if not matched:
            return TlmResult.skip(
                self.name,
                f"(quick mode) No test files found matching test_{module}*.py",
            )

        cmd = [sys.executable, "-m", "pytest"] + [str(p) for p in matched] + ["-q", "--tb=line"]
        try:
            import pytest_timeout  # noqa: F401
            cmd.append("--timeout=30")
        except ImportError:
            pass

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            return TlmResult.tool_error(self.name, "pytest (quick) timed out after 120s")
        except FileNotFoundError:
            return TlmResult.skip(self.name, "python/pytest not found")

        passed, skipped, failed, failed_tests = _parse_pytest_output(proc.stdout, proc.stderr)
        findings: list[TlmFinding] = []
        for t in failed_tests:
            findings.append(
                TlmFinding(
                    item=t,
                    detail=f"Failing (quick mode — module: {module})",
                    severity="P1",
                )
            )
        ok_msg = (
            f"{passed} passed, {skipped} skipped, {failed} failed "
            f"(quick mode — {len(matched)} file(s) matched test_{module}*.py)"
        )
        return self.build_result(self.name, findings, ok_msg)

    def run(self, args: argparse.Namespace) -> TlmResult:
        # Delegate to quick mode if --quick was passed
        if getattr(args, "quick", None):
            return self._run_quick(args)

        test_path = REPO_ROOT / TEST_DIR
        if not test_path.exists():
            return TlmResult.skip(self.name, f"Test directory not found: {TEST_DIR}")

        # Build pytest command — omit --timeout if pytest-timeout not available
        cmd = [sys.executable, "-m", "pytest", str(test_path), "-q", "--tb=line"]
        try:
            import pytest_timeout  # noqa: F401
            cmd.append("--timeout=60")
        except ImportError:
            pass  # run without timeout

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                timeout=300,
            )
        except subprocess.TimeoutExpired:
            return TlmResult.tool_error(self.name, "pytest timed out after 300s")
        except FileNotFoundError:
            return TlmResult.skip(self.name, "python/pytest not found")

        passed, skipped, failed, failed_tests = _parse_pytest_output(
            proc.stdout, proc.stderr
        )

        # Load previous snapshot
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=1)
        prev_path = LOG_DIR / f"pytest_snapshot_{yesterday.isoformat()}.txt"
        prev_data = _parse_snapshot(prev_path)
        first_run = not prev_data

        # Save today's snapshot
        today_path = LOG_DIR / f"pytest_snapshot_{today.isoformat()}.txt"
        _save_snapshot(today_path, passed, skipped, failed, failed_tests)

        findings: list[TlmFinding] = []

        if first_run:
            if failed:
                for t in failed_tests:
                    findings.append(
                        TlmFinding(
                            item=t,
                            detail="Test failing (baseline run — no prior snapshot)",
                            severity="P1",
                        )
                    )
                if not findings:
                    findings.append(
                        TlmFinding(
                            item="suite",
                            detail=f"{failed} test(s) failing on baseline run",
                            severity="P1",
                        )
                    )
            ok_msg = f"{passed} passed, {skipped} skipped, {failed} failed — baseline established"
        else:
            prev_failed = int(prev_data.get("failed", 0))
            prev_failed_tests: set[str] = set(
                t for t in prev_data.get("failed_tests", "").split(",") if t
            )

            # Any test failing today that was NOT failing yesterday = P1
            new_failures = [t for t in failed_tests if t not in prev_failed_tests]
            for t in new_failures:
                findings.append(
                    TlmFinding(
                        item=t,
                        detail="Was passing yesterday, failing today",
                        severity="P1",
                    )
                )

            # If failed count increased but we can't name the tests, still flag P1
            if failed > prev_failed and not new_failures and failed_tests:
                for t in failed_tests:
                    findings.append(
                        TlmFinding(
                            item=t,
                            detail=f"Failing (was {prev_failed} failed yesterday, now {failed})",
                            severity="P1",
                        )
                    )
            elif failed > prev_failed and not failed_tests:
                findings.append(
                    TlmFinding(
                        item="suite",
                        detail=f"Failed count increased: {prev_failed} -> {failed}",
                        severity="P1",
                    )
                )

            ok_msg = f"{passed} passed, {skipped} skipped, {failed} failed"

        return self.build_result(self.name, findings, ok_msg)


if __name__ == "__main__":
    tool = PyCheck()
    sys.exit(tool.main())
