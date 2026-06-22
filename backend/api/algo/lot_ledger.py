"""
Per-strategy FIFO lot ledger.

The ledger turns broker NET-position data into LOT-LEVEL attribution
so partial closes credit the right strategy's P&L. Two operations:

    open_lot(session, *, algo_order_id, ...)        — opens a new lot
    close_lot_fifo(session, *, account, symbol,
                   strategy_id, side, qty, price)   — consumes oldest lots first

Both are best-effort: if the order has no strategy_id (legacy /
operator-omitted attribution), the helper logs a debug line and
returns without writing. The caller's hot path stays the same; the
ledger fills in over time as strategies get attributed.

FIFO match details (`close_lot_fifo`):
- Find every OPEN lot with the same (strategy, account, symbol) and
  the OPPOSITE side. SELL-closes a long ('B') lot; BUY-closes a
  short ('S') lot.
- Sort by `opened_at ASC` (oldest first — FIFO).
- Consume `qty` across one or more lots:
  - For each lot, take min(lot.remaining_qty, qty_remaining_to_close).
  - Update lot.remaining_qty -= portion.
  - Update lot.close_price = weighted average of all closing prices.
  - Update lot.realized_pnl += portion × (close_price - open_price) × sign.
  - When lot.remaining_qty hits 0, set lot.closed_at = now.
- Partial closes leave remaining_qty > 0; the lot stays open for
  the next counter-fill.

P&L convention: long lots earn (close - open) × qty. Short lots
earn (open - close) × qty. The `_pnl_sign` helper just flips it.

Caller responsibility: passing the right `algo_order_id` so the
ledger entry can backlink. The fill path in PaperTradeEngine reads
the order's `strategy_id` (set at place time on AlgoOrder); the
ledger writes happen out-of-band after the row's status flips to
FILLED.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, and_, asc

logger = logging.getLogger(__name__)


def _pnl_sign(side: str) -> int:
    """+1 for a long lot (BUY-opened); -1 for short ('S')."""
    return 1 if side == "B" else -1


async def open_lot(
    session,
    *,
    strategy_id: Optional[int],
    algo_order_id: Optional[int],
    account: str,
    symbol: str,
    exchange: str,
    side_kite: str,   # 'BUY' | 'SELL'
    qty: int,
    open_price: float,
) -> Optional[int]:
    """Open a new lot on a fill. Returns the new lot's id, or None
    when strategy_id is falsy / qty is non-positive (best-effort —
    legacy orders pre-dating attribution skip the ledger and the
    realised-pnl rollup falls back to AlgoOrder.pnl for those).

    `side_kite` is the Kite convention (BUY / SELL); converted to
    the ledger's compact 'B' / 'S'.
    """
    if not strategy_id or qty <= 0:
        return None
    from backend.api.models import StrategyLot
    ledger_side = "B" if side_kite.upper() == "BUY" else "S"
    lot = StrategyLot(
        strategy_id=int(strategy_id),
        open_order_id=algo_order_id,
        account=account.strip().upper(),
        symbol=symbol.strip().upper(),
        exchange=exchange.strip().upper(),
        side=ledger_side,
        qty=int(qty),
        remaining_qty=int(qty),
        open_price=Decimal(str(open_price)),
        close_price=None,
        realized_pnl=Decimal("0.0"),
    )
    session.add(lot)
    await session.flush()    # populate lot.id while still in the txn
    logger.info(
        f"lot_ledger.open_lot: strategy={strategy_id} {ledger_side} {qty} "
        f"{symbol} @{open_price} (lot_id={lot.id}, order_id={algo_order_id})"
    )
    return lot.id


async def close_lot_fifo(
    session,
    *,
    strategy_id: Optional[int],
    algo_order_id: Optional[int],
    account: str,
    symbol: str,
    side_kite: str,   # 'BUY' | 'SELL' of the CLOSING fill
    qty: int,
    close_price: float,
) -> tuple[float, int]:
    """Consume `qty` from oldest open opposite-side lots.

    Returns (realised_pnl_total, qty_actually_closed). The closed qty
    may be less than `qty` when the ledger has fewer lots open than
    the broker reports (e.g. strategy_id was set on the close but not
    on the original open — the operator switched strategies mid-
    holding). Caller logs but doesn't fail in that case; the rest of
    the position attributes to whichever strategy holds the next
    open lot, or falls back to AlgoOrder.pnl when none.
    """
    if not strategy_id or qty <= 0:
        return (0.0, 0)
    from backend.api.models import StrategyLot

    closer_side_kite = side_kite.upper()
    # SELL closes a long lot ('B'); BUY closes a short lot ('S').
    target_side = "B" if closer_side_kite == "SELL" else "S"
    sign = _pnl_sign(target_side)

    open_lots = (await session.execute(
        select(StrategyLot).where(and_(
            StrategyLot.strategy_id == int(strategy_id),
            StrategyLot.account == account.strip().upper(),
            StrategyLot.symbol  == symbol.strip().upper(),
            StrategyLot.side    == target_side,
            StrategyLot.remaining_qty > 0,
        )).order_by(asc(StrategyLot.opened_at))
    )).scalars().all()

    if not open_lots:
        logger.debug(
            f"lot_ledger.close_lot_fifo: no open {target_side}-side lots for "
            f"strategy={strategy_id} {symbol} on {account}; "
            f"close of {qty} @{close_price} not attributed."
        )
        return (0.0, 0)

    remaining = int(qty)
    total_pnl = Decimal("0.0")
    closed_qty = 0
    close_price_d = Decimal(str(close_price))
    for lot in open_lots:
        if remaining <= 0:
            break
        consume = min(int(lot.remaining_qty), remaining)
        # Per-partial P&L. sign = +1 long, -1 short.
        lot_pnl = (close_price_d - lot.open_price) * Decimal(consume) * Decimal(sign)
        # Weighted-average close_price across every partial that hit
        # this lot. NULL on first close; subsequent closes blend.
        if lot.close_price is None:
            lot.close_price = close_price_d
        else:
            qty_already_closed = int(lot.qty) - int(lot.remaining_qty)
            new_qty_closed = qty_already_closed + consume
            lot.close_price = (
                (lot.close_price * Decimal(qty_already_closed)
                 + close_price_d * Decimal(consume))
                / Decimal(new_qty_closed)
            )
        lot.remaining_qty = int(lot.remaining_qty) - consume
        lot.realized_pnl  = (lot.realized_pnl or Decimal("0.0")) + lot_pnl
        if lot.remaining_qty == 0:
            lot.closed_at = datetime.now(timezone.utc)
        remaining -= consume
        closed_qty += consume
        total_pnl += lot_pnl

    logger.info(
        f"lot_ledger.close_lot_fifo: strategy={strategy_id} closed {closed_qty}/{qty} "
        f"{symbol} @{close_price} pnl={float(total_pnl):.2f} "
        f"(closing order_id={algo_order_id})"
    )
    return (float(total_pnl), closed_qty)


async def compute_strategy_pnl(session, strategy_id: int) -> dict:
    """Aggregate per-strategy P&L numbers off the lot ledger.

    Returns a dict with:
      realised_pnl    — sum of every lot's realized_pnl (closed +
                        partial). Authoritative number for SEBI
                        statements + the /strategies page rollup.
      unrealised_pnl  — APPROXIMATION. Lots with remaining_qty > 0
                        still have open exposure; without an LTP
                        feed here we can't mark them to market. The
                        caller's existing AlgoOrder.pnl SUM is a
                        better unrealised approximation today, so
                        this helper returns None for unrealised and
                        the caller composes.
      open_lots_count — count of lots where remaining_qty > 0.
      open_qty        — sum of remaining_qty across open lots
                        (operator's net open exposure attributed to
                        this strategy).
    """
    from backend.api.models import StrategyLot
    from sqlalchemy import func

    realised = (await session.execute(
        select(func.coalesce(func.sum(StrategyLot.realized_pnl), 0.0))
        .where(StrategyLot.strategy_id == int(strategy_id))
    )).scalar_one() or 0.0
    open_count = (await session.execute(
        select(func.count(StrategyLot.id))
        .where(StrategyLot.strategy_id == int(strategy_id),
               StrategyLot.remaining_qty > 0)
    )).scalar_one() or 0
    open_qty = (await session.execute(
        select(func.coalesce(func.sum(StrategyLot.remaining_qty), 0))
        .where(StrategyLot.strategy_id == int(strategy_id),
               StrategyLot.remaining_qty > 0)
    )).scalar_one() or 0

    return {
        "realised_pnl":     float(realised or 0.0),
        "unrealised_pnl":   None,        # caller fills in via AlgoOrder.pnl
        "open_lots_count":  int(open_count or 0),
        "open_qty":         int(open_qty or 0),
    }
