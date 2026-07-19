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
    last_price: float = 0.0
    day_change: float = 0.0
    day_change_val: float = 0.0
    day_change_percentage: float = 0.0
    # opening_quantity is what we had at start of day — stays stable
    # through intraday sells while `quantity` drops to 0 after a full
    # sell. Day-P&L and inv_val are computed against opening_quantity
    # in the broker layer, so callers (watchlist) that surface
    # per-symbol qty should prefer this when quantity has gone to 0.
    opening_quantity: int = 0
    # True when last_price was sourced from the last-known-good cache
    # (both PriceBroker.quote and KiteTicker returned 0 or raised).
    # Frontend can surface a small staleness indicator on these rows.
    last_price_stale: bool = False
    # True when the row was substituted from broker_apis' per-account
    # last-known-good frame cache because the account's circuit breaker
    # was OPEN at fetch time. Distinct from `last_price_stale` (LTP-only
    # staleness) — `account_stale` means the entire row is a stale copy
    # from a prior successful fetch. Frontend surfaces this via a slate
    # row tint + optional "stale @ HH:MM" account-cell badge.
    account_stale: bool = False
    # HH:MM IST string of the last successful broker fetch for this
    # account. Non-empty only when account_stale=True. Frontend renders
    # this as a small "STALE @ HH:MM" badge next to the account name.
    account_stale_since: str = ""
    # Per-row price source tag — three values under the unified animation
    # model (Jul 2026):
    #   • "live"                 — exchange open, broker/ticker LTP
    #   • "snapshot_settled"     — exchange closed and Kite has published
    #                              close_price (post-45m settled window)
    #   • "snapshot_unsettled"   — exchange closed but broker close_price
    #                              not yet available (pre-settled window)
    # Legacy value "snapshot" is still accepted by consumers until Commit 2
    # ships the resolver — treat it as equivalent to "snapshot_settled".
    price_source: str = "live"
    # Unified-model alias for last_price. Populated alongside last_price
    # in the route overlay so consumers can migrate off the LTP-specific
    # name gradually. Zero here means "unset" (frontend falls back to
    # last_price for backward compat).
    current_price: float = 0.0
    # Whether cells on this row should render tick-flash / freshness
    # shimmer animations. True while the row's exchange is currently
    # open; False on closed-exchange snapshot rows so cells render
    # static (no green/red pulse on the frozen close_price). Frontend's
    # tick-flash cellClass reads this to gate the flash primitive.
    is_animating: bool = True


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
    # ISO-8601 UTC timestamp of the snapshot that was returned.
    # Non-null only when serving a persisted off-hours snapshot so the
    # frontend can show a staleness hint ("as of <time>") instead of a
    # live label.  Null during market hours (live broker data).
    as_of: Optional[str] = None
    # Accounts whose rows were substituted from the broker_apis last-known-
    # good frame cache because their circuit breaker was OPEN at fetch
    # time. Frontend NavStrip / TOTAL rollups tag aggregates as "stale @
    # HH:MM" when any account in this list contributes to the sum.
    stale_accounts: list[str] = []


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
    last_price: float = 0.0
    pnl_percentage: float = 0.0
    # Yesterday's total_pnl from the most-recent daily_book snapshot
    # captured before today's midnight IST.  None means the position was
    # opened today (no prior-session book entry exists).
    prev_settlement_pnl: Optional[float] = None
    unrealised: float = 0.0
    realised: float = 0.0
    day_change: float = 0.0
    day_change_val: float = 0.0
    day_change_percentage: float = 0.0
    # Position-level Greeks for F&O rows — Δ-exposure (delta × qty) and
    # Θ-per-day (theta × qty). Non-option rows leave both at 0.0.
    # Surfaced on /performance + /dashboard positions grids; the IBKR
    # TWS / Bloomberg OMON convention is to show these alongside LTP.
    delta_pos: float = 0.0
    theta_pos: float = 0.0
    # Underlying spot for options + futures — the price of the
    # underlying at broker-fetch time (or LKG during closed hours).
    # Fetched once per unique underlying in _enrich_positions Pass 2
    # (positions.py) and stashed here so every frontend consumer
    # (NavStrip P.expiry, Snapshot Exp P&L, payoff overlay) can compute
    # intrinsic-at-expiry without reconstructing the spot via a chain
    # of client-side fallbacks. Non-derivative rows carry 0.0.
    underlying_ltp: float = 0.0
    # Intraday split — exposed so the Candidates grid can detect
    # close-and-reopen activity and synthesize separate display rows.
    # All post-multiplier (matches the `quantity` column's units).
    overnight_quantity: int   = 0
    day_buy_quantity:   int   = 0
    day_sell_quantity:  int   = 0
    day_buy_value:      float = 0.0
    day_sell_value:     float = 0.0
    # True when last_price was sourced from the last-known-good cache
    # (both PriceBroker.quote and KiteTicker returned 0 or raised).
    # Frontend can surface a small staleness indicator on these rows.
    last_price_stale: bool = False
    # True when the row was substituted from broker_apis' per-account
    # last-known-good frame cache because the account's circuit breaker
    # was OPEN at fetch time. Distinct from `last_price_stale` (LTP-only
    # staleness) — `account_stale` means the entire row is a stale copy
    # from a prior successful fetch. Frontend surfaces this via a slate
    # row tint + optional "stale @ HH:MM" account-cell badge.
    account_stale: bool = False
    # HH:MM IST string of the last successful broker fetch for this
    # account. Non-empty only when account_stale=True. Frontend renders
    # this as a small "STALE @ HH:MM" badge next to the account name.
    account_stale_since: str = ""
    # "live" for broker-fetched rows; "paper" for synthesized paper rows.
    # Default "live" preserves backward compatibility with all existing callers.
    mode: str = "live"
    # Per-row price source tag — three values under the unified animation
    # model (Jul 2026):
    #   • "live"                 — exchange open, broker/ticker LTP
    #   • "snapshot_settled"     — exchange closed and Kite has published
    #                              close_price (post-45m settled window)
    #   • "snapshot_unsettled"   — exchange closed but broker close_price
    #                              not yet available (pre-settled window)
    # Legacy value "snapshot" is still accepted by consumers until Commit 2
    # ships the resolver — treat it as equivalent to "snapshot_settled".
    price_source: str = "live"
    # Unified-model alias for last_price. Populated alongside last_price
    # in the route overlay so consumers can migrate off the LTP-specific
    # name gradually. Zero here means "unset" (frontend falls back to
    # last_price for backward compat).
    current_price: float = 0.0
    # Whether cells on this row should render tick-flash / freshness
    # shimmer animations. True while the row's exchange is currently
    # open; False on closed-exchange snapshot rows so cells render
    # static (no green/red pulse on the frozen close_price). Frontend's
    # tick-flash cellClass reads this to gate the flash primitive.
    is_animating: bool = True


