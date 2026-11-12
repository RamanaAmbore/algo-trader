#!/usr/bin/env python3
"""
alert_channel_authoring_audit.py — Read-only inventory of every place a
single `Agent` row currently carries its OWN alert-channel configuration,
ahead of the 2026-11 channel-authoring retirement (removal of the
`notify=[...]` action-call parameter and the two hardcoded producer
alert-channel literals — see CLAUDE.md's "channel-authoring retirement"
entries).

Per `Agent` row, reports:
  - Whether `preamble` contains a `{"type": "alert", ...}` entry — the
    compiled shape of an operator-authored standalone `ALERT ...`
    statement placed in an agent's preamble.
  - Whether any entry in `actions` carries a `notify` sibling key — the
    `notify=[...]` action-call parameter being retired in this same
    pass, with NO replacement.
  - The current contents of `events` and `has_explicit_alert` — the
    DO-level `alert(...)` clause's own compiled output. NOTE: per the
    operator's explicit correction mid-plan, `agent.events` /
    `has_explicit_alert` / the `alert(...)` DSL clause are NOT being
    retired or migrated by this pass — they remain a legitimate,
    working per-agent channel-selection mechanism. This script reports
    their contents purely for visibility; nothing in this plan clears
    or strips them.

This script makes NO writes, NEVER calls any seed/migration function,
and is NOT a gate for anything downstream — informational only, kept
for the operator's/implementer's own reference.

DB connection: reuses the exact same real YAML-backed `secrets`/`config`
loader `backend.api.database._build_url()` and `scripts/alert_seed_
audit.py` already use (never a hand-rolled line-by-line YAML parser —
see that script's own `_build_dsn()` comment for why). Resolves the
`backend`/`config` directory from `backend.shared.helpers.utils`'s OWN
file location, not this script's cwd.

Usage:
    ./venv/bin/python scripts/alert_channel_authoring_audit.py
    ./venv/bin/python scripts/alert_channel_authoring_audit.py --json
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


def _build_dsn() -> str:
    """Identical to `scripts/alert_seed_audit.py:_build_dsn()` — see that
    module's own docstring for why a hand-rolled YAML/DSN parser is not
    safe to reuse here (unescaped `#`/`@` in a real secrets.yaml
    password)."""
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
    registered) — decode it; pass through anything already decoded, and
    `None` unchanged."""
    if val is None:
        return None
    if isinstance(val, (dict, list)):
        return val
    return json.loads(val)


async def _fetch_agent_rows(dsn: str) -> list[SimpleNamespace]:
    import asyncpg  # type: ignore[import]

    asyncpg_dsn = dsn.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(asyncpg_dsn, timeout=10)
    try:
        rows = await conn.fetch(
            "SELECT id, slug, kind, preamble, actions, events, "
            "has_explicit_alert FROM agents ORDER BY id"
        )
    finally:
        await conn.close()

    out = []
    for r in rows:
        out.append(SimpleNamespace(
            id=r["id"],
            slug=r["slug"],
            kind=r["kind"],
            preamble=_parse_jsonish(r["preamble"]) or [],
            actions=_parse_jsonish(r["actions"]) or [],
            events=_parse_jsonish(r["events"]) or [],
            has_explicit_alert=r["has_explicit_alert"],
        ))
    return out


def _preamble_has_alert_entry(preamble: list) -> bool:
    return any(
        isinstance(e, dict) and e.get("type") == "alert" for e in (preamble or [])
    )


def _actions_with_notify(actions: list) -> list[dict]:
    return [
        a for a in (actions or [])
        if isinstance(a, dict) and "notify" in a
    ]


def _audit(rows: list[SimpleNamespace]) -> list[dict]:
    report: list[dict] = []
    for row in rows:
        notify_actions = _actions_with_notify(row.actions)
        entry = {
            "id": row.id,
            "slug": row.slug,
            "kind": row.kind,
            "preamble_has_alert_entry": _preamble_has_alert_entry(row.preamble),
            "actions_with_notify_count": len(notify_actions),
            "actions_with_notify_types": sorted(
                {a.get("type") for a in notify_actions if a.get("type")}
            ),
            "events": row.events,
            "has_explicit_alert": row.has_explicit_alert,
        }
        report.append(entry)
    return report


def _render(report: list[dict]) -> str:
    lines: list[str] = []
    lines.append("=" * 88)
    lines.append("alert_channel_authoring_audit — per-agent report")
    lines.append("=" * 88)
    any_notify = False
    any_preamble_alert = False
    any_explicit = False
    for entry in report:
        flags = []
        if entry["preamble_has_alert_entry"]:
            flags.append("preamble-alert")
            any_preamble_alert = True
        if entry["actions_with_notify_count"]:
            flags.append(f"notify x{entry['actions_with_notify_count']}"
                         f"({','.join(entry['actions_with_notify_types'])})")
            any_notify = True
        if entry["has_explicit_alert"]:
            flags.append("has_explicit_alert")
            any_explicit = True
        if entry["events"]:
            flags.append(f"events={entry['events']}")
        if not flags:
            continue
        lines.append(
            f"  id={entry['id']:<6} slug={entry['slug']:<32} kind={entry['kind']:<8} "
            f"{' '.join(flags)}"
        )
    lines.append("")
    lines.append(
        f"notify= on actions: {'FOUND' if any_notify else 'none'} — this is the "
        f"mechanism being retired with no replacement in this pass."
    )
    lines.append(
        f"preamble {{type:'alert'}} entries: {'FOUND' if any_preamble_alert else 'none'} "
        f"— informational only, NOT touched by this pass."
    )
    lines.append(
        f"has_explicit_alert=True rows: {'FOUND' if any_explicit else 'none'} — "
        f"informational only, NOT touched by this pass."
    )
    return "\n".join(lines)


async def _main_async(as_json: bool) -> int:
    dsn = _build_dsn()
    print(f"[alert_channel_authoring_audit] connecting to {_redacted_dsn(dsn)}",
          file=sys.stderr)
    rows = await _fetch_agent_rows(dsn)
    report = _audit(rows)
    if as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(_render(report))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true",
                        help="emit the full per-agent report as JSON instead "
                             "of the human-readable summary")
    args = parser.parse_args()
    return asyncio.run(_main_async(args.json))


if __name__ == "__main__":
    sys.exit(main())
