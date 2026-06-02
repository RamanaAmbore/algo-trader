"""
P&L summary by agent endpoint.

GET /api/admin/pnl/by-agent — aggregate algo_orders by agent, compute
                              fill-based PnL proxy, win rate, and slippage.

Admin-only. No demo access.

Note: PnL is a rough chase-slippage proxy, not realised P&L from position
pairing. It is computed as:
    gross_pnl = sum((fill_price - initial_price) * quantity * side_sign)
where side_sign = +1 for BUY (we paid less than expected = gain)
                  -1 for SELL (we received more than expected = gain).

This measures how much better/worse each order filled versus the initial
limit price, i.e. it captures chase slippage rather than true P&L from an
entry/exit pair. Orders without fill_price (OPEN / UNFILLED) contribute 0
to the PnL sum.
"""

from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException
from sqlalchemy import select, func as sa_func

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import Agent, AlgoOrder, DailyBook
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

_VALID_PERIODS = {"today", "week", "month", "all"}
_VALID_MODES = {"live", "paper", "all"}

_OPERATOR_TICKET_NAME = "(Operator ticket)"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AgentPnL(msgspec.Struct):
    """
    Per-agent order aggregate for the PnL summary view.

    Note: gross_pnl is a rough chase-slippage proxy, not realised P&L from
    position pairing. See module docstring for the exact formula.
    """
    agent_id: Optional[int]    # None for operator-ticket orders (no agent binding)
    agent_slug: Optional[str]  # Agent.slug; None for operator-ticket orders
    agent_name: str            # Agent.name, or "(Operator ticket)" when agent_id is None
    order_count: int
    filled_count: int
    gross_pnl: float           # chase-slippage proxy; 0 for unfilled/open orders
    win_count: int             # fills where PnL contribution > 0
    win_rate: float            # win_count / filled_count, 0.0 when no fills
    avg_slippage: float        # mean of AlgoOrder.slippage for filled rows, 0.0 otherwise


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _window_start(period: str) -> Optional[datetime]:
    """Return the UTC datetime at the start of the requested period."""
    now = datetime.now(tz=timezone.utc)
    if period == "today":
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "week":
        return now - timedelta(days=7)
    if period == "month":
        return now - timedelta(days=30)
    return None  # "all" — no lower bound


def _side_sign(transaction_type: str) -> float:
    """
    Sign used in the chase-slippage PnL formula.

    For a BUY order, the engine chases down: lower fill_price = positive
    outcome (we bought cheaper than the initial limit). Sign = +1.
    For a SELL order, the engine chases up: higher fill_price = positive
    outcome (we sold dearer than the initial limit). Sign = -1 (we want
    fill_price - initial_price to be negative, then flip sign).

    In practice:
      BUY  win → fill_price < initial_price → (fill - initial) is negative
                 → multiply by +1 → negative contribution
      SELL win → fill_price > initial_price → (fill - initial) is positive
                 → multiply by -1 → negative contribution

    Wait — that's backwards. Let's be explicit:
      The "good" outcome for BUY: fill_price ≤ initial_price (saved money).
      PnL contribution = (initial_price - fill_price) * qty   → positive = good.
      That is: -(fill_price - initial_price) * qty = (fill - initial) * qty * -1.

      The "good" outcome for SELL: fill_price ≥ initial_price (got more money).
      PnL contribution = (fill_price - initial_price) * qty   → positive = good.
      That is: (fill_price - initial_price) * qty * +1.

    So side_sign:
      BUY  → -1   (negate the fill−initial delta so "filled below limit" = positive)
      SELL → +1
    """
    t = (transaction_type or "").upper()
    if t == "BUY":
        return -1.0
    if t == "SELL":
        return 1.0
    return 0.0  # unknown — exclude from PnL


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

