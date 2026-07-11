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

import re
from dataclasses import dataclass, field, asdict
from typing import Optional

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
    """Protective wing leg for a SELL option entry. Symbol is computed
    from the parent's strike + template's wing_strike_offset (CE wing
    is +offset, PE wing is -offset). Quantity matches the parent so the
    spread net-margin is properly bounded.

    `estimated_price` is a heuristic (template's wing_premium_pct of
    parent price) — the actual entry price comes from the paper engine
    fill. Operator sees the estimate in the preview chip.
    """
    tradingsymbol:    str
    transaction_type: str = "BUY"
    quantity:         int = 0
    exchange:         str = "NFO"
    product:          str = "NRML"
    order_type:       str = "MARKET"   # market-take so the hedge lands first
    estimated_price:  Optional[float] = None
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
            "gtts":               [asdict(g) for g in self.gtts],
            "wing":               asdict(self.wing) if self.wing else None,
            "notes":              list(self.notes),
        }


@dataclass
class AttachResult:
    plan:           TemplatePlan
    gtt_ids:        list[str] = field(default_factory=list)
    wing_order_id:  Optional[str] = None
    sibling_pairs:  list[tuple[str, str]] = field(default_factory=list)
    errors:         list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "plan":          self.plan.to_dict(),
            "gtt_ids":       list(self.gtt_ids),
            "wing_order_id": self.wing_order_id,
            "sibling_pairs": [list(p) for p in self.sibling_pairs],
            "errors":        list(self.errors),
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

    def _do_telegram() -> None:
        try:
            from backend.shared.helpers.alert_utils import _send_telegram
            msg = (
                f"<b>⚠ Template guard fired — {ist_label}</b>\n\n"
                f"<code>"
                f"order #{parent_order_id}\n"
                f"{parent_side} {parent_qty} {parent_symbol}\n"
                f"@ ₹{parent_fill_price:.2f}  ({parent_account})\n\n"
                f"template:    {template_slug}\n"
                f"applies_to:  {applies_to}\n"
                f"reason:      {reason}\n\n"
                f"Parent order FILLED. Exits NOT attached.\n"
                f"Arm exits manually if needed.</code>"
            )
            _send_telegram(msg)
        except Exception as e:
            logger.warning(f"guard alert: Telegram failed: {e}")

    def _do_email() -> None:
        try:
            from backend.shared.helpers.alert_utils import get_alert_recipients
            from backend.shared.helpers.mail_utils import send_email
            recipients = get_alert_recipients()
            if not recipients:
                logger.info("guard alert: no email recipients configured; skipping")
                return
            subject = (
                f"RamboQuant: Template guard fired — "
                f"#{parent_order_id} {parent_side} {parent_qty} {parent_symbol}"
            )
            html = f"""
<html><body style='font-family:sans-serif;background:#0a1020;color:#c8d8f0;margin:0;padding:18px'>
  <div style='max-width:620px;margin:0 auto'>
    <div style='background:#7c2d12;color:#fff;padding:10px 14px;border-radius:4px;
                margin-bottom:14px;font-weight:700'>
      ⚠ Template guard fired
    </div>
    <p style='font-size:14px;color:#fbbf24;margin:0 0 12px 0'>
      <b>{ist_label}</b>
    </p>
    <p style='font-size:13px;line-height:1.5;color:#c8d8f0'>
      A template attach was refused because the leg's side or kind
      did not match the template's <code>applies_to</code> scope.
      The parent order filled normally; <b>exit legs were NOT
      attached</b>. Review the position and arm exits manually if
      needed.
    </p>
    <table style='border-collapse:collapse;font-family:ui-monospace,monospace;
                  font-size:13px;color:#c8d8f0;margin-top:12px'>
      <tr><td style='padding:4px 12px 4px 0;color:#94a3b8'>Order</td>
          <td style='padding:4px 0'>#{parent_order_id}</td></tr>
      <tr><td style='padding:4px 12px 4px 0;color:#94a3b8'>Side / Qty</td>
          <td style='padding:4px 0'>{parent_side} {parent_qty}</td></tr>
      <tr><td style='padding:4px 12px 4px 0;color:#94a3b8'>Symbol</td>
          <td style='padding:4px 0'>{parent_symbol}</td></tr>
      <tr><td style='padding:4px 12px 4px 0;color:#94a3b8'>Account</td>
          <td style='padding:4px 0'>{parent_account}</td></tr>
      <tr><td style='padding:4px 12px 4px 0;color:#94a3b8'>Fill price</td>
          <td style='padding:4px 0'>₹{parent_fill_price:.2f}</td></tr>
      <tr><td style='padding:4px 12px 4px 0;color:#94a3b8'>Template</td>
          <td style='padding:4px 0'><code>{template_slug}</code> (applies_to={applies_to})</td></tr>
      <tr><td style='padding:4px 12px 4px 0;color:#94a3b8'>Reason</td>
          <td style='padding:4px 0'>{reason}</td></tr>
    </table>
    <p style='font-size:11px;color:#7e97b8;margin-top:18px'>
      Sent automatically by RamboQuant's template_attach guard
      (2026-06-22 incident pattern). To suppress these alerts,
      either fix the template's <code>applies_to</code> scope or
      stop selecting a mismatched default in the OrderTicket.
    </p>
  </div>
</body></html>
"""
            for r in recipients:
                try:
                    send_email(r, r, subject, html)
                except Exception as e:
                    logger.warning(f"guard alert: email to {r} failed: {e}")
        except Exception as e:
            logger.warning(f"guard alert: email path failed: {e}")

    async def _both():
        # Run both synchronously inside one task so they share the
        # same wall-clock budget and the email never blocks Telegram.
        # Each helper is sync-on-the-network so they don't await.
        _do_telegram()
        _do_email()

    try:
        _asyncio.get_running_loop().create_task(_both())
    except RuntimeError:
        # Not in an asyncio context (test harness / sync caller).
        # Run the sync helpers directly so the alert still goes out.
        _do_telegram()
        _do_email()

    logger.info(f"guard alert dispatched: {summary}")


