"""
Agent template registry — saved $ref-able sub-trees (notify + condition).

An AgentTemplate is a saved JSONB body that an agent can reference via
`{"$ref": "<name>"}`. The engine resolves the ref against this
registry at evaluation / dispatch time.

This module owns:

  - in-memory cache of {kind: {name: body}} for fast lookup
  - load_from_db()                 — pull all active templates on boot / reload
  - resolve_events(events)         — expand $ref entries in a notify list
  - SYSTEM_AGENT_TEMPLATES         — declarative seed list (mirrors the
                                      grammar.SYSTEM_TOKENS pattern)
  - seed_agent_templates()         — upsert system templates on startup

Cycle detection: condition $refs walk a visited set so A → B → A loops
log a warning and return [] instead of recursing forever.

Renamed from fragment_registry.py / TemplateRegistry / SYSTEM_AGENT_TEMPLATES
in v2.1 — operator vocabulary unified under "templates" (order
templates + agent templates). DB table keeps its historical name
`agent_fragments` so existing rows survive without a migration. The
old `seed_fragments` / `resolve_events` callable names stay as
backward-compatible aliases (see end of file).
"""

from __future__ import annotations

import threading
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  SYSTEM_AGENT_TEMPLATES — seeded on every boot
# ═══════════════════════════════════════════════════════════════════════════
#
# Two notify fragments cover the patterns repeated across BUILTIN_AGENTS:
#   1. Critical trio  — telegram + email + log. Used by every loss-* and
#                       expiry-* agent. Editing this row (e.g. to add SMS)
#                       updates every consumer in one step.
#   2. Log only       — quietest channel for diagnostic agents that shouldn't
#                       spam the operator. Reserved for future use.
#
# These don't replace `BUILTIN_AGENT.events` (which still carries the
# legacy inline shape for backward compat); they GIVE OPERATORS a saved
# reference they can swap in when authoring custom agents via the UI.

SYSTEM_AGENT_TEMPLATES: list[dict] = [
    {
        "kind": "notify",
        "name": "notify-critical-trio",
        "description": (
            "Telegram + email + log + in-app popup. The default for any "
            "critical-tier agent — every channel an operator actually "
            "monitors. Kept under the historical 'trio' name even though "
            "it now ships four channels; renaming would orphan every "
            "agent's $ref. Edit once to add/remove a channel across every "
            "consumer."
        ),
        "body": [
            {"channel": "telegram", "enabled": True},
            {"channel": "email",    "enabled": True},
            {"channel": "log",      "enabled": True},
            {"channel": "inapp",    "enabled": True},
        ],
    },
    {
        "kind": "notify",
        "name": "notify-log-only",
        "description": (
            "Just the log file. Use for diagnostic agents that shouldn't "
            "page the operator (rate-limit probes, status snapshots)."
        ),
        "body": [
            {"channel": "log", "enabled": True},
        ],
    },
    {
        "kind": "notify",
        "name": "notify-telegram-only",
        "description": (
            "Telegram only. Use when an alert should reach the operator's "
            "phone but doesn't need to clutter the email thread."
        ),
        "body": [
            {"channel": "telegram", "enabled": True},
        ],
    },
    {
        "kind": "notify",
        "name": "notify-ntfy-critical",
        "description": "Telegram + ntfy + log. For critical alerts that must wake the operator.",
        "body": [
            {"channel": "telegram", "enabled": True},
            {"channel": "ntfy",     "enabled": True},
            {"channel": "log",      "enabled": True},
        ],
    },

    # ── Condition fragments (Stage 2) ────────────────────────────────────
    # These mirror the building blocks the loss-* and expiry-* agents
    # use. Seeded so the LLM / operator can compose new agents that
    # reference proven thresholds rather than re-typing them.
    {
        "kind": "condition",
        "name": "loss-positions-acct-default",
        "description": (
            "Per-account positions threshold set used by loss-positions-acct: "
            "pnl% ≤ -2 OR pnl ≤ -₹30k OR pnl_rate ≤ -₹3k/min OR "
            "pnl_rate% ≤ -0.25%/min. Reference from a new agent that "
            "wants the same trigger profile against a different "
            "notify/action set."
        ),
        "body": {"any": [
            {"metric": "pnl_pct",      "scope": "positions.any_acct", "op": "<=", "value": -2.0},
            {"metric": "pnl",          "scope": "positions.any_acct", "op": "<=", "value": -30000},
            {"metric": "pnl_rate_abs", "scope": "positions.any_acct", "op": "<=", "value": -3000},
            {"metric": "pnl_rate_pct", "scope": "positions.any_acct", "op": "<=", "value": -0.25},
        ]},
    },
    {
        "kind": "condition",
        "name": "loss-positions-total-default",
        "description": (
            "Book-wide positions threshold set: total pnl% ≤ -2 OR "
            "pnl ≤ -₹50k OR pnl_rate ≤ -₹6k/min OR rate% ≤ -0.25%/min. "
            "Same shape as loss-positions-total."
        ),
        "body": {"any": [
            {"metric": "pnl_pct",      "scope": "positions.total", "op": "<=", "value": -2.0},
            {"metric": "pnl",          "scope": "positions.total", "op": "<=", "value": -50000},
            {"metric": "pnl_rate_abs", "scope": "positions.total", "op": "<=", "value": -6000},
            {"metric": "pnl_rate_pct", "scope": "positions.total", "op": "<=", "value": -0.25},
        ]},
    },
    {
        "kind": "condition",
        "name": "near-market-close-30m",
        "description": (
            "True within the last 30 minutes of the nearest market segment "
            "close. Useful as a guard on auto-close agents that should "
            "only fire near close."
        ),
        "body": {
            "metric": "minutes_until_close",
            "scope":  "positions.total",
            "op":     "<=",
            "value":  30,
        },
    },
]