class PositionsSummaryRow(msgspec.Struct):
    account: str
    pnl: float
    day_change_val: float = 0.0
    day_change_percentage: float = 0.0
    # Per-account |close × qty| sum — Σ across all open positions for
    # the account. Lets the frontend's filtered-subset TOTAL row derive
    # a meaningful day_change_percentage (Σday_pnl / Σprev_val) without
    # re-fetching raw positions.
    day_prev_val: float = 0.0


class PositionsResponse(msgspec.Struct):
    rows: list[PositionRow]
    summary: list[PositionsSummaryRow]
    refreshed_at: str
    # ISO-8601 UTC timestamp of the snapshot that was returned.
    # Non-null only when serving a persisted off-hours snapshot so the
    # frontend can show a staleness hint ("as of <time>") instead of a
    # live label.  Null during market hours (live broker data).
    as_of: Optional[str] = None
    # Accounts whose rows were substituted from the broker_apis last-known-
    # good frame cache because their circuit breaker was OPEN at fetch
    # time. Frontend NavStrip / TOTAL rollups tag aggregates as "stale @
    # HH:MM" when any account in this list contributes to the sum.
    stale_accounts: list[str] = []


# ---------------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------------

class FundsRow(msgspec.Struct):
    account: str
    cash: float           # avail opening_balance — start-of-day cash
    avail_margin: float   # net — what's left for trading after used_margin
    used_margin: float    # util debits
    collateral: float     # avail collateral
    # Defaults are 0 — older Kite responses without `avail.cash` /
    # `util.option_premium` (or any broker adapter that doesn't
    # surface them) fall through cleanly instead of raising a
    # missing-key construction error in the route's FundsRow(**r)
    # builder.
    live_cash:        float = 0.0  # avail cash (= live_balance) — decreases on option premium debit
    option_premium:   float = 0.0  # util option_premium — net cash spent on currently-held long options
                                   # (≈ debits − receipts; positive when net long premium)
    # Derived convenience columns — computed server-side so the frontend
    # never re-implements the arithmetic.
    available_funds:  float = 0.0  # = avail_margin — free cash for new trades (broker "net")
    available_cash:   float = 0.0  # = cash − option_premium — SOD cash net of locked option premiums
    # True when the row was substituted from broker_apis' per-account
    # last-known-good frame cache because the account's circuit breaker
    # was OPEN at fetch time. Frontend surfaces via slate row tint.
    account_stale: bool = False
    # HH:MM IST string of the last successful broker fetch for this
    # account. Non-empty only when account_stale=True.
    account_stale_since: str = ""


