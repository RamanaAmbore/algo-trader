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
    # Per-row price source tag:
    #   • "live"                 — exchange open, broker/ticker LTP
    #   • "snapshot_settled"     — exchange closed and settled window passed
    #   • "snapshot_unsettled"   — exchange closed, pre-settled window
    #   • "pending_open"         — broker-connectivity window open but this
    #                              row's own exchange SEGMENT hasn't opened
    #                              yet (Item 1, 2026-10-22); last_price is
    #                              pinned to prev_close, day_change(_val/
    #                              _percentage) == 0.
    # Legacy value "snapshot" is still accepted by consumers — treat it as
    # equivalent to "snapshot_settled".
    price_source: str = "live"
    # Unified-model alias for last_price. Populated alongside last_price
    # in the route overlay so consumers can migrate off the LTP-specific
    # name gradually. Zero here means "unset" (frontend falls back to
    # last_price for backward compat).
    current_price: float = 0.0
    # Whether cells on this row should render tick-flash / freshness
    # shimmer animations. True while the row's exchange is currently
    # open; False on closed-exchange snapshot rows so cells render
    # static (no green/red pulse on the frozen prev_close). Frontend's
    # tick-flash cellClass reads this to gate the flash primitive.
    is_animating: bool = True
    # Frozen prior-session settlement price (from daily_book.ltp —
    # never overwritten once set). Used by the frontend as the reference
    # close for holdings day P&L so the formula `(ltp − prev_close) × qty`
    # stays correct even when Kite's broker close has drifted post-settlement.
    # Zero means "not yet available" (cold boot / first deploy).
    prev_close: float = 0.0
    # P&L per share = total pnl / quantity. Useful for per-unit return
    # display without exposing quantity. Zero when quantity is 0 (e.g.
    # fully-sold holdings still in the snapshot).
    pnl_per_share: float = 0.0


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
    # Entry lot count for a CLOSED (quantity == 0) F&O position — Kite
    # zeroes the lot count along with average_price once net quantity
    # hits zero, so the frontend cannot derive it from quantity/lot_size
    # any more (2026-11-05 live fix, operator: "lots and qty for close
    # positions in derivatives legs grid is zero"). None for every other
    # row — frontend fast path (lotsForRow.js) treats `lots != null` as
    # "trust this value," so a real 0 here would wrongly short-circuit
    # its own qty/lot_size derivation for a genuinely-open position.
    lots: Optional[int] = None
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
    # Per-row price source tag:
    #   • "live"                 — exchange open, broker/ticker LTP
    #   • "snapshot_settled"     — exchange closed and settled window passed
    #   • "snapshot_unsettled"   — exchange closed, pre-settled window
    #   • "pending_open"         — broker-connectivity window open but this
    #                              row's own exchange SEGMENT hasn't opened
    #                              yet (Item 1, 2026-10-22); last_price is
    #                              pinned to prev_close, day_change(_val/
    #                              _percentage) == 0.
    # Legacy value "snapshot" is still accepted by consumers — treat it as
    # equivalent to "snapshot_settled".
    price_source: str = "live"
    # Unified-model alias for last_price. Populated alongside last_price
    # in the route overlay so consumers can migrate off the LTP-specific
    # name gradually. Zero here means "unset" (frontend falls back to
    # last_price for backward compat).
    current_price: float = 0.0
    # Whether cells on this row should render tick-flash / freshness
    # shimmer animations. True while the row's exchange is currently
    # open; False on closed-exchange snapshot rows so cells render
    # static (no green/red pulse on the frozen prev_close). Frontend's
    # tick-flash cellClass reads this to gate the flash primitive.
    is_animating: bool = True
    # True when no AlgoOrder with status='OPEN' matches this position's
    # (account, tradingsymbol). Orphan positions have no active chase in
    # flight — surfaced in the UI so the operator can decide whether to
    # create an order or close the position manually.
    is_orphan: bool = False
    # Shared key for positions linked via an AlgoOrder parent-child
    # relationship. Set to the ROOT parent AlgoOrder id (as string) for
    # both parent and child positions; None when no AlgoOrder matches.
    # Lets the frontend group paired legs under the same visual card.
    pair_group_key: Optional[str] = None
    # Lots matched into a pair by the lot-waterfall auto-pairing algorithm.
    # 0 for unmatched (orphan) rows and quantity==0 rows.
    paired_qty: int = 0
    # Unmatched lots on this position: abs(quantity) - paired_qty.
    # 0 for fully-paired rows and quantity==0 rows.
    orphan_qty: int = 0
    # True when an OPEN AlgoOrder with a gtt_order_id exists for this
    # (account, tradingsymbol). Lets the frontend surface a GTT chip on
    # position rows that already have an active GTT attached, so the
    # operator doesn't accidentally create a duplicate.
    has_gtt: bool = False
    # Frozen prior-session settlement price (from daily_book.ltp —
    # never overwritten once set). Mirrors HoldingRow.prev_close.
    # Used by the frontend as the reference close for positions day P&L
    # so the formula `(ltp − prev_close) × qty` stays correct even
    # when the broker's close has drifted post-settlement (W3 window).
    # Zero means "not yet available" (cold boot / first deploy).
    prev_close: float = 0.0


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


