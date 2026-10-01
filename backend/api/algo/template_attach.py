"""
Template attachment — turns an OrderTemplate + parent-order context into
a concrete plan (TP/SL GTT + protective wing leg), then applies the
plan in either the sim or live path.

Two-step contract so the UI can `preview` before commit:

  resolve_template_plan(template, overrides, parent_order_ctx) → TemplatePlan
      Pure data — no broker calls, no DB writes. Operator sees this
      first via /api/orders/ticket/preview so they know exactly what
      will be placed.

  apply_plan_sim(plan, driver, parent_order_id) → AttachResult
  apply_plan_live(plan, broker, parent_order_id) → AttachResult
      Side-effecting — sim path routes to SimGttBook + SimDriver's
      paper engine for the wing; live path routes to KiteBroker.place_gtt
      + a parallel basket call for the wing.

Industry analogue: NinjaTrader ATM Strategy attachment, IBKR Bracket Order
expansion. Same shape — preview shows planned children before submit;
commit fans them out.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from backend.api.routes.orders_helpers import _snap_to_tick
from backend.brokers.capabilities import BrokerCapabilities
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Plan dataclasses ─────────────────────────────────────────────────

@dataclass
class GttSpec:
    """One GTT (TP-only, SL-only, or combined OCO). Trigger values and
    orders are aligned: orders[i] fires when trigger_values[i] crosses.
    For two-leg OCO on Kite/Dhan we pack TP at index 0, SL at index 1."""
    trigger_type:   str            # 'single' | 'two-leg'
    trigger_values: list[float]
    orders:         list[dict]
    label:          str = ""       # 'TP' / 'SL' / 'TP+SL' — operator-visible
    # Set during apply_plan_* — the broker / sim GTT id assigned at place.
    placed_id:      Optional[str] = None
    # Phase 3B — when this GTT carries a trailing stop, the background
    # poller (_task_trail_stop) ratchets the SL trigger toward LTP.
    # `sl_trail_pct` (% distance) flows through to attached_gtts_json
    # so the poller can resume across restarts. None on TP-only legs.
    sl_trail_pct:   Optional[float] = None


@dataclass
class WingSpec:
    """Protective/offset leg attached opposite the parent's option entry.

    Two directions (see `_wing_direction`):
      • SELL-option parent (short) → BUY wing, order_type=MARKET. This is
        the original "hedge" leg — `estimated_price` here is a COSMETIC
        heuristic only (template's wing_premium_pct of parent price, or
        the chain-scan's picked LTP at scan time); the real fill price
        comes from the broker/paper-engine market fill, never from this
        field. `limit_price` is always None on this branch.
      • BUY-option parent (long) → SELL offset leg, order_type=LIMIT.
        Operator-confirmed: never MARKET for this direction. Here
        `estimated_price` (mirrored into `limit_price`) IS the REAL
        broker-bound limit price — the chain-scan's (or manual-offset
        quote lookup's) picked LTP, tick-snapped. A WingSpec is only
        ever built for this direction when a live quote was resolved —
        there is no price-less fallback, because a LIMIT order with no
        price is rejected by every broker.

    Symbol is computed from the parent's strike + template's
    wing_strike_offset (CE wing is +offset, PE wing is -offset) or from
    the wing_premium_pct chain scan (`_pick_wing_by_premium`) — same
    strike-selection mechanism for both directions. Quantity matches the
    parent so the spread net-margin is properly bounded.
    """
    tradingsymbol:    str
    transaction_type: str = "BUY"
    quantity:         int = 0
    exchange:         str = "NFO"
    product:          str = "NRML"
    order_type:       str = "MARKET"   # market-take for the hedge direction; LIMIT for the offset direction
    estimated_price:  Optional[float] = None
    # Real broker-bound LIMIT price for the offset-SELL direction only.
    # None on the MARKET hedge direction (where `estimated_price` is
    # cosmetic). Kept as a distinct field so callers can tell "this is a
    # real order price" from "this is a preview estimate" without having
    # to branch on `order_type` — mirrors `estimated_price` by value
    # whenever order_type == "LIMIT".
    limit_price:      Optional[float] = None
    placed_id:        Optional[str] = None


@dataclass
class TemplatePlan:
    template_id:        Optional[int]
    template_name:      str
    template_slug:      Optional[str]
    parent_account:     str
    parent_symbol:      str
    parent_side:        str
    parent_qty:         int
    parent_exchange:    str
    parent_fill_price:  float
    # lot_size for MCX/NCO qty translation at apply time. Non-MCX = 1 (no-op).
    # Populated in apply_template_to_order via get_lot_size() before the plan
    # is resolved — keeps resolve_template_plan sync (pure data).
    parent_lot_size:    int = 1
    # tick_size for TP/SL trigger + LIMIT-offset snapping at apply time.
    # 0.0 = unknown/cache-miss — snap helpers (`_snap_trigger_price`,
    # `_tp_limit_offset`) no-op to plain round(x, 2), the pre-fix
    # behavior. Populated in apply_template_to_order via
    # `_resolve_tick_size_for_order()` before the plan is resolved —
    # keeps resolve_template_plan sync (pure data), same pattern as
    # parent_lot_size.
    parent_tick_size:   float = 0.0
    gtts:               list[GttSpec] = field(default_factory=list)
    wing:               Optional[WingSpec] = None
    notes:              list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "template_id":        self.template_id,
            "template_name":      self.template_name,
            "template_slug":      self.template_slug,
            "parent_account":     self.parent_account,
            "parent_symbol":      self.parent_symbol,
            "parent_side":        self.parent_side,
            "parent_qty":         self.parent_qty,
            "parent_exchange":    self.parent_exchange,
            "parent_fill_price":  self.parent_fill_price,
            "parent_lot_size":    self.parent_lot_size,
            "parent_tick_size":   self.parent_tick_size,
            "gtts":               [asdict(g) for g in self.gtts],
            "wing":               asdict(self.wing) if self.wing else None,
            "notes":              list(self.notes),
        }


@dataclass
class AttachResult:
    plan:              TemplatePlan
    gtt_ids:           list[str] = field(default_factory=list)
    wing_order_id:     Optional[str] = None
    sibling_pairs:     list[tuple[str, str]] = field(default_factory=list)
    errors:            list[str] = field(default_factory=list)
    # Set when _fire_guard_alert fired (applies_to mismatch path).
    # Structural note: apply_template_to_order returns None on guard
    # fire — so errors and guard_alert_fired are mutually exclusive in
    # practice. The flag is defensive documentation only.
    guard_alert_fired:    bool = False
    # Set when the wing scan returned no candidate (chain empty, all OI below
    # threshold, quote failure, etc.). Surfaced in the API response so the
    # operator and alert channel can see WHY the wing wasn't attached.
    wing_skipped_reason:  Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "plan":               self.plan.to_dict(),
            "gtt_ids":            list(self.gtt_ids),
            "wing_order_id":      self.wing_order_id,
            "sibling_pairs":      [list(p) for p in self.sibling_pairs],
            "errors":             list(self.errors),
            "wing_skipped_reason": self.wing_skipped_reason,
        }


# ── Strike + wing maths ──────────────────────────────────────────────

# Kite F&O option symbols: NIFTY25APR22000CE / NIFTY2542422000CE etc.
# Captures: root, strike, opt_type.
_OPT_SYM_RE = re.compile(
    r"^(?P<root>[A-Z]+?)"
    r"(?P<expiry_token>\d{2}[A-Z]{3}|\d{4,5})"
    r"(?P<strike>\d+(?:\.\d+)?)"
    r"(?P<opt>CE|PE)$"
)


def _fire_guard_alert(*, template_slug: str, applies_to: str,
                       parent_side: str, parent_symbol: str,
                       parent_account: str, parent_qty: int,
                       parent_fill_price: float,
                       parent_order_id: Optional[int],
                       reason: str) -> None:
    """Fire a Telegram + email alert when the applies_to guard
    refuses an attach. Out-of-band via asyncio.create_task so the
    fill-path latency stays untouched. Every failure mode logs +
    drops; never blocks the fill pipeline.

    Operator visibility goals:
    - Telegram ping ≤ 30 s after the guard fire (operator sees it
      on their phone immediately).
    - Email lands in the alert inbox (durable record for end-of-day
      review).
    - Both messages name the parent order id + symbol + side + qty
      + fill price so the operator can find the position and decide
      whether to arm exits manually.
    """
    import asyncio as _asyncio
    import html as _html
    from datetime import datetime, timezone, timedelta

    # Format IST timestamp inline (no dependency on the heavier
    # alert_utils.timestamp_display).
    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    ist_label = ist.strftime("%a, %b %d %Y, %H:%M IST")

    summary = (
        f"Refused to attach template '{template_slug}' "
        f"(applies_to={applies_to}) to parent order "
        f"#{parent_order_id} — {parent_side} {parent_qty} "
        f"{parent_symbol} @ ₹{parent_fill_price:.2f} on {parent_account}. "
        f"Reason: {reason}. Parent order is filled; EXITS NOT ATTACHED."
    )

    # Escape dynamic fields BEFORE embedding in the HTML message — this
    # string reaches ntfy via _html_to_plain(), whose tag-strip regex
    # (`<[^>]+>`) treats any unescaped literal '<' in the data as a tag
    # boundary and deletes everything up to the NEXT '>' anywhere later
    # in the string (e.g. a guard reason like "qty < lot_size" would eat
    # the rest of the message). Matches the one call site (_dispatch's
    # tg_table) that already escapes correctly (2026-09-27 council audit,
    # Bug 2).
    # str() first — these are typed `str` in the signature but a caller
    # can pass None in practice (e.g. `template.get("slug")` on a DB row
    # with slug=NULL is `None`, not the dict-default "?" — .get()'s
    # default only applies when the KEY is absent). html.escape(None)
    # raises AttributeError on this fire-and-forget alert path
    # (documented "never blocks the fill pipeline"); str(None) == "None"
    # matches the pre-fix f-string rendering exactly.
    reason_html        = _html.escape(str(reason))
    applies_to_html    = _html.escape(str(applies_to))
    template_slug_html = _html.escape(str(template_slug))

    tg_msg = (
        f"<b>⚠ Template guard fired — {ist_label}</b>\n\n"
        f"<code>"
        f"order #{parent_order_id}\n"
        f"{parent_side} {parent_qty} {parent_symbol}\n"
        f"@ ₹{parent_fill_price:.2f}  ({parent_account})\n\n"
        f"template:    {template_slug_html}\n"
        f"applies_to:  {applies_to_html}\n"
        f"reason:      {reason_html}\n\n"
        f"Parent order FILLED. Exits NOT attached.\n"
        f"Arm exits manually if needed.\n\n"
        f"Fix: at /admin/templates, change this template's "
        f"'applies_to' to 'both' or 'buy_option'.</code>"
    )

    def _dispatch_guard() -> None:
        try:
            from backend.shared.helpers.alert_utils import _alert_route
            _alert_route(
                'template_guard',
                title="Template guard fired",
                body=tg_msg,
                # template_guard routing has email:false — email_fn ignored
            )
        except Exception as e:
            logger.warning(f"guard alert: dispatch failed: {e}")

    async def _both():
        _dispatch_guard()

    try:
        _asyncio.get_running_loop().create_task(_both())
    except RuntimeError:
        _dispatch_guard()

    logger.info(f"guard alert dispatched: {summary}")


def _check_offhours_wing_gate(
    apply_path: str,
    parent_exchange: str,
    template: dict,
    parent_lot_size: int,
    parent_account: str,
    parent_symbol: str,
    parent_side: str,
    parent_qty: int,
    parent_fill_price: float,
    parent_order_id,
) -> "tuple[Optional[AttachResult], Optional[str]]":
    """(C1/#22) Return (early_result, offhours_note). early_result is non-None only
    when the template has a wing and exchange is closed — caller returns it immediately."""
    if apply_path not in ("live", "auto"):
        return None, None
    try:
        from backend.api.algo.agent_engine import _symbol_exchange_open, _build_now_ctx
        if _symbol_exchange_open(parent_exchange, _build_now_ctx()):
            return None, None
        if _template_has_wing(template):
            _err_msg = (
                f"Exchange {parent_exchange} closed — "
                "wing MARKET leg requires open market; attach deferred"
            )
            logger.warning("[TEMPLATE-GUARD] %s", _err_msg)
            _plan = TemplatePlan(
                template_id=template.get("id"),
                template_name=template.get("name") or "(unnamed)",
                template_slug=template.get("slug"),
                parent_account=parent_account,
                parent_symbol=parent_symbol,
                parent_side=parent_side,
                parent_qty=parent_qty,
                parent_exchange=parent_exchange,
                parent_fill_price=float(parent_fill_price),
                parent_lot_size=parent_lot_size,
            )
            _res = AttachResult(plan=_plan)
            _res.errors.append(_err_msg)
            _fire_attach_fail_alert(order_id=parent_order_id, symbol=parent_symbol, account=parent_account, errors=[_err_msg])
            return _res, None
        note = (
            f"GTT registered off-hours ({parent_exchange} closed) — "
            "will activate at next session open"
        )
        return None, note
    except Exception as _e:
        logger.warning("apply_template_to_order: market-hours check failed: %s", _e)
        return None, None


def _fire_wing_unprotected_alert(
    wing_skipped_reason: str,
    result,
    parent_order_id,
    parent_symbol: str,
    parent_exchange: str,
) -> None:
    """(#28B) Fire urgent ntfy when GTTs placed but wing failed post-fill."""
    msg = (
        f"GTTs placed (ids: {result.gtt_ids}) but wing failed: "
        f"{wing_skipped_reason} | order #{parent_order_id} "
        f"{parent_symbol} {parent_exchange}"
    )
    logger.warning("[WING-UNPROTECTED] %s", msg)
    try:
        from backend.shared.helpers.alert_utils import send_ntfy_alert
        send_ntfy_alert("Unprotected SELL position", msg, priority="urgent")
    except Exception as _e:
        logger.warning("wing unprotected ntfy alert failed: %s", _e)


def _fire_attach_fail_alert(
    *,
    order_id: Optional[int],
    symbol: str,
    account: str,
    errors: list,
) -> None:
    """Fire a Telegram-only alert when template attach fails due to an
    operational error (G1 guard, lot_size cache miss, broker placement
    failure, etc.) AFTER the parent order has filled.

    Telegram-only (not email) — these are operational failures, not
    security incidents. The operator needs an immediate ping so they
    can manually arm exits. Email reserved for the security-pattern
    applies_to guard alerts.

    Mutually exclusive with _fire_guard_alert: the applies_to guard
    returns None from apply_template_to_order (no AttachResult), so
    this alert only fires when an AttachResult with errors exists and
    guard_alert_fired is False.
    """
    import asyncio as _asyncio
    import html as _html
    from datetime import datetime, timezone, timedelta

    now_utc = datetime.now(timezone.utc)
    ist = now_utc + timedelta(hours=5, minutes=30)
    ist_label = ist.strftime("%a, %b %d %Y, %H:%M IST")

    err_summary = "; ".join((str(e) for e in errors[:2]))
    # Escape before embedding — see _fire_guard_alert's comment above for
    # the exact ntfy _html_to_plain() regression this guards against
    # (2026-09-27 council audit, Bug 2). err_summary is raw broker/guard
    # error text and may contain a literal '<' (e.g. "qty < lot_size").
    err_summary_html = _html.escape(err_summary)

    tg_msg = (
        f"<b>⚠ Template attach failed — {ist_label}</b>\n\n"
        f"<code>"
        f"order #{order_id}\n"
        f"symbol:   {symbol}\n"
        f"account:  {account}\n\n"
        f"errors:   {err_summary_html}\n\n"
        f"Parent order FILLED. Exits NOT attached.\n"
        f"Arm exits manually if needed.</code>"
    )

    def _dispatch_attach_fail() -> None:
        try:
            from backend.shared.helpers.alert_utils import _alert_route
            _alert_route(
                'template_attach_fail',
                title="Template attach failed",
                body=tg_msg,
            )
        except Exception as _e:
            logger.warning(f"attach fail alert: dispatch failed: {_e}")

    async def _task():
        _dispatch_attach_fail()

    try:
        _asyncio.get_running_loop().create_task(_task())
    except RuntimeError:
        _dispatch_attach_fail()

    logger.warning(
        "attach fail alert dispatched: order #%s %s %s errors=[%s]",
        order_id, symbol, account, err_summary,
    )


def _is_sell_option(side: str, symbol: str) -> bool:
    """SELL + parseable option symbol. Drives wing attach."""
    return side == "SELL" and bool(_OPT_SYM_RE.match(symbol.upper()))


def _wing_direction(parent_side: str, parent_symbol: str) -> Optional[tuple[str, str]]:
    """Return ``(wing_transaction_type, wing_order_type)`` for the leg
    attached opposite the parent's option entry, or ``None`` when the
    parent isn't a recognisable option entry (futures/equity on either
    side, or an unparseable symbol, never get a wing).

      • SELL option parent (short) → ``("BUY", "MARKET")``  — original
        protective hedge, unchanged.
      • BUY option parent (long)   → ``("SELL", "LIMIT")``  — new offset
        leg (operator-confirmed: this direction is never MARKET).

    Reuses `_is_sell_option` for the first branch so both stay in sync.
    """
    if _is_sell_option(parent_side, parent_symbol):
        return "BUY", "MARKET"
    if parent_side == "BUY" and bool(_OPT_SYM_RE.match(parent_symbol.upper())):
        return "SELL", "LIMIT"
    return None


def _wing_symbol(parent_symbol: str, offset: int) -> Optional[str]:
    """Compute the protective wing tradingsymbol for a SELL option.

    For SELL CE @ strike K, wing is BUY CE @ K + offset.
    For SELL PE @ strike K, wing is BUY PE @ K - offset.
    Returns None when the parent symbol isn't a recognisable option.
    """
    m = _OPT_SYM_RE.match(parent_symbol.upper())
    if not m:
        return None
    root         = m.group("root")
    expiry_token = m.group("expiry_token")
    strike       = int(float(m.group("strike")))
    opt          = m.group("opt")
    wing_strike  = strike + offset if opt == "CE" else strike - offset
    if wing_strike <= 0:
        return None
    return f"{root}{expiry_token}{wing_strike}{opt}"


def _ta_wing_depth_spread(q: dict, ltp: float) -> float:
    """Compute bid-ask spread% from broker quote depth. Returns 0.0 when
    depth is absent or either side is zero (thin books are not penalised)."""
    depth = q.get("depth") or {}
    buys  = depth.get("buy") or []
    sells = depth.get("sell") or []
    bid = float(buys[0].get("price") if buys else 0) or 0.0
    ask = float(sells[0].get("price") if sells else 0) or 0.0
    return (ask - bid) / ltp * 100.0 if (ask > 0 and bid > 0) else 0.0


def _wing_score_candidate(
    q: dict,
    target_premium: float,
) -> Optional[tuple[float, int, float, float]]:
    """Extract scoring fields from a single broker quote dict *q*.

    Returns ``(ltp, oi, spread_pct, score)`` when the quote has a positive
    LTP, or ``None`` when the quote is absent / zero (candidate skipped).

    Score formula: ``abs(ltp − target) + (spread_pct / 100) × target``.
    A lower score is better (closer premium, tighter spread).
    ``spread_pct`` is ``0.0`` when depth data is absent so thin-book
    options are not unfairly penalised.
    """
    ltp = float(q.get("last_price") or 0)
    if ltp <= 0:
        return None
    oi         = int(q.get("oi") or 0)
    spread_pct = _ta_wing_depth_spread(q, ltp)
    dist       = abs(ltp - target_premium)
    score      = dist + (spread_pct / 100.0) * target_premium
    return ltp, oi, spread_pct, score


class _WingHardRejectError(Exception):
    """Raised by _wing_scan_candidates when the OI hard-reject threshold is
    triggered (#10): all scanned candidates have OI below the configured
    `templates.wing_min_oi_hard_reject` floor. Caught by _pick_wing_by_premium
    and converted to a (None, None, reason) return + ntfy alert."""
    pass


def _wing_scan_candidates(
    candidates: list[dict],
    quote_data: dict,
    target_premium: float,
    min_oi: int,
    max_spread_pct: float,
    hard_reject_oi: int = 0,
) -> tuple[Optional[dict], Optional[dict], int, int, int]:
    """Score every candidate in *candidates* against *target_premium*.

    Returns ``(best, fallback, scanned, dropped_oi, dropped_spread)``:

    * ``best``      — highest-scoring candidate that also passed OI / spread
                      hard filters; ``None`` when every candidate failed.
    * ``fallback``  — highest-scoring candidate *ignoring* the hard filters,
                      used when ``best is None`` (stock options with thin OI).
    * ``scanned``   — count of candidates with a positive LTP.
    * ``dropped_oi``     — candidates dropped by the OI gate.
    * ``dropped_spread`` — candidates dropped by the spread% gate.

    Score formula: ``abs(ltp − target) + (spread_pct / 100) × target``.
    A lower score is better (closer premium + tighter spread).

    `hard_reject_oi` (#10): when > 0 and ALL scanned candidates have OI
    below this threshold, raises ``_WingHardRejectError`` so the caller
    can fire an ntfy alert and skip the fallback path entirely.
    """
    best: Optional[dict] = None
    best_score = float("inf")
    # Filter-relaxed fallback — best candidate by premium score
    # ignoring OI / spread gates. Stock options (e.g. DIXON) have
    # OI of a few hundred per strike, so the index-tuned min_oi=1000
    # default drops every candidate. When that happens we still want
    # a wing attached; we keep the filter-passing winner if any, and
    # fall back to this when nothing passes.
    fallback: Optional[dict] = None
    fallback_score = float("inf")
    scanned = dropped_oi = dropped_spread = 0
    max_oi_seen = 0
    for c in candidates:
        key = f"{c['exch']}:{c['ts']}"
        q = quote_data.get(key) or {}
        scored = _wing_score_candidate(q, target_premium)
        if scored is None:
            continue
        ltp, oi, spread_pct, score = scored
        scanned += 1
        if oi > max_oi_seen:
            max_oi_seen = oi
        # Track best-overall (ignoring filters) for the fallback path.
        if score < fallback_score:
            fallback_score = score
            fallback = {**c, "ltp": ltp, "oi": oi, "spread_pct": spread_pct}
        # Hard filters — OI / spread — preferred winner.
        if min_oi > 0 and oi < min_oi:
            dropped_oi += 1
            continue
        if max_spread_pct < 100 and spread_pct > max_spread_pct:
            dropped_spread += 1
            continue
        if score < best_score:
            best_score = score
            best = {**c, "ltp": ltp, "oi": oi, "spread_pct": spread_pct}
    # Hard OI reject (#10): if every candidate's OI is below the hard floor,
    # refuse the fallback path entirely — a zero-OI wing has no real liquidity
    # and the order will almost certainly reject or fill at terrible prices.
    if hard_reject_oi > 0 and scanned > 0 and max_oi_seen < hard_reject_oi:
        raise _WingHardRejectError(
            f"wing hard-reject: all {scanned} candidate(s) have OI "
            f"below hard floor {hard_reject_oi} (max OI seen: {max_oi_seen})"
        )
    return best, fallback, scanned, dropped_oi, dropped_spread


def _ta_wing_filter_candidates(
    insts_resp,
    parent_prefix: str,
    suffix: str,
) -> list[dict]:
    """Filter instruments cache to chain entries matching prefix + suffix."""
    result: list[dict] = []
    for inst in (insts_resp.items if insts_resp else []):
        ts = str(inst.s).upper()
        if not ts.startswith(parent_prefix):
            continue
        if not ts.endswith(suffix):
            continue
        if inst.k is None:
            continue
        result.append({"ts": ts, "strike": float(inst.k), "exch": inst.e})
    return result


def _ta_wing_slice_radius(
    candidates: list[dict],
    parent_strike: int,
    chain_radius: int,
) -> list[dict]:
    """Return the subset of *candidates* within *chain_radius* strikes of
    *parent_strike*. Candidates must already be sorted by strike ascending."""
    parent_idx = next(
        (i for i, c in enumerate(candidates) if c["strike"] == parent_strike),
        None,
    )
    if parent_idx is None:
        return candidates
    lo = max(0, parent_idx - chain_radius)
    hi = min(len(candidates), parent_idx + chain_radius + 1)
    return candidates[lo:hi]


def _wing_build_chain(
    insts_resp,
    root: str,
    expiry_token: str,
    opt: str,
    parent_strike: int,
    chain_radius: int,
) -> tuple[list[dict], Optional[str]]:
    """Filter the instruments cache to the matching option chain and
    apply the radius slice around *parent_strike*.

    Returns ``(candidates, error_reason)``:

    * On success: ``(non-empty list, None)``
    * On failure: ``([], reason_string)``

    The reason strings match the substrings asserted by the test suite
    ("no chain candidates", "chain_radius filter eliminated").
    """
    parent_prefix = f"{root}{expiry_token}"
    suffix        = opt   # 'CE' or 'PE'
    candidates = _ta_wing_filter_candidates(insts_resp, parent_prefix, suffix)

    if not candidates:
        return [], (
            f"wing_premium_pct skipped — no chain candidates found "
            f"for {root}{expiry_token}{suffix}"
        )

    candidates.sort(key=lambda c: c["strike"])
    candidates = _ta_wing_slice_radius(candidates, parent_strike, chain_radius)

    if not candidates:
        return [], (
            "wing_premium_pct skipped — chain_radius filter eliminated "
            "all candidates"
        )

    return candidates, None


async def _wing_fetch_quotes(
    candidates: list[dict],
    parent_exchange: str,
) -> tuple[dict, Optional[str]]:
    """Batch-fetch broker quotes for all *candidates* in one round-trip.

    Offloads the synchronous broker call to a thread so the event loop
    is not blocked during the network round-trip.

    Returns ``(quote_data, error_reason)``:

    * On success: ``(dict keyed by "EXCH:SYMBOL", None)``
    * On failure: ``({}, reason_string)``
    """
    quote_keys = [f"{c['exch']}:{c['ts']}" for c in candidates]
    try:
        import asyncio as _aio
        from backend.brokers.registry import get_market_data_broker
        broker = get_market_data_broker()
        quote_data = (
            await _aio.to_thread(broker.quote, quote_keys)
        ) or {}
        return quote_data, None
    except Exception as e:
        return {}, (
            f"wing_premium_pct skipped — broker.quote() failed: {e}"
        )


async def _pick_wing_by_premium(
    parent_symbol:    str,
    parent_exchange:  str,
    parent_fill_price: float,
    wing_premium_pct: float,
) -> tuple[Optional[str], Optional[float], str]:
    """Scan the option chain and pick a wing strike whose premium is
    closest to `parent_fill_price × wing_premium_pct / 100`, subject
    to liquidity filters from `/admin/settings` (`templates.wing_*`).

    Returns `(wing_tradingsymbol, picked_ltp, reason)`:
      • wing_tradingsymbol — picked strike's tradingsymbol, or None
      • picked_ltp         — its current LTP, or None
      • reason             — human-readable note for plan.notes (always
                              populated so the operator sees what
                              happened, even on the success path)

    Algorithm:
      1. Parse parent symbol → root, expiry, parent_strike, opt_type.
         Bail if unparseable.
      2. Read settings: min OI, max spread%, chain radius.
      3. Pull the cached instruments list, filter to same
         (root, expiry, opt_type), sort by strike, slice to
         `[parent_strike − radius × tick, parent_strike + radius × tick]`.
      4. Batched broker.quote() across every candidate's key.
      5. Score each: `abs(ltp − target_premium)` with a penalty if the
         spread% exceeds the threshold. Drop candidates that fail OI.
      6. Pick min score. Return tradingsymbol + ltp.

    All errors are caught and converted to a (None, None, reason)
    fallback — the plan resolver treats that as "no wing attached" and
    surfaces the reason via plan.notes. The parent order is NEVER
    blocked by a chain-scan failure.
    """
    target_premium = parent_fill_price * float(wing_premium_pct) / 100.0
    if target_premium <= 0:
        return None, None, (
            f"wing_premium_pct skipped — target premium "
            f"({target_premium:.2f}) not positive"
        )

    m = _OPT_SYM_RE.match(parent_symbol.upper())
    if not m:
        return None, None, (
            f"wing_premium_pct skipped — parent symbol {parent_symbol!r} "
            f"unparseable"
        )
    root         = m.group("root")
    expiry_token = m.group("expiry_token")
    parent_strike = int(float(m.group("strike")))
    opt          = m.group("opt")

    # Settings — read inside the function so operator tunes apply
    # without a service restart.
    try:
        from backend.shared.helpers.settings import get_int, get_float
        min_oi          = get_int("templates.wing_min_oi", 1000)
        max_spread_pct  = get_float("templates.wing_max_spread_pct", 10.0)
        chain_radius    = get_int("templates.wing_chain_radius", 20)
    except Exception:
        min_oi, max_spread_pct, chain_radius = 1000, 10.0, 20

    # #10: hard-reject floor — 0 = disabled (default).
    try:
        from backend.shared.helpers.settings import get_int as _get_int2
        hard_reject_oi = _get_int2("templates.wing_min_oi_hard_reject", 0)
    except Exception:
        hard_reject_oi = 0

    # Resolve the cached instruments dump, filter to matching chain.
    try:
        from backend.api.cache import get_or_fetch
        from backend.api.routes.instruments import _fetch_instruments, _TTL_SECONDS
        insts_resp = await get_or_fetch(
            "instruments", _fetch_instruments, ttl_seconds=_TTL_SECONDS,
        )
    except Exception as e:
        return None, None, (
            f"wing_premium_pct skipped — instruments cache lookup "
            f"failed: {e}"
        )

    candidates, chain_err = _wing_build_chain(
        insts_resp, root, expiry_token, opt, parent_strike, chain_radius,
    )
    if chain_err:
        return None, None, chain_err

    quote_data, quote_err = await _wing_fetch_quotes(candidates, parent_exchange)
    if quote_err:
        return None, None, quote_err

    try:
        best, fallback, scanned, dropped_oi, dropped_spread = _wing_scan_candidates(
            candidates, quote_data, target_premium, min_oi, max_spread_pct,
            hard_reject_oi=hard_reject_oi,
        )
    except _WingHardRejectError as _hre:
        # #10: all candidates below hard OI floor — refuse fallback path,
        # fire ntfy alert, and return a skip reason.
        _hr_reason = str(_hre)
        logger.critical(
            "[WING-HARD-REJECT] %s (parent=%s, exch=%s)",
            _hr_reason, parent_symbol, parent_exchange,
        )
        try:
            from backend.shared.helpers.alert_utils import send_ntfy_alert
            send_ntfy_alert(
                "Wing scan hard-rejected",
                f"{_hr_reason} | {parent_symbol} {parent_exchange} "
                f"target ₹{target_premium:.2f}",
                priority="urgent",
            )
        except Exception as _na:
            logger.warning("wing hard-reject ntfy alert failed: %s", _na)
        return None, None, f"wing_premium_pct hard-reject: {_hr_reason}"

    used_fallback = False
    if best is None:
        if fallback is None:
            return None, None, (
                f"wing_premium_pct skipped — scanned {scanned}, "
                f"dropped_oi={dropped_oi}, dropped_spread={dropped_spread} "
                f"(target ₹{target_premium:.2f})"
            )
        best = fallback
        used_fallback = True

    if used_fallback:
        reason = (
            f"wing picked by premium% (fallback — every candidate failed "
            f"OI≥{min_oi}/spread≤{max_spread_pct:g}%; scanned {scanned}, "
            f"dropped_oi={dropped_oi}, dropped_spread={dropped_spread}): "
            f"{best['ts']} @ ₹{best['ltp']:.2f} "
            f"(target ₹{target_premium:.2f}, OI {best['oi']}, "
            f"spread {best['spread_pct']:.1f}%)"
        )
    else:
        reason = (
            f"wing picked by premium% — {best['ts']} @ ₹{best['ltp']:.2f} "
            f"(target ₹{target_premium:.2f}, OI {best['oi']}, "
            f"spread {best['spread_pct']:.1f}%)"
        )
    return best["ts"], float(best["ltp"]), reason


# ── Trigger-price computation ────────────────────────────────────────

def _snap_trigger_price(raw: float, tick_size: float) -> float:
    """Snap a computed TP/SL trigger price to the instrument's tick grid.

    No-op (plain round(x, 2)) when tick_size is unknown/non-positive —
    preserves the pre-fix behavior exactly so callers that don't resolve
    a tick_size (e.g. existing test fixtures) see byte-identical output.

    Floor-at-one-tick guard: a low-premium contract's trigger can
    compute to a near-zero value that snaps DOWN to exactly 0 on the
    tick grid — Kite's place_gtt rejects `trigger_value <= 0`. Clamp up
    to one tick above zero instead of letting a 0 reach the broker.
    """
    if not tick_size or tick_size <= 0:
        return round(raw, 2)
    snapped = _snap_to_tick(raw, tick_size)
    if snapped <= 0:
        snapped = round(tick_size, 4)
    return snapped


def _tp_trigger(parent_side: str, fill_price: float, tp_pct: Optional[float],
                instrument_type: str = "", tick_size: float = 0.0) -> Optional[float]:
    """Convert template's tp_pct into an absolute price.

    BUY parent: TP fires above (long unwinds at gain). fill × (1 + tp%/100).
    SELL parent: TP fires below (short unwinds at gain). fill × (1 - tp%/100).

    `fill_price` must be strictly positive — a zero fill price produces a
    trigger at 0 or below which Kite will reject. (#9)
    `instrument_type` is informational — logged for MCX futures as a
    sanity reminder that lot-size translation must already be applied. (#9)
    `tick_size`, when known (>0), snaps the computed trigger to the
    instrument's tick grid via `_snap_trigger_price` — Kite rejects a
    trigger that isn't an exact multiple of tick_size. Unknown/0
    tick_size preserves the pre-fix plain round(x, 2) behavior exactly.
    """
    if tp_pct is None:
        return None
    assert fill_price > 0, (
        f"_tp_trigger: fill_price must be positive, got {fill_price!r}"
    )
    if instrument_type.upper() == "FUTMCX":
        logger.debug(
            "_tp_trigger: FUTMCX instrument — confirm lot-size translation "
            "applied before this call (fill_price=%.2f, tp_pct=%.2f)",
            fill_price, tp_pct,
        )
    sign = 1.0 if parent_side == "BUY" else -1.0
    raw = fill_price * (1.0 + sign * float(tp_pct) / 100.0)
    return _snap_trigger_price(raw, tick_size)


def _sl_trigger(parent_side: str, fill_price: float, sl_pct: Optional[float],
                instrument_type: str = "", tick_size: float = 0.0) -> Optional[float]:
    """SL fires opposite side of TP — protects against adverse move.

    BUY parent: SL fires below entry. fill × (1 - sl%/100).
    SELL parent: SL fires above entry. fill × (1 + sl%/100).

    `fill_price` must be strictly positive — a zero fill price produces a
    trigger at 0 or below which Kite will reject. (#9)
    `instrument_type` is informational — logged for MCX futures. (#9)
    `tick_size` — see `_tp_trigger`'s docstring; same snap convention.
    """
    if sl_pct is None:
        return None
    assert fill_price > 0, (
        f"_sl_trigger: fill_price must be positive, got {fill_price!r}"
    )
    if instrument_type.upper() == "FUTMCX":
        logger.debug(
            "_sl_trigger: FUTMCX instrument — confirm lot-size translation "
            "applied before this call (fill_price=%.2f, sl_pct=%.2f)",
            fill_price, sl_pct,
        )
    sign = 1.0 if parent_side == "BUY" else -1.0
    raw = fill_price * (1.0 - sign * float(sl_pct) / 100.0)
    return _snap_trigger_price(raw, tick_size)


# ── Plan resolution ──────────────────────────────────────────────────

def _close_side(parent_side: str) -> str:
    """The side a TP/SL exit must use to flatten the parent's position.
    BUY parent → SELL on exit. SELL parent → BUY on exit."""
    return "SELL" if parent_side == "BUY" else "BUY"


def _parse_tp_scales(tp_scales_raw) -> list[dict]:
    """Parse the tp_scales_json field (string or list) into a list of
    validated scale dicts: [{at_pct: float, close_pct: float}, ...].
    Entries with non-positive or out-of-range values are silently dropped.
    Returns [] on any parse error."""
    import json as _json
    if not tp_scales_raw:
        return []
    tp_scales: list[dict] = []
    try:
        parsed = _json.loads(tp_scales_raw) if isinstance(tp_scales_raw, str) else tp_scales_raw
        if isinstance(parsed, list):
            for e in parsed:
                if not isinstance(e, dict):
                    continue
                try:
                    ap = float(e.get("at_pct"))
                    cp = float(e.get("close_pct"))
                except (TypeError, ValueError):
                    continue
                if ap > 0 and 0 < cp <= 100:
                    tp_scales.append({"at_pct": ap, "close_pct": cp})
    except Exception:
        tp_scales = []
    return tp_scales


def _ta_pick_float_override(key: str, ov: dict, template: dict) -> Optional[float]:
    """Return float value for *key*, preferring *ov* over *template*. None when absent."""
    v = ov.get(key)
    if v is None:
        v = template.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ta_validate_pct(
    name: str, value: Optional[float], notes: list[str]
) -> Optional[float]:
    """Return *value* unchanged when positive; None + note when non-positive."""
    if value is not None and value <= 0:
        notes.append(f"{name}={value} is not positive — {name.split('_')[0].upper()} not attached")
        return None
    return value


def _parse_template_overrides(
    template: dict,
    overrides: dict,
) -> tuple[
    Optional[float],   # tp_pct
    Optional[float],   # sl_pct
    Optional[float],   # wing_premium_pct
    Optional[float],   # sl_trail_pct
    list[dict],        # tp_scales
    Optional[int],     # wing_strike_offset
    str,               # tp_order_type
    list[str],         # validation_notes
]:
    """Extract and normalise all override fields from the operator's
    inline edits + saved template row. Override > template > None.

    Returns a flat tuple of resolved values + any pre-plan validation
    notes (e.g. tp_pct / sl_pct non-positive).
    """
    _ov = overrides or {}

    tp_pct           = _ta_pick_float_override("tp_pct",           _ov, template)
    sl_pct           = _ta_pick_float_override("sl_pct",           _ov, template)
    wing_premium_pct = _ta_pick_float_override("wing_premium_pct", _ov, template)
    sl_trail_pct     = _ta_pick_float_override("sl_trail_pct",     _ov, template)

    # wing_premium_pct must be strictly positive — a zero or negative value
    # produces a nonsensical target premium (≤ 0) and the scan always falls
    # through with no candidates found, silently placing no wing. Raise 422
    # so the operator sees the error before submit rather than an ambiguous
    # silent skip.
    if wing_premium_pct is not None and wing_premium_pct <= 0:
        from litestar.exceptions import HTTPException
        raise HTTPException(
            status_code=422,
            detail="wing_premium_pct must be > 0",
        )

    # Audit fix — non-positive % values silently produced an invalid GTT
    # (trigger == fill price on a BUY, immediately rejected by Kite or
    # firing instantly on a SELL). Drop to None + surface a note via
    # `_validation_notes` (appended to `plan.notes` once plan exists).
    _validation_notes: list[str] = []
    tp_pct = _ta_validate_pct("tp_pct", tp_pct, _validation_notes)
    sl_pct = _ta_validate_pct("sl_pct", sl_pct, _validation_notes)

    # tp_scales_json — Phase 3A scale-out targets. When set, supersedes
    # tp_pct: a TP ladder of N entries, each placed as a separate
    # single GTT at fill × (1 + at_pct/100), sized to parent_qty ×
    # close_pct/100. Sum of close_pct ≤ 100; the remainder stays
    # open with no auto-exit (operator's call).
    tp_scales_raw = _ov.get("tp_scales_json")
    if tp_scales_raw is None:
        tp_scales_raw = template.get("tp_scales_json")
    tp_scales = _parse_tp_scales(tp_scales_raw)

    wing_offset_raw = _ov.get("wing_strike_offset")
    if wing_offset_raw is None:
        wing_offset_raw = template.get("wing_strike_offset")
    try:
        wing_strike_offset: Optional[int] = (
            int(wing_offset_raw) if wing_offset_raw is not None else None
        )
    except (TypeError, ValueError):
        wing_strike_offset = None

    # tp_order_type — LIMIT (default) or MARKET. Override > template >
    # 'LIMIT'. SL legs always stay LIMIT (a MARKET SL = stop-market,
    # different semantics; would be a separate `sl_order_type` field).
    _tp_ot_raw = _ov.get("tp_order_type")
    if _tp_ot_raw is None:
        _tp_ot_raw = template.get("tp_order_type")
    tp_order_type = str(_tp_ot_raw).upper() if _tp_ot_raw else "LIMIT"
    if tp_order_type not in ("LIMIT", "MARKET"):
        tp_order_type = "LIMIT"

    return (
        tp_pct, sl_pct, wing_premium_pct, sl_trail_pct,
        tp_scales, wing_strike_offset, tp_order_type,
        _validation_notes,
    )


def _build_scale_out_gtts(
    tp_scales:         list[dict],
    parent_side:       str,
    parent_fill_price: float,
    parent_qty:        int,
    exit_side:         str,
    parent_product:    str,
    tp_order_type:     str,
    sl_trig:           Optional[float],
    sl_trail_pct:      Optional[float],
    lot_size:          int = 1,
    parent_exchange:   str = "",
    tick_size:         float = 0.0,
) -> tuple[list[GttSpec], list[str]]:
    """Build GTT specs + notes for the Phase 3A scale-out path.

    Integer qty allocation: floor each phase, add the leftover to the
    LAST phase so allocations always sum to parent_qty. Returns
    (gtts_to_add, notes_to_add).

    `lot_size` (#3): each scale qty is rounded DOWN to the nearest lot
    multiple so no sub-lot GTT leg ever reaches the broker and allocations
    can never exceed parent_qty. The last entry takes whatever remains
    (also floored to a lot multiple, never negative) — this guarantees
    `sum(allocations) <= parent_qty` structurally, by construction, so no
    allocation is ever negative. Any remaining qty lost to rounding is
    noted in plan.notes.

    `parent_exchange` (#1): forwarded to `_leg` so LIMIT TP legs apply the
    exchange-appropriate tick offset (NFO/BFO/CDS vs futures/others).

    `tick_size` (2026-10): forwarded to `_tp_trigger` (per-scale trigger
    snap) and `_leg` (LIMIT offset snap). 0.0 = unknown — no-op, exact
    pre-fix behavior.
    """
    gtts: list[GttSpec] = []
    notes: list[str] = []

    # Lot-size rounding (#3, fixed): every scale — including the last —
    # is floored to the nearest lot multiple. Rounding UP (the prior
    # behaviour) let cumulative non-last allocations overshoot parent_qty,
    # which forced the last scale's `parent_qty - used` remainder negative;
    # `min(rounded, negative)` then let that negative value through, was
    # silently dropped by the `if q <= 0: continue` guard below, and left
    # earlier over-sized GTTs live and unaccounted (e.g. 1-lot NIFTY (75)
    # with scales [40,40,20] rounded scale 0 and scale 1 UP to 75 each —
    # two live 75-qty TP GTTs against a single 75-share position). Flooring
    # makes over-allocation structurally impossible.
    _ls = max(1, int(lot_size or 1))

    def _round_down_lots(raw_q: int) -> int:
        if _ls <= 1 or raw_q <= 0:
            return max(0, raw_q)
        remainder = raw_q % _ls
        return raw_q - remainder

    allocations: list[int] = []
    used = 0
    for i, sc in enumerate(tp_scales):
        if i == len(tp_scales) - 1:
            # Last scale: take whatever remains (never negative — every
            # prior scale was floored, so `used` can never exceed
            # parent_qty), floored to a lot multiple.
            _raw = max(0, parent_qty - used)
            _rounded = _round_down_lots(_raw)
            allocations.append(_rounded)
        else:
            _raw = int((parent_qty * float(sc["close_pct"])) // 100)
            _rounded = _round_down_lots(_raw)
            allocations.append(_rounded)
            used += _rounded

    _total_alloc = sum(allocations)
    if _total_alloc != parent_qty and _ls > 1:
        notes.append(
            f"Scale-out lot rounding: allocated {_total_alloc} of "
            f"{parent_qty} contracts (lot_size={_ls}); "
            f"residual {parent_qty - _total_alloc} left open (no auto-exit)."
        )

    for sc, q in zip(tp_scales, allocations):
        if q <= 0:
            continue
        scale_trig = _tp_trigger(parent_side, parent_fill_price, float(sc["at_pct"]),
                                  tick_size=tick_size)
        if scale_trig is None:
            continue
        label = f"TP+{sc['at_pct']}% × {q}"
        gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[scale_trig],
            orders=[_leg(exit_side, q, scale_trig, parent_product, tp_order_type,
                         tp_offset_exchange=parent_exchange if tp_order_type == "LIMIT" else "",
                         tick_size=tick_size)],
            label=label,
        ))
    if sl_trig is not None:
        gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[sl_trig],
            orders=[_leg(exit_side, parent_qty, sl_trig, parent_product, "LIMIT",
                         tick_size=tick_size)],
            label="SL",
            sl_trail_pct=sl_trail_pct,
        ))
        # Audit fix (C-5) — explicit operator warning. When the SL
        # fires AFTER any scale-TP has already executed, the SL
        # order's full-parent-qty leg can over-sell the residual
        # position (e.g. scale-0 closed 30% → operator holds 70%;
        # SL fires for 100% of original → 30% oversell on a SELL
        # parent that flips long, or vice versa). Most brokers
        # silently size the GTT execution to available qty for
        # NRML (Kite does), so the over-sell is rare but real
        # under fast moves through TP+SL within one tick.
        notes.append(
            "⚠ SL is sized for full parent qty. If a scale TP "
            "fires before SL, the SL may try to close more than "
            "the residual position — broker's NRML quantity "
            "behavior typically caps at available, but verify "
            "on the broker's side. Recommend not pairing scale-"
            "out with SL unless the broker's GTT supports residual "
            "sizing."
        )
    notes.append(
        f"Scale-out: {len(tp_scales)} TP step(s) — "
        + " / ".join(f"+{s['at_pct']:g}% × {s['close_pct']:g}% qty"
                     for s in tp_scales)
        + ("; SL at single trigger for full qty" if sl_trig is not None else "")
    )
    return gtts, notes


