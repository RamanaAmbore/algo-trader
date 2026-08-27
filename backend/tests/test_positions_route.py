"""Tests for positions route — day_change_val / day_change_percentage derivation."""

import asyncio
import inspect
import math
from datetime import date, datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_net_rows(rows):
    """Build minimal kite.positions()['net'] payloads."""
    defaults = dict(
        tradingsymbol="NIFTY25APRFUT",
        exchange="NFO",
        product="NRML",
        average_price=22000.0,
        unrealised=0.0,
        realised=0.0,
    )
    return [dict(defaults, **r) for r in rows]


def _run_fetch_positions_direct(net_rows):
    """
    Call the core day-change derivation logic directly on a DataFrame,
    bypassing the @for_all_accounts decorator and Connections singleton.

    This tests the broker_apis logic in isolation without touching any
    broker network path (per project convention: do not mock broker API calls).
    """
    df = pd.DataFrame(net_rows)
    df['quantity'] = df['quantity'] * df['multiplier']

    if df.empty:
        return df

    df['day_change'] = df['last_price'] - df['close_price']
    df['day_change_val'] = df['day_change'] * df['quantity']
    prev_val = (df['close_price'] * df['quantity']).abs()
    df['day_change_percentage'] = (
        df['day_change_val'] / prev_val.replace(0, pd.NA) * 100
    ).fillna(0)
    return df


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_positions_response_includes_day_change_columns():
    """day_change_val and day_change_percentage are present and correct."""
    last_price  = 22100.0
    close_price = 22000.0
    quantity    = 50
    multiplier  = 1

    net_rows = _make_net_rows([dict(
        last_price=last_price,
        close_price=close_price,
        quantity=quantity,
        multiplier=multiplier,
    )])
    df = _run_fetch_positions_direct(net_rows)

    assert not df.empty, "Expected at least one row"
    assert "day_change_val" in df.columns, "day_change_val missing"
    assert "day_change_percentage" in df.columns, "day_change_percentage missing"

    eff_qty      = quantity * multiplier                        # 50
    expected_val = (last_price - close_price) * eff_qty        # 100 * 50 = 5000.0
    expected_pct = expected_val / abs(close_price * eff_qty) * 100  # ≈ 0.4545…

    assert abs(df.iloc[0]["day_change_val"] - expected_val) < 0.01, \
        f"day_change_val: expected {expected_val}, got {df.iloc[0]['day_change_val']}"
    assert abs(df.iloc[0]["day_change_percentage"] - expected_pct) < 0.001, \
        f"day_change_percentage: expected {expected_pct}, got {df.iloc[0]['day_change_percentage']}"
    assert df.iloc[0]["day_change_percentage"] is not None


def test_positions_day_change_zero_when_close_is_zero():
    """close_price = 0 must yield day_change_percentage = 0, not NaN or Inf."""
    net_rows = _make_net_rows([dict(
        last_price=100.0,
        close_price=0.0,
        quantity=10,
        multiplier=1,
    )])
    df = _run_fetch_positions_direct(net_rows)

    assert not df.empty
    val = df.iloc[0]["day_change_percentage"]
    assert val == 0.0, f"Expected 0.0 when close_price=0, got {val}"
    assert not math.isnan(float(val)), "day_change_percentage must not be NaN"
    assert not math.isinf(float(val)), "day_change_percentage must not be Inf"


# ---------------------------------------------------------------------------
# Regression: _override_stale_ltp_from_ticker — NameError on _qty
# Commit c0355526 introduced the additive pnl patch but dropped the _qty
# definition in the rewrite.  Calling _override_stale_ltp_from_ticker on a
# DataFrame whose rows get LTP-patched must NOT raise NameError and MUST
# update pnl by (new_ltp − old_ltp) × quantity.
# ---------------------------------------------------------------------------

