"""
Tests for `option_underlying_quote_key()`'s MCX future resolution
(backend/api/algo/derivatives.py) — the bi-monthly-cycle fix.

Live incident (2026-11-20): GOLD options expire monthly on MCX, but GOLD
futures trade only on a bi-monthly cycle (Dec/Feb/Apr/Jun/Aug/Oct — no
November contract). `futures_symbol_for_expiry()` blindly built the
same-month symbol "GOLD26NOVFUT", which doesn't exist, so
`quote(['MCX:GOLD26NOVFUT'])` returned `{}` and every GOLD position's
`underlying_ltp` stayed 0 — collapsing Greeks and the Payoff chart for
every GOLD leg that month. Confirmed live against the real instruments
cache and a real quote() call on prod before the fix.

The fix (`_mcx_nearest_future_symbol`) falls back, via a sync peek of
the instruments cache, to the nearest real listed future expiring on or
after the option's own expiry — mirroring the async
`lookup_mcx_future_for_expiry()` rule used elsewhere. It must be a
no-op for commodities that DO trade every month (CRUDEOIL,
NATURALGAS), and must degrade gracefully on a cold cache.

Performance follow-up (2026-11-21): `_mcx_nearest_future_symbol()` is
called once per MCX option row on every positions poll, and the
original fix paid the full cost of scanning every instrument in the
~156K-row instruments cache even for CRUDEOIL/NATURALGAS, which never
needed the bi-monthly fallback at all — their naive same-month symbol
always exists. `_MCX_VERIFIED_MONTHLY_FUTURES` (confirmed against a
live Kite instruments dump) short-circuits BEFORE the cache is ever
peeked for those roots, while GOLD/SILVER and every other
unverified MCX root still run the full scan unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from backend.api.algo.derivatives import (
    _mcx_nearest_future_symbol,
    option_underlying_quote_key,
)


def _make_fut(sym: str, underlying: str, expiry: str) -> SimpleNamespace:
    return SimpleNamespace(s=sym, u=underlying, e="MCX", t="FUT", x=expiry)


# Real GOLD futures cycle confirmed live on prod 2026-11-20: Dec/Feb/Apr/
# Jun/Aug/Oct only — no November contract.
_GOLD_FUTS = [
    _make_fut("GOLD26DECFUT", "GOLD", "2026-12-04"),
    _make_fut("GOLD27FEBFUT", "GOLD", "2027-02-05"),
    _make_fut("GOLD27APRFUT", "GOLD", "2027-04-05"),
]

# CRUDEOIL trades every month — the naive same-month symbol always exists.
_CRUDEOIL_FUTS = [
    _make_fut("CRUDEOIL26NOVFUT", "CRUDEOIL", "2026-11-19"),
    _make_fut("CRUDEOIL26DECFUT", "CRUDEOIL", "2026-12-19"),
]


def _fake_cache_response(items):
    return SimpleNamespace(items=items)


def test_gold_november_option_resolves_to_real_december_future():
    """The reported bug: a November GOLD option must NOT resolve to the
    nonexistent 'GOLD26NOVFUT'. It should roll to the nearest real
    listed future (December)."""
    with patch("backend.api.cache.peek_stale",
               return_value=_fake_cache_response(_GOLD_FUTS)):
        key = option_underlying_quote_key("GOLD26NOV130000PE")
    assert key == "MCX:GOLD26DECFUT"
    assert "GOLD26NOVFUT" not in (key or "")


def test_gold_put_and_call_same_strike_month_agree():
    with patch("backend.api.cache.peek_stale",
               return_value=_fake_cache_response(_GOLD_FUTS)):
        put_key = option_underlying_quote_key("GOLD26NOV125000PE")
        call_key = option_underlying_quote_key("GOLD26NOV125000CE")
    assert put_key == call_key == "MCX:GOLD26DECFUT"


def test_crudeoil_monthly_cycle_is_a_no_op():
    """CRUDEOIL has a future every month — the naive same-month symbol
    exists in the cache, so resolution must be unchanged (no regression
    for commodities that already worked)."""
    with patch("backend.api.cache.peek_stale",
               return_value=_fake_cache_response(_CRUDEOIL_FUTS)):
        key = option_underlying_quote_key("CRUDEOIL26NOV5800CE")
    assert key == "MCX:CRUDEOIL26NOVFUT"


def test_cold_instruments_cache_falls_back_to_naive_symbol():
    """No regression when the instruments cache has never been warmed —
    same degraded behaviour as before this fix, not a crash."""
    with patch("backend.api.cache.peek_stale", return_value=None):
        key = option_underlying_quote_key("GOLD26NOV130000PE")
    assert key == "MCX:GOLD26NOVFUT"


def test_no_futures_listed_for_underlying_falls_back_to_naive_symbol():
    with patch("backend.api.cache.peek_stale",
               return_value=_fake_cache_response(_CRUDEOIL_FUTS)):
        key = option_underlying_quote_key("GOLD26NOV130000PE")
    assert key == "MCX:GOLD26NOVFUT"


def test_all_candidates_expire_before_option_falls_back_to_farthest_listed():
    """Defensive edge case: every listed future for this root expires
    before the option's own expiry (stale/cold cache lag). Must return
    the farthest-out real contract rather than a dead symbol."""
    stale_futs = [
        _make_fut("GOLD25DECFUT", "GOLD", "2025-12-04"),
        _make_fut("GOLD26FEBFUT", "GOLD", "2026-02-05"),
    ]
    with patch("backend.api.cache.peek_stale",
               return_value=_fake_cache_response(stale_futs)):
        key = option_underlying_quote_key("GOLD26NOV130000PE")
    assert key == "MCX:GOLD26FEBFUT"


# ── Performance fix (2026-11-21): verified-monthly roots never scan ───────

def test_crudeoil_never_peeks_instruments_cache():
    """CRUDEOIL is in `_MCX_VERIFIED_MONTHLY_FUTURES` — the naive
    same-month symbol is always correct, so the function must return it
    without ever touching the (potentially ~156K-row) instruments cache."""
    with patch("backend.api.cache.peek_stale") as mock_peek:
        key = option_underlying_quote_key("CRUDEOIL26NOV5800CE")
    assert key == "MCX:CRUDEOIL26NOVFUT"
    mock_peek.assert_not_called()


def test_crudeoilm_never_peeks_instruments_cache():
    with patch("backend.api.cache.peek_stale") as mock_peek:
        key = option_underlying_quote_key("CRUDEOILM26NOV5800CE")
    assert key == "MCX:CRUDEOILM26NOVFUT"
    mock_peek.assert_not_called()


def test_naturalgas_never_peeks_instruments_cache():
    """NATURALGAS trades every month, like CRUDEOIL — same no-scan
    fast path."""
    with patch("backend.api.cache.peek_stale") as mock_peek:
        key = option_underlying_quote_key("NATURALGAS26NOV250CE")
    assert key == "MCX:NATURALGAS26NOVFUT"
    mock_peek.assert_not_called()


def test_gold_still_peeks_instruments_cache_and_resolves_correctly():
    """GOLD is NOT in the verified-monthly skip-list (it's the bi-monthly
    case the original fix exists for) — it must still consult the
    instruments cache and still correctly roll to the nearest real
    listed future. This is the regression guard for the perf fix: the
    skip-list must never affect GOLD's own resolution."""
    with patch("backend.api.cache.peek_stale",
               return_value=_fake_cache_response(_GOLD_FUTS)) as mock_peek:
        key = option_underlying_quote_key("GOLD26NOV130000PE")
    assert key == "MCX:GOLD26DECFUT"
    mock_peek.assert_called_once()