class PositionsSymbolSummaryRow(msgspec.Struct):
    """Symbol-level (across accounts) rollup — parallel to PositionsSummaryRow,
    grouped by tradingsymbol instead of account. Per the Day P&L / Exp P&L
    redesign, per-position Day P&L is no longer displayed anywhere; only
    account-level (`summary`) and symbol-level (`symbol_summary`) rollups,
    plus grand totals, are shown."""
    tradingsymbol: str
    pnl: float
    day_change_val: float = 0.0
    day_change_percentage: float = 0.0
    # Σ|close × qty| across accounts for this symbol — denominator for
    # day_change_percentage, same convention as PositionsSummaryRow.day_prev_val.
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
    # Symbol-level (across accounts) rollup — parallel to `summary`.
    # See PositionsSymbolSummaryRow docstring.
    symbol_summary: list[PositionsSymbolSummaryRow] = []


# ---------------------------------------------------------------------------
# Funds
# ---------------------------------------------------------------------------

class FundsRow(msgspec.Struct):
    # cash/avail_margin/used_margin/collateral/live_cash/option_premium
    # are `float | None` per the missing-vs-zero convention (see
    # CLAUDE.md "Alert evaluation and latching" and
    # backend/tests/broker/test_funds_missing_vs_zero.py): a broker
    # adapter (Dhan/Groww) surfaces `None` — not a coerced 0.0 — when
    # the underlying field is genuinely absent/unmapped, so only a
    # broker-confirmed real 0 renders as an actual zero downstream.
    # funds.py's _fetch() preserves that null through to this Struct
    # instead of blanket-filling every numeric column with 0.
    account: str
    cash: float | None           # avail opening_balance — start-of-day cash
    avail_margin: float | None   # net — what's left for trading after used_margin
    used_margin: float | None    # util debits
    collateral: float | None     # avail collateral
    # Defaults are 0 — older Kite responses without `avail.cash` /
    # `util.option_premium` (or any broker adapter that doesn't
    # surface them) fall through cleanly instead of raising a
    # missing-key construction error in the route's FundsRow(**r)
    # builder.
    live_cash:        float | None = 0.0  # avail cash (direct funds only, NOT avail.live_balance —
                                    # Kite's live_balance also includes collateral) — decreases
                                    # on option premium debit
    option_premium:   float | None = 0.0  # util option_premium — net cash spent on currently-held long options
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
    # Staleness-freeze indicator (2026-09 shared dev/prod generation design
    # — see backend/api/helpers/dev_content_proxy.py). Reuses the same
    # source-tag vocabulary `closed_hours_or_broker()` already returns for
    # positions/holdings (CLAUDE.md "Closed-hours route gate"): 'live' =
    # freshly generated (prod) or freshly proxied (dev); 'snapshot-fallback'
    # = serving a frozen last-known-good copy after a failed proxy fetch to
    # prod, or dev's own historical DB row when even that doesn't exist.
    # Prod's own direct (non-proxied) responses are always 'live'/False —
    # this route has no market-hours-driven "deliberate snapshot" state, so
    # the tri-state's middle value ('snapshot') is not reachable here.
    stale: bool = False
    source: str = "live"


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
    # Same staleness-freeze indicator as MarketResponse — see its
    # docstring. NOT to be confused with NewsItem.source above (a
    # headline's publisher domain, e.g. "moneycontrol.com") — this is a
    # top-level field on the response envelope itself.
    stale: bool = False
    source: str = "live"


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
    # Sprint 17 — token-level (sibling of params_schema) declared
    # mutual-exclusion groups, e.g. [["wing_premium_pct_override",
    # "wing_strike_offset_override"]]. Populated only for system action
    # tokens, sourced live from backend.api.algo.grammar.get_action_mutex_groups
    # (the YAML catalog, not the grammar_tokens DB row — that table has no
    # column for this key). None when the action declares no mutex groups
    # or for any non-action/non-system token.
    mutually_exclusive_groups: list | None = None
    # Sprint 10 (amended Sprint D2) — set True only for a merged-in
    # `kind='template'` Agent ("Token"/"Template" in operator-facing
    # text) row representing a saved condition-subtree or
    # notify-channel-list $ref. Lets a caller distinguish "this is a real
    # metric/channel/action token" from "this is a reference to a saved
    # fragment" once both are merged into the same catalog response — a
    # bare use of this token should compile to {"$ref": <token>} rather
    # than a normal leaf/channel object. Never True for a real GrammarToken
    # row.
    is_fragment_ref: bool = False
    # Sprint 15 — operator-facing display text for this token's concept,
    # kept in sync with whatever label the UI already shows for the SAME
    # concept elsewhere (Funds grid, Legs/Snapshot grid, NavStrip, ...).
    # Populated today ONLY for `token_kind='column'` rows (the per-scope
    # exposed-column registry projection — see
    # backend.api.algo.grammar.EXPOSED_COLUMNS / routes/grammar.py's
    # `_exposed_column_catalog_rows`); None for every other token kind.
    # Sprint 11's existing hover/focus description mechanism can read this
    # directly instead of parsing it back out of `description`'s prose.
    label: str | None = None


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


