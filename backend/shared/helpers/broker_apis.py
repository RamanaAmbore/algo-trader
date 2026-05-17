import pandas as pd

from backend.shared.helpers.connections import Connections
from backend.shared.helpers.decorators import for_all_accounts
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


@for_all_accounts
def fetch_holdings(connections=Connections, account=None, kite=None):
    # ✅ Holdings
    df_holdings = pd.DataFrame()
    try:
        df_holdings = pd.DataFrame(kite.holdings())

        if not df_holdings.empty:
            df_holdings["account"] = account
            df_holdings["type"] = "H"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch holdings: {e}")

    # Calculated columns — guard against an empty / fetch-failed frame
    # (Kite 502 / 503 outages leave df_holdings empty and skipping the
    # math here is the difference between an empty response and a 500
    # KeyError on 'average_price').
    if df_holdings.empty:
        return df_holdings

    df_holdings["inv_val"] = df_holdings["average_price"] * df_holdings["opening_quantity"]
    df_holdings["cur_val"] = df_holdings["inv_val"] + df_holdings["pnl"]
    df_holdings["price_change"] = df_holdings["close_price"] - df_holdings["average_price"]
    df_holdings["pnl_percentage"] = df_holdings["pnl"] / df_holdings["inv_val"] * 100

    # Δ calculation (delta value)
    df_holdings["day_change_val"] = df_holdings["day_change"] * df_holdings["opening_quantity"]
    # Format Date column
    df_holdings["authorised_date"] = pd.to_datetime(df_holdings["authorised_date"]).dt.strftime("%d%b%y")

    return df_holdings


@for_all_accounts
def fetch_positions(connections=Connections, account=None, kite=None):
    # ✅ Positions
    df_positions = pd.DataFrame()
    try:
        df_positions = pd.DataFrame(kite.positions()["net"])  # "day" also available
        df_positions['quantity'] = df_positions['quantity'] * df_positions['multiplier']

        if not df_positions.empty:
            df_positions["account"] = account
            df_positions["type"] = "P"
    except Exception as e:
        logger.error(f"[{account}] Failed to fetch positions: {e}")
        return df_positions

    if df_positions.empty:
        return df_positions

    # Derive day-change. Kite gives last_price + close_price (yesterday's
    # close) per position row. Day change ₹ = (last - close) * qty.
    # Day change % = day_change_val / |close * qty| — guarded against zero.
    df_positions['day_change'] = df_positions['last_price'] - df_positions['close_price']
    df_positions['day_change_val'] = df_positions['day_change'] * df_positions['quantity']
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
def fetch_margins(connections=Connections, account=None, kite=None):
    # ✅ Margins (Cash)
    df_margins = pd.DataFrame()
    try:
        df_margins = pd.DataFrame([kite.margins(segment="equity")])

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


# def fetch_books(holdings, positions, margins):
#     books = [obj.books for obj in connections]
#
#     # Union/concat them into one dataframe
#     df_books = pd.concat(books, ignore_index=True)
#     df_positions = pd.concat(positions, ignore_index=True)
#     df_holdings = pd.concat(holdings, ignore_index=True)
#     df_margin = pd.concat(margins, ignore_index=True)
#
#     # Add calculated columns
#     df_holdings["inv_val"] = df_holdings["average_price"] * df_holdings["opening_quantity"]
#     df_holdings["cur_val"] = df_holdings["inv_val"] + df_holdings["pnl"]
#
#     # Δ calculation (delta value)
#     df_holdings["day_change_val"] = df_holdings["day_change"] * df_holdings["average_price"]
#
#     # Format Date column
#     df_holdings["Date"] = pd.to_datetime(df_holdings["Date"]).dt.strftime("%d%b%y")
#
#     total_pnl = df_holdings["P&L"].sum().round(2)
#     total_day_change_val = df_holdings["day_change_val"].sum().round(2)
#     total_inv_val = df_holdings["inv_val"].sum().round(2)
#     total_cur_val = df_holdings["cur_val"].sum().round(2)
#
#     # Prepend totals row
#     df_holdings = pd.concat([pd.DataFrame([totals]), df_holdings], ignore_index=True)
#
#     return df_books, df_positions, df_holdings, df_margin
#
#
# def fetch_all_holdings(connections):
#     all_holdings = pd.concat([fetch_holdings(conn) for conn in connections.connections])
#
#
# def fetch_all_positions(connections):
#     all_positions = pd.concat([fetch_holdings(conn) for conn in connections.connections])
#
#
# def fetch_all_margins(connections):
#     all_margins = pd.concat([fetch_holdings(conn) for conn in connections.connections])
#
#
# def fetch_data(conn):
#     """Fetch holdings, positions, and margins as DataFrames with indicators and account column."""
#     conn = Connections().conn
#     holdings, total_holdings = fetch_holdings()
#
#     positions, total_positions = fetch_positions()
#
#     margins, total_margins = fetch_margins()
#
#     books = update_books(holdings, positions, margins)


if __name__ == "__main__":
    df = pd.concat(fetch_holdings(), ignore_index=True)
    non_numeric_cols = [
        col for col in df.columns
        if not pd.to_numeric(df[col], errors="coerce").notna().all()
    ]

    print(non_numeric_cols)
    # print(pd.concat(fetch_positions(), ignore_index=True))
    # print(pd.concat(fetch_margins(), ignore_index=True))
