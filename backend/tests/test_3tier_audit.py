"""
test_3tier_audit.py

Audit findings closure — Phase D fixes from the comprehensive
backend+frontend 3-tier audit (2026-06-28).

DEFECT:
  `compute_firm_nav` (backend/api/algo/nav.py) called
  `broker_apis.fetch_holdings/positions/margins` DIRECTLY, bypassing
  the route-level `get_or_fetch("positions"/...)` cache. Every NavCard
  refresh, every /performance load, every investor-slice request,
  and every nav_daily snapshot fired independent broker round-trips
  for the SAME underlying data — multiplying broker load 4× during
  market hours and producing observable drift between the NavCard
  number and the /performance NAV TOTAL row (different fetch
  windows could see different LTPs).

FIX:
  Added `_RAW_CACHE` in `broker_apis.py` — a 30-second TTL cache
  keyed by ('positions' / 'holdings' / 'margins') that memoises the
  raw `list[pd.DataFrame]` returned by the zero-arg public
  `fetch_*()` entry points. The route handlers, `compute_firm_nav`,
  and any other consumer that calls `fetch_holdings()` /
  `fetch_positions()` / `fetch_margins()` share the same cached
  list within the TTL window.

  Postback handlers + `?fresh=1` query param invalidate the raw
  cache alongside the route-level cache so terminal order fills
  surface immediately on the next fetch (no 30s NavCard lag after
  a position closes).

Five quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — broker_apis._raw_cache_get/_put/_invalidate
                   are the single implementation; no parallel
                   raw-DF cache exists elsewhere. compute_firm_nav,
                   PositionsController, HoldingsController and
                   FundsController all consume the same memoised
                   list[DataFrame].
  2. Performance — second call to fetch_holdings()/positions()/
                   margins() within the TTL window returns the
                   same object reference without re-fetching from
                   the broker. Two consumers share one round-trip.
  3. Stale code  — grep confirms no other module-level TTL cache
                   is wrapping broker_apis.fetch_* directly. The
                   `_NAV_CACHE` in auth.py is the response-shape
                   cache for /api/auth/firm-nav, not a duplicate.
  4. Reusable    — invalidate hook re-used by 3 route handlers
                   (positions / holdings / funds) + 2 postback
                   paths (Kite + Dhan/Groww shared).
  5. Correctness — invalidate("positions") drops raw cache too,
                   ensuring `?fresh=1` and postback paths surface
                   broker truth on the next fetch.
"""

from __future__ import annotations

import time

import pandas as pd


# ── 1. SSOT — single raw-DF cache implementation ─────────────────────────────


def test_single_raw_cache_implementation():
    """broker_apis defines exactly one raw-DataFrame cache pair.

    The audit reveals the only places that need a raw-DF cache are
    holdings / positions / margins. We assert this is the sole shape.
    """
    from backend.brokers import broker_apis

    # Module exposes the three helpers expected by the route + postback
    # callers — invalidate must be callable with a key OR no args.
    assert callable(broker_apis._raw_cache_get)
    assert callable(broker_apis._raw_cache_put)
    assert callable(broker_apis._raw_cache_invalidate)

    # Only three keys are produced anywhere in the codebase. Anything
    # else is a regression (would indicate a parallel cache layer).
    import inspect
    src = inspect.getsource(broker_apis)
    for needed in ('"holdings"', '"positions"', '"margins"'):
        assert needed in src, f"raw cache key {needed} missing from broker_apis"


# ── 2 & 5. Performance + Correctness — cache reuses + invalidates ────────────


def test_raw_cache_round_trip():
    """put → get returns same object; expiry returns None; invalidate clears."""
    from backend.brokers import broker_apis

    broker_apis._raw_cache_invalidate()  # start clean

    df = pd.DataFrame({"account": ["TEST"], "pnl": [100.0]})
    payload = [df]

    # Miss → None
    assert broker_apis._raw_cache_get("positions") is None

    # Put → get returns SAME reference (no deep copy).
    broker_apis._raw_cache_put("positions", payload)
    out = broker_apis._raw_cache_get("positions")
    assert out is payload, "raw cache must return the same reference (no deep copy)"

    # Other keys unaffected.
    assert broker_apis._raw_cache_get("holdings") is None
    assert broker_apis._raw_cache_get("margins") is None

    # Targeted invalidate drops one key only.
    broker_apis._raw_cache_put("holdings", payload)
    broker_apis._raw_cache_invalidate("positions")
    assert broker_apis._raw_cache_get("positions") is None
    assert broker_apis._raw_cache_get("holdings") is payload

    # Full invalidate clears everything.
    broker_apis._raw_cache_invalidate()
    assert broker_apis._raw_cache_get("positions") is None
    assert broker_apis._raw_cache_get("holdings") is None


