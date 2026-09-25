"""
One-off migration: soft-disable 3 redundant built-in loss/rate agents.

Sets ``status='inactive'`` for exactly 3 built-in `agents` rows:

  - loss-rate-acct        (per-account burn-rate — redundant now that
                            loss-positions-total covers aggregate rate)
  - loss-positions-acct   (per-account static loss — same reasoning)
  - loss-margin-low       (already inactive in most environments; this
                            migration makes prod match that state
                            explicitly rather than relying on the
                            bidirectional status auto-sync in
                            seed_agents()/_ae_sync_builtin_status, which
                            only flips active<->inactive and does NOT
                            touch a row currently sitting in a
                            'cooldown' status — loss-positions-acct on
                            prod was observed in 'cooldown', not
                            'active', so the auto-sync alone would NOT
                            have fixed it on next deploy/restart)

`loss-positions-total` becomes the sole aggregate rate/loss agent.
`loss-funds-negative` (account-level negative cash/margin) is explicitly
UNCHANGED by this script.

This is a soft-disable (status flip), not a DELETE — `agent_events.agent_id`
has ON DELETE CASCADE, so a hard delete would destroy the historical audit
trail of every past fire from these agents. Setting status='inactive'
achieves "stops firing" without losing history, matching the existing
convention already used for `loss-margin-low` / `expiry-day-positions-alert`.

Run on dev first (if desired), verify, then run on prod:

  # dev
  cd /opt/ramboq_dev && ./venv/bin/python scripts/migrate_reduce_alert_agents_2026_09.py --dry-run
  cd /opt/ramboq_dev && ./venv/bin/python scripts/migrate_reduce_alert_agents_2026_09.py

  # prod
  cd /opt/ramboq && ./venv/bin/python scripts/migrate_reduce_alert_agents_2026_09.py --dry-run
  cd /opt/ramboq && ./venv/bin/python scripts/migrate_reduce_alert_agents_2026_09.py

The script reads the DB URL from the same path as the API
(secrets.yaml + backend_config.yaml -> deploy_branch -> db_name), via
`backend.api.database.async_session` — same connection pattern as
`scripts/fix_email_verified.py`.

--dry-run prints the before-state and what WOULD change, without executing
any UPDATE or commit.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select, update

from backend.api.database import async_session
from backend.api.models import Agent
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# The exact 3 slugs this migration soft-disables. Nothing else is touched.
TARGET_SLUGS: list[str] = [
    "loss-rate-acct",
    "loss-positions-acct",
    "loss-margin-low",
]


async def _fetch_state(session) -> list[tuple[str, str]]:
    """Return [(slug, status), ...] for the target slugs, ordered by slug."""
    q = (
        select(Agent.slug, Agent.status)
        .where(Agent.slug.in_(TARGET_SLUGS))
        .order_by(Agent.slug)
    )
    rows = (await session.execute(q)).all()
    return [(r.slug, r.status) for r in rows]


def _print_state(label: str, state: list[tuple[str, str]]) -> None:
    print(f"[{label}]")
    for slug, status in state:
        print(f"  {slug:<24} status={status}")
    if not state:
        print("  (no matching rows found)")


async def migrate_reduce_alert_agents(dry_run: bool = False) -> int:
    """
    Soft-disable the 3 target agent slugs.

    Prints before-state always. In --dry-run mode, prints the SQL that
    would run and returns 0 without touching the DB. Otherwise applies
    the UPDATE, commits, prints after-state, and returns the row count
    affected.
    """
    async with async_session() as session:
        before = await _fetch_state(session)
        _print_state("before", before)

        stmt = (
            update(Agent)
            .where(Agent.slug.in_(TARGET_SLUGS))
            .values(status="inactive")
        )

        if dry_run:
            print()
            print("[dry-run] Would execute:")
            print(
                "  UPDATE agents SET status='inactive' "
                "WHERE slug IN ('loss-rate-acct', 'loss-positions-acct', "
                "'loss-margin-low')"
            )
            already_inactive = [s for s, st in before if st == "inactive"]
            would_change = [s for s, st in before if st != "inactive"]
            print(f"[dry-run] Would change {len(would_change)} row(s): {would_change}")
            if already_inactive:
                print(
                    f"[dry-run] Already inactive, no-op: {already_inactive}"
                )
            print("[dry-run] No changes applied.")
            return 0

        result = await session.execute(stmt)
        await session.commit()
        rows_updated: int = result.rowcount

        after = await _fetch_state(session)
        print()
        _print_state("after", after)

        logger.info(
            "migrate_reduce_alert_agents_2026_09: committed — %d row(s) updated "
            "(slugs=%s)",
            rows_updated,
            TARGET_SLUGS,
        )
        return rows_updated


def _parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=(
            "One-off migration: set status='inactive' for "
            "loss-rate-acct, loss-positions-acct, loss-margin-low."
        )
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print before-state and the UPDATE that would run, without executing it.",
    )
    return ap.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    updated = asyncio.run(migrate_reduce_alert_agents(dry_run=args.dry_run))
    if args.dry_run:
        print("migrate_reduce_alert_agents_2026_09: dry-run complete, 0 row(s) changed")
    else:
        print(f"migrate_reduce_alert_agents_2026_09: {updated} row(s) updated")
