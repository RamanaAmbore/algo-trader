"""
Bracket registry — Sprint E (Agent/Bracket/Template unification phase).

A Bracket (the TP/SL/Wing exit-rule preset an operator picks at
OrderTicket submit time — "default-bull" etc.) is now ALSO an
`Agent(kind='bracket')` row, synchronously mirrored from the owning
`OrderTemplate` row by this module. `order_templates` stays the real,
unchanged write path (CRUD lives in `backend.api.routes.templates.
OrderTemplateController`; seeding in `backend.api.algo.templates_seed`)
and the real FK ANCHOR for `AlgoOrder.template_id` — kept in place
deliberately, not dropped, per Sprint E's own brief.

This module's job is narrower than Sprint D's `template_registry.py`:
it never retires anything, and its upsert is NOT best-effort. Every
mirror write here is meant to run in the SAME transaction as its
owning `OrderTemplate` write (caller controls commit) — if the mirror
raises, the whole write rolls back. A best-effort mirror (Sprint D's
own `mirror_fragment_upsert` pattern, which only logs on failure) would
let an `OrderTemplate` edit succeed while its `Agent` mirror silently
went stale — and `template_attach.load_template_for_slug_or_id` (the
LIVE order-fill resolver) reads ONLY the `Agent` mirror, never
`OrderTemplate` directly, so a stale mirror would arm the WRONG
bracket's TP/SL/wing on a real fill. That is the one invariant this
whole module exists to protect.

Historical id-mapping — direction matters, this is the part the
council-risk review blocked on: `AlgoOrder.template_id` keeps storing
the real, canonical `OrderTemplate.id` — a genuine Postgres FK,
unchanged. `Agent.legacy_template_id` is the back-reference, read in
the OPPOSITE direction: given a `template_id` value read off an
`AlgoOrder` row, find the `Agent` row whose `legacy_template_id`
equals it. Getting this backwards (storing `Agent.id` in
`AlgoOrder.template_id`, or resolving by `Agent.id` instead of
`Agent.legacy_template_id`) risks a numeric `Agent.id`/
`OrderTemplate.id` collision silently arming the WRONG bracket on a
live fill — a P&L-correctness defect, not a cosmetic one.

Slug derivation: a system `OrderTemplate` always has a real `slug`
(`templates_seed.SYSTEM_TEMPLATES`) — used verbatim, unchanged. A
custom (operator-created) `OrderTemplate` leaves `slug` NULL (see
`backend.api.models.OrderTemplate`'s own comment) — this module derives
a DETERMINISTIC, collision-free slug of `f"bracket-{row.id}"` for
those, rather than slugifying `name` (free-text, up to 128 chars, no
uniqueness guarantee, no obviously-safe 64-char truncation rule). Since
`OrderTemplate.id` is already globally unique, `f"bracket-{row.id}"`
can never collide with another DERIVED bracket slug; it is still
checked against the general `Agent.slug` namespace (the pre-check
below) in case an unrelated row already happens to own that exact
string.
"""

from __future__ import annotations

from typing import Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


class BracketSlugCollisionError(Exception):
    """Raised when a bracket's mirror slug is already owned by a
    different, non-matching row. Callers decide what fail-closed means
    for their own context: the live CRUD write path
    (`OrderTemplateController`) lets this abort the whole request (same
    transaction as the primary `OrderTemplate` write — both roll back);
    the boot-time bulk migration (`migrate_order_templates_to_bracket_
    agents`) catches it per-row, logs CRITICAL, and continues to the
    next row — matching Sprint D2's `migrate_and_retire_agent_
    fragments()` fail-closed-per-row style."""


def bracket_slug_for(row) -> str:
    """Deterministic `Agent.slug` for one `OrderTemplate` row. System
    rows already carry a real slug — used verbatim. Custom rows
    (`slug IS NULL`) get `f"bracket-{row.id}"` — see module docstring."""
    if row.slug:
        return row.slug
    return f"bracket-{row.id}"


def order_template_to_bracket_config(row) -> dict:
    """Map one `OrderTemplate` row's exit-rule fields onto the
    `Agent.bracket_config` JSON payload. Numeric columns stored as
    `Numeric` on `OrderTemplate` are coerced to float — same cast
    `template_attach._ta_template_row_to_dict` / `routes.templates.
    _to_out` already apply — so the JSON payload never round-trips a
    `decimal.Decimal` (not JSON-serialisable without a custom encoder)."""
    return {
        "tp_pct": float(row.tp_pct) if row.tp_pct is not None else None,
        "sl_pct": float(row.sl_pct) if row.sl_pct is not None else None,
        "wing_premium_pct": (
            float(row.wing_premium_pct) if row.wing_premium_pct is not None else None
        ),
        "wing_strike_offset": (
            int(row.wing_strike_offset) if row.wing_strike_offset is not None else None
        ),
        "wing_max_spread_pct": (
            float(row.wing_max_spread_pct)
            if getattr(row, "wing_max_spread_pct", None) is not None else None
        ),
        "tp_order_type": (row.tp_order_type or "LIMIT"),
        "tp_scales_json": row.tp_scales_json,
        "sl_trail_pct": (
            float(row.sl_trail_pct) if row.sl_trail_pct is not None else None
        ),
        "applies_to": row.applies_to,
        "is_default": bool(row.is_default),
    }