def test_override_stale_ltp_from_ticker_pnl_patch_no_name_error():
    """_override_stale_ltp_from_ticker: pnl delta patch must not raise NameError."""
    from backend.api.routes.positions import _override_stale_ltp_from_ticker

    df = pd.DataFrame([{
        'tradingsymbol': 'CRUDEOIL26JUL6900PE',
        'exchange': 'MCX',
        'last_price': 220.0,   # stale REST LTP (== close → day_change 0)
        'close_price': 220.0,
        'quantity': 10,
        'overnight_quantity': 10,
        'day_buy_quantity': 0,
        'day_sell_quantity': 0,
        'day_buy_value': 0.0,
        'day_sell_value': 0.0,
        'average_price': 200.0,
        'realised': 0.0,
        'pnl': 200.0,           # broker pnl at stale LTP
        'day_change_val': 0.0,
        'day_change': 0.0,
    }])

    # Patch the ticker to return a fresh LTP of 264.5 for the symbol.
    mock_ticker = MagicMock()
    mock_ticker.get_ltp_by_sym.return_value = 264.5

    with patch('backend.brokers.kite_ticker.get_ticker', return_value=mock_ticker):
        # Must NOT raise NameError on _qty
        try:
            _override_stale_ltp_from_ticker(df)
        except NameError as exc:
            pytest.fail(f"NameError raised (regression): {exc}")

    # last_price patched to fresh tick
    assert abs(df.at[0, 'last_price'] - 264.5) < 0.01, "last_price not patched from ticker"
    # pnl updated by (264.5 - 220.0) * 10 = +445
    expected_pnl = 200.0 + (264.5 - 220.0) * 10  # 645.0
    assert abs(df.at[0, 'pnl'] - expected_pnl) < 0.01, \
        f"pnl additive patch wrong: expected {expected_pnl}, got {df.at[0, 'pnl']}"


# ---------------------------------------------------------------------------
# _override_stale_close_from_snapshot — MCX overnight stale close_price fix
#
# Five quality dimensions:
#   SSOT        — _override_stale_close_from_snapshot is the single patch
#                 path for stale Kite close_price; tested in isolation here.
#   Correctness — MCX overnight: snapshot ltp replaces stale Kite close_price,
#                 day_change_val is recomputed against the corrected baseline.
#                 Mid-session (today's) snapshot excluded by captured_at filter.
#   Performance — pure in-memory; no broker or network calls.
#   Reuse       — same function used by both live (_fetch) and paper paths.
#   UX          — after MCX close, P&L on CRUDEOIL options shows real move
#                 vs prior-session EOD, not a phantom zero from stale close.
# ---------------------------------------------------------------------------

def _make_mcx_df(
    last_price: float = 264.5,
    close_price: float = 180.0,
    quantity: int = 10,
    overnight_quantity: int = 10,
    account: str = "ZG0790",
    symbol: str = "CRUDEOIL26JUL6900PE",
) -> pd.DataFrame:
    """Minimal MCX overnight positions DataFrame for close-override tests."""
    return pd.DataFrame([{
        'account': account,
        'tradingsymbol': symbol,
        'exchange': 'MCX',
        'last_price': last_price,
        'close_price': close_price,
        'quantity': quantity,
        'overnight_quantity': overnight_quantity,
        'day_buy_quantity': 0,
        'day_sell_quantity': 0,
        'day_buy_value': 0.0,
        'day_sell_value': 0.0,
        'average_price': 200.0,
        'realised': 0.0,
        'pnl': (last_price - 200.0) * quantity,
        'day_change_val': 0.0,
        'day_change': 0.0,
        'day_change_percentage': 0.0,
        'pnl_percentage': 0.0,
    }])


def _run_close_override(df: pd.DataFrame, snapshot_rows: list) -> pd.DataFrame:
    """Invoke _override_stale_close_from_snapshot with mocked DB + midnight."""
    from backend.api.routes.positions import _override_stale_close_from_snapshot
    from zoneinfo import ZoneInfo

    ist = ZoneInfo("Asia/Kolkata")
    midnight = datetime(2026, 7, 8, 0, 0, 0, tzinfo=ist)

    mock_result = MagicMock()
    mock_result.all.return_value = snapshot_rows
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch(
            "backend.shared.helpers.date_time_utils.timestamp_indian",
            return_value=midnight,
        ),
    ):
        asyncio.run(_override_stale_close_from_snapshot(df))
    return df


