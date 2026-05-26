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

    Use list_agents() + the /api/admin/grammar/tokens endpoint (via the
    page) to discover available tokens before drafting.

    Args:
        thread_id:        ID of the research thread to promote (you got
                          this from save_research_thread's return).
        name:             Human-readable agent name (shown in /agents).
        conditions:       v2 condition tree (dict).
        actions:          Optional list of action descriptors. Default
                          empty list = alert-only.
        events:           Optional list of notify channels (telegram /
                          email / websocket / log).
        scope:            'total' or 'per_account'.
        schedule:         'market_hours' (default) or 'always'.
        cooldown_minutes: Re-fire gap (default 30).
        description:      Optional free-form.

    Returns:
        Joined view {thread_id, symbol, agent_id, agent_slug,
        agent_name, agent_status='inactive', ...}.
    """
    body = {
        "name":             name,
        "conditions":       conditions,
        "actions":          actions or [],
        "events":           events or [],
        "scope":            scope,
        "schedule":         schedule,
        "cooldown_minutes": int(cooldown_minutes or 30),
        "description":      description or "",
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
