"""
NAV — firm-level daily NAV time series + ad-hoc recompute.

Read endpoints:
    GET /api/nav/                 — list NAV history (paginated by days)
    GET /api/nav/latest           — most recent NAV row + diff vs prior day

Write endpoints:
    POST /api/nav/compute         — operator-triggered recompute (admin / ops)
    POST /api/nav/{date}/note     — operator note on a historical row

All gated by the slice-7j caps `view_nav` (broad — admin / trader /
risk / ops / observer / demo) and `trigger_nav_compute` (admin / ops).
Demo can read but not trigger; trader / risk / observer are read-only.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, get, post
from litestar.exceptions import HTTPException
from sqlalchemy import select, desc

from backend.api.database import async_session
from backend.api.models import NavDaily, User
from backend.api.rbac import cap_guard
from backend.api.auth_guard import jwt_guard
from litestar import Request


class NavRow(msgspec.Struct):
    as_of_date:        str
    nav:               float
    cash_total:        float
    positions_mtm:     float
    holdings_mtm:      float
    accounts_snapshot: list[str]
    note:              Optional[str]


class NavListResponse(msgspec.Struct):
    rows: list[NavRow]
    days: int


class NavLatestResponse(msgspec.Struct):
    latest:    Optional[NavRow]
    prior:     Optional[NavRow]
    day_delta: Optional[float]      # latest.nav - prior.nav (₹)
    day_delta_pct: Optional[float]  # day_delta / prior.nav


class NavComputeResponse(msgspec.Struct):
    as_of_date:    str
    nav:           float
    cash_total:    float
    positions_mtm: float
    holdings_mtm:  float
    accounts:      list[str]
    errors:        list[str]


class InvestorSlice(msgspec.Struct):
    """Per-investor NAV slice. Returned by /api/nav/me.

    Calculation: units model (compute_slice in investor_units.py).
        units_held(user, t)   = Σ units_delta for events ≤ t
        total_units(t)        = Σ units_held across every LP
        nav_per_unit(t)       = firm_nav(t) / total_units(t)
        nav_share             = units_held × nav_per_unit
        cost_basis            = Σ amount (subscription+bootstrap)
                                − Σ amount (redemption)
        pnl                   = nav_share − cost_basis
        pnl_pct               = pnl / cost_basis (when basis > 0)
        day_delta_share       = nav_share(today) − nav_share(prior),
                                computed off the same event set so
                                subscriptions/redemptions between the
                                two snapshots show as capital
                                movements, not P&L.

    Auto-bootstrap (ensure_all_bootstrapped) backfills missing
    eligible LPs into the events register on first read, encoding
    their v1 share_pct + contribution. When share_pcts sum to 100,
    bootstrap reproduces v1 numbers exactly; otherwise units
    proportionally redistribute and slices sum to firm_nav by
    construction.

    `share_pct` + `contribution` are kept in the response shape for
    LP-facing display + back-compat with NavCard.svelte — the math
    no longer reads them.
    """
    username:              str
    share_pct:             float        # 0..100 (display only)
    contribution:          float        # ₹ initial / cumulative (display only)
    firm_nav:              float        # latest NavDaily.nav
    nav_share:             float        # units_held × nav_per_unit
    pnl:                   float        # nav_share - cost_basis
    pnl_pct:               Optional[float]
    day_delta_share:       Optional[float]
    day_delta_share_pct:   Optional[float]
    as_of_date:            Optional[str]


class InvestorHistoryPoint(msgspec.Struct):
    """One day of the per-investor NAV curve."""
    as_of_date: str
    firm_nav:   float
    nav_share:  float
    pnl:        float


class InvestorHistoryResponse(msgspec.Struct):
    rows:      list[InvestorHistoryPoint]
    days:      int
    share_pct: float
    contribution: float


def _to_row(r: NavDaily) -> NavRow:
    return NavRow(
        as_of_date=r.as_of_date.isoformat() if r.as_of_date else "",
        nav=float(r.nav or 0.0),
        cash_total=float(r.cash_total or 0.0),
        positions_mtm=float(r.positions_mtm or 0.0),
        holdings_mtm=float(r.holdings_mtm or 0.0),
        accounts_snapshot=list(r.accounts_snapshot or []),
        note=r.note,
    )


class NavController(Controller):
    path = "/api/nav"

    @get("/", guards=[cap_guard("view_nav")])
    async def list_nav(self, days: int = 90) -> NavListResponse:
        """NAV history — last `days` rows (default 90, cap 1825 ≈ 5y).
        Sorted ASC by date (oldest first) so the chart plots left-to-right."""
        days = max(1, min(int(days or 90), 1825))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        async with async_session() as s:
            rows = (await s.execute(
                select(NavDaily)
                  .where(NavDaily.as_of_date >= cutoff)
                  .order_by(NavDaily.as_of_date.asc())
            )).scalars().all()
        return NavListResponse(rows=[_to_row(r) for r in rows], days=days)

    @get("/latest", guards=[cap_guard("view_nav")])
    async def latest_nav(self) -> NavLatestResponse:
        """Most recent NAV row + delta vs the prior day. Used by the
        page-header NAV chip on /nav and the dashboard summary card."""
        async with async_session() as s:
            rows = (await s.execute(
                select(NavDaily)
                  .order_by(desc(NavDaily.as_of_date))
                  .limit(2)
            )).scalars().all()
        latest = _to_row(rows[0]) if len(rows) >= 1 else None
        prior  = _to_row(rows[1]) if len(rows) >= 2 else None
        delta: Optional[float] = None
        delta_pct: Optional[float] = None
        if latest is not None and prior is not None:
            delta = latest.nav - prior.nav
            delta_pct = (delta / prior.nav) if prior.nav else None
        return NavLatestResponse(
            latest=latest, prior=prior,
            day_delta=delta, day_delta_pct=delta_pct,
        )

    @get("/me", guards=[jwt_guard])
    async def my_slice(self, request: Request) -> InvestorSlice:
        """Per-investor NAV slice for the authenticated user. Uses
        the units-based fund-accounting model: each LP's slice =
        units_held × nav_per_unit, cost basis = Σ subscriptions −
        Σ redemptions. Auto-bootstraps a synthetic event from
        User.share_pct + User.contribution on first compute so the
        first request after deploy returns the same numbers as v1.

        Demo / anonymous → 401 via jwt_guard."""
        from backend.api.algo.investor_units import (
            compute_slice, ensure_all_bootstrapped,
            fetch_all_events, slice_value, cost_basis as _cb,
        )
        payload = getattr(request.state, "token_payload", {}) or {}
        username = str(payload.get("sub") or "")
        if not username:
            raise HTTPException(status_code=401, detail="No active session")
        async with async_session() as s:
            user = (await s.execute(
                select(User).where(User.username == username)
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            rows = (await s.execute(
                select(NavDaily)
                  .order_by(desc(NavDaily.as_of_date))
                  .limit(2)
            )).scalars().all()
            firm_nav = float(rows[0].nav) if rows else 0.0
            # Fetch events ONCE; compute_slice reuses, day-delta math
            # reuses. Pre-fix this fetched twice. (Slice M4.)
            await ensure_all_bootstrapped(s)
            all_events = await fetch_all_events(s)
            slice_now = await compute_slice(
                s, user, firm_nav, all_events=all_events
            )
            # Day delta on the investor's slice — recompute the
            # prior day's slice through the same units math and
            # subtract. Uses the same event set so a subscription /
            # redemption between the two snapshots is reflected as
            # a capital movement (not P&L) in the difference.
            day_delta_share: Optional[float] = None
            day_delta_share_pct: Optional[float] = None
            if len(rows) >= 2:
                user_events = [e for e in all_events if e.user_id == user.id]
                prior_val, _ = slice_value(
                    user_events, all_events, float(rows[1].nav),
                    as_of=rows[1].as_of_date,
                )
                day_delta_share = slice_now["nav_share"] - prior_val
                day_delta_share_pct = (
                    (day_delta_share / prior_val) if prior_val else None
                )

        as_of = rows[0].as_of_date.isoformat() if rows else None
        return InvestorSlice(
            username=username,
            share_pct=float(user.share_pct or 0.0),
            contribution=float(user.contribution or 0.0),
            firm_nav=firm_nav,
            nav_share=slice_now["nav_share"],
            pnl=slice_now["pnl"],
            pnl_pct=slice_now["pnl_pct"],
            day_delta_share=(round(day_delta_share, 2)
                             if day_delta_share is not None else None),
            day_delta_share_pct=day_delta_share_pct,
            as_of_date=as_of,
        )

    @get("/me/history", guards=[jwt_guard])
    async def my_history(self, request: Request,
                         days: int = 90) -> InvestorHistoryResponse:
        """Per-investor NAV curve. Walks the firm NavDaily curve and
        computes the LP's slice + cost basis + P&L at each date via
        the units model. Capital movements (subscriptions /
        redemptions) inside the window show up as step changes in
        the slice / cost basis."""
        from backend.api.algo.investor_units import (
            compute_slice_history, ensure_all_bootstrapped, fetch_all_events,
        )
        days = max(1, min(int(days or 90), 1825))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).date()
        payload = getattr(request.state, "token_payload", {}) or {}
        username = str(payload.get("sub") or "")
        if not username:
            raise HTTPException(status_code=401, detail="No active session")
        async with async_session() as s:
            user = (await s.execute(
                select(User).where(User.username == username)
            )).scalar_one_or_none()
            if user is None:
                raise HTTPException(status_code=404, detail="User not found")
            await ensure_all_bootstrapped(s)
            all_events = await fetch_all_events(s)
            user_events = [e for e in all_events if e.user_id == user.id]
            rows = (await s.execute(
                select(NavDaily)
                  .where(NavDaily.as_of_date >= cutoff)
                  .order_by(NavDaily.as_of_date.asc())
            )).scalars().all()
        share_pct = float(user.share_pct or 0.0)
        contribution = float(user.contribution or 0.0)
        history = compute_slice_history(user_events, all_events, rows)
        out: list[InvestorHistoryPoint] = [
            InvestorHistoryPoint(
                as_of_date=h["as_of_date"],
                firm_nav=h["firm_nav"],
                nav_share=h["nav_share"],
                pnl=h["pnl"],
            )
            for h in history
        ]
        return InvestorHistoryResponse(
            rows=out, days=days,
            share_pct=share_pct, contribution=contribution,
        )

    @post("/compute", guards=[cap_guard("trigger_nav_compute")])
    async def compute_now(self) -> NavComputeResponse:
        """Operator-triggered NAV recompute. Re-aggregates funds +
        positions + holdings for today's IST date and upserts into
        `nav_daily`. Same code path the daily 16:00 IST background
        task uses; idempotent so repeated triggers within the day
        just overwrite the row.

        Use cases:
        - Mid-day check after a position close
        - Backfill after a broker outage
        - Operator wants to see current NAV before EOD
        """
        from backend.api.algo.nav import write_nav_snapshot
        from backend.shared.helpers.date_time_utils import timestamp_indian
        snap = await write_nav_snapshot()
        today = timestamp_indian().date().isoformat()
        return NavComputeResponse(
            as_of_date=today,
            nav=float(snap["nav"]),
            cash_total=float(snap["cash_total"]),
            positions_mtm=float(snap["positions_mtm"]),
            holdings_mtm=float(snap["holdings_mtm"]),
            accounts=snap["accounts"],
            errors=snap["errors"],
        )
