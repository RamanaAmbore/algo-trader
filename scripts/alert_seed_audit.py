#!/usr/bin/env python3
"""
alert_seed_audit.py — Pre-bump audit for `event_agents.SEED_VERSION`.

Read-only. Connects to the live `agents` table, and for every row with
`kind='event'` reports, using the REAL functions this codebase ships
(never a raw-SQL or hand-rolled reimplementation):

  - `event_agents.seed_action(row, current_version=<target>)` — what a
    seed pass AT THE TARGET VERSION would do to this row: 'update',
    'modified_skip', or 'keep'.
  - Whether `compute_seed_spec_hash(row.conditions, row.events,
    row.actions) == row.seed_spec_hash` (a `None` stored hash — a
    legacy/never-seeded row — is reported separately, not folded into
    a false "mismatch").
  - The channel set that would actually dispatch TODAY
    (`alert_routing.resolve_channels(name=slug, priority=row.priority,
    agent_events=row.events)` — the exact call `_dispatch_agent` makes)
    versus what it would dispatch AFTER a fresh reseed at the target
    version (same call, but with the CODE's own spec fields instead of
    the row's current ones). A agent whose dispatched channels would
    change is flagged loudly — this is the one thing a hash-match
    check alone cannot tell you, since the hash only covers
    conditions/events/actions verbatim equality, not what the routing
    tree resolves a (possibly now-empty) `events` list to.

Also reports:
  - Any `SEEDED_AGENTS` slug with no matching DB row (would INSERT).
  - Any `kind='event'` DB row whose slug is not in `SEEDED_AGENTS`
    (an operator-authored event agent, not a seeded one — not an
    error, just surfaced so it isn't mistaken for a seeded row that
    vanished).

Never calls `seed_event_agents()`, never writes, never commits.

Usage:
    ./venv/bin/python scripts/alert_seed_audit.py [--target-version N]

DB connection: reads the REAL `backend.shared.helpers.utils.secrets`/
`config` YAML-backed dicts (the same ones `backend.api.database._build_url()`
reads) — NOT scripts/check_stale_snapshots.py's naive line-by-line YAML
parser. That parser failed auth against this script's real target DB
during development of this script (confirmed live, both for a first
draft of this script and for the pre-existing check_stale_snapshots.py
run the same way), and the exact failure mode was never root-caused
further than "don't hand-roll YAML parsing" — see this module's own
`_build_dsn()` comment below for what WAS isolated (a real DSN-breaking
bug, unescaped special characters in the user/password). Resolves the
`backend`/`config` directory from `backend.shared.helpers.utils`'s OWN
file location, not this script's cwd — so running it from /opt/ramboq
talks to `ramboq`; from /opt/ramboq_dev it talks to `ramboq_dev`. Or set
DATABASE_URL to override entirely. Run as a user that can read
secrets.yaml (e.g. `sudo -u www-data ./venv/bin/python
scripts/alert_seed_audit.py` on the server).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Config loading ──────────────────────────────────────────────────────
#
# Reuses the SAME real YAML-backed `secrets`/`config` module-level dicts
# `backend.api.database._build_url()` reads — not a hand-rolled parser.
# An early draft of this script used a minimal line-by-line YAML parser
# (the same pattern scripts/check_stale_snapshots.py already used) and
# it failed authentication against the real server DB — confirmed live
# both for that draft AND for check_stale_snapshots.py itself run the
# same way. The EXACT line-parser defect was never isolated (plausible
# candidates: a `#` inside the password being read as a comment start,
# or a quoting edge case) — what matters operationally is that
# hand-rolled YAML parsing of a real secrets.yaml is not safe to rely
# on, so this script uses the real loader instead. `backend.shared.
# helpers.utils` resolves its own config directory from ITS OWN file
# location, not the caller's cwd, so this works correctly even when
# this script is copied to a scratch path outside `scripts/` as long as
# it still imports the real installed `backend` package for that
# checkout (i.e. run with that checkout's own venv from that checkout's
# own directory tree).
#
# Separately, and DEFINITIVELY confirmed by the traceback this produced
# live (`ValueError: invalid literal for int() with base 10:
# '<password-fragment>'`): the real secrets.yaml password contains an
# unescaped `@` character, which a plain f-string DSN
# (`postgresql://{user}:{password}@{host}:{port}/{db}`) cannot
# distinguish from the user/password-vs-host separator — asyncpg's own
# DSN parser then tried to read a chunk of the password as the port
# number. `urllib.parse.quote()` on both `user` and `password` below is
# the actual, confirmed fix for that bug (unrelated to the line-parser
# question above).

def _build_dsn() -> str:
    env_url = os.environ.get("DATABASE_URL", "")
    if env_url:
        return env_url.replace("postgresql+asyncpg://", "postgresql://")

    from backend.shared.helpers.utils import config, secrets

    from urllib.parse import quote

    user = quote(str(secrets.get("db_user", "rambo_admin")), safe="")
    password = quote(str(secrets.get("db_password", "")), safe="")
    host = secrets.get("db_host", "localhost")
    port = secrets.get("db_port", 5432)

    branch = config.get("deploy_branch", "dev")
    db_name = "ramboq" if branch == "main" else "ramboq_dev"
    return f"postgresql://{user}:{password}@{host}:{port}/{db_name}"


def _redacted_dsn(dsn: str) -> str:
    import re
    return re.sub(r"(://[^:]+:)[^@]+(@)", r"\1***\2", dsn)


def _parse_jsonish(val):
    """asyncpg returns a jsonb column as a raw JSON string (no codec
    registered) — decode it; pass through anything already decoded
    (e.g. if a future caller registers a codec) and None unchanged."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    return json.loads(val)