def _is_sell_option(side: str, symbol: str) -> bool:
    """SELL + parseable option symbol. Drives wing attach."""
    return side == "SELL" and bool(_OPT_SYM_RE.match(symbol.upper()))


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

    # Filter to (root, expiry_token, opt_type) — match exactly the
    # parent contract's chain. Use the cached `s` (tradingsymbol)
    # field's prefix as the discriminator so MCX vs NFO doesn't
    # matter; the parent's exchange flows through unchanged.
    parent_prefix = f"{root}{expiry_token}"
    suffix        = opt   # 'CE' or 'PE'
    candidates: list[dict] = []
    for inst in (insts_resp.items if insts_resp else []):
        ts = str(inst.s).upper()
        if not ts.startswith(parent_prefix):
            continue
        if not ts.endswith(suffix):
            continue
        if inst.k is None:
            continue
        candidates.append({
            "ts":     ts,
            "strike": float(inst.k),
            "exch":   inst.e,
        })

    if not candidates:
        return None, None, (
            f"wing_premium_pct skipped — no chain candidates found "
            f"for {root}{expiry_token}{suffix}"
        )

    # Slice to within chain_radius strikes of the parent. Sorted by
    # strike for the slice arithmetic.
    candidates.sort(key=lambda c: c["strike"])
    parent_idx = next(
        (i for i, c in enumerate(candidates) if c["strike"] == parent_strike),
        None,
    )
    if parent_idx is not None:
        lo = max(0, parent_idx - chain_radius)
        hi = min(len(candidates), parent_idx + chain_radius + 1)
        candidates = candidates[lo:hi]

    if not candidates:
        return None, None, (
            f"wing_premium_pct skipped — chain_radius filter eliminated "
            f"all candidates"
        )

    # Batched quote — one round-trip across every candidate. Offload
    # the sync broker call to a thread so we don't block the event
    # loop while the round-trip is in flight.
    quote_keys = [f"{c['exch']}:{c['ts']}" for c in candidates]
    try:
        import asyncio as _aio
        from backend.brokers.registry import get_market_data_broker
        broker = get_market_data_broker()
        quote_data = (
            await _aio.to_thread(broker.quote, quote_keys)
        ) or {}
    except Exception as e:
        return None, None, (
            f"wing_premium_pct skipped — broker.quote() failed: {e}"
        )

    best = None
    best_score = float("inf")
    # Filter-relaxed fallback — best candidate by premium score
    # ignoring OI / spread gates. Stock options (e.g. DIXON) have
    # OI of a few hundred per strike, so the index-tuned min_oi=1000
    # default drops every candidate. When that happens we still want
    # a wing attached; we keep the filter-passing winner if any, and
    # fall back to this when nothing passes.
    fallback = None
    fallback_score = float("inf")
    scanned, dropped_oi, dropped_spread = 0, 0, 0
    for c in candidates:
        key = f"{c['exch']}:{c['ts']}"
        q = quote_data.get(key) or {}
        ltp = float(q.get("last_price") or 0)
        if ltp <= 0:
            continue
        scanned += 1
        oi = int(q.get("oi") or 0)
        depth = q.get("depth") or {}
        buys = depth.get("buy") or []
        sells = depth.get("sell") or []
        bid = float(buys[0].get("price") if buys else 0) or 0
        ask = float(sells[0].get("price") if sells else 0) or 0
        spread_pct = ((ask - bid) / ltp * 100.0) if (ask > 0 and bid > 0) else 0.0
        dist = abs(ltp - target_premium)
        score = dist + (spread_pct / 100.0) * target_premium
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

