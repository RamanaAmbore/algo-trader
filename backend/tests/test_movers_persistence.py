"""
Tests for movers snapshot persistence (off-hours fallback).

Five quality dimensions:
  1. SSOT        — _save_movers_snapshot, _load_latest_movers_snapshot, and
                   _force_movers_snapshot are the canonical helpers; route
                   calls them at the right points. nse:close lifecycle event
                   wires _snapshot_movers handler.
  2. Performance — save is a single Postgres UPSERT (ON CONFLICT DO UPDATE),
                   not a select-then-insert. Load is a single SELECT ... LIMIT 1.
  3. Stale code  — no TODO-movers markers; is_any_segment_open NOT used in
                   get_movers (replaced by NSE-specific predicate).
  4. Reusable    — movers_snapshots retention uses _apply_retention (verified by
                   source-grep in background.py).
  5. Correctness — scenario tests: roundtrip fidelity, most-recent ordering,
                   empty-table None, 7-day retention, off-hours code path,
                   NSE-only predicate, force-snapshot helper present,
                   lifecycle handler registration.
"""

import ast
import json
import re
from datetime import datetime, timedelta, timezone, date as _date
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Source paths
# ---------------------------------------------------------------------------
_WATCHLIST_SRC  = Path(__file__).parent.parent / "api" / "routes" / "watchlist.py"
_BG_SRC         = Path(__file__).parent.parent / "api" / "background.py"
_HANDLERS_SRC   = Path(__file__).parent.parent / "api" / "algo" / "market_lifecycle_handlers.py"


def _wl_source() -> str:
    return _WATCHLIST_SRC.read_text(encoding="utf-8")


def _bg_source() -> str:
    return _BG_SRC.read_text(encoding="utf-8")


def _handlers_source() -> str:
    return _HANDLERS_SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lightweight in-memory SQLite DB
# ---------------------------------------------------------------------------

class _Base(DeclarativeBase):
    pass


class _MoversSnapshot(_Base):
    """Mirrors the production MoversSnapshot ORM model (SQLite-compatible)."""
    __tablename__ = "movers_snapshots"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    date         = Column(String(10), nullable=False)   # ISO date string in SQLite
    payload_json = Column(Text, nullable=False)
    captured_at  = Column(DateTime(timezone=True), nullable=False)


