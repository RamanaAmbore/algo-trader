# RamboQ-TLM — Technical Lifecycle Management Toolbox

A named, documented suite of standalone monitoring and quality tools for the RamboQuant
codebase. Each tool is a first-class citizen with a consistent CLI interface and structured
JSON exit reports. A master orchestrator (`run_all.py`) ties them together.

---

## Why TLM?

TLM codifies the recurring quality checks that would otherwise be done ad hoc:

- Catching cyclomatic complexity regressions before they become unmaintainable
- Tracking test health across daily development cycles
- Scanning for known CVEs in Python and frontend dependencies
- Detecting architectural changes that were not reflected in the design documentation
- Serving as the foundation for a daily cron-based audit pipeline

---

## Tool Catalog

| Name | Script | Purpose | P-levels | Exit codes |
|------|--------|---------|----------|------------|
| CCWATCH | `ccwatch.py` | Cyclomatic complexity snapshot + grade regression | P2 (new/regressed F), P3 (existing F tracked) | 0=ok, 1=P2 found, 2=tool error |
| PYCHECK | `pycheck.py` | Pytest baseline tracker — detects new failures | P1 (new test failure) | 0=ok, 1=P1 found, 2=tool error |
| PERFMON | `perfmon.py` | Performance regression monitor (stub — delegates to `tools/perf_regression.py`) | P2 (regression) | 0=ok, 1=regression, 2=skip/error |
| SNAPCHECK | `snapcheck.py` | Stale closed-hours snapshot detector (stub — delegates to `scripts/check_stale_snapshots.py`) | P1 (stale snapshot) | 0=ok, 1=P1 found, 2=skip/error |
| DEPSCAN | `depscan.py` | pip-audit + npm audit security scan | P1 (CRITICAL/HIGH CVE), P2 (MEDIUM), P3 (LOW) | 0=ok, 1=P1/P2 found, 2=tool error |
| DOCDRIFT | `docdrift.py` | DESIGN_GUIDE.md vs recent architectural commit drift | P3 (doc not updated in same window) | 0=ok, 1=not used for P3, 2=tool error |

### Severity / Exit code semantics

| Status | Severity | Exit code | Meaning |
|--------|----------|-----------|---------|
| ok | (none) | 0 | All checks passed |
| warn | P3 | 0 | Minor findings; no action required immediately |
| warn | P2 | 1 | Significant findings; investigate before next release |
| fail | P1 | 1 | Critical finding; block release or alert on-call |
| skip | (none) | 2 | Tool could not run (dependency missing) |

---

## Running Individual Tools

All tools share a common CLI:

```bash
# Basic run
python tools/tlm/ccwatch.py

# Dry run — describe what would be checked, no side effects
python tools/tlm/ccwatch.py --dry-run

# JSON output
python tools/tlm/ccwatch.py --json

# Git look-back window (for DOCDRIFT, PYCHECK git-aware checks)
python tools/tlm/docdrift.py --since 7
```

### Tool-specific examples

```bash
# Cyclomatic complexity — compare today vs yesterday
python tools/tlm/ccwatch.py

# Pytest tracker — compare pass/fail counts
python tools/tlm/pycheck.py

# Dependency CVE scan
python tools/tlm/depscan.py

# Doc drift — last 7 days
python tools/tlm/docdrift.py --since 7
```

---

## Running the Master Orchestrator

```bash
# Run all tools
python tools/tlm/run_all.py

# Run specific tools only
python tools/tlm/run_all.py --tools ccwatch,pycheck,depscan

# Run with 7-day git window
python tools/tlm/run_all.py --since 7

# Write markdown audit report to docs/audits/
python tools/tlm/run_all.py --output docs/audits/AUDIT_TLM_2026-07-11.md

# Write markdown + JSON
python tools/tlm/run_all.py --output docs/audits/AUDIT_TLM.md --json

# Write report and auto-commit
python tools/tlm/run_all.py --output docs/audits/AUDIT_TLM.md --auto-commit

# Dry run — plan only, no network/disk/test side effects
python tools/tlm/run_all.py --dry-run
```

### Consolidated output example

```
RAMBOQ-TLM  2026-07-11 16:03 IST
============================================================
  v CCWATCH    ok      No CC regressions (9 F-grade functions tracked)
  v PYCHECK    ok      2264 passed, 13 skipped, 0 failed
  ~ PERFMON    warn    [P2]  2 routes >20% regression (P2)
  ! SNAPCHECK  FAIL    [P1]  nav snapshot stale since 2026-07-09 (P1)
  v DEPSCAN    ok      No HIGH/CRITICAL CVEs found
  ~ DOCDRIFT   warn    [P3]  3 architectural commits without DESIGN_GUIDE update

Overall: FAIL (1 P1 finding)
```

---

## Output Locations

| Artifact | Path | Retention |
|----------|------|-----------|
| CCWATCH daily snapshot | `.log/cc_snapshot_YYYY-MM-DD.txt` | Keep last 30 days (manual purge) |
| PYCHECK daily snapshot | `.log/pytest_snapshot_YYYY-MM-DD.txt` | Keep last 30 days (manual purge) |
| TLM audit reports | `docs/audits/AUDIT_TLM_YYYY-MM-DD.md` | Committed to repo |
| TLM JSON summaries | `docs/audits/AUDIT_TLM_YYYY-MM-DD.json` | Optional, alongside .md |

---

## Adding a New Tool

1. Create `tools/tlm/mytool.py`.
2. Add the shebang: `#!/usr/bin/env python3`
3. Add the path fix at the top:
   ```python
   import sys
   from pathlib import Path
   sys.path.insert(0, str(Path(__file__).resolve().parent))
   ```
4. Subclass `TlmTool`:
   ```python
   from _base import TlmTool, TlmResult, TlmFinding

   class MyTool(TlmTool):
       name = "MYTOOL"
       description = "One-line description."

       def _print_dry_run_plan(self, args):
           print("  Would check X and Y...")

       def run(self, args) -> TlmResult:
           findings = []
           # ... populate findings ...
           return self.build_result(self.name, findings, "All checks passed")

   if __name__ == "__main__":
       tool = MyTool()
       sys.exit(tool.main())
   ```
5. Register it in `run_all.py`:
   ```python
   from mytool import MyTool
   ALL_TOOLS = [..., MyTool()]
   ```
6. Make it executable: `chmod +x tools/tlm/mytool.py`
7. Update this README's tool catalog table.

---

## Daily Cron Integration

Add to crontab (runs at 08:45 IST = 03:15 UTC, after market cache warm):

```cron
15 3 * * 1-5 cd /opt/ramboq && python tools/tlm/run_all.py \
  --output docs/audits/AUDIT_TLM_$(date +\%Y-\%m-\%d).md \
  --json \
  >> .log/tlm_cron.log 2>&1
```

The orchestrator exits 1 if any P1/P2 finding is present — suitable for alerting via
systemd OnFailure or a monitoring wrapper.

---

## Relationship to Other Quality Tools

| Tool | Location | When to run |
|------|----------|-------------|
| TLM (this toolbox) | `tools/tlm/` | Daily cron + pre-deploy |
| Performance baseline | `tools/perf_baseline.py` | Before + after major changes |
| Playwright e2e | `playwright/` | On every frontend change |
| pytest | `backend/tests/` | On every backend change |
| MCP audit | `/admin/research` | Continuous (web UI) |
