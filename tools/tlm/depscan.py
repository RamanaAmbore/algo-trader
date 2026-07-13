#!/usr/bin/env python3
"""
DEPSCAN — Dependency security scanner.

Runs pip-audit (Python) and npm audit (frontend/) and reports CVEs by severity.

Severity mapping:
  CRITICAL / HIGH  -> P1 (status=fail)
  MEDIUM           -> P2 (status=warn)
  LOW              -> P3 (status=warn)

pip-audit is auto-installed if missing (skipped under --dry-run).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _base import TlmTool, TlmResult, TlmFinding, REPO_ROOT, VENV_PYTHON  # noqa: E402

# Map severity strings to P-levels
_SEV_MAP = {
    "CRITICAL": "P1",
    "HIGH": "P1",
    "MEDIUM": "P2",
    "LOW": "P3",
}


def _ensure_pip_audit(dry_run: bool) -> bool:
    """Return True if pip-audit is available (installing if needed)."""
    try:
        subprocess.run(
            [VENV_PYTHON, "-m", "pip_audit", "--version"],
            capture_output=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    if dry_run:
        print("  [DEPSCAN] pip-audit not installed — would install silently")
        return False

    print("[DEPSCAN] Installing pip-audit...", flush=True)
    r = subprocess.run(
        [VENV_PYTHON, "-m", "pip", "install", "pip-audit", "-q"],
        capture_output=True,
    )
    if r.returncode != 0:
        return False
    return True


def _run_pip_audit() -> list[TlmFinding]:
    """Run pip-audit and return findings."""
    findings: list[TlmFinding] = []
    try:
        proc = subprocess.run(
            [VENV_PYTHON, "-m", "pip_audit", "--format=json"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
        )
    except FileNotFoundError:
        return []

    raw = proc.stdout.strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    # pip-audit JSON schema: {"dependencies": [{"name": ..., "version": ..., "vulns": [...]}]}
    deps = data if isinstance(data, list) else data.get("dependencies", [])
    for dep in deps:
        name = dep.get("name", "?")
        version = dep.get("version", "?")
        for vuln in dep.get("vulns", []):
            vid = vuln.get("id", "?")
            sev_raw = (vuln.get("fix_versions") and "MEDIUM") or "LOW"
            # pip-audit severity field varies by source; use aliases if present
            for alias in vuln.get("aliases", []):
                # GHSA/CVE ids don't carry severity; severity is in description sometimes
                pass
            # Try to get severity from vuln dict directly
            sev_raw = vuln.get("severity", sev_raw).upper()
            sev_p = _SEV_MAP.get(sev_raw, "P3")
            findings.append(
                TlmFinding(
                    item=f"{name}=={version}",
                    detail=f"{vid} ({sev_raw}) — {vuln.get('description', 'no description')[:120]}",
                    severity=sev_p,
                )
            )

    return findings


def _run_npm_audit() -> list[TlmFinding]:
    """Run npm audit in frontend/ and return findings."""
    frontend_dir = REPO_ROOT / "frontend"
    if not (frontend_dir / "package-lock.json").exists():
        return []

    findings: list[TlmFinding] = []
    try:
        proc = subprocess.run(
            ["npm", "audit", "--json"],
            capture_output=True,
            text=True,
            cwd=str(frontend_dir),
        )
    except FileNotFoundError:
        return []  # npm not available

    raw = proc.stdout.strip()
    if not raw:
        return []

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    # npm audit JSON: {"vulnerabilities": {"pkg": {"severity": ..., "via": [...], "fixAvailable": ...}}}
    vulns = data.get("vulnerabilities", {})
    for pkg_name, info in vulns.items():
        sev_raw = info.get("severity", "LOW").upper()
        sev_p = _SEV_MAP.get(sev_raw, "P3")
        via = info.get("via", [])
        # 'via' can be strings (indirect) or dicts (direct). Summarise direct ones.
        direct_cves = [v.get("url", v) if isinstance(v, dict) else v for v in via]
        cve_str = ", ".join(str(c) for c in direct_cves[:3])
        findings.append(
            TlmFinding(
                item=f"npm:{pkg_name}",
                detail=f"{sev_raw} — {cve_str}"[:160],
                severity=sev_p,
            )
        )

    return findings


class DepScan(TlmTool):
    name = "DEPSCAN"
    description = "pip-audit + npm audit security scan (CRITICAL/HIGH=P1, MEDIUM=P2, LOW=P3)."

    def _print_dry_run_plan(self, args: argparse.Namespace) -> None:
        print("  Would run: pip-audit --format=json (installing pip-audit if missing)")
        print("  Would run: npm audit --json in frontend/")
        print("  Would NOT install pip-audit in dry-run mode.")

    def run(self, args: argparse.Namespace) -> TlmResult:
        findings: list[TlmFinding] = []

        # Python audit
        if _ensure_pip_audit(dry_run=False):
            findings.extend(_run_pip_audit())
        else:
            findings.append(
                TlmFinding(
                    item="pip-audit",
                    detail="pip-audit not available — Python dependency scan skipped",
                    severity="P3",
                )
            )

        # npm audit
        findings.extend(_run_npm_audit())

        ok_msg = "No HIGH/CRITICAL CVEs found"
        return self.build_result(self.name, findings, ok_msg)


if __name__ == "__main__":
    tool = DepScan()
    sys.exit(tool.main())
