"""
perf_baseline.py — Static perf/complexity snapshot for RamboQuant.

Emits one JSON file to `.log/perf_baseline_<timestamp>.json` and refreshes
the `perf_baseline_latest.json` symlink so `perf_diff.py` can pick up the
most recent snapshot with no arguments.

Metrics (all cheap greps + line counts — no runtime instrumentation):

Frontend, per page:
    - file           canonical Svelte source file backing the route
    - loc            wc -l on the file
    - effect_count   count of `$effect\\b`   in the source
    - state_count    count of `$state\\b`    in the source
    - derived_count  count of `$derived\\b`  in the source
    - subscribe_count count of `.subscribe(` calls

Frontend, aggregate:
    - bundle_size_kb   du -sk on the SvelteKit `_app/immutable/entry` dir
                      (skipped when --no-build is passed OR when the build
                      artefact is missing)

Backend, per route controller:
    - file             backend/api/routes/<name>.py
    - loc              wc -l
    - async_fn_count   count of `async def ` occurrences

Optional (--with-cyclomatic):
    Backend per route + frontend per page gain:
        - cyclomatic_avg       arithmetic mean of function CC in the file
        - cyclomatic_max       worst single-function CC
        - cyclomatic_hotspots  list of {fn_name, cc, line} for cc ≥ 10
    Backend runs `radon cc <path> -a -j` on each file (deduped by
    (lineno, name); class-type aggregators skipped). Frontend uses a
    regex heuristic on the extracted <script> block since escomplex on
    Svelte parts is fragile — counts if / else if / for / while / case /
    catch / && / || / ? per script block.

Optional (--with-runtime):
    Invokes `scripts/perf_capture_run.sh` (which runs Playwright's
    perf_capture.spec.js against a deployed base URL — default
    https://dev.ramboq.com) then merges its `.log/perf_capture_latest.json`
    runtime.* subblocks into each frontend page row.

Usage:
    ./venv/bin/python scripts/perf_baseline.py
    ./venv/bin/python scripts/perf_baseline.py --no-build
    ./venv/bin/python scripts/perf_baseline.py --with-cyclomatic
    ./venv/bin/python scripts/perf_baseline.py --with-runtime
    ./venv/bin/python scripts/perf_baseline.py --with-cyclomatic --with-runtime
    ./venv/bin/python scripts/perf_baseline.py --commit $(git rev-parse HEAD)
    ./venv/bin/python scripts/perf_baseline.py --dry-run   # print, no write

Output paths:
    .log/perf_baseline_<UTC-ISO>.json
    .log/perf_baseline_latest.json  (symlink → newest snapshot)

Zero DB writes. Zero config. All state lives in `.log/` JSON files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / ".log"

# ── Page → source-file map ─────────────────────────────────────────────────
#
# Each entry maps a bookmarkable route path to the Svelte file whose
# complexity we want to track. Some routes (e.g. /pulse, /dashboard) are
# thin +page.svelte shells that delegate to a heavy lib component; we
# measure the heavy component because that's where regressions land.
# Others (e.g. /admin/derivatives) hold everything in the +page itself,
# so we point at that file directly.
#
# Order defines the sort order in the diff table.
_FRONTEND_PAGES: list[tuple[str, str]] = [
    ("/pulse",             "frontend/src/lib/MarketPulse.svelte"),
    ("/dashboard",         "frontend/src/routes/(algo)/dashboard/+page.svelte"),
    ("/performance",       "frontend/src/lib/PerformancePage.svelte"),
    ("/admin/derivatives", "frontend/src/routes/(algo)/admin/derivatives/+page.svelte"),
    ("/charts",            "frontend/src/routes/(algo)/charts/+page.svelte"),
    ("/orders",            "frontend/src/routes/(algo)/orders/+page.svelte"),
    ("/admin/history",     "frontend/src/routes/(algo)/admin/history/+page.svelte"),
    ("/admin/audit",       "frontend/src/routes/(algo)/admin/audit/+page.svelte"),
    ("/automation",        "frontend/src/routes/(algo)/automation/+page.svelte"),
    ("/activity",          "frontend/src/routes/(algo)/activity/+page.svelte"),
]

# High-value shared lib components (mounted on many pages). Track them
# under a synthetic `lib::<name>` key so per-page rows stay clean.
_FRONTEND_LIB_COMPONENTS: list[tuple[str, str]] = [
    ("lib::MarketPulse",      "frontend/src/lib/MarketPulse.svelte"),
    ("lib::PerformancePage",  "frontend/src/lib/PerformancePage.svelte"),
    ("lib::NavCard",          "frontend/src/lib/NavCard.svelte"),
    ("lib::NavBreakdown",     "frontend/src/lib/NavBreakdown.svelte"),
    ("lib::PositionStrip",    "frontend/src/lib/PositionStrip.svelte"),
    ("lib::PriceChart",       "frontend/src/lib/PriceChart.svelte"),
    ("lib::ChartWorkspace",   "frontend/src/lib/ChartWorkspace.svelte"),
    ("lib::LogPanel",         "frontend/src/lib/LogPanel.svelte"),
    ("lib::OrderTicket",      "frontend/src/lib/order/OrderTicket.svelte"),
    ("lib::RefreshButton",    "frontend/src/lib/RefreshButton.svelte"),
]

# Backend route controllers we care about. `verb path` string is the
# label; the file is the controller source. We track LOC + `async def`
# count as a rough complexity proxy — deep routes tend to bloat both.
_BACKEND_ROUTES: list[tuple[str, str]] = [
    ("GET /api/positions",     "backend/api/routes/positions.py"),
    ("GET /api/holdings",      "backend/api/routes/holdings.py"),
    ("GET /api/funds",         "backend/api/routes/funds.py"),
    ("GET /api/nav",           "backend/api/routes/nav.py"),
    ("POST /api/orders",       "backend/api/routes/orders.py"),
    ("GET /api/charts",        "backend/api/routes/charts.py"),
    ("GET /api/options",       "backend/api/routes/options.py"),
    ("GET /api/quote",         "backend/api/routes/quote.py"),
    ("GET /api/history",       "backend/api/routes/history.py"),
    ("GET /api/audit",         "backend/api/routes/audit.py"),
    ("GET /api/health",        "backend/api/routes/health.py"),
    ("GET /api/execution",     "backend/api/routes/execution.py"),
    ("GET /api/logs",          "backend/api/routes/logs.py"),
    ("POST /api/agents",       "backend/api/routes/agents.py"),
]

# Word-boundary regexes for Svelte 5 runes. `$` must be escaped.
_RE_EFFECT   = re.compile(r"\$effect\b")
_RE_STATE    = re.compile(r"\$state\b")
_RE_DERIVED  = re.compile(r"\$derived\b")
_RE_SUB      = re.compile(r"\.subscribe\(")
_RE_ASYNCDEF = re.compile(r"^\s*async\s+def\s+", re.MULTILINE)

# Cyclomatic thresholds. Colour applied at report time; JSON just stores
# the raw score + whether it crossed the 10-line hotspot bar.
CYCLO_YELLOW = 10
CYCLO_RED    = 20

# Svelte cyclomatic heuristic — count these decision tokens inside every
# <script>...</script> block per component. Ternary counted via `\?`
# (subtracting `?.` optional-chaining to reduce false positives).
_RE_SVELTE_SCRIPT = re.compile(
    r"<script\b[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL,
)
_SVELTE_DECISION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("if",       re.compile(r"\bif\b")),
    ("else_if",  re.compile(r"\belse\s+if\b")),
    ("for",      re.compile(r"\bfor\b")),
    ("while",    re.compile(r"\bwhile\b")),
    ("case",     re.compile(r"\bcase\b")),
    ("catch",    re.compile(r"\bcatch\b")),
    ("and",      re.compile(r"&&")),
    ("or",       re.compile(r"\|\|")),
    ("ternary",  re.compile(r"\?")),
    ("optchain", re.compile(r"\?\.")),   # subtracted from ternary
]

# Svelte function-decl heuristic — used only to derive top-5 hotspots
# by name (script slice from decl line → next same-column decl OR EOF).
# Matches `function foo()`, `const foo = () =>`, `const foo = async () =>`,
# `const foo = function`. Naive: falls apart on multi-line arrow bodies
# with braces inside object literals — that's OK, this is a "top-5 by
# count" gesture, not a precise slicer.
_RE_SVELTE_FN = re.compile(
    r"^\s*(?:export\s+)?"
    r"(?:async\s+)?"
    r"(?:function\s+(?P<fn1>[A-Za-z_$][\w$]*)"
    r"|const\s+(?P<fn2>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?"
    r"(?:function\b|\([^)]*\)\s*=>|\w+\s*=>)"
    r"|let\s+(?P<fn3>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?"
    r"(?:function\b|\([^)]*\)\s*=>|\w+\s*=>))",
    re.MULTILINE,
)


# ── Helpers ────────────────────────────────────────────────────────────────

def _read(path: Path) -> str | None:
    """Read a file if present, else None. Broken symlinks / missing files
    return None so callers can degrade gracefully (row omitted or LOC=0)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"[warn] cannot read {path}: {e}", file=sys.stderr)
        return None