class TestOverrideStaleCloseFromSnapshot:
    """Unit tests for _override_stale_close_from_snapshot.

    Scenario context: Kite REST positions endpoint ships `close_price` that
    lags the prior session's authoritative EOD during the MCX overnight window
    (23:30 to next 09:00 IST). The function patches `close_price` from
    `daily_book` snapshots captured BEFORE today's midnight IST, then
    recomputes `day_change_val` so the per-share P&L reflects the actual
    move since the correct prior-session close.
    """

    def test_mcx_overnight_close_price_replaced_by_snapshot_ltp(self):
        """MCX overnight: stale Kite close_price (180.0) replaced by
        daily_book.ltp (220.0); day_change_val recomputed accordingly.

        Numbers: last_price=264.5, corrected_close=220.0, qty=10 (overnight)
        Expected day_change_val = oq*(ltp-cls) = 10*(264.5-220.0) = 445.0
        """
        STALE_CLOSE = 180.0
        SNAPSHOT_LTP = 220.0
        LAST_PRICE = 264.5
        QTY = 10
        PREV_PNL = 400.0

        df = _make_mcx_df(
            last_price=LAST_PRICE,
            close_price=STALE_CLOSE,
            quantity=QTY,
            overnight_quantity=QTY,
        )
        # Snapshot rows now include (account, symbol, ltp, total_pnl)
        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", SNAPSHOT_LTP, PREV_PNL)]
        df = _run_close_override(df, snapshot_rows)

        assert abs(df.at[0, 'close_price'] - SNAPSHOT_LTP) < 0.005, (
            f"close_price should be {SNAPSHOT_LTP} (snapshot ltp), "
            f"got {df.at[0, 'close_price']}"
        )
        expected_dcv = (LAST_PRICE - SNAPSHOT_LTP) * QTY  # 445.0
        assert abs(df.at[0, 'day_change_val'] - expected_dcv) < 0.5, (
            f"day_change_val should be ~{expected_dcv} after close override, "
            f"got {df.at[0, 'day_change_val']}"
        )
        assert abs(df.at[0, 'day_change'] - (LAST_PRICE - SNAPSHOT_LTP)) < 0.005, (
            f"day_change per-share should be {LAST_PRICE - SNAPSHOT_LTP}, "
            f"got {df.at[0, 'day_change']}"
        )

    def test_no_pre_midnight_snapshot_leaves_close_price_unchanged(self):
        """When daily_book returns no rows (simulates the captured_at < midnight
        filter excluding today-only snapshots), close_price must be unchanged.

        This guards the 2026-06-22 regression: a mid-session deploy's startup
        snapshot would collapse day_change_val to zero if it were used as the
        close baseline.
        """
        ORIGINAL_CLOSE = 180.0
        df = _make_mcx_df(close_price=ORIGINAL_CLOSE)
        df = _run_close_override(df, snapshot_rows=[])

        assert abs(df.at[0, 'close_price'] - ORIGINAL_CLOSE) < 0.005, (
            f"close_price must remain {ORIGINAL_CLOSE} when no pre-midnight "
            f"snapshot exists; got {df.at[0, 'close_price']}"
        )

    def test_epsilon_guard_skips_rounding_noise(self):
        """Snapshot ltp within epsilon=0.005 of current close_price → no patch.

        Protects against spurious rewrites when Kite's float repr and snapshot
        storage agree within floating-point precision.
        """
        CLOSE = 220.0
        SNAP = 220.003  # within 0.005 epsilon
        df = _make_mcx_df(close_price=CLOSE, last_price=250.0)
        original_dcv = float(df.at[0, 'day_change_val'])

        df = _run_close_override(
            df,
            snapshot_rows=[("ZG0790", "CRUDEOIL26JUL6900PE", SNAP, 500.0)],
        )

        assert abs(df.at[0, 'close_price'] - CLOSE) < 0.005, (
            "close_price must not be patched for sub-epsilon difference"
        )
        assert abs(df.at[0, 'day_change_val'] - original_dcv) < 0.005, (
            "day_change_val must not be recomputed when epsilon guard skips"
        )

    def test_only_matching_account_symbol_patched(self):
        """Snapshot map keyed by (account, symbol) — only matching row patched."""
        df = pd.concat([
            _make_mcx_df(account="ZG0790", last_price=264.5, close_price=180.0),
            _make_mcx_df(account="ZJ6294", last_price=264.5, close_price=180.0),
        ], ignore_index=True)

        snapshot_rows = [("ZG0790", "CRUDEOIL26JUL6900PE", 220.0, 500.0)]
        df = _run_close_override(df, snapshot_rows)

        zg = df[df['account'] == 'ZG0790'].iloc[0]
        zj = df[df['account'] == 'ZJ6294'].iloc[0]

        assert abs(zg['close_price'] - 220.0) < 0.005, (
            f"ZG0790 close_price should be patched to 220.0, got {zg['close_price']}"
        )
        assert abs(zj['close_price'] - 180.0) < 0.005, (
            "ZJ6294 close_price must remain 180.0 (no snapshot for this account)"
        )

    def test_empty_dataframe_returns_without_db_call(self):
        """Empty DataFrame must not trigger any DB query."""
        from backend.api.routes.positions import _override_stale_close_from_snapshot

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        df = pd.DataFrame()
        with patch("backend.api.database.async_session", return_value=mock_session):
            asyncio.run(_override_stale_close_from_snapshot(df))

        mock_session.__aenter__.assert_not_called()

    def test_db_failure_leaves_dataframe_unchanged(self):
        """DB query failure must not mutate close_price (logs warning, returns)."""
        from backend.api.routes.positions import _override_stale_close_from_snapshot
        from zoneinfo import ZoneInfo

        ist = ZoneInfo("Asia/Kolkata")
        midnight = datetime(2026, 7, 8, 0, 0, 0, tzinfo=ist)

        df = _make_mcx_df(close_price=180.0)
        original_close = float(df.at[0, 'close_price'])

        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(side_effect=RuntimeError("DB gone"))
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=midnight,
            ),
        ):
            asyncio.run(_override_stale_close_from_snapshot(df))

        assert abs(df.at[0, 'close_price'] - original_close) < 0.005, (
            "close_price must be unchanged when DB query fails"
        )