def _tp_trigger(parent_side: str, fill_price: float, tp_pct: Optional[float]) -> Optional[float]:
    """Convert template's tp_pct into an absolute price.

    BUY parent: TP fires above (long unwinds at gain). fill × (1 + tp%/100).
    SELL parent: TP fires below (short unwinds at gain). fill × (1 - tp%/100).
    """
    if tp_pct is None:
        return None
    sign = 1.0 if parent_side == "BUY" else -1.0
    return round(fill_price * (1.0 + sign * float(tp_pct) / 100.0), 2)


def _sl_trigger(parent_side: str, fill_price: float, sl_pct: Optional[float]) -> Optional[float]:
    """SL fires opposite side of TP — protects against adverse move.

    BUY parent: SL fires below entry. fill × (1 - sl%/100).
    SELL parent: SL fires above entry. fill × (1 + sl%/100).
    """
    if sl_pct is None:
        return None
    sign = 1.0 if parent_side == "BUY" else -1.0
    return round(fill_price * (1.0 - sign * float(sl_pct) / 100.0), 2)


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

    def _pick(key: str) -> Optional[float]:
        v = _ov.get(key)
        if v is None:
            v = template.get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    tp_pct           = _pick("tp_pct")
    sl_pct           = _pick("sl_pct")
    wing_premium_pct = _pick("wing_premium_pct")
    sl_trail_pct     = _pick("sl_trail_pct")

    # Audit fix — non-positive % values silently produced an invalid GTT
    # (trigger == fill price on a BUY, immediately rejected by Kite or
    # firing instantly on a SELL). Drop to None + surface a note via
    # `_validation_notes` (appended to `plan.notes` once plan exists).
    _validation_notes: list[str] = []
    if tp_pct is not None and tp_pct <= 0:
        _validation_notes.append(f"tp_pct={tp_pct} is not positive — TP not attached")
        tp_pct = None
    if sl_pct is not None and sl_pct <= 0:
        _validation_notes.append(f"sl_pct={sl_pct} is not positive — SL not attached")
        sl_pct = None

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
) -> tuple[list[GttSpec], list[str]]:
    """Build GTT specs + notes for the Phase 3A scale-out path.

    Integer qty allocation: floor each phase, add the leftover to the
    LAST phase so allocations always sum to parent_qty. Returns
    (gtts_to_add, notes_to_add).
    """
    gtts: list[GttSpec] = []
    notes: list[str] = []

    # Integer qty allocation across scales — distribute floor
    # values then add the leftover to the LAST scale so the
    # numbers always sum to parent_qty (avoids "1 lot lost to
    # rounding" surprise).
    allocations: list[int] = []
    used = 0
    for i, sc in enumerate(tp_scales):
        if i == len(tp_scales) - 1:
            allocations.append(parent_qty - used)
        else:
            _q = int((parent_qty * float(sc["close_pct"])) // 100)
            allocations.append(_q)
            used += _q
    for sc, q in zip(tp_scales, allocations):
        if q <= 0:
            continue
        scale_trig = _tp_trigger(parent_side, parent_fill_price, float(sc["at_pct"]))
        if scale_trig is None:
            continue
        label = f"TP+{sc['at_pct']}% × {q}"
        gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[scale_trig],
            orders=[_leg(exit_side, q, scale_trig, parent_product, tp_order_type)],
            label=label,
        ))
    if sl_trig is not None:
        gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[sl_trig],
            orders=[_leg(exit_side, parent_qty, sl_trig, parent_product, "LIMIT")],
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
    tp_trig:        float,
    sl_trig:        float,
    exit_side:      str,
    parent_qty:     int,
    parent_product: str,
    tp_order_type:  str,
    sl_trail_pct:   Optional[float],
    broker_caps:    Optional[BrokerCapabilities],
) -> tuple[list[GttSpec], list[str]]:
    """Build GTT specs for the combined TP+SL case.

    On Kite/Dhan (broker_caps.gtt_oco=True or caps=None): one two-leg
    OCO. On Groww (gtt_oco=False): two singles + a note. Returns
    (gtts_to_add, notes_to_add).
    """
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
                _leg(exit_side, parent_qty, tp_trig, parent_product, tp_order_type),
                _leg(exit_side, parent_qty, sl_trig, parent_product, "LIMIT"),
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
            orders=[_leg(exit_side, parent_qty, tp_trig, parent_product, tp_order_type)],
            label="TP",
        ))
        gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[sl_trig],
            orders=[_leg(exit_side, parent_qty, sl_trig, parent_product, "LIMIT")],
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
) -> tuple[Optional[WingSpec], list[str]]:
    """Build a WingSpec for a SELL option entry, or return (None, notes).

    Priority: pre-resolved _wing_picked_symbol (set by apply_template_to_order
    after chain scan) > wing_strike_offset > no wing. Returns
    (wing_spec_or_none, notes_to_add).
    """
    notes: list[str] = []
    if not _is_sell_option(parent_side, parent_symbol):
        return None, notes

    _ov = overrides or {}
    # Phase 1B — apply_template_to_order pre-resolves the wing via
    # _pick_wing_by_premium when wing_premium_pct is set, and seeds
    # the picked tradingsymbol back into overrides. Use it first.
    wing_picked_sym = _ov.get("_wing_picked_symbol")
    wing_picked_ltp = _ov.get("_wing_picked_ltp")
    if wing_picked_sym:
        return WingSpec(
            tradingsymbol=str(wing_picked_sym),
            transaction_type="BUY",
            quantity=parent_qty,
            exchange=parent_exchange,
            product=parent_product,
            order_type="MARKET",
            estimated_price=(float(wing_picked_ltp)
                             if wing_picked_ltp is not None else None),
        ), notes

    if wing_strike_offset is not None:
        wing_sym = _wing_symbol(parent_symbol, wing_strike_offset)
        if wing_sym is None:
            notes.append(
                f"could not compute wing strike for {parent_symbol} (parsing failed)"
            )
            return None, notes
        # Estimated wing premium — fraction of parent's premium.
        # Operator's preview shows this; actual fill comes from
        # paper engine.
        est = None
        if wing_premium_pct is not None:
            est = round(parent_fill_price * float(wing_premium_pct) / 100.0, 2)
        return WingSpec(
            tradingsymbol=wing_sym,
            transaction_type="BUY",
            quantity=parent_qty,
            exchange=parent_exchange,
            product=parent_product,
            order_type="MARKET",
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
    )
    # Surface the pre-plan validation notes (tp_pct/sl_pct rejected) so
    # the operator sees them in the preview chip + retry response.
    for _n in _validation_notes:
        plan.notes.append(_n)

    # ── GTT spec — TP / SL / both ────────────────────────────────────
    tp_trig = _tp_trigger(parent_side, parent_fill_price, tp_pct)
    sl_trig = _sl_trigger(parent_side, parent_fill_price, sl_pct)
    exit_side = _close_side(parent_side)

    # Phase 3A — scale-out path supersedes single tp_pct.
    if tp_scales:
        _gtts, _notes = _build_scale_out_gtts(
            tp_scales, parent_side, parent_fill_price, parent_qty,
            exit_side, parent_product, tp_order_type,
            sl_trig, sl_trail_pct,
        )
        plan.gtts.extend(_gtts)
        plan.notes.extend(_notes)
    elif tp_trig is not None and sl_trig is not None:
        _gtts, _notes = _build_tp_sl_gtts(
            tp_trig, sl_trig, exit_side, parent_qty,
            parent_product, tp_order_type, sl_trail_pct, broker_caps,
        )
        plan.gtts.extend(_gtts)
        plan.notes.extend(_notes)
    elif tp_trig is not None:
        plan.gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[tp_trig],
            orders=[_leg(exit_side, parent_qty, tp_trig, parent_product, tp_order_type)],
            label="TP",
        ))
    elif sl_trig is not None:
        plan.gtts.append(GttSpec(
            trigger_type="single",
            trigger_values=[sl_trig],
            orders=[_leg(exit_side, parent_qty, sl_trig, parent_product, "LIMIT")],
            label="SL",
            sl_trail_pct=sl_trail_pct,
        ))

    # ── Wing spec — SELL option only ─────────────────────────────────
    plan.wing, _wing_notes = _build_wing_spec(
        parent_side, parent_symbol, _ov,
        wing_strike_offset, wing_premium_pct,
        parent_qty, parent_exchange, parent_product, parent_fill_price,
    )
    plan.notes.extend(_wing_notes)

    return plan