def _build_tp_sl_gtts(
    tp_trig:         float,
    sl_trig:         float,
    exit_side:       str,
    parent_qty:      int,
    parent_product:  str,
    tp_order_type:   str,
    sl_trail_pct:    Optional[float],
    broker_caps:     Optional[BrokerCapabilities],
    parent_exchange: str = "",
    tick_size:       float = 0.0,
) -> tuple[list[GttSpec], list[str]]:
    """Build GTT specs for the combined TP+SL case.

    On Kite/Dhan (broker_caps.gtt_oco=True or caps=None): one two-leg
    OCO. On Groww (gtt_oco=False): two singles + a note. Returns
    (gtts_to_add, notes_to_add).

    `parent_exchange` (#1): forwarded to `_leg` for LIMIT TP legs to apply
    the exchange-appropriate tick offset.

    `tick_size` (2026-10): forwarded to `_leg` so the LIMIT offset snaps
    to the instrument's tick grid. 0.0 = unknown — no-op, exact
    pre-fix behavior.
    """
    _tp_exch = parent_exchange if tp_order_type == "LIMIT" else ""
    gtts: list[GttSpec] = []
    notes: list[str] = []
    # Operator wants both. On Kite/Dhan we pack as a two-leg OCO.
    # On Groww (no native OCO) we'd split into two singles — that's
    # done at the route layer when broker_caps.gtt_oco is False.
    if broker_caps is None or broker_caps.gtt_oco:
        gtts.append(GttSpec(
            trigger_type="two-leg",
            trigger_values=[tp_trig, sl_trig],
            orders=[
                _leg(exit_side, parent_qty, tp_trig, parent_product, tp_order_type,
                     tp_offset_exchange=_tp_exch, tick_size=tick_size),
                _leg(exit_side, parent_qty, sl_trig, parent_product, "LIMIT",
                     tick_size=tick_size),
            ],
            label="TP+SL",
            sl_trail_pct=sl_trail_pct,
        ))
    else:
        # Two singles + a note that the route layer will pair them
        # via SimGttBook.place(..., pair_with=...).
        gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[tp_trig],
            orders=[_leg(exit_side, parent_qty, tp_trig, parent_product, tp_order_type,
                         tp_offset_exchange=_tp_exch, tick_size=tick_size)],
            label="TP",
        ))
        gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[sl_trig],
            orders=[_leg(exit_side, parent_qty, sl_trig, parent_product, "LIMIT",
                         tick_size=tick_size)],
            label="SL",
            sl_trail_pct=sl_trail_pct,
        ))
        notes.append(
            f"{broker_caps.display_name} has no OCO — TP/SL placed as "
            f"two singles + paired so either fill cancels the other."
        )
    return gtts, notes