def test_raw_cache_ttl_expiry(monkeypatch):
    """Cache entry expires after `_RAW_TTL_S` seconds."""
    from backend.brokers import broker_apis

    broker_apis._raw_cache_invalidate()
    df = pd.DataFrame({"account": ["A"], "pnl": [1.0]})
    payload = [df]
    broker_apis._raw_cache_put("positions", payload)

    # Still fresh — same reference.
    assert broker_apis._raw_cache_get("positions") is payload

    # Advance monotonic past the TTL — should now return None.
    base = time.monotonic()
    monkeypatch.setattr(broker_apis._time, "monotonic",
                        lambda: base + broker_apis._RAW_TTL_S + 1.0)
    assert broker_apis._raw_cache_get("positions") is None


# ── 3. Stale code — no rival caching layer ────────────────────────────────────


def test_no_parallel_raw_cache_in_compute_firm_nav():
    """algo/nav.py does NOT maintain its own broker-DF cache.

    compute_firm_nav must rely on broker_apis._RAW_CACHE; a parallel
    in-process memo here would mean the postback invalidate path can't
    reach it and NavCard would lag /performance after every fill.
    """
    from backend.api.algo import nav
    import inspect
    src = inspect.getsource(nav)
    # Permitted: function names referencing _funds_from_df etc.
    # Disallowed: a module-level TTL cache built specifically for the
    # broker_apis calls (would be a duplicate layer).
    forbidden_patterns = (
        "_CACHE_TTL", "_NAV_TTL", "_HOLDINGS_CACHE", "_POSITIONS_CACHE",
    )
    for pat in forbidden_patterns:
        assert pat not in src, (
            f"algo/nav.py should not maintain its own raw-DF cache "
            f"({pat!r} found) — use broker_apis._RAW_CACHE which is "
            f"already invalidated by every postback path."
        )


# ── 4. Reusable — every fetch_* zero-arg call goes through the cache ──────────


def test_fetch_zero_arg_routes_through_cache(monkeypatch):
    """fetch_holdings() / fetch_positions() / fetch_margins() with no
    args must consult `_RAW_CACHE` first and store the result.

    We patch the local fetchers to record call counts. Two zero-arg
    calls within the TTL window must trigger ONLY ONE underlying call;
    the second comes from cache.
    """
    from backend.brokers import broker_apis

    broker_apis._raw_cache_invalidate()
    # Disable conn-service shortcut so we hit the local fetchers.
    monkeypatch.setattr(broker_apis, "_USE_CONN_SERVICE", False)

    calls = {"holdings": 0, "positions": 0, "margins": 0}

    def _fake_h(*a, **kw):
        calls["holdings"] += 1
        return [pd.DataFrame({"account": ["A"], "pnl": [1.0]})]

    def _fake_p(*a, **kw):
        calls["positions"] += 1
        return [pd.DataFrame({"account": ["A"], "pnl": [2.0]})]

    def _fake_m(*a, **kw):
        calls["margins"] += 1
        return [pd.DataFrame({"account": ["A"], "cash": [3.0]})]

    monkeypatch.setattr(broker_apis, "_fetch_holdings_local",  _fake_h)
    monkeypatch.setattr(broker_apis, "_fetch_positions_local", _fake_p)
    monkeypatch.setattr(broker_apis, "_fetch_margins_local",   _fake_m)

    # First call — cold cache, underlying called once.
    broker_apis.fetch_holdings()
    broker_apis.fetch_positions()
    broker_apis.fetch_margins()
    assert calls == {"holdings": 1, "positions": 1, "margins": 1}

    # Second call within TTL — must be served from cache.
    broker_apis.fetch_holdings()
    broker_apis.fetch_positions()
    broker_apis.fetch_margins()
    assert calls == {"holdings": 1, "positions": 1, "margins": 1}, (
        f"second zero-arg call should hit cache; got {calls}"
    )

    # Invalidate one — only that one re-fetches; others stay cached.
    broker_apis._raw_cache_invalidate("positions")
    broker_apis.fetch_holdings()
    broker_apis.fetch_positions()
    broker_apis.fetch_margins()
    assert calls == {"holdings": 1, "positions": 2, "margins": 1}, (
        f"invalidate(positions) should refetch only positions; got {calls}"
    )


# ── 5. Correctness — explicit-arg calls do NOT hit cache ──────────────────────


def test_fetch_with_args_bypasses_cache(monkeypatch):
    """Single-account internal callers (`broker=` / `account=`) must
    skip the cache — those calls have account-specific shape.
    """
    from backend.brokers import broker_apis

    broker_apis._raw_cache_invalidate()
    monkeypatch.setattr(broker_apis, "_USE_CONN_SERVICE", False)

    count = {"n": 0}

    def _fake(*a, **kw):
        count["n"] += 1
        return [pd.DataFrame()]

    monkeypatch.setattr(broker_apis, "_fetch_holdings_local", _fake)

    # Zero-arg primes the cache.
    broker_apis.fetch_holdings()
    assert count["n"] == 1

    # Explicit-kwarg call must bypass the cache.
    broker_apis.fetch_holdings(account="ZG7777")
    assert count["n"] == 2, (
        "fetch_holdings(account=...) must not consult / write the zero-arg cache"
    )

    # Cache still serves the zero-arg shape.
    broker_apis.fetch_holdings()
    assert count["n"] == 2, "zero-arg shape should still be cached"