def _leg(side: str, qty: int, price: float, product: str,
         order_type: str = "LIMIT") -> dict:
    """Compose a GTT leg dict — same shape SimGttBook + KiteBroker.place_gtt
    expect.

    `order_type='MARKET'` fires the GTT child as a market order at
    trigger time. Kite still expects a numeric `price` field in the
    leg dict (the SDK doesn't accept None), so MARKET legs pass the
    trigger value as a placeholder — the broker ignores it and fills
    at LTP. Same convention SimGttBook follows.
    """
    return {
        "transaction_type": side,
        "quantity":         int(qty),
        "price":            float(price),
        "order_type":       order_type,
        "product":          product,
    }


# ── Sim path application ─────────────────────────────────────────────

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
                pair_first_id = spec.placed_id
            elif pair_two_singles and idx == 1 and pair_first_id and spec.placed_id:
                result.sibling_pairs.append((pair_first_id, spec.placed_id))
                # Wire the sibling pointer back on the first GTT so EITHER
                # side fires cancellation. The book reads pair_with on the
                # firing GTT to find its sibling.
                first_gtt = driver._gtt_book.get(pair_first_id)
                if first_gtt is not None:
                    first_gtt.pair_with = spec.placed_id
        except Exception as e:
            msg = f"sim GTT placement failed for {spec.label}: {e}"
            logger.error(msg)
            result.errors.append(msg)

    # Wing leg — fan into the sim paper engine. Falls through to a
    # market-order chase at the next LTP of the wing's symbol.
    if plan.wing is not None:
        from datetime import datetime, timezone
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
                f"[SIM-WING] template={plan.template_name} → BUY "
                f"{plan.wing.quantity} {plan.wing.tradingsymbol} "
                f"(parent {plan.parent_side} {plan.parent_symbol})"
            ),
            "created_at":       datetime.now(timezone.utc),
            "attempts":         0,
        }
        try:
            driver.register_open_order(wing_order)
            # Pre-built wing id surface — the paper engine assigns its
            # own internal id, but we surface the planned tradingsymbol
            # for the operator's preview.
            plan.wing.placed_id = f"sim-wing-{plan.wing.tradingsymbol}"
            result.wing_order_id = plan.wing.placed_id
        except Exception as e:
            msg = f"sim wing placement failed: {e}"
            logger.error(msg)
            result.errors.append(msg)

    return result


