"""
NAV calculation — firm-level daily aggregate.

NAV (v4) = Σ (cash_sod + option_premium)  across all funded accounts
         + Σ position.unrealised          across open positions
         + Σ holding.cur_val              across all holdings

Why v4 replaces v3:

  • v3 added `used_margin` back to cash to undo broker_funds.net's
    subtraction. That double-counted any futures SPAN margin that's
    *already* embedded in position.unrealised (broker's M2M reflects
    the funded margin requirement; adding the cash form of it again
    inflates NAV). Audit Sprint E confirmed via account-level
    reconciliation against `kite.profile().net`.
  • v4 uses `option_premium` only — the sum of long-option premiums
    paid (operator-verified spec). Cash side becomes
    `cash_sod + option_premium`, leaving futures margin to flow
    purely through position.unrealised.

Operator framework (unchanged across v3 → v4):

  • Collateral has zero impact on NAV. Pledged stock is the SAME
    stock already counted in holdings.cur_val — including
    funds.collateral would double-count it.
  • Cash spent on options is captured by adding `option_premium`
    back: the broker debits cash when you buy a long option, then
    surfaces the premium under `util option_premium`. Adding it
    back means the long-option leg is reflected via
    position.unrealised (M2M re-valuation) without losing the cost
    basis.
  • Only M2M unrealized gains/losses move NAV. Positions
    contribute their unrealised field (LTP-avg)×qty — the broker's
    pre-computed open-position P&L. Holdings contribute cur_val
    (qty × LTP). Neither term double-counts the cash spent on
    them; that cash converted into the position/holding at cost,
    and cur_val / unrealised captures the M2M re-valuation.
  • Holdings qty × LTP: you DO own the shares, so the full
    mark-to-market value is your wealth. Pledged shares are
    already counted via funds.net (haircut collateral), so
    non-pledged holdings are what fetch_holdings returns.

LTPs come from the same fallback chain the strategy unrealised
calc uses: KiteTicker tick_map (zero broker quota for subscribed
symbols) → row.last_price. Symbols with no LTP available contribute
0 to the MTM (under-estimate is safer than refusing to compute).

Caller responsibility:
- Pass an active asyncio session (not running on the chase loop
  thread).
- The daily background task (`_run_nav_compute_once`, background.py)
  calls this once per day at the MCX close-settled moment (≈23:45 IST,
  2026-09 fix — previously an inaccurate fixed 16:00 IST that predated
  MCX's own 23:30 close); the operator can also trigger via the admin
  endpoint for ad-hoc recompute / backfill.

SSOT (2026-09 NAV consolidation): this module is now the ONLY place
the v4 formula is computed, firm-level AND per-account
(`compute_firm_nav()["by_account"]`). The frontend no longer
maintains a parallel implementation — `frontend/src/lib/data/nav.js`'s
`navRowForAccount`/`navByAccount` (which had already drifted: missing
the `realised` term, no unrealised qty!=0 gate, no holdings
ticker-rescue fallback) were removed in favour of
`GET /api/nav/by-account` (backend/api/routes/nav.py), which returns
this module's own `by_account` breakdown directly. Any future formula
revision only needs to change this file.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Optional

import polars as pl

logger = logging.getLogger(__name__)


def _funds_from_df(df) -> tuple[float, list[str]]:
    """Vectorized extraction of (cash_total, accounts) from a margins DataFrame.

    NAV cash term (v4): SOD cash (avail opening_balance) + long-option
    premium paid (util option_premium). See module docstring for why.

    Returns (cash_sum, list_of_account_strings).
    """
    if df is None or df.empty:
        return 0.0, []

    lf = pl.from_pandas(df, nan_to_null=True)

    # Resolve cash column — prefer "avail opening_balance", fall back to "cash".
    if "avail opening_balance" in lf.columns:
        cash_col = pl.col("avail opening_balance").cast(pl.Float64, strict=False).fill_null(0.0)
    elif "cash" in lf.columns:
        cash_col = pl.col("cash").cast(pl.Float64, strict=False).fill_null(0.0)
    else:
        cash_col = pl.lit(0.0)

    # Resolve premium column — prefer "util option_premium", fall back to "option_premium".
    if "util option_premium" in lf.columns:
        prem_col = pl.col("util option_premium").cast(pl.Float64, strict=False).fill_null(0.0)
    elif "option_premium" in lf.columns:
        prem_col = pl.col("option_premium").cast(pl.Float64, strict=False).fill_null(0.0)
    else:
        prem_col = pl.lit(0.0)

    cash_sum = float(
        lf.select((cash_col + prem_col).sum()).to_series()[0] or 0.0
    )

    accounts: list[str] = []
    if "account" in lf.columns:
        accounts = (
            lf.filter(
                pl.col("account").is_not_null()
                & (pl.col("account").cast(str) != "TOTAL")
                & (pl.col("account").cast(str) != "")
            )
            .select(pl.col("account").cast(str).unique())
            .to_series()
            .to_list()
        )

    return cash_sum, accounts


def _funds_null_cash_accounts(df) -> list[str]:
    """Return accounts in `df` whose cash figure (avail opening_balance /
    cash) is genuinely null — sibling to `_funds_from_df`, which
    `fill_null(0.0)`s this same column for arithmetic. Callers append an
    `_UNDERSTATED_TAG`-prefixed entry per account this returns, so a
    broker-confirmed missing cash figure defers the NAV write instead of
    silently landing as a confirmed ₹0 (2026-10 audit fix).

    Deliberately scoped to the CASH component only — NOT the premium
    column (`util option_premium` / `option_premium`). Groww's margins
    endpoint has no confirmed source field for option premium at all
    (see `groww.py:_groww_margin_utilised`'s docstring — "no confirmed
    Groww source field on this endpoint at all"), so a null premium
    there is a permanent, by-design limitation of that broker, not a
    per-cycle fetch gap. Tagging every Groww-account null premium as
    UNDERSTATED would mark every NAV snapshot for any firm with an
    active Groww account understated forever, defeating the entire
    purpose of the skip/force-write policy (see `_UNDERSTATED_TAG`'s own
    docstring) rather than catching a genuine gap. Dhan's `sodLimit`
    (mapped to `avail opening_balance`/cash) IS expected to be present
    on a healthy response, so a null there is the kind of "broker didn't
    tell us this cycle" gap this check exists to catch.
    """
    if df is None or df.empty or "account" not in df.columns:
        return []
    lf = pl.from_pandas(df, nan_to_null=True)
    if "avail opening_balance" in lf.columns:
        cash_col = pl.col("avail opening_balance")
    elif "cash" in lf.columns:
        cash_col = pl.col("cash")
    else:
        # No cash-ish column present at all — _funds_from_df already
        # treats this as a flat lit(0.0) for the whole frame, which is
        # a shape mismatch (not a per-account gap); not reported here.
        return []
    return (
        lf.filter(
            cash_col.is_null()
            & pl.col("account").is_not_null()
            & (pl.col("account").cast(str) != "TOTAL")
        )
        .select(pl.col("account").cast(str))
        .to_series()
        .to_list()
    )


def _positions_from_df(df) -> tuple[float, list[str]]:
    """Vectorized extraction of (positions_mtm, accounts) from a positions DataFrame.

    NAV wants `current_total_profit` (lifetime total, per pnl_math.py SSOT),
    NOT the baseline-diff Day P&L — summed as two legs:
      - unrealised: only for rows where quantity != 0 (broker-computed M2M —
        avoids the F&O notional-vs-value bug; legitimately 0 on flat rows).
      - realised: summed UNGATED (no qty filter). A same-day full exit has
        quantity == 0 but its realised P&L is exactly the value that used to
        go missing from NAV — gating it on qty != 0 silently dropped it.
    """
    if df is None or df.empty:
        return 0.0, []

    lf = pl.from_pandas(df, nan_to_null=True)

    qty_col = (
        pl.col("quantity").cast(pl.Float64, strict=False).fill_null(0.0)
        if "quantity" in lf.columns
        else pl.lit(0.0)
    )
    unr_col = (
        pl.col("unrealised").cast(pl.Float64, strict=False).fill_null(0.0)
        if "unrealised" in lf.columns
        else pl.lit(0.0)
    )
    real_col = (
        pl.col("realised").cast(pl.Float64, strict=False).fill_null(0.0)
        if "realised" in lf.columns
        else pl.lit(0.0)
    )

    # Unrealised only where qty != 0 (matches original per-row guard);
    # realised summed ungated — see docstring.
    mtm = float(
        lf.select(
            pl.when(qty_col != 0.0).then(unr_col).otherwise(pl.lit(0.0)).sum()
            + real_col.sum()
        ).to_series()[0] or 0.0
    )

    accounts: list[str] = []
    if "account" in lf.columns:
        accounts = (
            lf.filter(
                pl.col("account").is_not_null()
                & (pl.col("account").cast(str) != "")
            )
            .select(pl.col("account").cast(str).unique())
            .to_series()
            .to_list()
        )

    return mtm, accounts


def _resolve_qty_col(lf):
    """Return the Polars expression for the quantity column in a holdings frame."""
    for c in ("quantity", "opening_qty", "opening_quantity"):
        if c in lf.columns:
            return pl.col(c).cast(pl.Float64, strict=False).fill_null(0.0)
    return pl.lit(0.0)


def _row_ltp(sym: str, lf, ticker) -> float:
    """Resolve LTP for a single symbol: ticker first, last_price column fallback."""
    lp = ticker.get_ltp_by_sym(sym) or 0.0
    if lp <= 0 and "last_price" in lf.columns:
        last_prices = lf.filter(
            pl.col("tradingsymbol") == sym
        ).select(
            pl.col("last_price").cast(pl.Float64, strict=False).fill_null(0.0)
        ).to_series()
        lp = float(last_prices[0]) if not last_prices.is_empty() else 0.0
    return lp


def _ltp_or_cv_fallback_sum(lf_no_ltp, ticker) -> float:
    """Sum qty × ticker_ltp when available, else sum cur_val.

    Used for holdings rows where last_price column is absent entirely.
    Ticker is tried first (market value); cur_val is the fallback when ticker
    has no entry (safer than returning 0 for genuine data we trust).
    """
    if lf_no_ltp.is_empty() or "tradingsymbol" not in lf_no_ltp.columns:
        return 0.0
    total = 0.0
    has_cv = "_cv" in lf_no_ltp.columns
    for row in lf_no_ltp.select(
        [c for c in ["tradingsymbol", "_qty", "_cv"] if c in lf_no_ltp.columns]
    ).to_dicts():
        sym = str(row.get("tradingsymbol") or "")
        qty = float(row.get("_qty") or 0.0)
        cv  = float(row.get("_cv") or 0.0) if has_cv else 0.0
        if not sym:
            continue
        lp = ticker.get_ltp_by_sym(sym) or 0.0
        total += (qty * lp) if lp > 0 else cv
    return total


def _ltp_fallback_sum(lf_need_ltp, ticker) -> float:
    """Sum qty × LTP for holdings rows that lack cur_val.

    Looks up each symbol in the ticker first; falls back to the
    last_price column when the ticker has no entry. Symbols with no
    usable price contribute 0 (under-estimate is safer than refusing).
    """
    if lf_need_ltp.is_empty() or "tradingsymbol" not in lf_need_ltp.columns:
        return 0.0
    total = 0.0
    for row in lf_need_ltp.select(["tradingsymbol", "_qty"]).to_dicts():
        sym = str(row.get("tradingsymbol") or "")
        qty = float(row.get("_qty") or 0.0)
        if not sym or qty == 0.0:
            continue
        lp = _row_ltp(sym, lf_need_ltp, ticker)
        if lp > 0:
            total += qty * lp
    return total


def _holdings_from_df(df, ticker) -> tuple[float, list[str]]:
    """Vectorized extraction of (holdings_mtm, accounts) from a holdings DataFrame.

    Uses cur_val when populated. Falls back to qty × LTP for rows where
    cur_val == 0 but qty > 0 (same logic as the original iterrows path).
    """
    if df is None or df.empty:
        return 0.0, []

    lf = pl.from_pandas(df, nan_to_null=True)

    qty_col = _resolve_qty_col(lf)
    cv_col = (
        pl.col("cur_val").cast(pl.Float64, strict=False).fill_null(0.0)
        if "cur_val" in lf.columns
        else pl.lit(0.0)
    )

    # Build _ltp and _has_ltp columns.
    # _has_ltp=False when the column is entirely absent (e.g. synthetic test frames,
    # holdings summaries) — in that case cur_val is trusted directly.
    # _has_ltp=True + _ltp<=0 means broker delivered an explicit zero/null LTP
    # (Dhan/Groww cold-cache), so cur_val is cost basis and needs ticker rescue.
    if "last_price" in lf.columns:
        ltp_col     = pl.col("last_price").cast(pl.Float64, strict=False).fill_null(0.0)
        has_ltp_col = pl.lit(True)
    else:
        ltp_col     = pl.lit(0.0)
        has_ltp_col = pl.lit(False)

    # Rows where both qty and cur_val are zero — skip (same as original).
    # For rows with cv == 0 but qty != 0 we fall back to LTP below.
    lf = lf.with_columns(
        qty_col.alias("_qty"),
        cv_col.alias("_cv"),
        ltp_col.alias("_ltp"),
        has_ltp_col.alias("_has_ltp"),
    ).filter(~((pl.col("_qty") == 0.0) & (pl.col("_cv") == 0.0)))

    if lf.is_empty():
        return 0.0, []

    # Four-way split:
    #   lf_good_cv   — cv != 0 AND last_price column present AND ltp > 0
    #                  → cur_val is a real market value; trust it
    #   lf_stale_ltp — cv != 0 AND last_price column present AND ltp <= 0
    #                  → cur_val is cost basis (Dhan/Groww cold cache);
    #                    route via ticker rescue; contribute 0 if no ticker
    #   lf_no_ltp_col — cv != 0 AND last_price column absent
    #                  → try ticker first (market value), else fall back to cv
    #   lf_zero_cv   — cv == 0 → existing ticker fallback path (unchanged)
    lf_good_cv = lf.filter(
        (pl.col("_cv") != 0.0) & pl.col("_has_ltp") & (pl.col("_ltp") > 0.0)
    )
    lf_stale_ltp = lf.filter(
        (pl.col("_cv") != 0.0) & pl.col("_has_ltp") & (pl.col("_ltp") <= 0.0)
    )
    lf_no_ltp_col = lf.filter(
        (pl.col("_cv") != 0.0) & ~pl.col("_has_ltp")
    )
    lf_zero_cv = lf.filter(pl.col("_cv") == 0.0)

    cv_sum = float(
        lf_good_cv.select(pl.col("_cv").sum()).to_series()[0] or 0.0
        if not lf_good_cv.is_empty() else 0.0
    )
    # stale-LTP and zero-cv need ticker rescue (contribute 0 if no ticker).
    lf_need_rescue = (
        pl.concat([lf_stale_ltp, lf_zero_cv])
        if not lf_stale_ltp.is_empty() and not lf_zero_cv.is_empty()
        else lf_stale_ltp if not lf_stale_ltp.is_empty()
        else lf_zero_cv
    )
    ltp_sum = _ltp_fallback_sum(lf_need_rescue, ticker)

    # No-ltp-col rows: ticker-first, then cv as fallback (not 0).
    no_ltp_col_sum = _ltp_or_cv_fallback_sum(lf_no_ltp_col, ticker)

    mtm = cv_sum + ltp_sum + no_ltp_col_sum

    accounts: list[str] = []
    if "account" in lf.columns:
        accounts = (
            lf.filter(
                pl.col("account").is_not_null()
                & (pl.col("account").cast(str) != "")
            )
            .select(pl.col("account").cast(str).unique())
            .to_series()
            .to_list()
        )

    return mtm, accounts


# Prefix marking an `errors` entry as UNDERSTATED — the leg genuinely
# contributed less than its real value this cycle (no LKG anywhere to
# freeze to), as opposed to an entry that's merely informational (e.g. an
# LKG-substituted account, whose contributed number IS correct, just
# slightly old). `compute_firm_nav()` filters on this prefix to build
# `snap["understated"]`, which is what `write_nav_snapshot()` gates its
# never-poison-a-clean-row check on — an informational-only degradation
# must never block a write (the number is trustworthy), while an
# understated leg always must (2026-09-27 council audit follow-up: an
# earlier version of this fix conflated both severities into the single
# `errors` list, which would have also blocked the write for the common
# "LKG worked fine" case).
_UNDERSTATED_TAG = "UNDERSTATED: "


def _substitute_degraded_frames(dfs: list, kind: str, errors: list[str]) -> list:
    """Detect per-account frames carrying a masked-failure shape
    (`attrs['fetch_failed']` — set by `broker_apis._fetch_*_local` on any
    per-account exception) and substitute the broker layer's own
    last-known-good frame in place of silently letting that account's NAV
    leg contribute a hard zero.

    This closes the exact gap CLAUDE.md's "Staleness indicator freeze
    rule" documents for `positions.py`/`holdings.py`
    (`_is_positions_outage` / `_accounts_flagged_stale`): those routes
    detect the SAME `attrs['fetch_failed']` shape on the SAME per-account
    DataFrame contract `broker_apis.fetch_positions()` /
    `fetch_holdings()` / `fetch_margins()` produce — `nav.py` previously
    checked neither the attrs NOR the resulting `errors` list, so a
    transient single-account broker exception (which returns an EMPTY
    frame with `fetch_failed=True`, not a raised exception — the outer
    per-phase try/except never sees it) silently zeroed that account's
    cash/position/holdings leg with no operator-visible signal.

    Reuses `broker_apis._stale_substitute_frame` — the SAME helper the
    circuit-breaker-open / Dhan-interval-skip paths already call to
    freeze to LKG — rather than inventing a second cache. Two outcomes:

      • LKG exists → the substituted frame carries the account's real
        (if slightly stale) last-known values; it is used exactly like a
        normal successful fetch, and an informational entry is still
        appended to `errors` so `write_nav_snapshot()`'s `note` records
        the degradation even though the number itself is trustworthy.
      • No LKG anywhere (fresh restart / >24h offline) → the substitute
        is itself an empty `fetch_failed=True` frame; there is genuinely
        no last-known-good value to freeze to, so (matching
        positions.py's equivalent "account silently drops out of the
        sum, but is flagged" behaviour) the account contributes 0 and an
        `_UNDERSTATED_TAG`-prefixed entry is appended to `errors` —
        `compute_firm_nav()` filters this prefix into `snap["understated"]`,
        which is what actually gates `write_nav_snapshot()`'s decision to
        skip the write (see `_UNDERSTATED_TAG`'s own docstring).

    VERIFIED reachability (2026-09-27 council audit, empirically confirmed
    — not just inferred): `margins` frames are never routed through
    `broker_apis._apply_backfill_to_list`'s concat (holdings/positions
    are), so every per-account frame's attrs survive intact through
    `fetch_margins()` for BOTH full- and partial-outage shapes — this
    function's detection is fully reliable for margins.

    `positions`/`holdings` reliably preserve attrs ONLY for the
    ALL-accounts-failed shape (the same shape `_is_positions_outage`
    depends on — `_apply_backfill_to_list` returns the original
    per-account list untouched when every frame is empty). In a MIXED
    success/one-account-failure result, `_apply_backfill_to_list` filters
    the failed account's EMPTY frame out of `non_empty` before concat —
    empirically confirmed: the failed account leaves ZERO trace (no row,
    no attrs) in the combined single-element list this function receives,
    so this detection never fires for that shape and the leg silently
    contributes 0 with no error recorded, exactly the original bug
    pattern. Two sub-cases:
      • `positions` has its own R1 in-process substitution
        (`_fetch_positions_local`'s except/None branches already call
        `_stale_substitute_frame` BEFORE returning) — when an LKG
        exists, the substituted frame is non-empty and survives the
        `non_empty` filter with correct values already inline, so this
        gap does NOT apply there. It only applies when NO LKG exists
        anywhere (the substitute itself comes back empty).
      • `holdings` has no equivalent in-process substitution at all
        (`_fetch_holdings_local`'s except/None branches just set
        `fetch_failed=True` on an empty frame, unlike positions) — EVERY
        single-account holdings failure is unreachable by this function,
        LKG-available or not.
    This is a `backend/brokers/broker_apis.py` (`_apply_backfill_to_list`)
    reachability gap, not a `nav.py` one — flagged for the broker-layer
    owner rather than patched here (out of this module's domain). Suggested
    fix: `_apply_backfill_to_list` should stash failed account codes in
    `combined.attrs` (e.g. `attrs['partial_outage']`, mirroring
    `positions.py:_positions_partial_outage_accounts`'s existing
    convention) before returning, so callers above it can still see what
    was silently dropped.
    """
    from backend.brokers.broker_apis import _stale_substitute_frame

    out: list = []
    for df in (dfs or []):
        attrs = getattr(df, "attrs", {}) or {}
        if not attrs.get("fetch_failed"):
            out.append(df)
            continue
        acct = attrs.get("account")
        if not acct and not df.empty and "account" in df.columns:
            acct = df["account"].iloc[0]
        acct = str(acct) if acct else None
        if not acct:
            errors.append(f"{_UNDERSTATED_TAG}{kind}: fetch failed for an unidentified account")
            out.append(df)
            continue
        sub = _stale_substitute_frame(kind, acct)
        if sub.attrs.get("fetch_failed"):
            errors.append(f"{_UNDERSTATED_TAG}{kind}: {acct} fetch failed, no last-known-good available")
        else:
            since = sub.attrs.get("stale_since")
            logger.warning(
                f"nav: {kind} degraded for {acct} — serving last-known-good"
                + (f" (since {since})" if since else "")
            )
            errors.append(f"{kind}: {acct} degraded — served last-known-good")
        out.append(sub)
    return out


def _merge_accounts(accounts_in: list[str], new_accts: list[str]) -> None:
    """Append unique non-empty account codes from new_accts into accounts_in in place."""
    for a in new_accts:
        if a and a not in accounts_in:
            accounts_in.append(a)


def _accumulate_by_account(df, out: dict[str, float], from_df_fn, *extra_args) -> None:
    """Accumulate per-account totals into `out` by filtering `df` to each
    unique account and re-running the already-tested `from_df_fn` (one of
    `_funds_from_df` / `_positions_from_df` / `_holdings_from_df`) on that
    single-account subset.

    Deliberately reuses the exact same vectorized aggregation logic the
    firm total uses (unrealised qty!=0 gating, ticker-rescue fallback for
    holdings, etc.) instead of a parallel per-account formula — this is
    what makes `compute_firm_nav()`'s `by_account` breakdown structurally
    incapable of drifting from its own firm total: `sum(by_account) ==
    firm_total` by construction, not by convention.
    """
    if df is None or df.empty or "account" not in df.columns:
        return
    for acct in df["account"].dropna().unique():
        acct_s = str(acct)
        if not acct_s or acct_s == "TOTAL":
            continue
        sub = df[df["account"] == acct]
        chunk, _ = from_df_fn(sub, *extra_args)
        out[acct_s] = out.get(acct_s, 0.0) + chunk


async def _resolve_conn_keys() -> list[str]:
    """Return known broker account keys, falling back to conn_service when local is empty."""
    from backend.brokers.connections import Connections
    keys = list(Connections().conn.keys())
    if not keys:
        from backend.brokers.client import is_cutover_on
        if is_cutover_on():
            from backend.brokers.client.remote_broker import list_remote_accounts
            keys = [r["account"] for r in list_remote_accounts() if r.get("account")]
    return keys


def _recover_missing_margins_accounts(
    expected_accounts: Optional[list[str]], seen: set,
    accounts_in: list[str], errors: list[str],
    by_account: Optional[dict[str, float]],
) -> float:
    """Diff `expected_accounts` (every configured broker account) against
    `seen` (accounts that actually produced a margins row this phase) and
    attempt an LKG recovery for each gap.

    Exists because margins is the ONE broker-fetch shape where an
    account's contribution being silently absent is otherwise
    UNDETECTABLE by attrs alone. `_substitute_degraded_frames` (attrs-
    based) is fully reliable for margins in the SAME-process case
    (`RAMBOQ_USE_CONN_SERVICE` unset) — margins never passes through
    `_apply_backfill_to_list`'s concat, so per-account `fetch_failed`
    attrs survive intact. But under `RAMBOQ_USE_CONN_SERVICE=1` (prod),
    an empty `fetch_failed=True` per-account frame crosses the
    conn_service UDS boundary via `conn_sync.fetch_margins()`, and
    `DataFrame.attrs` is NOT guaranteed to survive that RPC
    serialization — this check is deliberately boundary-agnostic: it
    works from expected-vs-actual ACCOUNT PRESENCE instead of attrs, so
    it closes the gap regardless of whether attrs made it across. The
    `_peek("funds")` cached-closed-hours branch has the identical hole
    (a no-LKG account is simply absent from `cached_funds.rows`, with
    no `stale_accounts` entry either) — this helper covers both call
    sites.

    Every configured margins account always produces exactly one row on
    a healthy fetch (unlike positions/holdings, which can legitimately
    be flat/empty) — so "expected but never seen" unambiguously means a
    masked failure, never a legitimate empty state. No-op when
    `expected_accounts` is falsy (caller didn't resolve the account
    list — e.g. existing tests/call sites that predate this check).
    """
    if not expected_accounts:
        return 0.0
    missing = sorted(set(expected_accounts) - seen - {"TOTAL"})
    if not missing:
        return 0.0
    from backend.brokers.broker_apis import _stale_substitute_frame
    total = 0.0
    for acct in missing:
        sub = _stale_substitute_frame("margins", acct)
        if sub.empty or sub.attrs.get("fetch_failed"):
            errors.append(
                f"{_UNDERSTATED_TAG}margins: {acct} missing from fetch result, "
                f"no last-known-good available"
            )
            continue
        chunk, accts = _funds_from_df(sub)
        total += chunk
        _merge_accounts(accounts_in, accts)
        if by_account is not None:
            _accumulate_by_account(sub, by_account, _funds_from_df)
        errors.append(f"margins: {acct} degraded — served last-known-good")
    return total


def _fetch_funds_from_cache(
    cached_funds, accounts_in: list[str], errors: list[str],
    by_account: Optional[dict[str, float]],
    expected_accounts: Optional[list[str]],
) -> Optional[float]:
    """Extract cash_total from an already-cached FundsResponse (the
    closed-hours branch of `_fetch_funds_phase`). Returns None on any
    internal failure so the caller falls through to the live broker path
    — mirrors the original inline try/except exactly, just extracted to
    keep `_fetch_funds_phase`'s cyclomatic complexity under the project's
    D-grade gate.
    """
    try:
        total = 0.0
        accts: list[str] = []
        for row in (cached_funds.rows or []):
            acct = str(getattr(row, "account", "") or "")
            if acct == "TOTAL":
                continue
            _raw_cash = getattr(row, "cash", None)
            cash = float(_raw_cash or 0)
            premium = float(getattr(row, "option_premium", 0) or 0)
            total += cash + premium
            if acct:
                accts.append(acct)
                if by_account is not None:
                    by_account[acct] = by_account.get(acct, 0.0) + cash + premium
                # 2026-10 audit fix — sibling to the broker-path check in
                # `_fetch_funds_from_broker`: a FundsRow.cash of None
                # (the missing-vs-zero convention — see CLAUDE.md) means
                # the cached route itself never resolved a cash figure
                # for this account, not a confirmed real ₹0. Deliberately
                # NOT applied to `premium` — see `_funds_null_cash_accounts`.
                if _raw_cash is None:
                    errors.append(
                        f"{_UNDERSTATED_TAG}funds: {acct} cash figure "
                        f"missing (cached)"
                    )
        _merge_accounts(accounts_in, accts)
        _stale_accts = getattr(cached_funds, "stale_accounts", None)
        if isinstance(_stale_accts, (list, tuple, set)) and _stale_accts:
            # The funds route already froze these accounts to its own LKG
            # (see funds.py's stale_since_map) — values above are real (if
            # slightly old). Surface it (informational only — NOT
            # `_UNDERSTATED_TAG`-prefixed) so write_nav_snapshot()'s note
            # captures the degradation without skipping the write (see
            # `_UNDERSTATED_TAG`'s docstring for why a "degraded but has
            # real values" entry never blocks, only a genuinely
            # understated one does).
            errors.append(
                "funds: stale (cached) for "
                + ", ".join(sorted(str(a) for a in _stale_accts))
            )
        total += _recover_missing_margins_accounts(
            expected_accounts, set(accts), accounts_in, errors, by_account,
        )
        return total
    except Exception as e:
        logger.warning(f"nav: cached funds extraction failed ({e}) — falling through to broker")
        return None


async def _fetch_funds_from_broker(
    accounts_in: list[str], errors: list[str],
    by_account: Optional[dict[str, float]],
    expected_accounts: Optional[list[str]],
) -> float:
    """Live broker margins fetch + LKG substitution + missing-account
    recovery. Extracted from `_fetch_funds_phase` to keep its own
    cyclomatic complexity under the project's D-grade gate.
    """
    from backend.brokers.broker_apis import fetch_margins
    try:
        funds_dfs = await asyncio.to_thread(fetch_margins)
        funds_dfs = _substitute_degraded_frames(funds_dfs, "margins", errors)
        total = 0.0
        # `attempted` (distinct from `seen`/`accts`, which only tracks
        # accounts that actually CONTRIBUTED a row) also counts an
        # account whose frame carries `attrs['account']` even when
        # EMPTY/failed — `_substitute_degraded_frames` already handled
        # and reported that shape, so `_recover_missing_margins_accounts`
        # must not re-flag it a second time. Only accounts with ZERO
        # trace anywhere (the UDS-attrs-lost shape) should reach the
        # recovery helper.
        attempted: set = set()
        for df in funds_dfs or []:
            _df_attrs = getattr(df, "attrs", {}) or {}
            if _df_attrs.get("account"):
                attempted.add(str(_df_attrs["account"]))
            chunk, accts = _funds_from_df(df)
            total += chunk
            attempted.update(accts)
            _merge_accounts(accounts_in, accts)
            if by_account is not None:
                _accumulate_by_account(df, by_account, _funds_from_df)
            # 2026-10 audit fix: `_funds_from_df` fills a missing cash
            # figure to 0.0 for arithmetic — tag it UNDERSTATED here so a
            # genuine broker-side gap (e.g. Dhan's sodLimit key absent
            # from a response that otherwise succeeded) defers the NAV
            # write instead of silently landing as a confirmed ₹0. See
            # `_funds_null_cash_accounts`'s docstring for why this is
            # scoped to cash only, not option_premium.
            for _null_acct in _funds_null_cash_accounts(df):
                errors.append(
                    f"{_UNDERSTATED_TAG}funds: {_null_acct} cash figure "
                    f"missing from broker response"
                )
        total += _recover_missing_margins_accounts(
            expected_accounts, attempted, accounts_in, errors, by_account,
        )
        return total
    except Exception as e:
        # Whole-leg failure (not a single account) — total is genuinely
        # understated (0.0), not merely degraded-but-frozen.
        errors.append(f"{_UNDERSTATED_TAG}funds: {e}")
        return 0.0


async def _fetch_funds_phase(
    accounts_in: list[str], errors: list[str],
    by_account: Optional[dict[str, float]] = None,
    expected_accounts: Optional[list[str]] = None,
) -> float:
    """Fetch margin data and return cash_total; mutates accounts_in and errors.

    Market-hours gate: when both segments are closed and the funds route has a
    cached FundsResponse, extract cash_total from that without calling the broker.
    This keeps the NAV cash term consistent with what the funds route itself returns
    and avoids stale pre-settlement broker responses during the W3/W4/W5 windows.

    `by_account`, when passed, is mutated in place with each account's cash
    leg (cash + option_premium) — optional so existing callers/tests that
    only need the firm total are unaffected.

    `expected_accounts`, when passed, drives
    `_recover_missing_margins_accounts` — see its docstring for why
    margins needs this boundary-agnostic (attrs-independent) check.
    """
    from backend.api.helpers.snapshot_gate import _any_segment_open
    from backend.api.cache import peek as _peek

    mkt_open: bool = await asyncio.to_thread(_any_segment_open)
    if not mkt_open:
        cached_funds = _peek("funds")
        if cached_funds is not None:
            total = _fetch_funds_from_cache(
                cached_funds, accounts_in, errors, by_account, expected_accounts,
            )
            if total is not None:
                return total

    return await _fetch_funds_from_broker(
        accounts_in, errors, by_account, expected_accounts,
    )


async def _fetch_positions_phase(
    accounts_in: list[str], errors: list[str],
    by_account: Optional[dict[str, float]] = None,
) -> float:
    """Fetch positions data and return positions_mtm; mutates accounts_in and errors.

    `by_account`, when passed, is mutated in place with each account's
    position M2M leg (unrealised[qty!=0] + realised, per `_positions_from_df`).
    """
    from backend.brokers.broker_apis import fetch_positions
    try:
        pos_dfs = await asyncio.to_thread(fetch_positions)
        pos_dfs = _substitute_degraded_frames(pos_dfs, "positions", errors)
        total = 0.0
        for df in pos_dfs or []:
            chunk, accts = _positions_from_df(df)
            total += chunk
            _merge_accounts(accounts_in, accts)
            if by_account is not None:
                _accumulate_by_account(df, by_account, _positions_from_df)
        return total
    except Exception as e:
        errors.append(f"{_UNDERSTATED_TAG}positions: {e}")
        return 0.0


async def _fetch_holdings_from_snapshot(
    accounts_in: list[str], errors: list[str],
    by_account: Optional[dict[str, float]] = None,
) -> float:
    """Compute holdings_mtm when NSE is closed using the holdings route SSOT.

    Delegates to _holdings_snapshot() from holdings.py — the exact same path
    that GET /api/holdings uses in its closed-hours branch. Ensures firm NAV
    and the holdings grid cur_val always agree (true SSOT, no duplicate SQL).
    """
    from backend.api.routes.holdings import _holdings_snapshot
    try:
        snap = await _holdings_snapshot()
        if snap is None:
            # `_holdings_snapshot()` returns None for BOTH "no snapshot
            # exists yet" (legitimately empty) AND "the DB query failed"
            # (per its own docstring — `_query_holdings_snapshot_rows()`
            # catches its own exceptions and returns None) — the two are
            # conflated at the source, indistinguishable here. Flagged
            # via `errors` for visibility, but deliberately NOT tagged
            # `_UNDERSTATED_TAG`:
            #   1. Tagging it would make write_nav_snapshot() skip EVERY
            #      day for a genuinely zero-holdings firm/account
            #      (harmless empty book), not just genuine failures —
            #      the false-positive rate would be too high given the
            #      two cases are conflated.
            #   2. A DB that's actually down at the 23:45 IST write
            #      moment will ALSO fail the write itself (`s.execute`
            #      below raises), which DOES propagate as an exception
            #      out of `write_nav_snapshot()` and hits the existing,
            #      well-tested exception-based retry path in
            #      `_run_nav_compute_once` (background.py pops the
            #      `nav_done` latch on any exception already) — so a
            #      real DB outage still self-heals via that path even
            #      without tagging this specific read as understated.
            errors.append("holdings_snapshot: no snapshot available")
            return 0.0
        total = 0.0
        for row in (snap.rows or []):
            cur = float(getattr(row, "cur_val", 0) or 0)
            total += cur
            acct = str(getattr(row, "account", "") or "")
            _merge_accounts(accounts_in, [acct] if acct else [])
            if by_account is not None and acct:
                by_account[acct] = by_account.get(acct, 0.0) + cur
        return total
    except Exception as exc:
        errors.append(f"{_UNDERSTATED_TAG}holdings_snapshot: {exc}")
        return 0.0


async def _fetch_holdings_phase(
    accounts_in: list[str], errors: list[str], ticker,
    by_account: Optional[dict[str, float]] = None,
) -> float:
    """Fetch holdings data and return holdings_mtm; mutates accounts_in and errors.

    NSE closed → _fetch_holdings_from_snapshot (daily_book via _holdings_snapshot SSOT).
    NSE open   → live broker fetch; cur_val is current during the session.

    `by_account` propagates through both branches — mutated in place with
    each account's holdings M2M leg when passed.
    """
    from backend.api.helpers.snapshot_gate import is_exchange_closed_now
    try:
        if is_exchange_closed_now("NSE"):
            # Positional-arg call preserved exactly (no by_account) when the
            # caller didn't ask for a breakdown — keeps existing call-site
            # assertions (accounts_in, errors) intact in tests.
            if by_account is not None:
                return await _fetch_holdings_from_snapshot(accounts_in, errors, by_account)
            return await _fetch_holdings_from_snapshot(accounts_in, errors)

        from backend.brokers.broker_apis import fetch_holdings
        hold_dfs = await asyncio.to_thread(fetch_holdings)
        hold_dfs = _substitute_degraded_frames(hold_dfs, "holdings", errors)
        total = 0.0
        for df in hold_dfs or []:
            chunk, accts = _holdings_from_df(df, ticker)
            total += chunk
            _merge_accounts(accounts_in, accts)
            if by_account is not None:
                _accumulate_by_account(df, by_account, _holdings_from_df, ticker)
        return total
    except Exception as e:
        errors.append(f"{_UNDERSTATED_TAG}holdings: {e}")
        return 0.0


async def compute_firm_nav() -> dict:
    """Return today's NAV plus the breakdown components — firm-level AND
    per-account.

    Shape:
        {
          "nav":             float,
          "cash_total":      float,
          "positions_mtm":   float,
          "holdings_mtm":    float,
          "accounts":        list[str],   # which broker codes contributed
          "errors":          list[str],   # per-account failures (non-blocking)
          "by_account": {
              "<account_code>": {
                  "cash": float, "pos_m2m": float,
                  "holdings_mtm": float, "nav": float,
              }, ...
          },
        }

    Each broker-account call is wrapped in its own try/except so a
    single offline broker doesn't break the whole snapshot. The
    `errors` list surfaces what was excluded; the `accounts` list
    is the inverse (what WAS included).

    `by_account` is the SSOT for every per-account NAV consumer
    (PerformancePage's NAV grid via `GET /api/nav/by-account`, and — by
    extension — the dashboard chip and NavCard, which all read the same
    `compute_firm_nav()` call). It is built by
    `_accumulate_by_account()` re-running the SAME per-account-filtered
    `_funds_from_df` / `_positions_from_df` / `_holdings_from_df` calls
    the firm total uses, so `sum(row.nav for row in by_account.values())
    == nav` holds by construction (module-level invariant test:
    `backend/tests/test_nav_by_account.py`). This replaces the parallel
    client-side formula that used to live in
    `frontend/src/lib/data/nav.js` (`navRowForAccount` / `navByAccount`,
    removed 2026-09) — that formula had already drifted from this one
    (missing the `realised` term, no unrealised qty!=0 gate, no holdings
    ticker-rescue fallback).

    Vectorized via Polars — each per-account DataFrame is converted
    once with pl.from_pandas() and aggregated with .sum() expressions
    instead of .iterrows() (~50-100× faster on typical 5-account frames).
    """
    from backend.brokers.kite_ticker import get_ticker as _get_ticker

    _ticker = _get_ticker()
    accounts_in: list[str] = []
    errors: list[str] = []
    cash_by_acct: dict[str, float] = {}
    pos_by_acct: dict[str, float] = {}
    hold_by_acct: dict[str, float] = {}

    # 2026-09-27 council audit: previously called for its registry-
    # populating side-effect only and the result discarded — now also
    # passed to _fetch_funds_phase as the "every configured account
    # should have produced a margins row" expected set (see
    # _recover_missing_margins_accounts's docstring for why margins
    # specifically needs this boundary-agnostic expected-vs-actual
    # check rather than relying on attrs alone).
    expected_accounts = await _resolve_conn_keys()
    # Fire all three broker phases concurrently instead of sequentially —
    # each phase does its own broker round-trip (funds/positions/holdings),
    # and running them one-after-another made compute_firm_nav() pay the
    # SUM of three round-trips on a cold cache, while the frontend's own
    # separate funds/positions/holdings requests fetch in parallel and
    # only pay one each. Operator: "loading firm nav takes more time in
    # performance page. the other grid elements are loaded fast."
    # Safe to parallelize: each phase mutates its own by-account dict
    # (cash_by_acct/pos_by_acct/hold_by_acct); accounts_in/errors are
    # shared lists appended to by all three, but asyncio's single-threaded
    # cooperative scheduling makes list.append/_merge_accounts's
    # membership-check-then-append atomic between await points — no true
    # preemption occurs mid-statement.
    cash_total, positions_mtm, holdings_mtm = await asyncio.gather(
        _fetch_funds_phase(accounts_in, errors, cash_by_acct, expected_accounts),
        _fetch_positions_phase(accounts_in, errors, pos_by_acct),
        _fetch_holdings_phase(accounts_in, errors, _ticker, hold_by_acct),
    )

    nav = cash_total + positions_mtm + holdings_mtm

    by_account: dict[str, dict[str, float]] = {}
    for acct in accounts_in:
        c = cash_by_acct.get(acct, 0.0)
        p = pos_by_acct.get(acct, 0.0)
        h = hold_by_acct.get(acct, 0.0)
        by_account[acct] = {
            "cash": round(c, 2),
            "pos_m2m": round(p, 2),
            "holdings_mtm": round(h, 2),
            "nav": round(c + p + h, 2),
        }

    # `understated` (2026-09-27 council audit follow-up) — the subset of
    # `errors` where a leg genuinely contributed LESS than its real value
    # this cycle (no LKG anywhere to freeze to). `errors` as a whole stays
    # the broader "something was degraded" signal (drives `note` + the
    # public route's masking + auth.py's `stale`); `understated` is the
    # narrower "the NUMBER is actually wrong" signal `write_nav_snapshot`
    # gates its never-poison-a-clean-row check on. An LKG-substituted
    # account (real, if slightly old, values) is intentionally NOT in
    # this list — see `_UNDERSTATED_TAG`'s docstring.
    understated = [e for e in errors if e.startswith(_UNDERSTATED_TAG)]

    return {
        "nav": round(nav, 2),
        "cash_total": round(cash_total, 2),           # = Σ (cash_sod + option_premium)
        "positions_mtm": round(positions_mtm, 2),     # = Σ position.unrealised
        "holdings_mtm": round(holdings_mtm, 2),       # = Σ holding.cur_val
        "accounts": sorted(accounts_in),
        "errors": errors,
        "understated": understated,
        "by_account": by_account,
    }


async def write_nav_snapshot(
    target_date: Optional[date] = None, force: bool = False,
) -> dict:
    """Compute today's NAV and write it to `nav_daily` (upsert).
    Returns the snapshot dict + the row id.

    Idempotent — same `as_of_date` re-writes the existing row (e.g.
    operator triggers a recompute mid-day after an outage clears).

    Never-poison-persisted-storage guard (mirrors CLAUDE.md's "Staleness
    indicator freeze rule" — the positions/holdings frontend cache
    invariant "any response carrying stale_accounts... is never written
    to Tier 2 [localStorage]... to prevent poisoning the cache with a
    masked-failure value", applied here to `nav_daily`, the persisted
    analog).

    Gates purely on `snap["understated"]` (NOT the broader `errors`):
    an LKG-substituted account's contributed number is correct (if
    slightly old) — that degradation is informational only (still
    recorded in `note`) and must NOT block the write. An understated
    leg (no LKG anywhere — the number is actually wrong, not just old)
    skips the write UNLESS `force=True`, even for the day's first-ever
    snapshot — 2026-09-27 council audit follow-up: the scheduled 23:45
    IST write is usually the day's ONLY write, so a narrower "only skip
    if it would overwrite an already-clean row" guard never engages for
    that case, letting a permanently-understated row land as
    investor-facing history with only an easy-to-miss `note` marking it.
    A MISSING `nav_daily` row for a day is operator-visible (a gap in
    the NAV history chart/table); an understated one silently looks
    like a real, if bad, day.

    `force=True` (2026-09-27 council audit, second-pass refinement):
    the retry window for a skipped write is short and finite —
    `_run_nav_compute_once` (background.py) only retries between the
    scheduled fire time (~23:45 IST) and IST midnight (`now.time() <
    target`'s gate stops firing once `today` rolls over), and there is
    no backfill path (`POST /compute` always targets TODAY's live
    broker state, never a past date). A single account stuck offline
    all evening (documented Dhan margins flakiness — see
    `_MARGINS_SSOT_TTL`'s comment) would otherwise mean this trading
    day NEVER gets a `nav_daily` row at all — permanently, not just
    delayed — which breaks unit pricing (units × nav_per_unit) and the
    8-year SEBI audit history worse than a visibly-marked understated
    row would. `_run_nav_compute_once` passes `force=True` once the
    clock passes `target + _NAV_FORCE_GRACE` (NOT a fixed IST-midnight-
    adjacent time — see that constant's own docstring for why it must
    be relative to `target`) so a degraded-but-present row always beats
    a permanent gap, while every earlier attempt still gets the full
    skip-and-retry protection.

    OPERATOR POLICY DECISION (2026-09-27, confirmed): a forced write
    (`force=True` with `understated` legs) NEVER downgrades an
    already-good row. Before writing, this function checks whether a
    `nav_daily` row already exists for `target` — if one does (e.g. an
    earlier cycle that day computed cleanly, or an interim NSE-close
    snapshot), the forced write is skipped entirely and that existing
    row is left untouched, rather than being overwritten by a later,
    worse (understated) number from an account that broke afterward.
    Forcing only ever fills a genuine GAP (no row for `target` yet), it
    never regresses one. An account that stays broken with no
    last-known-good available (e.g. an expired credential), on a day
    with no earlier clean row, still gets a FORCED, clearly-labelled
    (`note` contains "UNDERSTATED" / "FORCED") understated row rather
    than the day having none at all — that residual case is accepted
    as strictly better than a permanent gap. `_NAV_FORCE_GRACE`
    (background.py) is a module constant the operator may want tuned
    (or exposed via `/admin/settings`) depending on how much protection
    window vs. guaranteed-daily-row they prefer.

    Returns `snap` with `snap["skipped_write"] = True` added when the
    write was skipped (only possible when NOT forced) —
    `_run_nav_compute_once` checks this to pop its once-per-day latch
    so the next 30s poll retries instead of considering the day done.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from backend.api.database import async_session
    from backend.api.models import NavDaily
    from backend.shared.helpers.date_time_utils import timestamp_indian

    snap = await compute_firm_nav()
    target = target_date or timestamp_indian().date()

    note = None
    if snap["errors"]:
        note = "errors: " + " | ".join(snap["errors"])[:500]

    if snap.get("understated") and not force:
        logger.warning(
            f"nav_daily: SKIPPED write for {target.isoformat()} — understated "
            f"legs (no last-known-good available): "
            f"{' | '.join(snap['understated'])[:400]}"
        )
        return {**snap, "skipped_write": True}

    if snap.get("understated") and force:
        from sqlalchemy import select

        async with async_session() as _chk:
            _existing_id = await _chk.scalar(
                select(NavDaily.id).where(NavDaily.as_of_date == target)
            )
        if _existing_id is not None:
            # A row for today already exists (an earlier clean cycle, or an
            # interim snapshot) — never downgrade it with a later, worse
            # understated number. Forcing only ever fills a genuine gap.
            logger.warning(
                f"nav_daily: SKIPPED forced write for {target.isoformat()} — "
                f"a row already exists for today; never downgrade an "
                f"existing good/interim snapshot with an understated one: "
                f"{' | '.join(snap['understated'])[:400]}"
            )
            return {**snap, "skipped_write": True}
        logger.warning(
            f"nav_daily: FORCED write for {target.isoformat()} after the "
            f"retry grace period despite understated legs — no row exists "
            f"for today yet, so a degraded row beats a permanent gap: "
            f"{' | '.join(snap['understated'])[:400]}"
        )
        note = (note or "") + " [FORCED after retry grace — value UNDERSTATED]"

    async with async_session() as s:
        stmt = pg_insert(NavDaily).values(
            as_of_date=target,
            nav=snap["nav"],
            cash_total=snap["cash_total"],
            positions_mtm=snap["positions_mtm"],
            holdings_mtm=snap["holdings_mtm"],
            accounts_snapshot=snap["accounts"],
            note=note,
        ).on_conflict_do_update(
            index_elements=["as_of_date"],
            set_=dict(
                nav=snap["nav"],
                cash_total=snap["cash_total"],
                positions_mtm=snap["positions_mtm"],
                holdings_mtm=snap["holdings_mtm"],
                accounts_snapshot=snap["accounts"],
                note=note,
            ),
        )
        await s.execute(stmt)
        await s.commit()
    logger.info(
        f"nav_daily: wrote NAV ₹{snap['nav']:,.0f} for {target.isoformat()} "
        f"(cash ₹{snap['cash_total']:,.0f} + pos ₹{snap['positions_mtm']:,.0f} "
        f"+ hold ₹{snap['holdings_mtm']:,.0f}, accts={len(snap['accounts'])}, "
        f"errors={len(snap['errors'])})"
    )
    return snap