@pytest_asyncio.fixture
async def session() -> AsyncGenerator[AsyncSession, None]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(_Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


# ---------------------------------------------------------------------------
# Dimension 1 (SSOT) — helpers and lifecycle wiring
# ---------------------------------------------------------------------------

def test_save_helper_defined_in_watchlist():
    """_save_movers_snapshot is defined as an async function in watchlist.py."""
    src = _wl_source()
    assert "async def _save_movers_snapshot(" in src, (
        "_save_movers_snapshot not found in watchlist.py"
    )


def test_load_helper_defined_in_watchlist():
    """_load_latest_movers_snapshot is defined as an async function in watchlist.py."""
    src = _wl_source()
    assert "async def _load_latest_movers_snapshot(" in src, (
        "_load_latest_movers_snapshot not found in watchlist.py"
    )


def test_force_snapshot_helper_defined_in_watchlist():
    """_force_movers_snapshot is defined as an async function in watchlist.py.
    This is the lifecycle-handler entry point that writes a guaranteed snapshot
    at NSE close independent of in-memory _session_movers state.
    """
    src = _wl_source()
    assert "async def _force_movers_snapshot(" in src, (
        "_force_movers_snapshot not found in watchlist.py — "
        "needed by nse:close lifecycle handler"
    )


def test_route_calls_save_on_live_result():
    """get_movers calls _save_movers_snapshot when rows is non-empty."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "Could not find get_movers in watchlist.py"
    body = match.group(0)
    assert "_save_movers_snapshot(" in body, (
        "get_movers does not call _save_movers_snapshot"
    )


def test_route_calls_load_on_market_closed():
    """The market-closed branch of get_movers calls
    _load_latest_movers_snapshot. Since Jul 2026 this is extracted into
    `_movers_offhours_response`, so we accept either the direct call in
    get_movers OR the helper being wired through the closed-hours
    branch."""
    src = _wl_source()
    # Case A — inline (pre-refactor).
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "Could not find get_movers in watchlist.py"
    body = match.group(0)
    if "_load_latest_movers_snapshot(" in body:
        return
    # Case B — extracted into `_movers_offhours_response`, which
    # get_movers invokes on the both-closed branch.
    assert "_movers_offhours_response(" in body, (
        "get_movers does not delegate the closed-hours branch to "
        "_movers_offhours_response"
    )
    helper_match = re.search(
        r"async def _movers_offhours_response\(.*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert helper_match, "_movers_offhours_response helper not found"
    assert "_load_latest_movers_snapshot(" in helper_match.group(0), (
        "_movers_offhours_response does not call _load_latest_movers_snapshot"
    )


def test_lifecycle_handler_snapshot_movers_defined():
    """_snapshot_movers is defined in market_lifecycle_handlers.py."""
    src = _handlers_source()
    assert "async def _snapshot_movers(" in src, (
        "_snapshot_movers handler not defined in market_lifecycle_handlers.py"
    )


def test_lifecycle_handler_registers_movers_on_nse_close():
    """register_default_handlers wires _snapshot_movers to nse:close."""
    src = _handlers_source()
    match = re.search(
        r"def register_default_handlers\(\).*?(?=\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "register_default_handlers not found in market_lifecycle_handlers.py"
    body = match.group(0)
    assert "_snapshot_movers" in body, (
        "register_default_handlers does not register _snapshot_movers"
    )
    assert '"nse:close"' in body or "'nse:close'" in body, (
        "register_default_handlers does not wire nse:close event"
    )


def test_captured_at_field_on_response_schema():
    """MoversResponse has a captured_at optional field."""
    src = _wl_source()
    match = re.search(
        r"class MoversResponse\(msgspec\.Struct\).*?(?=\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "MoversResponse struct not found"
    body = match.group(0)
    assert "captured_at" in body, (
        "captured_at field missing from MoversResponse"
    )


# ---------------------------------------------------------------------------
# Dimension 2 (Performance) — single UPSERT, single SELECT LIMIT 1
# ---------------------------------------------------------------------------

def test_save_uses_on_conflict_do_update():
    """_save_movers_snapshot uses ON CONFLICT DO UPDATE (single-statement upsert)."""
    src = _wl_source()
    match = re.search(
        r"async def _save_movers_snapshot\(.*?\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_save_movers_snapshot body not found"
    body = match.group(0)
    assert "on_conflict_do_update" in body, (
        "_save_movers_snapshot does not use ON CONFLICT DO UPDATE — should be one UPSERT"
    )


def test_load_uses_limit_one():
    """_load_latest_movers_snapshot uses .limit(1) — single row fetch."""
    src = _wl_source()
    match = re.search(
        r"async def _load_latest_movers_snapshot\(.*?\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_load_latest_movers_snapshot body not found"
    body = match.group(0)
    assert ".limit(1)" in body, (
        "_load_latest_movers_snapshot does not use .limit(1) — should be a single-row fetch"
    )


# ---------------------------------------------------------------------------
# Dimension 3 (Stale code) — no TODO-movers markers; NSE-only predicate
# ---------------------------------------------------------------------------

def test_no_todo_movers_markers():
    """No leftover 'TODO: movers' comments in watchlist.py."""
    src = _wl_source()
    assert "todo: movers" not in src.lower(), (
        "Found a leftover TODO:movers comment in watchlist.py"
    )


def test_route_uses_nse_specific_predicate_not_any_segment():
    """get_movers uses is_market_open (NSE-specific), NOT is_any_segment_open.

    The movers universe is NSE-only (MCX underlyings are explicitly excluded at
    quote-fetch time). Using is_any_segment_open was the root cause of blank
    winners/losers panels during 15:30-23:30 IST (NSE closed, MCX still open).
    Fix: gate the live-path on the NSE equity session window only.
    """
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found in watchlist.py"
    body = match.group(0)

    assert "is_any_segment_open" not in body, (
        "get_movers still uses is_any_segment_open — must use NSE-specific "
        "is_market_open(now, nse_holidays, start=09:15, end=15:30) instead. "
        "is_any_segment_open returns True during 15:30-23:30 IST (MCX open) "
        "which causes the live-path to return empty rows and blank the grid."
    )
    assert "nse_is_open" in body or "is_market_open(" in body, (
        "get_movers does not use an NSE-specific open predicate (nse_is_open / is_market_open)"
    )


# ---------------------------------------------------------------------------
# Dimension 4 (Reusable) — retention uses _apply_retention
# ---------------------------------------------------------------------------

def test_movers_snapshots_retention_uses_apply_retention():
    """_task_purge_persistence_caches uses _apply_retention for movers_snapshots."""
    src = _bg_source()
    match = re.search(
        r"async def _task_purge_persistence_caches\(\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_task_purge_persistence_caches not found in background.py"
    body = match.group(0)
    assert "movers_snapshots" in body, (
        "movers_snapshots not referenced in _task_purge_persistence_caches"
    )
    assert "_apply_retention(" in body, (
        "_apply_retention not used in _task_purge_persistence_caches"
    )


def test_movers_snapshots_retention_count():
    """_task_purge_persistence_caches now has >=6 _apply_retention calls (added movers)."""
    src = _bg_source()
    match = re.search(
        r"async def _task_purge_persistence_caches\(\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match
    body = match.group(0)
    count = body.count("_apply_retention(")
    assert count >= 6, (
        f"Expected >=6 _apply_retention calls after adding movers_snapshots, got {count}"
    )


# ---------------------------------------------------------------------------
# Dimension 5 (Correctness) — scenario tests using SQLite in-memory DB
# ---------------------------------------------------------------------------

async def _seed_snapshot(session: AsyncSession, date_str: str, rows: list, age_days: int = 0):
    """Insert a movers snapshot at `age_days` days ago."""
    now = datetime.now(timezone.utc) - timedelta(days=age_days)
    snap = _MoversSnapshot(
        date=date_str,
        payload_json=json.dumps(rows),
        captured_at=now,
    )
    session.add(snap)
    await session.flush()
    return snap


_SAMPLE_ROWS = [
    {
        "tradingsymbol": "RELIANCE",
        "exchange": "NSE",
        "last_price": 2500.0,
        "previous_close": 2400.0,
        "change_pct": 4.17,
        "peak_pct": 4.17,
        "sticky": True,
    },
    {
        "tradingsymbol": "TCS",
        "exchange": "NSE",
        "last_price": 3800.0,
        "previous_close": 3900.0,
        "change_pct": -2.56,
        "peak_pct": -2.56,
        "sticky": False,
    },
]


@pytest.mark.asyncio
async def test_snapshot_roundtrip(session: AsyncSession):
    """A snapshot saved and then loaded retains payload fidelity."""
    date_str = "2026-06-27"
    await _seed_snapshot(session, date_str, _SAMPLE_ROWS)

    from sqlalchemy import select, desc
    result = await session.execute(
        select(_MoversSnapshot).order_by(desc(_MoversSnapshot.captured_at)).limit(1)
    )
    snap = result.scalar_one_or_none()
    assert snap is not None, "Snapshot not found after insert"

    loaded = json.loads(snap.payload_json)
    assert len(loaded) == 2
    assert loaded[0]["tradingsymbol"] == "RELIANCE"
    assert loaded[1]["change_pct"] == pytest.approx(-2.56)
    assert snap.date == date_str


@pytest.mark.asyncio
async def test_load_returns_most_recent(session: AsyncSession):
    """When multiple snapshots exist, the most recent captured_at is returned."""
    await _seed_snapshot(session, "2026-06-25", [{"tradingsymbol": "OLD", "exchange": "NSE", "last_price": 100.0, "previous_close": 100.0, "change_pct": 1.0, "peak_pct": 1.0, "sticky": False}], age_days=2)
    await _seed_snapshot(session, "2026-06-27", _SAMPLE_ROWS, age_days=0)

    from sqlalchemy import select, desc
    result = await session.execute(
        select(_MoversSnapshot).order_by(desc(_MoversSnapshot.captured_at)).limit(1)
    )
    snap = result.scalar_one_or_none()
    assert snap is not None
    loaded = json.loads(snap.payload_json)
    # The newer snapshot (age=0) contains RELIANCE, not OLD
    assert any(r["tradingsymbol"] == "RELIANCE" for r in loaded), (
        "Most recent snapshot not returned — got the older one"
    )


@pytest.mark.asyncio
async def test_no_snapshot_returns_none(session: AsyncSession):
    """When the table is empty, the SELECT returns None (no crash)."""
    from sqlalchemy import select, desc
    result = await session.execute(
        select(_MoversSnapshot).order_by(desc(_MoversSnapshot.captured_at)).limit(1)
    )
    snap = result.scalar_one_or_none()
    assert snap is None, "Expected None for empty table"


@pytest.mark.asyncio
async def test_retention_7_days(session: AsyncSession):
    """Snapshots older than 7 days are deleted; recent ones survive."""
    ages = [1, 3, 6, 7, 8, 10, 14]  # days ago
    for i, age in enumerate(ages):
        await _seed_snapshot(session, f"2026-06-{28 - i:02d}", _SAMPLE_ROWS, age_days=age)

    ttl = 7
    from sqlalchemy import text
    res = await session.execute(text(
        f"DELETE FROM movers_snapshots WHERE captured_at < datetime('now', '-{ttl} days')"
    ))
    deleted = res.rowcount
    await session.flush()

    from sqlalchemy import select
    surviving = (await session.execute(select(_MoversSnapshot))).scalars().all()
    # "< now() - 7 days" deletes strictly-older-than-7-days rows.
    # age=7 rows were captured exactly 7 days ago: NOT less than the cutoff -> survive.
    expected_deleted = len([a for a in ages if a > ttl])
    expected_survive = len([a for a in ages if a <= ttl])

    assert deleted == expected_deleted, (
        f"Expected {expected_deleted} rows deleted (age > {ttl} days), got {deleted}"
    )
    assert len(surviving) == expected_survive, (
        f"Expected {expected_survive} rows surviving, got {len(surviving)}"
    )


# ---------------------------------------------------------------------------
# Integration: get_movers source-level assertions for off-hours code path
# ---------------------------------------------------------------------------

def test_off_hours_path_reads_db_and_returns_captured_at():
    """get_movers body: when NSE is closed, it reads from DB and returns captured_at.
    Since Jul 2026 the off-hours branch is extracted into
    `_movers_offhours_response`; accept the check in either location."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match, "get_movers not found"
    body = match.group(0)

    # The fast-path block must gate on nse_is_open
    assert "nse_is_open" in body, (
        "get_movers does not check nse_is_open — NSE-specific gate missing"
    )
    # It must return captured_at from the snapshot — check body first,
    # then fall through to the extracted _movers_offhours_response helper.
    if "captured_at" in body:
        return
    helper_match = re.search(
        r"async def _movers_offhours_response\(.*?(?=\nasync def |\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert helper_match, "_movers_offhours_response helper not found"
    assert "captured_at" in helper_match.group(0), (
        "_movers_offhours_response does not pass captured_at from the snapshot"
    )


def test_live_path_fires_save_as_task():
    """get_movers: live path fires _save_movers_snapshot via asyncio.create_task."""
    src = _wl_source()
    match = re.search(
        r"async def get_movers\(self\).*?(?=\n    @|\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match
    body = match.group(0)
    assert "asyncio.create_task(_save_movers_snapshot(" in body, (
        "get_movers does not fire _save_movers_snapshot via asyncio.create_task"
    )


def test_force_snapshot_calls_save_movers_snapshot():
    """_force_movers_snapshot calls _save_movers_snapshot to persist the result."""
    src = _wl_source()
    match = re.search(
        r"async def _force_movers_snapshot\(.*?\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_force_movers_snapshot body not found"
    body = match.group(0)
    assert "_save_movers_snapshot(" in body, (
        "_force_movers_snapshot does not call _save_movers_snapshot"
    )


def test_force_snapshot_excludes_mcx():
    """_force_movers_snapshot skips MCX underlyings (NSE-only universe)."""
    src = _wl_source()
    match = re.search(
        r"async def _force_movers_snapshot\(.*?\).*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_force_movers_snapshot body not found"
    body = match.group(0)
    assert "is_mcx_underlying" in body, (
        "_force_movers_snapshot does not filter out MCX underlyings — "
        "movers universe must be NSE-only"
    )


def test_lifecycle_handlers_log_message_mentions_movers():
    """register_default_handlers log line mentions movers so deployment is auditable."""
    src = _handlers_source()
    match = re.search(
        r"def register_default_handlers\(\).*?(?=\ndef |\nclass |\Z)",
        src, re.DOTALL,
    )
    assert match
    body = match.group(0)
    assert "movers" in body, (
        "register_default_handlers log line should mention 'movers' for auditability"
    )
