"""
Segment-scoped summary helpers used by the Litestar API background tasks.

Extracted from the legacy background_refresh daemon — these two functions
re-derive per-account + TOTAL rollups from segment-filtered broker DataFrames
(equity/commodity split).
"""

import pandas as pd


def summarise_holdings(seg_holdings, full_sum_holdings, seg_exchanges=None):
    """Re-derive per-account + TOTAL summary from segment-filtered holdings rows."""
    if seg_holdings.empty:
        return seg_holdings

    sum_columns = ["inv_val", "cur_val", "pnl", "day_change_val"]
    grouped = seg_holdings.groupby("account")[sum_columns].sum().reset_index()

    grouped['pnl_percentage']        = grouped['pnl'] / grouped['inv_val'] * 100
    grouped['day_change_percentage'] = grouped['day_change_val'] / grouped['cur_val'] * 100

    if 'cash' in full_sum_holdings.columns:
        cash_df = full_sum_holdings[full_sum_holdings['account'] != 'TOTAL'][['account', 'cash', 'net']]
        grouped = pd.merge(grouped, cash_df, on='account', how='left')

    total = grouped[[c for c in sum_columns if c in grouped.columns]].sum().to_frame().T
    total['account'] = 'TOTAL'
    if 'pnl_percentage' in grouped.columns:
        total['pnl_percentage'] = total['pnl'] / total['inv_val'] * 100
    if 'day_change_percentage' in grouped.columns:
        total['day_change_percentage'] = total['day_change_val'] / total['cur_val'] * 100

    return pd.concat([grouped, total], ignore_index=True)


def summarise_positions(seg_positions):
    """Re-derive per-account + TOTAL summary from segment-filtered positions rows."""
    if seg_positions.empty:
        return seg_positions

    grouped = seg_positions.groupby("account")[["pnl"]].sum().reset_index()
    total = pd.DataFrame([{'account': 'TOTAL', 'pnl': grouped['pnl'].sum()}])
    return pd.concat([grouped, total], ignore_index=True)


def _build_und_buckets(rows, parse_tradingsymbol) -> dict[str, dict]:
    """Aggregate rows into {underlying: {pnl, count}} buckets."""
    buckets: dict[str, dict] = {}
    for _, r in rows.iterrows():
        sym = str(r.get('tradingsymbol', '') or '').upper()
        if not sym:
            continue
        parsed = parse_tradingsymbol(sym)
        und = (parsed.get('root') if parsed else None) or sym
        b = buckets.setdefault(und, {'pnl': 0.0, 'count': 0})
        b['pnl']   += float(r.get('pnl', 0) or 0)
        b['count'] += 1
    return buckets


def breakdown_positions_by_underlying(df, *, account=None, top_n=5):
    """
    Group raw position rows by parsed underlying (NIFTY, BANKNIFTY, GOLDM, …)
    and return top-N entries sorted by |pnl| descending. account=None or
    'TOTAL' aggregates across every account; otherwise filters to that one
    account.

    Parsing comes from `derivatives.parse_tradingsymbol`, which understands
    NSE F&O monthly + weekly + stock-options + monthly futures + MCX
    commodities. Cash equity (parser returns None) falls back to the
    tradingsymbol itself as the bucket key.

    Returns: list of {underlying, pnl, count}.

    Used by the alert formatter to append a `by und: NIFTY -₹22k · …` line
    to position alerts, and by the open/close summary to surface the same
    breakdown alongside per-account totals.
    """
    # Lazy import — derivatives lives under backend.api and we don't want
    # the helpers package to pull it at module load.
    from backend.api.algo.derivatives import parse_tradingsymbol

    if df is None or df.empty:
        return []
    if account and account != 'TOTAL' and 'account' in df.columns:
        rows = df[df['account'] == account]
    else:
        rows = df
    if rows.empty:
        return []

    buckets = _build_und_buckets(rows, parse_tradingsymbol)
    out = [{'underlying': u, 'pnl': v['pnl'], 'count': v['count']}
           for u, v in buckets.items()]
    out.sort(key=lambda r: abs(r['pnl']), reverse=True)
    n = max(1, int(top_n or 5))
    return out[:n]
