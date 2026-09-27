"""
Tests for the 2026-09 expiry-day-final snapshot freeze fix wired into
daily_snapshot.py (confirmed-empty marker + orphan-sweep protection).

Real-data-derived scenarios (verified against prod `daily_book` rows for
accounts ZG0790/GOLD and ZJ6294/GOLDM during the investigation this fix
closes):
  - ZG0790 has an actively-traded CRUDEOIL leg (fresh anchor every day)
    AND a GOLD/GOLDM options leg that stopped appearing roughly a week
    BEFORE that contract's real expiry (2026-09-25) — an ORDINARY closed
    position, not an expiry artifact. Must keep being pruned by the
    existing 7-day sweep, completely unmodified.
  - ZJ6294 held GOLDM legs that closed on the session that IS their real
    expiry date (2026-09-25) and then went fully flat (broker returns
    zero positions every day after). Those legs must be protected from
    the sweep until the next market-open day's 08:00 IST, and the
    fully-flat day must still produce an anchor for future runs via the
    confirmed-empty ('positions_empty') marker.

Five quality dimensions:
  SSOT        — `_frozen_protected_symbols` is the single place both
                orphan-sweep variants consult for the expired/closed split.
  Correctness — mixed-account scenario (protected + unprotected candidate
                in the SAME sweep call) verified explicitly.
  Performance — pure in-memory mocks; no network or real DB calls.
  Reuse       — `_write_confirmed_empty_marker` reuses `_upsert_rows` (the
                existing UPSERT path), no new write mechanism invented.
  UX          — an ordinarily-closed position must never be resurrected
                by this fix (grep-level regression guard below).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch


def _utc(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)


ACCOUNT_MIXED = "ZG0790"
ACCOUNT_FLAT = "ZJ6294"


# ---------------------------------------------------------------------------
# _frozen_protected_symbols — mixed expired + ordinary-closed candidates
# ---------------------------------------------------------------------------

def test_frozen_protected_symbols_protects_only_the_expired_leg():
    """Candidates: GOLDM (closed exactly on its own real expiry session —
    frozen) and NIFTY (an ordinary closed position, unrelated to expiry).
    Only GOLDM must come back in the protected set; NIFTY must be swept
    normally, exactly matching pre-fix behaviour for non-expiry rows."""
    from backend.api.algo.daily_snapshot import _frozen_protected_symbols

    candidates = [
        ("GOLDM26SEP155000PE", "MCX", _utc(2026, 9, 18, 10, 0)),
        ("NIFTY24800CE", "NFO", _utc(2026, 9, 18, 10, 0)),
    ]

    async def _fake_status(symbol, captured_at, exchange, now_ist):
        return "frozen" if symbol == "GOLDM26SEP155000PE" else "not_expiry"

    with patch("backend.api.algo.expiry_freeze.expiry_status", side_effect=_fake_status):
        protected = asyncio.run(
            _frozen_protected_symbols(candidates, current_symbols=set(),
                                       now_ist=_utc(2026, 9, 18, 10, 0))
        )

    assert protected == {"GOLDM26SEP155000PE"}


def test_frozen_protected_symbols_excludes_symbols_already_current():
    """A symbol already present in current_symbols (still returned by the
    broker today) is skipped entirely — no need to classify it."""
    from backend.api.algo.daily_snapshot import _frozen_protected_symbols

    calls = []

    async def _fake_status(symbol, captured_at, exchange, now_ist):
        calls.append(symbol)
        return "frozen"

    candidates = [("CRUDEOIL26OCTFUT", "MCX", _utc(2026, 9, 27, 10, 0))]
    with patch("backend.api.algo.expiry_freeze.expiry_status", side_effect=_fake_status):
        protected = asyncio.run(
            _frozen_protected_symbols(
                candidates, current_symbols={"CRUDEOIL26OCTFUT"},
                now_ist=_utc(2026, 9, 27, 10, 0),
            )
        )

    assert protected == set()
    assert calls == [], "Must not classify a symbol the broker still reports today"


def test_frozen_protected_symbols_empty_after_refresh_boundary():
    """Once expiry_status reports 'refresh_eligible' (boundary passed),
    the symbol is no longer protected — falls back into the ordinary sweep."""
    from backend.api.algo.daily_snapshot import _frozen_protected_symbols

    async def _fake_status(symbol, captured_at, exchange, now_ist):
        return "refresh_eligible"

    candidates = [("GOLDM26SEP155000PE", "MCX", _utc(2026, 9, 25, 18, 30))]
    with patch("backend.api.algo.expiry_freeze.expiry_status", side_effect=_fake_status):
        protected = asyncio.run(
            _frozen_protected_symbols(candidates, current_symbols=set(),
                                       now_ist=_utc(2026, 9, 29, 3, 0))
        )

    assert protected == set()


# ---------------------------------------------------------------------------
# _delete_prior_orphan_positions — mixed-account integration (real ZG0790
# shape: a valid same-day anchor from CRUDEOIL, candidates = GOLDM (must
# survive) + NIFTY (an ordinary closed leg, must still be swept))
# ---------------------------------------------------------------------------

def _mock_session_with_candidates(candidate_rows, anchor, rowcount):
    async def _execute(stmt, *args, **kwargs):
        sql = str(stmt).upper().strip()
        result = MagicMock()
        if sql.startswith("SELECT MAX(CAPTURED_AT)"):
            result.scalar.return_value = anchor
        elif sql.startswith("SELECT SYMBOL"):
            result.all.return_value = candidate_rows
        else:
            result.rowcount = rowcount
        return result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_execute)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


def test_delete_prior_orphan_positions_protects_expired_keeps_ordinary_pruned():
    """Mixed account (real ZG0790 shape): current_symbols={CRUDEOIL} (a
    valid same-day anchor from an unrelated, still-live symbol). Candidate
    rows in the 7-day window: GOLDM (expired-on-own-session — must be
    protected) and NIFTY (ordinary closed — must be pruned normally)."""
    from backend.api.algo.daily_snapshot import _delete_prior_orphan_positions

    candidates = [
        ("GOLDM26SEP155000PE", "MCX", _utc(2026, 9, 25, 18, 30)),
        ("NIFTY24800CE", "NFO", _utc(2026, 9, 20, 10, 0)),
    ]
    mock_session = _mock_session_with_candidates(candidates, anchor=_utc(2026, 9, 27, 10, 0), rowcount=1)

    async def _fake_status(symbol, captured_at, exchange, now_ist):
        return "frozen" if symbol == "GOLDM26SEP155000PE" else "not_expiry"

    with (
        patch("backend.api.algo.daily_snapshot.async_session", return_value=mock_session),
        patch("backend.api.algo.expiry_freeze.expiry_status", side_effect=_fake_status),
    ):
        pruned = asyncio.run(
            _delete_prior_orphan_positions(ACCOUNT_MIXED, {"CRUDEOIL"})
        )

    assert pruned == 1
    # Final call is the DELETE — inspect its bound `symbols` (the KEEP list,
    # i.e. NOT IN this set means "protected from deletion").
    call_args = mock_session.execute.call_args_list[-1]
    params = call_args[0][1]
    assert "GOLDM26SEP155000PE" in params["symbols"], (
        "Expired-on-own-expiry-day row must be added to the keep/protect list"
    )
    assert "NIFTY24800CE" not in params["symbols"], (
        "An ordinary closed position (not expired) must NOT be protected — "
        "it stays out of the keep list and gets swept exactly as before"
    )
    assert "CRUDEOIL" in params["symbols"]


# ---------------------------------------------------------------------------
# Confirmed-empty marker wiring — snapshot_daily_book
# ---------------------------------------------------------------------------

def _make_loop_mock(raw_data: dict) -> MagicMock:
    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(return_value=raw_data)
    return mock_loop


def test_snapshot_writes_confirmed_empty_marker_on_genuine_flat_day():
    """Broker CONFIRMED (positions_confirmed=True) zero open positions
    today. snapshot_daily_book must write the 'positions_empty' sentinel
    AFTER the prune calls — this is failure mode 2's fix (account
    ZJ6294/GOLDM: without this marker, the prune's own anchor never
    advances and a stale row is never eligible for cleanup)."""
    from backend.api.algo.daily_snapshot import snapshot_daily_book

    raw_data = {
        "holdings": [], "positions": [], "trades": [], "funds": [],
        "positions_confirmed": True,
    }
    fake_broker = MagicMock()
    fake_broker.account = ACCOUNT_FLAT

    marker_calls = []

    async def fake_marker(account, target_date, now_ist):
        marker_calls.append((account, target_date))

    async def fake_delete_orphan(target_date, account, current_symbols):
        return 0

    async def fake_delete_prior(account, current_symbols):
        return 0

    async def fake_upsert(rows):
        return len(rows)

    mock_connections = MagicMock()
    mock_connections.conn = {ACCOUNT_FLAT: MagicMock()}
    mock_loop = _make_loop_mock(raw_data)
    target_date = date(2026, 9, 27)

    with (
        patch("backend.api.algo.daily_snapshot._get_connections", return_value=mock_connections),
        patch("backend.brokers.registry.all_brokers", return_value=[fake_broker]),
        patch("backend.api.algo.daily_snapshot._upsert_rows", side_effect=fake_upsert),
        patch("backend.api.algo.daily_snapshot._delete_orphan_positions", side_effect=fake_delete_orphan),
        patch("backend.api.algo.daily_snapshot._delete_prior_orphan_positions", side_effect=fake_delete_prior),
        patch("backend.api.algo.daily_snapshot._write_confirmed_empty_marker", side_effect=fake_marker),
        patch("asyncio.get_running_loop", return_value=mock_loop),
    ):
        result = asyncio.run(snapshot_daily_book(target_date=target_date))

    assert result["errors"] == []
    assert marker_calls == [(ACCOUNT_FLAT, target_date)], (
        f"Confirmed-empty marker must be written exactly once for the flat "
        f"account; got {marker_calls}"
    )


def test_snapshot_does_not_write_marker_when_not_confirmed():
    """Broker returned an ambiguous/non-confirming empty response
    (positions_confirmed=False) — must NOT write the marker, since that
    would wrongly assert 'checked and confirmed empty' for a response
    that never actually confirmed anything (missing-vs-zero convention)."""
    from backend.api.algo.daily_snapshot import snapshot_daily_book

    raw_data = {
        "holdings": [], "positions": [], "trades": [], "funds": [],
        "positions_confirmed": False,
    }
    fake_broker = MagicMock()
    fake_broker.account = ACCOUNT_FLAT

    marker_calls = []

    async def fake_marker(account, target_date, now_ist):
        marker_calls.append((account, target_date))

    async def fake_delete_orphan(target_date, account, current_symbols):
        return 0

    async def fake_delete_prior(account, current_symbols):
        return 0

    async def fake_upsert(rows):
        return len(rows)

    mock_connections = MagicMock()
    mock_connections.conn = {ACCOUNT_FLAT: MagicMock()}
    mock_loop = _make_loop_mock(raw_data)

    with (
        patch("backend.api.algo.daily_snapshot._get_connections", return_value=mock_connections),
        patch("backend.brokers.registry.all_brokers", return_value=[fake_broker]),
        patch("backend.api.algo.daily_snapshot._upsert_rows", side_effect=fake_upsert),
        patch("backend.api.algo.daily_snapshot._delete_orphan_positions", side_effect=fake_delete_orphan),
        patch("backend.api.algo.daily_snapshot._delete_prior_orphan_positions", side_effect=fake_delete_prior),
        patch("backend.api.algo.daily_snapshot._write_confirmed_empty_marker", side_effect=fake_marker),
        patch("asyncio.get_running_loop", return_value=mock_loop),
    ):
        asyncio.run(snapshot_daily_book(target_date=date(2026, 9, 27)))

    assert marker_calls == [], "Must never write the marker for an unconfirmed empty response"


def test_snapshot_does_not_write_marker_when_positions_nonempty():
    """Broker returned real positions today — no marker needed (there IS
    a real anchor from the actual positions rows)."""
    from backend.api.algo.daily_snapshot import snapshot_daily_book

    pos = {
        "tradingsymbol": "RELIANCE", "exchange": "NSE", "quantity": 10,
        "average_price": 2800.0, "last_price": 2850.0, "close_price": 2800.0,
        "pnl": 500.0, "day_change": 50.0, "day_change_percentage": 1.78,
        "overnight_quantity": 10, "day_buy_quantity": 0, "day_sell_quantity": 0,
        "day_buy_value": 0.0, "day_sell_value": 0.0,
    }
    raw_data = {
        "holdings": [], "positions": [pos], "trades": [], "funds": [],
        "positions_confirmed": True,
    }
    fake_broker = MagicMock()
    fake_broker.account = ACCOUNT_MIXED

    marker_calls = []

    async def fake_marker(account, target_date, now_ist):
        marker_calls.append((account, target_date))

    async def fake_delete_orphan(target_date, account, current_symbols):
        return 0

    async def fake_delete_prior(account, current_symbols):
        return 0

    async def fake_upsert(rows):
        return len(rows)

    mock_connections = MagicMock()
    mock_connections.conn = {ACCOUNT_MIXED: MagicMock()}
    mock_loop = _make_loop_mock(raw_data)

    with (
        patch("backend.api.algo.daily_snapshot._get_connections", return_value=mock_connections),
        patch("backend.brokers.registry.all_brokers", return_value=[fake_broker]),
        patch("backend.api.algo.daily_snapshot._upsert_rows", side_effect=fake_upsert),
        patch("backend.api.algo.daily_snapshot._delete_orphan_positions", side_effect=fake_delete_orphan),
        patch("backend.api.algo.daily_snapshot._delete_prior_orphan_positions", side_effect=fake_delete_prior),
        patch("backend.api.algo.daily_snapshot._write_confirmed_empty_marker", side_effect=fake_marker),
        patch("asyncio.get_running_loop", return_value=mock_loop),
    ):
        asyncio.run(snapshot_daily_book(target_date=date(2026, 9, 27)))

    assert marker_calls == []


# ---------------------------------------------------------------------------
# _fetch_account_data — positions_confirmed flag
# ---------------------------------------------------------------------------

def test_fetch_account_data_confirmed_true_on_real_empty_net():
    """broker.positions() returning {'net': []} is a CONFIRMED empty book."""
    from backend.api.algo.daily_snapshot import _fetch_account_data

    mock_broker = MagicMock()
    mock_broker.holdings.return_value = []
    mock_broker.positions.return_value = {"net": []}
    mock_broker.trades.return_value = []
    mock_broker.margins.return_value = {}

    result = _fetch_account_data(mock_broker, ACCOUNT_FLAT, date(2026, 9, 27))

    assert result["positions"] == []
    assert result["positions_confirmed"] is True


def test_fetch_account_data_confirmed_false_on_ambiguous_response():
    """broker.positions() returning None (not an exception, just a falsy
    non-dict) must NOT be confirmed — coerced to [] for backward
    compatibility but flagged as unconfirmed."""
    from backend.api.algo.daily_snapshot import _fetch_account_data

    mock_broker = MagicMock()
    mock_broker.holdings.return_value = []
    mock_broker.positions.return_value = None
    mock_broker.trades.return_value = []
    mock_broker.margins.return_value = {}

    result = _fetch_account_data(mock_broker, ACCOUNT_FLAT, date(2026, 9, 27))

    assert result["positions"] == []
    assert result["positions_confirmed"] is False


def test_fetch_account_data_confirmed_false_on_broker_exception():
    """An outright exception leaves positions=None and confirmed=False —
    unchanged failure-guard behaviour."""
    from backend.api.algo.daily_snapshot import _fetch_account_data

    mock_broker = MagicMock()
    mock_broker.holdings.return_value = []
    mock_broker.positions.side_effect = RuntimeError("Broker offline")
    mock_broker.trades.return_value = []
    mock_broker.margins.return_value = {}

    result = _fetch_account_data(mock_broker, ACCOUNT_FLAT, date(2026, 9, 27))

    assert result["positions"] is None
    assert result["positions_confirmed"] is False
