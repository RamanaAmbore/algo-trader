"""
Near-month futures rollover tests.

Verifies that all three resolver paths — ``_resolve_mcx_commodity``,
``_resolve_cds_currency`` (watchlist.py) and
``lookup_mcx_front_month_future`` (derivatives.py) — skip the expiring
contract on rollover day and pick the next-month contract.

Key invariants:
  - BEFORE expiry date  → return front-month (JUN).
  - ON expiry date      → skip JUN (settling), return JUL.
  - DAY AFTER expiry    → return JUL.
  - All-expired fallback → return last listed contract (not None).
  - CDS path mirrors MCX path on rollover day.

SSOT: every assert mirrors the identical ``inst.x > today_iso`` rule used
by ``lookup_mcx_front_month_future`` and now also by both watchlist
resolvers after the expiry-rollover fix.
Performance: zero I/O — instruments cache is monkey-patched in-process.
Stale code: no duplicate filtering logic; both paths delegated to shared
``inst.x > today_iso`` rule.
Reuse: uses the canonical public API — ``_resolve_mcx_commodity``,
``_resolve_cds_currency``, ``lookup_mcx_front_month_future``.
UX: on rollover day the operator's watchlist LTP panel resolves to the JUL
contract automatically; test confirms the exact symbol returned.
"""

from __future__ import annotations

import asyncio
import datetime as _real_datetime
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


# ---------------------------------------------------------------------------
# Helpers — fake instruments list
# ---------------------------------------------------------------------------

def _make_fut(sym: str, underlying: str, exchange: str, expiry: str) -> SimpleNamespace:
    """Build a minimal instrument namespace matching what the cache returns."""
    return SimpleNamespace(s=sym, u=underlying, e=exchange, t="FUT", x=expiry)


_CRUDEOIL_INSTRUMENTS = [
    _make_fut("CRUDEOIL26JUNFUT", "CRUDEOIL", "MCX", "2026-06-30"),
    _make_fut("CRUDEOIL26JULFUT", "CRUDEOIL", "MCX", "2026-07-31"),
    _make_fut("CRUDEOIL26AUGFUT", "CRUDEOIL", "MCX", "2026-08-31"),
]

_USDINR_INSTRUMENTS = [
    _make_fut("USDINR26JUNFUT", "USDINR", "CDS", "2026-06-25"),
    _make_fut("USDINR26JULFUT", "USDINR", "CDS", "2026-07-29"),
    _make_fut("USDINR26AUGFUT", "USDINR", "CDS", "2026-08-26"),
]


def _fake_resp(items):
    return SimpleNamespace(items=items)


# ---------------------------------------------------------------------------
# Context helpers
# ---------------------------------------------------------------------------

