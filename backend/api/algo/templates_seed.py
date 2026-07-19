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

    default-bull         — BUY EQ / FUT, TP+30%, SL-20%
    default-long-option  — BUY option (CE/PE), TP+80% MARKET, no SL
    default-bear         — SELL EQ / FUT, TP+30%, SL-20%
    default-short-vol    — SELL option (CE/PE), TP+50% + protective wing
    none                 — explicit "no auto-exit" pick

The four side-defaults form a 2×2 matrix over (BUY/SELL) × (EQ-FUT/OPTION)
so the Default pill in the order modal's Template row always lands on a
template that matches the leg's scope without operator interaction.
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
        "description":        "BUY EQ / FUT (non-option) entry with +30% take-profit "
                              "and -20% stop-loss. Translates to a Kite GTT-OCO on "
                              "supported brokers; on Groww the two trigger as separate "
                              "singles with a pair-watcher cancelling the sibling on "
                              "either fill. Side-default for the buy_any scope (BUY of "
                              "equity / futures).",
        "applies_to":         "buy_any",
        "tp_pct":             30.0,
        "sl_pct":             20.0,
        "wing_premium_pct":   None,
        "wing_strike_offset": None,
        "tp_order_type":      "LIMIT",
        "is_default":         True,
        "is_system":          True,
    },
    {
        "slug":               "default-long-option",
        "name":               "Default Long Option",
        "description":        "BUY option (CE/PE) entry — TP at +80% premium gain "
                              "(e.g. paid ₹100, exits when premium hits ₹180). TP "
                              "fires as a MARKET order so it lands in fast-moving "
                              "expiry markets even when the book thins out. No SL "
                              "by default — operators relying on max-loss of "
                              "premium-paid don't need one. Side-default for the "
                              "buy_option scope.",
        "applies_to":         "buy_option",
        "tp_pct":             80.0,
        "sl_pct":             None,
        "wing_premium_pct":   None,
        "wing_strike_offset": None,
        "tp_order_type":      "MARKET",
        "is_default":         True,
        "is_system":          True,
    },
    {
        "slug":               "default-bear",
        "name":               "Default Bear",
        "description":        "Short non-option entry (e.g. equity / future short). "
                              "Mirror of Default Bull — TP fires when price drops 30% "
                              "from short entry, SL fires on a 20% rally against.",
        "applies_to":         "sell_any",
        "tp_pct":             30.0,
        "sl_pct":             20.0,
        "wing_premium_pct":   None,
        "wing_strike_offset": None,
        "tp_order_type":      "LIMIT",
        "is_default":         True,
        "is_system":          True,
    },
    {
        "slug":               "default-short-vol",
        "name":               "Default Short Vol",
        "description":        "Short option entry — collects premium, expects decay. "
                              "TP at +50% premium recovery (buy-back when premium drops "
                              "by half). Protective Wing auto-picked from the option "
                              "chain — scanner finds the strike whose premium is ≈10% "
                              "of the parent's premium, respecting OI + spread% filters "
                              "(templates.wing_min_oi / templates.wing_max_spread_pct). "
                              "Operators upgrading from the strike-offset shape keep "
                              "their tuned values; fresh installs use the premium-% "
                              "scan.",
        "applies_to":         "sell_option",
        "tp_pct":             50.0,
        "sl_pct":             None,
        "wing_premium_pct":   10.0,
        "wing_strike_offset": None,
        "tp_order_type":      "LIMIT",
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
        "tp_order_type":      "LIMIT",
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


async def _promote_default_if_unclaimed(session, by_slug: dict, slug: str, applies_to: str) -> None:
    """Promote `slug` to is_default when its applies_to scope is unclaimed.

    Called once per side-default after the main upsert loop. Only fires when
    the row exists but is_default=False AND no other system template in the
    same applies_to scope already claims is_default=True. This allows an
    operator who manually promoted a custom template to keep it without the
    seeder stomping it on every deploy.
    """
    from sqlalchemy import select
    from backend.api.models import OrderTemplate

    row = by_slug.get(slug)
    if row is None or row.is_default:
        return
    others = await session.execute(
        select(OrderTemplate).where(
            OrderTemplate.applies_to == applies_to,
            OrderTemplate.is_default == True,  # noqa: E712
            OrderTemplate.slug != slug,
        )
    )
    if not others.scalars().first():
        row.is_default = True
        logger.info(
            f"Order template {slug!r} promoted to is_default — "
            f"now the side-default for {applies_to!r}."
        )


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

        # When a fresh system row claims is_default=True for an
        # applies_to scope (e.g. the new `default-long-option` for
        # buy_any), demote any existing is_default=True system row in
        # that scope so the seeder enforces "one default per scope".
        # Skips on already-seeded rows so the operator's `is_default`
        # tuning on existing templates is preserved.
        for spec in SYSTEM_TEMPLATES:
            if not spec.get("is_default"):
                continue
            if spec["slug"] in by_slug:
                continue   # row already exists, leave operator's pick alone
            scope = spec["applies_to"]
            for existing in by_slug.values():
                if existing.applies_to == scope and existing.is_default:
                    existing.is_default = False
                    logger.info(
                        f"Order template {existing.slug!r} demoted from "
                        f"is_default — superseded by {spec['slug']!r} in "
                        f"scope {scope!r}"
                    )

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
                    tp_order_type=spec.get("tp_order_type", "LIMIT"),
                    tp_scales_json=spec.get("tp_scales_json"),
                    sl_trail_pct=spec.get("sl_trail_pct"),
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

        # One-time rebalance for the 4-default matrix migration:
        # is_default is NOT in _MUTABLE_FIELDS by design (operators may
        # have intentionally demoted a default); this one-time path
        # only PROMOTES when the scope is unclaimed.
        await _promote_default_if_unclaimed(s, by_slug, "default-bull",      "buy_any")
        await _promote_default_if_unclaimed(s, by_slug, "default-bear",      "sell_any")
        await _promote_default_if_unclaimed(s, by_slug, "default-short-vol", "sell_option")

        await s.commit()
        logger.info(
            f"Order templates seeded — inserted={inserted} updated={updated}"
        )