async def mirror_bracket_upsert(session, ot_row):
    """Create/update the `Agent(kind='bracket')` row mirroring one
    `OrderTemplate` row, keyed by `legacy_template_id == ot_row.id`.

    Does NOT commit — the caller controls the transaction boundary so
    the `OrderTemplate` write and this mirror write land atomically
    (the council-risk review's "if the mirror fails, the write fails"
    requirement). Raises `BracketSlugCollisionError` when the derived
    slug is already owned by a different, non-matching row — never
    silently re-slugs or skips; the caller decides what to do with the
    exception. Returns the (added-but-not-yet-flushed, for a new row)
    `Agent` ORM instance.
    """
    from sqlalchemy import select
    from backend.api.models import Agent

    slug = bracket_slug_for(ot_row)
    existing = (await session.execute(
        select(Agent).where(Agent.slug == slug)
    )).scalar_one_or_none()

    if existing is not None and existing.legacy_template_id != ot_row.id:
        raise BracketSlugCollisionError(
            f"bracket mirror: slug {slug!r} (derived from OrderTemplate "
            f"id={ot_row.id}) is already owned by Agent id={existing.id} "
            f"kind={existing.kind!r} (legacy_template_id="
            f"{existing.legacy_template_id!r}) — refusing to overwrite"
        )

    row = existing
    if row is None:
        # Defensive fallback — slug is deterministic and immutable for
        # every write path this module knows about, so this branch is
        # not expected to ever actually differ from `existing is None`
        # meaning "no mirror yet". Kept for robustness against a future
        # caller that renames a mirror's slug out from under it.
        row = (await session.execute(
            select(Agent).where(Agent.legacy_template_id == ot_row.id)
        )).scalar_one_or_none()

    bracket_config = order_template_to_bracket_config(ot_row)
    status = "active" if ot_row.is_active else "inactive"
    group_name = getattr(ot_row, "group_name", None)
    tags = list(getattr(ot_row, "tags", None) or [])

    if row is None:
        row = Agent(
            slug=slug, name=ot_row.name,
            description=ot_row.description or "",
            conditions={}, events=[], actions=[],
            kind="bracket", is_system=bool(ot_row.is_system),
            status=status,
            group_name=group_name, tags=tags,
            legacy_template_id=ot_row.id,
            bracket_config=bracket_config,
        )
        session.add(row)
    else:
        row.slug = slug
        row.name = ot_row.name
        row.description = ot_row.description or ""
        row.is_system = bool(ot_row.is_system)
        row.status = status
        row.group_name = group_name
        row.tags = tags
        row.legacy_template_id = ot_row.id
        row.bracket_config = bracket_config
    return row


async def mirror_bracket_delete(session, ot_row_id: int) -> None:
    """Delete the `Agent(kind='bracket')` row mirroring `OrderTemplate`
    id=ot_row_id, if any. Caller controls commit (same-transaction
    contract as `mirror_bracket_upsert`)."""
    from sqlalchemy import select, delete as sa_delete
    from backend.api.models import Agent

    existing = (await session.execute(
        select(Agent).where(Agent.kind == "bracket",
                             Agent.legacy_template_id == ot_row_id)
    )).scalar_one_or_none()
    if existing is not None:
        await session.execute(sa_delete(Agent).where(Agent.id == existing.id))


async def demote_existing_default_brackets(
    session, applies_to: str, exclude_template_id: Optional[int],
) -> None:
    """Agent-side half of `routes.templates._demote_existing_default` —
    flips `bracket_config['is_default']` off for every OTHER bracket
    Agent row in the same `applies_to` scope. Always called in
    lockstep with the OrderTemplate-side flip, in the SAME request/
    transaction, so the two tables never disagree on which row is the
    scope's default — the auto-selector in OrderTicket reads the
    Agent-mirrored value (via `loadOrderTemplates()`), so a drift here
    would silently un-fix the "one default per scope" invariant on the
    UI side even though `order_templates` itself stayed consistent."""
    from sqlalchemy import select
    from backend.api.models import Agent

    rows = (await session.execute(
        select(Agent).where(Agent.kind == "bracket")
    )).scalars().all()
    for row in rows:
        cfg = row.bracket_config or {}
        if cfg.get("applies_to") != applies_to or not cfg.get("is_default"):
            continue
        if exclude_template_id is not None and row.legacy_template_id == exclude_template_id:
            continue
        cfg = dict(cfg)
        cfg["is_default"] = False
        row.bracket_config = cfg


async def migrate_order_templates_to_bracket_agents() -> None:
    """Boot-time, idempotent sync: mirror EVERY existing `OrderTemplate`
    row into its matching `Agent(kind='bracket')` row. Runs on EVERY
    boot (not one-time) — see `backend/api/database.py:init_db()`,
    called right after `seed_templates()` so a fresh deploy's
    just-reseeded system rows are mirrored within the same boot that
    created/refreshed them. Also the steady-state backstop that keeps
    a hand-edited-in-the-DB or pre-Sprint-E row in sync.

    Per-row fail-closed on a slug collision: logs CRITICAL and skips
    that one row, continues to the next — matches Sprint D2's
    `migrate_and_retire_agent_fragments()` bulk-migration style. The
    live CRUD write path (`OrderTemplateController`) uses
    `mirror_bracket_upsert` directly instead, inside its OWN
    transaction, where a collision aborts the WHOLE write (see that
    module) — this function is the bulk/boot-time counterpart only.
    """
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import OrderTemplate

    async with async_session() as s:
        rows = (await s.execute(select(OrderTemplate))).scalars().all()
        migrated = skipped = 0
        for ot_row in rows:
            try:
                await mirror_bracket_upsert(s, ot_row)
                migrated += 1
            except BracketSlugCollisionError as e:
                logger.critical(f"bracket migration: {e}")
                skipped += 1
        await s.commit()
    logger.info(
        f"Bracket migration — mirrored {migrated} OrderTemplate row(s) "
        f"into Agent(kind='bracket'); skipped={skipped}"
    )
