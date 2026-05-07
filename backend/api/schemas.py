"""
msgspec response schemas for all API endpoints.
msgspec.Struct is ~10x faster than pydantic for serialisation.
Litestar has native msgspec support — no adapter needed.
"""

from typing import Optional
import msgspec


# ---------------------------------------------------------------------------
# Holdings
# ---------------------------------------------------------------------------

class HoldingRow(msgspec.Struct):
    account: str
    tradingsymbol: str
    exchange: str
    quantity: int
    average_price: float
    close_price: float
    inv_val: float
    cur_val: float
    pnl: float
    pnl_percentage: float
    day_change: float = 0.0
    day_change_val: float = 0.0
    day_change_percentage: float = 0.0


class HoldingsSummaryRow(msgspec.Struct):
    account: str
    inv_val: float
    cur_val: float
    pnl: float
    pnl_percentage: float
    day_change_val: float
    day_change_percentage: float
    cash: Optional[float] = None
    net: Optional[float] = None


class HoldingsResponse(msgspec.Struct):
    rows: list[HoldingRow]
    summary: list[HoldingsSummaryRow]
    refreshed_at: str


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

class PositionRow(msgspec.Struct):
    account: str
    tradingsymbol: str
    exchange: str
    product: str
    quantity: int
    average_price: float
    close_price: float
    pnl: float
    unrealised: float = 0.0
    realised: float = 0.0


class PositionsSummaryRow(msgspec.Struct):
    account: str
    pnl: float


class PositionsResponse(msgspec.Struct):
    rows: list[PositionRow]
    summary: list[PositionsSummaryRow]
    refreshed_at: str


# ---------------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------------

class FundsRow(msgspec.Struct):
    account: str
    cash: float           # avail opening_balance
    avail_margin: float   # net
    used_margin: float    # util debits
    collateral: float     # avail collateral


class FundsResponse(msgspec.Struct):
    rows: list[FundsRow]
    refreshed_at: str


# ---------------------------------------------------------------------------
# Market update
# ---------------------------------------------------------------------------

class MarketResponse(msgspec.Struct):
    content: str
    cycle_date: str
    refreshed_at: str


# ---------------------------------------------------------------------------
# Market news headlines
# ---------------------------------------------------------------------------

class NewsItem(msgspec.Struct):
    title: str
    link: str
    source: str
    timestamp: str  # "Mon, April 20, 2026, 09:30 AM IST | Mon, April 20, 2026, 12:00 AM EDT"


class NewsResponse(msgspec.Struct):
    items: list[NewsItem]
    refreshed_at: str


# ---------------------------------------------------------------------------
# Agent grammar token CRUD
# ---------------------------------------------------------------------------

class GrammarTokenOut(msgspec.Struct):
    id:            int
    grammar_kind:  str                         # condition | notify | action
    token_kind:    str                         # metric | scope | operator | channel | format | template | action_type
    token:         str
    value_type:    str | None = None
    units:         str | None = None
    description:   str = ""
    resolver:      str | None = None
    params_schema: dict | None = None
    enum_values:   list | None = None
    template_body: str | None = None
    is_system:     bool = False
    is_active:     bool = True


class GrammarTokenCreate(msgspec.Struct):
    grammar_kind:  str
    token_kind:    str
    token:         str
    value_type:    str | None = None
    units:         str | None = None
    description:   str = ""
    resolver:      str | None = None
    params_schema: dict | None = None
    enum_values:   list | None = None
    template_body: str | None = None
    is_active:     bool = True


class GrammarTokenPatch(msgspec.Struct):
    # All optional — only fields the caller sets are mutated.
    value_type:    str | None = None
    units:         str | None = None
    description:   str | None = None
    resolver:      str | None = None
    params_schema: dict | None = None
    enum_values:   list | None = None
    template_body: str | None = None
    is_active:     bool | None = None


# ---------------------------------------------------------------------------
# Post / Insights
# ---------------------------------------------------------------------------

class PostResponse(msgspec.Struct):
    content: str
    refreshed_at: str


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

class PlaceOrderRequest(msgspec.Struct):
    account: str
    variety: str = "regular"
    exchange: str = ""
    tradingsymbol: str = ""
    transaction_type: str = ""
    quantity: int = 0
    product: str = ""
    order_type: str = ""
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    validity: str = "DAY"
    tag: Optional[str] = None


class TicketOrderRequest(msgspec.Struct):
    """
    Operator-initiated order from the reusable <OrderTicket>.
    `mode` selects the destination:
      - "paper" → register with the prod paper engine; lifecycle
        runs through the same chase loop agent fires use.
      - "live"  → real broker order via Kite (phase 3).
    Drafts never reach the backend (handled client-side).
    """
    mode: str               # "paper" | "live"
    side: str               # "BUY" | "SELL"
    tradingsymbol: str
    quantity: int
    exchange: str = "NFO"
    product: str = "NRML"
    order_type: str = "LIMIT"
    variety: str = "regular"
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    account: str = ""       # required for PAPER + LIVE; blank → 400
    # Chase the order to closure — re-quote the limit each tick
    # until filled, capped by `simulator.chase_max_attempts`. PAPER
    # orders honour this via the paper engine's tick loop; the flag
    # is also a hook for future LIVE chase wiring. MARKET / SL-M
    # orders ignore the flag (no limit to chase).
    chase: bool = True
    # Chase aggressiveness — controls how the engine re-quotes the
    # limit on each tick. Industry analogue: IBKR Adaptive Algo's
    # Patient / Normal / Urgent.
    #   "low"  → passive: SELL pegs to ASK, BUY pegs to BID (sit
    #            on your own side, wait for the market to come)
    #   "med"  → midpoint: (bid + ask) / 2
    #   "high" → aggressive: SELL pegs to BID, BUY pegs to ASK
    #            (cross the spread to take liquidity)
    # Default `low` — operator's standing instruction is "be
    # patient on entry"; callers explicitly bump to med/high when
    # they want fill speed at the cost of slippage.
    chase_aggressiveness: str = "low"


class TicketOrderResponse(msgspec.Struct):
    order_id: str
    mode: str
    status: str
    detail: str


class ModifyOrderRequest(msgspec.Struct):
    account: str
    variety: str = "regular"
    quantity: Optional[int] = None
    price: Optional[float] = None
    order_type: Optional[str] = None
    trigger_price: Optional[float] = None
    validity: Optional[str] = None


class OrderRow(msgspec.Struct):
    order_id: str
    account: str
    exchange: str
    tradingsymbol: str
    transaction_type: str
    quantity: int
    pending_quantity: int
    filled_quantity: int
    price: float
    trigger_price: float
    average_price: float
    status: str
    order_type: str
    product: str
    variety: str
    order_timestamp: str
    exchange_timestamp: Optional[str] = None
    status_message: Optional[str] = None
    tag: Optional[str] = None


class OrdersResponse(msgspec.Struct):
    rows: list[OrderRow]
    refreshed_at: str


class PlaceOrderResponse(msgspec.Struct):
    order_id: str
    account: str
    detail: str = "Order placed successfully"


class CancelOrderResponse(msgspec.Struct):
    order_id: str
    detail: str = "Order cancelled successfully"


class ModifyOrderResponse(msgspec.Struct):
    order_id: str
    detail: str = "Order modified successfully"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class AccountInfo(msgspec.Struct):
    account_id: str
    display: str


class AccountsResponse(msgspec.Struct):
    accounts: list[AccountInfo]