def _svelte_stats(text: str) -> dict:
    """Compute the four Svelte 5 rune / store counts from a source blob."""
    return {
        "loc":              text.count("\n") + (0 if text.endswith("\n") else 1),
        "effect_count":     len(_RE_EFFECT.findall(text)),
        "state_count":      len(_RE_STATE.findall(text)),
        "derived_count":    len(_RE_DERIVED.findall(text)),
        "subscribe_count":  len(_RE_SUB.findall(text)),
    }


def _backend_stats(text: str) -> dict:
    return {
        "loc":            text.count("\n") + (0 if text.endswith("\n") else 1),
        "async_fn_count": len(_RE_ASYNCDEF.findall(text)),
    }


def _measure_bundle(do_build: bool) -> float | None:
    """Return bundle-size in KB, or None if skipped / unavailable.

    We measure `_app/immutable/entry/` under whichever build dir is
    populated:
        - frontend/build/_app/immutable/entry          (SPA output)
        - frontend/.svelte-kit/output/client/_app/immutable/entry
    """
    if not do_build:
        return None

    candidates = [
        ROOT / "frontend" / "build" / "_app" / "immutable" / "entry",
        ROOT / "frontend" / ".svelte-kit" / "output" / "client" /
              "_app" / "immutable" / "entry",
    ]
    entry_dir: Path | None = next((c for c in candidates if c.exists()), None)

    if entry_dir is None:
        print("[warn] no build artefact found — run `npm run build` in "
              "frontend/ to populate bundle stats", file=sys.stderr)
        return None

    try:
        # `du -sk` prints "<kb>\t<path>" — take first field, parse as int.
        out = subprocess.check_output(
            ["du", "-sk", str(entry_dir)],
            stderr=subprocess.DEVNULL,
        ).decode("ascii", "replace").split()
        return float(out[0])
    except (subprocess.CalledProcessError, OSError, ValueError) as e:
        print(f"[warn] du failed on {entry_dir}: {e}", file=sys.stderr)
        return None