# ── Live path application (Kite) ─────────────────────────────────────

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

    # G1 lot-multiple guard — fire before any broker call so sub-lot GTT
    # legs are caught here, not by the adapter ceiling after wire cost.
    # plan.parent_lot_size is set by apply_template_to_order via get_lot_size()
    # for MCX/NCO/NFO/BFO/CDS so it is already resolved; this is a synchronous
    # check only.
    _g1_ls = int(plan.parent_lot_size or 1)
    if _g1_ls > 1:
        for _spec in plan.gtts:
            for _leg in _spec.orders:
                _q = int(_leg["quantity"])
                if _q % _g1_ls != 0:
                    result.errors.append(
                        f"G1 lot-multiple guard failed: "
                        f"{plan.parent_symbol} GTT leg qty={_q} "
                        f"not a multiple of lot_size={_g1_ls}"
                    )
                    return result
        if plan.wing is not None:
            _wq = int(plan.wing.quantity)
            if _wq % _g1_ls != 0:
                result.errors.append(
                    f"G1 lot-multiple guard failed: "
                    f"{plan.wing.tradingsymbol} wing qty={_wq} "
                    f"not a multiple of lot_size={_g1_ls}"
                )
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
        try:
            # MCX/NCO: translate each leg's quantity from contracts to lots
            # before sending to the broker. plan.parent_lot_size is set in
            # apply_template_to_order via get_lot_size() so we never need
            # an async call here. For non-MCX, translate_qty is a no-op
            # (returns raw_qty unchanged). Hard-fail on translation error
            # (sub-lot or cache miss on MCX) rather than sending raw qty.
            translated_orders: list[dict] = []
            for leg in spec.orders:
                raw_q = int(leg["quantity"])
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
                translated_orders.append({**leg, "quantity": kite_q})
            logger.info(
                "[GTT-QTY] %s/%s: contract legs %s → lot legs %s (lot_size=%s)",
                plan.parent_exchange, plan.parent_symbol,
                [int(l["quantity"]) for l in spec.orders],
                [int(l["quantity"]) for l in translated_orders],
                plan.parent_lot_size,
            )
            gtt_id = broker.place_gtt(
                trigger_type=spec.trigger_type,
                tradingsymbol=plan.parent_symbol,
                exchange=plan.parent_exchange,
                last_price=plan.parent_fill_price,
                orders=translated_orders,
                trigger_values=list(spec.trigger_values),
                # Kite tag cap: 20 chars. Use template_id (compact int)
                # instead of slug so the tag fits even for long slugs
                # like "default-short-vol". Labels are 2-5 chars.
                tag=f"tpl-{plan.template_id}-{spec.label}",
            )
            spec.placed_id = str(gtt_id)
            result.gtt_ids.append(spec.placed_id)
            if pair_two_singles and idx == 0:
                pair_first_id = spec.placed_id
            elif pair_two_singles and idx == 1 and pair_first_id and spec.placed_id:
                result.sibling_pairs.append((pair_first_id, spec.placed_id))
        except NotImplementedError as e:
            result.errors.append(
                f"{broker.broker_id} does not yet support GTT: {e}"
            )
        except Exception as e:
            msg = f"live GTT placement failed for {spec.label}: {e}"
            logger.error(msg)
            result.errors.append(msg)

    if plan.wing is not None:
        try:
            # Wing leg: same translate_qty guard. Wing exchange = parent_exchange
            # (options on the same underlying share the same lot convention).
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
            order_id = broker.place_order(
                tradingsymbol=plan.wing.tradingsymbol,
                exchange=plan.wing.exchange,
                transaction_type=plan.wing.transaction_type,
                quantity=kite_wing_q,
                order_type=plan.wing.order_type,
                product=plan.wing.product,
                variety="regular",
                tag=f"tpl-{plan.template_id}-wing",  # Kite tag cap: 20 chars
            )
            plan.wing.placed_id = str(order_id)
            result.wing_order_id = plan.wing.placed_id
        except Exception as e:
            msg = f"live wing placement failed: {e}"
            logger.error(msg)
            result.errors.append(msg)

    return result