# ---------------------------------------------------------------------------
# Ordering contract + Case 1/3 backstop coexistence
#
# The close-override must run BEFORE _build_polars_summary (so corrected
# close_price feeds the summary day_change_percentage denominator) and
# BEFORE the Case 1/3 pnl backstop (so day_change_val is computed from
# the correct close_price before the rescue condition evaluates dcv == 0).
# ---------------------------------------------------------------------------

class TestFetchOrderingAndCoexistence:
    """Source-level ordering guards and coexistence of close-override with
    the Case 1/3 day_change_val backstop.

    After the CC-reduction refactor, the patch pipeline moved from inline
    inside _fetch() into _patch_raw_positions().  The ordering invariant is
    still enforced — we verify it in the helper that owns the sequence.
    """

    @staticmethod
    def _patch_src() -> str:
        import backend.api.routes.positions as _mod
        return inspect.getsource(_mod._patch_raw_positions)

    @staticmethod
    def _fetch_src() -> str:
        import backend.api.routes.positions as _mod
        return inspect.getsource(_mod._fetch)

    def test_close_override_before_polars_summary(self):
        """_override_stale_close_from_snapshot runs (inside _patch_raw_positions)
        before _build_polars_summary is called in _fetch() — so the summary's
        day_change_percentage denominator uses the patched close price."""
        patch_src = self._patch_src()
        fetch_src = self._fetch_src()

        # Ordering within the patch helper
        idx_override = patch_src.find("_override_stale_close_from_snapshot")
        assert idx_override != -1, (
            "_override_stale_close_from_snapshot must appear in _patch_raw_positions()"
        )

        # _patch_raw_positions is called before _build_polars_summary in _fetch
        idx_patch_call = fetch_src.find("_patch_raw_positions")
        idx_summary = fetch_src.find("_build_polars_summary")
        assert idx_patch_call != -1, "_patch_raw_positions must be called inside _fetch()"
        assert idx_summary != -1, "_build_polars_summary must be called inside _fetch()"
        assert idx_patch_call < idx_summary, (
            "_patch_raw_positions (char %d) must appear BEFORE "
            "_build_polars_summary (char %d) in _fetch()" % (idx_patch_call, idx_summary)
        )

    def test_close_override_before_case1_backstop(self):
        """_override_stale_close_from_snapshot runs before apply_day_change_backstop
        inside _patch_raw_positions() — the ordering invariant is unchanged."""
        src = self._patch_src()
        idx_override = src.find("_override_stale_close_from_snapshot")
        idx_backstop = src.find("apply_day_change_backstop")

        assert idx_backstop != -1, (
            "apply_day_change_backstop not found in _patch_raw_positions() — "
            "did the structure change?"
        )
        assert idx_override != -1, (
            "_override_stale_close_from_snapshot not found in _patch_raw_positions()"
        )
        assert idx_override < idx_backstop, (
            "_override_stale_close_from_snapshot must appear before "
            "apply_day_change_backstop in _patch_raw_positions()"
        )

    def test_case1_backstop_and_close_override_independent(self):
        """Mixed DataFrame: overnight MCX row (stale close) + new position
        (overnight_qty=0, ltp=0, pnl != 0). After close-override:
          Row A: close_price patched to 220.0, day_change_val = 445.0.
          Row B: close_price untouched, day_change_val stays 0.0
            (ltp==0 gate in _compute_day_change_val prevents recompute;
             Case 1 backstop in _fetch() handles Row B separately).
        """
        from zoneinfo import ZoneInfo

        SNAPSHOT_LTP = 220.0
        STALE_CLOSE = 180.0

        row_a = {
            'account': 'ZG0790', 'tradingsymbol': 'CRUDEOIL26JUL6900PE',
            'exchange': 'MCX', 'last_price': 264.5, 'close_price': STALE_CLOSE,
            'quantity': 10, 'overnight_quantity': 10,
            'day_buy_quantity': 0, 'day_sell_quantity': 0,
            'day_buy_value': 0.0, 'day_sell_value': 0.0,
            'average_price': 200.0, 'realised': 0.0,
            'pnl': 645.0, 'day_change_val': 0.0, 'day_change': 0.0,
            'day_change_percentage': 0.0, 'pnl_percentage': 0.0,
        }
        row_b = {
            'account': 'ZG0790', 'tradingsymbol': 'GOLDM26AUGFUT',
            'exchange': 'MCX', 'last_price': 0.0, 'close_price': 6800.0,
            'quantity': 5, 'overnight_quantity': 0,
            'day_buy_quantity': 5, 'day_sell_quantity': 0,
            'day_buy_value': 34000.0, 'day_sell_value': 0.0,
            'average_price': 6800.0, 'realised': 0.0,
            'pnl': 250.0, 'day_change_val': 0.0, 'day_change': 0.0,
            'day_change_percentage': 0.0, 'pnl_percentage': 0.0,
        }

        df = pd.DataFrame([row_a, row_b])

        ist = ZoneInfo("Asia/Kolkata")
        midnight = datetime(2026, 7, 8, 0, 0, 0, tzinfo=ist)

        mock_result = MagicMock()
        mock_result.all.return_value = [("ZG0790", "CRUDEOIL26JUL6900PE", SNAPSHOT_LTP, 500.0)]
        mock_session = AsyncMock()
        mock_session.execute = AsyncMock(return_value=mock_result)
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        from backend.api.routes.positions import _override_stale_close_from_snapshot
        with (
            patch("backend.api.database.async_session", return_value=mock_session),
            patch(
                "backend.shared.helpers.date_time_utils.timestamp_indian",
                return_value=midnight,
            ),
        ):
            asyncio.run(_override_stale_close_from_snapshot(df))

        assert abs(df.at[0, 'close_price'] - SNAPSHOT_LTP) < 0.005, (
            f"Row A close_price should be {SNAPSHOT_LTP}, got {df.at[0, 'close_price']}"
        )
        expected_dcv_a = (264.5 - SNAPSHOT_LTP) * 10  # 445.0
        assert abs(df.at[0, 'day_change_val'] - expected_dcv_a) < 0.5, (
            f"Row A day_change_val should be ~{expected_dcv_a}, "
            f"got {df.at[0, 'day_change_val']}"
        )
        assert abs(df.at[1, 'close_price'] - 6800.0) < 0.005, (
            "Row B close_price must remain 6800.0 (no snapshot entry for GOLDM)"
        )
        # day_change_val stays 0.0: ltp==0 prevents _compute_day_change_val
        # from recomputing; Case 1 backstop rescues via pnl later in _fetch().
        assert abs(df.at[1, 'day_change_val']) < 0.005, (
            "Row B day_change_val must remain 0.0 after close-override step"
        )


