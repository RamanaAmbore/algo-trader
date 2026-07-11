"""
Sparkline snapshot — `snapshot_sparkline()` persists per-symbol closing-
bar series into `daily_book` with `kind='sparkline'`.

Five quality dimensions:
  • SSOT       — sparkline series produced ONLY by `snapshot_sparkline`
                 at close-settled. Frontend cell renderer reads from
                 the persisted rows when `is_animating === false`.
  • Correctness— on `<exch>:close`, series captured with settled=False;
                 on `<exch>:close_settled`, series overwritten with
                 settled=True (final bar).
  • Performance— caps universe at 500 symbols; skips symbols with no
                 OHLCV data (no crash on missing bars).
  • Reuse      — same `_upsert_rows` helper as the main daily snapshot;
                 identical UPSERT-on-conflict path.
  • UX         — payload shape `{"points": [{t, ltp}...], "settled":
                 <bool>, "captured_at": <iso>}` is stable so the frontend
                 renderer can rely on it.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest


IST = ZoneInfo("Asia/Kolkata")


def _fake_bars(days: int = 5) -> list[dict]:
    """Fabricate an OHLCV series."""
    base = date(2026, 3, 10)
    from datetime import timedelta
    out = []
    for i in range(days):
        d = base + timedelta(days=i)
        out.append({
            "date":  d,
            "open":  100.0 + i,
            "high":  102.0 + i,
            "low":    99.0 + i,
            "close": 101.0 + i,
            "volume": 10000 + i * 100,
        })
    return out


@pytest.mark.asyncio
async def test_snapshot_sparkline_writes_rows_for_universe():
    """A populated universe → one row per symbol with `points` array."""
    from backend.api.algo import daily_snapshot as ds

    universe = [("RELIANCE", "NSE"), ("TCS", "NSE")]

    with patch.object(ds, "_sparkline_universe_symbols",
                      new=AsyncMock(return_value=universe)), \
         patch("backend.api.persistence.ohlcv_store.get_or_fetch_daily",
               new=AsyncMock(return_value=_fake_bars(5))) as _m_ohlc, \
         patch.object(ds, "_upsert_rows",
                      new=AsyncMock(return_value=2)) as m_up:
        result = await ds.snapshot_sparkline(settled=False)

    assert result["symbols"] == 2
    m_up.assert_awaited_once()
    rows_batch = m_up.call_args[0][0]
    assert len(rows_batch) == 2
    for row in rows_batch:
        assert row["kind"] == "sparkline"
        assert row["account"] == "__firm__"
        payload = json.loads(row["payload_json"])
        assert "points" in payload
        assert len(payload["points"]) == 5
        assert payload["settled"] is False


@pytest.mark.asyncio
async def test_snapshot_sparkline_settled_flag_writes_true():
    """close_settled path writes settled=True in the payload."""
    from backend.api.algo import daily_snapshot as ds

    universe = [("NIFTY", "NSE")]

    with patch.object(ds, "_sparkline_universe_symbols",
                      new=AsyncMock(return_value=universe)), \
         patch("backend.api.persistence.ohlcv_store.get_or_fetch_daily",
               new=AsyncMock(return_value=_fake_bars(3))), \
         patch.object(ds, "_upsert_rows",
                      new=AsyncMock(return_value=1)) as m_up:
        result = await ds.snapshot_sparkline(settled=True)

    assert result["symbols"] == 1
    rows = m_up.call_args[0][0]
    payload = json.loads(rows[0]["payload_json"])
    assert payload["settled"] is True


@pytest.mark.asyncio
async def test_snapshot_sparkline_skips_symbols_without_bars():
    """Symbols with no OHLCV in ohlcv_store are silently skipped
    (not upserted as an empty payload).

    The mock returns None for MISSING on BOTH calls (db_only=True and
    db_only=False) so that the broker-fallback path also produces nothing
    and the symbol is still skipped after the retry.
    """
    from backend.api.algo import daily_snapshot as ds

    universe = [("RELIANCE", "NSE"), ("MISSING", "NSE")]

    async def _fake_bars_for(sym, exch, from_d=None, to_d=None, db_only=False, bypass_cache=None):
        # Both DB and broker return nothing for MISSING.
        if sym == "MISSING":
            return None
        return _fake_bars(3)  # RELIANCE hits on first or second call

    with patch.object(ds, "_sparkline_universe_symbols",
                      new=AsyncMock(return_value=universe)), \
         patch("backend.api.persistence.ohlcv_store.get_or_fetch_daily",
               new=AsyncMock(side_effect=_fake_bars_for)), \
         patch.object(ds, "_upsert_rows",
                      new=AsyncMock(return_value=1)) as m_up:
        result = await ds.snapshot_sparkline(settled=True)

    # Only RELIANCE written — MISSING skipped, not an empty row.
    rows = m_up.call_args[0][0]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIANCE"


@pytest.mark.asyncio
async def test_snapshot_sparkline_fetches_from_broker_on_db_miss():
    """When DB returns empty bars (db_only=True), the function retries
    without db_only=True to fetch from broker and writes the result."""
    from backend.api.algo import daily_snapshot as ds

    universe = [("RELIANCE", "NSE")]

    call_log: list[bool] = []

    async def _two_call_mock(sym, exch, from_d=None, to_d=None, db_only=False, bypass_cache=None):
        call_log.append(db_only)
        if db_only:
            return []          # first call: DB miss
        return _fake_bars(5)   # second call: broker hit

    with patch.object(ds, "_sparkline_universe_symbols",
                      new=AsyncMock(return_value=universe)), \
         patch("backend.api.persistence.ohlcv_store.get_or_fetch_daily",
               new=AsyncMock(side_effect=_two_call_mock)) as m_ohlc, \
         patch.object(ds, "_upsert_rows",
                      new=AsyncMock(return_value=1)) as m_up:
        result = await ds.snapshot_sparkline(settled=False)

    # get_or_fetch_daily called TWICE for the symbol: DB miss then broker hit.
    assert m_ohlc.await_count == 2, f"Expected 2 calls, got {m_ohlc.await_count}"
    assert call_log == [True, False], f"Expected [db_only=True, db_only=False], got {call_log}"

    # Verify the broker result was written as a sparkline row with 5 points.
    assert result["symbols"] == 1
    rows = m_up.call_args[0][0]
    assert len(rows) == 1
    assert rows[0]["symbol"] == "RELIANCE"
    assert rows[0]["kind"] == "sparkline"
    payload = json.loads(rows[0]["payload_json"])
    assert len(payload["points"]) == 5


@pytest.mark.asyncio
async def test_snapshot_sparkline_empty_universe_no_op():
    """Empty universe → returns cleanly without touching the DB."""
    from backend.api.algo import daily_snapshot as ds

    with patch.object(ds, "_sparkline_universe_symbols",
                      new=AsyncMock(return_value=[])), \
         patch.object(ds, "_upsert_rows",
                      new=AsyncMock(return_value=0)) as m_up:
        result = await ds.snapshot_sparkline(settled=False)

    assert result["symbols"] == 0
    m_up.assert_not_called()


@pytest.mark.asyncio
async def test_close_handler_fires_sparkline_snapshot():
    """`_snapshot_close` in market_lifecycle_handlers must also invoke
    `snapshot_sparkline` on both `<exch>:close` and `<exch>:close_settled`
    events."""
    from backend.api.algo import market_lifecycle_handlers as mlh

    with patch("backend.api.algo.daily_snapshot.snapshot_daily_book",
               new=AsyncMock(return_value={
                   "accounts": [], "holdings_rows": 0,
                   "positions_rows": 0, "trades_rows": 0,
                   "funds_rows": 0, "errors": [],
               })), \
         patch("backend.api.algo.daily_snapshot.snapshot_sparkline",
               new=AsyncMock(return_value={"symbols": 5, "errors": []})) as m_sp:
        await mlh._snapshot_close("nse", "close_settled")

    m_sp.assert_awaited_once()
    _, kwargs = m_sp.call_args
    assert kwargs.get("settled") is True


@pytest.mark.asyncio
async def test_sparkline_payload_points_are_ordered_and_include_ltp():
    """Payload's `points` array is oldest→newest, each row is {t, ltp}."""
    from backend.api.algo import daily_snapshot as ds

    universe = [("RELIANCE", "NSE")]
    bars = _fake_bars(4)

    with patch.object(ds, "_sparkline_universe_symbols",
                      new=AsyncMock(return_value=universe)), \
         patch("backend.api.persistence.ohlcv_store.get_or_fetch_daily",
               new=AsyncMock(return_value=bars)), \
         patch.object(ds, "_upsert_rows",
                      new=AsyncMock(return_value=1)) as m_up:
        await ds.snapshot_sparkline(settled=True)

    rows = m_up.call_args[0][0]
    payload = json.loads(rows[0]["payload_json"])
    points = payload["points"]
    assert len(points) == 4
    # Ordered oldest → newest by date string.
    assert points[0]["t"]  <  points[-1]["t"]
    # Each point has both `t` and `ltp` keys.
    for p in points:
        assert "t"   in p
        assert "ltp" in p


