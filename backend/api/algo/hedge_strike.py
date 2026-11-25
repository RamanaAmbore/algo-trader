"""
Hedge leg — order-entry-time strike search for the DSL `hedge_pct` param.

Mirrors the Chain tab's own Hedge checkbox (`frontend/src/lib/order/
OptionChainTab.svelte`, `resolveHedgeStrike()` in `spreadGate.js`) —
deliberately a SEPARATE implementation, not shared code. Python and JS
can't share a function; both read the same rules from the same design
spec (`.claude/PLAN.md`, Part A) so a BUY-hedge-at-50%-of-premium /
SELL-hedge-at-25%-of-premium pattern behaves identically whichever
surface staged it, without either side importing the other.

A "hedge leg" is a second, further-OTM, opposite-side option on the
SAME underlying/expiry/option-type as a just-placed parent leg,
sized to cost roughly a target % of the parent's own premium. This is
NOT the deleted Bracket/Template-GTT "wing" concept (a manually
spaced protective leg resolved at order time from the operator's own
point offsets) and NOT Part C's MCX settlement netting (a
backward-looking match among currently HELD positions, scored by
theta priority). Both of those solve different problems; do not try
to unify any of the three.

Public API:
    resolve_hedge_strike(parent_opt_type, parent_strike, parent_side,
                          parent_premium, target_pct, candidates) -> dict
    fetch_hedge_quotes(underlying, expiry, account) -> dict  (async)
    resolve_parent_real_expiry(symbol, fallback_iso) -> str  (async)
    candidates_match_parent_symbol(candidates, parent_strike,
                                    parent_opt_type, parent_symbol) -> bool
"""

from __future__ import annotations

import asyncio

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# A candidate is accepted only when its own cost-ratio falls within
# this fraction of the target — target 50 accepts the real ratio
# anywhere in [25, 75]. Matches the Chain-tab Hedge checkbox's own
# tolerance band exactly (see Part A design, `.claude/PLAN.md`).
_TOLERANCE_FRACTION = 0.5


def _hedge_validate_shape(parent_opt_type: str, parent_side: str) -> "tuple[str, str] | dict":
    """Normalize + validate the two enum-shaped inputs. Returns
    (opt_type, side) on success, or a {"ok": False, ...} failure dict."""
    opt_type = str(parent_opt_type or "").upper()
    side = str(parent_side or "").upper()
    if opt_type not in ("CE", "PE"):
        return {"ok": False, "reason": f"invalid parent option type {parent_opt_type!r}"}
    if side not in ("BUY", "SELL"):
        return {"ok": False, "reason": f"invalid parent side {parent_side!r}"}
    return opt_type, side


def _hedge_validate_numeric(parent_strike, parent_premium, target_pct) -> "tuple[float, float, float] | dict":
    """Coerce + range-check the three numeric inputs. Returns
    (strike_val, premium_val, target_val) on success, or a failure dict."""
    try:
        strike_val = float(parent_strike)
        premium_val = float(parent_premium)
        target_val = float(target_pct)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "invalid numeric input"}
    if premium_val <= 0:
        return {"ok": False, "reason": "parent premium must be positive"}
    if target_val <= 0:
        return {"ok": False, "reason": "target percent must be positive"}
    return strike_val, premium_val, target_val


def _hedge_normalize_candidates(candidates) -> "tuple[dict | None, dict | None]":
    """Returns ({float_strike: quote_dict, ...}, None) on success, or
    (None, {"ok": False, ...}) for an empty/malformed book — a tuple
    rather than one overloaded return type, so the caller never has to
    guess whether a returned dict is the candidate book or a failure."""
    if not isinstance(candidates, dict) or not candidates:
        return None, {"ok": False, "reason": "no quotes available"}
    try:
        return {float(k): v for k, v in candidates.items()}, None
    except (TypeError, ValueError):
        return None, {"ok": False, "reason": "malformed candidate strikes"}


def _hedge_ordered_strikes(opt_type: str, strike_val: float, by_strike: dict) -> list:
    """Further-OTM candidate strikes, nearest-to-parent first: ascending
    above the parent for a CE hedge, descending below it for a PE
    hedge — so the search-order tie-break in `_hedge_search_best()`
    naturally favors the tighter, closer-to-parent spread."""
    strikes = sorted(by_strike.keys())
    if opt_type == "CE":
        return [k for k in strikes if k > strike_val]
    return [k for k in reversed(strikes) if k < strike_val]


def _hedge_price_for_side(side_quote: dict, hedge_side: str) -> "float | None":
    """A real depth-backed price for the side the HEDGE leg would
    actually trade at — BUY reads ask, SELL reads bid, falling back to
    the other side only when the preferred one has no quote. Returns
    None (never 0) when neither side carries a usable price.

    `depth_available` (absent → treated as True, so a hand-built dict
    in a test that omits it still works): `_chain_quotes_bid_ask_from_q`
    (options.py) fills BOTH bid and ask from `last_price` whenever the
    real depth array is empty — an illiquid strike that has simply
    traded at some point in the past otherwise sails through the
    0/None rejection below with a stale, non-tradeable "quote". A
    candidate with no real depth is rejected outright here, same as a
    genuinely missing price.
    """
    if side_quote.get("depth_available", True) is False:
        return None
    bid = side_quote.get("bid")
    ask = side_quote.get("ask")
    price = (ask or bid) if hedge_side == "BUY" else (bid or ask)
    return price if price and price > 0 else None