# `OrderTemplateOut`/`OrderTemplateCreate`/`OrderTemplatePatch` removed
# (Bracket/Template-GTT removal, T3.2, 2026-11) — these were the
# `OrderTemplateController` route schemas, deleted along with
# `backend/api/routes/templates.py` in the prior sub-sprint (T3.1).
# Confirmed zero remaining importers (grep: only comment references).
# The underlying `OrderTemplate` DB model stays on `backend/api/models.py`
# — its own removal is a later sub-sprint.

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
    # `template_id` removed (Bracket/Template-GTT removal, T3.6, 2026-11)
    # — the frontend stopped sending it in T3.3, and `orders_basket.py`
    # never read it even while it was still accepted.
    # Per-leg chase + aggressiveness used by the basket-placement route.
    chase: bool = True
    chase_aggressiveness: str = "low"
    # Per-leg take-profit override fields (legacy single-leg path
    # carries these too).
    target_pct: Optional[float] = None
    target_abs: Optional[float] = None
    # `tp_pct_override`/`sl_pct_override`/`wing_premium_pct_override`/
    # `wing_strike_offset_override`/`sl_trail_pct_override`/
    # `tp_scales_json_override`/`hold_template_exit` removed (Bracket/
    # Template-GTT removal, T3.2, 2026-11) — the template-attach pipeline
    # they fed (`backend/api/algo/template_attach.py`) was deleted in the
    # prior sub-sprint (T3.1), and their one remaining reader
    # (`orders_helpers._ticket_overrides_dict`/`_build_overrides_json`)
    # was removed in this one.
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
    # 2026-11-05 fix (CRITICAL money-safety audit finding) — the client's
    # own resolved execution mode (SymbolPanel's `_resolveBasketMode()`),
    # same convention as `TicketOrderRequest.mode`. This field never
    # existed before, so msgspec silently dropped whatever mode the
    # client sent — `orders_basket.py:basket_order_handler`'s `eff_mode`
    # was computed ENTIRELY from server-side settings
    # (execution.paper_trading_mode / execution.shadow_mode / branch),
    # with no way to see that the navbar was actually in SIM or REPLAY.
    # A Chain basket submitted while the navbar showed SIM placed real
    # LIVE broker orders on prod. See `basket_order_handler`'s own
    # comment for how this is consumed — unlike `/ticket` (which trusts
    # `data.mode == "live"` directly), a non-"live" value here can only
    # ever ADD a reason to go paper, never override the server's own
    # force-to-paper conditions, and never escalate to live.
    mode: Optional[str] = None


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
    # Feeds the legacy, always-on `_arm_take_profit()` single-target TP
    # shim directly (persisted straight to `AlgoOrder.target_pct`) — the
    # `tp_pct_override`/template-attach pipeline it used to feed into was
    # removed (Bracket/Template-GTT removal, T3.1/T3.6, 2026-11).
    target_pct: Optional[float] = None
    # `template_id` removed (Bracket/Template-GTT removal, T3.6, 2026-11)
    # — the still-unmodified-at-the-time frontend sent it on every ticket
    # submission through T3.2, but it was already silently dropped before
    # persist and never acted on (see `orders_place.py:
    # _ticket_persist_live_algo_order`'s own comment); the frontend
    # stopped sending it in T3.3. `tp_pct_override`/`sl_pct_override`/
    # `wing_premium_pct_override`/`wing_strike_offset_override`/
    # `tp_order_type_override`/`wing_max_spread_pct_override`/
    # `sl_trail_pct_override`/`tp_scales_json_override` removed — the
    # template-attach pipeline they fed (`backend/api/algo/
    # template_attach.py`) was deleted in the prior sub-sprint (T3.1), and
    # their one remaining reader (`orders_helpers._ticket_overrides_dict`/
    # `_build_overrides_json`) was removed in this one.
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
    # Draft cleanup (2026-10) — when this ticket originated from a
    # server-persisted draft (see DraftOrderRequest / mode='draft' AlgoOrder
    # rows, backend.api.routes.orders), the originating draft row's id.
    # On CONFIRMED success of this /ticket submission (paper or live — see
    # ticket_order_handler), the draft row is deleted server-side. On any
    # failure the draft is left untouched — the backend is the single
    # source of truth for "did this draft actually get placed", fixing the
    # earlier client-side bug where the draft was removed before the
    # placement result was known. None/omitted when the ticket wasn't
    # sourced from a draft.
    draft_id: Optional[int] = None
    # MCP audit linkage (Sprint — MCP template/audit gaps). Set only by
    # backend.api.routes.lab._res_make_place_ticket, carrying the
    # mcp_audit.request_id minted for this call so the created AlgoOrder
    # row can be joined back to the MCP audit trail that created it.
    # None/omitted for every non-MCP caller (ticket/basket/chain/command).
    mcp_request_id: Optional[str] = None