# ── Unified entry point — shared by /ticket route AND agent actions ──
#
# Both surfaces (operator-driven OrderTicket and agent-fire place_order)
# call this single helper so template semantics + override handling stay
# in lockstep. The resolver dispatches to sim vs live based on whether
# SimDriver is active.

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
    return {
        "id":                 row.id,
        "slug":               row.slug,
        "name":               row.name,
        "applies_to":         row.applies_to,
        "tp_pct":             float(row.tp_pct)            if row.tp_pct is not None else None,
        "sl_pct":             float(row.sl_pct)            if row.sl_pct is not None else None,
        "wing_premium_pct":   float(row.wing_premium_pct)  if row.wing_premium_pct is not None else None,
        "wing_strike_offset": int(row.wing_strike_offset)  if row.wing_strike_offset is not None else None,
        "tp_order_type":      (row.tp_order_type or "LIMIT"),
        "tp_scales_json":     row.tp_scales_json,
        "sl_trail_pct":       float(row.sl_trail_pct)      if row.sl_trail_pct is not None else None,
    }


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
    applies_to = (template.get("applies_to") or "both").strip().lower()
    if applies_to not in ("both", "none"):
        parent_side_u = (parent_side or "").upper().strip()
        is_option = bool(_OPT_SYM_RE.match((parent_symbol or "").upper()))
        # buy_any:    BUY anything
        # sell_any:   SELL anything
        # buy_option: BUY of CE/PE only
        # sell_option:SELL of CE/PE only
        wants_buy   = applies_to in ("buy_any", "buy_option")
        wants_sell  = applies_to in ("sell_any", "sell_option")
        wants_option_only = applies_to in ("buy_option", "sell_option")
        mismatch_reason: Optional[str] = None
        if wants_buy and parent_side_u != "BUY":
            mismatch_reason = (
                f"side mismatch — template requires BUY parent but got {parent_side_u}"
            )
        elif wants_sell and parent_side_u != "SELL":
            mismatch_reason = (
                f"side mismatch — template requires SELL parent but got {parent_side_u}"
            )
        elif wants_option_only and not is_option:
            mismatch_reason = (
                f"kind mismatch — template is option-only but {parent_symbol!r} is not an option"
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
            return None

    # Capability lookup — only needed for live path's OCO-vs-singles
    # decision. Sim path uses two-leg unconditionally (SimGttBook
    # supports both natively).
    caps = None
    if apply_path in ("live", "auto"):
        try:
            from backend.brokers.capabilities import capabilities_for
            caps = capabilities_for(parent_account)
        except Exception:
            caps = None

    # F&O lot_size resolution — look up lot_size BEFORE resolving the plan so
    # apply_plan_live has what it needs without an async call.  get_lot_size
    # is async and we're already in async context here.  For non-derivative
    # exchanges this is always 1 (no-op translation later).
    # Covers MCX/NCO (commodities) + NFO/BFO/CDS (index/currency F&O).
    parent_lot_size: int = 1
    if parent_exchange.upper() in ("MCX", "NCO", "NFO", "BFO", "CDS"):
        try:
            from backend.brokers.adapters.kite import get_lot_size
            _ls = await get_lot_size(parent_exchange, parent_symbol)
            if _ls > 1:
                parent_lot_size = _ls
            elif _ls <= 1:
                # 0 = cache miss, 1 = equity sentinel — both are dangerous on
                # F&O exchanges. Surface to caller as a hard failure so no
                # untranslated qty ever reaches the broker.
                logger.error(
                    "[GTT-QTY-GUARD] lot_size=%s for %s/%s — "
                    "instruments cache miss or sub-lot. Refusing template attach.",
                    _ls, parent_exchange, parent_symbol,
                )
                result_err = AttachResult(plan=TemplatePlan(
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
                ))
                result_err.errors.append(
                    f"[GTT-QTY-GUARD] lot_size={_ls} for "
                    f"{parent_exchange}/{parent_symbol} — instruments cache miss. "
                    f"Cannot safely translate qty to lots. Template attach refused."
                )
                return result_err
        except Exception as _e:
            logger.error(
                "[GTT-QTY-GUARD] get_lot_size failed for %s/%s: %s — "
                "refusing F&O template attach.", parent_exchange, parent_symbol, _e,
            )
            result_err = AttachResult(plan=TemplatePlan(
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
            ))
            result_err.errors.append(
                f"[GTT-QTY-GUARD] lot_size lookup failed for "
                f"{parent_exchange}/{parent_symbol}: {_e}. "
                f"Template attach refused to prevent F&O oversize."
            )
            return result_err

    # Phase 1B — when the template says "pick wing by premium %" AND
    # no explicit wing_strike_offset overrides it, run the chain scan
    # here (we're in async context) and feed the picked tradingsymbol
    # back into the synchronous resolver via the merged overrides dict.
    # Scan failures convert to a plan note + skip wing attach; the
    # parent order is never blocked.
    wing_scan_note: Optional[str] = None
    wing_offset_pre = (overrides.get("wing_strike_offset")
                       if overrides else None)
    if wing_offset_pre is None:
        wing_offset_pre = template.get("wing_strike_offset")
    wing_pct_pre = (overrides.get("wing_premium_pct") if overrides else None)
    if wing_pct_pre is None:
        wing_pct_pre = template.get("wing_premium_pct")
    if (parent_side == "SELL"
            and bool(_OPT_SYM_RE.match(parent_symbol.upper()))
            and wing_pct_pre is not None
            and wing_offset_pre is None
            and parent_fill_price > 0):
        try:
            wsym, wltp, reason = await _pick_wing_by_premium(
                parent_symbol=parent_symbol,
                parent_exchange=parent_exchange,
                parent_fill_price=parent_fill_price,
                wing_premium_pct=float(wing_pct_pre),
            )
        except Exception as e:
            wsym, wltp, reason = None, None, (
                f"wing_premium_pct scan errored: {e}"
            )
        wing_scan_note = reason
        if wsym:
            overrides = dict(overrides or {})
            overrides["_wing_picked_symbol"] = wsym
            if wltp is not None:
                overrides["_wing_picked_ltp"] = wltp

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
    )
    if wing_scan_note:
        plan.notes.append(wing_scan_note)

    # Preview short-circuit — never apply.
    if apply_path == "preview":
        return AttachResult(plan=plan)

    # Resolve sim vs live.
    sim_active = False
    if apply_path == "auto":
        try:
            from backend.api.algo.sim.driver import SimDriver
            sim_active = SimDriver.instance().active
        except Exception:
            sim_active = False

    if apply_path == "sim" or (apply_path == "auto" and sim_active):
        from backend.api.algo.sim.driver import SimDriver
        return apply_plan_sim(plan, SimDriver.instance(),
                              parent_order_id=parent_order_id)

    if apply_path == "live" or (apply_path == "auto" and not sim_active):
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