class PnLController(Controller):
    path = "/api/admin/pnl"
    guards = [admin_guard]

    @get("/by-agent")
    async def pnl_by_agent(
        self,
        period: str = "today",
        mode: str = "all",
    ) -> list[AgentPnL]:
        """
        Aggregate algo_orders by the agent that generated them.

        Query params:
          period  — today / week / month / all (default: today).
          mode    — live / paper / all (default: all).

        Orders whose agent_id does not appear in the agents table (manual
        ticket orders) are grouped under agent_id=None with the display
        name "(Operator ticket)".

        The PnL figure is a chase-slippage proxy — see module docstring.
        """
        if period not in _VALID_PERIODS:
            raise HTTPException(
                status_code=422,
                detail=f"period must be one of {sorted(_VALID_PERIODS)}",
            )
        if mode not in _VALID_MODES:
            raise HTTPException(
                status_code=422,
                detail=f"mode must be one of {sorted(_VALID_MODES)}",
            )

        window = _window_start(period)

        try:
            async with async_session() as session:
                # Fetch algo_orders within the window, optionally mode-filtered.
                # AlgoOrder.agent_id is a nullable FK to agents.id; NULL rows
                # are operator-ticket orders with no agent binding.
                stmt = select(AlgoOrder)
                if window is not None:
                    stmt = stmt.where(AlgoOrder.created_at >= window)
                if mode in ("live", "paper", "sim"):
                    stmt = stmt.where(AlgoOrder.mode == mode)
                # mode == "all" — no additional filter
                orders = (await session.execute(stmt)).scalars().all()

                # Fetch all agents for id → (slug, name) lookup.
                agents_rows = (await session.execute(select(Agent))).scalars().all()
        except Exception as exc:
            logger.error(f"PnLController.pnl_by_agent DB error: {exc}")
            raise HTTPException(status_code=500, detail="Failed to query P&L data")

        # id → (slug, name) — small table, full load is fine.
        agent_meta: dict[int, tuple[str, str]] = {
            a.id: (a.slug, a.name) for a in agents_rows
        }

        # Group orders by agent_id (None = operator ticket).
        # Each group accumulates: order_count, filled_count, pnl_sum,
        # win_count, slippage_sum, slippage_count.
        groups: dict[Optional[int], dict] = {}

        for order in orders:
            key: Optional[int] = order.agent_id  # None for operator-ticket orders
            if key not in groups:
                if key is not None and key in agent_meta:
                    slug, name = agent_meta[key]
                else:
                    slug, name = None, _OPERATOR_TICKET_NAME
                groups[key] = {
                    "agent_id": key,
                    "agent_slug": slug,
                    "agent_name": name,
                    "order_count": 0,
                    "filled_count": 0,
                    "pnl_sum": 0.0,
                    "win_count": 0,
                    "slippage_sum": 0.0,
                    "slippage_count": 0,
                }
            g = groups[key]
            g["order_count"] += 1

            status = (order.status or "").upper()
            is_filled = status == "FILLED"
            if is_filled:
                g["filled_count"] += 1

            # PnL contribution: only for filled orders with both prices.
            if (is_filled
                    and order.fill_price is not None
                    and order.initial_price is not None):
                sign = _side_sign(order.transaction_type)
                qty = int(order.quantity or 0)
                contribution = (
                    (order.fill_price - order.initial_price)
                    * qty
                    * sign
                )
                g["pnl_sum"] += contribution
                if contribution > 0:
                    g["win_count"] += 1

            # Slippage — AlgoOrder.slippage is written by the chase engine
            # as the signed ₹ difference (negative = filled worse than limit).
            if is_filled and order.slippage is not None:
                g["slippage_sum"] += float(order.slippage)
                g["slippage_count"] += 1

        # Sort: named agents (by slug) first, operator-ticket group last.
        def _sort_key(item: tuple[Optional[int], dict]) -> tuple[int, str]:
            _, g = item
            slug = g["agent_slug"] or ""
            return (0 if slug else 1, slug)

        result: list[AgentPnL] = []
        for _, g in sorted(groups.items(), key=_sort_key):
            filled = g["filled_count"]
            slippage_count = g["slippage_count"]
            result.append(AgentPnL(
                agent_id=g["agent_id"],
                agent_slug=g["agent_slug"],
                agent_name=g["agent_name"],
                order_count=g["order_count"],
                filled_count=filled,
                gross_pnl=round(g["pnl_sum"], 2),
                win_count=g["win_count"],
                win_rate=round(g["win_count"] / filled, 4) if filled > 0 else 0.0,
                avg_slippage=round(
                    g["slippage_sum"] / slippage_count, 2
                ) if slippage_count > 0 else 0.0,
            ))

        return result

    # -----------------------------------------------------------------
    # GET /api/admin/pnl/range
    # -----------------------------------------------------------------

    @get("/range", guards=[admin_guard])
    async def pnl_range(
        self,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        segment: str = "all",
        kind: str = "all",
    ) -> dict:
        """Aggregate the daily_book EOD snapshots over the requested window.

        Powers the Performance chart on /dashboard?tab=pnl + the /admin/pnl
        page. Returns 5 parallel rollups so the frontend can switch between
        Segment / Account / Symbol / Daily views without re-fetching:

        - summary       : window-wide totals (day_pnl, total_pnl, rows)
        - by_segment    : per-segment rollup
        - by_account    : per-account rollup
        - by_symbol     : per-symbol rollup (top 50, sorted by |day_pnl|)
        - daily_series  : per-day series — day_pnl, cum_pnl, and a
                          pct_change_from_start anchor (cumulative return
                          since the first day of the window)

        Dates use ISO-8601 (YYYY-MM-DD). Default from = 7 days ago IST,
        default to = today IST.  `segment` ∈ {all|equity|commodity|
        currency|derivatives}.  `kind` ∈ {all|holdings|positions|trades}.
        """
        # Defaults — last 7 IST days through today.
        try:
            today = datetime.now(timezone.utc).date()
            to_d = _date.fromisoformat(to_date) if to_date else today
            from_d = _date.fromisoformat(from_date) if from_date else (to_d - timedelta(days=7))
        except ValueError as e:
            raise HTTPException(status_code=400, detail=f"Bad date: {e}")
        if from_d > to_d:
            raise HTTPException(status_code=400, detail="from_date > to_date")

        # Build the WHERE base — applied to every rollup query so the five
        # passes return mutually consistent numbers.
        def _base_filter(q):
            q = q.where(DailyBook.date >= from_d, DailyBook.date <= to_d)
            if segment and segment != "all":
                q = q.where(DailyBook.segment == segment)
            if kind and kind != "all":
                q = q.where(DailyBook.kind == kind)
            return q

        try:
            async with async_session() as session:
                # 1) summary — single-row totals.
                summary_row = (await session.execute(_base_filter(
                    select(
                        sa_func.coalesce(sa_func.sum(DailyBook.day_pnl), 0),
                        sa_func.coalesce(sa_func.sum(DailyBook.total_pnl), 0),
                        sa_func.count(DailyBook.id),
                    )
                ))).first()
                summary = {
                    "total_day_pnl":   float(summary_row[0] or 0),
                    "total_total_pnl": float(summary_row[1] or 0),
                    "row_count":       int(summary_row[2] or 0),
                }

                # 2) by_segment
                rows = (await session.execute(_base_filter(
                    select(
                        DailyBook.segment,
                        sa_func.coalesce(sa_func.sum(DailyBook.day_pnl), 0),
                        sa_func.coalesce(sa_func.sum(DailyBook.total_pnl), 0),
                        sa_func.count(DailyBook.id),
                    ).group_by(DailyBook.segment).order_by(DailyBook.segment)
                ))).all()
                by_segment = [
                    {"segment": r[0], "day_pnl": float(r[1] or 0),
                     "total_pnl": float(r[2] or 0), "rows": int(r[3] or 0)}
                    for r in rows
                ]

                # 3) by_account
                rows = (await session.execute(_base_filter(
                    select(
                        DailyBook.account,
                        sa_func.coalesce(sa_func.sum(DailyBook.day_pnl), 0),
                        sa_func.coalesce(sa_func.sum(DailyBook.total_pnl), 0),
                        sa_func.count(DailyBook.id),
                    ).group_by(DailyBook.account).order_by(DailyBook.account)
                ))).all()
                by_account = [
                    {"account": r[0], "day_pnl": float(r[1] or 0),
                     "total_pnl": float(r[2] or 0), "rows": int(r[3] or 0)}
                    for r in rows
                ]

                # 4) by_symbol — top 50 by absolute day_pnl so the table
                # focuses on what moved the book most.
                rows = (await session.execute(_base_filter(
                    select(
                        DailyBook.symbol,
                        sa_func.coalesce(sa_func.sum(DailyBook.day_pnl), 0),
                        sa_func.coalesce(sa_func.sum(DailyBook.total_pnl), 0),
                        sa_func.count(DailyBook.id),
                    ).group_by(DailyBook.symbol)
                     .order_by(sa_func.abs(sa_func.coalesce(sa_func.sum(DailyBook.day_pnl), 0)).desc())
                     .limit(50)
                ))).all()
                by_symbol = [
                    {"symbol": r[0], "day_pnl": float(r[1] or 0),
                     "total_pnl": float(r[2] or 0), "rows": int(r[3] or 0)}
                    for r in rows
                ]

                # 5) daily_series — chronological series for the
                # Performance chart. pct_change_from_start anchors at 0 %
                # on the first day with non-zero total_pnl, then is the
                # cumulative day_pnl as a fraction of the starting capital
                # proxy (sum |avg_cost × qty| on the anchor day).
                rows = (await session.execute(_base_filter(
                    select(
                        DailyBook.date,
                        sa_func.coalesce(sa_func.sum(DailyBook.day_pnl), 0),
                        sa_func.coalesce(sa_func.sum(DailyBook.total_pnl), 0),
                    ).group_by(DailyBook.date).order_by(DailyBook.date)
                ))).all()

                # Anchor — the starting capital proxy. We use the FIRST
                # day's total_pnl as the cumulative baseline so subsequent
                # days' day_pnl translates to "% change since window start"
                # against the same denominator. Falls back to a flat 1 so
                # the percent column doesn't blow up for new accounts.
                anchor = abs(float(rows[0][2] or 0)) if rows else 0.0
                if anchor < 1:
                    anchor = 1.0
                cum = 0.0
                daily_series = []
                for r in rows:
                    day = float(r[1] or 0)
                    cum += day
                    daily_series.append({
                        "date":  r[0].isoformat(),
                        "day_pnl": day,
                        "cum_pnl": cum,
                        "pct_change_from_start": round(cum / anchor * 100.0, 4),
                    })
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"PnLController.pnl_range DB error: {e}")
            raise HTTPException(status_code=500, detail="Failed to query daily_book")

        return {
            "from_date": from_d.isoformat(),
            "to_date":   to_d.isoformat(),
            "segment":   segment,
            "kind":      kind,
            "summary":      summary,
            "by_segment":   by_segment,
            "by_account":   by_account,
            "by_symbol":    by_symbol,
            "daily_series": daily_series,
        }
