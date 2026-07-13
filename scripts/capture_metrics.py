"""
Code-metrics capture — Phase 1.

Captures eight codebase-health metrics and writes one
`code_metrics_snapshots` row keyed by `release_tag`. Designed to be
run either:

  - Manually by the operator:
      python scripts/capture_metrics.py --release-tag manual-2026-06-30

  - Out of the deploy pipeline (webhook/deploy.sh, dev-branch deploys
    fall under `dev-<short-sha>` tags so they don't pollute the main
    release trend):
      python scripts/capture_metrics.py --release-tag "$(git describe --tags --abbrev=0)"

Idempotency: if a row for `release_tag` already exists the script
logs + exits 0 unless `--force` is passed (which UPDATEs in place
rather than INSERTing a second row — keeps the chart history clean).

Tools probed (each one is OPTIONAL — missing tool → metric set to
None rather than failing the snapshot):

  Backend:
    radon cc -j          → cyclomatic complexity (avg, max)
    radon raw -j         → lines-of-code
    vulture --min-confidence 80  → stale-code count
    pytest --cov=backend --cov-report=json --no-cov-on-fail
                          (only when --with-coverage is passed; without
                           it, coverage column stays at the previous
                           snapshot's value or NULL — too slow to run
                           on every capture)
    pytest-json-report   → per-test durations (--with-test-times)

  Frontend:
    find frontend/src -name '*.svelte' -o '*.js' → loc via wc
    jscpd                → duplicated lines (npx, no global install)
    eslint complexity rule → complexity avg/max (npx if config available)
    Vitest coverage      → frontend_coverage_pct (best-effort)
    Playwright JSON reporter → per-test durations (--with-test-times)

  Cross-cutting:
    git log              → bug commit heuristic (fix:|fix(|fix |bug:|URGENT|P0)
                           between current HEAD and previous tag
    Playwright perf spec → per-page DCL / Idle / LCP (best-effort —
                           the script tries the spec's own JSON output
                           if present at /tmp/ramboq_perf.json, else
                           leaves the column at {}).

Exit codes:
  0 — snapshot written (or skipped because tag exists and --force not passed)
  1 — fatal error (DB unreachable, git missing, etc.)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Optional

# Make the script runnable from the repo root via either
#   python scripts/capture_metrics.py
#   python -m scripts.capture_metrics
# Add the repo root to sys.path BEFORE the first backend.* import so
# `python scripts/capture_metrics.py` (without -m) resolves correctly.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Shell helpers (no Bash tool needed at runtime; pure-python subprocess)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, cwd: Optional[Path] = None, timeout: int = 600) -> tuple[int, str, str]:
    """Run a command capturing stdout/stderr. Returns (rc, stdout, stderr).
    Never raises — caller decides whether a non-zero rc is fatal."""
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd) if cwd else None,
            capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        return 127, "", f"tool not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout after {timeout}s"
    except Exception as e:  # noqa: BLE001
        return 1, "", f"{type(e).__name__}: {e}"


def _resolve_tool(name: str) -> Optional[str]:
    """Return absolute path to a tool, searching the running
    interpreter's venv `bin/` directory FIRST (so a venv-installed
    radon / vulture wins over a stale system copy), then `PATH`.
    Returns None if not found."""
    # sys.executable points at e.g. /Users/.../venv/bin/python — the
    # adjacent `bin/<name>` is where pip-installed CLIs live.
    venv_bin = Path(sys.executable).parent
    candidate = venv_bin / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which(name)


def _tool_available(name: str) -> bool:
    return _resolve_tool(name) is not None


# ---------------------------------------------------------------------------
# Backend metrics
# ---------------------------------------------------------------------------


def _radon_cc(target: Path) -> tuple[Optional[float], Optional[int], dict]:
    """Cyclomatic complexity via `radon cc -j`. Returns (avg, max, raw)."""
    radon = _resolve_tool("radon")
    if not radon:
        return None, None, {"_skipped": "radon not installed"}
    rc, out, err = _run([radon, "cc", "-j", str(target)])
    if rc != 0:
        return None, None, {"_error": err[:500]}
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError as e:
        return None, None, {"_error": f"json parse failed: {e}"}
    scores: list[int] = []
    for _file, blocks in payload.items():
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            try:
                scores.append(int(b.get("complexity", 0)))
            except (TypeError, ValueError):
                continue
    if not scores:
        return 0.0, 0, payload
    return round(mean(scores), 2), max(scores), payload


def _radon_raw(target: Path) -> tuple[Optional[int], dict]:
    """Lines of code via `radon raw -j`. Sums SLOC (source lines) across files."""
    radon = _resolve_tool("radon")
    if not radon:
        return None, {"_skipped": "radon not installed"}
    rc, out, err = _run([radon, "raw", "-j", str(target)])
    if rc != 0:
        return None, {"_error": err[:500]}
    try:
        payload = json.loads(out or "{}")
    except json.JSONDecodeError as e:
        return None, {"_error": f"json parse failed: {e}"}
    loc = 0
    for _file, stats in payload.items():
        if not isinstance(stats, dict):
            continue
        try:
            loc += int(stats.get("sloc", 0))
        except (TypeError, ValueError):
            continue
    return loc, payload


def _vulture(target: Path) -> tuple[Optional[int], str]:
    """Stale-code findings via `vulture --min-confidence 80`. Returns (count, raw stdout).
    vulture exits non-zero when it finds dead code — that's the normal
    case (rc=1 if findings, rc=3 if internal error). We treat anything
    rc <= 2 as 'tool ran fine, parse output'.
    """
    vulture = _resolve_tool("vulture")
    if not vulture:
        return None, "vulture not installed"
    rc, out, err = _run([vulture, "--min-confidence", "80", str(target)])
    # vulture's exit semantics: 0 = no findings, 1 = invocation/parse
    # error, 2 = configuration error, 3 = found dead code (the common
    # case). Treat rc in (0, 3) as a successful run; anything else is
    # a real failure.
    if rc not in (0, 3):
        return None, f"vulture rc={rc}: {err[:300]}"
    # Each line of stdout = one finding. Filter blank lines.
    findings = [ln for ln in (out or "").splitlines() if ln.strip()]
    return len(findings), out[:50_000]  # cap raw payload for forensics


def _pytest_coverage(target: Path) -> tuple[Optional[float], dict]:
    """Run pytest --cov and parse the JSON report. SLOW (full test suite),
    so only invoked when --with-coverage is passed. Returns (pct, summary).
    """
    json_path = Path("/tmp/ramboq_cov.json")
    if json_path.exists():
        json_path.unlink()
    rc, out, err = _run(
        [
            sys.executable, "-m", "pytest",
            f"--cov={target.name}",
            "--cov-report=json:/tmp/ramboq_cov.json",
            "--no-cov-on-fail",
            "-q", "--tb=no",
        ],
        cwd=ROOT, timeout=1800,
    )
    # pytest rc may be non-zero if tests fail — that's fine for the
    # purpose of capturing coverage % (we want the number, not a pass/fail).
    if not json_path.exists():
        return None, {"_error": err[-500:] or out[-500:]}
    try:
        payload = json.loads(json_path.read_text())
        pct = float(payload.get("totals", {}).get("percent_covered", 0.0))
        return round(pct, 2), {"summary": payload.get("totals", {})}
    except Exception as e:  # noqa: BLE001
        return None, {"_error": f"cov json parse failed: {e}"}


# ---------------------------------------------------------------------------
# Frontend metrics
# ---------------------------------------------------------------------------


def _frontend_loc(target: Path) -> tuple[Optional[int], dict]:
    """Frontend LOC via `wc -l` across .svelte / .js / .ts inside frontend/src.
    Excludes node_modules, .svelte-kit, build artifacts."""
    if not target.exists():
        return None, {"_skipped": "frontend/src not found"}
    total = 0
    by_ext: dict[str, int] = {".svelte": 0, ".js": 0, ".ts": 0, ".css": 0}
    for ext in by_ext.keys():
        n = 0
        for p in target.rglob(f"*{ext}"):
            # rglob already gives only files; exclude generated dirs
            if any(seg in {"node_modules", ".svelte-kit", "build"} for seg in p.parts):
                continue
            try:
                with p.open("rb") as fh:
                    n += sum(1 for _ in fh)
            except OSError:
                continue
        by_ext[ext] = n
        total += n
    return total, {"by_ext": by_ext}


def _jscpd(target: Path) -> tuple[Optional[int], dict]:
    """Duplicated lines via `npx jscpd`. Best-effort — if npx not
    installed or the project doesn't yet have jscpd configured this
    returns None. jscpd writes a JSON report to .log/jscpd_report
    that we parse."""
    if not _tool_available("npx"):
        return None, {"_skipped": "npx not installed"}
    report_dir = ROOT / ".log" / "jscpd_report"
    report_dir.mkdir(parents=True, exist_ok=True)
    rc, out, err = _run(
        [
            "npx", "--yes", "jscpd",
            "--silent",
            "--reporters", "json",
            "--output", str(report_dir),
            str(target),
        ],
        cwd=ROOT, timeout=600,
    )
    if rc not in (0, 1):
        return None, {"_error": (err or out)[:500]}
    # jscpd writes report_dir/jscpd-report.json
    json_path = report_dir / "jscpd-report.json"
    if not json_path.exists():
        return None, {"_error": "jscpd JSON report missing"}
    try:
        payload = json.loads(json_path.read_text())
        total = int(payload.get("statistics", {}).get("total", {}).get("duplicatedLines", 0))
        return total, {"statistics": payload.get("statistics", {})}
    except Exception as e:  # noqa: BLE001
        return None, {"_error": f"jscpd parse failed: {e}"}


def _parse_eslint_scores(results: list) -> list[int]:
    """Extract complexity scores from ESLint JSON results."""
    scores: list[int] = []
    for f in results:
        for msg in f.get("messages", []):
            if msg.get("ruleId") != "complexity":
                continue
            text = msg.get("message", "")
            for token in text.split():
                if token.rstrip(".").isdigit():
                    try:
                        scores.append(int(token.rstrip(".")))
                    except ValueError:
                        continue
                    break
    return scores


def _eslint_complexity(target: Path) -> tuple[Optional[float], Optional[int], dict]:
    """ESLint complexity via `npx eslint --rule "complexity: [error, 1]"`.
    Best-effort. Returns (avg, max, raw)."""
    if not _tool_available("npx"):
        return None, None, {"_skipped": "npx not installed"}
    if not target.exists():
        return None, None, {"_skipped": "frontend/src not found"}
    rc, out, err = _run(
        [
            "npx", "--no-install", "eslint",
            "--format", "json",
            "--no-eslintrc",
            "--rule", "complexity: [warn, 1]",
            "--rulesdir", "/dev/null",  # suppress any rulesdir lookup
            "--ext", ".js,.svelte",
            str(target),
        ],
        cwd=ROOT, timeout=300,
    )
    if rc not in (0, 1):
        return None, None, {"_error": (err or out)[:500]}
    try:
        results = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None, None, {"_error": "eslint JSON parse failed"}
    scores = _parse_eslint_scores(results)
    if not scores:
        return 0.0, 0, {"_note": "no complexity warnings"}
    return round(mean(scores), 2), max(scores), {"sample_count": len(scores)}


def _eslint_unused(target: Path) -> tuple[Optional[int], dict]:
    """Frontend stale-code count via ESLint `no-unused-vars`. Best-effort."""
    if not _tool_available("npx"):
        return None, {"_skipped": "npx not installed"}
    if not target.exists():
        return None, {"_skipped": "frontend/src not found"}
    rc, out, err = _run(
        [
            "npx", "--no-install", "eslint",
            "--format", "json",
            "--no-eslintrc",
            "--rule", "no-unused-vars: warn",
            "--ext", ".js,.svelte",
            str(target),
        ],
        cwd=ROOT, timeout=300,
    )
    if rc not in (0, 1):
        return None, {"_error": (err or out)[:500]}
    try:
        results = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None, {"_error": "eslint JSON parse failed"}
    count = 0
    for f in results:
        for msg in f.get("messages", []):
            if msg.get("ruleId") == "no-unused-vars":
                count += 1
    return count, {"file_count": len(results)}


# ---------------------------------------------------------------------------
# Test execution-time metrics
# ---------------------------------------------------------------------------

# Tests that take longer than this are surfaced in `slow_count` and listed
# in `top_10_slowest`. 1 s is the threshold recommended for unit tests;
# integration / async tests that hit DB or broker are naturally slower, but
# we want the operator to be AWARE of them accumulating over releases.
_SLOW_TEST_THRESHOLD_S: float = 1.0


def _aggregate_durations(durations: list[float], names: list[str]) -> dict:
    """Given parallel lists of per-test durations (seconds) and test names,
    return the aggregated timing dict stored in `test_response_times`.

    Returned dict shape:
        total_tests       — total number of tests observed
        total_wall_time_s — sum of all durations (approx wall time for
                            sequential run; not the same as process wall
                            time when tests run in parallel)
        median_s          — median test duration (better than mean for
                            skewed suites)
        max_s             — slowest single test
        top_10_slowest    — [{name, duration_s}, …] sorted slowest-first
        slow_count        — number of tests exceeding _SLOW_TEST_THRESHOLD_S
        slow_threshold_s  — the threshold used (for future-proofing if we
                            ever make it configurable)
    """
    if not durations:
        return {
            "total_tests": 0,
            "total_wall_time_s": 0.0,
            "median_s": 0.0,
            "max_s": 0.0,
            "top_10_slowest": [],
            "slow_count": 0,
            "slow_threshold_s": _SLOW_TEST_THRESHOLD_S,
        }

    n = len(durations)
    total = round(sum(durations), 3)
    sorted_dur = sorted(durations)
    mid = n // 2
    median = sorted_dur[mid] if n % 2 else (sorted_dur[mid - 1] + sorted_dur[mid]) / 2
    max_d = max(durations)
    slow_count = sum(1 for d in durations if d >= _SLOW_TEST_THRESHOLD_S)

    # Build top-10 from parallel lists, sorted slowest-first.
    paired = sorted(zip(durations, names), key=lambda x: x[0], reverse=True)
    top10 = [
        {"name": nm, "duration_s": round(dur, 4)}
        for dur, nm in paired[:10]
    ]

    return {
        "total_tests": n,
        "total_wall_time_s": total,
        "median_s": round(median, 4),
        "max_s": round(max_d, 4),
        "top_10_slowest": top10,
        "slow_count": slow_count,
        "slow_threshold_s": _SLOW_TEST_THRESHOLD_S,
    }


def _pytest_json_report(out_path: Path) -> "tuple[dict, dict] | None":
    """Run pytest with --json-report and parse durations. Returns (agg, meta) or None on miss."""
    if out_path.exists():
        out_path.unlink()
    rc, out, err = _run(
        [
            sys.executable, "-m", "pytest",
            "--json-report",
            f"--json-report-file={out_path}",
            "--json-report-summary",
            "-q", "--tb=no",
            str(ROOT / "backend" / "tests"),
        ],
        cwd=ROOT, timeout=600,
    )
    if not out_path.exists():
        return None
    try:
        report = json.loads(out_path.read_text())
        durations: list[float] = []
        names: list[str] = []
        for t in report.get("tests", []):
            dur = t.get("call", {}).get("duration", None)
            if dur is None:
                dur = t.get("duration", None)
            if dur is not None:
                try:
                    durations.append(float(dur))
                    names.append(t.get("nodeid", "unknown"))
                except (TypeError, ValueError):
                    continue
        return _aggregate_durations(durations, names), {"source": "pytest-json-report"}
    except Exception as e:  # noqa: BLE001
        return {}, {"_error": f"pytest-json-report parse failed: {e}"}


def _pytest_text_durations() -> tuple[dict, dict]:
    """Run pytest --durations=0 and parse text output. Always available."""
    rc, out, err = _run(
        [
            sys.executable, "-m", "pytest",
            "--durations=0",
            "-q", "--tb=no",
            str(ROOT / "backend" / "tests"),
        ],
        cwd=ROOT, timeout=600,
    )
    durations: list[float] = []
    names: list[str] = []
    for line in (out or "").splitlines():
        line = line.strip()
        if "s call" not in line and "s setup" not in line and "s teardown" not in line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            dur = float(parts[0].rstrip("s"))
            node = parts[-1]
            if "::" in node:
                durations.append(dur)
                names.append(node)
        except (ValueError, IndexError):
            continue
    return _aggregate_durations(durations, names), {"source": "pytest --durations=0 fallback"}


def _pytest_durations(json_report_path: Optional[Path] = None) -> tuple[dict, dict]:
    """Run pytest with `--json-report` (via `pytest-json-report`) and parse
    per-test durations.  Returns (agg_dict, meta_dict).

    `pytest-json-report` is an optional dev dependency (listed in
    requirements-api.txt).  When absent, falls back to parsing
    `--durations=0` text output instead — less precise (no per-test
    breakdown) but always available.

    Callers should pass `json_report_path` for testing (monkeypatching).
    When None, uses `/tmp/ramboq_pytest_durations.json`.
    """
    out_path = json_report_path or Path("/tmp/ramboq_pytest_durations.json")

    json_report_pkg = False
    try:
        import importlib
        importlib.import_module("pytest_jsonreport")  # noqa: F401
        json_report_pkg = True
    except ImportError:
        pass

    if json_report_pkg:
        result = _pytest_json_report(out_path)
        if result is not None:
            return result
        return {}, {"_error": "pytest-json-report output missing"}

    return _pytest_text_durations()


def _playwright_durations(json_report_path: Optional[Path] = None) -> tuple[dict, dict]:
    """Parse Playwright's JSON reporter output for per-test durations.

    Playwright writes its JSON report when the reporter is configured as
    `json` in `playwright.config.js`, or when `--reporter=json` is passed.
    The capture script looks for the report at `/tmp/ramboq_pw_report.json`
    (written by the deploy pipeline's test run) or at the path the operator
    can configure.

    Shape of Playwright JSON:
        { suites: [ { specs: [ { tests: [ { results: [{ duration: N_ms }] } ] } ] } ] }

    We parse recursively because specs can be nested in suites.
    Duration in Playwright JSON is in **milliseconds** — we convert to
    seconds for the unified schema.
    """
    candidates = [
        json_report_path,
        Path("/tmp/ramboq_pw_report.json"),
        ROOT / ".log" / "pw_report.json",
        ROOT / "frontend" / "test-results" / "results.json",
    ]
    for c in candidates:
        if c is None or not c.exists():
            continue
        try:
            payload = json.loads(c.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        durations_ms: list[float] = []
        names: list[str] = []

        def _walk_suites(suites: list) -> None:
            for suite in suites or []:
                # Recurse into nested suites.
                _walk_suites(suite.get("suites", []))
                for spec in suite.get("specs", []):
                    spec_title = spec.get("title", "unknown")
                    for test in spec.get("tests", []):
                        test_title = test.get("title", spec_title)
                        for result in test.get("results", []):
                            dur_ms = result.get("duration", None)
                            if dur_ms is not None:
                                try:
                                    durations_ms.append(float(dur_ms))
                                    names.append(f"{spec_title} > {test_title}")
                                except (TypeError, ValueError):
                                    continue

        _walk_suites(payload.get("suites", []))
        # Convert ms → s.
        durations_s = [round(d / 1000, 4) for d in durations_ms]
        return _aggregate_durations(durations_s, names), {"source": str(c)}

    return {}, {"_skipped": "no Playwright JSON report found (run `npx playwright test --reporter=json`)"}


def _per_page_latency() -> tuple[dict, dict]:
    """Best-effort load of the e2e perf spec's JSON output. The spec
    writes /tmp/ramboq_perf.json on each run; if absent we return {} +
    a note (capture still succeeds — latency just stays empty)."""
    candidates = [
        Path("/tmp/ramboq_perf.json"),
        ROOT / ".log" / "perf.json",
        ROOT / "frontend" / ".log" / "perf.json",
    ]
    for c in candidates:
        if c.exists():
            try:
                payload = json.loads(c.read_text())
                if isinstance(payload, dict):
                    return payload, {"source": str(c)}
            except json.JSONDecodeError:
                continue
    return {}, {"_skipped": "no perf JSON found (run main_thread_perf.spec.js first)"}


# ---------------------------------------------------------------------------
# Cross-cutting metrics
# ---------------------------------------------------------------------------


def _git_sha() -> Optional[str]:
    rc, out, _ = _run(["git", "-C", str(ROOT), "rev-parse", "HEAD"])
    if rc != 0:
        return None
    return out.strip() or None


def _previous_release_tag() -> Optional[str]:
    """Walk `git tag --sort=-creatordate` and return the second-most-
    recent tag (the most-recent IS the tag we're capturing). Falls back
    to None if fewer than two tags exist."""
    rc, out, _ = _run(["git", "-C", str(ROOT), "tag", "--sort=-creatordate"])
    if rc != 0:
        return None
    tags = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if len(tags) < 2:
        return None
    return tags[1]


def _count_bug_commits(prev_tag: Optional[str]) -> Optional[int]:
    """Count commits between `prev_tag..HEAD` matching the bug-fix
    heuristic. When prev_tag is None (first release), look at the
    last 30 days instead."""
    if prev_tag:
        rev_range = f"{prev_tag}..HEAD"
    else:
        since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
        rev_range = f"--since={since}"
    rc, out, _ = _run([
        "git", "-C", str(ROOT), "log", rev_range, "--pretty=%s",
    ])
    if rc != 0:
        return None
    needles = ("fix:", "fix(", "fix ", "bug:", "URGENT", "P0")
    count = 0
    for line in (out or "").splitlines():
        low = line.lower()
        for needle in needles:
            # Lowercase compare for fix:/fix(/fix /bug:, exact for URGENT/P0
            if needle in ("URGENT", "P0"):
                if needle in line:
                    count += 1
                    break
            elif needle in low:
                count += 1
                break
    return count


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


async def _write_snapshot(values: dict[str, Any], force: bool) -> tuple[bool, str]:
    """INSERT (or UPDATE if force) a code_metrics_snapshots row. Returns
    (was_written, message). All `backend.*` imports are deferred to
    here so the script can `--help` without a working DB."""
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import CodeMetricsSnapshot

    tag = values["release_tag"]
    async with async_session() as session:
        existing = (await session.execute(
            select(CodeMetricsSnapshot)
            .where(CodeMetricsSnapshot.release_tag == tag)
        )).scalar_one_or_none()

        if existing and not force:
            return False, f"snapshot '{tag}' already exists (pass --force to overwrite)"

        if existing:
            # UPDATE in place — keeps the per-tag uniqueness invariant
            # and avoids polluting the trend chart with duplicates.
            for k, v in values.items():
                if k == "release_tag":
                    continue
                setattr(existing, k, v)
            # Stamp captured_at to NOW on force-overwrite so the chart
            # reflects the latest run, not the original.
            existing.captured_at = datetime.now(timezone.utc)
            row_id = existing.id
        else:
            row = CodeMetricsSnapshot(**values)
            session.add(row)
            await session.flush()
            row_id = row.id
        await session.commit()
        return True, f"snapshot '{tag}' written (id={row_id})"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _capture(
    release_tag: str,
    *,
    with_coverage: bool,
    with_test_times: bool,
    force: bool,
    notes: str = "",
) -> int:
    backend_dir  = ROOT / "backend"
    frontend_dir = ROOT / "frontend" / "src"

    print(f"[capture_metrics] release_tag={release_tag}", file=sys.stderr)
    print(f"[capture_metrics] root={ROOT}", file=sys.stderr)

    # Backend
    print("[capture_metrics] radon cc…", file=sys.stderr)
    cc_avg, cc_max, cc_raw = _radon_cc(backend_dir)
    print("[capture_metrics] radon raw…", file=sys.stderr)
    be_loc, raw_raw       = _radon_raw(backend_dir)
    print("[capture_metrics] vulture…", file=sys.stderr)
    be_stale, vulture_raw = _vulture(backend_dir)
    if with_coverage:
        print("[capture_metrics] pytest --cov (slow)…", file=sys.stderr)
        be_cov, cov_raw   = _pytest_coverage(backend_dir)
    else:
        be_cov, cov_raw   = None, {"_skipped": "--with-coverage not passed"}

    # Frontend
    print("[capture_metrics] frontend loc…", file=sys.stderr)
    fe_loc, loc_raw       = _frontend_loc(frontend_dir)
    print("[capture_metrics] jscpd…", file=sys.stderr)
    fe_dupe, jscpd_raw    = _jscpd(frontend_dir)
    print("[capture_metrics] eslint complexity…", file=sys.stderr)
    fe_cc_avg, fe_cc_max, fe_cc_raw = _eslint_complexity(frontend_dir)
    print("[capture_metrics] eslint no-unused…", file=sys.stderr)
    fe_stale, fe_stale_raw = _eslint_unused(frontend_dir)
    fe_cov = None  # Vitest coverage not yet wired

    # Cross-cutting
    print("[capture_metrics] git history scan…", file=sys.stderr)
    prev_tag = _previous_release_tag()
    bug_n    = _count_bug_commits(prev_tag)
    perf, perf_raw = _per_page_latency()

    # Test response times (optional — requires pytest-json-report + a prior
    # Playwright JSON report run). We still write the column even when the
    # tools aren't available so the DB column exists and the frontend can
    # show a "not yet captured" state rather than crashing.
    test_times: Optional[dict] = None
    test_times_meta: dict = {}
    if with_test_times:
        print("[capture_metrics] pytest durations…", file=sys.stderr)
        be_times, be_times_meta = _pytest_durations()
        print("[capture_metrics] playwright durations…", file=sys.stderr)
        fe_times, fe_times_meta = _playwright_durations()
        test_times = {
            "backend":  be_times  if be_times  else {"_skipped": be_times_meta.get("_error") or be_times_meta.get("_skipped") or "no data"},
            "frontend": fe_times  if fe_times  else {"_skipped": fe_times_meta.get("_error") or fe_times_meta.get("_skipped") or "no data"},
        }
        test_times_meta = {"backend_meta": be_times_meta, "frontend_meta": fe_times_meta}
        if be_times:
            print(
                f"[capture_metrics] backend tests: {be_times.get('total_tests')} tests, "
                f"wall={be_times.get('total_wall_time_s')}s, "
                f"max={be_times.get('max_s')}s, "
                f"slow_count={be_times.get('slow_count')}",
                file=sys.stderr,
            )
        if fe_times:
            print(
                f"[capture_metrics] frontend tests: {fe_times.get('total_tests')} tests, "
                f"wall={fe_times.get('total_wall_time_s')}s, "
                f"max={fe_times.get('max_s')}s",
                file=sys.stderr,
            )

    raw_payload = {
        "radon_cc":      cc_raw      if isinstance(cc_raw,      dict) else {},
        "radon_raw":     raw_raw     if isinstance(raw_raw,     dict) else {},
        "vulture":       vulture_raw[:50_000] if isinstance(vulture_raw, str) else "",
        "coverage":      cov_raw     if isinstance(cov_raw,     dict) else {},
        "frontend_loc":  loc_raw     if isinstance(loc_raw,     dict) else {},
        "jscpd":         jscpd_raw   if isinstance(jscpd_raw,   dict) else {},
        "eslint_cc":     fe_cc_raw   if isinstance(fe_cc_raw,   dict) else {},
        "eslint_unused": fe_stale_raw if isinstance(fe_stale_raw, dict) else {},
        "perf_meta":     perf_raw,
        "test_times_meta": test_times_meta,
        "prev_release_tag": prev_tag,
    }
    # Cap raw_payload so a misbehaving tool doesn't bloat the row to GB-scale.
    blob = json.dumps(raw_payload)
    if len(blob) > 1_500_000:
        raw_payload = {"_truncated": True, "size_bytes": len(blob)}

    values: dict[str, Any] = {
        "release_tag": release_tag,
        "captured_at": datetime.now(timezone.utc),
        "git_sha":     _git_sha(),
        "backend_loc":               be_loc,
        "backend_complexity_avg":    cc_avg,
        "backend_complexity_max":    cc_max,
        "backend_duplicated_lines":  None,  # radon doesn't give this; reserved
        "backend_stale_count":       be_stale,
        "backend_coverage_pct":      be_cov,
        "frontend_loc":               fe_loc,
        "frontend_complexity_avg":    fe_cc_avg,
        "frontend_complexity_max":    fe_cc_max,
        "frontend_duplicated_lines":  fe_dupe,
        "frontend_stale_count":       fe_stale,
        "frontend_coverage_pct":      fe_cov,
        "bug_count_since_last_release": bug_n,
        "per_page_latency_ms":         perf or {},
        "test_response_times":         test_times,
        "notes":                       notes or None,
        "raw_payload":                 raw_payload,
    }

    print("[capture_metrics] values:", file=sys.stderr)
    for k, v in values.items():
        if k in ("raw_payload",):
            continue
        print(f"  {k}: {v}", file=sys.stderr)

    written, msg = await _write_snapshot(values, force=force)
    print(f"[capture_metrics] {msg}", file=sys.stderr)
    return 0 if (written or "already exists" in msg) else 1


def _derive_default_tag() -> str:
    """Default tag = `git describe --tags --abbrev=0` if available, else
    'manual-YYYY-MM-DD'."""
    rc, out, _ = _run(["git", "-C", str(ROOT), "describe", "--tags", "--abbrev=0"])
    if rc == 0 and out.strip():
        return out.strip()
    return "manual-" + datetime.now(timezone.utc).strftime("%Y-%m-%d")


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture code-metrics snapshot.")
    parser.add_argument(
        "--release-tag",
        default=None,
        help="Release tag (default: latest git tag, or 'manual-YYYY-MM-DD').",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing snapshot with the same release tag.",
    )
    parser.add_argument(
        "--with-coverage",
        action="store_true",
        help="Also run pytest --cov (slow — adds 5-30 minutes).",
    )
    parser.add_argument(
        "--with-test-times",
        action="store_true",
        help=(
            "Collect per-test execution times. "
            "Backend: uses pytest-json-report (falls back to --durations=0 text). "
            "Frontend: reads /tmp/ramboq_pw_report.json from a prior Playwright JSON run."
        ),
    )
    parser.add_argument(
        "--notes",
        default="",
        help="Free-text notes for the snapshot.",
    )
    args = parser.parse_args()

    tag = (args.release_tag or _derive_default_tag()).strip()
    if not tag:
        print("ERROR: could not derive release tag", file=sys.stderr)
        sys.exit(1)

    rc = asyncio.run(_capture(
        release_tag=tag,
        with_coverage=bool(args.with_coverage),
        with_test_times=bool(args.with_test_times),
        force=bool(args.force),
        notes=args.notes,
    ))
    sys.exit(rc)


if __name__ == "__main__":
    main()