def _radon_venv_bin() -> str:
    """Find the radon executable to invoke. Prefer the project venv so
    the tool version stays pinned; fall back to $PATH."""
    venv_radon = ROOT / "venv" / "bin" / "radon"
    if venv_radon.exists():
        return str(venv_radon)
    return "radon"


def _radon_cc_file(relpath: str) -> dict:
    """Run `radon cc <file> -a -j` on one file and return the parsed
    per-file complexity map. Returns empty dict on any failure so callers
    can degrade to `cyclomatic_avg=None`.

    Output shape from radon:
        [{ "type": "function"|"method"|"class",
           "name": "...", "lineno": 12, "complexity": 7,
           "classname": "Ctrl" (for methods),
           "closures": [...],   # nested defs
           "methods":  [...] }, # only on class rows
           ...]
    """
    abspath = ROOT / relpath
    if not abspath.exists():
        return {}
    try:
        out = subprocess.check_output(
            [_radon_venv_bin(), "cc", str(abspath), "-a", "-j"],
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as e:
        print(f"[warn] radon failed on {relpath}: {e}", file=sys.stderr)
        return {}
    try:
        data = json.loads(out.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return {}
    # radon keys by absolute path — we asked for one file, take its value.
    if not isinstance(data, dict) or not data:
        return {}
    return next(iter(data.values())) if isinstance(next(iter(data.values()), None), list) else {}


def _radon_walk(entries: list, out_functions: dict, prefix: str = "") -> None:
    """Depth-first collect every function/method entry into out_functions
    keyed by (lineno, qualified_name) so duplicates from radon's tree
    (methods appear both flat and nested inside their class) collapse to
    one row. Class-type entries are skipped — their `complexity` is a
    sum of methods and would inflate averages."""
    if not isinstance(entries, list):
        return
    for e in entries:
        if not isinstance(e, dict):
            continue
        kind = e.get("type", "")
        name = e.get("name", "")
        lineno = e.get("lineno", 0)
        cc = e.get("complexity", 0)

        if kind == "class":
            # Recurse into class methods, do NOT count the class row itself.
            _radon_walk(e.get("methods", []), out_functions, prefix=f"{name}.")
            continue

        qual = f"{prefix}{name}"
        # Dedupe on lineno alone — radon emits methods twice (nested
        # inside their class row AND flat at the top level of the file
        # entries list). Line numbers uniquely identify a function
        # within a file, so keeping the first-seen entry (which will be
        # the class-prefixed form when walked from `methods`) is enough.
        # If the flat form arrives first, we keep the shorter name.
        if lineno not in out_functions:
            out_functions[lineno] = {"fn_name": qual, "cc": cc, "line": lineno}
        # Recurse into closures (nested defs).
        _radon_walk(e.get("closures", []), out_functions, prefix=f"{qual}.")


def _radon_stats(relpath: str) -> dict:
    """Return `{cyclomatic_avg, cyclomatic_max, cyclomatic_hotspots}`
    for a Python file. Empty file → avg=0, max=0, hotspots=[]."""
    entries = _radon_cc_file(relpath)
    if not entries:
        return {"cyclomatic_avg": 0, "cyclomatic_max": 0, "cyclomatic_hotspots": []}

    fns: dict = {}
    _radon_walk(entries, fns)
    if not fns:
        return {"cyclomatic_avg": 0, "cyclomatic_max": 0, "cyclomatic_hotspots": []}

    ccs = [r["cc"] for r in fns.values()]
    avg = sum(ccs) / len(ccs)
    peak = max(ccs)
    hotspots = sorted(
        [r for r in fns.values() if r["cc"] >= CYCLO_YELLOW],
        key=lambda r: (-r["cc"], r["line"]),
    )
    return {
        "cyclomatic_avg":      round(avg, 1),
        "cyclomatic_max":      peak,
        "cyclomatic_hotspots": hotspots,
    }


def _svelte_cyclomatic(text: str) -> dict:
    """Heuristic cyclomatic estimate for a Svelte component. Extract every
    `<script>` block, tally decision tokens, and derive the top-5 named
    functions by decision-token density inside their slice.

    Returns:
        {
          "cyclomatic_est":      <int>,         # per-component total
          "cyclomatic_hotspots": [ {fn_name, cc, line}, … ]  # up to 5
        }
    """
    scripts = _RE_SVELTE_SCRIPT.findall(text or "")
    if not scripts:
        return {"cyclomatic_est": 0, "cyclomatic_hotspots": []}

    joined = "\n".join(scripts)
    counts = {name: len(pat.findall(joined)) for name, pat in _SVELTE_DECISION_PATTERNS}
    # Ternary count minus optional-chaining reduces the `?.` false-positive
    # inflation we'd otherwise see on TypeScript-heavy components.
    ternary = max(0, counts["ternary"] - counts["optchain"])
    est = (counts["if"] + counts["else_if"] + counts["for"] + counts["while"]
           + counts["case"] + counts["catch"] + counts["and"] + counts["or"]
           + ternary)

    # Per-function hotspots — walk the joined script, split at every
    # function-decl start, tally decision tokens in each slice. Line
    # numbers refer to the joined script (not the file); good enough
    # for "where do I start looking" hints. Top 5 by score.
    decls = list(_RE_SVELTE_FN.finditer(joined))
    hotspots: list[dict] = []
    # Cap slice length so a decl whose next-sibling is far away doesn't
    # attribute half the file to itself. 400 lines ≈ 20-30 KB — larger
    # than any sane function; anything above is almost certainly
    # multi-function noise our naive slicer can't split.
    _SLICE_CAP = 400 * 80   # ~80 chars/line average
    for i, m in enumerate(decls):
        name = m.group("fn1") or m.group("fn2") or m.group("fn3") or "<anon>"
        # Slice from this decl to the next decl (or EOF), capped.
        start = m.start()
        raw_end = decls[i + 1].start() if i + 1 < len(decls) else len(joined)
        end = min(raw_end, start + _SLICE_CAP)
        slice_ = joined[start:end]
        score = 0
        for pname, pat in _SVELTE_DECISION_PATTERNS:
            if pname == "optchain":
                continue
            score += len(pat.findall(slice_))
        # subtract optional-chain from ternary contribution
        score -= len(_SVELTE_DECISION_PATTERNS[-1][1].findall(slice_))
        score = max(0, score)
        if score >= CYCLO_YELLOW:
            # `line` is line-in-joined-scripts, not absolute file line —
            # still useful as a rough locator.
            line = joined.count("\n", 0, start) + 1
            hotspots.append({"fn_name": name, "cc": score, "line": line})

    hotspots.sort(key=lambda r: (-r["cc"], r["line"]))
    return {
        "cyclomatic_est":      est,
        "cyclomatic_hotspots": hotspots[:5],
    }


def _perf_capture_run() -> Path | None:
    """Shell out to scripts/perf_capture_run.sh, then return the path to
    the freshly-written `.log/perf_capture_latest.json`. Prints any
    stdout/stderr straight through — this is a long-running subprocess
    and progress matters.

    Returns None if the script fails or the JSON is missing."""
    script = ROOT / "scripts" / "perf_capture_run.sh"
    if not script.exists():
        print(f"[warn] {script.relative_to(ROOT)} missing — cannot capture runtime",
              file=sys.stderr)
        return None
    try:
        subprocess.run([str(script)], check=True, cwd=str(ROOT))
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"[warn] perf_capture_run.sh failed: {e}", file=sys.stderr)
        return None
    latest = LOG_DIR / "perf_capture_latest.json"
    if not latest.exists():
        print("[warn] perf_capture_latest.json missing after run", file=sys.stderr)
        return None
    return latest


def _merge_runtime(snap: dict, capture_path: Path) -> None:
    """Fold each `frontend.pages.<route>.runtime` block from a capture
    JSON into the matching page row of `snap`. Missing routes on either
    side are left untouched."""
    try:
        cap = json.loads(capture_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"[warn] cannot merge runtime from {capture_path.name}: {e}",
              file=sys.stderr)
        return
    cap_pages = cap.get("frontend", {}).get("pages", {})
    snap_pages = snap.setdefault("frontend", {}).setdefault("pages", {})
    for route, row in cap_pages.items():
        rt = row.get("runtime")
        if not rt:
            continue
        if route in snap_pages:
            snap_pages[route]["runtime"] = rt
        else:
            # Route captured but not in baseline map — surface it anyway
            # under a `runtime_only` marker so nothing is silently dropped.
            snap_pages[route] = {"runtime": rt, "runtime_only": True}


def _git_head() -> str:
    """Return the short SHA of HEAD, or empty string if git call fails."""
    try:
        return subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode("ascii", "replace").strip()
    except (subprocess.CalledProcessError, OSError):
        return ""


# ── Snapshot ────────────────────────────────────────────────────────────────

def build_snapshot(
    *,
    do_build: bool,
    commit_override: str | None,
    with_cyclomatic: bool = False,
    with_runtime: bool = False,
) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot: dict = {
        "captured_at": now.isoformat().replace("+00:00", "Z"),
        "commit":      commit_override or _git_head(),
        "frontend":    {"pages": {}, "bundle_size_kb": _measure_bundle(do_build)},
        "backend":     {"routes": {}},
    }

    for route, relpath in _FRONTEND_PAGES + _FRONTEND_LIB_COMPONENTS:
        abspath = ROOT / relpath
        text = _read(abspath)
        row: dict = {"file": relpath}
        if text is None:
            row.update({"loc": 0, "effect_count": 0, "state_count": 0,
                        "derived_count": 0, "subscribe_count": 0,
                        "missing": True})
        else:
            row.update(_svelte_stats(text))
            if with_cyclomatic:
                row.update(_svelte_cyclomatic(text))
        snapshot["frontend"]["pages"][route] = row

    for label, relpath in _BACKEND_ROUTES:
        abspath = ROOT / relpath
        text = _read(abspath)
        row = {"file": relpath}
        if text is None:
            row.update({"loc": 0, "async_fn_count": 0, "missing": True})
        else:
            row.update(_backend_stats(text))
            if with_cyclomatic:
                row.update(_radon_stats(relpath))
        snapshot["backend"]["routes"][label] = row

    # Optional runtime merge — driven by --with-runtime. Happens LAST so
    # the runtime block lands on top of any static row it matches.
    if with_runtime:
        cap_path = _perf_capture_run()
        if cap_path is not None:
            _merge_runtime(snapshot, cap_path)

    return snapshot


def write_snapshot(snap: dict) -> Path:
    LOG_DIR.mkdir(exist_ok=True, parents=True)
    stamp = snap["captured_at"].replace(":", "").replace("-", "")
    dst = LOG_DIR / f"perf_baseline_{stamp}.json"
    dst.write_text(json.dumps(snap, indent=2, sort_keys=False))

    # Update the "latest" symlink. Use a relative symlink so the .log/
    # dir stays portable across checkouts.
    latest = LOG_DIR / "perf_baseline_latest.json"
    try:
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        os.symlink(dst.name, latest)
    except OSError as e:
        # Some filesystems (dockerfs) reject symlinks — fall back to
        # a plain copy so the diff tool still finds the payload.
        print(f"[warn] symlink failed ({e}); copying instead", file=sys.stderr)
        latest.write_text(dst.read_text())
    return dst


# ── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--no-build", action="store_true",
                    help="Skip bundle-size measurement (do not require an "
                         "up-to-date `npm run build`).")
    ap.add_argument("--commit", default=None,
                    help="Commit SHA to record in the snapshot (defaults "
                         "to `git rev-parse --short HEAD`).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print snapshot to stdout, do not write to .log/.")
    ap.add_argument("--with-cyclomatic", action="store_true",
                    help="Compute cyclomatic complexity (radon for Python, "
                         "regex heuristic for Svelte). Adds ~2-4s wall time.")
    ap.add_argument("--with-runtime", action="store_true",
                    help="Invoke scripts/perf_capture_run.sh (Playwright "
                         "against deployed dev API) and merge captured "
                         "LCP/heap/long-task into per-page runtime blocks. "
                         "Requires network + admin creds.")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    snap = build_snapshot(
        do_build=not args.no_build,
        commit_override=args.commit,
        with_cyclomatic=args.with_cyclomatic,
        with_runtime=args.with_runtime,
    )
    if args.dry_run:
        json.dump(snap, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
        return 0
    dst = write_snapshot(snap)
    print(f"[perf_baseline] wrote {dst.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