class DraftOrderRequest(msgspec.Struct):
    """POST /api/orders/drafts — create a client-visible draft as a real
    AlgoOrder row (mode='draft', status='OPEN'). No broker call is ever
    made for a draft; it exists purely so the OrderTicket "Add to Payoff"
    flow survives a page refresh / is visible across devices.

    `quantity` convention — DIFFERENT from TicketOrderRequest.quantity:
    this is the CONTRACTS-equivalent figure (same unit AlgoOrder.quantity
    stores everywhere downstream, and the same unit
    frontend/src/lib/data/payoffDrafts.svelte.js's internal `qty` already
    uses — already resolved from lots × lot_size client-side before
    reaching here). NOT lots. Sending a raw lots figure here would
    silently under/over-represent the draft's real size.
    """
    symbol: str
    transaction_type: str           # "BUY" | "SELL"
    quantity: int                   # contracts-equivalent, > 0
    exchange: str = "NFO"
    price: Optional[float] = None
    # None/omitted → stored as "" (AlgoOrder.account is NOT NULL in the DB).
    # "" means "no account assigned yet" in every response too.
    account: Optional[str] = None


class DraftOrderPatchRequest(msgspec.Struct):
    """PATCH /api/orders/drafts/{id}. Every field is optional; an omitted
    (None) field is left UNCHANGED — same convention as ModifyOrderRequest.
    There is no way to explicitly clear a field back to null via this
    endpoint (matches ModifyOrderRequest's existing limitation)."""
    symbol: Optional[str] = None
    exchange: Optional[str] = None
    transaction_type: Optional[str] = None
    quantity: Optional[int] = None
    price: Optional[float] = None
    account: Optional[str] = None


