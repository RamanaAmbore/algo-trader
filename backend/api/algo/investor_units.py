"""
Units-based NAV math for LPs.

Replaces the v1 static-share model (`slice = share_pct × firm_nav`)
with proper fund-accounting units:

    units_held(user, t)   = Σ units_delta for user's events <= t
    total_units(t)        = Σ units_held across every LP <= t
    nav_per_unit(t)       = firm_nav(t) / total_units(t)
    slice(user, t)        = units_held × nav_per_unit
    cost_basis(user, t)   = Σ amount (subscription+bootstrap) − Σ amount (redemption)
    pnl(user, t)          = slice − cost_basis

This is the standard fund-accounting model — every Cat-III AIF,
mutual fund, hedge fund admin (Carta, CAMSonline, SS&C/GP-Link)
runs this same calculation. The difference vs v1 share_pct: an LP
who joined mid-period and bought in at a higher per-unit value
gets the correct cost basis, and a partial-redemption that pays
out at the current NAV/unit doesn't break the remaining slice.

Auto-bootstrap: any eligible LP (active + share_pct > 0) that has
no events gets a synthetic `bootstrap` event on first compute. The
bootstrap encodes the v1 state so the units math reproduces v1
numbers exactly when share_pct's sum to 100 across all eligible
LPs. Operator can edit / replace bootstrap events from the
/admin Portal modal Events tab.
"""

from __future__ import annotations

from datetime import date as _date, datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.models import InvestorEvent, User
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