def _build_wing_spec(
    parent_side:       str,
    parent_symbol:     str,
    overrides:         dict,
    wing_strike_offset: Optional[int],
    wing_premium_pct:  Optional[float],
    parent_qty:        int,
    parent_exchange:   str,
    parent_product:    str,
    parent_fill_price: float,
    tick_size:         float = 0.0,
) -> tuple[Optional[WingSpec], list[str]]:
    """Build a WingSpec for the parent's option entry, or return (None, notes).

    Direction (hedge MARKET vs offset LIMIT) comes from `_wing_direction` —
    see `WingSpec`'s own docstring for the full contract. Priority within
    either direction: pre-resolved `_wing_picked_symbol`/`_wing_picked_ltp`
    (set by `apply_template_to_order`, either via the wing_premium_pct
    chain scan or — for the LIMIT offset direction only — a manual-offset
    live quote lookup) > `wing_strike_offset` (hedge/MARKET direction
    only — no live price needed there) > no wing.

    `tick_size` (>0 when known) snaps the LIMIT offset leg's price to the
    instrument's tick grid, same convention as `_snap_trigger_price`.
    """
    notes: list[str] = []
    direction = _wing_direction(parent_side, parent_symbol)
    if direction is None:
        return None, notes
    wing_txn_type, wing_order_type = direction
    is_limit = wing_order_type == "LIMIT"

    _ov = overrides or {}
    # Phase 1B — apply_template_to_order pre-resolves the wing via
    # _pick_wing_by_premium (or, for the LIMIT offset direction with a
    # manual wing_strike_offset, a single-symbol quote lookup) and seeds
    # the picked tradingsymbol + its live LTP back into overrides.
    wing_picked_sym = _ov.get("_wing_picked_symbol")
    wing_picked_ltp = _ov.get("_wing_picked_ltp")
    if wing_picked_sym:
        est = float(wing_picked_ltp) if wing_picked_ltp is not None else None
        limit_px: Optional[float] = None
        if is_limit:
            # The offset leg is a real order — never place a LIMIT with
            # no (or non-positive) price.
            if est is None or est <= 0:
                notes.append(
                    f"wing offset skipped — no valid live price for "
                    f"{wing_picked_sym}"
                )
                return None, notes
            limit_px = _snap_trigger_price(est, tick_size)
            est = limit_px
        return WingSpec(
            tradingsymbol=str(wing_picked_sym),
            transaction_type=wing_txn_type,
            quantity=parent_qty,
            exchange=parent_exchange,
            product=parent_product,
            order_type=wing_order_type,
            estimated_price=est,
            limit_price=limit_px,
        ), notes

    if wing_strike_offset is not None:
        if is_limit:
            # BUY-parent offset leg needs a REAL broker-bound price.
            # A manual wing_strike_offset with no resolved live quote
            # (apply_template_to_order's offset-quote lookup failed, or
            # wasn't reached) cannot safely place a LIMIT order — never
            # fall back to the cosmetic wing_premium_pct estimate as a
            # real price, and never silently place MARKET instead.
            notes.append(
                "wing_strike_offset set but no live quote resolved for "
                "the LIMIT offset leg — wing not attached (see wing scan notes)"
            )
            return None, notes
        wing_sym = _wing_symbol(parent_symbol, wing_strike_offset)
        if wing_sym is None:
            notes.append(
                f"could not compute wing strike for {parent_symbol} (parsing failed)"
            )
            return None, notes
        # Estimated wing premium — fraction of parent's premium.
        # Operator's preview shows this; actual fill comes from
        # paper engine. (MARKET direction only — cosmetic, see above.)
        est = None
        if wing_premium_pct is not None:
            est = round(parent_fill_price * float(wing_premium_pct) / 100.0, 2)
        return WingSpec(
            tradingsymbol=wing_sym,
            transaction_type=wing_txn_type,
            quantity=parent_qty,
            exchange=parent_exchange,
            product=parent_product,
            order_type=wing_order_type,
            estimated_price=est,
        ), notes

    # No fallback note here — apply_template_to_order already
    # appended the chain-scan reason (success or skip) to plan.notes
    # before this resolver ran.
    return None, notes