# ═══════════════════════════════════════════════════════════════════════════
#  Registry singleton
# ═══════════════════════════════════════════════════════════════════════════

class TemplateRegistry:
    _instance: "TemplateRegistry | None" = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "TemplateRegistry":
        # Double-checked locking so concurrent callers don't race on first
        # construction. All one-time state is set here inside __new__ so
        # there is nothing sensitive in __init__ that could be re-run.
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    instance._cache = {"notify": {}, "condition": {}}
                    cls._instance = instance
        return cls._instance

    def __init__(self) -> None:
        # __new__ handles all initialisation. __init__ is an explicit no-op
        # so repeated TemplateRegistry() calls never touch _cache.
        pass

    async def reload(self) -> None:
        """Replace the in-memory cache with the latest active rows from
        agent_fragments. Called on app startup AND on every CRUD
        mutation so edits land without a restart."""
        from sqlalchemy import select
        from backend.api.database import async_session
        from backend.api.models import AgentTemplate

        next_cache: dict[str, dict[str, list | dict]] = {
            "notify": {}, "condition": {},
        }
        async with async_session() as s:
            result = await s.execute(
                select(AgentTemplate).where(AgentTemplate.is_active == True)  # noqa: E712
            )
            for row in result.scalars().all():
                bucket = next_cache.setdefault(row.kind, {})
                bucket[row.name] = row.body
        self._cache = next_cache
        logger.info(
            f"TemplateRegistry reloaded — "
            f"notify={len(next_cache.get('notify', {}))} "
            f"condition={len(next_cache.get('condition', {}))}"
        )

    def get(self, kind: str, name: str) -> Any | None:
        """Lookup a fragment body. Returns None for missing names so
        callers can surface a warning + skip."""
        return self._cache.get(kind, {}).get(name)


REGISTRY = TemplateRegistry()


# ═══════════════════════════════════════════════════════════════════════════
#  Notify resolution
# ═══════════════════════════════════════════════════════════════════════════

def resolve_events(events: list | None) -> list[dict]:
    """Expand `{"$ref": "<name>"}` entries in a notify list against the
    registry. Items that aren't a $ref pass through unchanged. Missing
    refs log a warning and are skipped (no fire blocked, but the
    operator sees the broken reference).

    Stage 1 keeps this flat — a notify fragment body is a list of
    `{channel, enabled}` dicts with no nested $refs. Cycle detection
    is therefore unnecessary; it lands in Stage 2 with conditions.
    """
    if not events or not isinstance(events, list):
        return []
    out: list[dict] = []
    for entry in events:
        if not isinstance(entry, dict):
            continue
        if "$ref" in entry:
            ref_name = entry.get("$ref")
            body = REGISTRY.get("notify", ref_name)
            if body is None:
                logger.warning(
                    f"resolve_events: unknown notify fragment "
                    f"'{ref_name}' — skipping"
                )
                continue
            # Body should be a list of channel dicts; merge into out.
            if isinstance(body, list):
                for ch in body:
                    if isinstance(ch, dict):
                        out.append(dict(ch))   # shallow copy — don't mutate cache
            continue
        # Plain channel entry — pass through.
        out.append(entry)
    return out


# ═══════════════════════════════════════════════════════════════════════════
#  Seeder — upsert SYSTEM_AGENT_TEMPLATES on every app startup
# ═══════════════════════════════════════════════════════════════════════════

async def seed_agent_templates() -> None:
    """Insert/update system agent templates. Custom (is_system=False)
    rows are never touched. Existing system rows have their body and
    description refreshed from code so operator edits to system rows
    are intentionally overwritten — operators clone to a custom
    template if they want a permanent change."""
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import AgentTemplate

    async with async_session() as s:
        existing = await s.execute(
            select(AgentTemplate).where(AgentTemplate.is_system == True)  # noqa: E712
        )
        by_key = {(r.kind, r.name): r for r in existing.scalars().all()}

        inserted = updated = 0
        for spec in SYSTEM_AGENT_TEMPLATES:
            key = (spec["kind"], spec["name"])
            row = by_key.get(key)
            if row is None:
                s.add(AgentTemplate(
                    kind=spec["kind"],
                    name=spec["name"],
                    body=spec["body"],
                    description=spec.get("description", ""),
                    is_system=True,
                    is_active=True,
                ))
                inserted += 1
            else:
                # Refresh body + description; preserve is_active toggle.
                row.body = spec["body"]
                row.description = spec.get("description", row.description or "")
                updated += 1
        await s.commit()
        logger.info(
            f"Agent templates seeded — inserted={inserted} updated={updated}"
        )
    # Hot-rebuild the in-memory cache.
    await REGISTRY.reload()


# ── Backwards-compatibility shims ─────────────────────────────────────
# Pre-v2.1 callers used SYSTEM_FRAGMENTS / FragmentRegistry / seed_fragments.
# Aliasing here keeps any external script that imported the old names
# working without a code change while internal consumers migrate to the
# new vocabulary. Remove these shims in v2.2 once external surfaces
# are confirmed migrated.
SYSTEM_FRAGMENTS = SYSTEM_AGENT_TEMPLATES   # noqa: N816 — legacy name
FragmentRegistry = TemplateRegistry         # noqa: N816 — legacy alias
seed_fragments   = seed_agent_templates     # noqa: N816 — legacy name