async def ensure_user_bootstrap(s: AsyncSession, user: User) -> bool:
    """If `user` has no events AND is an eligible LP (share_pct > 0),
    insert a synthetic bootstrap event encoding their v1 state.
    Returns True when a row was inserted, False otherwise (caller
    doesn't need this; the boolean is for telemetry).

    Bootstrap math:
        units_delta  = share_pct       (a unit-less share unit; preserves
                                         v1 math when share_pcts sum to 100)
        amount       = contribution    (preserves the LP's cost basis)
        nav_per_unit = contribution/share_pct  (back-derived; 1.0 fallback
                                         when contribution=0 — operator
                                         residual case)

    The math constraint `amount = units_delta × nav_per_unit` holds
    by construction. The event_type='bootstrap' tag lets the operator
    visually distinguish synthetic seeds from real subscriptions in
    the /admin Portal Events tab.
    """
    if not user or not user.is_active or float(user.share_pct or 0.0) <= 0.0:
        return False

    existing = (await s.execute(
        select(InvestorEvent.id).where(InvestorEvent.user_id == user.id).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return False

    share_pct = float(user.share_pct or 0.0)
    contribution = float(user.contribution or 0.0)
    # nav_per_unit guard: operator-residual rows with contribution=0
    # divide by zero; default to 1.0 (an arbitrary per-unit value).
    npu = (contribution / share_pct) if (contribution > 0 and share_pct > 0) else 1.0
    # Event date: contribution_date when known, else creation date,
    # else today. Prefer the earliest reasonable timestamp so the
    # bootstrap doesn't claim to predate the LP's actual entry.
    ed: _date
    if user.contribution_date:
        ed = user.contribution_date
    elif user.created_at:
        # User.created_at is a tz-aware datetime; take the date.
        ed = user.created_at.date()
    else:
        ed = datetime.now(timezone.utc).date()
    row = InvestorEvent(
        user_id=user.id,
        event_type="bootstrap",
        event_date=ed,
        amount=contribution,
        nav_per_unit=npu,
        units_delta=share_pct,
        note="auto-bootstrap from v1 share_pct",
    )
    s.add(row)
    try:
        await s.commit()
    except IntegrityError:
        # Concurrent caller won the race; partial unique index
        # (uq_investor_events_user_bootstrap) rejected the second
        # insert. Rollback and treat as no-op — the other caller's
        # bootstrap is the canonical one.
        await s.rollback()
        return False
    logger.info(
        f"ensure_user_bootstrap: u={user.id} share_pct={share_pct} "
        f"contribution={contribution} units_delta={share_pct} "
        f"nav_per_unit={npu:.6f} event_date={ed}"
    )
    return True


async def ensure_all_bootstrapped(s: AsyncSession) -> int:
    """Auto-bootstrap every eligible LP that's missing events.
    Idempotent. Returns count of rows actually inserted.

    Called at the start of every units-based compute to guarantee
    the units register is complete before we read it. Without this,
    a half-bootstrapped fund would compute nav_per_unit against a
    partial total_units, inflating bootstrapped LPs' slices."""
    eligible = (await s.execute(
        select(User).where(
            User.is_active.is_(True),
            User.share_pct > 0,
        )
    )).scalars().all()
    inserted = 0
    for user in eligible:
        if await ensure_user_bootstrap(s, user):
            inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Math primitives
# ---------------------------------------------------------------------------

def units_held(events: Iterable[InvestorEvent],
               as_of: Optional[_date] = None) -> float:
    """Sum of units_delta for the given events up to and including
    `as_of`. When `as_of` is None, sums every event."""
    total = 0.0
    for e in events:
        if as_of is not None and e.event_date > as_of:
            continue
        total += float(e.units_delta or 0.0)
    return total


def cost_basis(events: Iterable[InvestorEvent],
               as_of: Optional[_date] = None) -> float:
    """Cumulative cost basis up to `as_of`. Subscription + bootstrap
    add; redemption subtracts. The bootstrap event's amount = the
    LP's recorded contribution, so an LP who never adds capital
    keeps their original cheque as cost basis indefinitely."""
    basis = 0.0
    for e in events:
        if as_of is not None and e.event_date > as_of:
            continue
        amt = float(e.amount or 0.0)
        if e.event_type == "redemption":
            basis -= amt
        else:  # 'subscription' or 'bootstrap'
            basis += amt
    return basis


def slice_value(user_events: Iterable[InvestorEvent],
                all_events: Iterable[InvestorEvent],
                firm_nav: float,
                as_of: Optional[_date] = None) -> tuple[float, float]:
    """Compute (slice_value, nav_per_unit) for the user. Returns
    (0.0, 0.0) when total_units is 0 (empty fund). The caller is
    responsible for ensuring `all_events` covers every LP — that's
    what `ensure_all_bootstrapped` is for."""
    u_held = units_held(user_events, as_of=as_of)
    t_units = units_held(all_events, as_of=as_of)
    if t_units <= 0:
        return 0.0, 0.0
    npu = firm_nav / t_units
    return u_held * npu, npu


# ---------------------------------------------------------------------------
# High-level helpers used by route + statement code
# ---------------------------------------------------------------------------

async def fetch_all_events(s: AsyncSession) -> list[InvestorEvent]:
    """Single query returning every event across every LP. Cheap —
    we run on the order of dozens of events per fund per year."""
    return list((await s.execute(
        select(InvestorEvent).order_by(InvestorEvent.event_date.asc())
    )).scalars().all())


async def compute_slice(s: AsyncSession, user: User,
                        firm_nav: float,
                        as_of: Optional[_date] = None) -> dict:
    """Compute the LP's slice + cost basis + P&L at `as_of`.
    Auto-bootstraps before reading. Returns a dict with keys:
        nav_share, cost_basis, pnl, pnl_pct (None when basis<=0),
        units, nav_per_unit
    """
    await ensure_all_bootstrapped(s)
    all_events = await fetch_all_events(s)
    user_events = [e for e in all_events if e.user_id == user.id]
    val, npu = slice_value(user_events, all_events, firm_nav, as_of=as_of)
    basis = cost_basis(user_events, as_of=as_of)
    pnl = val - basis
    pnl_pct = (pnl / basis) if basis > 0 else None
    u_held = units_held(user_events, as_of=as_of)
    return {
        "nav_share":    round(val, 2),
        "cost_basis":   round(basis, 2),
        "pnl":          round(pnl, 2),
        "pnl_pct":      pnl_pct,
        "units":        round(u_held, 6),
        "nav_per_unit": round(npu, 6),
    }


def compute_slice_history(user_events: list[InvestorEvent],
                          all_events: list[InvestorEvent],
                          firm_curve: list) -> list[dict]:
    """Walk a NavDaily curve and emit per-date slice values for the
    given user. `firm_curve` is a list of NavDaily-ish objects with
    `.as_of_date` (date) and `.nav` (float).

    Used by /api/nav/me/history and the statement PDF's daily table.
    No DB calls — caller pre-fetches both event lists and the curve.
    """
    out: list[dict] = []
    for nd in firm_curve:
        as_of = nd.as_of_date
        firm_nav = float(nd.nav or 0.0)
        val, npu = slice_value(user_events, all_events, firm_nav, as_of=as_of)
        basis = cost_basis(user_events, as_of=as_of)
        out.append({
            "as_of_date":    as_of.isoformat() if as_of else "",
            "firm_nav":      firm_nav,
            "nav_share":     round(val, 2),
            "cost_basis":    round(basis, 2),
            "pnl":           round(val - basis, 2),
            "nav_per_unit":  round(npu, 6),
            "units":         round(units_held(user_events, as_of=as_of), 6),
        })
    return out