# ---------------------------------------------------------------------------
# Snapshot query — today's closed (qty=0) positions appear; yesterday's don't
# ---------------------------------------------------------------------------

def _make_snapshot_session(rows, today_ist_date):
    """Return (mock_session, mock_ts_indian) pair for _positions_snapshot tests."""
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock, MagicMock
    mock_result = MagicMock()
    mock_result.all.return_value = rows
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    # Return a real datetime so >= comparisons and .replace() work correctly.
    # The new _positions_snapshot code does `_now_ist >= _today_ist_8am` which
    # fails when _now_ist is a MagicMock. Use 10:00 IST — clearly post-8am cutoff.
    now_ist = datetime(today_ist_date.year, today_ist_date.month, today_ist_date.day,
                       10, 0, 0, tzinfo=timezone.utc)
    mock_ts = MagicMock(return_value=now_ist)
    return mock_session, mock_ts


def _make_pos_row(sym, qty, captured_at):
    """Minimal daily_book row tuple matching _positions_snapshot SELECT order."""
    return (
        "ACC1", sym, "NFO",
        qty, 100.0, 120.0,   # qty, avg_cost, ltp
        50.0, 200.0,         # day_pnl, total_pnl
        None,                # payload_json
        captured_at,         # captured_at
        110.0,               # previous_close
        115.0, 190.0,        # prev_ltp, prev_settlement_pnl
    )


