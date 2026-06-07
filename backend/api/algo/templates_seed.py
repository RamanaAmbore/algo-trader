"""
Order-template seeder — system-defined exit-rule presets the operator
picks at OrderTicket submit time.

Pattern follows backend/api/algo/template_registry.py::seed_agent_templates:
- SYSTEM_TEMPLATES is the declarative list, slug-keyed.
- seed_templates() runs in app.on_startup; inserts missing rows,
  refreshes mutable metadata (name / description / applies_to /
  is_default) on existing rows, preserves the operator's edited
  numeric values (tp_pct / sl_pct / wing_*) so a tuned default isn't
  reset on every deploy.
- Custom (non-system) templates are never touched.

Industry analogue: NinjaTrader ships with built-in ATM Strategy
templates (e.g. "1-tick stop, 2-tick target"); operators clone or
tweak them. Our equivalent ships four canonical presets:

    default-bull       — long position, TP+30%, SL-20%
    default-bear       — short non-option, TP+30% (gain), SL-20% (loss)
    default-short-vol  — short option, TP+50% (premium decay), Wing +500
    none               — explicit "no auto-exit" pick
"""

from __future__ import annotations

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── System templates ───────────────────────────────────────────────────
#
# Each row is one template. `slug` is the stable identifier — operators
# can rename a template's `name` field but the slug never changes (the
# seeder upserts by slug). Numeric fields can be NULL to mean "this
# template doesn't enforce a TP / SL / wing".
#
# Values are in % units: tp_pct=30.0 → TP at fill × 1.30 for BUY;
# sl_pct=20.0 → SL at fill × 0.80 for BUY. The action layer flips signs
# for SELL parents.

SYSTEM_TEMPLATES: list[dict] = [
    {
        "slug":               "default-bull",
        "name":               "Default Bull",
        "description":        "Long entry with +30% take-profit and -20% stop-loss. "
                              "Translates to a Kite GTT-OCO on supported brokers; on "
                              "Groww the two trigger as separate singles with a "
                              "pair-watcher cancelling the sibling on either fill.",
        "applies_to":         "buy_any",
        "tp_pct":             30.0,
        "sl_pct":             20.0,
        "wing_premium_pct":   None,
        "wing_strike_offset": None,
        "is_default":         True,
        "is_system":          True,
    },
    {
        "slug":               "default-bear",
        "name":               "Default Bear",
        "description":        "Short non-option entry (e.g. equity / future short). "
                              "Mirror of Default Bull — TP fires when price drops 30% "
                              "from short entry, SL fires on a 20% rally against.",
        "applies_to":         "sell_option",   # actually sell-any-non-option; using closest scope
        "tp_pct":             30.0,
        "sl_pct":             20.0,
        "wing_premium_pct":   None,
        "wing_strike_offset": None,
        "is_default":         False,
        "is_system":          True,
    },
    {
        "slug":               "default-short-vol",
        "name":               "Default Short Vol",
        "description":        "Short option entry — collects premium, expects decay. "
                              "TP at +50% premium recovery (buy-back when premium drops "
                              "by half). Protective Wing leg auto-built at +500 strike "
                              "(CE) / -500 strike (PE) to cap tail risk; submitted as "
                              "an atomic basket on broker that supports it, paired "
                              "fan-out otherwise.",
        "applies_to":         "sell_option",
        "tp_pct":             50.0,
        "sl_pct":             None,
        "wing_premium_pct":   None,
        "wing_strike_offset": 500,
        "is_default":         True,
        "is_system":          True,
    },
    {
        "slug":               "none",
        "name":               "None",
        "description":        "Explicit opt-out — submit the entry order only. No TP, "
                              "no SL, no wing. Operator manages the position manually.",
        "applies_to":         "both",
        "tp_pct":             None,
        "sl_pct":             None,
        "wing_premium_pct":   None,
        "wing_strike_offset": None,
        "is_default":         False,
        "is_system":          True,
    },
]


# Mutable metadata refreshed on every boot (drift fix when we change a
# template's description / name / applies_to in code). Numeric fields
# (tp_pct / sl_pct / wing_*) and is_default are PRESERVED so the
# operator's tuning survives a deploy — same contract as Setting and
# Agent seeders.
_MUTABLE_FIELDS = ("name", "description", "applies_to")


async def seed_templates() -> None:
    """Insert system templates that don't yet exist; refresh mutable
    metadata on existing rows; preserve numeric tuning + is_default.
    Custom (non-system) rows are never touched."""
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import OrderTemplate

    async with async_session() as s:
        existing = await s.execute(
            select(OrderTemplate).where(OrderTemplate.is_system == True)  # noqa: E712
        )
        by_slug = {row.slug: row for row in existing.scalars().all()}

        inserted = updated = 0
        for spec in SYSTEM_TEMPLATES:
            row = by_slug.get(spec["slug"])
            if row is None:
                s.add(OrderTemplate(
                    slug=spec["slug"],
                    name=spec["name"],
                    description=spec["description"],
                    applies_to=spec["applies_to"],
                    tp_pct=spec["tp_pct"],
                    sl_pct=spec["sl_pct"],
                    wing_premium_pct=spec["wing_premium_pct"],
                    wing_strike_offset=spec["wing_strike_offset"],
                    is_default=spec["is_default"],
                    is_system=True,
                    is_active=True,
                ))
                inserted += 1
            else:
                # Refresh mutable metadata; preserve numeric tuning.
                for f in _MUTABLE_FIELDS:
                    setattr(row, f, spec[f])
                updated += 1
        await s.commit()
        logger.info(
            f"Order templates seeded — inserted={inserted} updated={updated}"
        )
