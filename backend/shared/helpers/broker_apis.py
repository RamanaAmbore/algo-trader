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
            return df_holdings
        df_holdings = pd.DataFrame(rows)

        if not df_holdings.empty:
            df_holdings["account"] = account
            df_holdings["type"] = "H"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch holdings: {e}")

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
            return df_positions
        df_positions = pd.DataFrame(net_rows)
        if not df_positions.empty and "multiplier" in df_positions.columns:
            df_positions['quantity'] = df_positions['quantity'] * df_positions['multiplier']

        if not df_positions.empty:
            df_positions["account"] = account
            df_positions["type"] = "P"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch positions: {e}")
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
    _dcv_calc = (_ltp - _cls) * _qty
    # Trust broker pnl when present (not null / not NaN). Fall back
    # to (LTP - avg) × qty only on missing values. Same posture for
    # day_change_val.
    if 'pnl' in df_positions.columns:
        _broker_pnl = pd.to_numeric(df_positions['pnl'], errors='coerce')
        df_positions['pnl'] = _broker_pnl.where(_broker_pnl.notna(), _pnl_calc)
    else:
        # Adapter didn't ship a pnl column at all — synthesize from
        # the simple formula, guarded by ltp+avg validity (avoid
        # phantom values during pre-open warm-up).
        _pnl_valid = (_ltp > 0) & (_avg > 0)
        df_positions['pnl'] = _pnl_calc.where(_pnl_valid, 0.0)
    if 'day_change_val' in df_positions.columns:
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


def backfill_close_prices(df) -> int:
    """Backfill `close_price` on rows where the adapter didn't supply
    one (Dhan v2 positions endpoint doesn't return prev-close), then
    recompute `day_change_val` and `day_change_percentage` for the
    patched rows so Day P&L shows correctly downstream.

    Called by the /api/positions and /api/holdings endpoints AFTER
    `pd.concat(broker_apis.fetch_*())` so the PriceBroker.quote call
    is ONE batched round-trip across every broker's missing-close
    rows — not N per N accounts (the prior shape called the lookup
    inside the per-account `@for_all_accounts` body and burned N
    quote() calls per poll).

    No-op when `close_price` is already populated on every row
    (Kite always returns it; Groww too). Exception-safe: a broker
    outage on PriceBroker leaves rows untouched and Day P&L
    fallback behaviour matches the pre-patch state.

    Returns the count of patched rows (informational for callers'
    debug logs).
    """
    if df is None or df.empty or 'close_price' not in df.columns:
        return 0
    _missing = pd.to_numeric(df['close_price'], errors='coerce').fillna(0).le(0)
    if not _missing.any():
        return 0

    # Build unique quote keys across every missing-close row.
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
            f"PriceBroker close-price backfill failed (1 batched call for "
            f"{len(_unique_keys)} symbols): {_e}"
        )
        return 0

    _close_lookup: dict[str, float] = {}
    for _k, _v in _q.items():
        if not isinstance(_v, dict):
            continue
        _ohlc = _v.get('ohlc') if isinstance(_v.get('ohlc'), dict) else {}
        _cls_val = _ohlc.get('close') if _ohlc else None
        if _cls_val is None:
            _cls_val = _v.get('close_price')
        try:
            _f = float(_cls_val) if _cls_val is not None else 0.0
        except (TypeError, ValueError):
            _f = 0.0
        if _f > 0:
            _close_lookup[_k] = _f

    # Patch close_price in place; record which row indices got values
    # so we can re-run the day_change_val recompute for ONLY those rows
    # (rows that already had a valid close stay untouched).
    _row_indices = df.index[_missing].tolist()
    _patched_indices: list = []
    for _idx, _k in zip(_row_indices, _key_per_row):
        _looked_up = _close_lookup.get(_k)
        if _looked_up:
            df.at[_idx, 'close_price'] = _looked_up
            _patched_indices.append(_idx)

    if not _patched_indices:
        return 0

    # Re-run the (LTP - close) × qty recompute on patched rows only.
    # The per-account fetch already wrote a value (0 or broker-reported)
    # that the consumer treats as authoritative — overwrite it now that
    # we have a real close.
    _qty_col = 'opening_quantity' if 'opening_quantity' in df.columns else 'quantity'
    if _qty_col not in df.columns or 'last_price' not in df.columns:
        return len(_patched_indices)

    _idx_array = pd.Index(_patched_indices)
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
    df.loc[_idx_array, 'day_change'] = _ltp_p - _cls_p

    # day_change_percentage rides off close × qty in the consumer's
    # per-account summary. For row-level we update the column directly
    # so the API response is consistent.
    if 'day_change_percentage' in df.columns:
        _prev_val_p = (_cls_p * _qty_p).abs()
        _pct_p = (_dcv_p / _prev_val_p.replace(0, pd.NA) * 100).fillna(0)
        df.loc[_idx_array, 'day_change_percentage'] = _pct_p.where(
            _valid_p, df.loc[_idx_array, 'day_change_percentage']
        )

    return len(_patched_indices)


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

    Cached per-day in-process — first call of the day hits the API, the
    rest of the day's calls return the cached set. The cache key is the
    exchange + today's date, so the natural rollover happens at midnight.
    """
    import requests
    from datetime import datetime as dt_datetime, date as dt_date

    today = dt_date.today()
    cached = _HOLIDAY_CACHE.get(exchange)
    if cached and cached[0] == today:
        return cached[1]

    # Map Kite exchange names to NSE holiday API segment keys
    # CM=equity cash, FO=F&O, CD=currency, COM=commodity(MCX)
    _SEGMENT_MAP = {"NSE": "CM", "BSE": "CM", "NFO": "FO", "CDS": "CD", "MCX": "COM"}

    holidays = set()
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
    return holidays


def update_books(holdings, positions, margins):
    """Return all data combined into one DataFrame (optional)."""
    dfs = [holdings, positions, margins]
    dfs = [df for df in dfs if not df.empty]
    return pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()


