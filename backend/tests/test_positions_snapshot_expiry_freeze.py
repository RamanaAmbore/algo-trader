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
from zoneinfo import ZoneInfo

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


def _empty_frozen_candidates_session():
    """A DB session whose frozen-candidates lookup (and any other query)
    returns zero rows — isolates `_union_and_filter_expiry_frozen_rows`'s
    UNION step so only the FILTER step (this module's actual target) is
    exercised, with the REAL `expiry_status` / `is_live_row_past_freeze_window`
    predicates running unmocked against a pinned `now_ist`."""
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session


@pytest.mark.asyncio
async def test_not_expiry_row_excluded_when_contract_independently_expired_past_freeze():
    """2026-09-29 second GOLDM finding (account ZJ6294): the row's own
    `captured_at` (2026-09-18) does NOT fall on its contract's expiry-day
    session (2026-09-25) — the REAL `expiry_status` classifies it
    `"not_expiry"`, same as any ordinary row (asserted directly below,
    not mocked). But by the pinned `now_ist` (2026-09-29) the contract HAS
    independently expired and its freeze boundary (mocked via
    `next_market_open_ist`, the only I/O `is_live_row_past_freeze_window`
    needs) has elapsed — the general-purpose second check in
    `_union_and_filter_expiry_frozen_rows` must exclude it anyway, exactly
    reproducing the real ZJ6294 failure chain rather than asserting on a
    fully-mocked predicate."""
    from backend.api.algo.expiry_freeze import expiry_status, is_live_row_past_freeze_window
    from backend.api.routes.positions import _union_and_filter_expiry_frozen_rows

    captured_at = _utc(2026, 9, 18, 10, 0)
    main_row = (
        "ZJ6294", "GOLDM26SEP148000PE", "MCX", 40, Decimal("120.00"),
        Decimal("4300.00"), Decimal("500.00"), Decimal("800.00"), "{}",
        captured_at, Decimal("4014.50"), None, None,
    )
    pinned_now = datetime(2026, 9, 29, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    # Sanity: the real (unmocked) classifier genuinely says "not_expiry"
    # for this row — proving this test exercises the actual gap, not a
    # stand-in assumption.
    real_status = await expiry_status("GOLDM26SEP148000PE", captured_at, "MCX", pinned_now)
    assert real_status == "not_expiry"

    mock_session = _empty_frozen_candidates_session()
    boundary_mock = AsyncMock(return_value=datetime(2026, 9, 26, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")))

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.api.algo.expiry_freeze.next_market_open_ist", boundary_mock),
    ):
        # Also sanity-check the real live-fetch predicate agrees, using the
        # same patched boundary — belt-and-braces against the filter call
        # inside _union_and_filter_expiry_frozen_rows silently no-op'ing.
        assert await is_live_row_past_freeze_window("GOLDM26SEP148000PE", "MCX", pinned_now) is True
        filtered = await _union_and_filter_expiry_frozen_rows([main_row], pinned_now)

    assert filtered == [], (
        "GOLDM row's own capture predates its expiry-day session (real "
        "expiry_status says 'not_expiry'), but the contract has since "
        "independently expired past the freeze boundary — must still be "
        "excluded by the general-purpose is_live_row_past_freeze_window "
        "backstop"
    )


@pytest.mark.asyncio
async def test_not_expiry_row_kept_when_contract_still_has_time_left():
    """Safety-check control case, exercised against the REAL predicates
    (not mocked) with a pinned `now_ist` still short of the contract's own
    expiry: a row captured while its contract's own expiry is still in the
    FUTURE relative to `now_ist` must NEVER be filtered by the new
    general-purpose check, no matter how stale the row's own capture is —
    this is the 'closed, not expired' case the module docstring explicitly
    protects. `is_live_row_past_freeze_window`'s fast no-I/O path returns
    False whenever `expiry >= now_ist.date()`, so `next_market_open_ist`
    must never even be awaited here — asserted explicitly below, proving
    the fast path (not merely the end result) is what fires."""
    from backend.api.algo.expiry_freeze import expiry_status
    from backend.api.routes.positions import _union_and_filter_expiry_frozen_rows

    captured_at = _utc(2026, 9, 18, 10, 0)
    main_row = (
        "ZJ6294", "GOLDM26SEP148000PE", "MCX", 40, Decimal("120.00"),
        Decimal("4300.00"), Decimal("500.00"), Decimal("800.00"), "{}",
        captured_at, Decimal("4014.50"), None, None,
    )
    # Pinned now_ist is BEFORE the contract's own 2026-09-25 expiry —
    # captured_at is stale (7 days before this pinned "now") but the
    # contract genuinely hasn't expired yet as of pinned_now.
    pinned_now = datetime(2026, 9, 20, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    real_status = await expiry_status("GOLDM26SEP148000PE", captured_at, "MCX", pinned_now)
    assert real_status == "not_expiry"

    mock_session = _empty_frozen_candidates_session()
    boundary_mock = AsyncMock()

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.api.algo.expiry_freeze.next_market_open_ist", boundary_mock),
    ):
        filtered = await _union_and_filter_expiry_frozen_rows([main_row], pinned_now)

    assert boundary_mock.await_count == 0, (
        "next_market_open_ist must never be awaited when the contract's "
        "own expiry hasn't happened yet relative to pinned now_ist — proves "
        "the no-I/O fast path fired, not merely that the row was kept"
    )
    assert filtered == [main_row], (
        "a row whose contract's own expiry hasn't happened yet must NEVER "
        "be excluded, regardless of how stale its own capture is"
    )


@pytest.mark.asyncio
async def test_not_expiry_row_kept_inside_freeze_window_after_expiry():
    """Optional third case from the safety-check matrix: pinned `now_ist`
    is AFTER the contract's own expiry but BEFORE the freeze boundary
    (next market open) — the row must still be kept, proving the general
    check distinguishes 'expired but still inside its freeze window' from
    'expired and past the boundary' using the REAL predicates."""
    from backend.api.routes.positions import _union_and_filter_expiry_frozen_rows

    captured_at = _utc(2026, 9, 18, 10, 0)
    main_row = (
        "ZJ6294", "GOLDM26SEP148000PE", "MCX", 40, Decimal("120.00"),
        Decimal("4300.00"), Decimal("500.00"), Decimal("800.00"), "{}",
        captured_at, Decimal("4014.50"), None, None,
    )
    # Saturday 2026-09-26 — after the Friday 2026-09-25 expiry, but the
    # freeze boundary (mocked next market open) hasn't arrived yet.
    pinned_now = datetime(2026, 9, 26, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    mock_session = _empty_frozen_candidates_session()
    boundary_mock = AsyncMock(return_value=datetime(2026, 9, 28, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")))

    with (
        patch("backend.api.database.async_session", return_value=mock_session),
        patch("backend.api.algo.expiry_freeze.next_market_open_ist", boundary_mock),
    ):
        filtered = await _union_and_filter_expiry_frozen_rows([main_row], pinned_now)

    assert filtered == [main_row], (
        "row must be kept while still inside its freeze window, even "
        "though the contract's own expiry has already passed"
    )


@pytest.mark.asyncio
async def test_ordinary_row_with_no_expiry_concept_unaffected():
    """Control: a plain equity row (no F&O expiry concept at all) run
    through the same direct-call path must never be touched by either the
    frozen/refresh_eligible branch or the new general-purpose backstop."""
    from backend.api.routes.positions import _union_and_filter_expiry_frozen_rows

    main_row = (
        "ZG0790", "RELIANCE", "NSE", 10, Decimal("2800.00"),
        Decimal("2850.00"), Decimal("500.00"), Decimal("500.00"), "{}",
        _utc(2026, 9, 1, 10, 0), Decimal("2800.00"), None, None,
    )
    pinned_now = datetime(2026, 9, 29, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))

    mock_session = _empty_frozen_candidates_session()

    with patch("backend.api.database.async_session", return_value=mock_session):
        filtered = await _union_and_filter_expiry_frozen_rows([main_row], pinned_now)

    assert filtered == [main_row]


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