def resolve_template_plan(
    template: dict,
    overrides: dict,
    *,
    parent_account:    str,
    parent_symbol:     str,
    parent_side:       str,
    parent_qty:        int,
    parent_exchange:   str,
    parent_fill_price: float,
    parent_product:    str = "NRML",
    broker_caps:       Optional[BrokerCapabilities] = None,
    parent_lot_size:   int = 1,
    parent_tick_size:  float = 0.0,
) -> TemplatePlan:
    """Build the plan. No broker calls, no DB writes — pure data."""

    # Override numeric fields (operator's inline edits on OrderTicket
    # win over the template default). None in either layer means "no
    # attach for this slot". Defensive: accept overrides=None from
    # callers like `_attach_basket_leg_template` that don't surface
    # operator overrides (the basket leg carries only template_id).
    (
        tp_pct, sl_pct, wing_premium_pct, sl_trail_pct,
        tp_scales, wing_strike_offset, tp_order_type,
        _validation_notes,
    ) = _parse_template_overrides(template, overrides)
    _ov = overrides or {}

    plan = TemplatePlan(
        template_id=template.get("id"),
        template_name=template.get("name") or "(unnamed)",
        template_slug=template.get("slug"),
        parent_account=parent_account,
        parent_symbol=parent_symbol,
        parent_side=parent_side,
        parent_qty=parent_qty,
        parent_exchange=parent_exchange,
        parent_fill_price=float(parent_fill_price),
        parent_lot_size=int(parent_lot_size) if parent_lot_size > 1 else 1,
        parent_tick_size=float(parent_tick_size) if parent_tick_size and parent_tick_size > 0 else 0.0,
    )
    # Surface the pre-plan validation notes (tp_pct/sl_pct rejected) so
    # the operator sees them in the preview chip + retry response.
    for _n in _validation_notes:
        plan.notes.append(_n)

    # ── GTT spec — TP / SL / both ────────────────────────────────────
    tp_trig = _tp_trigger(parent_side, parent_fill_price, tp_pct,
                          tick_size=plan.parent_tick_size)
    sl_trig = _sl_trigger(parent_side, parent_fill_price, sl_pct,
                          tick_size=plan.parent_tick_size)
    exit_side = _close_side(parent_side)

    # Phase 3A — scale-out path supersedes single tp_pct.
    if tp_scales:
        _gtts, _notes = _build_scale_out_gtts(
            tp_scales, parent_side, parent_fill_price, parent_qty,
            exit_side, parent_product, tp_order_type,
            sl_trig, sl_trail_pct,
            lot_size=plan.parent_lot_size,         # #3: round scale qtys to lot multiples
            parent_exchange=parent_exchange,        # #1: TP LIMIT offset
            tick_size=plan.parent_tick_size,        # tick-grid snap
        )
        plan.gtts.extend(_gtts)
        plan.notes.extend(_notes)
    elif tp_trig is not None and sl_trig is not None:
        _gtts, _notes = _build_tp_sl_gtts(
            tp_trig, sl_trig, exit_side, parent_qty,
            parent_product, tp_order_type, sl_trail_pct, broker_caps,
            parent_exchange=parent_exchange,        # #1: TP LIMIT offset
            tick_size=plan.parent_tick_size,        # tick-grid snap
        )
        plan.gtts.extend(_gtts)
        plan.notes.extend(_notes)
    elif tp_trig is not None:
        _tp_exch = parent_exchange if tp_order_type == "LIMIT" else ""
        plan.gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[tp_trig],
            orders=[_leg(exit_side, parent_qty, tp_trig, parent_product, tp_order_type,
                         tp_offset_exchange=_tp_exch,
                         tick_size=plan.parent_tick_size)],  # #1
            label="TP",
        ))
    elif sl_trig is not None:
        plan.gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[sl_trig],
            orders=[_leg(exit_side, parent_qty, sl_trig, parent_product, "LIMIT",
                         tick_size=plan.parent_tick_size)],
            label="SL",
            sl_trail_pct=sl_trail_pct,
        ))

    # ── Wing spec — SELL option only ─────────────────────────────────
    plan.wing, _wing_notes = _build_wing_spec(
        parent_side, parent_symbol, _ov,
        wing_strike_offset, wing_premium_pct,
        parent_qty, parent_exchange, parent_product, parent_fill_price,
        tick_size=plan.parent_tick_size,
    )
    plan.notes.extend(_wing_notes)

    return plan


