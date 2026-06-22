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

    Calculation (v1 — static share_pct, no units accounting):
        nav_share   = share_pct × firm_nav
        pnl         = nav_share - contribution
        pnl_pct     = pnl / contribution    (when contribution > 0)
        day_delta_share = day_delta × share_pct

    Treats `User.share_pct` as the investor's % of total firm NAV.
    Works when share_pct sums to 100 % across LPs; otherwise the
    leftover is firm equity (operator's own stake).

    Subscription / redemption events are NOT modelled — share_pct
    is static. Mid-period contribution changes need pro-rata
    accounting (units model); that lands in a future slice when
    the boutique opens up to a second LP.
    """
    username:              str
    share_pct:             float        # 0..100
    contribution:          float        # ₹ initial / cumulative
    firm_nav:              float        # latest NavDaily.nav
    nav_share:             float        # share_pct/100 × firm_nav
    pnl:                   float        # nav_share - contribution
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
        """Per-investor NAV slice for the authenticated user. Reads
        User.share_pct + User.contribution against the most recent
        NavDaily row. Demo / anonymous → 401 via jwt_guard."""
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
            # Latest NAV + prior for day-delta.
            rows = (await s.execute(
                select(NavDaily)
                  .order_by(desc(NavDaily.as_of_date))
                  .limit(2)
            )).scalars().all()
        share_pct = float(user.share_pct or 0.0)
        contribution = float(user.contribution or 0.0)
        firm_nav = float(rows[0].nav) if rows else 0.0
        nav_share = (share_pct / 100.0) * firm_nav
        pnl = nav_share - contribution
        pnl_pct: Optional[float] = (pnl / contribution) if contribution > 0 else None
        # Day delta on the investor's share.
        day_delta_share: Optional[float] = None
        day_delta_share_pct: Optional[float] = None
        if len(rows) >= 2:
            firm_delta = float(rows[0].nav) - float(rows[1].nav)
            day_delta_share = firm_delta * (share_pct / 100.0)
            prior_share = float(rows[1].nav) * (share_pct / 100.0)
            day_delta_share_pct = (day_delta_share / prior_share) if prior_share else None
        as_of = rows[0].as_of_date.isoformat() if rows else None
        return InvestorSlice(
            username=username,
            share_pct=share_pct,
            contribution=contribution,
            firm_nav=firm_nav,
            nav_share=round(nav_share, 2),
            pnl=round(pnl, 2),
            pnl_pct=pnl_pct,
            day_delta_share=(round(day_delta_share, 2)
                             if day_delta_share is not None else None),
            day_delta_share_pct=day_delta_share_pct,
            as_of_date=as_of,
        )

    @get("/me/history", guards=[jwt_guard])
    async def my_history(self, request: Request,
                         days: int = 90) -> InvestorHistoryResponse:
        """Per-investor NAV curve. Same firm NAV history scaled by
        share_pct so the LP sees their own slice over time."""
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
            rows = (await s.execute(
                select(NavDaily)
                  .where(NavDaily.as_of_date >= cutoff)
                  .order_by(NavDaily.as_of_date.asc())
            )).scalars().all()
        share_pct = float(user.share_pct or 0.0)
        contribution = float(user.contribution or 0.0)
        ratio = share_pct / 100.0
        out: list[InvestorHistoryPoint] = []
        for r in rows:
            firm_nav = float(r.nav or 0.0)
            nav_share = firm_nav * ratio
            out.append(InvestorHistoryPoint(
                as_of_date=r.as_of_date.isoformat() if r.as_of_date else "",
                firm_nav=firm_nav,
                nav_share=round(nav_share, 2),
                pnl=round(nav_share - contribution, 2),
            ))
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
