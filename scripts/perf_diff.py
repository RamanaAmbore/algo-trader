"""
perf_diff.py — Diff two perf snapshots (baseline or capture).

Reads two JSON files produced by perf_baseline.py and/or perf_capture.py,
prints a table of per-page + per-route deltas, and flags anything >5%
regression in ANSI red.

Usage:
    ./venv/bin/python scripts/perf_diff.py <before.json> <after.json>
    ./venv/bin/python scripts/perf_diff.py   # defaults to two newest baselines

Writes a copy of the printed table to `.log/perf_diff_<from>_<to>.txt`
so the raw output is committable / attachable to sprint diaries.

Regression threshold:
    - LOC / effect / state / derived / subscribe / async_fn: >5% up  → red
    - LCP / TBT / heap / long-task: >5% up  → red
    - No RED for going down (that's an improvement).

Snapshots without a `runtime.*` block (e.g. baseline-only) simply
render `-` for the runtime columns; the tool never errors on missing
fields.

Colours: minimal ANSI (red only). Skipped when stdout is not a TTY OR
when the RAMBOQ_NO_COLOR env var is set.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / ".log"

_REGRESSION_PCT = 5.0     # anything worse than this = flag

_COL_RED   = "\033[31m"
_COL_RESET = "\033[0m"


def _colorize(s: str, use_colour: bool) -> str:
    return f"{_COL_RED}{s}{_COL_RESET}" if use_colour else s


def _load(path: Path) -> dict:
    if not path.exists():
        sys.exit(f"[perf_diff] missing {path}")
    return json.loads(path.read_text())


def _default_snapshots() -> tuple[Path, Path]:
    """Pick the two newest perf_baseline files in .log/."""
    files = sorted(LOG_DIR.glob("perf_baseline_*.json"))
    # Exclude the `latest` symlink from the sort (name-wise) — its
    # actual target is already in the list.
    files = [f for f in files if f.name != "perf_baseline_latest.json"]
    if len(files) < 2:
        sys.exit("[perf_diff] need ≥2 baseline files in .log/ (or pass paths)")
    return files[-2], files[-1]


def _delta_pct(before: float | None, after: float | None) -> float | None:
    """% change (after - before) / before * 100. Returns None if either is
    missing OR if `before` is zero (undefined ratio)."""
    if before is None or after is None:
        return None
    if before == 0:
        return None if after == 0 else float("inf")
    return (after - before) / before * 100.0


def _fmt_pct(pct: float | None) -> str:
    if pct is None:
        return "-"
    if pct == float("inf"):
        return "new"
    sign = "+" if pct > 0 else ""
    return f"{sign}{pct:.1f}%"


def _fmt_int(v: float | int | None) -> str:
    return "-" if v is None else f"{int(v)}"


def _fmt_ms(v: float | int | None) -> str:
    return "-" if v is None else f"{int(v)}ms"


def _fmt_mb(v: float | int | None) -> str:
    return "-" if v is None else f"{v:.1f}MB"


def _is_regression(pct: float | None) -> bool:
    return pct is not None and pct != float("inf") and pct > _REGRESSION_PCT


# ── Table build ────────────────────────────────────────────────────────────

_FRONTEND_COLS = [
    ("Page",       28, "l"),
    ("LOC",         6, "r"),
    ("ΔLOC",        7, "r"),
    ("Effects",     7, "r"),
    ("ΔEff",        7, "r"),
    ("LCP",         7, "r"),
    ("ΔLCP",        7, "r"),
    ("Heap",        7, "r"),
    ("ΔHeap",       7, "r"),
]

_BACKEND_COLS = [
    ("Route",      32, "l"),
    ("LOC",         6, "r"),
    ("ΔLOC",        7, "r"),
    ("AsyncFn",     8, "r"),
    ("ΔAsync",      7, "r"),
]


def _row(cells: list[str], cols: list[tuple[str, int, str]]) -> str:
    parts: list[str] = []
    for (title, width, align), cell in zip(cols, cells):
        # Colour codes inflate len; pad on visible width, not raw length.
        visible = cell
        for esc in (_COL_RED, _COL_RESET):
            visible = visible.replace(esc, "")
        pad = max(0, width - len(visible))
        parts.append((" " * pad + cell) if align == "r" else (cell + " " * pad))
    return "  ".join(parts)


def _header(cols: list[tuple[str, int, str]]) -> str:
    return _row([c[0] for c in cols], cols)


def _build_frontend_table(before: dict, after: dict, use_colour: bool) -> list[str]:
    lines: list[str] = ["Frontend", _header(_FRONTEND_COLS),
                        "-" * (sum(c[1] for c in _FRONTEND_COLS)
                               + 2 * (len(_FRONTEND_COLS) - 1))]

    b_pages = before.get("frontend", {}).get("pages", {})
    a_pages = after.get("frontend", {}).get("pages", {})
    keys = sorted(set(b_pages) | set(a_pages))

    for k in keys:
        b = b_pages.get(k, {})
        a = a_pages.get(k, {})
        loc_pct = _delta_pct(b.get("loc"), a.get("loc"))
        eff_pct = _delta_pct(b.get("effect_count"), a.get("effect_count"))

        # Runtime metrics — pulled from the optional `runtime` sub-key
        # written by perf_capture.py. Missing → "-".
        b_rt = b.get("runtime", {})
        a_rt = a.get("runtime", {})
        lcp_pct  = _delta_pct(b_rt.get("lcp_ms"), a_rt.get("lcp_ms"))
        heap_pct = _delta_pct(b_rt.get("heap_mb"), a_rt.get("heap_mb"))

        cells = [
            k[:28],
            _fmt_int(a.get("loc")),
            _colorize(_fmt_pct(loc_pct), use_colour and _is_regression(loc_pct)),
            _fmt_int(a.get("effect_count")),
            _colorize(_fmt_pct(eff_pct), use_colour and _is_regression(eff_pct)),
            _fmt_ms(a_rt.get("lcp_ms")),
            _colorize(_fmt_pct(lcp_pct), use_colour and _is_regression(lcp_pct)),
            _fmt_mb(a_rt.get("heap_mb")),
            _colorize(_fmt_pct(heap_pct), use_colour and _is_regression(heap_pct)),
        ]
        lines.append(_row(cells, _FRONTEND_COLS))

    # Bundle size row
    b_bundle = before.get("frontend", {}).get("bundle_size_kb")
    a_bundle = after.get("frontend", {}).get("bundle_size_kb")
    bundle_pct = _delta_pct(b_bundle, a_bundle)
    lines.append("")
    lines.append(f"bundle_size_kb:  before={b_bundle}  after={a_bundle}  "
                 f"Δ={_colorize(_fmt_pct(bundle_pct), use_colour and _is_regression(bundle_pct))}")
    return lines


def _build_backend_table(before: dict, after: dict, use_colour: bool) -> list[str]:
    lines: list[str] = ["", "Backend", _header(_BACKEND_COLS),
                        "-" * (sum(c[1] for c in _BACKEND_COLS)
                               + 2 * (len(_BACKEND_COLS) - 1))]

    b_routes = before.get("backend", {}).get("routes", {})
    a_routes = after.get("backend", {}).get("routes", {})
    keys = sorted(set(b_routes) | set(a_routes))

    for k in keys:
        b = b_routes.get(k, {})
        a = a_routes.get(k, {})
        loc_pct   = _delta_pct(b.get("loc"), a.get("loc"))
        async_pct = _delta_pct(b.get("async_fn_count"), a.get("async_fn_count"))
        cells = [
            k[:32],
            _fmt_int(a.get("loc")),
            _colorize(_fmt_pct(loc_pct), use_colour and _is_regression(loc_pct)),
            _fmt_int(a.get("async_fn_count")),
            _colorize(_fmt_pct(async_pct), use_colour and _is_regression(async_pct)),
        ]
        lines.append(_row(cells, _BACKEND_COLS))
    return lines


# ── Main ────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Diff two perf snapshots")
    ap.add_argument("before", nargs="?", type=Path,
                    help="Baseline JSON (older). Defaults to 2nd-newest "
                         ".log/perf_baseline_*.json.")
    ap.add_argument("after", nargs="?", type=Path,
                    help="Baseline JSON (newer). Defaults to newest.")
    ap.add_argument("--no-color", action="store_true",
                    help="Disable ANSI red regression flags.")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    if args.before is None or args.after is None:
        before_p, after_p = _default_snapshots()
    else:
        before_p, after_p = args.before, args.after

    before = _load(before_p)
    after  = _load(after_p)

    use_colour = (
        sys.stdout.isatty() and
        not args.no_color and
        not os.environ.get("RAMBOQ_NO_COLOR")
    )

    lines: list[str] = [
        f"before: {before_p.name}   commit={before.get('commit','?')}   "
        f"captured={before.get('captured_at','?')}",
        f"after:  {after_p.name}   commit={after.get('commit','?')}   "
        f"captured={after.get('captured_at','?')}",
        "",
    ]
    lines += _build_frontend_table(before, after, use_colour)
    lines += _build_backend_table(before, after, use_colour)

    text = "\n".join(lines)
    sys.stdout.write(text + "\n")

    # Persist a colour-free copy for the log.
    LOG_DIR.mkdir(exist_ok=True, parents=True)
    stem_b = before_p.stem.replace("perf_baseline_", "")
    stem_a = after_p.stem.replace("perf_baseline_", "")
    dst = LOG_DIR / f"perf_diff_{stem_b}_{stem_a}.txt"
    stripped = "\n".join(lines)
    for esc in (_COL_RED, _COL_RESET):
        stripped = stripped.replace(esc, "")
    dst.write_text(stripped + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
