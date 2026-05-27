"""
Regression test for the Phase 23 exchange-open gate's timezone bug.

History: _build_now_ctx() initially passed datetime.now(timezone.utc)
to _build_context, but _build_context's hours_start / hours_end are
IST times-of-day and now.replace(hour=…) keeps the original tz. So
at 10:13 IST (= 04:43 UTC) on a real trading day, the MCX range
check became `09:00 UTC <= 04:43 UTC <= 23:30 UTC` and reported MCX
closed when in fact it was open. Operator hit this on a CRUDEOIL
order ticket.

The fix uses `timestamp_indian()` so the comparison is IST-vs-IST.
This test fakes the wall clock to a known IST hour during MCX hours
and asserts _symbol_exchange_open('MCX', ctx) is True. If a future
refactor reintroduces a UTC `now`, this test fails fast.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo
from unittest.mock import patch

import pytest

from backend.api.algo.agent_engine import _symbol_exchange_open, _build_now_ctx

_IST = ZoneInfo("Asia/Kolkata")


def _fake_ist(hour: int, minute: int = 0, *, weekday_monday: bool = True) -> datetime:
    """Pick a recent Monday (so weekend gating doesn't false-negative)."""
    # 2026-05-25 is a Monday.
    return datetime(2026, 5, 25, hour, minute, 0, tzinfo=_IST)


def _patched_now(ist_dt):
    """Patch both timestamp_indian (used by the new _build_now_ctx) and
    fetch_holidays (so the test doesn't hit nseindia.com)."""
    return [
        patch("backend.shared.helpers.date_time_utils.datetime",
              new=type("FakeDT", (), {
                  "now": staticmethod(lambda tz=None: ist_dt if tz is _IST else ist_dt),
              })),
        patch("backend.shared.helpers.broker_apis.fetch_holidays",
              return_value=set()),
    ]


def test_mcx_open_at_10am_ist():
    """10:00 IST during MCX (09:00–23:30 IST) → MCX must be open."""
    ist = _fake_ist(10, 0)
    patches = _patched_now(ist)
    for p in patches: p.start()
    try:
        ctx = _build_now_ctx()
        assert _symbol_exchange_open("MCX", ctx) is True, (
            f"MCX should be open at 10:00 IST — ctx={ctx}"
        )
    finally:
        for p in patches: p.stop()


def test_mcx_closed_at_2am_ist():
    """02:00 IST — MCX is between sessions (closes 23:30 prev day)."""
    ist = _fake_ist(2, 0)
    patches = _patched_now(ist)
    for p in patches: p.start()
    try:
        ctx = _build_now_ctx()
        assert _symbol_exchange_open("MCX", ctx) is False
    finally:
        for p in patches: p.stop()


def test_nse_open_at_11am_ist_but_mcx_open_too():
    """11:00 IST — both NSE (09:15–15:30) and MCX (09:00–23:30) open."""
    ist = _fake_ist(11, 0)
    patches = _patched_now(ist)
    for p in patches: p.start()
    try:
        ctx = _build_now_ctx()
        assert _symbol_exchange_open("NSE", ctx) is True
        assert _symbol_exchange_open("NFO", ctx) is True
        assert _symbol_exchange_open("MCX", ctx) is True
    finally:
        for p in patches: p.stop()


def test_nse_closed_mcx_open_at_5pm_ist():
    """17:00 IST — NSE closed (15:30), MCX still open."""
    ist = _fake_ist(17, 0)
    patches = _patched_now(ist)
    for p in patches: p.start()
    try:
        ctx = _build_now_ctx()
        assert _symbol_exchange_open("NSE", ctx) is False
        assert _symbol_exchange_open("MCX", ctx) is True
    finally:
        for p in patches: p.stop()


def test_unknown_exchange_returns_false():
    ctx = _build_now_ctx()   # real now — content doesn't matter
    assert _symbol_exchange_open("FAKE", ctx) is False
    assert _symbol_exchange_open("", ctx) is False