def _tp_limit_offset(trigger: float, side: str, exchange: str = "",
                      tick_size: float = 0.0) -> float:
    """Return the adjusted LIMIT price for a TP leg to improve fill probability.

    For a BUY parent, the exit is SELL so we set LIMIT slightly *below* the
    trigger so the order rests just inside the trigger and fills on the next
    tick. For a SELL parent, the exit is BUY so we set LIMIT slightly *above*.

    Exchange-specific tick sizes read from settings with a YAML default:
      templates.tp_limit_tick_offset_nfo    (0.05) — options (NFO/MCX)
      templates.tp_limit_tick_offset_default (0.5) — futures + others

    The offset is cosmetic on trigger-linked GTT legs (Kite fires the GTT at
    the trigger, then places the child LIMIT order); the adjustment protects
    against rounding edge cases when the trigger and LIMIT price are identical.

    `tick_size`, when known (>0), snaps the offset LIMIT price to the
    instrument's tick grid using INTEGER tick-count arithmetic (never
    float floor/ceil — a float snap can overshoot by a whole tick on
    binary-float residue, the same bug class fixed in commit 4c61e47f).
    `trigger` is always already tick-aligned (computed via
    `_tp_trigger`/`_sl_trigger`'s own snap), so `t = round(trigger /
    tick_size)` is an exact tick count; stepping by whole ticks away
    from `t` makes a collapse onto the trigger structurally impossible
    (the offset-in-ticks is clamped to >= 1). Unknown/0 tick_size
    preserves the pre-fix plain round(x, 2) behavior exactly.
    """
    try:
        from backend.shared.helpers.settings import get_float
        exch_upper = (exchange or "").upper()
        is_options_exch = exch_upper in ("NFO", "BFO", "CDS")
        if is_options_exch:
            offset = get_float("templates.tp_limit_tick_offset_nfo", 0.05)
        else:
            offset = get_float("templates.tp_limit_tick_offset_default", 0.5)
    except Exception:
        offset = 0.05
    # exit side: BUY parent exits SELL → LIMIT below trigger;
    # SELL parent exits BUY → LIMIT above trigger.
    # The `side` passed here is the EXIT side (from _close_side), so:
    # exit=SELL means BUY parent → push LIMIT down; exit=BUY → push up.
    is_sell = side.upper() == "SELL"
    if not tick_size or tick_size <= 0:
        return round(trigger - offset, 2) if is_sell else round(trigger + offset, 2)
    t = round(trigger / tick_size)
    off_ticks = max(1, math.ceil(offset / tick_size - 1e-9))
    lim_ticks = (t - off_ticks) if is_sell else (t + off_ticks)
    # Floor-at-one-tick guard — never let the LIMIT price settle at or
    # below zero (Kite rejects a non-positive price).
    lim_ticks = max(1, lim_ticks)
    return round(lim_ticks * tick_size, 4)


def _leg(side: str, qty: int, price: float, product: str,
         order_type: str = "LIMIT",
         tp_offset_exchange: str = "",
         tick_size: float = 0.0) -> dict:
    """Compose a GTT leg dict — same shape SimGttBook + KiteBroker.place_gtt
    expect.

    `order_type='MARKET'` fires the GTT child as a market order at
    trigger time. Kite still expects a numeric `price` field in the
    leg dict (the SDK doesn't accept None), so MARKET legs pass the
    trigger value as a placeholder — the broker ignores it and fills
    at LTP. Same convention SimGttBook follows.

    `tp_offset_exchange` — when set for a LIMIT TP leg, applies a small
    inbound-of-trigger price offset (see `_tp_limit_offset`) so the
    limit order rests just inside the trigger and has a better fill chance.
    Set to the parent exchange for TP legs; leave empty for SL legs.

    `tick_size` — forwarded to `_tp_limit_offset` so the offset price
    snaps to the instrument's tick grid. 0.0 = unknown — no-op.
    """
    leg_price = float(price)
    if order_type == "LIMIT" and tp_offset_exchange:
        leg_price = _tp_limit_offset(leg_price, side, tp_offset_exchange, tick_size=tick_size)
    return {
        "transaction_type": side,
        "quantity":         int(qty),
        "price":            leg_price,
        "order_type":       order_type,
        "product":          product,
    }


# ── Sim path application ─────────────────────────────────────────────


def _ta_sim_wire_oco_pair(
    driver,
    spec: GttSpec,
    pair_first_id: str,
    result: AttachResult,
) -> None:
    """Wire OCO sibling back-pointer on the first GTT so either leg fires
    cancellation in SimGttBook. Updates result.sibling_pairs in-place."""
    result.sibling_pairs.append((pair_first_id, spec.placed_id))
    first_gtt = driver._gtt_book.get(pair_first_id)
    if first_gtt is not None:
        first_gtt.pair_with = spec.placed_id


def _ta_sim_place_one_gtt(
    driver,
    plan: TemplatePlan,
    spec: GttSpec,
    idx: int,
    pair_first_id: Optional[str],
    pair_two_singles: bool,
    result: AttachResult,
    parent_order_id: Optional[int],
) -> Optional[str]:
    """Place one GTT in the SimGttBook and update *result*.

    Returns the new pair_first_id (set when idx==0 for Groww OCO pairs).
    Errors are appended to result.errors; never raised.
    """
    try:
        placed = driver.place_sim_gtt(
            account=plan.parent_account,
            tradingsymbol=plan.parent_symbol,
            exchange=plan.parent_exchange,
            trigger_type=spec.trigger_type,
            trigger_values=list(spec.trigger_values),
            orders=list(spec.orders),
            last_price=plan.parent_fill_price,
            pair_with=pair_first_id if (pair_two_singles and idx == 1) else None,
            template_id=plan.template_id,
            parent_order_id=parent_order_id,
            tag=spec.label,
        )
        spec.placed_id = placed.get("gtt_id")
        result.gtt_ids.append(spec.placed_id)
        if pair_two_singles and idx == 0:
            return spec.placed_id
        if pair_two_singles and idx == 1 and pair_first_id and spec.placed_id:
            _ta_sim_wire_oco_pair(driver, spec, pair_first_id, result)
    except Exception as e:
        msg = f"sim GTT placement failed for {spec.label}: {e}"
        logger.error(msg)
        result.errors.append(msg)
    return pair_first_id


def _ta_sim_place_wing(
    driver,
    plan: TemplatePlan,
    result: AttachResult,
) -> None:
    """Register the wing/offset leg with SimDriver's paper engine.

    MARKET hedge direction (unchanged): the dict intentionally omits
    `side`/`limit_price` — `PaperTradeEngine._paper_step_single_order`
    then defaults `side` to "SELL" and `limit` to 0, which fills
    immediately at the current bid. This is the pre-existing
    "market-take" sim approximation; untouched here.

    LIMIT offset direction (BUY-parent → SELL offset leg): this is a
    REAL limit order, so `side`/`qty`/`limit_price` are set explicitly —
    the paper engine then evaluates it as a genuine resting SELL limit
    (`_paper_is_fillable`: fills only when bid >= limit_price), not an
    instant market-style fill.
    """
    if plan.wing is None:
        return
    from datetime import datetime, timezone
    is_limit = str(plan.wing.order_type).upper() == "LIMIT"
    wing_order = {
        "account":          plan.parent_account,
        "symbol":           plan.wing.tradingsymbol,
        "exchange":         plan.wing.exchange,
        "transaction_type": plan.wing.transaction_type,
        "quantity":         plan.wing.quantity,
        "initial_price":    plan.wing.estimated_price or plan.parent_fill_price,
        "status":           "OPEN",
        "mode":             "sim",
        "engine":           "sim",
        "detail":           (
            f"[SIM-WING] template={plan.template_name} → "
            f"{plan.wing.transaction_type} {plan.wing.quantity} "
            f"{plan.wing.tradingsymbol} "
            f"(parent {plan.parent_side} {plan.parent_symbol})"
        ),
        "created_at":       datetime.now(timezone.utc),
        "attempts":         0,
    }
    if is_limit:
        _lp = (
            plan.wing.limit_price
            if plan.wing.limit_price is not None
            else plan.wing.estimated_price
        )
        wing_order["side"] = plan.wing.transaction_type
        wing_order["qty"] = plan.wing.quantity
        wing_order["limit_price"] = _lp
    try:
        driver.register_open_order(wing_order)
        plan.wing.placed_id = f"sim-wing-{plan.wing.tradingsymbol}"
        result.wing_order_id = plan.wing.placed_id
    except Exception as e:
        msg = f"sim wing placement failed: {e}"
        logger.error(msg)
        result.errors.append(msg)


def apply_plan_sim(
    plan: TemplatePlan,
    driver,    # SimDriver
    *,
    parent_order_id: Optional[int] = None,
) -> AttachResult:
    """Route the plan into SimDriver. GTTs land in SimGttBook; wing
    leg registers with SimDriver._paper as a paper order. Errors are
    collected, not raised — caller decides how to surface them."""
    result = AttachResult(plan=plan)

    # Pair_with handling: when a Groww-emulated split produced TWO
    # single GTTs (both labelled "TP" / "SL"), pair them via pair_with
    # so SimGttBook auto-cancels the sibling on either fire. Detected
    # by seeing two singles in plan.gtts.
    pair_first_id: Optional[str] = None
    pair_two_singles = len(plan.gtts) == 2 and all(g.trigger_type == "single" for g in plan.gtts)

    for idx, spec in enumerate(plan.gtts):
        pair_first_id = _ta_sim_place_one_gtt(
            driver, plan, spec, idx, pair_first_id,
            pair_two_singles, result, parent_order_id,
        )

    _ta_sim_place_wing(driver, plan, result)
    return result


# ── Live path application (Kite) ─────────────────────────────────────

def _apply_live_g1_guard(plan: TemplatePlan) -> Optional[str]:
    """G1 lot-multiple guard — run before any broker call.

    Returns an error string when a GTT leg qty or the wing qty is not a
    multiple of the plan's lot_size.  Returns None when all quantities
    are valid (or when lot_size == 1, i.e. non-F&O — no-op).
    """
    _g1_ls = int(plan.parent_lot_size or 1)
    if _g1_ls <= 1:
        return None
    for _spec in plan.gtts:
        for _leg_dict in _spec.orders:
            try:
                _q = int(_leg_dict.get("quantity"))
            except (TypeError, ValueError):
                logger.warning("[G1-GUARD] leg has None/invalid quantity — skipping: %s", _leg_dict)
                continue
            if _q % _g1_ls != 0:
                return (
                    f"G1 lot-multiple guard failed: "
                    f"{plan.parent_symbol} GTT leg qty={_q} "
                    f"not a multiple of lot_size={_g1_ls}"
                )
    if plan.wing is not None:
        _wq = int(plan.wing.quantity)
        if _wq % _g1_ls != 0:
            return (
                f"G1 lot-multiple guard failed: "
                f"{plan.wing.tradingsymbol} wing qty={_wq} "
                f"not a multiple of lot_size={_g1_ls}"
            )
    return None


