import pandas as pd

from backend.shared.helpers.connections import Connections
from backend.shared.helpers.decorators import for_all_accounts
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


@for_all_accounts
def fetch_holdings(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker holdings fetch. Uses the Broker ABC abstraction
    (broker.holdings()) when available so Dhan / Groww accounts route
    through their own adapters; falls back to the legacy `kite=`
    handle for backwards compatibility with the original Kite-only
    path. Every adapter normalises its response to the Kite-shape
    column set used by downstream UI (tradingsymbol, average_price,
    opening_quantity, pnl, day_change, close_price, etc.)."""
    df_holdings = pd.DataFrame()
    try:
        rows = None
        if broker is not None:
            rows = broker.holdings()
        elif kite is not None:
            rows = kite.holdings()
        if rows is None:
            df_holdings.attrs['fetch_failed'] = True
            return df_holdings
        df_holdings = pd.DataFrame(rows)

        if not df_holdings.empty:
            df_holdings["account"] = account
            df_holdings["type"] = "H"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch holdings: {e}")
        df_holdings.attrs['fetch_failed'] = True

    # Calculated columns — guard against an empty / fetch-failed frame
    # (broker 502 / 503 outages leave df_holdings empty and skipping the
    # math here is the difference between an empty response and a 500
    # KeyError on 'average_price'). Also guard each column reference
    # individually: a normaliser that omits one of the Kite-shape
    # columns (e.g. Groww doesn't always carry close_price) won't break
    # the others.
    if df_holdings.empty:
        return df_holdings

    if {"average_price", "opening_quantity"}.issubset(df_holdings.columns):
        df_holdings["inv_val"] = df_holdings["average_price"] * df_holdings["opening_quantity"]
    # Reconciled P&L posture (mirrors fetch_positions):
    #   - Broker pnl / day_change_val are the source of truth when the
    #     adapter shipped them (Kite always does; Dhan + Groww populate
    #     via their normalisers).
    #   - Our (LTP - avg) × qty / (LTP - close) × qty formulas are the
    #     synthesise-from-scratch fallback for adapters that don't
    #     populate the column.
    if {"last_price", "average_price", "opening_quantity"}.issubset(df_holdings.columns):
        _ltp_h = pd.to_numeric(df_holdings["last_price"], errors="coerce").fillna(0)
        _avg_h = pd.to_numeric(df_holdings["average_price"], errors="coerce").fillna(0)
        _qty_h = pd.to_numeric(df_holdings["opening_quantity"], errors="coerce").fillna(0)
        _pnl_calc = (_ltp_h - _avg_h) * _qty_h
        if "pnl" in df_holdings.columns:
            _broker_pnl_h = pd.to_numeric(df_holdings["pnl"], errors="coerce")
            df_holdings["pnl"] = _broker_pnl_h.where(_broker_pnl_h.notna(), _pnl_calc)
        else:
            _valid = (_ltp_h > 0) & (_avg_h > 0)
            df_holdings["pnl"] = _pnl_calc.where(_valid, 0.0)
    if "pnl" in df_holdings.columns and "inv_val" in df_holdings.columns:
        df_holdings["cur_val"] = df_holdings["inv_val"] + df_holdings["pnl"]
        df_holdings["pnl_percentage"] = df_holdings["pnl"] / df_holdings["inv_val"] * 100
    if {"close_price", "average_price"}.issubset(df_holdings.columns):
        df_holdings["price_change"] = df_holdings["close_price"] - df_holdings["average_price"]
    if {"last_price", "close_price", "opening_quantity"}.issubset(df_holdings.columns):
        _ltp_h2 = pd.to_numeric(df_holdings["last_price"], errors="coerce").fillna(0)
        _cls_h  = pd.to_numeric(df_holdings["close_price"], errors="coerce").fillna(0)
        _qty_h2 = pd.to_numeric(df_holdings["opening_quantity"], errors="coerce").fillna(0)
        # Day P&L = pnl − yesterday's overnight P&L = broker.pnl − (close − cost) × opening_qty
        #
        # This handles all three cases correctly:
        #   • Still-held holding: pnl = (LTP − cost) × opening_qty so the
        #     expression collapses to (LTP − close) × opening_qty (drifts
        #     with LTP — correct).
        #   • Partially sold today: pnl = (sale − cost) × sold + (LTP − cost) × held
        #     so day_pnl = (sale − close) × sold + (LTP − close) × held
        #     (sold portion frozen, held drifts — correct).
        #   • Fully sold today: pnl = (sale − cost) × opening_qty so
        #     day_pnl = (sale − close) × opening_qty (frozen — correct).
        #
        # Operator: "IFCI went down by more than 8% which should reduce
        # h delta." For IFCI the operator sold all 10 000 today; the
        # previous (LTP − close) × opening formula kept drifting with
        # LTP after the sale, but the operator's actual day P&L was
        # locked in at the sale price. Switching to `pnl −
        # overnight_pnl` freezes it correctly.
        if "average_price" in df_holdings.columns and "pnl" in df_holdings.columns:
            _avg_h2 = pd.to_numeric(df_holdings["average_price"], errors="coerce").fillna(0)
            _pnl_h2 = pd.to_numeric(df_holdings["pnl"], errors="coerce")
            _overnight_pnl = (_cls_h - _avg_h2) * _qty_h2
            _dcv_calc = _pnl_h2 - _overnight_pnl
            # Fall back to (LTP - close) × opening when broker pnl
            # isn't usable (NaN / cold open) so the row still renders.
            _ltp_fallback = (_ltp_h2 - _cls_h) * _qty_h2
            _dcv_calc = _dcv_calc.where(_pnl_h2.notna(), _ltp_fallback)
        else:
            # Older adapter shape — no avg_price/pnl column. Use the
            # naive overnight formula (operator with no intraday sells
            # gets correct numbers; sold-today rows drift, same as
            # before).
            _dcv_calc = (_ltp_h2 - _cls_h) * _qty_h2
        if "day_change_val" in df_holdings.columns:
            _broker_dcv_h = pd.to_numeric(df_holdings["day_change_val"], errors="coerce")
            df_holdings["day_change_val"] = _broker_dcv_h.where(
                _broker_dcv_h.notna(), _dcv_calc
            )
        else:
            _valid_d = (_ltp_h2 > 0) & (_cls_h > 0)
            df_holdings["day_change_val"] = _dcv_calc.where(_valid_d, 0.0)
    elif {"day_change", "opening_quantity"}.issubset(df_holdings.columns):
        df_holdings["day_change_val"] = df_holdings["day_change"] * df_holdings["opening_quantity"]
    if "authorised_date" in df_holdings.columns:
        df_holdings["authorised_date"] = pd.to_datetime(
            df_holdings["authorised_date"], errors="coerce"
        ).dt.strftime("%d%b%y")

    return df_holdings


@for_all_accounts
def fetch_positions(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker positions fetch. Same broker-vs-kite resolution
    pattern as fetch_holdings; non-Kite adapters return Kite-shape
    rows via their respective normalisers."""
    df_positions = pd.DataFrame()
    try:
        net_rows = None
        if broker is not None:
            resp = broker.positions()
            # broker.positions() returns a Kite-shape dict {net: [...], day: [...]}
            # for every adapter (Kite + Dhan + Groww normalise to this).
            if isinstance(resp, dict):
                net_rows = resp.get("net", [])
            elif isinstance(resp, list):
                net_rows = resp
        elif kite is not None:
            net_rows = kite.positions()["net"]
        if net_rows is None:
            df_positions.attrs['fetch_failed'] = True
            return df_positions
        df_positions = pd.DataFrame(net_rows)
        if not df_positions.empty and "multiplier" in df_positions.columns:
            # MCX commodities: Kite ships `quantity` in LOTS but
            # `last_price` / `close_price` are per CONTRACT (gram for
            # GOLDM, barrel for CRUDEOIL, etc.) so we multiply qty by
            # `multiplier` (lot_size) to put it in contract units —
            # downstream consumers can do `qty × price = ₹` without
            # caring about the per-instrument lot size.
            #
            # CRITICAL: do the same for overnight_quantity +
            # day_buy_quantity + day_sell_quantity. They land in the
            # decomposed day_pnl formula alongside last_price/close_price
            # so they MUST be in the same unit. Pre-fix, MCX intraday
            # fields stayed in lots and `sq × LTP` was off by `multiplier`
            # — producing the GOLDM146000CE ₹61 537 phantom that pushed
            # the strip's P∆ to ₹1.11 L on a real ~₹50 k day.
            #
            # Second pass (Jun 26 2026): day_buy_value + day_sell_value
            # MUST also scale by multiplier. Kite ships these as
            # `lots × per_unit_price` (NOT total ₹) — same lot-unit
            # convention as quantity. The day_pnl formula
            #     (_bq × LTP − _bv) + (_sv − _sq × LTP)
            # is dimensionally wrong if _bq/_sq are in contracts
            # (post-multiply) but _bv/_sv are still in lot-units. For
            # GOLDM with multiplier=100, a 1-lot buy at 9200 with LTP
            # 9250 produces _bq×LTP − _bv = 100×9250 − 9200 = 915,800
            # phantom day_pnl vs the correct 100×(9250−9200) = 5,000.
            # That's the "GOLDM calculation not correct" operator
            # report. Multiplying _bv and _sv by `_mult` here brings
            # them onto the same contract-units basis as _bq/_sq.
            _mult = df_positions['multiplier']
            df_positions['quantity'] = df_positions['quantity'] * _mult
            for _c in ('overnight_quantity', 'day_buy_quantity', 'day_sell_quantity',
                       'day_buy_value', 'day_sell_value'):
                if _c in df_positions.columns:
                    df_positions[_c] = df_positions[_c] * _mult

        if not df_positions.empty:
            df_positions["account"] = account
            df_positions["type"] = "P"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch positions: {e}")
        df_positions.attrs['fetch_failed'] = True
        return df_positions

    if df_positions.empty:
        return df_positions

    # ── P&L + day change reconciliation ────────────────────────────────
    # Broker is the source of truth. The earlier revision overrode
    # `pnl` and `day_change_val` with `(LTP - avg) × qty` and
    # `(LTP - close) × qty` for every row where LTP + avg/close were
    # positive — that replaced Kite's broker.pnl (which includes
    # realised cash flow from intraday closeouts) with a simple
    # unrealised-only formula. End-to-end effect: a position with
    # intraday partial closeouts showed different numbers in the
    # strip / Legs / Payoff than the operator's broker app, because
    # the realised portion got dropped.
    #
    # Reconciled posture:
    #   - When the adapter shipped a numeric pnl / day_change_val
    #     value, TRUST IT. Kite's pnl includes realised; Dhan's
    #     adapter computes (LTP - avg) × qty natively (because
    #     Dhan's unrealisedProfit field is unreliable); Groww
    #     forwards its own pnl. All three are the broker-canonical
    #     values for that adapter.
    #   - Fall back to our simple formula ONLY when the adapter
    #     left the field null / missing — defensive against future
    #     adapters that don't populate the column.
    #
    # day_change column (per-share delta — `LTP - close`) is
    # cosmetic, kept verbatim for downstream readers that still
    # reference it.
    df_positions['day_change'] = df_positions['last_price'] - df_positions['close_price']
    _ltp = pd.to_numeric(df_positions['last_price'],    errors='coerce').fillna(0)
    _avg = pd.to_numeric(df_positions['average_price'], errors='coerce').fillna(0)
    _cls = pd.to_numeric(df_positions['close_price'],   errors='coerce').fillna(0)
    _qty = pd.to_numeric(df_positions['quantity'],      errors='coerce').fillna(0)
    _pnl_calc = (_ltp - _avg) * _qty
    # Day P&L — the CORRECT formula that handles intraday-added
    # positions. The naive `(LTP - close_price) × qty` treats every
    # share as if held since yesterday's close, which over/understates
    # by the gap between prev_close and today's entry price for any
    # position opened TODAY.
    #
    # Operator: "delta p is not correct for newly added position as
    # it might be calculating incorrect price for calculation."
    # Verified against SUZLON 26JUN60CE: overnight=-9025, today sold
    # 9025 more at 1.21, LTP=1.03, prev_close=1.5. Naive formula
    # gave ₹8484, correct = ₹4242 (overnight) + ₹1624 (today's trade)
    # = ₹5866. Over by ₹2617 — phantom gain between prev_close 1.5
    # and today's entry 1.21 on the new lot.
    #
    # Correct decomposition:
    #   day_pnl = overnight_qty × (LTP − prev_close)        # carried
    #           + day_buy_qty   × LTP − day_buy_value       # bought today
    #           + day_sell_value − day_sell_qty × LTP       # sold today
    #
    # Falls back to the naive formula only when the intraday fields
    # are missing entirely (adapter doesn't ship them — Dhan v2
    # behaved this way until recently).
    _intraday_fields = {'overnight_quantity', 'day_buy_quantity',
                        'day_sell_quantity', 'day_buy_value', 'day_sell_value'}
    if _intraday_fields.issubset(df_positions.columns):
        _oq = pd.to_numeric(df_positions['overnight_quantity'], errors='coerce').fillna(0)
        _bq = pd.to_numeric(df_positions['day_buy_quantity'],   errors='coerce').fillna(0)
        _sq = pd.to_numeric(df_positions['day_sell_quantity'],  errors='coerce').fillna(0)
        _bv = pd.to_numeric(df_positions['day_buy_value'],      errors='coerce').fillna(0)
        _sv = pd.to_numeric(df_positions['day_sell_value'],     errors='coerce').fillna(0)
        _dcv_calc = (
            _oq * (_ltp - _cls)
            + (_bq * _ltp - _bv)
            + (_sv - _sq * _ltp)
        )
    else:
        # Pre-intraday-fields fallback. Operator rule: newly-added
        # positions read from purchase price; old positions read from
        # prev_close. When close_price is missing/zero (typical for
        # fresh same-day buys before EOD reconciliation), fall back to
        # (LTP - avg_price) × qty so the Day P&L still reflects the
        # operator's actual movement since entry.
        _dcv_calc = (_ltp - _cls) * _qty
        _missing_close = (_cls <= 0) & (_avg > 0) & (_ltp > 0)
        if bool(_missing_close.any()):
            _dcv_calc = _dcv_calc.mask(_missing_close, (_ltp - _avg) * _qty)
    # Trust broker pnl when present (not null / not NaN). Fall back
    # to (LTP - avg) × qty only on missing values.
    if 'pnl' in df_positions.columns:
        _broker_pnl = pd.to_numeric(df_positions['pnl'], errors='coerce')
        df_positions['pnl'] = _broker_pnl.where(_broker_pnl.notna(), _pnl_calc)
    else:
        # Adapter didn't ship a pnl column at all — synthesize from
        # the simple formula, guarded by ltp+avg validity (avoid
        # phantom values during pre-open warm-up).
        _pnl_valid = (_ltp > 0) & (_avg > 0)
        df_positions['pnl'] = _pnl_calc.where(_pnl_valid, 0.0)
    # day_change_val priority:
    #   1. Decomposed intraday formula `_dcv_calc` when the row carries
    #      the full intraday-field set. Mathematically exact AND
    #      naturally FREEZES closed positions — once current_qty hits
    #      0 the LTP coefficient drops to 0 and day_pnl becomes
    #      `−overnight × close + day_sell_value − day_buy_value`, all
    #      static numbers that don't drift between polls. Operator:
    #      "closed position should freeze delta p and p — there will
    #      not be any more change in day p&l and day % in legs."
    #
    #      Kite + Dhan + Groww all normalise to this shape today so
    #      this branch covers every broker. Kite's `m2m` was the prior
    #      winner but its closed-position semantics aren't documented
    #      and observed to drift across polls; the decomposed formula
    #      is trust-the-math and stays stable.
    #   2. broker.m2m (only when decomposed isn't available — Kite
    #      without intraday fields is the theoretical case).
    #   3. Adapter-shipped day_change_val (fallback for adapters that
    #      ship it but DON'T expose the intraday-field set).
    #   4. Naive (LTP - close) × qty (final fallback).
    if _intraday_fields.issubset(df_positions.columns):
        # Trust _dcv_calc unconditionally. Validity guard: zero the
        # row when LTP is obviously unhealthy (pre-open warm-up).
        _dcv_valid = (_ltp > 0)
        df_positions['day_change_val'] = _dcv_calc.where(_dcv_valid, 0.0)
    elif 'm2m' in df_positions.columns:
        _broker_m2m = pd.to_numeric(df_positions['m2m'], errors='coerce')
        df_positions['day_change_val'] = _broker_m2m.where(
            _broker_m2m.notna(), _dcv_calc
        )
    elif 'day_change_val' in df_positions.columns:
        _broker_dcv = pd.to_numeric(df_positions['day_change_val'], errors='coerce')
        df_positions['day_change_val'] = _broker_dcv.where(
            _broker_dcv.notna(), _dcv_calc
        )
    else:
        _dcv_valid = (_ltp > 0) & (_cls > 0)
        df_positions['day_change_val'] = _dcv_calc.where(_dcv_valid, 0.0)
    prev_val = (df_positions['close_price'] * df_positions['quantity']).abs()
    df_positions['day_change_percentage'] = (
        df_positions['day_change_val'] / prev_val.replace(0, pd.NA) * 100
    ).fillna(0)

    # P&L % — pnl over the cost basis (avg × |qty|). Holdings get this
    # natively from Kite as `inv_val`; for positions we compute it the
    # same way to keep the column meaningful across both grids.
    cost_basis = (df_positions['average_price'] * df_positions['quantity']).abs()
    df_positions['pnl_percentage'] = (
        df_positions['pnl'] / cost_basis.replace(0, pd.NA) * 100
    ).fillna(0)

    return df_positions


def backfill_market_data(df) -> int:
    """Generalised market-data backfill. Operator: "if the fields any
    time available from dhan or groww, it can be backfilled from kite
    using symbol. only cost price is required from the broker."

    Industry pattern (IBKR / Bloomberg PRTU / Sensibull / Streak):
    each row of a multi-broker book is split into two slices —
      account-specific facts → trust the source broker
        (avg_price, quantity, opening_quantity, realised, account)
      market-data facts → one canonical source for the whole book
        (close_price, last_price, day_change_*, instrument identity)

    Kite's `quote()` is the most complete market-data feed across
    Dhan / Groww / Kite, so we route every market-data lookup
    through `PriceBroker.quote()` (which prefers Kite, then falls
    through to Dhan, then Groww via the registry's preference
    order). Source brokers that already populate close_price /
    last_price keep their values — backfill only kicks in on
    zero / missing, never overwriting a non-zero broker value.

    Called by the /api/positions and /api/holdings endpoints AFTER
    `pd.concat(broker_apis.fetch_*())` so the PriceBroker.quote
    call is ONE batched round-trip across every missing-field row
    from every broker — not N per N accounts (the prior shape
    called the lookup inside the per-account `@for_all_accounts`
    body and burned N quote() calls per poll).

    No-op when both close_price and last_price are already
    populated on every row (Kite always returns them; Dhan + Groww
    sometimes don't). Exception-safe: a PriceBroker outage leaves
    rows untouched and downstream P&L fallback behaviour matches
    the pre-patch state.

    Returns the count of patched rows (informational for callers'
    debug logs).
    """
    if df is None or df.empty:
        return 0
    if 'close_price' not in df.columns and 'last_price' not in df.columns:
        return 0
    # A row needs backfill if EITHER close_price or last_price is
    # zero / missing. Unions across both criteria so the single
    # batched quote call covers everything.
    _cls_missing = (pd.to_numeric(df['close_price'], errors='coerce').fillna(0).le(0)
                    if 'close_price' in df.columns
                    else pd.Series(False, index=df.index))
    _ltp_missing = (pd.to_numeric(df['last_price'], errors='coerce').fillna(0).le(0)
                    if 'last_price' in df.columns
                    else pd.Series(False, index=df.index))
    _missing = _cls_missing | _ltp_missing
    if not _missing.any():
        return 0

    # Build unique quote keys across every missing-field row.
    _missing_rows = df[_missing]
    _key_per_row: list[str] = []
    _seen_keys: set[str] = set()
    _unique_keys: list[str] = []
    for _, _row in _missing_rows.iterrows():
        _exch = str(_row.get('exchange', '') or 'NFO').upper()
        _sym  = str(_row.get('tradingsymbol', '') or '').upper()
        if _sym:
            _k = f"{_exch}:{_sym}"
            _key_per_row.append(_k)
            if _k not in _seen_keys:
                _seen_keys.add(_k)
                _unique_keys.append(_k)
        else:
            _key_per_row.append('')

    if not _unique_keys:
        return 0

    try:
        from backend.shared.brokers.registry import get_price_broker
        _pb = get_price_broker()
        _q = _pb.quote(_unique_keys) or {}
    except Exception as _e:
        logger.warning(
            f"PriceBroker market-data backfill failed (1 batched call for "
            f"{len(_unique_keys)} symbols): {_e}"
        )
        return 0

    # Extract two fields per quote: close (from ohlc.close, fallback
    # to top-level close_price) and last_price (from top-level
    # last_price). Only positive values land in the lookup tables —
    # zeros are treated as "broker didn't have it either".
    _close_lookup: dict[str, float] = {}
    _ltp_lookup: dict[str, float] = {}
    for _k, _v in _q.items():
        if not isinstance(_v, dict):
            continue
        _ohlc = _v.get('ohlc') if isinstance(_v.get('ohlc'), dict) else {}
        _cls_val = _ohlc.get('close') if _ohlc else None
        if _cls_val is None:
            _cls_val = _v.get('close_price')
        try:
            _f_cls = float(_cls_val) if _cls_val is not None else 0.0
        except (TypeError, ValueError):
            _f_cls = 0.0
        if _f_cls > 0:
            _close_lookup[_k] = _f_cls

        _ltp_val = _v.get('last_price')
        try:
            _f_ltp = float(_ltp_val) if _ltp_val is not None else 0.0
        except (TypeError, ValueError):
            _f_ltp = 0.0
        if _f_ltp > 0:
            _ltp_lookup[_k] = _f_ltp

    # Patch close_price + last_price in place, but ONLY on rows
    # where the source broker came back with 0. Never overwrite a
    # non-zero broker value — Dhan/Groww LTP may be a fresher tick
    # than the snapshot-time Kite quote.
    def _missing_val(value) -> bool:
        try:
            v = float(value)
        except (TypeError, ValueError):
            return True
        if v != v:  # NaN
            return True
        return v <= 0

    _has_close = 'close_price' in df.columns
    _has_ltp   = 'last_price'  in df.columns
    _row_indices = df.index[_missing].tolist()
    _patched_indices: set = set()
    _unresolved: list[str] = []
    for _idx, _k in zip(_row_indices, _key_per_row):
        if not _k:
            continue
        _touched = False
        if _has_close and _missing_val(df.at[_idx, 'close_price']):
            _cls_p = _close_lookup.get(_k)
            if _cls_p:
                df.at[_idx, 'close_price'] = _cls_p
                _touched = True
        if _has_ltp and _missing_val(df.at[_idx, 'last_price']):
            _ltp_p = _ltp_lookup.get(_k)
            if _ltp_p:
                df.at[_idx, 'last_price'] = _ltp_p
                _touched = True
        if _touched:
            _patched_indices.add(_idx)
        elif _k not in _close_lookup and _k not in _ltp_lookup:
            _unresolved.append(_k)

    # Diagnostic: log symbols where PriceBroker.quote() returned
    # neither close nor LTP. These rows stay at 0 → Day P&L = 0
    # downstream — the canonical "Dhan Day P&L shows zero while
    # Kite shows non-zero" symptom. When the operator reports it,
    # this log line names the exact symbols that failed so the
    # next step is deterministic (usually: symbol not in Kite
    # instruments cache, or broker quote returned no ohlc).
    if _unresolved:
        logger.warning(
            f"market-data backfill: {len(_unresolved)}/{len(_unique_keys)} "
            f"symbols unresolved by PriceBroker; rows stay at close=0 / "
            f"ltp=0 → Day P&L=0. Unresolved: {_unresolved[:10]}"
            + (f" (+{len(_unresolved)-10} more)" if len(_unresolved) > 10 else "")
        )

    if not _patched_indices:
        return 0

    # Re-run the (LTP - close) × qty recompute on patched rows only.
    # The per-account fetch already wrote a value (0 or broker-
    # reported) the consumer treats as authoritative — overwrite it
    # now that we have real market data.
    _qty_col = 'opening_quantity' if 'opening_quantity' in df.columns else 'quantity'
    if _qty_col not in df.columns or 'last_price' not in df.columns:
        return len(_patched_indices)

    _idx_array = pd.Index(sorted(_patched_indices))
    _ltp_p = pd.to_numeric(df.loc[_idx_array, 'last_price'], errors='coerce').fillna(0)
    _cls_p = pd.to_numeric(df.loc[_idx_array, 'close_price'], errors='coerce').fillna(0)
    _qty_p = pd.to_numeric(df.loc[_idx_array, _qty_col], errors='coerce').fillna(0)
    _dcv_p = (_ltp_p - _cls_p) * _qty_p
    _valid_p = (_ltp_p > 0) & (_cls_p > 0)
    if 'day_change_val' in df.columns:
        df.loc[_idx_array, 'day_change_val'] = _dcv_p.where(
            _valid_p, df.loc[_idx_array, 'day_change_val']
        )
    else:
        df.loc[_idx_array, 'day_change_val'] = _dcv_p.where(_valid_p, 0.0)
    if 'day_change' in df.columns:
        df.loc[_idx_array, 'day_change'] = _ltp_p - _cls_p

    # day_change_percentage rides off close × qty in the consumer's
    # per-account summary. For row-level we update the column
    # directly so the API response is consistent.
    if 'day_change_percentage' in df.columns:
        _prev_val_p = (_cls_p * _qty_p).abs()
        _pct_p = (_dcv_p / _prev_val_p.replace(0, pd.NA) * 100).fillna(0)
        df.loc[_idx_array, 'day_change_percentage'] = _pct_p.where(
            _valid_p, df.loc[_idx_array, 'day_change_percentage']
        )

    # Recompute pnl on rows where LTP was patched and we have a
    # cost basis. The source broker's pnl on those rows was
    # computed against the (broken) zero LTP, so it's typically
    # wrong (= -cost × qty for a long position). Cost basis is
    # the only field we trust the source broker for here — it's
    # the account-specific fact only that broker knows.
    if 'average_price' in df.columns and 'pnl' in df.columns:
        _avg_p = pd.to_numeric(df.loc[_idx_array, 'average_price'], errors='coerce').fillna(0)
        _pnl_calc = (_ltp_p - _avg_p) * _qty_p
        # Include realised when present (positions carry it; holdings
        # typically don't because holdings are open-only).
        if 'realised' in df.columns:
            _rea_p = pd.to_numeric(df.loc[_idx_array, 'realised'], errors='coerce').fillna(0)
            _pnl_calc = _pnl_calc + _rea_p
        _valid_pnl = (_ltp_p > 0) & (_avg_p > 0)
        df.loc[_idx_array, 'pnl'] = _pnl_calc.where(
            _valid_pnl, df.loc[_idx_array, 'pnl']
        )
        # cur_val + pnl_percentage chain off pnl — keep them
        # consistent when present.
        if 'inv_val' in df.columns and 'cur_val' in df.columns:
            _inv_p = pd.to_numeric(df.loc[_idx_array, 'inv_val'], errors='coerce').fillna(0)
            df.loc[_idx_array, 'cur_val'] = _inv_p + df.loc[_idx_array, 'pnl']
            if 'pnl_percentage' in df.columns:
                _pp = (df.loc[_idx_array, 'pnl'] / _inv_p.replace(0, pd.NA) * 100).fillna(0)
                df.loc[_idx_array, 'pnl_percentage'] = _pp

    return len(_patched_indices)


# Back-compat alias — the function used to be narrower (close only).
# Old name still resolves so external scripts / future-refactor
# callers don't break in the same deploy as the rename.
backfill_close_prices = backfill_market_data


@for_all_accounts
def fetch_margins(connections=Connections, account=None, kite=None, broker=None):
    """Multi-broker margins fetch. broker.margins(segment) returns the
    same Kite-shape dict every adapter normalises to."""
    df_margins = pd.DataFrame()
    try:
        if broker is not None:
            margins_data = broker.margins(segment="equity")
        elif kite is not None:
            margins_data = kite.margins(segment="equity")
        else:
            return df_margins
        df_margins = pd.DataFrame([margins_data])

        # Flatten 'utilised' if it exists
        if "utilised" in df_margins.columns:
            utilised_df = pd.json_normalize(df_margins["utilised"])
            # Optional: prefix column names
            utilised_df = utilised_df.add_prefix("util ")
            # Drop original nested column and concat flattened
            df_margins = pd.concat([df_margins.drop(columns=["utilised"]), utilised_df], axis=1)

        # Flatten 'available' if needed
        if "available" in df_margins.columns:
            available_df = pd.json_normalize(df_margins["available"])
            available_df = available_df.add_prefix("avail ")
            df_margins = pd.concat([df_margins.drop(columns=["available"]), available_df], axis=1)

        if not df_margins.empty:
            df_margins["account"] = account
            df_margins["type"] = "C"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch margins: {e}")

    return df_margins


# Daily-TTL cache for fetch_holidays. The holiday list only changes once
# per year, but the agent engine's _build_context calls fetch_holidays on
# every run_cycle (every 5 min real-path, every 2 s in sim) — without
# this, every tick fired a blocking HTTP GET to nseindia.com.
# Format: {exchange: (cached_date, set_of_dates)}
_HOLIDAY_CACHE: dict[str, tuple] = {}


def fetch_holidays(exchange="NSE"):
    """
    Fetch trading holidays from NSE/MCX official APIs.

    NSE API returns segments: CM (equity cash), FO (F&O), CD (currency), CBM (commodity on BSE).
    MCX holidays are fetched from MCX website.
    Maps exchange param to the right segment.

    Read priority:
      1. holidays_store._MEM_CACHE (Tier 1 of the persistent store) — sync read.
         Populated on the async path by get_or_fetch_holidays().
      2. Module-level _HOLIDAY_CACHE fallback — used when the persistent store
         has not been warmed yet (first cold call from a sync context).
         Falls through to NSE API and fires a background populate as a side-effect.

    This function is sync so it can be called from non-async code (agent engine,
    background tasks). Do NOT make it async — that would require changing every
    callsite throughout the codebase.
    """
    import requests
    from datetime import datetime as dt_datetime, date as dt_date

    exch = exchange.upper().strip()

    # ── Tier 1: check holidays_store memory cache (sync read) ─────────────────
    try:
        from backend.api.persistence.holidays_store import (
            _MEM_CACHE as _hol_mem,
            _ist_year as _hol_year,
        )
        yr = _hol_year()
        hol_key = (exch, yr)
        if hol_key in _hol_mem:
            # Mirror into _HOLIDAY_CACHE so future sync callers get fast path.
            cached_set = _hol_mem[hol_key]
            today = dt_date.today()
            _HOLIDAY_CACHE[exchange] = (today, cached_set)
            return cached_set
    except Exception:
        pass  # persistent store not available — fall through

    # ── Legacy Tier 2: module-level _HOLIDAY_CACHE (daily TTL) ────────────────
    today = dt_date.today()
    cached = _HOLIDAY_CACHE.get(exchange)
    if cached and cached[0] == today:
        return cached[1]

    # ── Legacy Tier 3: NSE API fetch ──────────────────────────────────────────
    # Map Kite exchange names to NSE holiday API segment keys
    # CM=equity cash, FO=F&O, CD=currency, COM=commodity(MCX)
    _SEGMENT_MAP = {"NSE": "CM", "BSE": "CM", "NFO": "FO", "CDS": "CD", "MCX": "COM"}

    holidays: set = set()
    try:
        resp = requests.get(
            "https://www.nseindia.com/api/holiday-master?type=trading",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        segment = _SEGMENT_MAP.get(exchange, "CM")
        entries = data.get(segment, [])

        for h in entries:
            d = h.get("tradingDate", "")
            if d:
                try:
                    holidays.add(dt_datetime.strptime(d, "%d-%b-%Y").date())
                except ValueError:
                    pass
    except Exception:
        pass

    # Cache even on failure (empty set) — avoids retry-hammering nseindia
    # all day if the API is down. Next day's call retries naturally.
    _HOLIDAY_CACHE[exchange] = (today, holidays)

    # Fire-and-forget: populate the persistent store so future restarts
    # hit Tier 1 or Tier 2 instead of calling the API again.
    if holidays:
        _trigger_holidays_store_populate(exch, holidays)

    return holidays


def _trigger_holidays_store_populate(exchange: str, holidays: set) -> None:
    """Schedule a background write to holidays_store from a sync context.

    Schedules on the running event loop if one exists. Silently skips
    in test / import-only contexts where no loop is running.
    """
    try:
        import asyncio as _asyncio
        from backend.api.persistence.holidays_store import (
            _MEM_CACHE as _hol_mem,
            _ist_year as _hol_year,
            _enqueue_db as _hol_enqueue,
        )
        yr = _hol_year()
        key = (exchange, yr)
        # Populate Tier 1 synchronously — it's just a dict write.
        if key not in _hol_mem:
            _hol_mem[key] = holidays
        # Enqueue DB write — also sync (just puts to a queue).
        _hol_enqueue(exchange, yr, holidays)
    except Exception:
        pass  # never let background populate surface to the caller


def update_books(holdings, positions, margins):
    """Return all data combined into one DataFrame (optional)."""
    dfs = [holdings, positions, margins]
    dfs = [df for df in dfs if not df.empty]
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


