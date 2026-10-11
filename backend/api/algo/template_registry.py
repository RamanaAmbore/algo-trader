"""
Agent template registry — saved $ref-able sub-trees (notify + condition).

A Template is an `Agent` row with `kind='template'` that another agent's
condition tree or events list can reference via `{"$ref": "<slug>"}`. The
engine resolves the ref against this registry at evaluation / dispatch
time.

This module owns:

  - in-memory cache of {kind: {slug: body}} for fast lookup
  - reload()                       — pull all kind='template' Agent rows on boot / reload
  - resolve_events(events)         — expand $ref entries in a notify list
  - SYSTEM_AGENT_TEMPLATES         — declarative seed list (mirrors the
                                      grammar.SYSTEM_TOKENS pattern)
  - seed_agent_templates()         — upsert system templates (as Agent
                                      rows) on startup
  - migrate_and_retire_agent_fragments() — one-time, self-contained
                                      backfill-then-DROP of the legacy
                                      `agent_fragments` table (Sprint D2)

Cycle detection: condition $refs walk a visited set so A → B → A loops
log a warning and return [] instead of recursing forever.

History: renamed from fragment_registry.py / FragmentRegistry /
SYSTEM_FRAGMENTS in v2.1 (operator vocabulary unified under "templates").
Sprint D (2026-10, Agent/Bracket/Template unification phase) additively
migrated every `agent_fragments` row into a matching `Agent(kind=
'template')` row, keeping the old table as a transitional fallback read.
Sprint D2 (same phase) retired that table + the `AgentTemplate` model
entirely — `reload()` now sources ONLY from `Agent` rows, and
`migrate_and_retire_agent_fragments()` (below) is the one-time migration
that backfills any not-yet-migrated row and drops the old table in the
same boot, self-contained (it does not assume Sprint D's own migration
ever ran in this environment — confirmed via `git merge-base` that
neither dev nor prod had deployed Sprint D before D2 landed).
"""

from __future__ import annotations

import threading
from typing import Any

import sqlalchemy as sa

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
        """Replace the in-memory cache with the latest kind='template'
        Agent rows. Called on app startup AND on every mutation
        (create_agent/update_agent/delete for kind='template' rows) so
        edits land without a restart.

        Sprint D2 (Agent/Bracket/Template unification phase) — `Agent`
        rows are the ONLY source; the legacy `agent_fragments` table +
        its fallback-read path (Sprint D's transitional dual-source read)
        were retired this sprint, once `migrate_and_retire_agent_
        fragments()` confirmed every row had a matching Agent row.

        Bucketing a kind='template' Agent row into 'condition' vs
        'notify' is by CONTENT SHAPE, not a stored discriminator column:
        a non-empty `conditions` dict is a condition fragment; a
        non-empty `events` list is a notify fragment. The cache key is
        `Agent.slug` — every Template-authoring path (the CLI's
        `token(...)` statement, `seed_agent_templates()`, the retirement
        migration's backfill) sets `slug` equal to the Template's own
        name, so slug and the $ref name are always identical.

        Status is intentionally NOT filtered on the Agent side — a
        template is never independently fired (run_cycle()/
        event_agents.py are positive kind=='cycle'/kind=='event'
        allowlists), so active/inactive has no bearing on whether a
        $ref can resolve it. This also sidesteps a trap: create_agent
        always writes new rows with status='inactive' regardless of
        kind, which would otherwise make a freshly API-authored template
        unresolvable until an operator remembered to activate it."""
        from sqlalchemy import select
        from backend.api.database import async_session
        from backend.api.models import Agent

        next_cache: dict[str, dict[str, list | dict]] = {
            "notify": {}, "condition": {},
        }
        async with async_session() as s:
            result = await s.execute(select(Agent).where(Agent.kind == "template"))
            for a in result.scalars().all():
                if isinstance(a.conditions, dict) and a.conditions:
                    next_cache.setdefault("condition", {})[a.slug] = a.conditions
                if isinstance(a.events, list) and a.events:
                    next_cache.setdefault("notify", {})[a.slug] = a.events
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
    """Insert/update system Templates — as `Agent(kind='template',
    is_system=True)` rows directly (Sprint D2; there is no `AgentTemplate`
    table to seed into anymore). Custom (is_system=False) rows are never
    touched. An existing system row has its `conditions`/`events`/
    `description` refreshed from code so operator edits to a system row
    are intentionally overwritten — operators clone to a custom template
    if they want a permanent change.

    Slug collision guards (same spirit as the retired `mirror_fragment_
    upsert`'s guards): a slug already used by a non-template Agent, or
    already used by a custom (is_system=False) Template, is left alone
    with a log line rather than being overwritten."""
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import Agent

    async with async_session() as s:
        inserted = updated = skipped = 0
        for spec in SYSTEM_AGENT_TEMPLATES:
            slug = spec["name"]
            conditions = spec["body"] if spec["kind"] == "condition" else {}
            events = spec["body"] if spec["kind"] == "notify" else []
            existing = (await s.execute(
                select(Agent).where(Agent.slug == slug)
            )).scalar_one_or_none()
            if existing is None:
                s.add(Agent(
                    slug=slug, name=slug,
                    description=spec.get("description", ""),
                    conditions=conditions, events=events, actions=[],
                    kind="template", is_system=True, status="active",
                ))
                inserted += 1
            elif existing.kind != "template":
                logger.critical(
                    f"template seed: slug {slug!r} already used by a "
                    f"non-template agent (id={existing.id}) — skipping seed"
                )
                skipped += 1
            elif not existing.is_system:
                logger.warning(
                    f"template seed: slug {slug!r} exists as a custom "
                    f"(non-system) template — leaving it alone"
                )
                skipped += 1
            else:
                existing.description = spec.get("description", existing.description or "")
                existing.conditions = conditions
                existing.events = events
                updated += 1
        await s.commit()
        logger.info(
            f"Agent templates seeded — inserted={inserted} updated={updated} "
            f"skipped={skipped}"
        )
    # Hot-rebuild the in-memory cache.
    await REGISTRY.reload()


