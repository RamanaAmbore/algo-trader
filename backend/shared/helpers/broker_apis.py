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
    if "pnl" in df_holdings.columns and "inv_val" in df_holdings.columns:
        df_holdings["cur_val"] = df_holdings["inv_val"] + df_holdings["pnl"]
        df_holdings["pnl_percentage"] = df_holdings["pnl"] / df_holdings["inv_val"] * 100
    if {"close_price", "average_price"}.issubset(df_holdings.columns):
        df_holdings["price_change"] = df_holdings["close_price"] - df_holdings["average_price"]
    if {"day_change", "opening_quantity"}.issubset(df_holdings.columns):
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

    # Derive day-change. Broker-agnostic split — operator: "for overnight
    # positions, use yesterday's market close; for newly opened positions,
    # use entry price; for closed positions, use the realised cash". The
    # naive `(last - close) × current_qty` form double-counts the
    # (close - avg) × qty gap on legs opened today, treating yesterday's
    # close as a mark the position never actually experienced. The full
    # formula splits the leg into overnight + intraday halves:
    #
    #   day_pnl = (last - close) × overnight_qty                ← overnight mark
    #           + last × (day_buy_qty − day_sell_qty)           ← intraday net qty marked at last
    #           + (day_sell_value − day_buy_value)              ← intraday realised cash flow
    #
    # Reduces correctly: pure overnight → first term; opened today → 2nd + 3rd
    # cancel into (last − avg) × qty; partially closed today → mark on
    # remainder + realised on closed portion; fully closed today → realised
    # cash only.
    df_positions['day_change'] = df_positions['last_price'] - df_positions['close_price']
    _split_cols = ('overnight_quantity', 'day_buy_quantity',
                   'day_sell_quantity', 'day_buy_value', 'day_sell_value')
    _has_intraday_split = all(c in df_positions.columns for c in _split_cols)
    if _has_intraday_split:
        # Apply the lot-size multiplier consistently — Kite returns
        # quantity / overnight_quantity / day_buy_quantity /
        # day_sell_quantity in raw lots (per-doc convention); the
        # `quantity` column was already rescaled at line ~85 above so
        # the rest of the qty-shaped columns get the same treatment.
        # IMPORTANT: day_buy_value / day_sell_value are documented as
        # CASH (₹) in Kite's API spec — Day's accumulated buy/sell
        # value already includes the multiplier — so they MUST NOT be
        # rescaled here. Earlier patch did, inflating MCX legs ~100×
        # (operator saw a +161 L blow-up). Other adapters (Dhan,
        # Groww) follow the same cash-units convention.
        if 'multiplier' in df_positions.columns:
            mult = df_positions['multiplier'].fillna(1).replace(0, 1)
            df_positions['overnight_quantity'] = df_positions['overnight_quantity'] * mult
            df_positions['day_buy_quantity']   = df_positions['day_buy_quantity']   * mult
            df_positions['day_sell_quantity']  = df_positions['day_sell_quantity']  * mult
        _day_net_qty       = df_positions['day_buy_quantity'] - df_positions['day_sell_quantity']
        _day_realised_cash = df_positions['day_sell_value']   - df_positions['day_buy_value']
        df_positions['day_change_val'] = (
            (df_positions['last_price'] - df_positions['close_price'])
                * df_positions['overnight_quantity']
            + df_positions['last_price'] * _day_net_qty
            + _day_realised_cash
        )
    else:
        # Legacy fallback — broker adapter doesn't surface intraday split.
        # Treat every leg as overnight; same as the historical formula.
        df_positions['day_change_val'] = df_positions['day_change'] * df_positions['quantity']
    # day_change_percentage denominator stays as |close × current_qty|
    # so the chip reads "% MTM vs notional book value coming into today".
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


