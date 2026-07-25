#!/usr/bin/env python3
"""
check_broker_conn_issues.py — Broker connection issue threshold checker.

Queries broker_issue_daily for the last N days and flags brokers whose
issue counts exceed configured thresholds.

Exit codes:
  0 — all brokers within thresholds
  1 — at least one P1 threshold exceeded
  2 — only P2 threshold exceeded (warn level)
  3 — DB unreachable or config error

Thresholds (read from backend_config.yaml broker_issue_thresholds):
  auth_fail_p1:  10  — auth failures per account per day → P1
  circuit_open_p1: 5 — circuit opens per account per day → P1
  total_p1:      50  — total issues per account per day → P1
  total_p2:      20  — total issues per account per day → P2
  lookback_days:  7  — days to look back

Usage:
  python scripts/check_broker_conn_issues.py [--dry-run]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
_IST_OFFSET = timedelta(hours=5, minutes=30)


def _today_ist() -> date:
    return (datetime.now(timezone.utc) + _IST_OFFSET).date()


def _load_config() -> tuple[dict, dict]:
    """Returns (cfg, sec) from YAML files."""
    import yaml
    cfg_path = ROOT / "backend" / "config" / "backend_config.yaml"
    sec_path = ROOT / "backend" / "config" / "secrets.yaml"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    with open(sec_path, encoding="utf-8") as f:
        sec = yaml.safe_load(f) or {}
    return cfg, sec


def _build_dsn(sec: dict, cfg: dict) -> str:
    """Build asyncpg DSN from secrets, picking prod vs dev DB based on deploy_branch."""
    import os
    if dsn := os.environ.get("DATABASE_URL"):
        # Convert sqlalchemy+asyncpg:// or postgresql:// to plain asyncpg form
        return dsn.replace("postgresql+asyncpg://", "postgresql://")
    branch = cfg.get("deploy_branch", "main")
    db_name = sec.get("db") or ("ramboq" if branch == "main" else "ramboq_dev")
    host = sec.get("db_host", "localhost")
    port = sec.get("db_port", 5432)
    user = sec.get("db_user", "ramboq")
    pw   = sec.get("db_password", "")
    return f"postgresql://{user}:{pw}@{host}:{port}/{db_name}"


async def _query(dsn: str, thresholds: dict) -> list[dict]:
    """Query broker_issue_daily and return rows that breach thresholds."""
    import asyncpg
    lookback = thresholds.get("lookback_days", 7)
    since = _today_ist() - timedelta(days=lookback)

    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        rows = await conn.fetch("""
            SELECT broker_id, account, issue_date, issue_count, breakdown
            FROM broker_issue_daily
            WHERE issue_date >= $1
            ORDER BY issue_date DESC, issue_count DESC
        """, since)
    finally:
        await conn.close()
    return [dict(r) for r in rows]


def _check_thresholds(rows: list[dict], thresholds: dict) -> list[dict]:
    """Return list of breach dicts with severity."""
    auth_fail_p1    = thresholds.get("auth_fail_p1",    10)
    circuit_open_p1 = thresholds.get("circuit_open_p1",  5)
    total_p1        = thresholds.get("total_p1",        50)
    total_p2        = thresholds.get("total_p2",        20)

    breaches = []
    for row in rows:
        bd = row.get("breakdown") or {}
        total = row["issue_count"]
        auth_fail    = bd.get("auth_fail", 0)
        circuit_open = bd.get("circuit_open", 0)

        sev = None
        if total >= total_p1 or auth_fail >= auth_fail_p1 or circuit_open >= circuit_open_p1:
            sev = "P1"
        elif total >= total_p2:
            sev = "P2"

        if sev:
            parts = [f"auth_fail={auth_fail}", f"circuit_open={circuit_open}",
                     f"fetch_fail={bd.get('fetch_fail', 0)}",
                     f"rotation_detected={bd.get('rotation_detected', 0)}"]
            breaches.append({
                "severity": sev,
                "broker_id": row["broker_id"],
                "account": row["account"],
                "date": str(row["issue_date"]),
                "total": total,
                "detail": ", ".join(parts),
            })
    return breaches


def main() -> None:
    parser = argparse.ArgumentParser(description="Check broker connection issue thresholds")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be checked without connecting to DB")
    args = parser.parse_args()

    try:
        cfg, sec = _load_config()
    except Exception as e:
        print(f"check_broker_conn: config load failed: {e}", file=sys.stderr)
        sys.exit(3)

    thresholds = cfg.get("broker_issue_thresholds", {
        "auth_fail_p1": 10, "circuit_open_p1": 5,
        "total_p1": 50, "total_p2": 20, "lookback_days": 7,
    })

    if args.dry_run:
        print(f"check_broker_conn: would query broker_issue_daily with thresholds {thresholds}")
        sys.exit(0)

    try:
        dsn = _build_dsn(sec, cfg)
        rows = asyncio.run(_query(dsn, thresholds))
    except Exception as e:
        print(f"check_broker_conn: DB unreachable: {e}", file=sys.stderr)
        sys.exit(3)

    breaches = _check_thresholds(rows, thresholds)

    if not breaches:
        print(f"check_broker_conn: ok — {len(rows)} rows checked, no threshold breaches")
        sys.exit(0)

    has_p1 = any(b["severity"] == "P1" for b in breaches)
    for b in breaches:
        print(f"{b['severity']} broker={b['broker_id']} account={b['account']} "
              f"date={b['date']} total={b['total']} ({b['detail']})")

    sys.exit(1 if has_p1 else 2)


if __name__ == "__main__":
    main()
