"""
Preflight validation helpers for agent actions.

Extracted from actions.py — all `_preflight_*` helpers, `run_preflight`,
`diagnose_live_failure`, and the scope/margin helpers they depend on.
"""

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Shared F&O quantity-safety caps (2026-09-27 audit — SSOT consolidation).
# Previously each of /ticket (orders_place.py), /basket (orders_basket.py),
# and this module's own `_preflight_validate_lots` hardcoded its own literal
# `5` / `20` independently — same value today only by coincidence, with no
# guard against them silently drifting apart on a future edit to just one
# call site. Both /ticket and /basket import these directly rather than
# redefining them.
MCX_MAX_LOTS = 20
FO_FAT_FINGER_LOT_CAP = 5


def _live_positions_in_scope(context: dict, params: dict) -> list[dict]:
    """
    Mirror of `_sim_positions_in_scope` for the real-data paper path.
    Pulls rows from `context['df_positions']` (the live Kite snapshot
    threaded through by `_task_performance`) filtered by scope.
    """
    scope = (params.get("scope") or "total").lower()
    acct_filter = str(params.get("account") or "") if scope == "account" else None
    df = context.get("df_positions")
    if df is None or getattr(df, "empty", True):
        return []
    try:
        rows = df.to_dict(orient="records")
    except Exception:
        return []
    if acct_filter:
        rows = [r for r in rows if str(r.get("account")) == acct_filter]
    return rows


async def _basket_margin_validate(broker, order: dict) -> tuple[bool, str]:
    """
    Ask the real account's broker to dry-run the order via its own
    basket-margin endpoint. Returns (ok, detail). On `ok=False` the
    detail is the broker's error message — mirror of what `place_order`
    would have rejected with.

    2026-10 fix (item 11): this hardcoded Kite's `to_kite_qty` regardless
    of which broker the account actually is — for a Groww MCX leg this
    sent the margin endpoint a LOTS-shaped qty while Groww's real order
    sends CONTRACTS, undercounting margin by a factor of `lot_size` in
    this paper-mode dry-run check only (no real placement path affected).
    Fixed: use `broker.translate_qty` (resolved for the real account's
    broker, already in scope via the `broker` param) instead.
    """
    try:
        from backend.brokers.adapters.kite import get_lot_size
        exchange  = order.get("exchange", "NFO")
        symbol    = order.get("symbol") or ""
        raw_qty   = int(order.get("qty") or 0)
        lot_size  = await get_lot_size(exchange, symbol)
        wire_qty  = broker.translate_qty(exchange, raw_qty, lot_size)
        basket_order = {
            "exchange":         exchange,
            "tradingsymbol":    symbol,
            "transaction_type": order.get("side"),
            "quantity":         wire_qty,
            "order_type":       "LIMIT",
            "product":          order.get("product", "NRML"),
            "price":            order.get("price"),
            "variety":          order.get("variety", "regular"),
        }
        # basket_order_margins lives on the Broker ABC — every adapter
        # validates orders without placing them. Sync HTTP under the
        # hood, so offload to a thread to keep the event loop free.
        import asyncio
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, broker.basket_order_margins, [basket_order])
        return True, "basket_margin OK"
    except Exception as e:
        return False, str(e)[:240]


async def _preflight_validate_lots(
    exchange: str,
    symbol: str,
    qty: int,
    is_close: bool,
) -> tuple[list[dict], bool]:
    """
    G1 (LOT_MULTIPLE) + G2 (FAT_FINGER_5_LOT_CAP) guards.

    G1 applies uniformly to every F&O exchange, including MCX/NCO — qty
    reaching this preflight is always already normalized to CONTRACTS
    (see `backend/brokers/broker_apis.py:_annotate_lot_size`), so the
    lot-multiple check is exchange-agnostic. G2 (FAT_FINGER_5_LOT_CAP)
    still skips MCX/NCO, but for an independent reason: the route-level
    MCX size guard (20-lot cap, 422) is the authoritative fat-finger
    check for that exchange.

    Returns (blockers, hard_stop). hard_stop=True when LOT_MULTIPLE or
    LOT_SIZE_UNKNOWN fires — caller should short-circuit immediately.
    FAT_FINGER_5_LOT_CAP does NOT set hard_stop; broker instruments may
    also report QTY_FREEZE which must be surfaced alongside.
    """
    blockers: list[dict] = []
    _FO_EXCHANGES = ("NFO", "MCX", "NCO", "CDS", "BFO")
    if exchange not in _FO_EXCHANGES or qty <= 0 or not symbol:
        return blockers, False

    try:
        from backend.brokers.adapters.kite import get_lot_size as _pf_get_lot_size
        _pf_lot = int(await _pf_get_lot_size(exchange, symbol) or 0)
    except Exception:
        _pf_lot = 0

    # Position/order quantities reaching this preflight are already
    # normalized to CONTRACTS for every exchange, including MCX/NCO —
    # `backend/brokers/broker_apis.py:_annotate_lot_size` (~line
    # 1992-2003) converts MCX/NCO position quantity columns (and the
    # overnight/day_buy/day_sell variants) from broker-native LOTS to
    # CONTRACTS before a positions row ever reaches an agent-driven
    # `place_order`/`close_position` call that lands here. The prior
    # premise — "MCX/NCO qty is already in lots, so a G1 lot-multiple
    # check would wrongly reject a valid whole-lot quantity" — is FALSE
    # for the paths that reach this function; skipping G1 for MCX/NCO
    # instead let genuinely non-multiple quantities through unchecked,
    # bypassing every ceiling. The G1 check itself (`qty % lot_size`) is
    # exchange-agnostic and correct once qty is confirmed to be in
    # contracts, so it now applies uniformly to every F&O exchange.
    #
    # FAT_FINGER_5_LOT_CAP still skips MCX/NCO below, but for an
    # INDEPENDENT, still-valid reason unrelated to the units premise
    # above: the route-level MCX size guard (20-lot cap, 422) is the
    # authoritative fat-finger check for that exchange, and re-applying
    # a 5-lot cap here would shadow it with a spurious preflight block.
    _is_mcx_exch = exchange in ("MCX", "NCO")

    if _pf_lot > 1:
        if qty % _pf_lot != 0:
            blockers.append({
                "code": "LOT_MULTIPLE",
                "reason": (
                    f"qty={qty} is not a multiple of "
                    f"lot_size={_pf_lot} (would be "
                    f"{qty / _pf_lot:.2f} lots)"
                ),
                "fix": (
                    f"send qty={_pf_lot} for 1 lot, or N × {_pf_lot} for N lots"
                ),
                "data": {"qty": qty, "lot_size": _pf_lot},
            })
        elif not is_close and not _is_mcx_exch:
            _pf_lots = qty // _pf_lot
            if _pf_lots > FO_FAT_FINGER_LOT_CAP:
                blockers.append({
                    "code": "FAT_FINGER_5_LOT_CAP",
                    "reason": (
                        f"{_pf_lots} lots exceeds the {FO_FAT_FINGER_LOT_CAP}-lot "
                        f"safety cap (qty={qty}, lot_size={_pf_lot})"
                    ),
                    "fix": (
                        f"split into ≤{FO_FAT_FINGER_LOT_CAP}-lot orders or "
                        "contact ops to raise the cap"
                    ),
                    "data": {"qty": qty, "lot_size": _pf_lot,
                             "lots": _pf_lots, "cap": FO_FAT_FINGER_LOT_CAP},
                })

    # MCX/NCO cold-cache: the route raises 503 before calling preflight,
    # so LOT_SIZE_UNKNOWN should never fire here for live orders. It remains
    # as a backstop for agent-driven calls that bypass the route-level guard.
    hard_codes = {"LOT_MULTIPLE", "LOT_SIZE_UNKNOWN"}
    hard_stop = any(b["code"] in hard_codes for b in blockers)
    return blockers, hard_stop