def test_unverified_root_with_full_monthly_listing_still_scans():
    """Fail-safe direction check: a root NOT in the verified-monthly
    skip-list must still run the full scan even when its naive symbol
    turns out to be present — correctness over speed for anything not
    explicitly verified. (Uses a synthetic root/cache fixture, not a
    real MCX commodity, purely to prove the scan path is exercised.)"""
    monthly_futs = [
        _make_fut("ZINC26NOVFUT", "ZINC", "2026-11-25"),
        _make_fut("ZINC26DECFUT", "ZINC", "2026-12-25"),
    ]
    with patch("backend.api.cache.peek_stale",
               return_value=_fake_cache_response(monthly_futs)) as mock_peek:
        key = option_underlying_quote_key("ZINC26NOV250CE")
    assert key == "MCX:ZINC26NOVFUT"
    mock_peek.assert_called_once()


def test_mcx_nearest_future_symbol_skip_list_contents():
    """Pin the exact skip-list — catches an accidental future addition
    (e.g. GOLD/SILVER) without a corresponding live-data verification."""
    from backend.api.algo.derivatives import _MCX_VERIFIED_MONTHLY_FUTURES
    assert _MCX_VERIFIED_MONTHLY_FUTURES == {"CRUDEOIL", "CRUDEOILM", "NATURALGAS"}
    assert "GOLD" not in _MCX_VERIFIED_MONTHLY_FUTURES
    assert "SILVER" not in _MCX_VERIFIED_MONTHLY_FUTURES


def test_direct_call_crudeoil_lowercase_underlying_still_skips_scan():
    """`_mcx_nearest_future_symbol()` called directly (not through
    `option_underlying_quote_key`) — confirms the skip-list match is
    case-insensitive and that the cache is never touched."""
    import datetime as _dt
    with patch("backend.api.cache.peek_stale") as mock_peek:
        sym = _mcx_nearest_future_symbol("crudeoil", _dt.date(2026, 11, 25))
    assert sym == "CRUDEOIL26NOVFUT"
    mock_peek.assert_not_called()