class FundsResponse(msgspec.Struct):
    rows: list[FundsRow]
    refreshed_at: str
    # Accounts whose rows were substituted from the broker_apis last-known-
    # good frame cache. See PositionsResponse.stale_accounts for details.
    stale_accounts: list[str] = []


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
    # Optional bull / bear / neutral tag, populated when the route is
    # called with ?sentiment=true. NULL means "not scored on this call"
    # — clients distinguish "no sentiment data" from "neutral" cleanly.
    sentiment: str | None = None


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
# OrderTemplate — TP/SL/Wing exit-rule preset attached at order entry
# ---------------------------------------------------------------------------

class OrderTemplateOut(msgspec.Struct):
    id:                  int
    slug:                str | None = None
    name:                str = ""
    description:         str = ""
    applies_to:          str = "both"
    tp_pct:              float | None = None
    sl_pct:              float | None = None
    wing_premium_pct:    float | None = None
    wing_strike_offset:  int | None = None
    tp_order_type:       str = "LIMIT"   # 'LIMIT' | 'MARKET'
    # JSON string of [{at_pct, close_pct}] entries; None / empty = no
    # scale-out (TP behaves as a single trigger via tp_pct).
    tp_scales_json:      str | None = None
    sl_trail_pct:        float | None = None
    is_default:          bool = False
    is_system:           bool = False
    is_active:           bool = True


class OrderTemplateCreate(msgspec.Struct):
    name:                str
    description:         str = ""
    applies_to:          str = "both"      # 'buy_any' / 'sell_option' / 'both'
    tp_pct:              float | None = None
    sl_pct:              float | None = None
    wing_premium_pct:    float | None = None
    wing_strike_offset:  int | None = None
    tp_order_type:       str = "LIMIT"
    tp_scales_json:      str | None = None
    sl_trail_pct:        float | None = None
    is_default:          bool = False
    is_active:           bool = True


class OrderTemplatePatch(msgspec.Struct):
    # All optional — only fields the caller sets are mutated. System
    # templates accept these same field edits (operator tunes the
    # numeric defaults from the UI); only `is_system` itself + delete
    # are off-limits.
    name:                str | None = None
    description:         str | None = None
    applies_to:          str | None = None
    tp_pct:              float | None = None
    sl_pct:              float | None = None
    wing_premium_pct:    float | None = None
    wing_strike_offset:  int | None = None
    tp_order_type:       str | None = None
    tp_scales_json:      str | None = None
    sl_trail_pct:        float | None = None
    is_default:          bool | None = None
    is_active:           bool | None = None


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


class BasketLeg(msgspec.Struct):
    """One leg inside a basket order group.

    `quantity` follows the v2 convention (2026-07-08): LOTS for F&O
    exchanges (NFO/MCX/CDS/BFO/BCD/NCO), raw shares for equity. Same
    unit as TicketOrderRequest."""
    tradingsymbol: str
    exchange: str
    transaction_type: str           # "BUY" | "SELL"
    quantity: int                   # LOTS for F&O; raw shares for equity
    order_type: str = "LIMIT"
    product: str = "NRML"
    price: Optional[float] = None
    trigger_price: Optional[float] = None
    variety: str = "regular"
    # When set, the basket-placement route runs
    # `apply_template_to_order` on this leg after submit so the
    # leg's fill attaches TP / SL / Wing GTTs per the template.
    # Same pipeline TicketOrderRequest uses. Frontend's option-chain
    # basket builder sets this from the operator's "On fill"
    # selector in the shell basket bar (SymbolPanel).
    template_id: Optional[int] = None
    # Per-leg chase + aggressiveness used by the basket-placement route.
    chase: bool = True
    chase_aggressiveness: str = "low"
    # Per-leg take-profit override fields (legacy single-leg path
    # carries these too).
    target_pct: Optional[float] = None
    target_abs: Optional[float] = None
    # Per-leg template parameter overrides — operator's tweaks to
    # the selected template's defaults for THIS submit. Persisted on
    # the AlgoOrder row as `template_overrides_json` so the postback
    # handler re-applies them when the parent fills. Empty / None on
    # any field means "use the template's default for that param."
    tp_pct_override:             Optional[float] = None
    sl_pct_override:             Optional[float] = None
    wing_premium_pct_override:   Optional[float] = None
    wing_strike_offset_override: Optional[int]   = None
    # Optional carry-through for the trail-stop + scale-out template
    # fields. Frontend doesn't expose inputs for these today, but the
    # `_build_overrides_json` serializer reads them so a future per-
    # ticket override path needs only the UI work. None = inherit the
    # selected template's value.
    sl_trail_pct_override:       Optional[float] = None
    tp_scales_json_override:     Optional[str]   = None
    # Intent — "open" | "close". Same semantics as TicketOrderRequest.intent
    # (bypasses G2 5-lot cap for close). None → treat as open.
    intent: Optional[str] = None
    # Attribution — foreign key to strategies.id. Flows through to the
    # AlgoOrder row for per-strategy P&L attribution. Same semantics as
    # TicketOrderRequest.strategy_id. None = unattributed.
    strategy_id: Optional[int] = None


