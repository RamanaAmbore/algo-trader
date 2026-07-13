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

_ACCEPT_FILE = Path(__file__).resolve().parent / "depscan_accept.yaml"


def _load_accepted_ids() -> set[str]:
    """Return set of CVE/advisory IDs listed in depscan_accept.yaml."""
    if not _ACCEPT_FILE.exists():
        return set()
    try:
        import re
        text = _ACCEPT_FILE.read_text()
        return set(re.findall(r"^\s+- id:\s*(\S+)", text, re.MULTILINE))
    except Exception:
        return set()


_ACCEPTED: set[str] = _load_accepted_ids()


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
            # Skip IDs accepted in depscan_accept.yaml
            if vid in _ACCEPTED:
                continue
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
    import re as _re

    vulns = data.get("vulnerabilities", {})

    # Build transitive closure of accepted packages.
    # Seed: packages whose direct advisory ID is in _ACCEPTED.
    # Iterate: if all via-string deps of a package are already accepted, accept it too.
    accepted_pkgs: set[str] = set()
    for pkg_name, info in vulns.items():
        via = info.get("via", [])
        for v in via:
            if not isinstance(v, dict):
                continue
            ids: set[str] = set()
            if gid := v.get("ghsaId"):
                ids.add(gid)
            for m in _re.findall(r"GHSA-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+", v.get("url", "")):
                ids.add(m)
            if ids & _ACCEPTED:
                accepted_pkgs.add(pkg_name)
                break
    # Propagate transitively (iterate until stable)
    prev_size = -1
    while prev_size != len(accepted_pkgs):
        prev_size = len(accepted_pkgs)
        for pkg_name, info in vulns.items():
            if pkg_name in accepted_pkgs:
                continue
            via = info.get("via", [])
            via_strings = [v for v in via if isinstance(v, str)]
            if via_strings and all(p in accepted_pkgs for p in via_strings):
                accepted_pkgs.add(pkg_name)

    for pkg_name, info in vulns.items():
        sev_raw = info.get("severity", "LOW").upper()
        sev_p = _SEV_MAP.get(sev_raw, "P3")
        via = info.get("via", [])
        # 'via' can be strings (indirect deps) or dicts (direct advisories).
        direct_cves = [v.get("url", v) if isinstance(v, dict) else v for v in via]

        # Collect advisory IDs for direct acceptance check
        advisory_ids: set[str] = set()
        for v in via:
            if not isinstance(v, dict):
                continue
            if gid := v.get("ghsaId"):
                advisory_ids.add(gid)
            for m in _re.findall(r"GHSA-[a-z0-9]+-[a-z0-9]+-[a-z0-9]+", v.get("url", "")):
                advisory_ids.add(m)

        # Suppress if direct advisory is accepted OR all indirect via refs are accepted pkgs
        if advisory_ids & _ACCEPTED:
            continue
        via_strings = [v for v in via if isinstance(v, str)]
        if via_strings and all(p in accepted_pkgs for p in via_strings):
            continue

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
