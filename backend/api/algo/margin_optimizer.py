"""
Margin optimizer — given a current options book, suggest alternative
structures with similar return profile but lower margin requirement.

Strategy library (v1, intentionally small — each generator handles
ONE recognisable pattern that operators tune for capital efficiency):

  • add_wing            — naked SELL options → buy a wing N strikes
                          OTM, capping margin via defined-risk
                          (Kite margin drops ~40-70% for an iron
                          condor leg vs a naked short).
  • convert_to_spread   — long option → sell further OTM strike,
                          reducing cost basis + lowering margin.
  • roll_strike_otm     — ATM SELL → close + open 1 step further
                          OTM, lowering margin + assignment risk.

Scoring per alternative:
  • Δ margin saved (₹) and (%)
  • Δ EV (expected value of the structure — change is OK if within tolerance)
  • Δ POP (probability of profit)
  • Δ Max loss (worst-case loss can grow within tolerance)
  • Composite ranking: margin-saved-per-Δ-max-loss with EV/POP gates

Cache: 30-min TTL keyed by (account, legs_hash). The compute is heavy
(N kite.basket_order_margins round-trips per request) so we deliberately
serve stale results in the same window unless the operator hits Refresh.

Industry analogue: TastyTrade Margin Analyzer + Sensibull Strategy Lab.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, asdict, field
from typing import Optional, Any

from backend.shared.helpers.ramboq_logger import get_logger
from backend.api.algo.derivatives import (
    parse_tradingsymbol,
    multileg_payoff_curve,
    multileg_extremes,
    multileg_pop,
    expected_value,
    find_breakevens,
    DEFAULT_IV,
)

logger = get_logger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────
DEFAULT_EV_TOLERANCE_PCT      = 15.0   # alternatives within ±15% EV qualify
DEFAULT_MAX_LOSS_DRIFT_PCT    = 25.0   # max_loss can grow by up to 25%
DEFAULT_WING_OFFSET_STRIKES   = 1      # add_wing: 1 strike-step OTM
DEFAULT_ROLL_OFFSET_STRIKES   = 1      # roll_strike: 1 strike-step OTM


# ── Data shapes ──────────────────────────────────────────────────────

@dataclass
class Leg:
    symbol:    str        # Kite tradingsymbol
    qty:       int        # signed: + long, - short
    side:      str        # "BUY" | "SELL"
    avg_cost:  float
    ltp:       float
    exchange:  str = "NFO"
    parsed:    Optional[dict] = None  # cached parse_tradingsymbol() output


@dataclass
class Metrics:
    margin_required: float = 0.0
    max_profit:      float = 0.0
    max_loss:        float = 0.0
    breakevens:      list[float] = field(default_factory=list)
    ev:              float = 0.0
    pop_pct:         float = 0.0
    payoff_curve:    list[dict] = field(default_factory=list)


@dataclass
class Alternative:
    name:           str            # strategy slug
    description:    str            # operator-readable
    close_legs:     list[dict]     # legs to close
    open_legs:      list[dict]     # legs to open
    metrics:        Metrics

    # Deltas vs current
    margin_delta:     float = 0.0
    margin_delta_pct: float = 0.0
    ev_delta:         float = 0.0
    pop_delta:        float = 0.0
    max_loss_delta:   float = 0.0

    score: float = 0.0
    notes: list[str] = field(default_factory=list)


@dataclass
class OptimizeResult:
    computed_at:           str
    ttl_remaining_seconds: int
    request_hash:          str
    current:               Metrics
    alternatives:          list[Alternative]
    notes:                 list[str] = field(default_factory=list)


# ── Cache ────────────────────────────────────────────────────────────

_OPTIMIZE_CACHE: dict[str, tuple[float, OptimizeResult]] = {}
_OPTIMIZE_TTL_S = 30 * 60   # 30 minutes


def _hash_request(account: str, underlying: str, legs: list[Leg]) -> str:
    """Stable hash over (account, underlying, leg fingerprints).
    Symbol-wise scoping — the optimizer always looks at one
    underlying's worth of legs for one account, so the cache key
    triples include both. avg_cost + LTP flux doesn't bust the cache
    (margin is qty + strike-driven)."""
    payload = json.dumps(
        [{"a": account, "u": underlying},
         *[{"s": l.symbol, "q": l.qty, "side": l.side} for l in legs]],
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


# ── Strategy generators ──────────────────────────────────────────────

def _strike_step(parsed: dict) -> int:
    """Strike-step grid by underlying. Mostly 50 for index options;
    100/500 for commodities. Used by add_wing + roll_strike_otm to
    nudge by N steps."""
    root = (parsed.get("underlying") or "").upper()
    grid = {
        "NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25,
        "SENSEX": 100, "BANKEX": 100,
        "CRUDEOIL": 50, "NATURALGAS": 5, "GOLDM": 100, "GOLD": 100,
        "SILVER": 500, "SILVERMIC": 500, "COPPER": 0.5,
    }
    return int(grid.get(root, 50))


def _build_alt_symbol(orig_symbol: str, parsed: dict, new_strike: int) -> str:
    """Reconstruct a Kite tradingsymbol with a new strike, preserving
    root + expiry token + CE/PE. parse_tradingsymbol doesn't return
    the raw expiry token, so we slice it from the original symbol:
        NIFTY26JUN22000CE → root="NIFTY", strike="22000", opt="CE"
        → expiry token = symbol slice between root and strike
    """
    root = parsed["underlying"]
    opt  = parsed["opt_type"]
    strike = int(parsed["strike"])
    # The original symbol carries the expiry token between root and
    # strike: NIFTY26JUN22000CE → "26JUN" between "NIFTY" and "22000CE".
    s = orig_symbol.upper()
    # Strip root prefix and the trailing "<strike><opt>" suffix.
    middle = s[len(root):-len(f"{strike}{opt}")]
    return f"{root}{middle}{new_strike}{opt}"


def gen_add_wing(legs: list[Leg]) -> list[Alternative]:
    """For every naked SELL option (no matching BUY at higher/lower
    strike), propose buying a wing N strikes OTM."""
    out: list[Alternative] = []
    for leg in legs:
        if leg.side != "SELL":
            continue
        if not leg.parsed or leg.parsed.get("kind") != "opt":
            continue
        p = leg.parsed
        strike = int(p["strike"])
        opt    = p["opt_type"]
        step   = _strike_step(p)
        if step <= 0:
            continue
        offset = DEFAULT_WING_OFFSET_STRIKES * step
        # CE wing — buy at HIGHER strike (caps upside damage).
        # PE wing — buy at LOWER strike (caps downside damage).
        new_strike = strike + offset if opt == "CE" else strike - offset
        if new_strike <= 0:
            continue
        wing_sym = _build_alt_symbol(leg.symbol, p, new_strike)
        # Already has a wing? Skip (avoid double-hedging).
        if any(l.symbol == wing_sym for l in legs):
            continue
        wing_leg = {
            "symbol":   wing_sym,
            "qty":      abs(leg.qty),
            "side":     "BUY",
            "exchange": leg.exchange,
        }
        out.append(Alternative(
            name="add_wing",
            description=(
                f"Add protective {opt} wing at strike {new_strike} "
                f"to convert naked {leg.symbol} into a defined-risk spread"
            ),
            close_legs=[],
            open_legs=[wing_leg],
            metrics=Metrics(),
        ))
    return out


def gen_convert_to_spread(legs: list[Leg]) -> list[Alternative]:
    """For every naked LONG option (no matching SELL), propose selling
    a further-OTM strike — reduces cost basis + lowers margin (premium
    received offsets debit)."""
    out: list[Alternative] = []
    for leg in legs:
        if leg.side != "BUY":
            continue
        if not leg.parsed or leg.parsed.get("kind") != "opt":
            continue
        p = leg.parsed
        strike = int(p["strike"])
        opt    = p["opt_type"]
        step   = _strike_step(p)
        if step <= 0:
            continue
        offset = DEFAULT_WING_OFFSET_STRIKES * step
        # Long CE → sell a HIGHER-strike CE (call vertical spread, bull).
        # Long PE → sell a LOWER-strike PE (put vertical spread, bear).
        new_strike = strike + offset if opt == "CE" else strike - offset
        if new_strike <= 0:
            continue
        sell_sym = _build_alt_symbol(leg.symbol, p, new_strike)
        if any(l.symbol == sell_sym for l in legs):
            continue
        sell_leg = {
            "symbol":   sell_sym,
            "qty":      abs(leg.qty),
            "side":     "SELL",
            "exchange": leg.exchange,
        }
        out.append(Alternative(
            name="convert_to_spread",
            description=(
                f"Convert long {leg.symbol} into a vertical spread "
                f"by selling strike {new_strike} — reduces cost basis"
            ),
            close_legs=[],
            open_legs=[sell_leg],
            metrics=Metrics(),
        ))
    return out


def gen_roll_strike_otm(legs: list[Leg]) -> list[Alternative]:
    """For ATM-ish SELL options, propose closing + reopening 1 step
    OTM. Trades premium for lower margin + lower assignment risk."""
    out: list[Alternative] = []
    for leg in legs:
        if leg.side != "SELL":
            continue
        if not leg.parsed or leg.parsed.get("kind") != "opt":
            continue
        p = leg.parsed
        strike = int(p["strike"])
        opt    = p["opt_type"]
        step   = _strike_step(p)
        if step <= 0:
            continue
        offset = DEFAULT_ROLL_OFFSET_STRIKES * step
        # CE OTM = higher strike; PE OTM = lower strike. Same direction
        # as add_wing but we're CLOSING the current leg + REOPENING at
        # new strike (instead of layering a wing on top).
        new_strike = strike + offset if opt == "CE" else strike - offset
        if new_strike <= 0:
            continue
        new_sym = _build_alt_symbol(leg.symbol, p, new_strike)
        if any(l.symbol == new_sym for l in legs):
            continue
        close_leg = {
            "symbol":   leg.symbol,
            "qty":      abs(leg.qty),
            "side":     "BUY",          # BUY to close a SELL
            "exchange": leg.exchange,
        }
        open_leg = {
            "symbol":   new_sym,
            "qty":      abs(leg.qty),
            "side":     "SELL",
            "exchange": leg.exchange,
        }
        out.append(Alternative(
            name="roll_strike_otm",
            description=(
                f"Roll {leg.symbol} → {new_sym} (1 step OTM): lower "
                f"margin + lower assignment probability, less premium"
            ),
            close_legs=[close_leg],
            open_legs=[open_leg],
            metrics=Metrics(),
        ))
    return out


_GENERATORS = {
    "add_wing":             gen_add_wing,
    "convert_to_spread":    gen_convert_to_spread,
    "roll_strike_otm":      gen_roll_strike_otm,
}


# ── Margin + return scoring ──────────────────────────────────────────

async def _compute_basket_margin(account: str, basket: list[dict]) -> float:
    """Round-trip kite.basket_order_margins. Returns 0.0 on any
    failure (caller treats 0 as "unavailable" and skips ranking)."""
    if not basket:
        return 0.0
    try:
        from backend.shared.brokers.registry import get_broker
        broker = get_broker(account)
        kite_basket = [
            {
                "exchange":         l.get("exchange") or "NFO",
                "tradingsymbol":    l["symbol"],
                "transaction_type": l["side"],
                "variety":          "regular",
                "product":          "NRML",
                "order_type":       "MARKET",
                "quantity":         int(l["qty"]),
            }
            for l in basket
        ]
        resp = await asyncio.to_thread(broker.basket_order_margins, kite_basket)
        if isinstance(resp, list) and resp:
            # Kite returns a list of per-leg dicts; the LAST entry's
            # `final.total` is the basket-wide net margin.
            tail = resp[-1] if isinstance(resp[-1], dict) else {}
            final = tail.get("final") or {}
            return float(final.get("total") or 0.0)
        return 0.0
    except Exception as e:
        logger.warning(f"basket_order_margins for {account!r} failed: {e}")
        return 0.0


def _legs_to_payoff_input(legs: list[Leg]) -> list[dict]:
    """Convert internal Leg dataclass list to multileg_payoff_curve's
    expected input shape."""
    out = []
    for l in legs:
        p = l.parsed or {}
        out.append({
            "kind":         p.get("kind", "opt"),
            "underlying":   p.get("underlying"),
            "strike":       p.get("strike"),
            "opt_type":     p.get("opt_type"),
            "T_years":      p.get("T_years") or 0.05,
            "sigma":        p.get("sigma")   or DEFAULT_IV,
            "qty":          l.qty if l.side == "BUY" else -abs(l.qty),
            "entry_price":  l.avg_cost,
        })
    return out


def _compute_metrics(legs: list[Leg], spot: float) -> Metrics:
    """Build the full Metrics block (payoff curve + extremes + EV +
    POP + breakevens) for a set of legs."""
    if not legs:
        return Metrics()
    multi = _legs_to_payoff_input(legs)
    try:
        curve = multileg_payoff_curve(multi, S=spot, span_pct=0.20)
        max_profit, max_loss = multileg_extremes(curve)
        breakevens = find_breakevens(curve)
        # qty-weighted IV proxy for POP
        sigma_proxy = sum(abs(l["qty"]) * (l["sigma"] or DEFAULT_IV) for l in multi)
        sigma_total_qty = sum(abs(l["qty"]) for l in multi)
        sigma_proxy = sigma_proxy / sigma_total_qty if sigma_total_qty else DEFAULT_IV
        T_years = max((l["T_years"] or 0.05) for l in multi)
        pop_val = multileg_pop(curve, S=spot, T_years=T_years, sigma=sigma_proxy) * 100.0
        ev_val  = expected_value(curve, S=spot, T_years=T_years, sigma=sigma_proxy)
        # Compact serialisable curve for the UI.
        compact_curve = [{"s": round(p["spot"], 2),
                          "t": round(p["today_value"], 2),
                          "e": round(p["expiry_value"], 2)}
                         for p in curve]
        return Metrics(
            max_profit=max_profit,
            max_loss=max_loss,
            breakevens=list(breakevens),
            ev=ev_val,
            pop_pct=pop_val,
            payoff_curve=compact_curve,
        )
    except Exception as e:
        logger.warning(f"_compute_metrics failed: {e}")
        return Metrics()


def _score(current: Metrics, alt: Metrics,
           ev_tolerance_pct: float, max_loss_drift_pct: float) -> tuple[float, list[str]]:
    """Composite score. Higher = better proposal.

    Returns (score, notes). When the proposal falls outside tolerance
    the score is negative and the notes explain why.
    """
    notes: list[str] = []
    if current.margin_required <= 0 or alt.margin_required < 0:
        return (-1.0, ["margin unavailable"])
    margin_saved = current.margin_required - alt.margin_required
    margin_pct = margin_saved / current.margin_required if current.margin_required else 0.0
    ev_delta = alt.ev - current.ev
    ev_abs_pct = abs(ev_delta) / max(abs(current.ev), 1.0) * 100.0
    if ev_abs_pct > ev_tolerance_pct:
        notes.append(f"EV drifts {ev_abs_pct:.0f}% (limit {ev_tolerance_pct:.0f}%)")
        return (-1.0, notes)
    max_loss_drift = (alt.max_loss - current.max_loss)
    if current.max_loss < 0 and max_loss_drift < 0:
        # Worse max loss (more negative). Bound by tolerance.
        drift_pct = abs(max_loss_drift) / abs(current.max_loss) * 100.0
        if drift_pct > max_loss_drift_pct:
            notes.append(f"max_loss drifts {drift_pct:.0f}% (limit {max_loss_drift_pct:.0f}%)")
            return (-1.0, notes)
    # Composite: margin saved per unit risk. POP bonus.
    pop_delta = alt.pop_pct - current.pop_pct
    score = margin_pct * 100.0 + (pop_delta * 2.0) + (ev_delta / 1000.0)
    return (round(score, 2), notes)


# ── Orchestrator ─────────────────────────────────────────────────────

async def optimize(
    account: str,
    underlying: str,
    legs_data: list[dict],
    *,
    spot: float,
    ev_tolerance_pct: float = DEFAULT_EV_TOLERANCE_PCT,
    max_loss_drift_pct: float = DEFAULT_MAX_LOSS_DRIFT_PCT,
    strategies: Optional[list[str]] = None,
    force_refresh: bool = False,
    template: Optional[dict] = None,
) -> OptimizeResult:
    """Top-level orchestrator — scoped to one (account, underlying)
    pair. The caller passes only legs that belong to that
    account/underlying combo; we filter defensively to drop any leg
    that's parsed as a different underlying, so a misrouted leg
    from the frontend can't pollute alternative generation.

    Reads cache, generates alternatives, computes metrics + scores,
    ranks. Returns an OptimizeResult.
    """
    legs = [_legs_data_to_leg(d) for d in legs_data]
    legs = [l for l in legs if l]
    # Defensive filter — drop legs whose parsed underlying doesn't match.
    underlying_upper = (underlying or "").upper()
    if underlying_upper:
        legs = [
            l for l in legs
            if l.parsed and (l.parsed.get("underlying") or "").upper() == underlying_upper
        ]

    req_hash = _hash_request(account, underlying_upper, legs)
    now = time.time()
    if not force_refresh:
        cached = _OPTIMIZE_CACHE.get(req_hash)
        if cached and cached[0] > now:
            result = cached[1]
            # Refresh ttl_remaining_seconds for the response.
            result.ttl_remaining_seconds = max(0, int(cached[0] - now))
            return result

    strats = strategies or list(_GENERATORS.keys())
    notes: list[str] = []

    # Template-driven strategy suppression. When the operator's chosen
    # template carries wing config (either wing_strike_offset or
    # wing_premium_pct), it ALREADY provides protective-wing legs at
    # order-attach time. Layering the optimizer's add_wing strategy on
    # top would double-hedge — burns margin without improving the risk
    # profile. Suppress with an operator-visible note so the choice is
    # transparent.
    if template:
        has_wing = (template.get("wing_strike_offset") is not None
                    or template.get("wing_premium_pct")   is not None)
        if has_wing and "add_wing" in strats:
            strats = [s for s in strats if s != "add_wing"]
            tname = template.get("name") or template.get("slug") or "(template)"
            notes.append(
                f"Template '{tname}' includes protective wings — "
                f"add_wing strategy suppressed (no double-hedge)."
            )

    # Current metrics + margin
    current_metrics = _compute_metrics(legs, spot)
    current_basket = [
        {"symbol": l.symbol, "qty": abs(l.qty), "side": l.side,
         "exchange": l.exchange}
        for l in legs
    ]
    current_metrics.margin_required = await _compute_basket_margin(account, current_basket)

    # Generate alternatives
    alts: list[Alternative] = []
    for s in strats:
        gen = _GENERATORS.get(s)
        if not gen:
            notes.append(f"unknown strategy {s!r}")
            continue
        alts.extend(gen(legs))

    # Score each — compute the COMBINED legs (current minus close + open)
    # to get the alt's metrics.
    for alt in alts:
        post_legs = _apply_alt(legs, alt)
        alt.metrics = _compute_metrics(post_legs, spot)
        alt_basket = [
            {"symbol": l.symbol, "qty": abs(l.qty), "side": l.side,
             "exchange": l.exchange}
            for l in post_legs
        ]
        alt.metrics.margin_required = await _compute_basket_margin(account, alt_basket)
        alt.margin_delta     = current_metrics.margin_required - alt.metrics.margin_required
        alt.margin_delta_pct = (
            alt.margin_delta / current_metrics.margin_required
            if current_metrics.margin_required else 0.0
        )
        alt.ev_delta       = alt.metrics.ev - current_metrics.ev
        alt.pop_delta      = alt.metrics.pop_pct - current_metrics.pop_pct
        alt.max_loss_delta = alt.metrics.max_loss - current_metrics.max_loss
        alt.score, alt.notes = _score(
            current_metrics, alt.metrics,
            ev_tolerance_pct, max_loss_drift_pct,
        )

    # Rank: viable (score > 0) first, then by score desc.
    alts.sort(key=lambda a: (a.score < 0, -a.score))
    # Take top 5 to keep payload manageable.
    alts = alts[:5]

    if not alts:
        notes.append("no actionable alternatives found for current book")

    expires_at = now + _OPTIMIZE_TTL_S
    from datetime import datetime, timezone
    result = OptimizeResult(
        computed_at=datetime.now(timezone.utc).isoformat(),
        ttl_remaining_seconds=_OPTIMIZE_TTL_S,
        request_hash=req_hash,
        current=current_metrics,
        alternatives=alts,
        notes=notes,
    )
    _OPTIMIZE_CACHE[req_hash] = (expires_at, result)
    return result


def _apply_alt(legs: list[Leg], alt: Alternative) -> list[Leg]:
    """Return a new leg list after closing alt.close_legs and adding
    alt.open_legs. Quantities are matched by symbol; partial closes
    are supported (subtract qty)."""
    by_sym = {l.symbol: l for l in legs}
    # Apply closes
    for c in alt.close_legs:
        sym = c["symbol"]
        cur = by_sym.get(sym)
        if not cur:
            continue
        if abs(cur.qty) <= abs(c["qty"]):
            by_sym.pop(sym)
        else:
            # Reduce qty
            sign = 1 if cur.qty > 0 else -1
            cur.qty = sign * (abs(cur.qty) - abs(c["qty"]))
    # Add opens
    for o in alt.open_legs:
        l = _legs_data_to_leg({
            "symbol": o["symbol"], "qty": abs(o["qty"]),
            "side":   o["side"],
            "avg_cost": 0.0, "ltp": 0.0,
            "exchange": o.get("exchange", "NFO"),
        })
        if l:
            by_sym[l.symbol] = l
    return list(by_sym.values())


def _legs_data_to_leg(d: dict) -> Optional[Leg]:
    sym  = str(d.get("symbol") or "").upper().strip()
    if not sym:
        return None
    side = str(d.get("side") or "BUY").upper()
    qty_raw = int(d.get("qty") or 0)
    if qty_raw == 0:
        return None
    parsed = parse_tradingsymbol(sym)
    leg = Leg(
        symbol=sym,
        qty=abs(qty_raw) if side == "BUY" else -abs(qty_raw),
        side=side,
        avg_cost=float(d.get("avg_cost") or 0.0),
        ltp=float(d.get("ltp") or 0.0),
        exchange=str(d.get("exchange") or "NFO"),
        parsed=parsed,
    )
    return leg


def result_to_dict(r: OptimizeResult) -> dict:
    """Serialise for JSON response — dataclasses don't auto-encode."""
    return {
        "computed_at":           r.computed_at,
        "ttl_remaining_seconds": r.ttl_remaining_seconds,
        "request_hash":          r.request_hash,
        "current":               asdict(r.current),
        "alternatives":          [
            {**asdict(a), "metrics": asdict(a.metrics)} for a in r.alternatives
        ],
        "notes":                 list(r.notes),
    }
