"""
Reusable bid-ask spread threshold check.

Fetches a live broker quote for a (tradingsymbol, exchange) pair and
reports whether its bid-ask spread% is within an operator-configured
threshold. Built for the Chain-tab pre-submission gate (operator wants
a warning — not a block — when either the original leg or the
computed offset/wing leg has a spread too wide to trade comfortably),
but kept broker-agnostic and import-light so the declarative agent
grammar (`backend/api/algo/grammar.py`) can wrap it in a future metric
token without any new broker-call pattern.

Spread% formula matches the existing wing-scan liquidity filter
(`template_attach._ta_wing_depth_spread`): `(ask - bid) / ltp * 100`.
`ltp` is preferred as the denominator because it's the same reference
price the wing-scan scores candidates against; when `ltp` is absent or
non-positive (quote still warming up) we fall back to the bid/ask
midpoint and tag the result so callers can tell which basis was used.

Never raises. Every failure mode (quote-fetch exception, missing
instrument, zero/invalid bid-ask) is converted to a structured
`SpreadCheckResult` with `ok=False` instead of propagating an
exception to the caller — same "never silently crash the caller"
convention as `backend.shared.helpers.market_probe.probe_market_active`
and `template_attach._pick_wing_by_premium`.

Public API:
    evaluate_spread(quote, *, tradingsymbol, exchange, max_spread_pct) -> SpreadCheckResult
    check_spreads(legs, max_spread_pct, *, quote_fn=None) -> list[SpreadCheckResult]
    resolve_max_spread_pct(template, overrides) -> tuple[float, str]
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, asdict
from typing import Any, Callable, Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


@dataclass
class SpreadCheckResult:
    """Outcome of one spread check against one tradingsymbol+exchange.

    `status`:
      "ok"       — quote resolved, spread_pct <= max_spread_pct
      "wide"     — quote resolved, spread_pct  > max_spread_pct
      "no_quote" — quote fetched but bid/ask (or both) are zero/absent,
                   or a reference price to compute spread% off of was
                   unavailable
      "error"    — the quote fetch itself raised

    `ok` is a convenience bool — True only when status == "ok". Callers
    that just want "is this leg tradeable right now" can check `ok`
    directly without string-matching `status`.
    """
    status:         str             # "ok" | "wide" | "no_quote" | "error"
    ok:             bool
    tradingsymbol:  str
    exchange:       str
    max_spread_pct: float
    spread_pct:     Optional[float] = None
    bid:            Optional[float] = None
    ask:            Optional[float] = None
    ltp:            Optional[float] = None
    basis:          Optional[str]   = None   # "ltp" | "mid" | None
    reason:         Optional[str]   = None   # human-readable note, set on anything but "ok"
    role:           Optional[str]   = None   # caller-supplied tag, e.g. "parent" / "wing"

    def to_dict(self) -> dict:
        return asdict(self)


def _top_of_book(quote: Optional[dict]) -> tuple[float, float]:
    """Extract (bid, ask) from a broker quote dict's depth. 0.0 for
    either side when depth / that side is absent."""
    depth = (quote or {}).get("depth") or {}
    buys  = depth.get("buy")  or []
    sells = depth.get("sell") or []
    bid = float((buys[0] or {}).get("price") or 0) if buys else 0.0
    ask = float((sells[0] or {}).get("price") or 0) if sells else 0.0
    return bid, ask


def _spread_result(
    status: str,
    *,
    tradingsymbol: str,
    exchange: str,
    max_spread_pct: float,
    **fields,
) -> SpreadCheckResult:
    """Build one SpreadCheckResult; `ok` always derives from `status`
    so callers can never construct an inconsistent (status, ok) pair."""
    return SpreadCheckResult(
        status=status, ok=(status == "ok"),
        tradingsymbol=tradingsymbol, exchange=exchange,
        max_spread_pct=max_spread_pct, **fields,
    )


def evaluate_spread(
    quote: Optional[dict],
    *,
    tradingsymbol: str,
    exchange: str,
    max_spread_pct: float,
) -> SpreadCheckResult:
    """Pure function — compute a SpreadCheckResult from one broker quote
    dict already in hand. No I/O. Never raises.

    `quote` is the per-symbol dict `broker.quote([...])` returns — reads
    `quote["depth"]["buy"][0]["price"]` / `["sell"][0]["price"]` for
    top-of-book bid/ask, `quote["last_price"]` for ltp. Any missing
    field is treated as absent, not an error.
    """
    kw = dict(tradingsymbol=tradingsymbol, exchange=exchange, max_spread_pct=max_spread_pct)
    try:
        bid, ask = _top_of_book(quote)
        ltp = float((quote or {}).get("last_price") or 0)
    except Exception as e:
        logger.debug("evaluate_spread(%s:%s) failed: %s", exchange, tradingsymbol, e)
        return _spread_result("error", reason=f"evaluate_spread failed: {e}", **kw)

    if bid <= 0 or ask <= 0:
        return _spread_result(
            "no_quote", bid=(bid or None), ask=(ask or None), ltp=(ltp or None),
            reason="bid/ask unavailable — thin book or no depth data", **kw,
        )

    basis, denom = ("ltp", ltp) if ltp > 0 else ("mid", (bid + ask) / 2.0)
    if denom <= 0:
        return _spread_result(
            "no_quote", bid=bid, ask=ask, ltp=(ltp or None),
            reason="cannot compute spread — no positive reference price", **kw,
        )

    spread_pct = (ask - bid) / denom * 100.0
    status = "ok" if spread_pct <= max_spread_pct else "wide"
    reason = (
        None if status == "ok"
        else f"spread {spread_pct:.2f}% exceeds threshold {max_spread_pct:.2f}%"
    )
    return _spread_result(
        status, spread_pct=spread_pct, bid=bid, ask=ask, ltp=(ltp or None),
        basis=basis, reason=reason, **kw,
    )


async def check_spreads(
    legs: list[dict],
    max_spread_pct: float,
    *,
    quote_fn: Optional[Callable[[list[str]], Any]] = None,
) -> list[SpreadCheckResult]:
    """Batch-fetch quotes for every leg and evaluate each against
    *max_spread_pct*. One round-trip for N legs — same batching
    `template_attach._wing_fetch_quotes` already uses.

    `legs` — list of dicts: `{"tradingsymbol": str, "exchange": str,
    "role": str (optional, e.g. "parent"/"wing")}`.

    `quote_fn` — test seam. When supplied, called with the list of
    `"EXCHANGE:SYMBOL"` keys instead of hitting the real broker; may
    return a dict directly or a coroutine/awaitable. Defaults to
    `get_market_data_broker().quote(keys)` run off the event loop via
    `asyncio.to_thread` (the broker SDK call is synchronous network IO).

    Never raises — a quote-fetch exception degrades every leg in this
    batch to `status="error"`, never propagates.
    """
    if not legs:
        return []

    keys = [
        f"{(leg.get('exchange') or 'NFO')}:{leg.get('tradingsymbol') or ''}"
        for leg in legs
    ]

    quote_data: dict = {}
    fetch_err: Optional[str] = None
    try:
        if quote_fn is not None:
            res = quote_fn(keys)
            quote_data = (await res) if asyncio.iscoroutine(res) else res
        else:
            from backend.brokers.registry import get_market_data_broker
            broker = get_market_data_broker()
            quote_data = await asyncio.to_thread(broker.quote, keys)
        quote_data = quote_data or {}
    except Exception as e:
        fetch_err = str(e)
        logger.warning("check_spreads: quote fetch failed for %s: %s", keys, e)

    results: list[SpreadCheckResult] = []
    for leg, key in zip(legs, keys):
        sym   = str(leg.get("tradingsymbol") or "")
        exch  = str(leg.get("exchange") or "NFO")
        role  = leg.get("role")
        if fetch_err is not None:
            results.append(SpreadCheckResult(
                status="error", ok=False,
                tradingsymbol=sym, exchange=exch, max_spread_pct=max_spread_pct,
                reason=f"quote fetch failed: {fetch_err}", role=role,
            ))
            continue
        q = quote_data.get(key)
        if not q:
            results.append(SpreadCheckResult(
                status="no_quote", ok=False,
                tradingsymbol=sym, exchange=exch, max_spread_pct=max_spread_pct,
                reason=f"no quote returned for {key}", role=role,
            ))
            continue
        r = evaluate_spread(
            q, tradingsymbol=sym, exchange=exch, max_spread_pct=max_spread_pct,
        )
        r.role = role
        results.append(r)
    return results


def resolve_max_spread_pct(
    template: Optional[dict],
    overrides: Optional[dict],
) -> tuple[float, str]:
    """Resolve the effective spread% threshold: override > template >
    global admin setting (`templates.wing_max_spread_pct`, default
    0.5 — same setting `_pick_wing_by_premium`'s own liquidity filter
    reads, deliberately: the operator's brief is "the global setting's
    current value as the default, editable per-order/per-template").

    Returns `(value, source)` where `source` is one of
    `"override" | "template" | "setting"` so a caller/response can show
    the operator where the number came from (e.g. "using template
    default" vs "using global default" vs "your override").

    `template` may be `None` (no saved template — ad-hoc overrides
    only) or a dict missing the key (older template row / system
    default); `overrides` may be `None`. Never raises — a non-numeric
    override/template value is treated as absent and falls through to
    the next tier.
    """
    _ov = overrides or {}
    v = _ov.get("wing_max_spread_pct")
    if v is not None:
        try:
            return float(v), "override"
        except (TypeError, ValueError):
            pass

    if template:
        v = template.get("wing_max_spread_pct")
        if v is not None:
            try:
                return float(v), "template"
            except (TypeError, ValueError):
                pass

    try:
        from backend.shared.helpers.settings import get_float
        return float(get_float("templates.wing_max_spread_pct", 0.5)), "setting"
    except Exception:
        return 0.5, "setting"
