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

Usage:
    ./venv/bin/python scripts/perf_baseline.py
    ./venv/bin/python scripts/perf_baseline.py --no-build
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

def build_snapshot(*, do_build: bool, commit_override: str | None) -> dict:
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
        snapshot["frontend"]["pages"][route] = row

    for label, relpath in _BACKEND_ROUTES:
        abspath = ROOT / relpath
        text = _read(abspath)
        row = {"file": relpath}
        if text is None:
            row.update({"loc": 0, "async_fn_count": 0, "missing": True})
        else:
            row.update(_backend_stats(text))
        snapshot["backend"]["routes"][label] = row

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
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    snap = build_snapshot(do_build=not args.no_build,
                          commit_override=args.commit)
    if args.dry_run:
        json.dump(snap, sys.stdout, indent=2, sort_keys=False)
        sys.stdout.write("\n")
        return 0
    dst = write_snapshot(snap)
    print(f"[perf_baseline] wrote {dst.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
