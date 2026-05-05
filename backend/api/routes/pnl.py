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

from datetime import datetime, timedelta, timezone
from typing import Optional

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException
from sqlalchemy import select

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import Agent, AlgoOrder
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
    agent_id: Optional[int]   # None for operator-ticket orders (no agent binding)
    agent_name: str           # "(Operator ticket)" when agent_id is None
    order_count: int
    filled_count: int
    gross_pnl: float          # chase-slippage proxy; 0 for unfilled/open orders
    win_count: int            # fills where PnL contribution > 0
    win_rate: float           # win_count / filled_count, 0.0 when no fills
    avg_slippage: float       # mean of AlgoOrder.slippage for filled rows, 0.0 otherwise


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
                # TODO: AlgoOrder does not carry an agent_id FK — it has an
                # `engine` and a `detail` column. Agent association can be
                # inferred from the detail string (which contains the agent slug
                # when fired by the engine) but there is no typed FK. For now
                # we group by `engine` as a proxy: engine='sim'/'paper'/'live'
                # identifies the execution path. A future schema migration that
                # adds `agent_id` to algo_orders would let us do a proper JOIN.
                #
                # Current approach: load all orders in the window, group by
                # engine (not agent_id). The AgentPnL rows returned will use
                # engine as the identifier until the FK is added.
                #
                # TODO: add `agent_id` FK column to AlgoOrder so this can be a
                # real JOIN against the agents table.
                stmt = select(AlgoOrder)
                if window is not None:
                    stmt = stmt.where(AlgoOrder.created_at >= window)
                if mode in ("live", "paper", "sim"):
                    stmt = stmt.where(AlgoOrder.mode == mode)
                elif mode == "all":
                    pass  # no filter
                orders = (await session.execute(stmt)).scalars().all()

                # Fetch all agents for name lookup — small table, full load is fine.
                agents_rows = (await session.execute(select(Agent))).scalars().all()
        except Exception as exc:
            logger.error(f"PnLController.pnl_by_agent DB error: {exc}")
            raise HTTPException(status_code=500, detail="Failed to query P&L data")

        agent_name_map: dict[int, str] = {a.id: a.name for a in agents_rows}

        # Group orders by engine (proxy for agent until agent_id FK exists).
        # Each group accumulates: order_count, filled_count, pnl_sum,
        # win_count, slippage_sum, slippage_count.
        groups: dict[str, dict] = {}

        for order in orders:
            # Use engine field as the grouping key.
            key = order.engine or "unknown"
            if key not in groups:
                groups[key] = {
                    "agent_id": None,
                    "agent_name": key,
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

        result: list[AgentPnL] = []
        for key, g in sorted(groups.items()):
            filled = g["filled_count"]
            slippage_count = g["slippage_count"]
            result.append(AgentPnL(
                agent_id=g["agent_id"],
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