def _preflight_validate_account(account: str) -> dict | None:
    """
    Check that *account* is present in the loaded broker connections.

    Returns a blocker dict when unknown, None when valid.
    Side-effect free; does not modify any shared state.
    """
    from backend.brokers.connections import Connections
    conns = Connections()
    loaded_accounts: set[str] = set(conns.conn.keys())
    # Cutover branch — local Connections is empty when conn_service owns
    # the sessions, so consult /internal/accounts for the canonical list.
    from backend.brokers.client import is_cutover_on
    if is_cutover_on() and not loaded_accounts:
        from backend.brokers.client.remote_broker import list_remote_accounts
        loaded_accounts = {r["account"] for r in list_remote_accounts() if r.get("account")}
    if account not in loaded_accounts:
        from backend.shared.helpers.utils import mask_account
        masked = mask_account(account) if account else account
        return {
            "code":   "ACCOUNT_UNKNOWN",
            "reason": f"Account {masked} not loaded in broker connections",
            "fix":    "Add the account in /admin/brokers and verify it shows LOADED",
            "data":   {},
        }
    return None


async def _preflight_build_basket_orders(
    broker: object,
    exchange: str,
    symbol: str,
    side: str,
    qty: int,
    order_type: str,
    product: str,
    variety: str,
    price: float,
    paired_orders: list[dict] | None,
) -> list[dict]:
    """
    Build the list of basket-order dicts for basket_order_margins.

    The primary leg is always index 0. Each paired leg is appended;
    invalid paired legs (missing symbol, zero qty, or any exception)
    are skipped with a debug log.
    """
    from backend.brokers.adapters.kite import get_lot_size

    _lot_size = await get_lot_size(exchange, symbol)
    _broker_qty = broker.normalise_qty(exchange, qty, _lot_size)
    primary_leg = {
        "exchange":         exchange,
        "tradingsymbol":    symbol,
        "transaction_type": side,
        "quantity":         _broker_qty,
        "order_type":       order_type,
        "product":          product,
        "price":            float(price) if price else 0.0,
        "variety":          variety,
    }
    basket: list[dict] = [primary_leg]
    # Paired legs (typically the template's wing) factored into the
    # basket. Kite's basket_order_margins returns the NET margin across
    # every leg, so a short option + protective long wing reads as the
    # capped spread margin instead of the naked SPAN. Only a caller that
    # explicitly passes `paired_orders` gets this netting — today that is
    # only the read-only `/preflight` route when an API client sends
    # `paired_legs`. No submit-time caller passes it: the wing is placed
    # on parent FILL, so the broker prices the naked parent at placement,
    # and the frontend margin preview is naked too (2026-10 item 8).
    for pl in paired_orders or []:
        try:
            _pl_exchange = str(pl.get("exchange") or exchange)
            _pl_symbol   = str(pl.get("tradingsymbol") or pl.get("symbol") or "")
            if not _pl_symbol:
                continue
            _pl_qty = int(pl.get("quantity") or 0)
            if _pl_qty <= 0:
                continue
            _pl_lot = await get_lot_size(_pl_exchange, _pl_symbol)
            basket.append({
                "exchange":         _pl_exchange,
                "tradingsymbol":    _pl_symbol,
                "transaction_type": str(pl.get("transaction_type") or pl.get("side") or "BUY"),
                "quantity":         broker.normalise_qty(_pl_exchange, _pl_qty, _pl_lot),
                "order_type":       str(pl.get("order_type") or "MARKET"),
                "product":          str(pl.get("product") or product),
                "price":            float(pl.get("price") or 0),
                "variety":          str(pl.get("variety") or "regular"),
            })
        except Exception as _e:
            logger.debug(f"[PREFLIGHT] paired leg skipped: {_e}")
    return basket


