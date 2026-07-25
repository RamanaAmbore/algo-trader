"""
CONNCHECK — Broker connection issue threshold checker.

Runs scripts/check_broker_conn_issues.py and maps exit codes to TlmResult:
  0 → ok
  1 → P1 (threshold exceeded)
  2 → P2 (warn-level exceeded)
  3 → skip (DB unreachable or config error)
"""
from __future__ import annotations

import argparse
import subprocess

from _base import TlmFinding, TlmResult, TlmTool, REPO_ROOT, VENV_PYTHON


class ConnCheck(TlmTool):
    name = "CONNCHECK"
    description = "Broker connection issue threshold checker (broker_issue_daily)"

    def run(self, args: argparse.Namespace) -> TlmResult:
        script = REPO_ROOT / "scripts" / "check_broker_conn_issues.py"

        if not script.exists():
            return TlmResult.skip(self.name, "check_broker_conn_issues.py not found")

        cmd = [VENV_PYTHON, str(script)]
        if args.dry_run:
            cmd.append("--dry-run")

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(REPO_ROOT),
            )
        except subprocess.TimeoutExpired:
            return TlmResult.tool_error(self.name, "check_broker_conn_issues.py timed out")
        except Exception as exc:
            return TlmResult.tool_error(self.name, str(exc))

        output = (proc.stdout + proc.stderr).strip()

        if proc.returncode == 3:
            return TlmResult.skip(self.name, f"DB unreachable — {output[:120]}")

        if proc.returncode == 0:
            return TlmResult(
                tool=self.name,
                status="ok",
                severity="",
                summary=output or "no threshold breaches",
                findings=[],
                exit_code=0,
            )

        # Parse output lines for findings
        findings = []
        severity_overall = "P2"
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            sev = "P1" if line.startswith("P1") else "P2"
            if sev == "P1":
                severity_overall = "P1"
            findings.append(TlmFinding(
                item=line.split()[0] if line else "broker",
                detail=line,
                severity=sev,
            ))

        return self.build_result(self.name, findings, "ok")


if __name__ == "__main__":
    raise SystemExit(ConnCheck().main())