@pytest.mark.asyncio
async def test_snapshot_sparkline_uses_correct_call_signature():
    """Verify get_or_fetch_daily is called with from_d/to_d kwargs,
    not the invalid days= kwarg. Regression test for call signature bug."""
    from backend.api.algo import daily_snapshot as ds
    from datetime import timedelta

    universe = [("NIFTY 50", "NSE")]
    bars = _fake_bars(5)

    with patch.object(ds, "_sparkline_universe_symbols",
                      new=AsyncMock(return_value=universe)), \
         patch("backend.api.persistence.ohlcv_store.get_or_fetch_daily",
               new=AsyncMock(return_value=bars)) as m_ohlc, \
         patch.object(ds, "_upsert_rows",
                      new=AsyncMock(return_value=1)) as m_up:
        result = await ds.snapshot_sparkline(settled=False)

    # Verify the call was made exactly once
    m_ohlc.assert_awaited_once()

    # Extract the call arguments
    call_args, call_kwargs = m_ohlc.call_args

    # Verify positional args: symbol and exchange
    assert call_args[0] == "NIFTY 50", "First arg should be symbol"
    assert call_args[1] == "NSE", "Second arg should be exchange"

    # Verify from_d and to_d kwargs exist and are dates
    assert "from_d" in call_kwargs, "Must call with from_d kwarg, not days="
    assert "to_d" in call_kwargs, "Must call with to_d kwarg, not days="
    assert isinstance(call_kwargs["from_d"], date), "from_d must be a date"
    assert isinstance(call_kwargs["to_d"], date), "to_d must be a date"

    # Verify buffer spans ~10 days (accounting for weekends)
    from_d = call_kwargs["from_d"]
    to_d = call_kwargs["to_d"]
    delta = (to_d - from_d).days
    assert delta == 10, f"Expected 10-day buffer (weekends), got {delta} days"

    # Verify db_only=True was passed
    assert call_kwargs.get("db_only") is True, "db_only must be True"

    # Verify no invalid 'days' kwarg was passed
    assert "days" not in call_kwargs, "Invalid 'days=' kwarg must not be present"

    # Verify sparkline rows were written (row was not silently skipped)
    assert result["symbols"] == 1, "One symbol should be written"
    rows = m_up.call_args[0][0]
    assert len(rows) == 1, "One row should be upserted"
    assert rows[0]["symbol"] == "NIFTY 50"
    assert rows[0]["kind"] == "sparkline"
    payload = json.loads(rows[0]["payload_json"])
    assert "points" in payload
    assert len(payload["points"]) == 5


