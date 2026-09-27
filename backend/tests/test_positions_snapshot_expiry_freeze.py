"""
Tests for the serving-side half of the 2026-09 expiry-day-final snapshot
freeze fix: `_union_and_filter_expiry_frozen_rows` wired into
`_positions_snapshot()` (backend/api/routes/positions.py).

Real bug this closes (account ZG0790, verified against prod `daily_book`):
`_positions_snapshot`'s `latest_batch` CTE selects only ONE `captured_at`
per ACCOUNT. An account holding both a still-actively-traded symbol
(CRUDEOIL — fresh batch every day) and an expired-but-still-frozen symbol
(GOLD/GOLDM — older batch) only ever has the CRUDEOIL batch selected; GOLD
is silently masked every single night even though it still exists in the
DB and is still inside its freeze window. `expiry_status` is mocked at
source in these tests (its own classification logic is covered exhaustively
in test_expiry_freeze.py) so these tests isolate the UNION/FILTER wiring.

Five quality dimensions:
  SSOT        — one shared classifier (`expiry_status`) drives both the
                union-in and the exclude-past-boundary decisions.
  Correctness — masked-by-fresher-batch scenario reproduced verbatim from
                the real ZG0790 shape (CRUDEOIL fresh, GOLDM frozen-older).
  Performance — pure in-memory mocks; no network or real DB calls.
  Reuse       — the SAME `build_row_from_snapshot_raw` row-builder used by
                the main query path builds the unioned rows too (no
                parallel row-shaping code).
  UX          — a refresh-eligible (past-boundary) row must never be
                indistinguishable from a live row — excluded outright.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _utc(y, m, d, h, mi, s=0):
    return datetime(y, m, d, h, mi, s, tzinfo=timezone.utc)


def _algo_orders_result():
    r = MagicMock()
    r.__iter__ = lambda self: iter([])
    return r


def _make_dispatching_session(main_rows, frozen_rows):
    """session.execute() dispatches on SQL shape: the main combined
    `_positions_snapshot` query (contains 'latest_batch AS'), the frozen-
    candidates lookup (contains 'QTY != 0' and 'DISTINCT ON (ACCOUNT'),
    and the GTT lookup (contains 'ALGO_ORDERS') each get their own canned
    result."""

    async def _execute(stmt, *args, **kwargs):
        sql = str(stmt).upper()
        result = MagicMock()
        if "ALGO_ORDERS" in sql:
            return _algo_orders_result()
        if "DISTINCT ON (ACCOUNT, SYMBOL)" in sql and "QTY != 0" in sql:
            result.all.return_value = frozen_rows
        else:
            result.all.return_value = main_rows
        return result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_execute)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_frozen_symbol_unioned_in_when_masked_by_fresher_batch():
    """Real ZG0790 shape: main query's latest_batch only surfaces
    CRUDEOIL (fresher batch); GOLDM's older frozen batch is invisible to
    that join. The dedicated frozen-candidates lookup must surface it,
    and the final response must include BOTH symbols."""
    from backend.api.routes.positions import _positions_snapshot

    main_row = (
        "ZG0790", "CRUDEOIL26OCTFUT", "MCX", 100, Decimal("8500.00"),
        Decimal("8600.00"), Decimal("1000.00"), Decimal("2000.00"), "{}",
        _utc(2026, 9, 27, 10, 0), Decimal("8550.00"), None, None,
    )
    frozen_row = (
        "ZG0790", "GOLDM26SEP155000PE", "MCX", -100, Decimal("120.00"),
        Decimal("4300.00"), Decimal("500.00"), Decimal("800.00"), "{}",
        _utc(2026, 9, 25, 18, 30, 19), Decimal("4014.50"), None,
    )

    mock_session = _make_dispatching_session([main_row], [frozen_row])

    async def _fake_status(symbol, captured_at, exchange, now_ist):
        return "frozen" if symbol == "GOLDM26SEP155000PE" else "not_expiry"

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.api.algo.expiry_freeze.expiry_status", side_effect=_fake_status),
    ):
        resp = await _positions_snapshot()

    assert resp is not None
    symbols = {r.tradingsymbol for r in resp.rows}
    assert "CRUDEOIL26OCTFUT" in symbols
    assert "GOLDM26SEP155000PE" in symbols, (
        "Expired-but-frozen GOLDM leg must be unioned in even though a "
        "fresher CRUDEOIL batch exists for the same account"
    )


@pytest.mark.asyncio
async def test_refresh_eligible_row_excluded_even_if_it_would_otherwise_be_masking():
    """Once a candidate's freeze window has ended (expiry_status returns
    'refresh_eligible'), it must be excluded outright — never unioned in,
    and never left over from the main query either."""
    from backend.api.routes.positions import _positions_snapshot

    main_row = (
        "ZJ6294", "GOLDM26SEP155000PE", "MCX", -100, Decimal("120.00"),
        Decimal("4300.00"), Decimal("500.00"), Decimal("800.00"), "{}",
        _utc(2026, 9, 25, 18, 30, 19), Decimal("4014.50"), None, None,
    )
    mock_session = _make_dispatching_session([main_row], [])

    async def _fake_status(symbol, captured_at, exchange, now_ist):
        return "refresh_eligible"

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.api.algo.expiry_freeze.expiry_status", side_effect=_fake_status),
    ):
        resp = await _positions_snapshot()

    assert resp is None, (
        "The account's only row is past its refresh boundary — response "
        "must be empty (None), not silently serve the obsolete row"
    )


@pytest.mark.asyncio
async def test_ordinary_row_unaffected_by_expiry_freeze_filter():
    """A normal, non-expiry row (expiry_status='not_expiry') must pass
    through completely unchanged — zero regression for the common case."""
    from backend.api.routes.positions import _positions_snapshot

    main_row = (
        "ZG0790", "RELIANCE", "NSE", 10, Decimal("2800.00"),
        Decimal("2850.00"), Decimal("500.00"), Decimal("500.00"), "{}",
        _utc(2026, 9, 27, 10, 0), Decimal("2800.00"), None, None,
    )
    mock_session = _make_dispatching_session([main_row], [])

    async def _fake_status(symbol, captured_at, exchange, now_ist):
        return "not_expiry"

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.api.algo.expiry_freeze.expiry_status", side_effect=_fake_status),
    ):
        resp = await _positions_snapshot()

    assert resp is not None
    assert {r.tradingsymbol for r in resp.rows} == {"RELIANCE"}