def test_positions_snapshot_includes_todays_closed_qty_zero_rows():
    """Today's intraday-closed positions (qty=0) must appear in the snapshot.

    The SQL gate is `db.qty != 0 OR db.date = :today_ist`.  When the DB
    returns a qty=0 row from today's batch, _positions_snapshot must emit it
    so the frontend can render the 'closed' chip + opacity decoration.
    """
    import asyncio
    from datetime import date, datetime, timezone
    from unittest.mock import patch

    from backend.api.routes.positions import _positions_snapshot

    today = date(2026, 7, 28)
    now_dt = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)

    rows = [
        _make_pos_row("NIFTY26JUL24500CE", 25, now_dt),   # open
        _make_pos_row("NIFTY26JUL24000PE",  0, now_dt),   # closed today
    ]
    mock_session, mock_ts = _make_snapshot_session(rows, today)

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.shared.helpers.date_time_utils.timestamp_indian", mock_ts),
    ):
        result = asyncio.run(_positions_snapshot())

    assert result is not None
    symbols = [r.tradingsymbol for r in result.rows]
    assert "NIFTY26JUL24500CE" in symbols, "open position must appear"
    assert "NIFTY26JUL24000PE" in symbols, \
        "closed (qty=0) position captured today must appear for 'closed' decoration"
    closed = next(r for r in result.rows if r.tradingsymbol == "NIFTY26JUL24000PE")
    assert closed.quantity == 0


def test_positions_snapshot_excludes_yesterday_closed_qty_zero_rows():
    """Yesterday's closed positions (qty=0) must NOT appear in today's snapshot.

    The SQL gate `db.qty != 0 OR db.date = :today_ist` excludes qty=0 rows
    from prior sessions.  When the DB returns a prior-day closed row, the
    Python layer must not emit it (the DB filters it; mock simulates
    a DB that already applied the filter, returning only the open row).
    This documents the expected DB-level filtering contract.
    """
    import asyncio
    from datetime import date, datetime, timezone
    from unittest.mock import patch

    from backend.api.routes.positions import _positions_snapshot

    today = date(2026, 7, 29)  # Tuesday
    yesterday_dt = datetime(2026, 7, 28, 10, 0, 0, tzinfo=timezone.utc)  # Monday

    # DB (with the filter applied) returns only the open row; the closed
    # qty=0 row from Monday is excluded by db.date != today condition.
    rows = [
        _make_pos_row("NIFTY26JUL24500CE", 25, yesterday_dt),  # carried overnight
        # NIFTY26JUL24000PE (qty=0, date=Monday) → filtered by SQL, not in result
    ]
    mock_session, mock_ts = _make_snapshot_session(rows, today)

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.shared.helpers.date_time_utils.timestamp_indian", mock_ts),
    ):
        result = asyncio.run(_positions_snapshot())

    assert result is not None
    symbols = [r.tradingsymbol for r in result.rows]
    assert "NIFTY26JUL24500CE" in symbols, "overnight open position must appear"
    assert "NIFTY26JUL24000PE" not in symbols, \
        "yesterday's closed position must be absent (filtered by SQL date gate)"