def _hedge_search_best(
    ordered: list, by_strike: dict, quote_key: str, hedge_side: str,
    target_val: float, premium_val: float,
) -> dict:
    """Scan `ordered` strikes nearest-to-parent first, accept only
    those within ±50% of `target_val`, and return the nearest-to-target
    match. Never falls back to an out-of-tolerance strike."""
    low = target_val * (1 - _TOLERANCE_FRACTION)
    high = target_val * (1 + _TOLERANCE_FRACTION)

    best_strike = None
    best_price = None
    best_ratio = None
    best_diff = None
    checked = 0

    for strike in ordered:
        side_quote = (by_strike.get(strike) or {}).get(quote_key) or {}
        price = _hedge_price_for_side(side_quote, hedge_side)
        if price is None:
            continue
        checked += 1
        ratio = (price / premium_val) * 100.0
        if not (low <= ratio <= high):
            continue
        diff = abs(ratio - target_val)
        if best_diff is None or diff < best_diff:
            best_strike, best_price, best_ratio, best_diff = strike, price, ratio, diff

    if best_strike is None:
        if checked:
            reason = (
                f"no strike within ±{_TOLERANCE_FRACTION * 100:.0f}% of "
                f"target {target_val:g}% hedge cost ({checked} candidate(s) priced)"
            )
        else:
            reason = "no depth-backed quote for any candidate strike"
        return {"ok": False, "reason": reason}

    return {"ok": True, "strike": best_strike, "price": best_price, "ratio": best_ratio}


def resolve_hedge_strike(
    parent_opt_type: str,
    parent_strike: float,
    parent_side: str,
    parent_premium: float,
    target_pct: float,
    candidates: dict,
) -> dict:
    """Pure strike search for a hedge leg. Never raises.

    `candidates` is shaped {strike: {"ce": {"bid":.., "ask":..},
    "pe": {"bid":.., "ask":..}}} — the exact per-strike bid/ask book
    `fetch_hedge_quotes()` below builds, reusing `options.py`'s own
    chain-quote helpers, so a strike this returns is priced off the
    same live depth the Chain tab itself reads.

    Direction: a CE hedge searches strikes ABOVE `parent_strike`
    (further OTM for a call); a PE hedge searches BELOW it (further
    OTM for a put) — the parent's own strike is never a candidate.
    The hedge leg's side is always the OPPOSITE of `parent_side`;
    that inversion is what makes it a hedge rather than a second
    directional bet on the same name.

    Pricing follows the side the HEDGE leg would actually be placed
    at (never the parent's): BUY reads a candidate's own `ask` (what
    a buyer actually pays), SELL reads `bid` (what a seller actually
    receives) — falling back to the other side only when the
    preferred one carries no quote. A candidate with no real
    depth-backed price on either side (0 or missing) is rejected
    outright, never treated as a valid 0-cost hedge.

    `target_pct` and the returned `ratio` are both percentage-point
    numbers on the same 0-100 scale (50.0 means "50% of the parent's
    own premium"), not a 0-1 fraction. Among candidates within
    tolerance, the nearest-to-target wins; ties favor whichever
    candidate was checked first (closer-to-parent strikes are
    checked first), never an arbitrary later one.

    Returns {"ok": True, "strike": float, "price": float,
    "ratio": float} on a match, or {"ok": False, "reason": str}
    otherwise. No match within tolerance, no quotes, or invalid
    input never falls back to an out-of-tolerance strike — a
    structured failure is the only alternative to a genuine match.
    """
    shape = _hedge_validate_shape(parent_opt_type, parent_side)
    if isinstance(shape, dict):
        return shape
    opt_type, side = shape

    numeric = _hedge_validate_numeric(parent_strike, parent_premium, target_pct)
    if isinstance(numeric, dict):
        return numeric
    strike_val, premium_val, target_val = numeric

    by_strike, err = _hedge_normalize_candidates(candidates)
    if err is not None:
        return err

    ordered = _hedge_ordered_strikes(opt_type, strike_val, by_strike)
    if not ordered:
        return {"ok": False, "reason": "no further-OTM strikes quoted"}

    hedge_side = "SELL" if side == "BUY" else "BUY"
    result = _hedge_search_best(ordered, by_strike, opt_type.lower(), hedge_side, target_val, premium_val)
    return result


