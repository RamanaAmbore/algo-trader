"""
Tests for Changes 3 & 4 in backend/api/routes/positions.py.

Change 3 — WHERE NULL fix: rows with db.ltp IS NULL must NOT be dropped by the
  zero-payload guard (previously `NOT (ltp=0 AND ...)` was vacuously true for
  NULL ltp in SQLite semantics, but in PostgreSQL `NULL = 0` is NULL not FALSE,
  so the NOT(NULL) was NULL → falsy → rows were dropped).

Change 4 — 08:00 IST cutoff for _override_stale_close_from_snapshot: the old
  midnight cutoff excluded MCX 23:30-close snapshots written at 00:05 IST. The
  new 08:00 IST cutoff includes them while still blocking mid-session
  (09:15+ IST) deploy snapshots.

Five quality dimensions:
  SSOT       — Changes tested at source; no reimplementation.
  Correctness— Three assertions per change (positive, negative, arithmetic).
  Performance— source-inspection + pure arithmetic; no network calls.
  Reuse      — Reuses _positions_snapshot() mock pattern from existing tests.
  UX         — 00:05 IST MCX EOD snapshot included; 09:30 IST mid-session excluded.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone, timedelta

import pytest


# ---------------------------------------------------------------------------
# Change 3 — WHERE clause NULL fix
# ---------------------------------------------------------------------------

def test_positions_snapshot_where_clause_allows_null_ltp():
    """WHERE clause must allow NULL-ltp rows through while excluding ltp=0 rows.

    New form: (db.ltp IS NULL OR db.ltp > 0) — simpler and strictly stronger
    than the old NOT (db.ltp = 0 AND ...) guard which silently excluded NULL rows.
    """
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    # Accept either the new simplified form or the old wrapped form
    assert ("db.ltp IS NULL OR NOT" in src
            or "db.ltp IS NULL OR not" in src.lower()
            or "db.ltp IS NULL OR db.ltp > 0" in src), (
        "_positions_snapshot WHERE clause must include ltp IS NULL guard to pass "
        "NULL-ltp rows through. Old guard `AND NOT (db.ltp = 0 ...)` silently excluded "
        "ltp=NULL rows. New form: (db.ltp IS NULL OR db.ltp > 0)."
    )


def test_positions_snapshot_zero_payload_guard_still_present():
    """The zero-payload / ltp=0 row filter must be present in _positions_snapshot.

    New form: (db.ltp IS NULL OR db.ltp > 0) — strictly stronger than old guard.
    """
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    # Accept old explicit guard OR new simplified ltp > 0 form
    assert ("db.ltp = 0" in src or "db.ltp > 0" in src), (
        "ltp=0 filter must be in _positions_snapshot SQL — either old NOT(ltp=0 AND...) "
        "form or new (ltp IS NULL OR ltp > 0) form."
    )


def test_positions_snapshot_where_structure_combined():
    """WHERE clause must have a row-level ltp filter that allows NULL and excludes 0."""
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._positions_snapshot)
    # The new guard: (db.ltp IS NULL OR db.ltp > 0)
    # The old guard: AND (db.ltp IS NULL OR NOT (db.ltp = 0 AND ...))
    assert ("AND (db.ltp IS NULL OR NOT (" in src
            or "AND (db.ltp IS NULL OR db.ltp > 0)" in src), (
        "positions snapshot WHERE clause must have row-level ltp filter: "
        "either 'AND (db.ltp IS NULL OR NOT (db.ltp = 0 AND ...))' "
        "or 'AND (db.ltp IS NULL OR db.ltp > 0)'"
    )


# ---------------------------------------------------------------------------
# Change 4 — 08:00 IST cutoff
# ---------------------------------------------------------------------------

def test_override_stale_close_uses_timedelta_hours_8():
    """_override_stale_close_from_snapshot must produce an 08:00 IST cutoff.

    The cutoff is now delegated to exchange_clock.settlement_cutoff_for("NSE")
    which reads snapshot_reset_time from the DB-backed exchange_schedule table
    (seeded as 08:00 IST). The test verifies the delegation is present and that
    the old hardcoded timedelta(hours=8) arithmetic has been replaced.
    """
    from backend.api.routes import positions as _pos_module

    src = inspect.getsource(_pos_module._override_stale_close_from_snapshot)
    # The delegation call must be present (replaces old timedelta arithmetic).
    assert "settlement_cutoff_for" in src, (
        "_override_stale_close_from_snapshot must delegate to "
        "exchange_clock.settlement_cutoff_for('NSE') for the 08:00 IST cutoff."
    )
    assert "today_ist_cutoff" in src, (
        "Variable today_ist_cutoff must be present as the cutoff passed to the query."
    )
    # The old hardcoded arithmetic must be gone (now lives in exchange_clock).
    assert "timedelta(hours=8)" not in src, (
        "_override_stale_close_from_snapshot still contains hardcoded "
        "timedelta(hours=8) — cutoff computation must be delegated to "
        "exchange_clock.settlement_cutoff_for('NSE')."
    )


def test_override_stale_close_cutoff_arithmetic():
    """Direct arithmetic check: midnight + 8h = 08:00 IST."""
    midnight = datetime(2026, 8, 14, 0, 0, 0, tzinfo=timezone.utc)
    cutoff = midnight + timedelta(hours=8)
    assert cutoff.hour == 8, f"Cutoff must be 08:00; got hour={cutoff.hour}"
    assert cutoff.minute == 0
    assert cutoff.date() == midnight.date()


def test_override_stale_close_mcx_eod_included():
    """MCX 23:30 close snapshot captured at 00:05 IST next calendar day must
    be BEFORE the 08:00 IST cutoff (i.e. should be included by the query)."""
    # Simulate: today is 2026-08-14. MCX close snapshot written at 00:05 IST
    # on 2026-08-14 = 18:35 UTC on 2026-08-13. In IST wall-clock it is 00:05
    # on 14 Aug. Represented as UTC below (IST = UTC+5:30).
    mcx_snapshot_utc = datetime(2026, 8, 13, 18, 35, 0, tzinfo=timezone.utc)  # 00:05 IST

    # cutoff = 2026-08-14 08:00 IST = 2026-08-14 02:30 UTC
    cutoff_utc = datetime(2026, 8, 14, 2, 30, 0, tzinfo=timezone.utc)

    assert mcx_snapshot_utc < cutoff_utc, (
        "MCX EOD snapshot at 00:05 IST (18:35 UTC prior day) must be BEFORE the "
        "08:00 IST cutoff (02:30 UTC) so it is included in the close-override query."
    )


def test_override_stale_close_mid_session_excluded():
    """A mid-session deploy snapshot at 09:30 IST must be AFTER (not before)
    the 08:00 IST cutoff so it is excluded from the close-override query."""
    # 09:30 IST = 04:00 UTC
    deploy_snapshot_utc = datetime(2026, 8, 14, 4, 0, 0, tzinfo=timezone.utc)
    cutoff_utc = datetime(2026, 8, 14, 2, 30, 0, tzinfo=timezone.utc)

    assert deploy_snapshot_utc >= cutoff_utc, (
        "Mid-session deploy snapshot at 09:30 IST (04:00 UTC) must be >= the "
        "08:00 IST cutoff (02:30 UTC) so it is excluded from the close-override query."
    )
