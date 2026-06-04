"""
broker_apis — single source of truth for every P&L number the rest of
the app consumes (strip, dashboard grids, agent engine, paper trade
sim, summarise helpers).

Policy — what's COMPUTED here vs PASSED THROUGH from the broker:
─────────────────────────────────────────────────────────────────────
COMPUTED HERE from universal market-data primitives (`quantity`,
`average_price`, `last_price`, `close_price`, `opening_quantity`,
`overnight_quantity`, `day_buy/sell_quantity`, `day_buy/sell_value`).
No broker-supplied P&L number is trusted; any one provided is
shadowed under `_broker_*` for the divergence log and dropped from
the result. Generic across Kite / Dhan / Groww / any future adapter:

  • Holdings: inv_val, cur_val, pnl, pnl_percentage, price_change,
    day_change, day_change_val
  • Positions: unrealised, realised (today only), pnl, day_change,
    day_change_val, day_change_percentage, pnl_percentage

PASSED THROUGH from the broker (broker-side account state — not
derivable from market data; we don't try):

  • Margins / Cash: avail.cash, live_cash, opening_balance,
    avail_margin, used_margin, collateral, span, exposure,
    option_premium, withdrawable/payout, intraday_payin
  • Reflects the broker's ledger (cash balance, exchange-imposed
    margin requirements, pledged collateral) that we can't
    reproduce. Strip's M / Cl / C chips rely on these.

This boundary keeps every P&L computation independent of broker
quirks (Kite's `m2m`, Dhan's `unrealisedProfit`, Groww's `pnl`) and
lets the operator audit every number against (last − avg) × qty +
(intraday split formula) without consulting per-broker docs.
"""

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

    # Operator: "strip calculations and profit/loss for positions and
    # holdings should not depend on brokers. It should be generic [and]
    # calculated independently." Every numeric column the strip + the
    # holdings grid consume is derived here from four primitives the
    # adapter must surface in Kite-shape: `quantity`, `average_price`,
    # `last_price`, `close_price`, plus `opening_quantity` for the
    # day-mark (the count present at end of yesterday's session). We
    # do NOT trust the broker's own `pnl` / `day_change` / `cur_val`
    # fields — they're rederived from the primitives below and any
    # broker number is logged when it drifts from ours.
    if {"average_price", "quantity"}.issubset(df_holdings.columns):
        df_holdings["inv_val"]  = df_holdings["average_price"] * df_holdings["quantity"]
    if {"last_price",   "quantity"}.issubset(df_holdings.columns):
        df_holdings["cur_val"]  = df_holdings["last_price"]    * df_holdings["quantity"]
    if {"last_price", "average_price", "quantity"}.issubset(df_holdings.columns):
        # Holdings are always long-only (no shorts) so pnl == unrealised
        # against entry; no realised-leg to add. (last − avg) × qty.
        df_holdings["pnl"] = (
            (df_holdings["last_price"] - df_holdings["average_price"])
            * df_holdings["quantity"]
        )
        if "inv_val" in df_holdings.columns:
            df_holdings["pnl_percentage"] = (
                df_holdings["pnl"] / df_holdings["inv_val"].replace(0, pd.NA) * 100
            ).fillna(0)
    if {"close_price", "average_price"}.issubset(df_holdings.columns):
        df_holdings["price_change"] = df_holdings["close_price"] - df_holdings["average_price"]
    if {"last_price", "close_price"}.issubset(df_holdings.columns):
        df_holdings["day_change"] = df_holdings["last_price"] - df_holdings["close_price"]
    if {"day_change", "opening_quantity"}.issubset(df_holdings.columns):
        # Day P&L on holdings = (last − close) × opening_quantity. The
        # `opening_quantity` is the count present at end of yesterday's
        # session — buys credited today are NOT marked from yesterday's
        # close (operator: "for newly opened position you need the entry
        # price"). Generic across every broker that exposes
        # opening_quantity (Kite, Dhan, Groww).
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

    # Operator: "the goal is not to depend on any broker api for any
    # calculation and make the code generic irrespective of the broker."
    # Every P&L number consumed downstream is derived here from the
    # universal primitives — `quantity`, `average_price`, `last_price`,
    # `close_price`, plus the intraday split fields
    # (`overnight_quantity`, `day_buy/sell_quantity`,
    # `day_buy/sell_value`). The broker's own `pnl` / `unrealised` /
    # `realised` / `day_change` numbers are stashed under
    # `_broker_*` columns for a divergence log only and overwritten
    # with our generic derivation. No broker computation is trusted.

    # -- Stash broker-reported numbers for the divergence log ------------
    for _src, _dst in (('pnl',        '_broker_pnl'),
                       ('unrealised', '_broker_unrealised'),
                       ('realised',   '_broker_realised'),
                       ('day_change_val', '_broker_day_change_val')):
        if _src in df_positions.columns:
            df_positions[_dst] = df_positions[_src]

    # -- 1. unrealised = (last − avg) × current_quantity ----------------
    # Universal definition. Works for every broker because it depends
    # only on the three primitives the broker MUST surface.
    df_positions['unrealised'] = (
        (df_positions['last_price'] - df_positions['average_price'])
        * df_positions['quantity']
    )

    # -- Rescale qty-shape intraday-split fields by multiplier --------
    # `quantity` was already rescaled at line ~85 (multiplier transform).
    # `overnight_quantity` / `day_buy_quantity` / `day_sell_quantity`
    # are documented (Kite) as raw lots and need the same treatment so
    # the formulas below mix consistent post-multiplier units. NOTE:
    # `day_buy_value` / `day_sell_value` are ALREADY ₹ cash per Kite's
    # API spec — must NOT be rescaled (earlier patch did and inflated
    # MCX legs ~100×).
    _split_cols = ('overnight_quantity', 'day_buy_quantity',
                   'day_sell_quantity', 'day_buy_value', 'day_sell_value')
    _has_intraday_split = all(c in df_positions.columns for c in _split_cols)
    if _has_intraday_split and 'multiplier' in df_positions.columns:
        _mult = df_positions['multiplier'].fillna(1).replace(0, 1)
        df_positions['overnight_quantity'] = df_positions['overnight_quantity'] * _mult
        df_positions['day_buy_quantity']   = df_positions['day_buy_quantity']   * _mult
        df_positions['day_sell_quantity']  = df_positions['day_sell_quantity']  * _mult

    # -- 2. realised — cash realised on intraday closeouts -------------
    # `(day_sell_value − day_buy_value) − last × (day_buy_qty − day_sell_qty)`
    # is the cash impact of trades that were CLOSED today (vs. trades
    # still held marked-to-last). For a fully-closed-today row
    # (qty=0, day_net_qty=0), this reduces to the gross cash flow
    # (sell_value − buy_value). For a fresh open today, it's zero.
    # Cumulative realised across prior sessions is NOT derivable from
    # a single snapshot — that requires session-to-session ledger
    # storage. Documented limitation: `realised` is TODAY ONLY.
    if _has_intraday_split:
        _day_net_qty = (df_positions['day_buy_quantity']
                        - df_positions['day_sell_quantity'])
        df_positions['realised'] = (
            (df_positions['day_sell_value'] - df_positions['day_buy_value'])
            - df_positions['last_price'] * _day_net_qty
        )
    else:
        df_positions['realised'] = 0.0

    # -- 3. pnl = unrealised + realised --------------------------------
    # Today's total P&L on the position. NOT lifetime cumulative.
    # Strip's "P" chip surfaces this sum.
    df_positions['pnl'] = df_positions['unrealised'] + df_positions['realised']

    # -- 3. day-change (broker-agnostic split) ----------------------------
    # Operator: "for overnight positions, use yesterday's market close;
    # for newly opened position you need the entry price of the
    # position as open price. For closed position you need to consider
    # the [exit] price. Rest of the positions usual calculation". The
    # split formula reduces to (last − close) × qty for pure overnight
    # legs and to (last − avg) × qty for legs opened today; mixed legs
    # take both terms plus the realised cash from any portion that
    # was already closed today.
    #
    #   day_pnl = (last − close) × overnight_qty                ← overnight mark
    #           + last × (day_buy_qty − day_sell_qty)           ← intraday net qty marked at last
    #           + (day_sell_value − day_buy_value)              ← intraday realised cash flow
    #
    df_positions['day_change'] = df_positions['last_price'] - df_positions['close_price']
    if _has_intraday_split:
        # Reuses the qty columns already rescaled above. `day_buy_value`
        # / `day_sell_value` are ₹ cash and stay as-is.
        df_positions['day_change_val'] = (
            (df_positions['last_price'] - df_positions['close_price'])
                * df_positions['overnight_quantity']
            + df_positions['last_price']
                * (df_positions['day_buy_quantity'] - df_positions['day_sell_quantity'])
            + (df_positions['day_sell_value'] - df_positions['day_buy_value'])
        )
    else:
        # Adapter doesn't surface intraday split — fall back to
        # (last − close) × current_qty (overnight-only assumption).
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

    # Divergence log — sanity-check our generic derivation against the
    # broker's own numbers stashed earlier. Open-position pnl should
    # match (broker reports lifetime cumulative which includes any
    # prior-session realised; our derivation is today-only). Drop the
    # broker-shadow columns after the log so the API surface stays
    # clean of legacy fields.
    try:
        _shadow_cols = ['_broker_pnl', '_broker_unrealised',
                        '_broker_realised', '_broker_day_change_val']
        _present = [c for c in _shadow_cols if c in df_positions.columns]
        if _present:
            for _i, _r in df_positions.iterrows():
                _broker_pnl = float(_r.get('_broker_pnl', 0.0) or 0.0)
                _ours_pnl   = float(_r.get('pnl', 0.0) or 0.0)
                # Only flag OPEN rows — closed-today rows can legitimately
                # diverge because the broker counts lifetime realised we
                # can't reproduce from a single snapshot.
                if int(_r.get('quantity', 0)) != 0:
                    _diff = abs(_broker_pnl - _ours_pnl)
                    if _diff > 5.0:    # ₹5 noise threshold for FX/rounding
                        logger.debug(
                            f"[{account}] {_r.get('tradingsymbol', '?')}: "
                            f"pnl divergence broker={_broker_pnl:.2f} "
                            f"ours={_ours_pnl:.2f} Δ={_broker_pnl - _ours_pnl:+.2f}"
                        )
            df_positions = df_positions.drop(columns=_present)
    except Exception as _e:        # noqa: BLE001
        logger.debug(f"[{account}] pnl divergence log skipped: {_e}")

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