# ── DB read ──────────────────────────────────────────────────────────────

async def _fetch_event_agent_rows(dsn: str) -> list[SimpleNamespace]:
    import asyncpg  # type: ignore[import]

    asyncpg_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(asyncpg_dsn, timeout=10)
    try:
        rows = await conn.fetch(
            "SELECT slug, conditions, events, actions, priority, "
            "seed_version, seed_spec_hash FROM agents WHERE kind = 'event' "
            "ORDER BY slug"
        )
    finally:
        await conn.close()

    out = []
    for r in rows:
        out.append(SimpleNamespace(
            slug=r["slug"],
            conditions=_parse_jsonish(r["conditions"]) or {},
            events=_parse_jsonish(r["events"]) or [],
            actions=_parse_jsonish(r["actions"]) or [],
            priority=r["priority"],
            seed_version=r["seed_version"],
            seed_spec_hash=r["seed_spec_hash"],
        ))
    return out


# ── Audit ────────────────────────────────────────────────────────────────

def _audit(rows: list[SimpleNamespace], target_version: int) -> dict:
    from backend.api.algo import alert_routing, event_agents

    rows_by_slug = {r.slug: r for r in rows}
    specs_by_slug = {spec["slug"]: spec for spec in event_agents.SEEDED_AGENTS}

    per_slug: list[dict] = []
    for slug, spec in specs_by_slug.items():
        row = rows_by_slug.get(slug)
        if row is None:
            per_slug.append({"slug": slug, "db_row": "MISSING (would insert)"})
            continue

        action = event_agents.seed_action(row, current_version=target_version)
        hash_match = None
        if row.seed_spec_hash:
            current_hash = event_agents.compute_seed_spec_hash(
                row.conditions, row.events, row.actions)
            hash_match = current_hash == row.seed_spec_hash

        today_channels = alert_routing.resolve_channels(
            name=slug, priority=row.priority, agent_events=row.events)
        post_bump_channels = alert_routing.resolve_channels(
            name=slug, priority=spec.get("priority"),
            agent_events=spec.get("events", []))
        channels_change = today_channels != post_bump_channels

        per_slug.append({
            "slug": slug,
            "seed_version_in_db": row.seed_version,
            "seed_action_at_target": action,
            "hash_match": hash_match,
            "hash_is_null": not bool(row.seed_spec_hash),
            "today_channels": today_channels,
            "post_bump_channels": post_bump_channels,
            "channels_would_change": channels_change,
        })

    unseeded_rows = sorted(set(rows_by_slug) - set(specs_by_slug))
    return {"per_slug": per_slug, "unseeded_db_rows": unseeded_rows}


def _render(report: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append("alert_seed_audit — per-slug report")
    lines.append("=" * 72)
    any_risk = False
    for entry in report["per_slug"]:
        slug = entry["slug"]
        if entry.get("db_row") == "MISSING (would insert)":
            lines.append(f"  {slug:30s} MISSING — seed would INSERT a new row")
            continue
        action = entry["seed_action_at_target"]
        hash_tag = (
            "NULL(legacy)" if entry["hash_is_null"]
            else ("MATCH" if entry["hash_match"] else "MISMATCH")
        )
        chg_tag = "CHANGES" if entry["channels_would_change"] else "unchanged"
        risky = action == "modified_skip" or entry["channels_would_change"]
        any_risk = any_risk or risky
        marker = "  !! " if risky else "     "
        lines.append(
            f"{marker}{slug:30s} action={action:14s} hash={hash_tag:12s} "
            f"dispatched_channels={chg_tag}"
        )
        if entry["channels_would_change"]:
            lines.append(f"         today:      {entry['today_channels']}")
            lines.append(f"         post-bump:  {entry['post_bump_channels']}")
    if report["unseeded_db_rows"]:
        lines.append("")
        lines.append("kind='event' DB rows with no matching SEEDED_AGENTS spec "
                      "(operator-authored, not a seeded agent — informational only):")
        for slug in report["unseeded_db_rows"]:
            lines.append(f"    {slug}")
    lines.append("")
    lines.append("RISK FOUND" if any_risk else "No risk found — all seeded rows are "
                  "'update'/'keep' with hash-match (or legacy-null) and no dispatched-"
                  "channel change.")
    return "\n".join(lines)


async def _main_async(target_version: int) -> int:
    dsn = _build_dsn()
    print(f"[alert_seed_audit] connecting to {_redacted_dsn(dsn)}", file=sys.stderr)
    rows = await _fetch_event_agent_rows(dsn)
    report = _audit(rows, target_version)
    print(_render(report))
    return 1 if any(
        e.get("seed_action_at_target") == "modified_skip" or e.get("channels_would_change")
        for e in report["per_slug"]
    ) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-version", type=int, default=None,
        help="SEED_VERSION to simulate bumping to (default: the code's "
             "current event_agents.SEED_VERSION + 1, i.e. 'what would "
             "happen on the next bump').",
    )
    args = parser.parse_args()
    from backend.api.algo.event_agents import SEED_VERSION
    target_version = args.target_version if args.target_version is not None else SEED_VERSION + 1
    return asyncio.run(_main_async(target_version))


if __name__ == "__main__":
    sys.exit(main())
