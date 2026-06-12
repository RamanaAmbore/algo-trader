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
    # Universal P&L recompute — operator's formula:
    #   pnl       = (LTP   − avg)   × qty
    #   day_change= (LTP   − close)        (per-share, then × qty for the val)
    # Apply at the central chokepoint so every broker (Kite, Dhan, Groww)
    # surfaces consistent numbers. Falls back to broker-reported pnl
    # when last_price hasn't ticked yet (pre-open, broker glitch) so the
    # column doesn't go blank during early-session warm-up.
    if {"last_price", "average_price", "opening_quantity"}.issubset(df_holdings.columns):
        _ltp_h = pd.to_numeric(df_holdings["last_price"], errors="coerce").fillna(0)
        _avg_h = pd.to_numeric(df_holdings["average_price"], errors="coerce").fillna(0)
        _qty_h = pd.to_numeric(df_holdings["opening_quantity"], errors="coerce").fillna(0)
        _pnl_calc = (_ltp_h - _avg_h) * _qty_h
        # Only overwrite when LTP + avg are both positive (a quoted, real
        # position). For pre-open rows where LTP=0 the recompute would
        # post a phantom −avg×qty loss; keep broker's pnl in that case.
        _valid = (_ltp_h > 0) & (_avg_h > 0)
        if "pnl" in df_holdings.columns:
            df_holdings["pnl"] = _pnl_calc.where(_valid, df_holdings["pnl"])
        else:
            df_holdings["pnl"] = _pnl_calc.where(_valid, 0.0)
    if "pnl" in df_holdings.columns and "inv_val" in df_holdings.columns:
        df_holdings["cur_val"] = df_holdings["inv_val"] + df_holdings["pnl"]
        df_holdings["pnl_percentage"] = df_holdings["pnl"] / df_holdings["inv_val"] * 100
    if {"close_price", "average_price"}.issubset(df_holdings.columns):
        df_holdings["price_change"] = df_holdings["close_price"] - df_holdings["average_price"]
    # day_change_val = (LTP − close) × qty — recompute from LTP first
    # (matches operator's spec). Broker-reported `day_change` (delta per
    # share) is a fallback only when last_price is missing.
    if {"last_price", "close_price", "opening_quantity"}.issubset(df_holdings.columns):
        _ltp_h2 = pd.to_numeric(df_holdings["last_price"], errors="coerce").fillna(0)
        _cls_h  = pd.to_numeric(df_holdings["close_price"], errors="coerce").fillna(0)
        _qty_h2 = pd.to_numeric(df_holdings["opening_quantity"], errors="coerce").fillna(0)
        _dcv_calc = (_ltp_h2 - _cls_h) * _qty_h2
        _valid_d = (_ltp_h2 > 0) & (_cls_h > 0)
        if "day_change_val" in df_holdings.columns:
            df_holdings["day_change_val"] = _dcv_calc.where(_valid_d, df_holdings["day_change_val"])
        else:
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

    # ── P∆ (day change in P&L on positions) ─────────────────────────────
    # Operator-stated formula, applied universally across every broker
    # adapter:
    #
    #   pnl            = (LTP − avg)   × qty           ← lifetime / unrealised
    #   day_change_val = (LTP − close) × qty           ← today's change
    #
    # Falls back to the broker-reported value only when LTP / avg / close
    # is zero or missing (pre-open warm-up, broker glitch) — keeps the
    # column non-blank during early session while still letting our
    # formula take over the moment a real quote lands.
    #
    # NOTE: an earlier revision used a "split" formula that combined
    # overnight MTM + intraday MTM + realised cash flow to correctly
    # attribute partial-closeout P&L. That gave numerically different
    # values from (LTP − avg) × qty when a leg was partially closed
    # today, which the operator reported as wrong. The simple formula
    # above matches the operator's mental model ("entry price and
    # current price difference is P&L; from yesterday's closing price
    # and today's price is day P&L") and is now the source of truth
    # across Kite, Dhan, and Groww.
    # Dhan's v2 positions endpoint doesn't return a previous-close
    # field (only position state — qty / avg / LTP / unrealised). Without
    # it, close_price=0 on every Dhan row and the recompute below would
    # fall back to broker-reported day_change_val (also 0 by the same
    # logic in the Dhan adapter). End-to-end effect: Day P&L renders
    # as `—` for every Dhan position on /pulse + Candidates panel.
    #
    # Fix: lookup yesterday's close via PriceBroker (Kite has the data
    # for every symbol regardless of which broker holds the position)
    # in one batched call. broker.quote returns ohlc.close per quote
    # key — same data feed every other consumer reads. Costs 1 Kite
    # quote() call per /api/positions tick when at least one row has
    # close_price=0; skipped entirely when every adapter populated
    # close_price natively (Kite, Groww).
    if 'close_price' in df_positions.columns:
        _missing_close_mask = pd.to_numeric(
            df_positions['close_price'], errors='coerce'
        ).fillna(0).le(0)
        if _missing_close_mask.any():
            try:
                from backend.shared.brokers.registry import get_price_broker
                _missing_rows = df_positions[_missing_close_mask]
                _quote_keys: list[str] = []
                _key_per_row: list[str] = []
                for _, _row in _missing_rows.iterrows():
                    _exch = str(_row.get('exchange', '') or 'NFO').upper()
                    _sym  = str(_row.get('tradingsymbol', '') or '').upper()
                    if _sym:
                        _k = f"{_exch}:{_sym}"
                        _key_per_row.append(_k)
                        if _k not in _quote_keys:
                            _quote_keys.append(_k)
                    else:
                        _key_per_row.append('')
                if _quote_keys:
                    _pb = get_price_broker()
                    _q = _pb.quote(_quote_keys) or {}
                    _close_lookup: dict[str, float] = {}
                    for _k, _v in _q.items():
                        if isinstance(_v, dict):
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
                    # Patch close_price on rows where we found a value.
                    _row_indices = df_positions.index[_missing_close_mask].tolist()
                    for _idx, _k in zip(_row_indices, _key_per_row):
                        _looked_up = _close_lookup.get(_k)
                        if _looked_up:
                            df_positions.at[_idx, 'close_price'] = _looked_up
            except Exception as _e:
                logger.warning(
                    f"[{account}] PriceBroker close-price lookup failed "
                    f"for Dhan/other rows: {_e}"
                )

    df_positions['day_change'] = df_positions['last_price'] - df_positions['close_price']
    _ltp = pd.to_numeric(df_positions['last_price'],    errors='coerce').fillna(0)
    _avg = pd.to_numeric(df_positions['average_price'], errors='coerce').fillna(0)
    _cls = pd.to_numeric(df_positions['close_price'],   errors='coerce').fillna(0)
    _qty = pd.to_numeric(df_positions['quantity'],      errors='coerce').fillna(0)
    _pnl_calc = (_ltp - _avg) * _qty
    _dcv_calc = (_ltp - _cls) * _qty
    _pnl_valid = (_ltp > 0) & (_avg > 0)
    _dcv_valid = (_ltp > 0) & (_cls > 0)
    if 'pnl' in df_positions.columns:
        df_positions['pnl'] = _pnl_calc.where(_pnl_valid, df_positions['pnl'])
    else:
        df_positions['pnl'] = _pnl_calc.where(_pnl_valid, 0.0)
    df_positions['day_change_val'] = _dcv_calc.where(
        _dcv_valid,
        df_positions.get('day_change_val', 0.0)
        if 'day_change_val' in df_positions.columns
        else 0.0,
    )
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