def _preflight_leg_required(entry: dict) -> float | None:
    """
    Extract the required margin for a single basket-margin leg entry.

    Prefers `final.total` (net spread margin) over `initial.total`
    (naked margin) — Kite's nested shape. Falls back to the top-level
    `required` field, then to a FLAT top-level `total` key — Dhan's
    `_margin_for_order` / Groww's `basket_order_margins` both return
    `{"total": ..., "var": ..., "exposure": ..., "available": {...}}`
    with no `final`/`initial` nesting at all, so without this fallback
    `required` silently read as 0.0 for 100% of Dhan/Groww orders
    (confirmed directly: `_preflight_leg_required({"total": 150000.0})`
    returned 0.0 pre-fix) — the margin-shortfall gate was completely
    inert for those two brokers. Checked LAST (after final/initial/
    required) so a genuine Kite response is parsed exactly as before;
    this only ever engages when Kite's nested keys are absent.

    Returns None (not 0.0) when nothing parses — a missing/unparseable
    value is "unknown", never "confirmed zero margin needed" (missing-
    vs-zero convention, item 5).
    """
    if not isinstance(entry, dict):
        return None
    for branch in ("final", "initial"):
        slot = (entry.get(branch) or {}).get("total")
        if slot is not None:
            try:
                return float(slot)
            except (TypeError, ValueError):
                pass
    for flat_key in ("required", "total"):
        val = entry.get(flat_key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass
    return None


def _preflight_parse_basket_margin(bm_result: object) -> float | None:
    """
    Parse the raw return value of broker.basket_order_margins into a
    single ``required`` float, or None when it genuinely can't be
    determined.

    Kite returns a list (one dict per leg) or occasionally a bare dict.
    Multi-leg: sum per-leg final/initial totals so the spread offset is
    captured — but if ANY leg's own required margin is unknown (None),
    the WHOLE basket is unknown (fail open on the whole basket, never a
    silent partial sum that understates the real requirement). Single-
    leg list: unwrap and read the single entry. Dict fallback: read as
    a single entry.
    """
    if isinstance(bm_result, list) and bm_result:
        if len(bm_result) > 1:
            legs = [_preflight_leg_required(r) for r in bm_result]
            if any(leg is None for leg in legs):
                return None
            return float(sum(legs))
        return _preflight_leg_required(bm_result[0])
    return _preflight_leg_required(bm_result if isinstance(bm_result, dict) else {})


def _preflight_check_segment(
    profile_res: dict | None,
    exchange: str,
) -> dict | None:
    """
    Return a SEGMENT_INACTIVE blocker when *exchange* is not in the
    broker profile's enabled exchange list, or None when the check passes
    (or is not applicable because profile_res is None / empty).
    """
    if profile_res is None:
        return None
    enabled_exchanges = set(profile_res.get("exchanges") or [])
    if enabled_exchanges and exchange not in enabled_exchanges:
        return {
            "code":   "SEGMENT_INACTIVE",
            "reason": f"{exchange} segment not activated on this account",
            "fix":    (f"Activate the {exchange} segment in the Kite developer "
                       "console for this account, then re-test"),
            "data":   {"enabled_exchanges": sorted(enabled_exchanges)},
        }
    return None


def _preflight_check_qty_freeze(
    instruments_res: list | None,
    symbol: str,
    qty: int,
) -> dict | None:
    """
    Return a QTY_FREEZE blocker when *qty* exceeds the instrument's
    exchange-imposed freeze quantity, or None when the check passes
    (or is not applicable because instruments_res is None / empty).
    """
    if not instruments_res:
        return None
    freeze_qty: int | None = None
    lot_size: int = 1
    for inst in instruments_res:
        if inst.get("tradingsymbol") == symbol:
            freeze_qty = inst.get("freeze_qty") or None
            lot_size   = int(inst.get("lot_size") or 1)
            break
    if freeze_qty is not None and qty > int(freeze_qty):
        max_qty  = int(freeze_qty)
        max_lots = max(1, max_qty // lot_size) if lot_size > 0 else max_qty
        return {
            "code":   "QTY_FREEZE",
            "reason": (f"Quantity {qty} exceeds {symbol} freeze qty "
                       f"{freeze_qty}"),
            "fix":    (f"Reduce qty to {max_qty:,} "
                       f"({max_lots:,} lot{'s' if max_lots != 1 else ''}) "
                       "or split into multiple orders"),
            "data":   {
                "freeze_qty": int(freeze_qty),
                "lot_size":   lot_size,
                "requested":  qty,
            },
        }
    return None


def _preflight_resolve_available_margin(
    margins_res: tuple,
    segment: str,
    account: str,
) -> tuple[bool | None, float | None]:
    """
    Parse the (segment_dict, err_str_or_None) tuple from _preflight_fetch_account_margins
    into ``(m_enabled, available)``.

    Returns (None, None) on error or when the margin call failed.
    """
    import math
    m, _m_err = (margins_res or (None, None))
    m = m or {}
    if _m_err:
        logger.warning(f"[PREFLIGHT] margins({segment}) failed for {account}: {_m_err}")
        return None, None
    m_enabled = bool(m.get("enabled"))
    net = m.get("net")
    if isinstance(net, (int, float)) and not math.isnan(float(net)):
        return m_enabled, float(net)
    return m_enabled, None


def _preflight_margin_shortfall_fix_qty(
    required: float,
    available: float | None,
    qty: int,
    exchange: str = "",
    lot_size: int = 0,
) -> str:
    """
    Compute the ``fix_qty`` suffix for MARGIN_SHORTFALL blockers.

    Returns a string like " or reduce qty to 25" when per-unit margin
    can be inferred, or an empty string when the calculation is not
    possible (required=0, available=0, qty=0, or less than one full
    lot fits — see item 10 below).

    ``required`` is the total rupee margin the broker returned for the
    full order (the wire-unit conversion inside `broker.normalise_qty` /
    `translate_qty` is internal to the adapter and does not change the
    rupee figure). `qty` here is always CONTRACTS. The per-lot pricing
    below is NOT because `required` is denominated in lots — it exists so
    the suggested reduced quantity is a whole-lot multiple.

    2026-10 fix (item 10): this used to round the suggested qty down to
    a lot multiple ONLY for MCX/NCO; the NFO/BFO/CDS branch returned a
    raw `int(available / per_unit)` contract count with NO lot
    rounding, so the suggestion could itself be a fractional-lot qty
    that would trip the G1 lot-multiple guard if the operator typed it
    in literally. Fixed by pricing per LOT uniformly whenever
    `lot_size > 1` (true for every F&O exchange, not just MCX/NCO —
    `qty` reaching this preflight is always already a clean lot
    multiple per G1, so `qty // lot_size` is exact): price per lot
    (`required / (qty // lot_size)`), fit lots against `available`,
    then convert back to contracts for display. When less than one
    full lot fits, returns "" (no suggestion) rather than a sub-lot
    number.
    """
    if not (required > 0 and available is not None and available > 0):
        return ""
    _has_lots = lot_size > 1
    if _has_lots:
        lots = qty // lot_size
        per_unit = required / lots if lots > 0 else 0
    else:
        per_unit = required / qty if qty > 0 else 0
    if per_unit <= 0:
        return ""
    try:
        max_units_fit = max(0, int(available / per_unit))
        max_qty_fit = max_units_fit * lot_size if _has_lots else max_units_fit
        return f" or reduce qty to {max_qty_fit:,}" if max_qty_fit > 0 else ""
    except Exception:
        return ""


def _preflight_margin_policy(broker, account: str, side: str) -> dict:
    """
    Build the `margin_ctx` dict threaded through every margin-blocker
    emission site (item 6).

    Kite's gate is always enforced — numerically trustworthy, already
    enforcing today, and `basket_order_margins` already nets a close via
    `consider_positions=True` so it never needs a close exemption.

    Dhan/Groww each default to LOG-ONLY (computed + logged at WARNING,
    never blocking) via their own `preflight.enforce_margin_dhan` /
    `preflight.enforce_margin_groww` settings (both default False) —
    operator-tunable from /admin/settings with no deploy — until the
    orchestrator flips them after confirming real live numbers from a
    dev-deploy verification pass. The broker id is read off the already-
    resolved `broker` object (`broker.broker_id`), not a second account
    lookup — reliable in conn-service/RemoteBroker mode too, since
    `registry.get_broker()` constructs `RemoteBroker(account,
    broker_id=_broker_id_for(account))` with the real DB-backed id.

    Uses an explicit ALLOWLIST (`{"dhan", "groww"}`), not a `!=
    "zerodha_kite"` exclusion — a future 4th broker must not silently
    inherit either the close exemption or the log-only default without
    an explicit decision.
    """
    from backend.shared.helpers.settings import get_bool

    bid = str(getattr(broker, "broker_id", "") or "")
    if bid == "dhan":
        enforce = get_bool("preflight.enforce_margin_dhan", False)
    elif bid == "groww":
        enforce = get_bool("preflight.enforce_margin_groww", False)
    else:
        enforce = True
    return {
        "is_dhan_groww": bid in ("dhan", "groww"),
        "enforce": enforce,
        "side": side,
        "broker_id": bid,
    }


async def _preflight_emit_margin_blocker(
    blockers: list[dict],
    diag: dict,
    blocker: dict,
    *,
    margin_ctx: dict,
    account: str,
    symbol: str,
    exchange: str,
    qty: int,
) -> None:
    """
    SOLE gate every margin-shortfall/insufficient-funds blocker must pass
    through — every call site that would otherwise `blockers.append(...)`
    a MARGIN_SHORTFALL or INSUFFICIENT_FUNDS blocker calls this instead
    (item 6). A second, un-gated append site anywhere in this module
    would make the Dhan/Groww "log-only" default not actually log-only.

    Applies, in order:

    1. Close exemption — SCOPED TO DHAN/GROWW ONLY (`margin_ctx
       ["is_dhan_groww"]`). Kite's own `basket_order_margins` already
       nets a close via `consider_positions=True`, so it is never
       reached with an unnecessary close-path blocker in the first
       place — no change to Kite's existing behaviour here. Dhan's
       `margin_calculator` and Groww's `get_order_margin_details` price
       each order INDEPENDENTLY with no concept of existing positions,
       so once `required` reads real numbers (item 4), an unexempted
       close would be priced as a brand-new position in that direction
       and could get wrongly blocked by the very gate that exists to
       let margin-tight closes through.

       SERVER-VERIFIED ONLY — via `_verify_close_intent` (sign AND
       magnitude checked against the REAL broker position). The raw
       caller-claimed `order["intent"] == "close"` is read only to
       decide whether `run_preflight` even bypasses G2 (unrelated, pre-
       existing behaviour); it is NEVER trusted here. This re-verifies
       inside the shared gate itself rather than trusting any caller's
       own prior verification, so every current and future
       `run_preflight` caller gets the same real protection uniformly —
       confirmed against every current caller (ticket, basket, the
       agent `place_order` action, `_action_live_close_position`,
       `_chase_build_tasks`, the `/preflight` preview route): none of
       them could reach this exemption with an unverified claim, because
       verification happens HERE regardless of what they passed in.

    2. Enforce-vs-log-only — when not enforced, the blocker is recorded
       in `diag["margin_would_block"]` and logged at WARNING instead of
       being appended to `blockers`, so the order is never actually
       blocked.
    """
    side = margin_ctx.get("side") or ""
    if margin_ctx.get("is_dhan_groww") and side and qty:
        try:
            from backend.api.routes.orders_place import _verify_close_intent
            if await _verify_close_intent(symbol, exchange, side, account, qty):
                diag["close_exemption"] = True
                logger.info(
                    f"[PREFLIGHT] close exemption granted for {account}/{symbol} "
                    f"({exchange}) — server-verified close, {blocker.get('code')} "
                    f"suppressed"
                )
                return
        except Exception as e:
            logger.debug(
                f"[PREFLIGHT] close-intent verification failed for {account}: {e}"
            )

    if margin_ctx.get("enforce", True):
        blockers.append(blocker)
        return

    diag.setdefault("margin_would_block", []).append(blocker)
    logger.warning(
        f"[PREFLIGHT] margin gate LOG-ONLY (preflight.enforce_margin_"
        f"{margin_ctx.get('broker_id', '?')}=False) for {account}/{symbol}: "
        f"would have blocked with code={blocker.get('code')} "
        f"reason={blocker.get('reason')}"
    )


def _preflight_collect_leg_errors(bm_result: object) -> list[str]:
    """Collect per-leg `error` strings from a basket-margin result whose
    `required` could not be parsed (None) — used to route a genuine
    Dhan/Groww-side margin failure into the same keyword-matching
    classification the raised-exception branch already applies, instead
    of silently treating every unparseable result as "margin check not
    needed"."""
    errors: list[str] = []
    if isinstance(bm_result, list):
        errors = [str(r.get("error")) for r in bm_result
                  if isinstance(r, dict) and r.get("error")]
    elif isinstance(bm_result, dict) and bm_result.get("error"):
        errors = [str(bm_result.get("error"))]
    return errors


async def _preflight_handle_positive_margin(
    required: float,
    margins_res: tuple,
    segment: str,
    account: str,
    qty: int,
    exchange: str = "",
    lot_size: int = 0,
    margin_ctx: dict | None = None,
    symbol: str = "",
) -> tuple[list[dict], dict]:
    """
    Handle the ``required >= 0`` branch of the margin gate.

    Parses available margin, emits INSUFFICIENT_FUNDS when available=0,
    computes shortfall, and builds MARGIN_SHORTFALL blockers — both
    routed through `_preflight_emit_margin_blocker` (close exemption +
    enforce-vs-log-only policy, item 6).

    Returns ``(new_blockers, diag_partial)``.
    """
    import math
    new_blockers: list[dict] = []
    diag: dict = {}
    shortfall: float = 0.0
    margin_ctx = margin_ctx or {"is_dhan_groww": False, "enforce": True, "side": ""}

    m_enabled, available = _preflight_resolve_available_margin(margins_res, segment, account)

    diag["basket_margin_used"] = required
    diag["available_margin"]   = available
    diag["margin_wallet"]      = ((margins_res or (None, None))[0] or {}).get("_wallet", segment)
    diag["margin_shortfall"]   = None

    # ── Available-is-zero gate ────────────────────────────────────
    # When available=0 AND required>0 AND segment is enabled, the
    # order has zero chance of going through — block immediately.
    if m_enabled and available == 0.0 and required > 0:
        logger.warning(
            f"[PREFLIGHT] available_margin=0 with required={required:.2f} "
            f"for {account} — blocking as INSUFFICIENT_FUNDS."
        )
        await _preflight_emit_margin_blocker(
            new_blockers, diag,
            {
                "code":   "INSUFFICIENT_FUNDS",
                "reason": (
                    f"Available margin is ₹0 but order requires "
                    f"₹{required:,.0f}. Account has no usable balance "
                    f"in the {segment} segment."
                ),
                "fix": (
                    f"Add at least ₹{required:,.0f} to the {segment} wallet "
                    f"before placing this order."
                ),
                "data": {"required": required, "available": 0.0},
            },
            margin_ctx=margin_ctx, account=account, symbol=symbol,
            exchange=exchange, qty=qty,
        )
        diag["margin_shortfall"] = required

    # Four states for the margin gate:
    #   1. enabled=False (or margins call failed): DO NOT block.
    #   2. enabled=True + net = 0: blocked above as INSUFFICIENT_FUNDS.
    #   3. enabled=True + net < required: real shortfall — block.
    #   4. enabled=True + net >= required: pass.
    if m_enabled and available is not None and available != 0.0:
        shortfall = max(0.0, required - available)
        diag["margin_shortfall"] = shortfall if shortfall > 0 else None
    elif required > 0 and not (m_enabled and available == 0.0):
        logger.info(
            f"[PREFLIGHT] margin check SKIPPED for {account} "
            f"(segment={segment} enabled={m_enabled} available={available}); "
            f"required={required:.2f} — relying on Kite place_order for the real verdict"
        )

    if shortfall > 0 and not math.isnan(shortfall):
        fix_qty = _preflight_margin_shortfall_fix_qty(
            required, available, qty, exchange=exchange, lot_size=lot_size
        )
        await _preflight_emit_margin_blocker(
            new_blockers, diag,
            {
                "code":   "MARGIN_SHORTFALL",
                "reason": (f"Required margin ₹{required:,.0f} exceeds available "
                           f"₹{available:,.0f} (shortfall ₹{shortfall:,.0f})"),
                "fix":    (f"Add ₹{shortfall:,.0f} more margin to the account" + fix_qty),
                "data":   {
                    "required":   required,
                    "available":  available,
                    "shortfall":  shortfall,
                },
            },
            margin_ctx=margin_ctx, account=account, symbol=symbol,
            exchange=exchange, qty=qty,
        )

    return new_blockers, diag


async def _preflight_check_margin(
    bm_res: object,
    margins_res: tuple,
    segment: str,
    account: str,
    symbol: str,
    qty: int,
    loop: object,
    exchange: str = "",
    lot_size: int = 0,
    margin_ctx: dict | None = None,
) -> tuple[list[dict], dict]:
    """
    Evaluate basket-order margin and account margin to emit blockers.

    Returns ``(new_blockers, diag_updates)`` where *diag_updates* is a
    partial diagnostics dict that the caller should merge into its own.
    Never raises — broker-call failures are handled internally.
    """
    new_blockers: list[dict] = []
    diag: dict = {}
    margin_ctx = margin_ctx or {"is_dhan_groww": False, "enforce": True, "side": ""}

    if isinstance(bm_res, Exception):
        bm_exception: Exception | None = bm_res
        bm_result = None
    else:
        bm_exception = None
        bm_result = bm_res

    try:
        if bm_exception is not None:
            raise bm_exception

        required = _preflight_parse_basket_margin(bm_result)

        # ── Unknown-margin gate (item 4/5) ────────────────────────────
        # `required` is None when no leg's margin could be parsed at
        # all (missing/unparseable fields on every leg — a genuinely
        # different situation from Kite's "negative required" credit
        # case below, which is a real parsed number). Fail OPEN on the
        # WHOLE basket rather than silently treating an unknown amount
        # as zero: if any leg carried its own `error` string, route that
        # text through the SAME keyword-matching classification the
        # raised-exception branch below already applies (re-raising
        # reuses that logic rather than duplicating it) so a genuine
        # Dhan/Groww-side margin failure still surfaces as
        # MARGIN_SHORTFALL; otherwise log and skip — broker's own
        # place_order remains the real backstop.
        if required is None:
            diag["basket_margin_used"] = None
            diag["available_margin"]   = None
            diag["margin_shortfall"]   = None
            _leg_errors = _preflight_collect_leg_errors(bm_result)
            if _leg_errors:
                raise RuntimeError(" | ".join(_leg_errors)[:240])
            logger.info(
                f"[PREFLIGHT] margin required could not be determined for "
                f"{account}/{symbol} (no parseable total on any leg, no "
                f"leg error) — skipping margin-shortfall gate; broker's "
                f"own place_order remains the real backstop."
            )

        # ── Negative-margin sanity check ────────────────────────────────
        # Kite's basket_order_margins can return a negative `required` value
        # when existing positions on the account NET with the new leg to
        # release margin (deep-OTM short option positions carrying "credit
        # margin" at the basket level). That's a LEGITIMATE outcome — the
        # operator receives premium and the basket's total margin drops.
        #
        # The original safety-blocker landed on 2026-06-30 to catch the
        # "qty sent in lots instead of contracts" bug that produced grossly
        # over-sized negative margins (up to -₹8.5cr on a 1-lot order).
        # That bug is now prevented at the source by `translate_qty` (same
        # commit 29f3ef58), so this branch no longer needs to block — the
        # qty unit is guaranteed correct before the broker call.
        #
        # We keep the WARNING log + diagnostic surfacing so the operator
        # sees any unusual value; Kite's own place_order remains the
        # ultimate gate for insufficient funds.
        elif required < 0:
            logger.warning(
                f"[PREFLIGHT] negative basket margin for {account}/{symbol}: "
                f"required={required:.2f} — treating as legitimate netting "
                f"credit (qty={qty} is unit-safe via "
                f"translate_qty). Kite's place_order will reject if the "
                f"actual SPAN check fails."
            )
            diag["basket_margin_used"]   = required
            diag["available_margin"]     = None
            diag["margin_shortfall"]     = None
            diag["negative_margin_note"] = (
                "Broker returned a credit basket margin — usually indicates "
                "existing positions net with the new leg. Broker will still "
                "verify at order-placement time."
            )

        else:
            _pm_blockers, _pm_diag = await _preflight_handle_positive_margin(
                required, margins_res, segment, account, qty,
                exchange=exchange, lot_size=lot_size,
                margin_ctx=margin_ctx, symbol=symbol,
            )
            new_blockers.extend(_pm_blockers)
            diag.update(_pm_diag)
    except Exception as e:
        bm_msg = str(e).lower()
        # basket_margin raised — interpret the error signal.
        if any(k in bm_msg for k in ("margin", "fund", "shortfall", "balance")):
            await _preflight_emit_margin_blocker(
                new_blockers, diag,
                {
                    "code":   "MARGIN_SHORTFALL",
                    "reason": f"Margin check failed: {str(e)[:160]}",
                    "fix":    "Add margin to the account or reduce quantity",
                    "data":   {"broker_error": str(e)[:240]},
                },
                margin_ctx=margin_ctx, account=account, symbol=symbol,
                exchange=exchange, qty=qty,
            )
        else:
            logger.debug(f"[PREFLIGHT] basket_margin raised for {account}: {e}")

    return new_blockers, diag


async def _preflight_fetch_account_margins(broker, loop, segment: str) -> "tuple[dict, str | None]":
    """Fetch account margin from the broker and normalise to a flat dict.

    Returns (margin_dict, error_str_or_None).

    Broker shape detection:
      Kite returns nested: {"equity": {"net": ..., ...}, "commodity": {...}}
      Dhan returns flat:   {"net": ..., "available": ..., "utilised": ...}

    For Kite we slice by segment; for Dhan the flat dict is returned
    directly so downstream margin checks can read "available" / "net"
    without getting an empty dict.

    Raised as the inner try/except so `TypeError` on the no-arg call
    (some adapters accept an optional segment arg) falls back to the
    segmented call transparently.
    """
    try:
        # Un-segmented call first (returns both wallets; some
        # accounts report enabled=True there but False on the
        # segmented call due to a Kite scope quirk).
        try:
            m_all = await loop.run_in_executor(None, broker.margins)
            m = m_all or {}
            # Kite returns nested: {"equity": {...}, "commodity": {...}}
            # Dhan returns flat:   {"net": ..., "available": ..., ...}
            # Detect flat shape by presence of top-level numeric keys so
            # Dhan preflight margin gate doesn't silently return {} and
            # pass every order regardless of available margin.
            if "net" in m or "available" in m:
                return m, None
            seg = m.get(segment, {})
            equity = m.get("equity") or {}
            if (segment != "equity" and not seg.get("enabled")
                    and equity.get("enabled")):
                return {**equity, "_wallet": "equity"}, None
            return {**seg, "_wallet": segment}, None
        except TypeError:
            return await loop.run_in_executor(
                None, broker.margins, segment), None
    except Exception as e:
        return None, str(e)


_EXCHANGE_SEGMENT: dict[str, str] = {
    "MCX": "commodity", "NCO": "commodity",
    "CDS": "currency",  "BCD": "currency",
}


async def _preflight_fetch_profile(broker, loop, account: str):
    if broker.broker_id != "zerodha_kite":
        return None
    try:
        return await loop.run_in_executor(None, broker.profile)
    except Exception as e:
        logger.debug(f"[PREFLIGHT] profile fetch failed for {account}: {e}")
        return None


# R1 fix (2026-09) — cache the preflight instruments dump behind a
# short TTL. Pre-fix, every `run_preflight` call (each debounced
# margin-preview keystroke AND every ticket placement) fired a fresh
# uncached full-exchange `broker.instruments(exchange)` download —
# tens of thousands of rows — even though the same (account, exchange)
# dump is valid for several seconds. A literal swap to the existing
# `get_or_fetch("instruments", _fetch_instruments, ...)` cache used by
# `_align_price_to_tick`/`get_lot_size` is NOT viable here: that
# cache's `InstrumentsResponse.items` are trimmed msgspec structs
# (`s`/`e`/`t`/`ls`/`ts`/`u`/`x`/`k`, no `.get()`, no `freeze_qty`
# field), while `_preflight_check_qty_freeze` needs the RAW broker
# dict shape (`inst.get("tradingsymbol")`, `inst.get("freeze_qty")`,
# `inst.get("lot_size")`). It would also trigger a cold 5-exchange
# dump synchronously inside a request if that shared cache hadn't been
# warmed yet — the same "no T+0 broker downloads inside a request"
# pattern that caused the Aug-2026 OOM incident. So this reuses the
# `get_or_fetch` PRIMITIVE with its own dedicated short-TTL cache key
# and fetcher instead, preserving the raw dict shape.
_PREFLIGHT_INSTR_CACHE_PREFIX = "preflight_instr:"
_PREFLIGHT_INSTR_TTL_S = 5  # covers the 350ms-debounced margin-preview storm


def _preflight_trim_instruments(raw: list | None) -> list[dict]:
    """Trim a raw broker instruments(exchange) dump down to only the
    3 fields `_preflight_check_qty_freeze` reads.

    `get_or_fetch` never proactively evicts expired entries (they sit
    in `cache._store` until the next call for the same key overwrites
    them), so caching the FULL raw dump — tens of thousands of rows ×
    ~20 keys each, per (account, exchange) — would grow unbounded
    memory the same way the shared `instruments.py` cache did before
    it was trimmed to slim structs (the Aug-2026 OOM incident). A
    trimmed 3-key dict list keeps the retained footprint a small
    fraction of the raw response while preserving the exact shape
    `_preflight_check_qty_freeze` needs (`.get("tradingsymbol")`,
    `.get("freeze_qty")`, `.get("lot_size")`).
    """
    return [
        {
            "tradingsymbol": r.get("tradingsymbol"),
            "freeze_qty":    r.get("freeze_qty"),
            "lot_size":      r.get("lot_size"),
        }
        for r in (raw or [])
    ]


async def _preflight_fetch_instruments(broker, exchange: str, qty: int, account: str):
    """Cached instruments(exchange) fetch for the QTY_FREEZE gate.

    Key is `{account}:{exchange}` (string-keyed, not `id(broker)`) —
    `registry.get_broker()` constructs a fresh adapter instance on
    every call, so keying on `id(broker)` would never hit the cache in
    production. `get_or_fetch` handles the sync-fetcher-to-thread
    offload internally, so no explicit `loop.run_in_executor` call is
    needed here (the `loop` param this function previously took is no
    longer used — see the `run_preflight` call site). The cached value
    is TRIMMED (see `_preflight_trim_instruments`), not the raw broker
    response.
    """
    if exchange not in ("NFO", "BFO", "MCX", "CDS") or qty <= 0:
        return None
    from backend.api.cache import get_or_fetch

    def _fetch_and_trim():
        return _preflight_trim_instruments(broker.instruments(exchange))

    try:
        return await get_or_fetch(
            f"{_PREFLIGHT_INSTR_CACHE_PREFIX}{account}:{exchange}",
            _fetch_and_trim,
            ttl_seconds=_PREFLIGHT_INSTR_TTL_S,
        )
    except Exception as e:
        logger.debug(f"[PREFLIGHT] instruments fetch failed for {account}/{exchange}: {e}")
        return None


async def _preflight_fetch_basket_margin(broker, loop, basket_orders: list):
    try:
        return await loop.run_in_executor(None, broker.basket_order_margins, basket_orders)
    except Exception as e:
        return e


def _parse_lot_check_fields(order: dict) -> "tuple[str, str, str, int]":
    """Extract (exchange, symbol, intent, qty) for the G1/G2 lot guards."""
    exch   = str(order.get("exchange") or "").upper()
    sym    = str(order.get("tradingsymbol") or order.get("symbol") or "").upper()
    intent = str(order.get("intent") or "").lower()
    try:
        qty = int(order.get("quantity") or order.get("qty") or 0)
    except Exception:
        qty = 0
    return exch, sym, intent, qty


def _parse_broker_call_fields(order: dict) -> "tuple[str, str, int, str, object, str, str, str]":
    """Extract order fields needed for broker profile/instruments/margin calls."""
    exchange   = str(order.get("exchange", "NFO"))
    symbol     = str(order.get("tradingsymbol") or order.get("symbol", ""))
    qty        = int(order.get("quantity") or order.get("qty") or 0)
    side       = str(order.get("transaction_type") or order.get("side", "BUY"))
    price      = order.get("price") or 0
    product    = str(order.get("product", "NRML"))
    order_type = str(order.get("order_type", "LIMIT"))
    variety    = str(order.get("variety", "regular"))
    return exchange, symbol, qty, side, price, product, order_type, variety


async def run_preflight(
    account: str,
    order: dict,
    paired_orders: list[dict] | None = None,
) -> dict:
    """
    Pre-validate an order before any broker placement.

    Runs four checks in order:
      1. ACCOUNT_UNKNOWN  — account not in Connections map.
      2. SEGMENT_INACTIVE — exchange not in kite.profile()['exchanges'].
      3. QTY_FREEZE       — quantity exceeds the instrument's freeze_qty
                           from the Kite instruments dump.
      4. MARGIN_SHORTFALL — kite.basket_order_margins reports required > available.

    Returns a dict:
      {
        "ok": bool,
        "blocked": [{"code", "reason", "fix", "data"}, ...],
        "diagnostics": {
          "basket_margin_used": float | None,
          "available_margin":   float | None,
          "margin_shortfall":   float | None,
        }
      }

    Never raises — any broker call failure surfaced as a blocker or
    skipped gracefully.
    """
    import asyncio
    import math

    blocked: list[dict] = []
    diagnostics: dict = {
        "basket_margin_used": None,
        "available_margin":   None,
        "margin_shortfall":   None,
    }

    # ── 0. QTY↔LOT SAFETY GUARDS (G1 multiple + G2 5-lot cap) ────────────
    # Operator 2026-07-01: "the code by mistake ordered 100 lots instead
    # of 1 lot ... happened multiple times." /ticket + /basket enforce
    # the same guards; agent-driven place_order / close_position paths
    # ALSO run this preflight, so the guards fire there too. Only F&O.
    # G2 is BYPASSED when order["intent"] == "close" — closing an
    # existing large position with a matching-size opposing order is
    # legitimate. G1 still applies (qty must be a valid multiple).
    _exch, _sym, _intent, _qty_check = _parse_lot_check_fields(order)
    _is_close = (_intent == "close")
    _lot_blockers, _hard_stop = await _preflight_validate_lots(
        _exch, _sym, _qty_check, _is_close
    )
    blocked.extend(_lot_blockers)
    # Early-return for hard blockers that make further broker checks meaningless:
    # LOT_MULTIPLE (qty is not a valid multiple — freeze_qty irrelevant) and
    # LOT_SIZE_UNKNOWN (MCX qty unknown — can't validate anything).
    # FAT_FINGER_5_LOT_CAP does NOT short-circuit — broker instruments may
    # also report QTY_FREEZE which must be surfaced alongside the lot cap.
    if _hard_stop:
        _hard_blockers = [b for b in blocked
                          if b["code"] in ("LOT_MULTIPLE", "LOT_SIZE_UNKNOWN")]
        return {"ok": False, "blocked": _hard_blockers, "diagnostics": diagnostics}

    # ── 1. ACCOUNT_UNKNOWN ────────────────────────────────────────────────
    _acct_blocker = _preflight_validate_account(account)
    if _acct_blocker is not None:
        blocked.append(_acct_blocker)
        return {"ok": False, "blocked": blocked, "diagnostics": diagnostics}

    # Resolve via the Broker registry — every method below is on the
    # Broker ABC (profile / instruments / basket_order_margins / margins),
    # so this function is broker-agnostic. When a Groww or Dhan account
    # lands, no further change here is needed.
    from backend.brokers.registry import get_broker
    broker = get_broker(account)
    loop = asyncio.get_running_loop()

    exchange, symbol, qty, side, price, product, order_type, variety = (
        _parse_broker_call_fields(order)
    )

    # ── Stage 1: build basket orders (primary leg + paired wings) ───────────
    # Done before the broker-call fan-out so basket_orders is ready when
    # the parallel gather fires. get_lot_size + normalise_qty are
    # cache-hits; no broker network.
    basket_orders = await _preflight_build_basket_orders(
        broker, exchange, symbol, side, qty,
        order_type, product, variety, price, paired_orders,
    )

    # ── Stage 2: fan out 4 independent broker calls in parallel ──────────
    # All four are orthogonal — no data dependency between them. Pre-fix
    # this section ran sequentially via four `await run_in_executor`
    # calls, costing ~800-1200ms on Kite (each round-trip ~200-300ms).
    # Now they're gathered; total time = max(individual call), typically
    # ~300ms. Operator's reported "order placement deteriorated" pain
    # tracks back to this section accumulating across recent slices.
    segment = _EXCHANGE_SEGMENT.get(exchange, "equity")

    profile_res, instruments_res, bm_res, margins_res = await asyncio.gather(
        _preflight_fetch_profile(broker, loop, account),
        _preflight_fetch_instruments(broker, exchange, qty, account),
        _preflight_fetch_basket_margin(broker, loop, basket_orders),
        _preflight_fetch_account_margins(broker, loop, segment),
    )

    # ── Apply segment-inactive gate from profile result ──────────────────
    _seg_blocker = _preflight_check_segment(profile_res, exchange)
    if _seg_blocker is not None:
        blocked.append(_seg_blocker)

    # ── Apply qty-freeze gate from instruments result ────────────────────
    _freeze_blocker = _preflight_check_qty_freeze(instruments_res, symbol, qty)
    if _freeze_blocker is not None:
        blocked.append(_freeze_blocker)

    # ── Margin-shortfall gate (basket_order_margins + account margins) ───
    # Re-fetch lot_size here (O(1) cached lookup, no network — same
    # function `_preflight_build_basket_orders` already called above)
    # so the fix_qty suggestion can round to a whole-lot multiple. `required`
    # is a rupee total, not a per-lot figure.
    from backend.brokers.adapters.kite import get_lot_size as _pf_margin_lot_fn
    _margin_lot_size = await _pf_margin_lot_fn(exchange, symbol)
    _margin_ctx = _preflight_margin_policy(broker, account, side)
    # TEMPORARY diagnostic for the item 4/5 live-verification gate — runs
    # in THIS process (backend/api/algo/, never the conn_service), so it
    # fires on dev even before a conn-service restart. In conn-
    # service/cutover mode (prod + dev share one conn_service instance
    # seeded from /opt/ramboq — see deploy.sh's CONN_TOUCHED comment:
    # "Dev pushes NEVER restart ramboq_conn ... conn-related code changes
    # only take effect after merging to main"), `bm_res` arrives over the
    # UDS RPC from whatever adapter code conn_service is CURRENTLY
    # running — which may be older than this dev deploy. The adapter's
    # own `"raw": resp` key was already present before this fix (both old
    # and new code populate it), so this line still captures the real
    # broker response shape even when conn_service hasn't been restarted
    # with the new adapter code — unlike the adapter-internal
    # [MARGIN-RAW-DHAN]/[MARGIN-RAW-GROWW] lines, which DO require a
    # conn_service restart (prod-only) to fire at all.
    if _margin_ctx["is_dhan_groww"]:
        logger.info(
            f"[MARGIN-RAW-API] {_margin_ctx['broker_id']} {account}/{symbol} "
            f"bm_res={bm_res!r}"
        )
    _margin_blockers, _margin_diag = await _preflight_check_margin(
        bm_res, margins_res, segment, account, symbol, qty, loop,
        exchange=exchange, lot_size=_margin_lot_size, margin_ctx=_margin_ctx,
    )
    blocked.extend(_margin_blockers)
    diagnostics.update(_margin_diag)

    return {
        "ok":          len(blocked) == 0,
        "blocked":     blocked,
        "diagnostics": diagnostics,
    }


async def diagnose_live_failure(broker, order: dict, kite_error: str) -> str:
    """
    When a place_order call raises, re-run basket_order_margins to
    distinguish the likely root cause:

      - basket_margin succeeds  →  margin OK; the failure was likely a
                                   segment-permission issue
      - basket_margin fails with margin/fund/shortfall keywords → margin
      - basket_margin fails with the same generic error → unclear

    `broker` is a `Broker` adapter from the registry. For backwards
    compatibility we still accept a raw SDK handle and reach for its
    `basket_order_margins` method — callers should pass the adapter.
    """
    if kite_error:
        logger.debug("kite_error in diagnose_live_failure: %s", kite_error)
    import asyncio
    from backend.brokers.adapters.kite import get_lot_size
    # Accept either a Broker adapter or a legacy SDK handle.
    basket_margin_fn = (
        broker.basket_order_margins
        if hasattr(broker, "basket_order_margins")
        else getattr(broker, "kite", broker).basket_order_margins
    )
    normalise = (
        broker.normalise_qty
        if hasattr(broker, "normalise_qty")
        else (lambda _exch, _qty, _ls: _qty)
    )
    _exch   = order.get("exchange", "NFO")
    _sym    = order.get("symbol") or order.get("tradingsymbol") or ""
    _raw_q  = int(order.get("qty") or order.get("quantity") or 0)
    _ls     = await get_lot_size(_exch, _sym)
    _bq     = normalise(_exch, _raw_q, _ls)
    basket_order = {
        "exchange":         _exch,
        "tradingsymbol":    _sym,
        "transaction_type": order.get("side") or order.get("transaction_type"),
        "quantity":         _bq,
        "order_type":       order.get("order_type", "LIMIT"),
        "product":          order.get("product", "NRML"),
        "price":            order.get("price") or 0,
        "variety":          order.get("variety", "regular"),
    }
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, basket_margin_fn, [basket_order])
        return ("margin OK via basket_margin — likely segment permission "
                "(check Account → Segments + API key exchange scope at "
                "the broker's developer console)")
    except Exception as bm_e:
        bm_msg = str(bm_e)
        low = bm_msg.lower()
        if any(k in low for k in ("margin", "fund", "shortfall", "balance")):
            return f"margin shortfall (basket_margin: {bm_msg[:160]})"
        return f"basket_margin also failed ({bm_msg[:160]}); cause unclear"
