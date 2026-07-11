#!/usr/bin/env python3
"""
check_stale_snapshots.py — Closed-hours snapshot freshness checker.

Queries the PostgreSQL database to verify that all operator-visible data
surfaces have a recent snapshot in the daily_book / nav_daily tables.

Checked surfaces:
    positions  — daily_book WHERE kind='positions'
    holdings   — daily_book WHERE kind='holdings'
    nav        — nav_daily (separate table, not daily_book)
    sparkline  — daily_book WHERE kind='sparkline'

Freshness rule:
    - date = today (IST), OR
    - date within the last MAX_SNAPSHOT_AGE_DAYS calendar days
      (covers long weekends: Friday close visible through Tuesday morning)

Exit codes:
    0 — all surfaces are OK
    1 — one or more surfaces are STALE or MISSING

Usage:
    python scripts/check_stale_snapshots.py
    python scripts/check_stale_snapshots.py --help
    python scripts/check_stale_snapshots.py --dry-run

DB connection: reads backend/config/secrets.yaml (db_user, db_password,
db_host, db_port, db) + backend/config/backend_config.yaml (deploy_branch).
Falls back to DATABASE_URL environment variable (sync postgresql:// form).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ── Project root ────────────────────────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent

# ── Freshness window ────────────────────────────────────────────────────────

# Allow snapshots up to this many calendar days old.
# 4 days covers: Friday close → Monday/Tuesday morning (with public holidays).
MAX_SNAPSHOT_AGE_DAYS = 4

# ── IST offset ─────────────────────────────────────────────────────────────

_IST_OFFSET = timedelta(hours=5, minutes=30)


def _today_ist() -> date:
    return (datetime.now(timezone.utc) + _IST_OFFSET).date()


def _now_ist_str() -> str:
    dt = datetime.now(timezone.utc) + _IST_OFFSET
    return dt.strftime("%Y-%m-%d %H:%M IST")


# ── Config loading ──────────────────────────────────────────────────────────

def _load_yaml_simple(path: Path) -> dict[str, Any]:
    """Minimal YAML key: value parser — no external deps, handles strings,
    ints, booleans, and quoted strings. Nested keys (with indentation) are
    treated as flat — sufficient for our secrets structure."""
    result: dict[str, Any] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        # Strip comments
        line = line.split("#")[0].rstrip()
        if not line or ":" not in line:
            continue
        # Only parse top-level keys (no leading spaces)
        if line[0] == " " or line[0] == "\t":
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        value = raw_value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        # Try int conversion
        try:
            result[key] = int(value)
        except ValueError:
            result[key] = value
    return result


def _build_dsn() -> str:
    """Build a sync postgresql:// DSN from config files or env var.

    Priority:
    1. DATABASE_URL environment variable (must use postgresql:// scheme)
    2. Constructed from secrets.yaml + backend_config.yaml
    """
    env_url = os.environ.get("DATABASE_URL", "")
    if env_url:
        # Normalise asyncpg URLs so we can use asyncpg directly
        return env_url.replace("postgresql+asyncpg://", "postgresql://")

    secrets_path = ROOT / "backend" / "config" / "secrets.yaml"
    config_path  = ROOT / "backend" / "config" / "backend_config.yaml"

    secrets = _load_yaml_simple(secrets_path)
    config  = _load_yaml_simple(config_path)

    user     = secrets.get("db_user", "rambo_admin")
    password = secrets.get("db_password", "")
    host     = str(secrets.get("db_host", "localhost"))
    port     = int(secrets.get("db_port", 5432))

    branch  = str(config.get("deploy_branch", "dev"))
    db_name = "ramboq" if branch == "main" else "ramboq_dev"

    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


def _redacted_dsn(dsn: str) -> str:
    """Replace password in DSN with '***' for safe display."""
    import re
    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", dsn)


# ── Surface definitions ─────────────────────────────────────────────────────

# Each entry: (surface_label, table, filter_clause, date_column)
# For surfaces on daily_book we use `kind=` filter; for nav_daily, no filter.
_SURFACES: list[tuple[str, str, str, str]] = [
    ("positions", "daily_book", "kind = 'positions'", "date"),
    ("holdings",  "daily_book", "kind = 'holdings'",  "date"),
    ("nav",       "nav_daily",  "",                    "date"),
    ("sparkline", "daily_book", "kind = 'sparkline'",  "date"),
]


# ── DB queries ──────────────────────────────────────────────────────────────

async def _query_max_dates(dsn: str) -> dict[str, date | None]:
    """Connect to PostgreSQL via asyncpg and return the most recent snapshot
    date for each surface. Returns None for a surface with no rows."""
    try:
        import asyncpg  # type: ignore[import]
    except ImportError:
        print(
            "[error] asyncpg is not installed. Install it with: "
            "pip install asyncpg",
            file=sys.stderr,
        )
        sys.exit(2)

    # asyncpg expects postgresql:// not postgresql+asyncpg://
    asyncpg_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")

    results: dict[str, date | None] = {}
    try:
        conn = await asyncpg.connect(asyncpg_dsn, timeout=10)
    except Exception as exc:
        print(f"[error] DB connection failed: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        for label, table, where, date_col in _SURFACES:
            clause = f" WHERE {where}" if where else ""
            sql = f"SELECT MAX({date_col}) FROM {table}{clause}"
            try:
                row = await conn.fetchrow(sql)
                val = row[0] if row else None
                # asyncpg may return datetime.date or datetime.datetime
                if val is None:
                    results[label] = None
                elif isinstance(val, datetime):
                    results[label] = val.date()
                else:
                    results[label] = val
            except Exception as exc:
                print(f"[warn] query failed for {label}: {exc}", file=sys.stderr)
                results[label] = None
    finally:
        await conn.close()

    return results


# ── Freshness check ─────────────────────────────────────────────────────────

def _classify(snapshot_date: date | None, today: date) -> tuple[str, str]:
    """Return (status_label, detail_str) for a snapshot date."""
    if snapshot_date is None:
        return ("MISSING", "")
    cutoff = today - timedelta(days=MAX_SNAPSHOT_AGE_DAYS)
    if snapshot_date >= cutoff:
        return ("OK", str(snapshot_date))
    return ("STALE", str(snapshot_date))


# ── Output formatting ───────────────────────────────────────────────────────

_TICK = "✓"   # ✓
_CROSS = "✗"  # ✗


def print_report(
    results: dict[str, date | None],
    today: date,
    any_bad: bool,
) -> None:
    header = f"STALE SNAPSHOT REPORT {_now_ist_str()}"
    print(header)
    print("=" * len(header))
    for label, table, _, _ in _SURFACES:
        snap_date = results.get(label)
        status, detail = _classify(snap_date, today)
        if status == "OK":
            icon = _TICK
            detail_str = f"({detail})"
            status_str = f"{icon} OK       "
        elif status == "STALE":
            icon = _CROSS
            detail_str = f"(last: {detail})"
            status_str = f"{icon} STALE    "
        else:
            icon = _CROSS
            detail_str = ""
            status_str = f"{icon} MISSING  "
        print(f"  {label:<10}  {status_str}  {detail_str}")

    print()
    if any_bad:
        print("Exit: 1 (stale snapshots detected)")
    else:
        print("Exit: 0")


# ── CLI ─────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "Check that closed-hours snapshots exist and are fresh for all "
            "operator-visible data surfaces (positions, holdings, nav, sparkline). "
            "Exits 1 if any surface is STALE or MISSING."
        )
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the DB DSN (password redacted) and the queries that would "
            "run, without actually connecting to the database or exiting "
            "non-zero."
        ),
    )
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    today = _today_ist()
    dsn = _build_dsn()

    if args.dry_run:
        print("[dry-run] check_stale_snapshots.py — no DB queries will run.")
        print(f"[dry-run] DB DSN:  {_redacted_dsn(dsn)}")
        print(f"[dry-run] Today (IST): {today}")
        print(f"[dry-run] Freshness window: {MAX_SNAPSHOT_AGE_DAYS} calendar days")
        print("[dry-run] Queries that would run:")
        for label, table, where, date_col in _SURFACES:
            clause = f" WHERE {where}" if where else ""
            sql = f"SELECT MAX({date_col}) FROM {table}{clause}"
            print(f"  [{label}]  {sql}")
        print("[dry-run] Note: NAV freshness is checked against `nav_daily` "
              "(not daily_book), because NAV snapshots are stored in a "
              "separate table.")
        return 0

    results = asyncio.run(_query_max_dates(dsn))

    any_bad = any(
        _classify(results.get(label), today)[0] != "OK"
        for label, _, _, _ in _SURFACES
    )

    print_report(results, today, any_bad)
    return 1 if any_bad else 0


if __name__ == "__main__":
    sys.exit(main())