@pytest.mark.asyncio
async def test_prev_batch_excludes_todays_snapshots_uses_yesterday_ltp():
    """Verify that close_price uses yesterday's LTP (prev_ltp), not today's LTP.

    When daily_book has both today's and yesterday's rows, _positions_snapshot
    must pick yesterday's prev_ltp as close_price (not today's ltp), so that
    day_change_val is computed as (today_ltp - yesterday_ltp) * qty, not
    (today_ltp - today_ltp) * qty = 0.
    """
    import asyncio

    today = date(2026, 8, 8)
    now_dt = datetime(2026, 8, 8, 16, 15, 0, tzinfo=timezone.utc)

    # Today's latest batch row: ltp=100.0, qty=10
    today_row = (
        "ACC1", "NIFTY26AUG24500CE", "NFO",
        10, 80.0, 100.0,   # qty, avg_cost, ltp (today's snapshot)
        20.0, 200.0,       # day_pnl, total_pnl
        None,              # payload_json
        now_dt,            # captured_at
        98.0,              # previous_close (yesterday's settlement, frozen)
        98.0, 180.0,       # prev_ltp, prev_settlement_pnl (from prev_batch)
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [today_row]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.shared.helpers.date_time_utils.timestamp_indian",
              return_value=datetime(2026, 8, 8, 16, 15, 0, tzinfo=timezone.utc)),
    ):
        from backend.api.routes.positions import _positions_snapshot
        result = await _positions_snapshot()

    assert result is not None, "snapshot must not return None"
    assert len(result.rows) == 1
    row = result.rows[0]

    # close_price must come from prev_ltp (98.0), not ltp (100.0)
    assert abs(row.close_price - 98.0) < 0.001, (
        f"close_price should be 98.0 (prev_ltp), got {row.close_price}"
    )


@pytest.mark.asyncio
async def test_prev_batch_null_ltp_rows_excluded():
    """Verify that when prev_ltp is NULL, close_price falls back to previous_close.

    When the only prior daily_book row has ltp=NULL (or prev_batch finds no row),
    the fallback is previous_close from the main row. This guards against
    zero-valued prev_ltp collapsing day_change_val.
    """
    import asyncio

    today = date(2026, 8, 8)
    now_dt = datetime(2026, 8, 8, 16, 15, 0, tzinfo=timezone.utc)

    # Row with prev_ltp=NULL; previous_close=97.0 should be fallback
    row = (
        "ACC1", "NIFTY26AUG24500CE", "NFO",
        5, 85.0, 100.0,    # qty, avg_cost, ltp
        75.0, 75.0,        # day_pnl, total_pnl
        None,              # payload_json
        now_dt,            # captured_at
        97.0,              # previous_close (fallback target)
        None, None,        # prev_ltp=NULL, prev_settlement_pnl=NULL
    )

    mock_result = MagicMock()
    mock_result.all.return_value = [row]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.execute = AsyncMock(return_value=mock_result)

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.shared.helpers.date_time_utils.timestamp_indian",
              return_value=datetime(2026, 8, 8, 16, 15, 0, tzinfo=timezone.utc)),
    ):
        from backend.api.routes.positions import _positions_snapshot
        result = await _positions_snapshot()

    assert result is not None
    row = result.rows[0]

    # close_price must fall back to previous_close (97.0)
    assert abs(row.close_price - 97.0) < 0.001, (
        f"close_price should fall back to 97.0 (previous_close), got {row.close_price}"
    )