async def fetch_hedge_quotes(underlying: str, expiry: str, account: str) -> dict:
    """Build the per-strike bid/ask book `resolve_hedge_strike()` consumes.

    Reuses `options.py`'s existing chain-quote-building helpers
    (`_chain_quotes_sym_lookup`, `_chain_quotes_batch_quote`,
    `_chain_quotes_build_book`) rather than re-deriving the instruments
    lookup or the broker quote call — the candidate set returned here
    is therefore premium-basis-identical to what the Chain tab's own
    `chainQuotesMap` would show for the same (underlying, expiry), not
    a parallel approximation.

    `account` is accepted for signature symmetry with every other
    account-scoped helper the DSL action layer calls — chain quotes
    are market-data, not account-scoped (one shared market-data
    broker), so it is not used to select a broker here.

    Returns {} on any failure (cold instruments cache, broker outage,
    no matching strikes, import error) — callers already treat an
    empty candidate set exactly like "no hedge match found" via
    `resolve_hedge_strike()`'s own "no quotes available" branch, so
    there is never a need to distinguish "couldn't fetch" from
    "fetched nothing" at this layer.
    """
    try:
        from backend.api.cache import peek as _cache_peek
        from backend.api.routes.options import (
            _chain_quotes_sym_lookup,
            _chain_quotes_batch_quote,
            _chain_quotes_build_book,
        )

        und = str(underlying or "").strip().upper()
        exp = str(expiry or "").strip()
        if not und or not exp:
            return {}

        inst_resp = _cache_peek("instruments_chain") or _cache_peek("instruments")
        if inst_resp is None:
            logger.warning("[hedge_strike] instruments cache cold — no hedge candidates for %s", und)
            return {}

        sym_by_strike, _all_expiries = await _chain_quotes_sym_lookup(und, exp, inst_resp)
        if not sym_by_strike:
            return {}

        quote_resp, key_meta = await _chain_quotes_batch_quote(sym_by_strike, und, exp)
        book_by_strike = _chain_quotes_build_book(sym_by_strike, quote_resp, key_meta)

        return {
            strike: {
                "ce": {"bid": sides["CE"]["bid"], "ask": sides["CE"]["ask"],
                       "sym": sides["CE"]["sym"], "depth_available": sides["CE"]["depth_available"]},
                "pe": {"bid": sides["PE"]["bid"], "ask": sides["PE"]["ask"],
                       "sym": sides["PE"]["sym"], "depth_available": sides["PE"]["depth_available"]},
            }
            for strike, sides in book_by_strike.items()
        }
    except Exception as e:
        logger.warning("[hedge_strike] fetch_hedge_quotes failed for %s/%s: %s", underlying, expiry, e)
        return {}


def _resolve_parent_real_expiry_scan(inst_resp, sym_upper: str) -> "str | None":
    """Sync scan over the instruments dump (35K-156K rows) — run inside
    `asyncio.to_thread` by the async wrapper below, never inline on the
    event loop. Mirrors `_chain_quotes_sym_lookup`'s own
    compute-outside-the-loop pattern (`options.py`) for the identical
    cache object."""
    for inst in inst_resp.items:
        if inst.s == sym_upper and inst.x:
            return inst.x
    return None


async def resolve_parent_real_expiry(symbol: str, fallback_iso: str) -> str:
    """Prefer the parent's own real expiry from the instruments-cache
    row for this EXACT tradingsymbol over `parse_tradingsymbol()`'s
    "last Tuesday/Thursday" approximation. A holiday-shifted monthly
    expiry makes that approximation wrong on the actual calendar date,
    which would otherwise feed `fetch_hedge_quotes()` an expiry no
    option in the chain actually carries — an empty book, read by the
    caller as a confusing, silent "no hedge match found" rather than a
    correct hedge. Falls back to `fallback_iso` on any cache miss,
    lookup failure, or missing-match.
    """
    try:
        from backend.api.cache import peek as _cache_peek
        inst_resp = _cache_peek("instruments_chain") or _cache_peek("instruments")
        if inst_resp is None:
            return fallback_iso
        sym_upper = str(symbol or "").upper()
        found = await asyncio.to_thread(_resolve_parent_real_expiry_scan, inst_resp, sym_upper)
        if found:
            return found
    except Exception as e:
        logger.warning("[hedge_strike] resolve_parent_real_expiry failed for %s: %s", symbol, e)
    return fallback_iso


def candidates_match_parent_symbol(
    candidates: dict, parent_strike: float, parent_opt_type: str, parent_symbol: str,
) -> bool:
    """Sanity check: does the fetched chain actually contain the
    PARENT's own tradingsymbol at its own strike? A False result means
    `fetch_hedge_quotes()` resolved the wrong expiry (or the wrong
    underlying root) — trusting that chain to find a hedge would mean
    hedging against a different contract month than the one actually
    held. Fails closed (False) on any malformed input, never raises.
    """
    try:
        quote_key = str(parent_opt_type or "").lower()
        entry = (candidates.get(float(parent_strike)) or {}).get(quote_key) or {}
        return bool(entry.get("sym")) and entry["sym"].upper() == str(parent_symbol or "").upper()
    except (TypeError, ValueError, AttributeError):
        return False
