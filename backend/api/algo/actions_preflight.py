"""
Preflight validation helpers for agent actions.

Extracted from actions.py — all `_preflight_*` helpers, `run_preflight`,
`diagnose_live_failure`, and the scope/margin helpers they depend on.
"""

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


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
    Ask Kite to dry-run the order via `basket_margin`. Returns
    (ok, detail). On `ok=False` the detail is Kite's error message —
    mirror of what `place_order` would have rejected with.
    """
    try:
        from backend.brokers.adapters.kite import to_kite_qty, get_lot_size
        exchange  = order.get("exchange", "NFO")
        symbol    = order.get("symbol") or ""
        raw_qty   = int(order.get("qty") or 0)
        lot_size  = await get_lot_size(exchange, symbol)
        kite_qty  = to_kite_qty(exchange, raw_qty, lot_size)
        basket_order = {
            "exchange":         exchange,
            "tradingsymbol":    symbol,
            "transaction_type": order.get("side"),
            "quantity":         kite_qty,
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
        elif not is_close:
            _pf_lots = qty // _pf_lot
            # MCX/NCO: the route-level MCX size guard (20-lot cap, 422) is
            # the authoritative check. Skip the 5-lot FAT_FINGER cap here
            # to avoid a 422 preflight-block that shadows the MCX route guard.
            _is_mcx_exch = exchange in ("MCX", "NCO")
            if _pf_lots > 5 and not _is_mcx_exch:
                blockers.append({
                    "code": "FAT_FINGER_5_LOT_CAP",
                    "reason": (
                        f"{_pf_lots} lots exceeds the 5-lot safety cap "
                        f"(qty={qty}, lot_size={_pf_lot})"
                    ),
                    "fix": (
                        "split into ≤5-lot orders or contact ops to raise the cap"
                    ),
                    "data": {"qty": qty, "lot_size": _pf_lot,
                             "lots": _pf_lots, "cap": 5},
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
    # capped spread margin instead of the naked SPAN. Operator sees the
    # actual margin they'll be charged, not a scarier naked-short number.
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


def _preflight_leg_required(entry: dict) -> float:
    """
    Extract the required margin for a single basket-margin leg entry.

    Prefers `final.total` (net spread margin) over `initial.total`
    (naked margin), and falls back to the top-level `required` field.
    Returns 0.0 on any parsing failure.
    """
    if not isinstance(entry, dict):
        return 0.0
    for branch in ("final", "initial"):
        slot = (entry.get(branch) or {}).get("total")
        if slot is not None:
            try:
                return float(slot)
            except (TypeError, ValueError):
                pass
    try:
        return float(entry.get("required") or 0)
    except (TypeError, ValueError):
        return 0.0


def _preflight_parse_basket_margin(bm_result: object) -> float:
    """
    Parse the raw return value of broker.basket_order_margins into a
    single ``required`` float.

    Kite returns a list (one dict per leg) or occasionally a bare dict.
    Multi-leg: sum per-leg final/initial totals so the spread offset is
    captured. Single-leg list: unwrap and read the single entry.
    Dict fallback: read as a single entry.
    """
    if isinstance(bm_result, list) and bm_result:
        if len(bm_result) > 1:
            return float(sum(_preflight_leg_required(r) for r in bm_result))
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
) -> str:
    """
    Compute the ``fix_qty`` suffix for MARGIN_SHORTFALL blockers.

    Returns a string like " or reduce qty to 25" when per-unit margin
    can be inferred, or an empty string when the calculation is not
    possible (required=0, available=0, qty=0).
    """
    if not (required > 0 and available is not None and available > 0):
        return ""
    per_unit = required / qty if qty > 0 else 0
    if per_unit <= 0:
        return ""
    try:
        max_qty_fit = max(0, int(available / per_unit))
        return f" or reduce qty to {max_qty_fit:,}" if max_qty_fit > 0 else ""
    except Exception:
        return ""


def _preflight_handle_positive_margin(
    required: float,
    margins_res: tuple,
    segment: str,
    account: str,
    qty: int,
) -> tuple[list[dict], dict]:
    """
    Handle the ``required >= 0`` branch of the margin gate.

    Parses available margin, emits INSUFFICIENT_FUNDS when available=0,
    computes shortfall, and builds MARGIN_SHORTFALL blockers.

    Returns ``(new_blockers, diag_partial)``.
    """
    import math
    new_blockers: list[dict] = []
    diag: dict = {}
    shortfall: float = 0.0

    m_enabled, available = _preflight_resolve_available_margin(margins_res, segment, account)

    diag["basket_margin_used"] = required
    diag["available_margin"]   = available
    diag["margin_shortfall"]   = None

    # ── Available-is-zero gate ────────────────────────────────────
    # When available=0 AND required>0 AND segment is enabled, the
    # order has zero chance of going through — block immediately.
    if m_enabled and available == 0.0 and required > 0:
        logger.warning(
            f"[PREFLIGHT] available_margin=0 with required={required:.2f} "
            f"for {account} — blocking as INSUFFICIENT_FUNDS."
        )
        new_blockers.append({
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
        })
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
        fix_qty = _preflight_margin_shortfall_fix_qty(required, available, qty)
        new_blockers.append({
            "code":   "MARGIN_SHORTFALL",
            "reason": (f"Required margin ₹{required:,.0f} exceeds available "
                       f"₹{available:,.0f} (shortfall ₹{shortfall:,.0f})"),
            "fix":    (f"Add ₹{shortfall:,.0f} more margin to the account" + fix_qty),
            "data":   {
                "required":   required,
                "available":  available,
                "shortfall":  shortfall,
            },
        })

    return new_blockers, diag


async def _preflight_check_margin(
    bm_res: object,
    margins_res: tuple,
    segment: str,
    account: str,
    symbol: str,
    qty: int,
    loop: object,
) -> tuple[list[dict], dict]:
    """
    Evaluate basket-order margin and account margin to emit blockers.

    Returns ``(new_blockers, diag_updates)`` where *diag_updates* is a
    partial diagnostics dict that the caller should merge into its own.
    Never raises — broker-call failures are handled internally.
    """
    new_blockers: list[dict] = []
    diag: dict = {}

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
        if required < 0:
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
            _pm_blockers, _pm_diag = _preflight_handle_positive_margin(
                required, margins_res, segment, account, qty
            )
            new_blockers.extend(_pm_blockers)
            diag.update(_pm_diag)
    except Exception as e:
        bm_msg = str(e).lower()
        # basket_margin raised — interpret the error signal.
        if any(k in bm_msg for k in ("margin", "fund", "shortfall", "balance")):
            new_blockers.append({
                "code":   "MARGIN_SHORTFALL",
                "reason": f"Margin check failed: {str(e)[:160]}",
                "fix":    "Add margin to the account or reduce quantity",
                "data":   {"broker_error": str(e)[:240]},
            })
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
            return m.get(segment, {}), None
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


async def _preflight_fetch_instruments(broker, loop, exchange: str, qty: int, account: str):
    if exchange not in ("NFO", "BFO", "MCX", "CDS") or qty <= 0:
        return None
    try:
        return await loop.run_in_executor(None, broker.instruments, exchange)
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
        _preflight_fetch_instruments(broker, loop, exchange, qty, account),
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
    _margin_blockers, _margin_diag = await _preflight_check_margin(
        bm_res, margins_res, segment, account, symbol, qty, loop
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
