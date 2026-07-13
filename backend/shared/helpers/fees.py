"""
Indian broker fee model — simplified Kite-style schedule.

Used by the simulator's iteration summary so reported P&L reflects
what the operator would actually keep after Kite's bill of charges.
Real Kite charges have 6-7 line items (brokerage, STT, GST, exchange,
SEBI, stamp duty, IPF); this module collapses them into:

  brokerage = ₹20 flat per executed F&O order
  stt       = 0.0625 % × premium × qty   (options SELL only)
              0.0125 % × notional         (futures SELL only)
  ancillary = 0.05 % × turnover           (covers exchange + SEBI +
                                           stamp duty in one number)
  gst       = 18 % × (brokerage + ancillary)

The total is the operator's net deduction per trade. For a 1-lot
NIFTY option close at ₹150 premium, that's roughly:
  brokerage ₹20 + stt 0.0625% × 150 × 50 = ₹4.69
  + ancillary 0.05% × 7500 = ₹3.75
  + gst 18% × 23.75 = ₹4.28
  ≈ ₹32.72 per leg

Two legs per round trip → ~₹65 per round trip. Multiply by lot count.

Exposed: compute_order_fees(order_dict) → float total fees in ₹.
"""

from __future__ import annotations


_BROKERAGE_PER_ORDER  = 20.0     # ₹ flat per executed F&O order (option or future)
_STT_OPT_SELL_PCT     = 0.0625   # %
_STT_FUT_SELL_PCT     = 0.0125   # %
_ANCILLARY_PCT        = 0.05     # % of turnover — covers exchange + SEBI + stamp + IPF
_GST_PCT              = 18.0     # % on (brokerage + ancillary)


def _compute_stt(turnover: float, side: str, is_option: bool, is_future: bool) -> float:
    """Return Securities Transaction Tax for an F&O SELL leg (0 on BUY)."""
    if side != "SELL":
        return 0.0
    if is_option:
        return turnover * (_STT_OPT_SELL_PCT / 100.0)
    if is_future:
        return turnover * (_STT_FUT_SELL_PCT / 100.0)
    return 0.0


def _parse_order_fields(order: dict) -> "tuple[str, str, float, float] | None":
    """Parse sym, side, qty, price from an order dict. Returns None on error."""
    try:
        sym   = str(order.get("tradingsymbol") or order.get("symbol") or "").upper()
        side  = str(order.get("transaction_type") or order.get("side") or "").upper()
        qty   = float(order.get("quantity") or 0)
        price = order.get("fill_price") if order.get("fill_price") is not None \
            else order.get("initial_price") if order.get("initial_price") is not None \
            else order.get("price")
        price = float(price or 0)
        return sym, side, qty, price
    except (TypeError, ValueError):
        return None


def compute_order_fees(order: dict) -> float:
    """
    Estimate the total fees (₹) Kite would charge for a single executed
    F&O order. Reads from an AlgoOrder-shaped dict (sim or live):
      tradingsymbol  — ends in CE/PE/FUT
      transaction_type — 'BUY' or 'SELL'
      quantity        — total contracts (post-multiplier)
      fill_price OR initial_price — per-share / per-unit price
      mode            — informational

    Returns 0 for non-F&O orders, malformed rows, or zero-qty/price.
    The estimate is conservative (uses ancillary rate that absorbs
    several small items into a single round number); error vs Kite's
    actual bill is typically < 5%.
    """
    fields = _parse_order_fields(order)
    if fields is None:
        return 0.0
    sym, side, qty, price = fields

    if qty <= 0 or price <= 0:
        return 0.0

    is_option = sym.endswith("CE") or sym.endswith("PE")
    is_future = sym.endswith("FUT")
    if not (is_option or is_future):
        return 0.0

    turnover  = price * qty
    brokerage = _BROKERAGE_PER_ORDER
    stt       = _compute_stt(turnover, side, is_option, is_future)
    ancillary = turnover * (_ANCILLARY_PCT / 100.0)
    gst       = (brokerage + ancillary) * (_GST_PCT / 100.0)
    return round(brokerage + stt + ancillary + gst, 2)