def _translate_gtt_orders(broker, spec: GttSpec, plan: TemplatePlan) -> list[dict]:
    """Translate each GTT leg's quantity from contracts to lots.

    For non-MCX brokers, translate_qty is a no-op (returns raw_qty
    unchanged). Raises ValueError on any translation failure so the
    caller's except block catches it without extra nesting.
    """
    translated: list[dict] = []
    for leg_dict in spec.orders:
        raw_q = int(leg_dict["quantity"])
        try:
            kite_q = broker.translate_qty(
                plan.parent_exchange, raw_q, plan.parent_lot_size
            )
        except (ValueError, AttributeError) as _te:
            raise ValueError(
                f"[GTT-QTY-GUARD] translate_qty failed for "
                f"{plan.parent_exchange}/{plan.parent_symbol} "
                f"qty={raw_q} lot_size={plan.parent_lot_size}: {_te}"
            ) from _te
        translated.append({**leg_dict, "quantity": kite_q})
    logger.info(
        "[GTT-QTY] %s/%s: contract legs %s → lot legs %s (lot_size=%s)",
        plan.parent_exchange, plan.parent_symbol,
        [int(l["quantity"]) for l in spec.orders],
        [int(l["quantity"]) for l in translated],
        plan.parent_lot_size,
    )
    return translated


def _place_wing_leg(broker, plan: TemplatePlan) -> str:
    """Translate the wing/offset leg's quantity and place the order.

    MARKET direction (hedge, unchanged): no `price` kwarg is sent — exactly
    byte-identical to the pre-existing behavior.

    LIMIT direction (BUY-parent offset leg): `price` MUST be present and
    positive for Kite's `_validate_kite_order_prices` to accept the order
    ("LIMIT order requires price > 0") — a LIMIT order_type with no price
    kwarg at all would be rejected by the broker, never silently placed
    as MARKET. `WingSpec.limit_price` (falling back to `estimated_price`,
    which is the same real price on this direction — see WingSpec's
    docstring) supplies it. Raises ValueError before the broker call when
    no valid price is available, so the caller's except block collects a
    clear error instead of letting an opaque broker rejection surface.

    Returns the broker order_id as a string. Raises on any failure so
    the caller's except block collects the error.
    """
    raw_wing_q = int(plan.wing.quantity)
    try:
        kite_wing_q = broker.translate_qty(
            plan.wing.exchange, raw_wing_q, plan.parent_lot_size
        )
    except (ValueError, AttributeError) as _te:
        raise ValueError(
            f"[WING-QTY-GUARD] translate_qty failed for "
            f"{plan.wing.exchange}/{plan.wing.tradingsymbol} "
            f"qty={raw_wing_q} lot_size={plan.parent_lot_size}: {_te}"
        ) from _te
    # intent omitted intentionally — wing/offset legs are new opens, 50-lot ceiling applies
    _wing_kwargs: dict = dict(
        tradingsymbol=plan.wing.tradingsymbol,
        exchange=plan.wing.exchange,
        transaction_type=plan.wing.transaction_type,
        quantity=kite_wing_q,
        order_type=plan.wing.order_type,
        product=plan.wing.product,
        variety="regular",
        tag=f"tpl-{plan.template_id}-wing",  # Kite tag cap: 20 chars
    )
    if str(plan.wing.order_type).upper() == "LIMIT":
        _price = (
            plan.wing.limit_price
            if plan.wing.limit_price is not None
            else plan.wing.estimated_price
        )
        if not _price or float(_price) <= 0:
            raise ValueError(
                f"[WING-LIMIT-PRICE-GUARD] LIMIT offset leg for "
                f"{plan.wing.tradingsymbol} has no valid price "
                f"(limit_price={plan.wing.limit_price!r}, "
                f"estimated_price={plan.wing.estimated_price!r})"
            )
        _wing_kwargs["price"] = float(_price)
    order_id = broker.place_order(**_wing_kwargs)
    return str(order_id)


def _ta_live_place_one_gtt(
    broker,
    plan: TemplatePlan,
    spec: GttSpec,
    idx: int,
    pair_first_id: Optional[str],
    pair_two_singles: bool,
    result: AttachResult,
) -> Optional[str]:
    """Translate quantities and call broker.place_gtt for one GTT spec.

    Returns the updated pair_first_id (set on idx==0 for Groww OCO
    pairs). Errors are appended to *result.errors*; never raised.

    translate_qty + G1 guard: ``_translate_gtt_orders`` calls
    ``broker.translate_qty`` for every leg — hard-fail on sub-lot error
    propagates to the except block here, not to the caller.
    """
    try:
        translated_orders = _translate_gtt_orders(broker, spec, plan)
        gtt_id = broker.place_gtt(
            trigger_type=spec.trigger_type,
            tradingsymbol=plan.parent_symbol,
            exchange=plan.parent_exchange,
            last_price=plan.parent_fill_price,
            orders=translated_orders,
            trigger_values=list(spec.trigger_values),
            tag=f"tpl-{plan.template_id}-{spec.label}",
        )
        spec.placed_id = str(gtt_id)
        result.gtt_ids.append(spec.placed_id)
        if pair_two_singles and idx == 0:
            return spec.placed_id
        if pair_two_singles and idx == 1 and pair_first_id and spec.placed_id:
            result.sibling_pairs.append((pair_first_id, spec.placed_id))
    except NotImplementedError as e:
        result.errors.append(
            f"{broker.broker_id} does not yet support GTT: {e}"
        )
    except Exception as e:
        msg = f"live GTT placement failed for {spec.label}: {e}"
        logger.error(msg)
        result.errors.append(msg)
    return pair_first_id


def _ta_live_place_wing(
    broker,
    plan: TemplatePlan,
    result: AttachResult,
) -> None:
    """Translate qty and call broker.place_order for the wing leg."""
    if plan.wing is None:
        return
    try:
        order_id = _place_wing_leg(broker, plan)
        plan.wing.placed_id = order_id
        result.wing_order_id = plan.wing.placed_id
    except Exception as e:
        msg = f"live wing placement failed: {e}"
        logger.error(msg)
        result.errors.append(msg)


def apply_plan_live(
    plan: TemplatePlan,
    broker,   # KiteBroker (or any Broker adapter with place_gtt)
    *,
    parent_order_id: Optional[int] = None,
) -> AttachResult:
    """Route the plan into a real broker via the Broker ABC's place_gtt.
    Wing leg fans through broker.place_order. Idempotency: failures on
    any single attach are collected, never raised — the caller decides
    whether to roll back.

    Sprint C — when the broker doesn't support OCO natively (Groww
    today) AND the plan produced two singles (TP + SL), wire them as
    a sibling pair so the postback-handler persistence + the OCO
    pair-watcher background task know to cancel the survivor when
    one side fires."""
    result = AttachResult(plan=plan)

    # Broker-layer exchange validation — fail fast before any broker call.
    try:
        broker.validate_gtt_exchange(plan.parent_exchange)
    except ValueError as _ve:
        result.errors.append(str(_ve))
        return result

    # Pre-flight: confirm broker supports GTT at all before any placement work.
    _bcaps = broker.capabilities
    if not _bcaps.gtt_single:
        result.errors.append(
            f"{broker.broker_id} does not support GTT (gtt_single=False) — "
            "template attach skipped"
        )
        return result

    # C2 — Market-hours guard: GTT registration itself is accepted by Kite
    # 24×7 so GTT-only plans are allowed off-hours. Only plans with a wing
    # MARKET leg need an open exchange — the wing order is rejected
    # immediately by the broker when the exchange is closed.
    # Mirror of C1 in apply_template_to_order which gates on _template_has_wing.
    if plan.wing is not None:
        try:
            from backend.api.algo.agent_engine import (  # circular: lazy OK
                _symbol_exchange_open, _build_now_ctx,
            )
            if not _symbol_exchange_open(plan.parent_exchange, _build_now_ctx()):
                result.errors.append(
                    f"Exchange {plan.parent_exchange} closed — "
                    "wing order skipped off-hours"
                )
                return result
        except Exception as _mh_e:
            logger.warning(f"apply_plan_live: market-hours check failed: {_mh_e}")

    # G1 lot-multiple guard — fire before any broker call so sub-lot GTT
    # legs are caught here, not by the adapter ceiling after wire cost.
    # plan.parent_lot_size is set by apply_template_to_order via get_lot_size()
    # for MCX/NCO/NFO/BFO/CDS so it is already resolved; this is a synchronous
    # check only.
    _g1_err = _apply_live_g1_guard(plan)
    if _g1_err is not None:
        result.errors.append(_g1_err)
        return result

    # Detect Groww-style two-singles split. Same predicate `apply_plan_sim`
    # uses to wire SimGttBook.pair_with — the resolver produces two
    # `trigger_type="single"` GTTs labelled TP + SL when broker_caps.
    # gtt_oco is False, and we pair-stitch them post-place.
    pair_two_singles = (
        len(plan.gtts) == 2
        and all(g.trigger_type == "single" for g in plan.gtts)
        and {g.label for g in plan.gtts} == {"TP", "SL"}
    )
    pair_first_id: Optional[str] = None

    for idx, spec in enumerate(plan.gtts):
        pair_first_id = _ta_live_place_one_gtt(
            broker, plan, spec, idx, pair_first_id,
            pair_two_singles, result,
        )

    _ta_live_place_wing(broker, plan, result)
    return result


# ── Unified entry point — shared by /ticket route AND agent actions ──
#
# Both surfaces (operator-driven OrderTicket and agent-fire place_order)
# call this single helper so template semantics + override handling stay
# in lockstep. The resolver dispatches to sim vs live based on whether
# SimDriver is active.

def _ta_template_row_to_dict(row) -> dict:
    """Convert one OrderTemplate ORM row to a plain dict for the resolver."""
    return {
        "id":                 row.id,
        "slug":               row.slug,
        "name":               row.name,
        "applies_to":         row.applies_to,
        "tp_pct":             float(row.tp_pct)           if row.tp_pct is not None else None,
        "sl_pct":             float(row.sl_pct)           if row.sl_pct is not None else None,
        "wing_premium_pct":   float(row.wing_premium_pct) if row.wing_premium_pct is not None else None,
        "wing_strike_offset": int(row.wing_strike_offset) if row.wing_strike_offset is not None else None,
        # Chain-tab pre-submission spread gate default (spread_check.
        # resolve_max_spread_pct reads this key). NOT consumed by
        # resolve_template_plan / _parse_template_overrides — adding
        # it here is deliberately isolated from that override tuple.
        "wing_max_spread_pct": (float(row.wing_max_spread_pct)
                                 if getattr(row, "wing_max_spread_pct", None) is not None else None),
        "tp_order_type":      (row.tp_order_type or "LIMIT"),
        "tp_scales_json":     row.tp_scales_json,
        "sl_trail_pct":       float(row.sl_trail_pct)     if row.sl_trail_pct is not None else None,
    }


async def load_template_for_slug_or_id(
    *,
    template_id:   Optional[int],
    template_slug: Optional[str],
) -> Optional[dict]:
    """Fetch one OrderTemplate row as a dict. Returns None when neither
    id nor slug resolves to a row (caller treats that as "no template
    selected — build an ad-hoc template from overrides instead").
    Async because we hit Postgres; pure read so no transaction
    boundary to worry about."""
    if template_id is None and not template_slug:
        return None
    from sqlalchemy import select
    from backend.api.database import async_session
    from backend.api.models import OrderTemplate

    async with async_session() as s:
        stmt = select(OrderTemplate)
        if template_id is not None:
            stmt = stmt.where(OrderTemplate.id == int(template_id))
        else:
            stmt = stmt.where(OrderTemplate.slug == str(template_slug))
        row = (await s.execute(stmt)).scalars().first()
    if row is None:
        return None
    return _ta_template_row_to_dict(row)


def build_adhoc_template(overrides: dict) -> dict:
    """When the operator didn't pick a saved template but supplied
    inline TP/SL/Wing overrides, package them as an ad-hoc template
    dict so the same resolve_template_plan path applies. Lets the
    legacy target_pct field flow through the new unified pipeline
    without a parallel code path."""
    return {
        "id":                 None,
        "slug":               None,
        "name":               "(ad-hoc)",
        "applies_to":         "both",
        "tp_pct":             overrides.get("tp_pct"),
        "sl_pct":             overrides.get("sl_pct"),
        "wing_premium_pct":   overrides.get("wing_premium_pct"),
        "wing_strike_offset": overrides.get("wing_strike_offset"),
        "tp_order_type":      overrides.get("tp_order_type", "LIMIT"),
        "tp_scales_json":     overrides.get("tp_scales_json"),
    }


def has_any_override(overrides: Optional[dict]) -> bool:
    """True when at least one TP/SL/Wing override is non-None — means
    "build an ad-hoc template even if no template_id was passed".
    Defensive against overrides=None from callers like
    `_attach_basket_leg_template`.

    Sprint E (audit) — `tp_scales_json` + `sl_trail_pct` were missing
    from the override key set. An operator hand-passing only
    `tp_scales_json` (or only `sl_trail_pct`) would get
    has_any_override → False and the ad-hoc template path silently
    did nothing — no GTT placed despite a valid override blob.
    """
    if not overrides:
        return False
    keys = ("tp_pct", "sl_pct", "wing_premium_pct", "wing_strike_offset",
            "tp_scales_json", "sl_trail_pct")
    return any(overrides.get(k) is not None for k in keys)