# ═══════════════════════════════════════════════════════════════════════════
#  Sprint D2 (Agent/Bracket/Template unification phase) — one-time,
#  self-contained retirement of the legacy `agent_fragments` table.
# ═══════════════════════════════════════════════════════════════════════════
#
# This function does NOT assume Sprint D's own additive migration
# (`migrate_fragments_to_template_agents()` / `mirror_fragment_upsert()`,
# both deleted this sprint along with the `AgentTemplate` model and the
# `/api/admin/fragments` CRUD route that was their only caller) ever ran
# in this environment — confirmed via `git merge-base --is-ancestor` that
# neither dev nor prod had deployed Sprint D before this sprint landed.
# Backfill and DROP happen together, in the SAME boot: a custom fragment
# that was never migrated must never be silently lost by a DROP running
# before its backfill.
#
# Reads the OLD table via a plain (non-ORM-mapped) `sqlalchemy.Table`
# object — not the deleted `AgentTemplate` class — using the generic
# `sa.JSON` column type (not `postgresql.JSONB`) so the same code path
# is exercisable against the sqlite test double this codebase's other
# migration tests already use.

_LEGACY_FRAGMENTS_TABLE = sa.Table(
    "agent_fragments", sa.MetaData(),
    sa.Column("id", sa.Integer, primary_key=True),
    sa.Column("kind", sa.String),
    sa.Column("name", sa.String),
    sa.Column("body", sa.JSON),
    sa.Column("description", sa.String),
    sa.Column("is_system", sa.Boolean),
    sa.Column("is_active", sa.Boolean),
)


def _tr_agent_content_kind(agent) -> str | None:
    """Infer which legacy fragment `kind` an Agent(kind='template') row's
    content shape corresponds to (no stored discriminator column — see
    `reload()`'s own docstring). None when neither shape is populated."""
    if isinstance(agent.conditions, dict) and agent.conditions:
        return "condition"
    if isinstance(agent.events, list) and agent.events:
        return "notify"
    return None


