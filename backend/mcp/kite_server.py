"""
RamboQuant MCP server — read-only research tools over stdio.

Launched by Claude Code from `.mcp.json`. Talks to a running RamboQuant
API (default: https://dev.ramboq.com) via HTTPS using the operator's
JWT supplied through `RAMBOQ_TOKEN`. No genai is invoked from this
process — Claude Code is the LLM, this is the data pipe.

Environment:
    RAMBOQ_BASE   — API base URL (default: https://dev.ramboq.com)
    RAMBOQ_TOKEN  — JWT from POST /api/auth/login (required for any tool)

Tools (Phase 1, read-only):
    get_positions, get_holdings, get_quote, get_ohlcv,
    get_recent_news, get_option_analytics, list_agents,
    save_research_thread, get_research_thread

Run:
    python -m backend.mcp.kite_server
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP


_BASE  = (os.environ.get("RAMBOQ_BASE") or "https://dev.ramboq.com").rstrip("/")
_TOKEN = os.environ.get("RAMBOQ_TOKEN") or ""
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _headers() -> dict[str, str]:
    h = {"Accept": "application/json"}
    if _TOKEN:
        h["Authorization"] = f"Bearer {_TOKEN}"
    return h


async def _get(path: str, params: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_BASE}{path}", headers=_headers(), params=params or {})
        r.raise_for_status()
        return r.json()


async def _post(path: str, body: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_BASE}{path}", headers=_headers(), json=body)
        r.raise_for_status()
        return r.json()


app = FastMCP("ramboq-research")


# ── Market-data tools ─────────────────────────────────────────────────

@app.tool()
async def get_positions(account: str | None = None) -> dict:
    """Current intraday positions across all loaded broker accounts.

    Args:
        account: Optional account filter (e.g. ZG0790). When omitted,
            returns positions from every loaded account.

    Returns:
        dict with `positions` list — each row carries tradingsymbol,
        exchange, quantity, average_price, last_price, pnl, account.
    """
    rows = await _get("/api/positions/")
    if account:
        rows = [r for r in (rows or []) if (r.get("account") or "").upper() == account.upper()]
    return {"positions": rows, "count": len(rows or [])}


@app.tool()
async def get_holdings(account: str | None = None) -> dict:
    """Current long-term holdings across all loaded broker accounts.

    Args:
        account: Optional account filter (e.g. ZG0790).

    Returns:
        dict with `holdings` list — each row carries tradingsymbol,
        exchange, quantity, average_price, last_price, pnl, account.
    """
    rows = await _get("/api/holdings/")
    if account:
        rows = [r for r in (rows or []) if (r.get("account") or "").upper() == account.upper()]
    return {"holdings": rows, "count": len(rows or [])}


@app.tool()
async def get_quote(symbols: list[str]) -> dict:
    """Live quote (LTP + day OHLC + change%) for one or more symbols.

    Args:
        symbols: List of `EXCHANGE:TRADINGSYMBOL` keys, e.g.
            ["NSE:RELIANCE", "NSE:NIFTY 50", "NFO:NIFTY25APRFUT"].
            Max 300 per call.

    Returns:
        dict with `items` list. Each row: {exchange, tradingsymbol,
        ltp, close, change_pct, ohlc{open,high,low,close}}.
    """
    if not symbols:
        return {"items": [], "count": 0}
    body = {"keys": list(symbols)[:300]}
    res = await _post("/api/quote/batch", body)
    return {"items": res.get("items", []), "refreshed_at": res.get("refreshed_at"), "count": len(res.get("items", []))}


@app.tool()
async def get_ohlcv(symbol: str, days: int = 30, exchange: str = "NSE") -> dict:
    """Historical daily OHLCV candles for a symbol via Kite's
    historical-data endpoint.

    Args:
        symbol: Tradingsymbol (e.g. RELIANCE, NIFTY25APRFUT, RELIANCE2542428000CE).
        days: Lookback window in trading days (default 30, max 365).
        exchange: NSE / BSE / NFO / BFO / MCX / CDS (default NSE).

    Returns:
        dict with `bars` list of {date, open, high, low, close, volume}.
    """
    days = max(1, min(int(days or 30), 365))
    res = await _get("/api/options/historical", {
        "symbol":   symbol,
        "days":     days,
        "interval": "day",
        "exchange": exchange,
    })
    return {"symbol": symbol, "bars": res.get("bars", []), "count": len(res.get("bars", []))}


@app.tool()
async def get_recent_news(symbol: str | None = None, hours: int = 24, sentiment: bool = True) -> dict:
    """Recent Indian-market headlines from the news feed.

    Args:
        symbol: Optional case-insensitive substring filter on title
            (e.g. "RELIANCE" returns headlines that mention it).
        hours: Lookback window (default 24, max 168).
        sentiment: When True (default), each headline gets a bull /
            bear / neutral tag via Gemini Flash. Tags are server-side
            cached for 10 min so back-to-back research calls share the
            LLM round-trip — stays comfortably under the free-tier
            limit. Pass False to skip scoring entirely (raw feed only).

    Returns:
        dict with `items` list of {title, source, link, timestamp,
        sentiment}. sentiment is null when scoring was disabled.
    """
    params: dict[str, Any] = {}
    if sentiment:
        params["sentiment"] = "true"
    res = await _get("/api/news/", params)
    items = res.get("items", []) if isinstance(res, dict) else []
    if symbol:
        s = symbol.upper()
        items = [it for it in items if s in (it.get("title") or "").upper()]
    return {
        "items":        items,
        "count":        len(items),
        "refreshed_at": (res or {}).get("refreshed_at"),
        "scored":       sentiment,
    }


@app.tool()
async def get_option_analytics(symbol: str, qty: int = 0, account: str | None = None) -> dict:
    """Greeks + payoff curve + risk metrics for a single-leg option.

    Args:
        symbol: Option tradingsymbol (e.g. NIFTY25APR22000CE).
        qty: Signed position size (positive = long, negative = short, 0 = analytical only).
        account: Optional account scoping for sourcing avg_cost from your book.

    Returns:
        Full analytics bundle — greeks{delta,gamma,theta,vega,rho},
        pricing{spot,ltp,iv,T_years}, risk{max_profit,max_loss,
        breakeven,pop,ev,rr_ratio}, payoff[{spot,today_value,expiry_value}].
    """
    params: dict[str, Any] = {"mode": "live" if not account else "live", "symbol": symbol}
    if qty:
        params["qty"] = qty
    if account:
        params["account"] = account
    return await _get("/api/options/analytics", params)


@app.tool()
async def get_options_chain_snapshot(
    underlying: str,
    expiry: str,
    atm_window: int = 10,
) -> dict:
    """One-round-trip option chain with Greeks + LTP + bid/ask + IV
    for every strike within ±`atm_window` of ATM. Use this BEFORE
    calling get_option_analytics — a 5-strike iron-condor analysis
    that previously needed 5 separate get_option_analytics round-trips
    is now one call.

    Returns:
        dict with `underlying`, `expiry`, `spot`, `atm_strike`,
        `days_to_expiry`, `risk_free_rate`, `rows` list. Each row is
        {k: strike, atm_distance: signed-distance-from-spot,
         ce: {ltp, bid, ask, iv, delta, gamma, theta, vega, rho},
         pe: {same shape}}.
        Theta is per-day; vega/rho per 1% change.

    Args:
        underlying: NIFTY, BANKNIFTY, RELIANCE, etc. (case-insensitive).
        expiry:     ISO date YYYY-MM-DD (e.g. "2025-04-24").
        atm_window: ±N strikes around the ATM strike. Default 10 (so
                    21 strikes total when chain is dense). Cap 30.

    Hint: when iv is null for a leg, the LTP wasn't available, so the
    Greeks fall back to DEFAULT_IV (15%). Weight those legs lower in
    your reasoning vs strikes where iv is populated.
    """
    params: dict[str, Any] = {
        "underlying": underlying,
        "expiry":     expiry,
        "atm_window": int(atm_window),
    }
    return await _get("/api/options/chain-snapshot", params)


@app.tool()
async def get_economic_snapshot() -> dict:
    """India macro context — RBI repo rate, CPI inflation, IIP, GDP
    growth, USD/INR spot. Each metric carries its as_of date + an
    age_days + a `stale` flag (true when older than the metric's
    natural release cadence — e.g. CPI > 45 days, IIP > 60 days,
    repo > 120 days). Use the stale flag to apply a freshness
    discount in your reasoning; don't take a stale CPI as a fresh
    inflation read.

    Data source: hand-maintained in backend/config/backend_config.yaml
    under `macros:` — operator updates it monthly after MoSPI / RBI
    releases. No external API call, ₹0 incremental cost, zero
    failure modes. Missing entries return as null.

    Returns:
        dict {repo_rate, cpi, iip, gdp_growth, inr_usd, refreshed_at}.
        Each metric (when present): {value, as_of, age_days, stale, label}.
    """
    return await _get("/api/economic/snapshot")


@app.tool()
async def get_funds_summary(account: str | None = None) -> dict:
    """Cash + margin snapshot per broker account. Use BEFORE proposing
    a trade to confirm the operator can actually fund it — Kite rejects
    place_order with cryptic 'insufficient funds' messages, much better
    to size legs against this snapshot up front.

    Args:
        account: Optional account filter (e.g. ZG0790). Omit for all.

    Returns:
        dict with `rows` list. Each row: {account, segment, cash,
        available_margin, used_margin, opening_balance, …}.
    """
    res = await _get("/api/funds/")
    rows = (res or {}).get("rows") or []
    if account:
        a = account.upper().strip()
        rows = [r for r in rows if (r.get("account") or "").upper() == a]
    return {
        "rows":         rows,
        "count":        len(rows),
        "refreshed_at": (res or {}).get("refreshed_at"),
    }


@app.tool()
async def get_watchlist(name: str) -> dict:
    """Look up one of the operator's curated watchlists by name and
    return its symbol list. Useful for scoping research to a known set —
    "do a relative-value scan across my IT watchlist" beats hand-listing
    every TCS / INFY / WIPRO / HCL by name in the prompt.

    Matches case-insensitive on watchlist name. Returns 404-style empty
    payload (with a clear note) if no watchlist with that name exists.

    Args:
        name: Watchlist name (e.g. 'Default', 'IT', 'Banking').

    Returns:
        dict with {id, name, items: [{exchange, tradingsymbol, ...}],
        item_count}. When no match: {error, available_names: [...]}.
    """
    target = (name or "").strip().lower()
    if not target:
        return {"error": "name is required", "items": []}
    # 1. List all → find by name (case-insensitive).
    wls = await _get("/api/watchlist/")
    match = next((w for w in (wls or []) if (w.get("name") or "").strip().lower() == target), None)
    if not match:
        return {
            "error":           f"No watchlist named {name!r}",
            "available_names": [w.get("name") for w in (wls or []) if w.get("name")],
            "items":           [],
        }
    # 2. Fetch full detail.
    full = await _get(f"/api/watchlist/{int(match['id'])}")
    items = (full or {}).get("items") or []
    return {
        "id":         full.get("id"),
        "name":       full.get("name"),
        "items":      items,
        "item_count": len(items),
    }


@app.tool()
async def get_pnl_attribution(period: str = "today", mode: str = "all") -> dict:
    """P&L attribution grouped by the agent that generated each order.
    Use this to answer "which of my agents made money this week?" or
    "is my new loss-cut agent actually saving capital?".

    The PnL figure is a chase-slippage proxy — see the platform doc for
    the exact formula. Manual ticket orders (no agent_id) cluster under
    a synthetic '(Operator ticket)' row.

    Args:
        period: today / week / month / all (default today).
        mode:   live / paper / all (default all). Filter by AlgoOrder.mode.

    Returns:
        dict with `agents` list of {agent_slug, agent_name, orders,
        filled, gross_pnl, win_pct, avg_slippage}.
    """
    valid_periods = {"today", "week", "month", "all"}
    valid_modes = {"live", "paper", "all"}
    if period not in valid_periods:
        return {"error": f"period must be one of {sorted(valid_periods)}", "agents": []}
    if mode not in valid_modes:
        return {"error": f"mode must be one of {sorted(valid_modes)}", "agents": []}
    rows = await _get("/api/admin/pnl/by-agent", {"period": period, "mode": mode})
    return {"agents": rows or [], "count": len(rows or []), "period": period, "mode": mode}


@app.tool()
async def list_agents(status: str | None = None, limit: int = 200) -> dict:
    """List agent rows from the agents table. Useful for "show me my
    NIFTY-related agents" or finding the agent that just fired.

    Args:
        status: Optional filter — "active", "inactive", "paused".
        limit: Max rows to return (default 200).

    Returns:
        dict with `agents` list of {id, slug, name, description, status,
        scope, cooldown_minutes, last_triggered_at, trigger_count}.
    """
    res = await _get("/api/agents/")
    rows = res if isinstance(res, list) else (res.get("agents") or res.get("items") or [])
    if status:
        rows = [r for r in rows if (r.get("status") or "").lower() == status.lower()]
    return {"agents": rows[:limit], "count": len(rows)}


# ── Research-thread persistence ───────────────────────────────────────

@app.tool()
async def save_research_thread(
    symbol: str,
    title: str = "",
    thesis_text: str | None = None,
    confidence: str = "unsure",
    transcript: list | None = None,
) -> dict:
    """Persist the current research session to the RamboQuant DB so the
    Lab page (/admin/research) can show it later. Call this once you've
    synthesized a thesis the operator wants to keep.

    Args:
        symbol: The stock being researched (e.g. RELIANCE).
        title: Short label (auto-generated from symbol if blank).
        thesis_text: The synthesized thesis (markdown OK).
        confidence: bull / bear / neutral / unsure.
        transcript: Optional list of {role, content, ...} messages from
            this session. Stored opaque as JSONB.

    Returns:
        Created thread {id, symbol, title, confidence, created_at}.
    """
    body = {
        "symbol":      symbol,
        "title":       title,
        "thesis_text": thesis_text,
        "confidence":  confidence,
        "transcript":  transcript or [],
    }
    return await _post("/api/research/threads", body)


@app.tool()
async def get_research_thread(thread_id: int) -> dict:
    """Fetch a previously saved research thread by id — useful for
    "remind me what I thought about RELIANCE last week".

    Args:
        thread_id: The thread row id (visible in the Lab page sidebar).
    """
    return await _get(f"/api/research/threads/{int(thread_id)}")


@app.tool()
async def save_agent_draft(
    thread_id: int,
    name: str,
    conditions: dict,
    actions: list | None = None,
    events: list | None = None,
    scope: str = "total",
    schedule: str = "market_hours",
    cooldown_minutes: int = 30,
    description: str = "",
    lifespan_type: str = "persistent",
    lifespan_max_fires: int | None = None,
    lifespan_expires_at: str | None = None,
    debounce_minutes: int = 0,
    tags: list | None = None,
    blackout_windows: list | None = None,
) -> dict:
    """Promote the current research thread to an INACTIVE draft Agent.

    The agent always ships disabled (status='inactive') and paper-mode
    (trade_mode='paper') — operator's next step is "Run in Simulator"
    from /agents to validate before flipping it on. The MCP server
    cannot create an active or live agent; that's a deliberate safety
    rail matching how Composer.trade and IBKR TraderGPT gate LLM-driven
    strategy creation.

    Conditions must be a v2 grammar tree, e.g.:
        {"all": [
            {"metric": "pnl", "scope": "positions.any_acct",
             "op": "<=", "value": -30000}
        ]}

    Lifespan (Phase 19): use this for one-shot or time-bound agents,
    which is the natural fit for LLM-created trade-idea agents:

      lifespan_type='one_shot'       — completes after first fire.
                                       Best for "alert me once if my
                                       RELIANCE breakout happens"
                                       style ideas.
      lifespan_type='n_fires' + lifespan_max_fires=N
                                     — completes after N fires.
      lifespan_type='until_date' + lifespan_expires_at=ISO
                                     — completes when wall-clock crosses
                                       the date. Good for "watch this
                                       for the next 7 days then forget".
      lifespan_type='persistent'     — default. Never completes.

    The engine's run_cycle() flips status to 'completed' automatically
    once the limit is reached; the operator does not need to deactivate.

    Use list_agents() + the /api/admin/grammar/tokens endpoint (via the
    page) to discover available tokens before drafting.

    Args:
        thread_id:           Research thread ID (from save_research_thread).
        name:                Human-readable agent name (shown in /agents).
        conditions:          v2 condition tree (dict).
        actions:             Optional list of action descriptors.
        events:              Optional list of notify channels.
        scope:               'total' or 'per_account'.
        schedule:            'market_hours' (default) or 'always'.
        cooldown_minutes:    Re-fire gap (default 30).
        description:         Optional free-form.
        lifespan_type:       persistent / one_shot / n_fires / until_date.
        lifespan_max_fires:  required when lifespan_type='n_fires'.
        lifespan_expires_at: ISO datetime, required when lifespan_type='until_date'.
        debounce_minutes:    Phase 21 — "for N minutes" gate. 0 (default)
                             = fire immediately. N > 0 = condition must
                             hold N consecutive minutes before firing.
                             Use this to suppress spike-driven false
                             positives on noisy metrics. Industry pattern
                             (Datadog `For:`, Grafana `For:`, CloudWatch
                             `EvaluationPeriods`). Typical values: 2-5
                             for live alerts on twitchy series.

    Returns:
        Joined view {thread_id, symbol, agent_id, agent_slug,
        agent_name, agent_status='inactive', ...}.
    """
    body = {
        "name":                name,
        "conditions":          conditions,
        "actions":             actions or [],
        "events":              events or [],
        "scope":               scope,
        "schedule":            schedule,
        "cooldown_minutes":    int(cooldown_minutes or 30),
        "description":         description or "",
        "lifespan_type":       lifespan_type,
        "lifespan_max_fires":  lifespan_max_fires,
        "lifespan_expires_at": lifespan_expires_at,
        "debounce_minutes":    max(0, int(debounce_minutes or 0)),
        "tags":                tags or [],
        "blackout_windows":    blackout_windows or [],
    }
    return await _post(f"/api/research/threads/{int(thread_id)}/promote", body)


@app.tool()
async def place_order(
    confirm_token: str,
    account: str,
    tradingsymbol: str,
    side: str,
    quantity: int,
    mode: str = "paper",
    order_type: str = "LIMIT",
    price: float | None = None,
    trigger_price: float | None = None,
    exchange: str = "NFO",
    product: str = "NRML",
    variety: str = "regular",
    chase: bool = True,
    chase_aggressiveness: str = "low",
) -> dict:
    """Place an order via the operator's broker pipeline. REQUIRES a
    valid confirm_token minted by the operator from the Lab page
    (`POST /api/research/confirm-token`); you cannot generate one
    yourself.

    The token is single-use, expires in 60 seconds, and is bound to
    the EXACT order it was minted for. If account / tradingsymbol /
    side / quantity / mode / order_type / price / trigger_price
    don't match what the operator typed when minting, the call
    returns 403 — change nothing, ask the operator to re-mint.

    Mode resolution (server-side, cannot be overridden):
      - dev branch: forces paper regardless of `mode`.
      - prod + execution.paper_trading_mode=True: forces paper.
      - prod + execution.paper_trading_mode=False + mode='live':
        real Kite order via the existing chase engine.
    Default `mode='paper'` is intentional — the LLM should never
    ASK for live; the operator picks live by minting a token with
    mode='live'.

    Safety pattern matches Composer.trade's "Deploy" gate and IBKR
    TraderGPT's per-trade confirm. No LLM-initiated order has ever
    moved a rupee without an explicit operator confirm in this
    architecture.

    Args:
        confirm_token:        16-byte hex token from the Lab page.
        account:              Broker account code (e.g. ZG0790).
        tradingsymbol:        Kite tradingsymbol (e.g. NIFTY25APRFUT).
        side:                 BUY or SELL.
        quantity:             Positive integer.
        mode:                 paper (default) or live.
        order_type:           LIMIT (default) / MARKET / SL / SL-M.
        price:                Required for LIMIT / SL.
        trigger_price:        Required for SL / SL-M.
        exchange:             NFO (default) / NSE / BSE / MCX / CDS / BFO.
        product:              NRML (default) / MIS / CNC.
        variety:              regular (default) / amo / iceberg.
        chase:                True (default) → engine re-quotes the limit
                              each tick until filled or unfilled.
        chase_aggressiveness: low (default) / med / high.

    Returns:
        {order_id, mode, status, detail} from the underlying ticket
        pipeline. The mcp_audit table records every call.
    """
    body = {
        "confirm_token":        confirm_token,
        "account":              account,
        "tradingsymbol":        tradingsymbol,
        "side":                 side,
        "quantity":             int(quantity),
        "mode":                 mode,
        "order_type":           order_type,
        "price":                price,
        "trigger_price":        trigger_price,
        "exchange":             exchange,
        "product":              product,
        "variety":              variety,
        "chase":                chase,
        "chase_aggressiveness": chase_aggressiveness,
    }
    return await _post("/api/research/place-order", body)


@app.tool()
async def cancel_order(
    confirm_token: str,
    account: str,
    order_id: str,
    mode: str = "live",
    variety: str = "regular",
) -> dict:
    """Cancel a working order. REQUIRES a confirm token minted with
    kind='cancel' for THIS (account, order_id, mode). Token cannot be
    redeemed against a different order, mode, or account.

    Dispatch:
      - mode='live' (default) → Kite broker cancel
      - mode='paper'          → paper engine cancel; order_id must be
                                 the integer AlgoOrder.id of an OPEN
                                 paper order (look it up via the
                                 /api/orders/algo/recent?mode=paper
                                 endpoint or the LogPanel Order tab).

    Args:
        confirm_token: 32-char hex token from Lab page.
        account:       Broker account code (e.g. ZG0790).
        order_id:      Broker order_id (live) OR AlgoOrder.id (paper).
        mode:          live (default) or paper. Must match the token's
                       minted mode — cross-mode cancel returns 403.
        variety:       regular / amo / iceberg (default regular).

    Returns:
        {order_id, detail}. mcp_audit + Telegram ping land per call.
    """
    body = {
        "confirm_token": confirm_token,
        "account":       account,
        "order_id":      order_id,
        "mode":          mode,
        "variety":       variety,
    }
    return await _post("/api/research/cancel-order", body)


@app.tool()
async def modify_order(
    confirm_token: str,
    account: str,
    order_id: str,
    quantity: int = 0,
    order_type: str = "LIMIT",
    price: float | None = None,
    trigger_price: float | None = None,
    mode: str = "live",
    variety: str = "regular",
    validity: str | None = None,
) -> dict:
    """Modify a working order. REQUIRES a confirm token minted with
    kind='modify' for THIS (account, order_id, mode, quantity,
    order_type, price, trigger_price). The new values are part of
    the purpose hash — bait-and-switch on the new price / qty after
    the operator approves is blocked at the gate.

    Dispatch:
      - mode='live' (default) → Kite modify_order
      - mode='paper'          → paper engine modify_paper_order; next
                                 chase tick uses the new values

    Args:
        confirm_token: 32-char hex token from Lab page (kind='modify').
        account:       Broker account code.
        order_id:      Broker order_id (live) OR AlgoOrder.id (paper).
        quantity:      New quantity (0 = unchanged).
        order_type:    LIMIT / MARKET / SL / SL-M.
        price:         New limit price (LIMIT / SL).
        trigger_price: New trigger (SL / SL-M).
        mode:          live (default) or paper.
        variety:       regular / amo / iceberg.
        validity:      Optional broker validity tag (live only).

    Returns:
        {order_id, detail}.
    """
    body = {
        "confirm_token": confirm_token,
        "account":       account,
        "order_id":      order_id,
        "quantity":      int(quantity),
        "order_type":    order_type,
        "price":         price,
        "trigger_price": trigger_price,
        "mode":          mode,
        "variety":       variety,
        "validity":      validity,
    }
    return await _post("/api/research/modify-order", body)


@app.tool()
async def get_audit_recent(
    tool: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> dict:
    """Recent MCP-initiated actions (Phase 3+) — lets you verify your
    own work landed correctly without round-tripping to the operator.

    Each row carries the redacted args, the result_status
    (ok / denied / error), the result_summary, and the request_id —
    same data the Lab page's Audit tab shows. Token material is
    NEVER returned, even via this tool.

    Useful pattern after a place_order call:

        result = place_order(confirm_token=..., ...)
        # Verify with a sanity peek:
        recent = get_audit_recent(tool="place_order", limit=3)
        # → should show your call as the most recent 'ok' row

    Args:
        tool:   Optional filter — 'place_order', 'cancel_order',
                'modify_order'.
        status: Optional filter — 'ok' / 'denied' / 'error'.
        limit:  Max rows (default 50, max 1000).

    Returns:
        dict with `rows` list (reverse-chrono) of {id, tool, user_id,
        args_redacted, result_status, result_summary, request_id,
        created_at}.
    """
    params: dict[str, Any] = {"limit": max(1, min(int(limit or 50), 1000))}
    if tool:   params["tool"]   = tool
    if status: params["status"] = status
    rows = await _get("/api/research/audit", params)
    return {"rows": rows or [], "count": len(rows or [])}


@app.tool()
async def dry_run_agent(agent_slug: str) -> dict:
    """Phase 22 — evaluate this agent's condition tree against CURRENT
    live market state. Returns what would fire WITHOUT firing. No
    audit row, no Telegram ping, no action execution.

    Use BEFORE asking the operator to mint an activate token, so you
    can answer "this rule would fire 0 times right now" vs "this
    rule would fire 3 times immediately if activated" and adjust
    the thresholds accordingly.

    Industry analogue: Datadog 'Test Notifications', Grafana
    'Preview Alerts'.

    Args:
        agent_slug: The agent's slug (visible in /agents or in the
                    save_agent_draft response).

    Returns:
        dict with:
          agent_slug: str
          matches:     list[dict] of per-leaf match objects
          match_count: int (total matches, may exceed displayed)
          would_fire:  bool — true iff matches non-empty AND no gate
                       (schedule, cooldown, fire_at_time, blackout,
                       debounce) is currently blocking
          blocked_by:  str or null — name of the gate that's blocking
          evaluated_at: ISO timestamp
    """
    return await _post(f"/api/agents/{agent_slug}/dry-run", {})


@app.tool()
async def activate_agent(confirm_token: str, agent_slug: str) -> dict:
    """Flip an agent from inactive → active. REQUIRES a confirm token
    minted with kind='activate' for THIS exact agent_slug.

    Activation is the HIGHEST-STAKES write the LLM can request — once
    active, the agent fires automatically on every matching tick. The
    token gate makes this impossible without explicit per-call operator
    approval; you cannot mint the token yourself.

    Recommended pattern after promote → simulator review:
      1. save_agent_draft(...) → returns inactive Agent + thread link
      2. (operator: Run in Simulator on /agents, reviews fires)
      3. Ask operator to mint kind='activate' token for the slug
      4. activate_agent(confirm_token=..., agent_slug=...)
      5. Verify with get_audit_recent(tool='activate_agent', limit=3)

    Args:
        confirm_token: 32-char hex token (kind='activate').
        agent_slug:    The agent's slug (visible in /agents or the
                       save_agent_draft response).

    Returns:
        {agent_slug, status='active', detail}.
    """
    return await _post("/api/research/activate-agent", {
        "confirm_token": confirm_token,
        "agent_slug":    agent_slug,
    })


@app.tool()
async def deactivate_agent(confirm_token: str, agent_slug: str) -> dict:
    """Flip an agent from active → inactive. REQUIRES a confirm token
    minted with kind='deactivate' for THIS exact agent_slug. A
    deactivate token CANNOT be redeemed to activate (the action verb
    is part of the purpose hash).

    Lower-stakes than activate, but still gated — accidentally
    deactivating a critical risk agent (e.g. a loss-cut rule) at the
    wrong moment is its own kind of trouble.

    Args:
        confirm_token: 32-char hex token (kind='deactivate').
        agent_slug:    The agent's slug.

    Returns:
        {agent_slug, status='inactive', detail}.
    """
    return await _post("/api/research/deactivate-agent", {
        "confirm_token": confirm_token,
        "agent_slug":    agent_slug,
    })


@app.tool()
async def update_agent(
    confirm_token: str,
    agent_slug: str,
    proposed_changes: dict,
) -> dict:
    """Edit an existing Agent's condition tree / events / actions /
    scope / schedule / cooldown / fire_at_time / description.
    REQUIRES a confirm token minted with kind='update' for THIS
    agent_slug + THIS exact proposed_changes dict — the whole
    canonical-JSON of changes is part of the purpose hash.

    Only whitelisted fields are honoured server-side:
        conditions, events, actions, scope, schedule,
        cooldown_minutes, fire_at_time, description
    status / trade_mode / lifespan_* are silently dropped. The LLM
    cannot flip an agent active or live through update_agent — that's
    what activate_agent is for, and even there mode='live' requires
    the master execution.paper_trading_mode flag.

    Recommended pattern:
      1. Propose the full diff as a JSON dict to the operator.
      2. Operator pastes the same JSON into the Mint widget (kind='update'),
         clicks Mint, copies the token.
      3. Pass IDENTICAL proposed_changes + the confirm_token here.
      4. Verify with get_audit_recent(tool='update_agent').

    Use case: tighten a cooldown on a live loss-cut agent without
    deactivating it; widen a threshold during a known-volatile
    window; add a Telegram channel to an existing alert.

    For inactive drafts, delete + re-promote is usually cleaner.

    Args:
        confirm_token:    32-char hex token (kind='update').
        agent_slug:       The agent's slug.
        proposed_changes: dict of {field: new_value} — only
                          whitelisted keys count. Same JSON the
                          operator pasted at mint time, byte-for-byte.

    Returns:
        {agent_slug, status, detail}. status reflects the agent's
        CURRENT status (unchanged by update — only activate /
        deactivate change it).
    """
    return await _post("/api/research/update-agent", {
        "confirm_token":    confirm_token,
        "agent_slug":       agent_slug,
        "proposed_changes": proposed_changes or {},
    })


@app.tool()
async def list_research_threads(symbol: str | None = None, limit: int = 50) -> dict:
    """List recent research threads. Filter by symbol when revisiting
    a specific stock's history.

    Args:
        symbol: Optional symbol filter (case-insensitive).
        limit: Max rows (default 50, max 500).
    """
    params: dict[str, Any] = {"limit": max(1, min(int(limit or 50), 500))}
    if symbol:
        params["symbol"] = symbol.upper()
    rows = await _get("/api/research/threads", params)
    return {"threads": rows or [], "count": len(rows or [])}


# ── Server config introspection ───────────────────────────────────────

@app.tool()
async def get_server_info() -> dict:
    """Diagnostic — returns the RamboQuant base URL this MCP server is
    talking to + whether a JWT is configured. Use this if other tools
    are failing with 401 / connection errors to confirm setup."""
    return {
        "base_url":     _BASE,
        "has_token":    bool(_TOKEN),
        "token_prefix": (_TOKEN[:12] + "…") if _TOKEN else "",
    }


if __name__ == "__main__":
    app.run(transport="stdio")