def _ta_guard_detect_mismatch(
    applies_to: str,
    parent_side_u: str,
    is_option: bool,
    parent_symbol: str,
) -> Optional[str]:
    """Return a mismatch reason string, or None when the attach is allowed.

    ``applies_to`` must already be lowercased and stripped. Covers the
    four directional values: ``buy_any``, ``sell_any``, ``buy_option``,
    ``sell_option``. ``both`` / ``none`` callers never reach this helper.
    """
    wants_buy         = applies_to in ("buy_any", "buy_option")
    wants_sell        = applies_to in ("sell_any", "sell_option")
    wants_option_only = applies_to in ("buy_option", "sell_option")
    if wants_buy and parent_side_u != "BUY":
        return f"side mismatch — template requires BUY parent but got {parent_side_u}"
    if wants_sell and parent_side_u != "SELL":
        return f"side mismatch — template requires SELL parent but got {parent_side_u}"
    if wants_option_only and not is_option:
        return (
            f"kind mismatch — template is option-only but {parent_symbol!r} is not an option"
        )
    return None


def _check_applies_to_guard(
    template:          dict,
    parent_side:       str,
    parent_symbol:     str,
    parent_account:    str,
    parent_qty:        int,
    parent_fill_price: float,
    parent_order_id:   Optional[int],
) -> bool:
    """Enforce the template's applies_to field against the parent order's
    side and kind (incident 2026-06-22 guard).

    Returns True when the attach must be REFUSED (mismatch detected —
    caller should return None). Fires Telegram + email alert as a
    side-effect when refusing. Returns False when the attach is allowed.

    'both' and 'none' are always allowed (no filtering).
    """
    applies_to = (template.get("applies_to") or "both").strip().lower()
    if applies_to in ("both", "none"):
        return False

    parent_side_u = (parent_side or "").upper().strip()
    is_option = bool(_OPT_SYM_RE.match((parent_symbol or "").upper()))
    mismatch_reason = _ta_guard_detect_mismatch(
        applies_to, parent_side_u, is_option, parent_symbol
    )

    if mismatch_reason:
        slug = template.get("slug", "?")
        logger.warning(
            f"template_attach.applies_to_guard: refusing to attach "
            f"template slug={slug!r} (applies_to={applies_to}) to "
            f"{parent_side_u} {parent_symbol!r} parent_order={parent_order_id} — "
            f"{mismatch_reason}. 2026-06-22 incident pattern; non-destructive skip."
        )
        # Fire-and-forget alert so the operator gets immediate
        # Telegram + email visibility on every guard fire. The
        # PARENT order already filled successfully; this alert
        # tells the operator "your exit plan didn't attach —
        # check + manually arm if needed".
        _fire_guard_alert(
            template_slug=slug,
            applies_to=applies_to,
            parent_side=parent_side_u,
            parent_symbol=parent_symbol,
            parent_account=parent_account,
            parent_qty=parent_qty,
            parent_fill_price=parent_fill_price,
            parent_order_id=parent_order_id,
            reason=mismatch_reason,
        )
        return True

    return False


async def _resolve_lot_size_for_order(
    template:          dict,
    parent_exchange:   str,
    parent_symbol:     str,
    parent_account:    str,
    parent_side:       str,
    parent_qty:        int,
    parent_fill_price: float,
) -> tuple[int, Optional[AttachResult]]:
    """Async lot_size resolution for F&O exchanges (MCX/NCO/NFO/BFO/CDS).

    Returns (lot_size, None) on success. Returns (1, AttachResult_with_errors)
    on failure — caller must return the error result immediately.

    For non-derivative exchanges, returns (1, None) immediately (no-op).
    """
    if parent_exchange.upper() not in ("MCX", "NCO", "NFO", "BFO", "CDS"):
        return 1, None

    def _err_plan() -> TemplatePlan:
        return TemplatePlan(
            template_id=template.get("id"),
            template_name=template.get("name") or "(unnamed)",
            template_slug=template.get("slug"),
            parent_account=parent_account,
            parent_symbol=parent_symbol,
            parent_side=parent_side,
            parent_qty=parent_qty,
            parent_exchange=parent_exchange,
            parent_fill_price=float(parent_fill_price),
            parent_lot_size=1,
        )

    try:
        from backend.brokers.adapters.kite import get_lot_size
        _ls = await get_lot_size(parent_exchange, parent_symbol)
        if _ls <= 1 and parent_exchange.upper() in ("MCX", "NCO"):
            # Instruments cache warms asynchronously at startup; a fresh
            # postback that arrives before the cache is fully populated
            # can return 0 (miss) or 1 (equity sentinel) for MCX/NCO.
            # One 3 s retry catches the common race without blocking the
            # fill pipeline significantly.
            import asyncio as _asyncio_retry
            await _asyncio_retry.sleep(3)
            _ls = await get_lot_size(parent_exchange, parent_symbol)
        if _ls > 1:
            return _ls, None
        # 0 = cache miss, 1 = equity sentinel — both are dangerous on
        # F&O exchanges. Surface to caller as a hard failure so no
        # untranslated qty ever reaches the broker.
        logger.error(
            "[GTT-QTY-GUARD] lot_size=%s for %s/%s — "
            "instruments cache miss or sub-lot. Refusing template attach.",
            _ls, parent_exchange, parent_symbol,
        )
        result_err = AttachResult(plan=_err_plan())
        result_err.errors.append(
            f"[GTT-QTY-GUARD] lot_size={_ls} for "
            f"{parent_exchange}/{parent_symbol} — instruments cache miss. "
            f"Cannot safely translate qty to lots. Template attach refused."
        )
        # TODO(#21): write template_attach_error = "lot_size_cache_miss" to
        # AlgoOrder when AlgoOrder.template_attach_error column exists.
        return 1, result_err
    except Exception as _e:
        logger.error(
            "[GTT-QTY-GUARD] get_lot_size failed for %s/%s: %s — "
            "refusing F&O template attach.", parent_exchange, parent_symbol, _e,
        )
        result_err = AttachResult(plan=_err_plan())
        result_err.errors.append(
            f"[GTT-QTY-GUARD] lot_size lookup failed for "
            f"{parent_exchange}/{parent_symbol}: {_e}. "
            f"Template attach refused to prevent F&O oversize."
        )
        # TODO(#21): write template_attach_error = "lot_size_lookup_failed" to
        # AlgoOrder when AlgoOrder.template_attach_error column exists.
        return 1, result_err


async def _resolve_tick_size_for_order(parent_exchange: str, parent_symbol: str) -> float:
    """Resolve tick_size from the instruments cache for TP/SL trigger +
    LIMIT-offset snapping (2026-10 hardening fix).

    Applies to EVERY exchange, not just F&O — equity triggers are just
    as tick-sensitive (NSE equities commonly tick at 0.05 too).

    Fail-open to 0.0 ("unknown") on any lookup miss or error. This is
    the OPPOSITE policy of `_resolve_lot_size_for_order`: a tick-size
    miss must never refuse the template attach — the snap helpers
    already no-op to the pre-fix plain round(x, 2) behavior when
    tick_size is 0.0, so a broker-side rejection on an off-tick GTT is
    the worst case, not a silently-unarmed parent position.

    Reuses the same (exchange, symbol) → tick_size index the ticket
    route's `_align_price_to_tick` builds from the instruments cache.
    Accessed via module attribute, not a direct name import —
    `orders_helpers._rebuild_tick_index` REBINDS the module-level dict
    (`global _TICK_INDEX; _TICK_INDEX = new_index`) on every cache
    refresh, so `from orders_helpers import _TICK_INDEX` would capture
    a stale reference once and never see a later refresh.
    """
    try:
        import backend.api.routes.orders_helpers as _oh
        await _oh._ensure_tick_index()
        tick = _oh._TICK_INDEX.get(
            ((parent_exchange or "").upper(), (parent_symbol or "").upper())
        )
        return float(tick) if tick else 0.0
    except Exception as e:
        logger.warning(
            "[TEMPLATE-TICK] tick_size lookup failed for %s/%s: %s — "
            "falling back to plain round(x, 2) (no tick snap).",
            parent_exchange, parent_symbol, e,
        )
        return 0.0


def _template_has_wing(template: dict) -> bool:
    """Return True when the template dict specifies a wing leg.

    A wing is present when wing_strike_offset is non-None (including 0 for
    ATM) or wing_premium_pct is non-zero. Called by the market-hours guard to
    decide whether a closed-exchange attach can proceed (GTT-only: yes;
    wing: no). Note: offset=0 is a valid ATM wing — matches resolve_template_plan
    which uses `if wing_strike_offset is not None:`.
    """
    offset = template.get("wing_strike_offset")
    pct    = template.get("wing_premium_pct")
    return (offset is not None) or bool(pct)


def _ta_resolve_sim_active(apply_path: str) -> bool:
    """Return True when SimDriver is active and apply_path is 'auto' or 'sim'."""
    if apply_path == "sim":
        return True
    if apply_path != "auto":
        return False
    try:
        from backend.api.algo.sim.driver import SimDriver
        return bool(SimDriver.instance().active)
    except Exception:
        return False


def _route_apply_path(
    plan:            TemplatePlan,
    apply_path:      str,
    parent_account:  str,
    parent_order_id: Optional[int],
) -> AttachResult:
    """Route the resolved plan into sim, live, or preview path.

    'preview': return AttachResult(plan) with no broker calls.
    'sim' / 'auto'+SimDriver.active: route to apply_plan_sim.
    'live' / 'auto'+not sim: resolve broker + route to apply_plan_live.
    Fallback: return AttachResult(plan) unchanged.
    """
    # Preview short-circuit — never apply.
    if apply_path == "preview":
        return AttachResult(plan=plan)

    # Resolve sim vs live.
    sim_active = _ta_resolve_sim_active(apply_path)

    if sim_active:
        from backend.api.algo.sim.driver import SimDriver
        return apply_plan_sim(plan, SimDriver.instance(),
                              parent_order_id=parent_order_id)

    if apply_path in ("live", "auto"):
        try:
            from backend.brokers.registry import get_broker
            broker = get_broker(parent_account)
        except Exception as e:
            result = AttachResult(plan=plan)
            result.errors.append(
                f"could not resolve broker for {parent_account!r}: {e}"
            )
            return result
        return apply_plan_live(plan, broker, parent_order_id=parent_order_id)

    return AttachResult(plan=plan)


def _ta_wing_scan_precondition(
    template: dict,
    overrides: dict,
    parent_side: str,
    parent_symbol: str,
    parent_fill_price: float,
) -> Optional[float]:
    """Return the resolved wing_premium_pct when the premium-scan should run.

    Returns ``None`` when the scan should be skipped — either because the
    parent isn't a recognisable option entry on either side (see
    `_wing_direction` — covers both the SELL-parent hedge direction and
    the BUY-parent offset direction), the fill price is zero, a manual
    ``wing_strike_offset`` is already set, or no ``wing_premium_pct`` is
    configured on the template or overrides.
    """
    _ov = overrides or {}
    wing_offset_pre = _ov.get("wing_strike_offset")
    if wing_offset_pre is None:
        wing_offset_pre = template.get("wing_strike_offset")
    wing_pct_pre = _ov.get("wing_premium_pct")
    if wing_pct_pre is None:
        wing_pct_pre = template.get("wing_premium_pct")

    if not (
        _wing_direction(parent_side, parent_symbol) is not None
        and wing_pct_pre is not None
        and wing_offset_pre is None
        and parent_fill_price > 0
    ):
        return None
    return float(wing_pct_pre)


async def _maybe_scan_wing_by_premium(
    template:          dict,
    overrides:         dict,
    parent_side:       str,
    parent_symbol:     str,
    parent_exchange:   str,
    parent_fill_price: float,
    parent_order_id:   Optional[int] = None,
) -> tuple[dict, Optional[str], Optional[str]]:
    """Phase 1B — when the template's wing mode is premium% AND the
    operator has not supplied an explicit wing_strike_offset, run the
    async chain scan and inject the picked symbol into overrides.

    Returns (overrides_possibly_augmented, wing_scan_note_or_None, wing_skipped_reason_or_None).
    On any failure, wing_scan_note and wing_skipped_reason both carry the reason.
    On success, wing_skipped_reason is None and the note explains which strike was picked.
    On scan skipped (not a SELL option etc.), both are None.

    (#6) When the scan ran but returned no winner (wsym is None):
      - wing_scan_note is set (as before, appended to plan.notes)
      - wing_skipped_reason is set (new — surfaced in AttachResult.wing_skipped_reason)
      - ntfy alert fired so operator is notified the wing is unprotected
    """
    wing_pct = _ta_wing_scan_precondition(
        template, overrides, parent_side, parent_symbol, parent_fill_price
    )
    if wing_pct is None:
        return overrides, None, None

    try:
        wsym, wltp, reason = await _pick_wing_by_premium(
            parent_symbol=parent_symbol,
            parent_exchange=parent_exchange,
            parent_fill_price=parent_fill_price,
            wing_premium_pct=wing_pct,
        )
    except Exception as e:
        wsym, wltp, reason = None, None, f"wing_premium_pct scan errored: {e}"

    if wsym:
        overrides = dict(overrides or {})
        overrides["_wing_picked_symbol"] = wsym
        if wltp is not None:
            overrides["_wing_picked_ltp"] = wltp
        # Wing found — no skip reason.
        return overrides, reason, None

    # (#6) Wing scan ran but found no candidate — notify operator.
    logger.warning(
        "[WING-SKIP] wing scan returned no candidate for order #%s %s: %s",
        parent_order_id, parent_symbol, reason,
    )
    try:
        from backend.shared.helpers.alert_utils import send_ntfy_alert
        send_ntfy_alert(
            "Wing attach skipped",
            f"{reason} | order #{parent_order_id} {parent_symbol} {parent_exchange}",
            priority="high",
        )
    except Exception as _na:
        logger.warning("wing skip ntfy alert failed: %s", _na)

    return overrides, reason, reason