class DraftOrderCreateResponse(msgspec.Struct):
    id: int


class TicketOrderResponse(msgspec.Struct):
    order_id: str
    mode: str
    status: str
    detail: str
    # `template_attachment` (the resolved Bracket/Template plan + any
    # placed GTT/wing order ids) removed 2026-11 (Track 3, T3.6,
    # Bracket/Template/GTT removal) — nothing constructed it since T3.1.


# `TicketPreviewRequest`/`TicketPreviewResponse` removed (Bracket/
# Template-GTT removal, T3.6, 2026-11) along with the `/orders/ticket/
# preview` route itself (`orders.py:ticket_preview`) — the template
# resolver they fed was stubbed to an empty plan since T3.1, and the
# frontend's own caller was removed in T3.3/T3.4 (confirmed zero
# remaining callers via grep). Nothing else constructed either struct.


class SpreadCheckResponse(msgspec.Struct):
    """GET /api/orders/spread-check response — one tradingsymbol+exchange's
    bid-ask spread% vs an (optional) threshold. See
    backend.api.algo.spread_check.SpreadCheckResult for field semantics;
    this mirrors it 1:1 plus the resolved threshold + its source."""
    ok:               bool
    tradingsymbol:    str
    exchange:         str
    threshold_pct:    float
    threshold_source: str              # "override" | "template" | "setting"
    status:           str              # "ok" | "wide" | "no_quote" | "error"
    spread_pct:       Optional[float] = None
    bid:              Optional[float] = None
    ask:              Optional[float] = None
    basis:            Optional[str]   = None   # "ltp" | "mid" | None
    reason:           Optional[str]   = None


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
    # True when the broker quantity could not be converted from lots (lot size not loaded).
    qty_unverified: bool = False


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


class CancelAllOrdersRequest(msgspec.Struct):
    """Body for `POST /api/orders/cancel-all`. Mirrors the
    `cancel_all_orders` agent-action params_schema (agent_grammar.yaml)
    field-for-field — `scope="account"` without `account` is refused by
    the shared helper (`actions_live.cancel_all_orders_scoped`).

    2026-10-31 — `role`/`user`: scope='role' narrows to every trader-role
    user's own assigned accounts (role must be 'trader' — every other
    role is firm-wide and has no well-defined per-user account list);
    scope='user' narrows to one trader-role user's own assigned accounts.
    """
    scope: "str | None" = None
    account: "str | None" = None
    symbol: "str | None" = None
    side: "str | None" = None
    role: "str | None" = None
    user: "str | None" = None


class CancelAllOrdersResponse(msgspec.Struct):
    cancelled: int
    errors: int
    scope: str
    account: "str | None" = None
    symbol: "str | None" = None
    side: "str | None" = None
    role: "str | None" = None
    user: "str | None" = None
    detail: str = "Cancel-all requested"


class ReconcileSingleRequest(msgspec.Struct):
    """Body for per-card reconcile: which account holds the broker
    order so the route knows which Kite handle to query."""
    account: str


# ---------------------------------------------------------------------------
# Standalone broker GTTs (Good-Till-Triggered) — distinct from the per-order
# `attached_gtts_json` exit-GTT concept used by OrderCard.svelte. This is a
# listable/countable feed of every GTT resting at the broker across accounts.
# ---------------------------------------------------------------------------

class GttRow(msgspec.Struct):
    """One broker GTT, normalised across Kite/Dhan/Groww shapes + tagged
    with the account it belongs to. `orders` is the raw per-leg dict list
    passed through unnormalised — shape varies by broker and the UI only
    needs it for display, not for re-placement."""
    gtt_id: str
    account: str
    broker_id: str
    status: str
    trigger_type: str
    tradingsymbol: str
    exchange: str
    trigger_values: list[float]
    last_price: float
    orders: list[dict]
    created_at: str


class GttListResponse(msgspec.Struct):
    gtts: list[GttRow]
    count: int


class GttCancelResponse(msgspec.Struct):
    gtt_id: str
    detail: str = "GTT cancelled successfully"


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