async def migrate_and_retire_agent_fragments() -> None:
    """One-time, idempotent, self-contained backfill-then-DROP of the
    legacy `agent_fragments` table. Safe to call on every boot:

      1. No-ops immediately if the table is already gone (checked via
         dialect-portable `sqlalchemy.inspect(...).has_table()`, not a
         Postgres-only `to_regclass()` call, so the exact same function
         is exercisable against the sqlite test double this codebase's
         other migration tests already use).
      2. Reads every row from the OLD table (raw Core `Table`, not the
         deleted `AgentTemplate` ORM class).
      3. For each row whose `name` doesn't already have a matching
         `Agent(kind='template', slug=name)` row, inserts one (mirrors
         the retired `mirror_fragment_upsert(force=False)`'s guards:
         name > 64 chars, slug already owned by a non-template agent,
         or a notify/condition name collision all skip that one row
         with a CRITICAL log, rather than aborting the whole pass).
      4. If every row ended up matched (inserted this pass, OR already
         present — e.g. a system Template `seed_agent_templates()` just
         seeded directly, or a custom row a prior partial boot already
         backfilled), re-verifies via a SQL anti-join (belt-and-suspenders
         against a transaction-visibility edge case) and only THEN drops
         the old table.
      5. If anything is unmatched, logs CRITICAL with the offending names
         and leaves `agent_fragments` in place for manual review — never
         drops a table that still holds un-migrated data.
    """
    from sqlalchemy import select, text, inspect as sa_inspect
    from backend.api.database import async_session
    from backend.api.models import Agent

    async with async_session() as s:
        has_table = await s.run_sync(
            lambda sync_session: sa_inspect(sync_session.get_bind()).has_table("agent_fragments")
        )
        if not has_table:
            return   # already dropped — nothing to do

        rows = (await s.execute(select(_LEGACY_FRAGMENTS_TABLE))).all()
        unmatched: list[str] = []
        for row in rows:
            name, kind, body = row.name, row.kind, row.body
            if len(name) > 64:
                logger.critical(
                    f"agent_fragments retirement: name {name!r} exceeds "
                    f"Agent.slug's 64-char limit — leaving row un-migrated"
                )
                unmatched.append(name)
                continue
            existing = (await s.execute(
                select(Agent).where(Agent.slug == name)
            )).scalar_one_or_none()
            if existing is not None:
                if existing.kind != "template":
                    logger.critical(
                        f"agent_fragments retirement: slug {name!r} already "
                        f"used by a non-template agent (id={existing.id}) "
                        f"— leaving row un-migrated"
                    )
                    unmatched.append(name)
                    continue
                prior_kind = _tr_agent_content_kind(existing)
                if prior_kind is not None and prior_kind != kind:
                    logger.critical(
                        f"agent_fragments retirement: name {name!r} is used "
                        f"by both a notify and a condition fragment — "
                        f"leaving row un-migrated (existing Agent row is "
                        f"kind={prior_kind})"
                    )
                    unmatched.append(name)
                    continue
                continue   # already matched — nothing to insert
            conditions = body if (kind == "condition" and isinstance(body, dict)) else {}
            events = body if (kind == "notify" and isinstance(body, list)) else []
            s.add(Agent(
                slug=name, name=name, description=row.description or "",
                conditions=conditions, events=events, actions=[],
                kind="template", is_system=bool(row.is_system),
                status="active" if row.is_active else "inactive",
            ))
        await s.commit()

        if unmatched:
            logger.critical(
                f"agent_fragments retirement: {len(unmatched)} row(s) could "
                f"not be matched to an Agent row ({sorted(unmatched)}) — "
                f"table LEFT IN PLACE for manual review"
            )
            return

        remaining = (await s.execute(text(
            "SELECT af.name FROM agent_fragments af "
            "LEFT JOIN agents a ON a.slug = af.name AND a.kind = 'template' "
            "WHERE a.id IS NULL"
        ))).scalars().all()
        if remaining:
            logger.critical(
                f"agent_fragments retirement: anti-join still finds "
                f"{list(remaining)} unmatched after backfill — not dropping"
            )
            return

        # SET LOCAL is Postgres-only syntax (no sqlite equivalent) — this
        # bounds how long the DROP can wait behind any in-flight lock on
        # `agent_fragments` in production; harmless to skip in the
        # sqlite test double, which has no such lock-contention concern.
        dialect_name = s.get_bind().dialect.name
        if dialect_name == "postgresql":
            await s.execute(text("SET LOCAL lock_timeout = '5s'"))
        await s.execute(text("DROP TABLE agent_fragments"))
        await s.commit()
    logger.info("agent_fragments table dropped — retirement complete")