def _ta_offset_wing_precondition(
    overrides: dict,
    template: dict,
    parent_side: str,
    parent_symbol: str,
) -> Optional[int]:
    """Return the manual wing_strike_offset int when the LIMIT offset
    direction (BUY-parent) needs its own live-quote lookup — i.e. the
    premium-scan (`_maybe_scan_wing_by_premium`) didn't already resolve a
    picked symbol AND the operator (or template) set an explicit
    wing_strike_offset. Returns None (no-op) for the MARKET hedge
    direction — that branch never needs a live price."""
    _ov = overrides or {}
    if _ov.get("_wing_picked_symbol"):
        return None  # already resolved by the premium scan
    direction = _wing_direction(parent_side, parent_symbol)
    if direction is None or direction[1] != "LIMIT":
        return None  # MARKET hedge direction — no live price needed
    offset_pre = _ov.get("wing_strike_offset")
    if offset_pre is None:
        offset_pre = template.get("wing_strike_offset")
    if offset_pre is None:
        return None  # no manual offset configured — nothing to quote
    try:
        return int(offset_pre)
    except (TypeError, ValueError):
        return None


async def _maybe_fetch_wing_quote_for_offset(
    template:          dict,
    overrides:         dict,
    parent_side:       str,
    parent_symbol:     str,
    parent_exchange:   str,
    parent_order_id:   Optional[int] = None,
) -> tuple[dict, Optional[str], Optional[str]]:
    """BUY-parent LIMIT offset leg, manual `wing_strike_offset` path.

    The offset leg's strike is computed the SAME way as the existing
    MARKET-hedge offset path (`_wing_symbol` — no new strike-selection
    logic), but a LIMIT order needs a REAL price. When no
    `wing_premium_pct` scan already resolved one (see
    `_maybe_scan_wing_by_premium`), fetch a single live quote for the
    computed strike here, reusing the same `_wing_fetch_quotes` plumbing
    `_pick_wing_by_premium` uses — not a new broker-call pattern.

    Returns (overrides_possibly_augmented, note_or_None, skip_reason_or_None) —
    same contract as `_maybe_scan_wing_by_premium`. No-ops (all None) when
    this path doesn't apply (MARKET hedge direction, premium-scan already
    resolved a symbol, or no manual offset configured).
    """
    offset = _ta_offset_wing_precondition(overrides, template, parent_side, parent_symbol)
    if offset is None:
        return overrides, None, None

    wing_sym = _wing_symbol(parent_symbol, offset)
    if wing_sym is None:
        return overrides, None, None  # _build_wing_spec's own note covers parse failure

    try:
        quote_data, quote_err = await _wing_fetch_quotes(
            [{"exch": parent_exchange, "ts": wing_sym}], parent_exchange,
        )
    except Exception as e:
        quote_data, quote_err = {}, f"wing offset LIMIT quote failed: {e}"

    reason: Optional[str] = quote_err
    if not quote_err:
        q = quote_data.get(f"{parent_exchange}:{wing_sym}") or {}
        ltp = float(q.get("last_price") or 0)
        if ltp > 0:
            overrides = dict(overrides or {})
            overrides["_wing_picked_symbol"] = wing_sym
            overrides["_wing_picked_ltp"] = ltp
            return overrides, f"wing offset leg priced — {wing_sym} @ ₹{ltp:.2f}", None
        reason = f"wing offset LIMIT quote unavailable for {wing_sym}"

    logger.warning(
        "[WING-OFFSET-SKIP] order #%s %s: %s",
        parent_order_id, parent_symbol, reason,
    )
    try:
        from backend.shared.helpers.alert_utils import send_ntfy_alert
        send_ntfy_alert(
            "Wing offset attach skipped",
            f"{reason} | order #{parent_order_id} {parent_symbol} {parent_exchange}",
            priority="high",
        )
    except Exception as _na:
        logger.warning("wing offset skip ntfy alert failed: %s", _na)
    return overrides, reason, reason


def _mcx_capability_guard(
    caps,
    parent_exchange: str,
    parent_account: str,
    parent_symbol: str,
    parent_side: str,
    parent_qty: int,
    parent_fill_price: float,
    parent_order_id,
    template: dict,
) -> "AttachResult | None":
    """Return a pre-filled AttachResult error if the broker doesn't support GTT
    on MCX/NCO, else return None. Fires an attach-fail alert when blocking."""
    if caps is None or caps.gtt_supports_mcx or parent_exchange not in ("MCX", "NCO"):
        return None
    _err = (
        f"{caps.display_name} does not support GTT on {parent_exchange} — "
        "template attach skipped; use a Kite account for MCX/NCO templates"
    )
    logger.warning("[TEMPLATE-GUARD] %s", _err)
    _plan = TemplatePlan(
        template_id=template.get("id"),
        template_name=template.get("name") or "(unnamed)",
        template_slug=template.get("slug"),
        parent_account=parent_account,
        parent_symbol=parent_symbol,
        parent_side=parent_side,
        parent_qty=parent_qty,
        parent_exchange=parent_exchange,
        parent_fill_price=float(parent_fill_price),
        parent_lot_size=1,
    )
    result = AttachResult(plan=_plan)
    result.errors.append(_err)
    _fire_attach_fail_alert(
        order_id=parent_order_id,
        symbol=parent_symbol,
        account=parent_account,
        errors=[_err],
    )
    result.guard_alert_fired = True
    return result


async def _resolve_wing_pricing(
    template:          dict,
    overrides:         dict,
    parent_side:       str,
    parent_symbol:     str,
    parent_exchange:   str,
    parent_fill_price: float,
    parent_order_id:   Optional[int] = None,
) -> tuple[dict, Optional[str], Optional[str]]:
    """Resolve any live wing/offset pricing needed before `resolve_template_plan`
    runs — the wing_premium_pct chain scan (either direction) followed by
    the BUY-parent manual-offset LIMIT quote lookup (no-op unless that
    specific case applies). Thin sequencing wrapper kept separate from
    `apply_template_to_order` so the CC gate doesn't trip on the caller.

    Returns (overrides_possibly_augmented, note_or_None, skip_reason_or_None) —
    same contract as the two helpers it sequences.
    """
    overrides, wing_scan_note, wing_skipped_reason = await _maybe_scan_wing_by_premium(
        template, overrides, parent_side, parent_symbol,
        parent_exchange, parent_fill_price,
        parent_order_id=parent_order_id,
    )
    overrides, offset_note, offset_skip = await _maybe_fetch_wing_quote_for_offset(
        template, overrides, parent_side, parent_symbol,
        parent_exchange, parent_order_id=parent_order_id,
    )
    return overrides, (wing_scan_note or offset_note), (wing_skipped_reason or offset_skip)


async def apply_template_to_order(
    *,
    template_id:        Optional[int],
    template_slug:      Optional[str],
    overrides:          dict,
    parent_account:     str,
    parent_symbol:      str,
    parent_side:        str,
    parent_qty:         int,
    parent_exchange:    str,
    parent_fill_price:  float,
    parent_product:     str = "NRML",
    parent_order_id:    Optional[int] = None,
    apply_path:         str = "auto",  # 'auto' | 'sim' | 'live' | 'preview'
) -> Optional[AttachResult]:
    """One entry point used by:

      • /api/orders/ticket            (operator clicked Submit)
      • _handler_place_order          (agent fired place_order action)
      • /api/orders/ticket/preview    (operator wants the plan only)

    Returns None when NO template / NO overrides were supplied (caller
    skips the attach entirely). Otherwise returns an AttachResult.

    `apply_path` selection:
      'auto'    — SimDriver.active → sim; else 'live' (skipped today
                  pending broker-side fill-postback wiring)
      'sim'     — force the sim path (test fixtures use this)
      'live'    — force the live path
      'preview' — resolve plan, DO NOT apply; returns an AttachResult
                  with empty gtt_ids / wing_order_id so the UI can
                  render the planned artefacts
    """
    # Build or load template
    template = await load_template_for_slug_or_id(
        template_id=template_id, template_slug=template_slug,
    )
    if template is None:
        if not has_any_override(overrides):
            return None
        template = build_adhoc_template(overrides)

    # If the operator explicitly picked the "none" template (no TP/SL/Wing)
    # AND no overrides, short-circuit so we don't issue spurious GTTs.
    if (template.get("slug") == "none"
            and not has_any_override(overrides)):
        return None

    # ── applies_to guard (incident 2026-06-22) ────────────────────────
    # `default-bull` (applies_to='buy_any', BUY-side template) got
    # attached to a SELL on a PE option fill, placed a TP+SL OCO that
    # used buy-side price math, and one leg fired → unintended BUY 20
    # at ₹1447.5 closed part of the operator's short position.
    #
    # Enforce applies_to here so the template can never attach to a
    # leg shape it wasn't built for. Mismatch → log + alert (Telegram
    # + email) + return None. The PARENT order itself already filled;
    # we just don't add exits. Non-destructive failure mode.
    if _check_applies_to_guard(
        template, parent_side, parent_symbol,
        parent_account, parent_qty, parent_fill_price, parent_order_id,
    ):
        return None

    # Capability lookup — only needed for live path's OCO-vs-singles
    # decision. Sim path uses two-leg unconditionally (SimGttBook
    # supports both natively).
    caps = None
    if apply_path in ("live", "auto"):
        try:
            from backend.brokers.capabilities import capabilities_for
            caps = capabilities_for(parent_account)
        except Exception as _caps_err:
            from backend.brokers.capabilities import UNKNOWN_CAPS
            caps = UNKNOWN_CAPS
            logger.warning(
                "capabilities_for(%s) failed (%s) — using UNKNOWN_CAPS (no GTT)",
                parent_account, _caps_err,
            )

    # Pre-attach MCX guard — reject before lot-size resolution and plan resolution
    # so no work is wasted on an unsupported broker/exchange combination.
    _mcx_guard = _mcx_capability_guard(
        caps, parent_exchange, parent_account, parent_symbol,
        parent_side, parent_qty, parent_fill_price, parent_order_id, template,
    )
    if _mcx_guard is not None:
        return _mcx_guard

    # F&O lot_size resolution — look up lot_size BEFORE resolving the plan so
    # apply_plan_live has what it needs without an async call.  get_lot_size
    # is async and we're already in async context here.  For non-derivative
    # exchanges this is always 1 (no-op translation later).
    # Covers MCX/NCO (commodities) + NFO/BFO/CDS (index/currency F&O).
    parent_lot_size, _lot_err = await _resolve_lot_size_for_order(
        template, parent_exchange, parent_symbol,
        parent_account, parent_side, parent_qty, parent_fill_price,
    )
    if _lot_err is not None:
        # lot_size lookup failure is a silent operational error — alert
        # the operator so they can arm exits manually. Parent already filled.
        if _lot_err.errors:
            _fire_attach_fail_alert(
                order_id=parent_order_id,
                symbol=parent_symbol,
                account=parent_account,
                errors=_lot_err.errors,
            )
        return _lot_err

    # Tick-size resolution (2026-10 hardening fix) — unlike lot_size this
    # applies to EVERY exchange (equity triggers are tick-sensitive too)
    # and fails OPEN to 0.0 on any miss/error: see
    # `_resolve_tick_size_for_order`'s docstring for why a tick-size
    # miss must never refuse the attach the way a lot_size miss does.
    parent_tick_size = await _resolve_tick_size_for_order(parent_exchange, parent_symbol)

    # C1 — Market-hours guard: wing MARKET legs fail when exchange closed.
    # GTT-only templates proceed off-hours (Kite accepts GTTs 24×7).
    _early, _offhours_note = _check_offhours_wing_gate(
        apply_path, parent_exchange, template, parent_lot_size,
        parent_account, parent_symbol, parent_side, parent_qty,
        parent_fill_price, parent_order_id,
    )
    if _early is not None:
        return _early

    # Phase 1B / BUY-offset — resolve any live wing pricing (premium-scan
    # for either direction, or a manual-offset quote lookup for the new
    # LIMIT offset direction) before the sync plan resolver runs. Scan/
    # quote failures convert to a plan note + skip wing attach; the
    # parent order is never blocked. Extracted to its own helper to keep
    # this function's own branching flat (CC gate).
    overrides, wing_scan_note, wing_skipped_reason = await _resolve_wing_pricing(
        template, overrides, parent_side, parent_symbol,
        parent_exchange, parent_fill_price,
        parent_order_id=parent_order_id,
    )

    plan = resolve_template_plan(
        template, overrides,
        parent_account=parent_account,
        parent_symbol=parent_symbol,
        parent_side=parent_side,
        parent_qty=parent_qty,
        parent_exchange=parent_exchange,
        parent_fill_price=parent_fill_price,
        parent_product=parent_product,
        broker_caps=caps,
        parent_lot_size=parent_lot_size,
        parent_tick_size=parent_tick_size,
    )
    if wing_scan_note:
        plan.notes.append(wing_scan_note)
    if _offhours_note:
        plan.notes.append(_offhours_note)

    result = _route_apply_path(plan, apply_path, parent_account, parent_order_id)
    # (#6) Propagate wing skip reason into the result so callers and the
    # API response can surface why the wing wasn't attached.
    if wing_skipped_reason:
        result.wing_skipped_reason = wing_skipped_reason
    # (#28B) When the wing scan failed and GTTs were placed, alert immediately.
    if wing_skipped_reason and not result.wing_order_id and result.gtt_ids:
        _fire_wing_unprotected_alert(
            wing_skipped_reason, result, parent_order_id, parent_symbol, parent_exchange
        )
        # TODO(#28B): write wing_failed note to attached_gtts_json when
        # template_attach_error column exists on AlgoOrder.
    # Fire operational-failure alert on any error that silently
    # prevented exits from attaching. Only fires when the applies_to
    # guard did NOT already send a notification (guard_alert_fired=False,
    # which is always the case here — the guard path returns None, not
    # an AttachResult with errors).
    if result.errors and not result.guard_alert_fired:
        _fire_attach_fail_alert(
            order_id=parent_order_id,
            symbol=parent_symbol,
            account=parent_account,
            errors=result.errors,
        )
    return result
