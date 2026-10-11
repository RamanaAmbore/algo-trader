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
        """Replace the in-memory cache with the latest rows from BOTH
        sources. Called on app startup AND on every CRUD mutation (the
        legacy /api/admin/fragments routes, and create_agent/update_agent/
        delete for kind='template' rows) so edits land without a restart.

        Sprint D (Agent/Bracket/Template unification phase) — the
        `agents` table (kind='template' rows) is now the primary source;
        `agent_fragments` (the legacy AgentTemplate table, kept fully in
        place) is read FIRST and only used as a fallback for any name not
        also present as a kind='template' Agent row, so a name that
        exists in both (the common case, post-migration — every fragment
        is mirrored into an Agent row by `migrate_fragments_to_template_
        agents()` / `mirror_fragment_upsert()`) resolves from the newer,
        canonical Agent-backed source.

        Bucketing a kind='template' Agent row into 'condition' vs
        'notify' is by CONTENT SHAPE, not a stored discriminator column:
        a non-empty `conditions` dict is a condition fragment; a
        non-empty `events` list is a notify fragment (mirrors exactly how
        AgentTemplate.body splits structurally for its two kinds). The
        cache key is `Agent.slug`, which `mirror_fragment_upsert()`
        always sets equal to the fragment's own `name` for any row it
        successfully migrates/mirrors (a genuine name collision is
        skipped with a CRITICAL log there, never silently re-slugged) —
        so slug and the original $ref name are always identical for
        every resolvable row.

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
        from backend.api.models import AgentTemplate, Agent

        next_cache: dict[str, dict[str, list | dict]] = {
            "notify": {}, "condition": {},
        }
        async with async_session() as s:
            # Legacy fallback source — unchanged read, still gated on
            # is_active same as before this sprint.
            result = await s.execute(
                select(AgentTemplate).where(AgentTemplate.is_active == True)  # noqa: E712
            )
            for row in result.scalars().all():
                bucket = next_cache.setdefault(row.kind, {})
                bucket[row.name] = row.body

            # Primary source — kind='template' Agent rows.
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


# ═══════════════════════════════════════════════════════════════════════════
#  Sprint D (Agent/Bracket/Template unification phase) — mirror
#  agent_fragments rows into kind='template' Agent rows
# ═══════════════════════════════════════════════════════════════════════════
#
# agent_fragments / AgentTemplate stay in place through this sprint — this
# is additive only. Every AgentTemplate row (system AND custom, 'notify'
# AND 'condition') gets a matching Agent(kind='template') row so $ref
# resolution (agent_evaluator.py's evaluate()/validate(), via this
# module's reload()) can source from Agent rows going forward, per the
# Sprint D brief. Slug is always set equal to the fragment's own `name`
# — never re-slugged on collision — because $ref strings reference that
# exact name; a genuine collision (another agent already owns that slug,
# or a notify and a condition fragment share the same name) is skipped
# with a CRITICAL log rather than silently diverging slug from name.

def _tr_fragment_to_agent_fields(frag) -> dict:
    """Map one AgentTemplate row onto the Agent columns a mirrored
    kind='template' row occupies. `body` lands in `conditions` (dict)
    for a 'condition' fragment, or `events` (list) for a 'notify'
    fragment — the other of the two stays at its Agent-model default
    (empty dict / empty list), which is also how `reload()` discriminates
    condition-shaped vs notify-shaped rows back out again. `actions` is
    always empty — AgentTemplate has no action-kind content today."""
    conditions = frag.body if (frag.kind == "condition" and isinstance(frag.body, dict)) else {}
    events = frag.body if (frag.kind == "notify" and isinstance(frag.body, list)) else []
    return dict(
        name=frag.name,
        description=frag.description or "",
        conditions=conditions,
        events=events,
        actions=[],
        kind="template",
        is_system=frag.is_system,
        status="active" if frag.is_active else "inactive",
    )


def _tr_agent_fragment_kind(agent) -> str | None:
    """Infer which AgentTemplate `kind` a mirrored Agent row corresponds
    to, from content shape alone (no stored discriminator column —
    see reload()'s own docstring). None when neither shape is populated
    (shouldn't happen for a row this module wrote, but a defensive
    guard against hand-edited rows)."""
    if isinstance(agent.conditions, dict) and agent.conditions:
        return "condition"
    if isinstance(agent.events, list) and agent.events:
        return "notify"
    return None


async def mirror_fragment_upsert(session, frag, *, force: bool = False) -> None:
    """Best-effort: create/update the Agent(kind='template') row that
    mirrors one AgentTemplate row, keyed by slug == frag.name.

    Never raises — every failure path logs and returns, so a fragment
    CRUD request (which already committed its own AgentTemplate write
    before calling this) never fails because of this additive safety
    net, and the one-time migration backfill never aborts partway
    through on one bad row.

    `force=False` (the migration backfill's own call shape): insert
    only if missing. An existing SYSTEM row is refreshed (mirrors
    `seed_agent_templates()`'s own "system rows are code-owned" rule);
    an existing CUSTOM row is left alone — the backfill must not clobber
    an operator's independent edit to the Agent-side row.

    `force=True` (the legacy /api/admin/fragments CRUD routes' own call
    shape): this call IS the authoritative edit — always overwrite the
    mirrored row's fields, system or custom."""
    from backend.api.models import Agent
    from sqlalchemy import select

    try:
        if len(frag.name) > 64:
            logger.critical(
                f"template mirror: fragment name {frag.name!r} exceeds "
                f"Agent.slug's 64-char limit — skipping Agent mirror"
            )
            return
        existing = (await session.execute(
            select(Agent).where(Agent.slug == frag.name)
        )).scalar_one_or_none()
        fields = _tr_fragment_to_agent_fields(frag)

        if existing is None:
            session.add(Agent(slug=frag.name, **fields))
            await session.commit()
            return

        if existing.kind != "template":
            logger.critical(
                f"template mirror: slug {frag.name!r} is already used by "
                f"a non-template agent (id={existing.id}) — skipping "
                f"Agent mirror for fragment kind={frag.kind}"
            )
            return

        prior_kind = _tr_agent_fragment_kind(existing)
        if prior_kind is not None and prior_kind != frag.kind:
            logger.critical(
                f"template mirror: name {frag.name!r} is used by both a "
                f"notify and a condition fragment — skipping Agent "
                f"mirror for fragment kind={frag.kind} (existing mirrored "
                f"row is kind={prior_kind})"
            )
            return

        if force or existing.is_system or frag.is_system:
            for k, v in fields.items():
                setattr(existing, k, v)
            await session.commit()
        # else: already-mirrored custom row, backfill call — leave alone.
    except Exception as e:
        logger.error(
            f"template mirror: failed to upsert Agent row for fragment "
            f"{frag.kind}/{frag.name}: {e}"
        )


async def mirror_fragment_delete(session, frag_kind: str, frag_name: str) -> None:
    """Best-effort: delete the Agent(kind='template') row mirroring a
    just-deleted AgentTemplate row, if one exists AND its content shape
    still matches frag_kind (guards against deleting the wrong mirror in
    the rare notify/condition name-collision case, where only one of the
    two ever actually got mirrored — see mirror_fragment_upsert)."""
    from backend.api.models import Agent
    from sqlalchemy import select, delete as sa_delete

    try:
        existing = (await session.execute(
            select(Agent).where(Agent.slug == frag_name, Agent.kind == "template")
        )).scalar_one_or_none()
        if existing is None:
            return
        if _tr_agent_fragment_kind(existing) != frag_kind:
            return
        await session.execute(sa_delete(Agent).where(Agent.id == existing.id))
        await session.commit()
    except Exception as e:
        logger.error(
            f"template mirror: failed to delete Agent row for fragment "
            f"{frag_kind}/{frag_name}: {e}"
        )


async def migrate_fragments_to_template_agents() -> None:
    """One-time (idempotent) backfill: copy every existing agent_fragments
    row into a matching Agent(kind='template') row, preserving is_system
    and the slug/name. agent_fragments / AgentTemplate stay fully in
    place — this is additive only (Sprint D of the Agent/Bracket/Template
    unification phase; a later, not-yet-scheduled cleanup sprint removes
    the old table once the new path has proven out).

    Safe to call on every boot: `mirror_fragment_upsert(force=False)`
    inserts missing rows, refreshes already-migrated SYSTEM rows (same
    "code owns system bodies" convention as `seed_agent_templates()`),
    and leaves already-migrated CUSTOM rows untouched so a repeat boot
    never clobbers an operator's independent edit to the Agent-side row.
    """
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import AgentTemplate

    async with async_session() as s:
        rows = (await s.execute(select(AgentTemplate))).scalars().all()
        for frag in rows:
            await mirror_fragment_upsert(s, frag, force=False)
    logger.info(
        f"migrate_fragments_to_template_agents: processed {len(rows)} "
        f"agent_fragments row(s)"
    )


# ── Backwards-compatibility shims ─────────────────────────────────────
# Pre-v2.1 callers used SYSTEM_FRAGMENTS / FragmentRegistry / seed_fragments.
# Aliasing here keeps any external script that imported the old names
# working without a code change while internal consumers migrate to the
# new vocabulary. Remove these shims in v2.2 once external surfaces
# are confirmed migrated.
SYSTEM_FRAGMENTS = SYSTEM_AGENT_TEMPLATES   # noqa: N816 — legacy name
FragmentRegistry = TemplateRegistry         # noqa: N816 — legacy alias
seed_fragments   = seed_agent_templates     # noqa: N816 — legacy name