@pytest.mark.asyncio
async def test_close_handler_fires_sparkline_snapshot_unsettled():
    """On `<exch>:close` event (not settled), sparkline snapshot is fired
    with settled=False."""
    from backend.api.algo import market_lifecycle_handlers as mlh

    with patch("backend.api.algo.daily_snapshot.snapshot_daily_book",
               new=AsyncMock(return_value={
                   "accounts": [], "holdings_rows": 0,
                   "positions_rows": 0, "trades_rows": 0,
                   "funds_rows": 0, "errors": [],
               })), \
         patch("backend.api.algo.daily_snapshot.snapshot_sparkline",
               new=AsyncMock(return_value={"symbols": 3, "errors": []})) as m_sp:
        await mlh._snapshot_close("nse", "close")

    m_sp.assert_awaited_once()
    _, kwargs = m_sp.call_args
    assert kwargs.get("settled") is False, \
        f"Expected settled=False for close event, got {kwargs}"


@pytest.mark.asyncio
async def test_mcx_close_fires_both_snapshots():
    """On MCX close event, both snapshot_daily_book AND snapshot_sparkline
    are fired (not just one). Verifies that MCX close also triggers sparkline
    persistence."""
    from backend.api.algo import market_lifecycle_handlers as mlh

    m_daily = AsyncMock(return_value={
        "accounts": ["MCX_ACCT"],
        "holdings_rows": 2,
        "positions_rows": 4,
        "trades_rows": 1,
        "funds_rows": 1,
    })
    m_spark = AsyncMock(return_value={
        "symbols": 10,
        "errors": [],
    })

    with patch("backend.api.algo.daily_snapshot.snapshot_daily_book", m_daily), \
         patch("backend.api.algo.daily_snapshot.snapshot_sparkline", m_spark):
        await mlh._snapshot_close("mcx", "close")

    # Both handlers must have been called exactly once
    assert m_daily.await_count == 1, "snapshot_daily_book should be called for MCX"
    assert m_spark.await_count == 1, "snapshot_sparkline should be called for MCX"

    # Verify settled=False was passed to both
    _, daily_kwargs = m_daily.call_args
    _, spark_kwargs = m_spark.call_args
    assert daily_kwargs.get("settled") is False
    assert spark_kwargs.get("settled") is False