def _make_fake_datetime_class(today_iso: str):
    """Build a datetime subclass whose ``now()`` returns a fixed IST date.

    The resolver functions use ``from datetime import datetime as _dt`` as a
    local deferred import, then call ``_dt.now(ZoneInfo("Asia/Kolkata"))``.
    We patch ``datetime.datetime`` in the stdlib ``datetime`` module so that
    local binding resolves to our fake class.
    """
    d = _real_datetime.date.fromisoformat(today_iso)

    class _FakeDatetime(_real_datetime.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return _real_datetime.datetime(
                d.year, d.month, d.day, 12, 0, 0, tzinfo=tz
            )

    return _FakeDatetime


def _patch_watchlist(items, today_iso: str):
    """Return list of context managers that patch watchlist resolver paths.

    Both ``get_or_fetch`` and ``datetime`` are deferred-imported inside the
    resolver function bodies (``from backend.api.cache import get_or_fetch``
    and ``from datetime import datetime as _dt``).  We patch at their
    canonical source locations so the local binding picks up the mock.
    """
    fake_dt = _make_fake_datetime_class(today_iso)

    async def _fake_gor(*_a, **_kw):
        return _fake_resp(items)

    return [
        # Patch the source module so the deferred ``from backend.api.cache
        # import get_or_fetch`` in the resolver picks up our fake.
        patch("backend.api.cache.get_or_fetch",
              new=AsyncMock(side_effect=_fake_gor)),
        # Patch datetime.datetime in the stdlib module so
        # ``from datetime import datetime as _dt`` resolves to FakeDatetime.
        patch("datetime.datetime", new=fake_dt),
    ]


def _patch_derivatives(items, today_iso: str):
    """Return list of context managers that patch derivatives resolver paths."""
    fake_dt = _make_fake_datetime_class(today_iso)

    async def _fake_gor(*_a, **_kw):
        return _fake_resp(items)

    return [
        patch("backend.api.cache.get_or_fetch",
              new=AsyncMock(side_effect=_fake_gor)),
        patch("datetime.datetime", new=fake_dt),
    ]


# ---------------------------------------------------------------------------
# _resolve_mcx_commodity — MCX rollover
# ---------------------------------------------------------------------------

def test_mcx_before_expiry_returns_jun():
    """Day before expiry: JUN contract (2026-06-30) is still the front month."""
    from backend.api.routes.watchlist import _resolve_mcx_commodity

    with ExitStack() as stack:
        for cm in _patch_watchlist(_CRUDEOIL_INSTRUMENTS, "2026-06-29"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_mcx_commodity("CRUDEOIL"))
    assert result == "CRUDEOIL26JUNFUT", (
        f"day before expiry should still be JUN, got {result!r}")


def test_mcx_on_expiry_day_skips_jun_picks_jul():
    """On expiry day (2026-06-30) JUN contract is settling — must roll to JUL."""
    from backend.api.routes.watchlist import _resolve_mcx_commodity

    with ExitStack() as stack:
        for cm in _patch_watchlist(_CRUDEOIL_INSTRUMENTS, "2026-06-30"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_mcx_commodity("CRUDEOIL"))
    assert result == "CRUDEOIL26JULFUT", (
        f"on expiry day JUN must be skipped, expected JUL, got {result!r}")


def test_mcx_day_after_expiry_returns_jul():
    """Day after expiry: JUL is now the front month."""
    from backend.api.routes.watchlist import _resolve_mcx_commodity

    with ExitStack() as stack:
        for cm in _patch_watchlist(_CRUDEOIL_INSTRUMENTS, "2026-07-01"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_mcx_commodity("CRUDEOIL"))
    assert result == "CRUDEOIL26JULFUT", (
        f"day after expiry should be JUL, got {result!r}")


def test_mcx_all_expired_returns_last_not_none():
    """When instruments cache lags and all contracts are expired, return the
    last listed (not None) — prevents the watchlist from silently losing
    the symbol entirely."""
    from backend.api.routes.watchlist import _resolve_mcx_commodity

    with ExitStack() as stack:
        for cm in _patch_watchlist(_CRUDEOIL_INSTRUMENTS, "2026-09-01"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_mcx_commodity("CRUDEOIL"))
    assert result == "CRUDEOIL26AUGFUT", (
        f"all-expired fallback should be last listed (AUG), got {result!r}")


def test_mcx_missing_commodity_returns_none():
    """Unknown commodity — instruments cache has no matching rows — None."""
    from backend.api.routes.watchlist import _resolve_mcx_commodity

    with ExitStack() as stack:
        for cm in _patch_watchlist(_CRUDEOIL_INSTRUMENTS, "2026-06-15"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_mcx_commodity("NONEXISTENT"))
    assert result is None


# ---------------------------------------------------------------------------
# _resolve_cds_currency — CDS rollover
# ---------------------------------------------------------------------------

def test_cds_before_expiry_returns_jun():
    """Day before CDS expiry (2026-06-25): JUN is still front month."""
    from backend.api.routes.watchlist import _resolve_cds_currency

    with ExitStack() as stack:
        for cm in _patch_watchlist(_USDINR_INSTRUMENTS, "2026-06-24"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_cds_currency("USDINR"))
    assert result == "USDINR26JUNFUT", (
        f"day before CDS expiry should be JUN, got {result!r}")


def test_cds_on_expiry_day_skips_jun_picks_jul():
    """On CDS expiry day (2026-06-25) the JUN contract is settling — roll to JUL."""
    from backend.api.routes.watchlist import _resolve_cds_currency

    with ExitStack() as stack:
        for cm in _patch_watchlist(_USDINR_INSTRUMENTS, "2026-06-25"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_cds_currency("USDINR"))
    assert result == "USDINR26JULFUT", (
        f"on CDS expiry day JUN must be skipped, expected JUL, got {result!r}")


def test_cds_day_after_expiry_returns_jul():
    """Day after CDS expiry: JUL is the front month."""
    from backend.api.routes.watchlist import _resolve_cds_currency

    with ExitStack() as stack:
        for cm in _patch_watchlist(_USDINR_INSTRUMENTS, "2026-06-26"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_cds_currency("USDINR"))
    assert result == "USDINR26JULFUT", (
        f"day after CDS expiry should be JUL, got {result!r}")


def test_cds_all_expired_returns_last_not_none():
    """CDS all-expired fallback returns last listed contract (not None)."""
    from backend.api.routes.watchlist import _resolve_cds_currency

    with ExitStack() as stack:
        for cm in _patch_watchlist(_USDINR_INSTRUMENTS, "2026-09-01"):
            stack.enter_context(cm)
        result = asyncio.run(_resolve_cds_currency("USDINR"))
    assert result == "USDINR26AUGFUT", (
        f"all-expired fallback should be last listed (AUG), got {result!r}")


# ---------------------------------------------------------------------------
# lookup_mcx_front_month_future — derivatives.py (already correct; verify)
# ---------------------------------------------------------------------------

def test_derivatives_front_month_before_expiry():
    """SSOT check: derivatives resolver returns JUN the day before expiry."""
    from backend.api.algo.derivatives import lookup_mcx_front_month_future

    with ExitStack() as stack:
        for cm in _patch_derivatives(_CRUDEOIL_INSTRUMENTS, "2026-06-29"):
            stack.enter_context(cm)
        result = asyncio.run(lookup_mcx_front_month_future("CRUDEOIL"))
    assert result == "CRUDEOIL26JUNFUT"


def test_derivatives_front_month_on_expiry_day_rolls():
    """On expiry day the derivatives resolver already skips JUN — regression guard."""
    from backend.api.algo.derivatives import lookup_mcx_front_month_future

    with ExitStack() as stack:
        for cm in _patch_derivatives(_CRUDEOIL_INSTRUMENTS, "2026-06-30"):
            stack.enter_context(cm)
        result = asyncio.run(lookup_mcx_front_month_future("CRUDEOIL"))
    assert result == "CRUDEOIL26JULFUT"


# ---------------------------------------------------------------------------
# Symmetry: watchlist resolver matches derivatives resolver on rollover
# ---------------------------------------------------------------------------

def test_watchlist_and_derivatives_resolvers_symmetric_across_rollover():
    """``_resolve_mcx_commodity`` must return the same symbol as
    ``lookup_mcx_front_month_future`` on every day around expiry — including
    the rollover day itself.  This is the canonical SSOT invariant."""
    from backend.api.routes.watchlist import _resolve_mcx_commodity
    from backend.api.algo.derivatives import lookup_mcx_front_month_future

    test_dates = [
        "2026-06-28",  # 2 days before
        "2026-06-29",  # 1 day before
        "2026-06-30",  # expiry day — key rollover
        "2026-07-01",  # day after
        "2026-07-15",  # mid next month
    ]
    for today_iso in test_dates:
        with ExitStack() as stack:
            for cm in _patch_watchlist(_CRUDEOIL_INSTRUMENTS, today_iso):
                stack.enter_context(cm)
            wl_result = asyncio.run(_resolve_mcx_commodity("CRUDEOIL"))

        with ExitStack() as stack:
            for cm in _patch_derivatives(_CRUDEOIL_INSTRUMENTS, today_iso):
                stack.enter_context(cm)
            der_result = asyncio.run(lookup_mcx_front_month_future("CRUDEOIL"))

        assert wl_result == der_result, (
            f"on {today_iso!r}: watchlist={wl_result!r} but derivatives={der_result!r}"
        )