class BasketGroup(msgspec.Struct):
    """All legs that belong to one broker account in a basket call."""
    account: str
    legs: list[BasketLeg]


class BasketOrderRequest(msgspec.Struct):
    """POST /api/orders/basket request body."""
    groups: list[BasketGroup]
    # Optional TP override; falls back to algo.default_target_pct when None.
    target_pct: Optional[float] = None


class BasketLegResult(msgspec.Struct):
    leg_index: int
    order_id: Optional[str]
    status: str            # "OPEN" | "PAPER" | "SHADOW" | "error"
    error: Optional[str] = None


class BasketGroupResult(msgspec.Struct):
    account: str
    basket_id: str
    results: list[BasketLegResult]
    margin_required: Optional[float] = None
    margin_available: Optional[float] = None


class BasketOrderResponse(msgspec.Struct):
    groups: list[BasketGroupResult]


class BasketMarginGroupResult(msgspec.Struct):
    account: str
    required: Optional[float]
    available: Optional[float]
    shortfall: Optional[float]
    error: Optional[str] = None


class BasketMarginResponse(msgspec.Struct):
    groups: list[BasketMarginGroupResult]


class TicketOrderRequest(msgspec.Struct):
    """
    Operator-initiated order from the reusable <OrderTicket>.
    `mode` selects the destination:
      - "paper" → register with the prod paper engine; lifecycle
        runs through the same chase loop agent fires use.
      - "live"  → real broker order via Kite (phase 3).
    Drafts never reach the backend (handled client-side).

    `quantity` unit convention (v2 API — 2026-07-08):
      - F&O exchanges (NFO/MCX/CDS/BFO/BCD/NCO): quantity is LOTS. The
        backend resolves lot_size from the instruments cache and
        multiplies to get contracts internally. `lot_size_hint` acts
        as a fallback when the backend cache is cold.
      - Equity exchanges (NSE/BSE): quantity is raw shares.
    Sending contracts for an F&O order is a critical bug that was
    the P0 CRUDEOIL 100× oversize incident (2026-07-01) — the new
    lots convention eliminates the class of bug entirely by aligning
    the API unit with the operator's mental model.
    """
    mode: str               # "paper" | "live"
    side: str               # "BUY" | "SELL"
    tradingsymbol: str
    quantity: int           # LOTS for F&O; raw shares for equity
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
    # Source tag for the manual-agent audit trail. Defaults to "ticket"
    # so existing callers need no change; chain/command tabs can pass
    # "chain" or "command" to distinguish in agent_events.
    source: str = "ticket"
    # Attribution (slice 6). Optional foreign key to strategies.id —
    # captured on the AlgoOrder row so per-strategy P&L flows from
    # this order. None / 0 = unattributed (legacy / operator chose
    # "no strategy"). Will tighten to required after slice 7 lands
    # the lot ledger; for now nullable to keep the existing operator
    # workflow untouched.
    strategy_id: Optional[int] = None
    # ── Legacy single-TP path ─────────────────────────────────────────
    # `target_pct` is the v1 take-profit field — fractional (0.30 =
    # +30%). Target portfolio allocation percentage for this position.
    # When present and no template is supplied, the handler auto-maps
    # it to tp_pct_override so the same downstream attach pipeline fires.
    target_pct: Optional[float] = None
    # ── v2 template attachment ────────────────────────────────────────
    # `template_id` references an OrderTemplate row. When set, the
    # handler resolves the template + applies its TP/SL/Wing via
    # backend.api.algo.template_attach. Override fields below let the
    # operator tune the chosen template for THIS order without saving;
    # template defaults supply anything left None.
    template_id:                  Optional[int]   = None
    tp_pct_override:              Optional[float] = None   # % (30.0 = +30%)
    sl_pct_override:              Optional[float] = None
    wing_premium_pct_override:    Optional[float] = None
    wing_strike_offset_override:  Optional[int]   = None
    # Trail-stop + scale-out override carriers. UI doesn't expose inputs
    # for these yet; `_build_overrides_json` already reads them so they
    # flow through the postback handler's override-replay path when the
    # frontend starts sending them.
    sl_trail_pct_override:        Optional[float] = None
    tp_scales_json_override:      Optional[str]   = None
    # Lot-size hint from OrderTicket UI. Frontend already resolved
    # _lotSize from the instruments cache; shipping it here lets the
    # backend cross-check against its own cache lookup and use the
    # frontend value as a fallback when the backend cache is cold.
    # Only relevant for MCX/NCO where translate_qty divides by lot_size;
    # ignored (None / 0) for non-derivative symbols. Integer — same units
    # as instruments.lot_size (barrels for CRUDEOIL, grams for GOLD, etc.)
    lot_size_hint: Optional[int] = None
    # Intent hint — "open" (new position) vs "close" (reduce existing).
    # The 5-lot fat-finger cap (G2) is BYPASSED when intent == "close"
    # because closing an existing 20-lot position with a single 20-lot
    # SELL order is legitimate. The G1 multiple-of-lot check STILL fires
    # (qty must always be a valid multiple). Operator 2026-07-01: "for
    # closing an existing order, qty should not be an issue. the guard
    # should not apply for closing position." None / "open" (default) →
    # both guards apply. Frontend passes "close" when the ticket is
    # opened from a close-position context.
    intent: Optional[str] = None