@pytest.mark.asyncio
async def test_cds_close_handler_fires():
    """CDS close event fires sparkline snapshot with settled=True when
    it's a close_settled event."""
    from backend.api.algo import market_lifecycle_handlers as mlh

    m_daily = AsyncMock(return_value={
        "accounts": [],
        "holdings_rows": 0,
        "positions_rows": 1,
        "trades_rows": 0,
        "funds_rows": 0,
    })
    m_spark = AsyncMock(return_value={
        "symbols": 2,
        "errors": [],
    })

    with patch("backend.api.algo.daily_snapshot.snapshot_daily_book", m_daily), \
         patch("backend.api.algo.daily_snapshot.snapshot_sparkline", m_spark):
        await mlh._snapshot_close("cds", "close_settled")

    assert m_daily.await_count == 1
    assert m_spark.await_count == 1

    _, spark_kwargs = m_spark.call_args
    assert spark_kwargs.get("settled") is True, \
        "Expected settled=True for CDS close_settled event"


@pytest.mark.asyncio
async def test_settled_kwarg_sequence_close_then_settled():
    """Sequence of close then close_settled events shows proper settled flag progression."""
    from backend.api.algo import market_lifecycle_handlers as mlh

    spark_calls: list[dict] = []

    async def _fake_sparkline(*, settled: bool = False):
        spark_calls.append({"settled": settled})
        return {"symbols": 5, "errors": []}

    with patch("backend.api.algo.daily_snapshot.snapshot_daily_book",
               new=AsyncMock(return_value={"accounts": [], "holdings_rows": 0,
                                          "positions_rows": 0, "trades_rows": 0,
                                          "funds_rows": 0, "errors": []})), \
         patch("backend.api.algo.daily_snapshot.snapshot_sparkline",
               side_effect=_fake_sparkline):
        await mlh._snapshot_close("nse", "close")
        await mlh._snapshot_close("nse", "close_settled")

    assert len(spark_calls) == 2, f"Expected 2 sparkline calls, got {len(spark_calls)}"
    assert spark_calls[0]["settled"] is False, \
        f"First call should have settled=False, got {spark_calls[0]}"
    assert spark_calls[1]["settled"] is True, \
        f"Second call should have settled=True, got {spark_calls[1]}"
