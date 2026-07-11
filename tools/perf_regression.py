#!/usr/bin/env python3
"""
perf_regression.py — Route-level latency regression detector for RamboQuant.

Compares the two most recent `.log/perf_snapshot_YYYY-MM-DD.txt` files
(today vs yesterday, as produced by the daily audit cron) and reports any
route whose p95 latency regressed by more than 10%.

Exit codes:
    0 — no route exceeded the 20% p95 regression threshold (or fewer than 2
        snapshots exist)
    1 — at least one route regressed more than 20% on p95

Usage:
    python tools/perf_regression.py
    python tools/perf_regression.py --help
    python tools/perf_regression.py --dry-run

Snapshot format (one route per line):
    GET /api/positions  p50=12ms p95=45ms p99=102ms  calls=847
    POST /api/orders/ticket  p50=88ms p95=310ms p99=520ms  calls=23
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import NamedTuple

# ── Constants ──────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / ".log"

# Regression thresholds (% delta on p95)
MINOR_THRESHOLD = 10.0   # report but don't fail
MAJOR_THRESHOLD = 20.0   # report as P2 and exit 1

# Regex to parse one snapshot line:
# `GET /api/positions  p50=12ms p95=45ms p99=102ms  calls=847`
_RE_LINE = re.compile(
    r"^(?P<method>[A-Z]+)\s+(?P<path>/\S*)"
    r".*?p50=(?P<p50>\d+(?:\.\d+)?)ms"
    r".*?p95=(?P<p95>\d+(?:\.\d+)?)ms"
    r".*?p99=(?P<p99>\d+(?:\.\d+)?)ms"
    r".*?calls=(?P<calls>\d+)",
    re.IGNORECASE,
)

# Regex to extract a YYYY-MM-DD date from a snapshot filename.
_RE_DATE = re.compile(r"perf_snapshot_(\d{4}-\d{2}-\d{2})\.txt$")


# ── Data types ─────────────────────────────────────────────────────────────

class RouteStats(NamedTuple):
    p50: float
    p95: float
    p99: float
    calls: int


# ── Parsing ────────────────────────────────────────────────────────────────

def parse_snapshot(path: Path) -> dict[str, RouteStats]:
    """Parse a snapshot text file and return a map of route label → stats.

    Lines that don't match the expected format are silently skipped so that
    comment lines or blank lines don't cause hard failures.
    """
    routes: dict[str, RouteStats] = {}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"[error] cannot read {path}: {exc}", file=sys.stderr)
        return routes

    for lineno, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = _RE_LINE.match(line)
        if not m:
            # Non-matching lines (headers, section separators) are silently
            # ignored — don't warn on every blank/separator line.
            continue
        label = f"{m.group('method').upper()} {m.group('path')}"
        routes[label] = RouteStats(
            p50=float(m.group("p50")),
            p95=float(m.group("p95")),
            p99=float(m.group("p99")),
            calls=int(m.group("calls")),
        )
    return routes


# ── Snapshot discovery ─────────────────────────────────────────────────────

def find_snapshots() -> list[tuple[date, Path]]:
    """Return all perf_snapshot_YYYY-MM-DD.txt files sorted by date ascending."""
    results: list[tuple[date, Path]] = []
    if not LOG_DIR.exists():
        return results
    for p in LOG_DIR.iterdir():
        m = _RE_DATE.match(p.name)
        if m:
            try:
                d = date.fromisoformat(m.group(1))
                results.append((d, p))
            except ValueError:
                continue
    results.sort(key=lambda t: t[0])
    return results


# ── Regression analysis ────────────────────────────────────────────────────

def _pct_delta(before: float, after: float) -> float | None:
    """Return % delta (after - before) / before * 100. None if before == 0."""
    if before == 0.0:
        return None
    return (after - before) / before * 100.0


def _fmt_ms(v: float) -> str:
    return f"{int(round(v))}ms"


def _fmt_pct(v: float) -> str:
    sign = "+" if v >= 0 else ""
    return f"({sign}{v:.0f}%)"


def compare(
    yesterday_stats: dict[str, RouteStats],
    today_stats: dict[str, RouteStats],
) -> tuple[
    list[tuple[str, RouteStats, RouteStats, float]],   # major regressions (>20%)
    list[tuple[str, RouteStats, RouteStats, float]],   # minor regressions (10-20%)
    list[str],                                          # new routes
    list[str],                                          # dropped routes
]:
    """Compare two snapshots and return categorised results."""
    major: list[tuple[str, RouteStats, RouteStats, float]] = []
    minor: list[tuple[str, RouteStats, RouteStats, float]] = []

    all_yesterday = set(yesterday_stats)
    all_today = set(today_stats)

    new_routes = sorted(all_today - all_yesterday)
    dropped_routes = sorted(all_yesterday - all_today)
    common = all_yesterday & all_today

    for label in sorted(common):
        prev = yesterday_stats[label]
        curr = today_stats[label]
        pct = _pct_delta(prev.p95, curr.p95)
        if pct is None:
            continue
        if pct > MAJOR_THRESHOLD:
            major.append((label, prev, curr, pct))
        elif pct > MINOR_THRESHOLD:
            minor.append((label, prev, curr, pct))

    # Sort major regressions by severity (worst first)
    major.sort(key=lambda t: -t[3])
    minor.sort(key=lambda t: -t[3])

    return major, minor, new_routes, dropped_routes


# ── Output formatting ──────────────────────────────────────────────────────

def print_report(
    today_date: date,
    yesterday_date: date,
    major: list[tuple[str, RouteStats, RouteStats, float]],
    minor: list[tuple[str, RouteStats, RouteStats, float]],
    new_routes: list[str],
    dropped_routes: list[str],
    exit_code: int,
) -> None:
    """Print the regression report to stdout."""
    header = f"PERF REGRESSION REPORT {today_date} vs {yesterday_date}"
    print(header)
    print("=" * len(header))

    if major:
        print("\nREGRESSION (>20%):")
        for label, prev, curr, pct in major:
            line = (
                f"  {label:<40}  p95: {_fmt_ms(prev.p95)} → {_fmt_ms(curr.p95)}"
                f"  {_fmt_pct(pct)}  <- P2"
            )
            print(line)
    else:
        print("\nREGRESSION (>20%): none")

    if minor:
        print("\nMINOR (10-20%):")
        for label, prev, curr, pct in minor:
            line = (
                f"  {label:<40}  p95: {_fmt_ms(prev.p95)} → {_fmt_ms(curr.p95)}"
                f"  {_fmt_pct(pct)}"
            )
            print(line)
    else:
        print("\nMINOR (10-20%): none")

    new_str = ", ".join(new_routes) if new_routes else "none"
    dropped_str = ", ".join(dropped_routes) if dropped_routes else "none"
    print(f"\nNEW ROUTES: {new_str}")
    print(f"DROPPED ROUTES: {dropped_str}")
    print(f"\nExit: {exit_code}"
          + (" (regression threshold exceeded)" if exit_code == 1 else ""))


# ── CLI ────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Compare two consecutive perf snapshots and report route-level "
            "p95 latency regressions. Exits 1 if any route regresses >20%."
        )
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print what would be compared without loading snapshots or "
            "exiting non-zero. Shows the two most recent snapshot files "
            "that would be used (or explains that none exist)."
        ),
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    snapshots = find_snapshots()

    if args.dry_run:
        print("[dry-run] perf_regression.py — no files will be read or "
              "non-zero exits produced.")
        if len(snapshots) < 2:
            print(
                f"[dry-run] Found {len(snapshots)} snapshot file(s) in "
                f"{LOG_DIR.relative_to(ROOT)} — need ≥2 to compare."
            )
            if snapshots:
                print(f"[dry-run]   {snapshots[-1][1].name}")
            print("[dry-run] Would print: "
                  "'No prior snapshot to compare — skipping regression check'")
        else:
            yesterday_date, yesterday_path = snapshots[-2]
            today_date, today_path = snapshots[-1]
            print(f"[dry-run] Would compare:")
            print(f"[dry-run]   yesterday: {yesterday_path.name}")
            print(f"[dry-run]   today:     {today_path.name}")
            print(f"[dry-run] Regression thresholds: minor={MINOR_THRESHOLD}%  "
                  f"major (P2)={MAJOR_THRESHOLD}%")
        return 0

    if len(snapshots) < 2:
        print("No prior snapshot to compare — skipping regression check")
        return 0

    yesterday_date, yesterday_path = snapshots[-2]
    today_date, today_path = snapshots[-1]

    yesterday_stats = parse_snapshot(yesterday_path)
    today_stats = parse_snapshot(today_path)

    if not yesterday_stats and not today_stats:
        print(
            f"[warn] both snapshot files parsed to zero routes — "
            f"check file format in {yesterday_path.name} / {today_path.name}",
            file=sys.stderr,
        )

    major, minor, new_routes, dropped_routes = compare(yesterday_stats, today_stats)

    exit_code = 1 if major else 0
    print_report(
        today_date=today_date,
        yesterday_date=yesterday_date,
        major=major,
        minor=minor,
        new_routes=new_routes,
        dropped_routes=dropped_routes,
        exit_code=exit_code,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