class TicketOrderResponse(msgspec.Struct):
    order_id: str
    mode: str
    status: str
    detail: str
    # When a template was attached, the resolved plan + any placed GTT
    # ids / wing order id flow back here so the UI can show "TP @ ₹X
    # placed (gtt #42), Wing @ ₹Y placed (#43)" in the success line.
    # None when no template was attached.
    template_attachment: Optional[dict] = None


class TicketPreviewRequest(msgspec.Struct):
    """Same shape as TicketOrderRequest — what would happen if this
    were submitted. No side effects. Returns the resolved TemplatePlan
    so the OrderTicket can show "Will place TP @ ₹X · SL @ ₹Y · Wing
    -500CE" inline before the operator hits Submit.

    `quantity` follows the same v2 convention as TicketOrderRequest:
    LOTS for F&O exchanges (NFO/MCX/CDS/BFO/BCD/NCO), raw shares for
    equity. Backend converts to contracts internally before feeding
    the template resolver."""
    mode: str
    side: str
    tradingsymbol: str
    quantity: int           # LOTS for F&O; raw shares for equity
    exchange: str = "NFO"
    product: str = "NRML"
    account: str = ""
    # The preview needs a reference price — UI passes the current LTP
    # or the operator's typed limit. Defaults to 0 (preview returns
    # only the structural plan; numeric trigger values come back as 0).
    reference_price: float = 0.0
    template_id:                  Optional[int]   = None
    tp_pct_override:              Optional[float] = None
    sl_pct_override:              Optional[float] = None
    wing_premium_pct_override:    Optional[float] = None
    wing_strike_offset_override:  Optional[int]   = None
    # Backward compat — legacy target_pct (fractional) also accepted
    # here so OrderTicket can preview without separating the two paths.
    target_pct:                   Optional[float] = None


class TicketPreviewResponse(msgspec.Struct):
    plan: dict     # TemplatePlan.to_dict()


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


class ReconcileSingleRequest(msgspec.Struct):
    """Body for per-card reconcile: which account holds the broker
    order so the route knows which Kite handle to query."""
    account: str


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

class AccountInfo(msgspec.Struct):
    account_id: str
    display: str


class AccountsResponse(msgspec.Struct):
    accounts: list[AccountInfo]
    # Operator-configured default broker account (orders.default_account).
    # Empty string means "no default — auto-pick when only one account, else
    # leave it to the operator". Frontend SymbolPanel reads this on mount
    # to pre-select the Account dropdown.
    default_account: str = ""
    # Operator-configured default symbol (orders.default_symbol). May be
    # an underlying name (NIFTY / CRUDEOIL / GOLD) which the modal
    # resolves into a tradeable contract via the instruments cache, or a
    # full tradeable symbol. Empty = open modal without a pre-filled
    # symbol.
    default_symbol: str = ""
