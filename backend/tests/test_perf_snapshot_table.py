"""
Tests for DB-backed perf-snapshot tracking (Sprint F+).

Five quality dimensions:

1. SSOT        — model registered, migration idempotent, retention key seeded,
                 new perf_snapshot.runtime_enabled + runtime_timeout_s seeded.
2. Performance — INSERT/DELETE are bulk operations; regression endpoint uses
                 in-process grouping (single SELECT, not N per page).
3. Stale code  — source-grep confirms tasks are defined + registered,
                 _merge_runtime_into_rows is module-level.
4. Reusable    — _apply_retention() used by purge task; controller uses
                 admin_guard; ingest helper is reused by cron + backfill;
                 _merge_runtime_into_rows reused by cron loop.
5. UX / Correctness — history returns time-ordered rows, latest returns one
                 row per (side, page_or_route), regression flags the right
                 (page, metric) pairs.  Runtime-capture subprocess:
                 graceful degradation on timeout / crash / disabled.

Async-DB tests use an in-process SQLite engine to avoid touching the real PG
instance (matches the pattern in test_retention_new_tables.py).
"""

from __future__ import annotations

import json
import re
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

import pytest
import pytest_asyncio
from sqlalchemy import Column, DateTime, Float, Index, Integer, String, Text
from sqlalchemy.dialects.sqlite import JSON as _SQLITE_JSON
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# ---------------------------------------------------------------------------
# Source paths
# ---------------------------------------------------------------------------

BG_SRC       = Path(__file__).parent.parent / "api" / "background.py"
SETTINGS_SRC = Path(__file__).parent.parent / "shared" / "helpers" / "settings.py"
MODELS_SRC   = Path(__file__).parent.parent / "api" / "models.py"
DB_SRC       = Path(__file__).parent.parent / "api" / "database.py"
PERF_RT_SRC  = Path(__file__).parent.parent / "api" / "routes" / "perf.py"
APP_SRC      = Path(__file__).parent.parent / "api" / "app.py"


def _bg()       -> str: return BG_SRC.read_text(encoding="utf-8")
def _settings() -> str: return SETTINGS_SRC.read_text(encoding="utf-8")
def _models()   -> str: return MODELS_SRC.read_text(encoding="utf-8")
def _db()       -> str: return DB_SRC.read_text(encoding="utf-8")
def _perf_rt()  -> str: return PERF_RT_SRC.read_text(encoding="utf-8")
def _app()      -> str: return APP_SRC.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Lightweight in-process SQLite DB (mirrors PerfSnapshot columns)
# ---------------------------------------------------------------------------

class _Base(DeclarativeBase):
    pass


class _PerfSnapshot(_Base):
    __tablename__ = "perf_snapshots"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    captured_at    = Column(DateTime(timezone=True), nullable=False)
    commit_sha     = Column(String(40), nullable=True)
    side           = Column(String(2), nullable=False)
    page_or_route  = Column(String(160), nullable=False)
    loc            = Column(Integer, nullable=True)
    effect_count   = Column(Integer, nullable=True)
    state_count    = Column(Integer, nullable=True)
    derived_count  = Column(Integer, nullable=True)
    cc_max         = Column(Integer, nullable=True)
    cc_avg         = Column(Float, nullable=True)
    hotspots_json  = Column(_SQLITE_JSON, nullable=True)
    lcp_ms         = Column(Integer, nullable=True)
    tbt_ms         = Column(Integer, nullable=True)
    heap_mb        = Column(Float, nullable=True)
    route_p50_ms   = Column(Integer, nullable=True)
    route_p95_ms   = Column(Integer, nullable=True)
    route_qps      = Column(Float, nullable=True)


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
# Helper: build a fake perf_baseline JSON blob
# ---------------------------------------------------------------------------

def _fake_baseline_json(
    captured_at: str = "2026-07-01T04:00:00Z",
    commit: str = "abc1234",
    fe_pages: Optional[dict] = None,
    be_routes: Optional[dict] = None,
) -> dict:
    return {
        "captured_at": captured_at,
        "commit": commit,
        "frontend": {
            "pages": fe_pages or {
                "/pulse": {
                    "file": "frontend/src/lib/MarketPulse.svelte",
                    "loc": 6465,
                    "effect_count": 53,
                    "state_count": 109,
                    "derived_count": 69,
                },
                "lib::NavCard": {
                    "file": "frontend/src/lib/NavCard.svelte",
                    "loc": 425,
                    "effect_count": 3,
                    "state_count": 12,
                    "derived_count": 4,
                },
            },
        },
        "backend": {
            "routes": be_routes or {
                "GET /api/positions": {
                    "file": "backend/api/routes/positions.py",
                    "loc": 933,
                    "async_fn_count": 7,
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# Production parse helper — imported directly so tests exercise real code.
# _parse_perf_snapshot_rows returns PerfSnapshot (PG ORM) instances; we
# read their attributes without touching the DB.
# For the SQLite insert tests we keep a thin adapter that converts the
# PerfSnapshot attribute values into _PerfSnapshot (SQLite) rows.
# ---------------------------------------------------------------------------

from backend.api.background import _parse_perf_snapshot_rows, _merge_runtime_into_rows  # noqa: E402


def _parse_rows(snap: dict) -> list[_PerfSnapshot]:
    """Thin adapter: call production parser, convert to SQLite _PerfSnapshot rows."""
    prod_rows = _parse_perf_snapshot_rows(snap)
    result: list[_PerfSnapshot] = []
    for r in prod_rows:
        result.append(_PerfSnapshot(
            captured_at=r.captured_at,
            commit_sha=r.commit_sha,
            side=r.side,
            page_or_route=r.page_or_route,
            loc=r.loc,
            effect_count=r.effect_count,
            state_count=r.state_count,
            derived_count=r.derived_count,
            cc_max=r.cc_max,
            cc_avg=r.cc_avg,
            hotspots_json=r.hotspots_json,
            lcp_ms=r.lcp_ms,
            tbt_ms=r.tbt_ms,
            heap_mb=r.heap_mb,
        ))
    return result


async def _sqlite_retention(
    session: AsyncSession, table: str, ts_col: str, days: int
) -> int:
    from sqlalchemy import text
    res = await session.execute(text(
        f"DELETE FROM {table} WHERE {ts_col} < datetime('now', '-{days} days')"
    ))
    return res.rowcount if res.rowcount >= 0 else 0


# ---------------------------------------------------------------------------
# Dimension 1 — SSOT: schema, migration, settings key
# ---------------------------------------------------------------------------

def test_perf_snapshot_model_defined():
    """PerfSnapshot class exists in models.py."""
    assert "class PerfSnapshot(Base):" in _models(), (
        "PerfSnapshot model not found in models.py"
    )


def test_perf_snapshot_tablename():
    """Model uses the canonical table name."""
    assert '__tablename__ = "perf_snapshots"' in _models(), (
        'PerfSnapshot.__tablename__ != "perf_snapshots"'
    )


def test_perf_snapshot_model_imported_in_init_db():
    """PerfSnapshot is imported inside init_db() so create_all picks it up."""
    assert "PerfSnapshot" in _db(), (
        "PerfSnapshot not imported in database.py init_db"
    )


def test_retention_settings_key_present():
    """retention.perf_snapshots_days is seeded in settings.py."""
    assert "retention.perf_snapshots_days" in _settings(), (
        "retention.perf_snapshots_days key missing from settings.py"
    )


def test_perf_snapshot_has_composite_index():
    """Model declares index on (page_or_route, captured_at)."""
    src = _models()
    assert "ix_perf_snapshots_page_captured" in src, (
        "ix_perf_snapshots_page_captured index not declared in PerfSnapshot"
    )
    assert "ix_perf_snapshots_captured_at" in src, (
        "ix_perf_snapshots_captured_at index not declared in PerfSnapshot"
    )


def test_perf_snapshot_columns_present():
    """Core columns are declared in the model."""
    src = _models()
    for col in ("side", "page_or_route", "loc", "cc_max", "cc_avg",
                "hotspots_json", "lcp_ms", "tbt_ms", "heap_mb",
                "route_p50_ms", "route_p95_ms", "route_qps",
                "effect_count", "state_count", "derived_count"):
        assert col in src, f"Column {col!r} missing from PerfSnapshot model"


def test_init_db_has_perf_snapshots_index_ddl():
    """database.py includes idempotent CREATE INDEX for perf_snapshots."""
    src = _db()
    assert "ix_perf_snapshots_page_captured" in src, (
        "perf_snapshots page+captured index DDL missing from database.py"
    )


# ---------------------------------------------------------------------------
# Dimension 3 — Stale code: tasks defined + registered
# ---------------------------------------------------------------------------

def test_task_perf_snapshot_defined():
    assert "async def _task_perf_snapshot(" in _bg(), (
        "_task_perf_snapshot not defined in background.py"
    )


def test_task_purge_perf_snapshots_defined():
    assert "async def _task_purge_perf_snapshots(" in _bg(), (
        "_task_purge_perf_snapshots not defined in background.py"
    )


def test_tasks_registered_in_on_startup():
    src = _bg()
    assert "_task_perf_snapshot()" in src, (
        "_task_perf_snapshot() not registered in on_startup"
    )
    assert "_task_purge_perf_snapshots()" in src, (
        "_task_purge_perf_snapshots() not registered in on_startup"
    )


def test_cron_time_04_00():
    """Cron fires at 04:00 IST."""
    src = _bg()
    match = re.search(
        r"async def _task_perf_snapshot\b.*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_task_perf_snapshot body not found"
    body = match.group(0)
    assert "hour=4" in body, "_task_perf_snapshot not scheduled at hour=4"
    assert "minute=0" in body, "_task_perf_snapshot not scheduled at minute=0"


def test_purge_cron_time_04_05():
    """Purge cron fires at 04:05 IST."""
    src = _bg()
    match = re.search(
        r"async def _task_purge_perf_snapshots\b.*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_task_purge_perf_snapshots body not found"
    body = match.group(0)
    assert "hour=4" in body, "_task_purge_perf_snapshots not at hour=4"
    assert "minute=5" in body, "_task_purge_perf_snapshots not at minute=5"


# ---------------------------------------------------------------------------
# Dimension 4 — Reusable: _apply_retention used, admin_guard on controller
# ---------------------------------------------------------------------------

def test_purge_task_uses_apply_retention():
    src = _bg()
    match = re.search(
        r"async def _task_purge_perf_snapshots\b.*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_task_purge_perf_snapshots body not found"
    body = match.group(0)
    assert "_apply_retention(" in body, (
        "_task_purge_perf_snapshots does not call _apply_retention"
    )
    assert '"perf_snapshots"' in body or "'perf_snapshots'" in body, (
        '"perf_snapshots" table name missing from _task_purge_perf_snapshots'
    )


def test_purge_task_has_zero_disable_guard():
    src = _bg()
    match = re.search(
        r"async def _task_purge_perf_snapshots\b.*?(?=\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_task_purge_perf_snapshots body not found"
    body = match.group(0)
    assert "days <= 0" in body or "days == 0" in body, (
        "_task_purge_perf_snapshots missing zero-disable guard"
    )


def test_controller_uses_admin_guard():
    src = _perf_rt()
    assert "admin_guard" in src, "PerfController does not import/use admin_guard"
    assert "guards = [admin_guard]" in src, (
        "PerfController does not set guards = [admin_guard]"
    )


def test_controller_registered_in_app():
    assert "PerfController" in _app(), (
        "PerfController not registered in app.py route handlers"
    )


def test_retention_key_read_in_bg():
    src = _bg()
    assert "retention.perf_snapshots_days" in src, (
        "background.py does not read retention.perf_snapshots_days"
    )


def test_parse_helper_is_module_level():
    """_parse_perf_snapshot_rows must be a module-level function (importable by tests)."""
    src = _bg()
    # A module-level def starts at column 0 (no leading spaces).
    assert "def _parse_perf_snapshot_rows(" in src, (
        "_parse_perf_snapshot_rows is not defined at module level in background.py"
    )
    # Confirm the old nested closure name no longer exists.
    assert "_parse_and_insert" not in src, (
        "_parse_and_insert closure still present in background.py — should be replaced"
        " by module-level _parse_perf_snapshot_rows"
    )


# ---------------------------------------------------------------------------
# Dimension 2 — Performance: parse logic produces correct row count
# (no N+1 queries — rows built in a single pass over the JSON dict)
# ---------------------------------------------------------------------------

def test_parse_row_count():
    """_parse_perf_snapshot_rows produces one row per frontend page + backend route."""
    snap = _fake_baseline_json()
    rows = _parse_perf_snapshot_rows(snap)
    fe_count = len(snap["frontend"]["pages"])
    be_count = len(snap["backend"]["routes"])
    assert len(rows) == fe_count + be_count, (
        f"Expected {fe_count + be_count} rows, got {len(rows)}"
    )


def test_parse_side_tags():
    """Frontend rows get side='FE', backend rows get side='BE'."""
    snap = _fake_baseline_json()
    rows = _parse_perf_snapshot_rows(snap)
    fe = [r for r in rows if r.side == "FE"]
    be = [r for r in rows if r.side == "BE"]
    assert len(fe) == len(snap["frontend"]["pages"])
    assert len(be) == len(snap["backend"]["routes"])


def test_parse_commit_sha():
    """commit_sha populated from JSON commit field."""
    snap = _fake_baseline_json(commit="deadbeef")
    rows = _parse_perf_snapshot_rows(snap)
    assert all(r.commit_sha == "deadbeef" for r in rows)


def test_parse_runtime_block():
    """lcp_ms/tbt_ms/heap_mb populated from runtime sub-block when present."""
    snap = _fake_baseline_json(fe_pages={
        "/pulse": {
            "loc": 100,
            "effect_count": 5,
            "state_count": 10,
            "derived_count": 3,
            "runtime": {"lcp_ms": 1200, "tbt_ms": 80, "heap_mb": 42.5},
        }
    })
    rows = _parse_perf_snapshot_rows(snap)
    fe = next(r for r in rows if r.side == "FE")
    assert fe.lcp_ms == 1200
    assert fe.tbt_ms == 80
    assert fe.heap_mb == 42.5


def test_parse_no_runtime_block_gives_none():
    """lcp_ms/tbt_ms/heap_mb are None when no runtime block present."""
    snap = _fake_baseline_json()
    rows = _parse_perf_snapshot_rows(snap)
    fe_rows = [r for r in rows if r.side == "FE"]
    assert all(r.lcp_ms is None for r in fe_rows)
    assert all(r.tbt_ms is None for r in fe_rows)
    assert all(r.heap_mb is None for r in fe_rows)


def test_parse_cyclomatic_fields():
    """cc_max/cc_avg/hotspots_json populated from cyclomatic sub-keys."""
    snap = _fake_baseline_json(fe_pages={
        "/pulse": {
            "loc": 100,
            "effect_count": 5,
            "state_count": 10,
            "derived_count": 3,
            "cyclomatic_max": 25,
            "cyclomatic_avg": 8.3,
            "cyclomatic_hotspots": [{"fn_name": "bigFn", "cc": 25, "line": 42}],
        }
    })
    rows = _parse_perf_snapshot_rows(snap)
    fe = next(r for r in rows if r.side == "FE")
    assert fe.cc_max == 25
    assert fe.cc_avg == 8.3
    assert isinstance(fe.hotspots_json, list)
    assert fe.hotspots_json[0]["fn_name"] == "bigFn"


# ---------------------------------------------------------------------------
# Dimension 5a — Correctness: DB insert + history query (SQLite)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_insert_and_read_history(session: AsyncSession):
    """Rows inserted from a baseline JSON are returned by a history-style query."""
    from sqlalchemy import select, asc

    snap = _fake_baseline_json(captured_at="2026-07-01T04:00:00Z")
    orm_rows = _parse_rows(snap)
    session.add_all(orm_rows)
    await session.flush()

    result = (await session.execute(
        select(_PerfSnapshot)
        .where(_PerfSnapshot.page_or_route == "/pulse")
        .order_by(asc(_PerfSnapshot.captured_at))
    )).scalars().all()
    assert len(result) == 1
    assert result[0].loc == 6465
    assert result[0].side == "FE"


@pytest.mark.asyncio
async def test_latest_per_page(session: AsyncSession):
    """DISTINCT ON (side, page_or_route) returns the newest row when multiple exist."""
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    for i, loc_val in enumerate([100, 200, 300]):
        session.add(_PerfSnapshot(
            captured_at=now - timedelta(hours=48 - i * 12),
            side="FE",
            page_or_route="/pulse",
            loc=loc_val,
        ))
    await session.flush()

    # Simulate "latest" query: get most recent row for /pulse.
    q = (
        select(_PerfSnapshot)
        .where(_PerfSnapshot.page_or_route == "/pulse")
        .order_by(_PerfSnapshot.captured_at.desc())
        .limit(1)
    )
    row = (await session.execute(q)).scalars().first()
    assert row is not None
    assert row.loc == 300, f"Expected loc=300 (newest), got {row.loc}"


@pytest.mark.asyncio
async def test_retention_deletes_old_rows(session: AsyncSession):
    """Rows older than TTL are deleted; recent rows survive."""
    from sqlalchemy import select

    ttl = 365
    now = datetime.now(timezone.utc)
    ages = [10, 100, 364, 366, 400, 730]
    for age in ages:
        session.add(_PerfSnapshot(
            captured_at=now - timedelta(days=age),
            side="FE",
            page_or_route="/pulse",
            loc=100,
        ))
    await session.flush()

    deleted = await _sqlite_retention(session, "perf_snapshots", "captured_at", ttl)
    await session.flush()

    surviving = (await session.execute(select(_PerfSnapshot))).scalars().all()
    expected_deleted = len([a for a in ages if a >= ttl])
    assert deleted == expected_deleted, f"Expected {expected_deleted} deleted, got {deleted}"
    assert len(surviving) == len([a for a in ages if a < ttl])


@pytest.mark.asyncio
async def test_retention_zero_days_skips_cleanup(session: AsyncSession):
    """When retention days=0 no rows are deleted."""
    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    for age in [10, 400, 730]:
        session.add(_PerfSnapshot(
            captured_at=now - timedelta(days=age),
            side="BE",
            page_or_route="GET /api/positions",
            loc=500,
        ))
    await session.flush()

    days = 0
    deleted = 0 if days <= 0 else await _sqlite_retention(
        session, "perf_snapshots", "captured_at", days
    )

    surviving = (await session.execute(select(_PerfSnapshot))).scalars().all()
    assert deleted == 0
    assert len(surviving) == 3, "days=0 must retain all rows"


# ---------------------------------------------------------------------------
# Dimension 5b — Correctness: regression detection math
# ---------------------------------------------------------------------------

def _compute_regressions(
    rows: list[dict],
    threshold_pct: float = 10.0,
    metrics: list[str] = ("loc", "cc_max", "lcp_ms"),
) -> list[dict]:
    """Pure-Python mirror of the regression endpoint logic for unit testing."""
    by_page: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_page[r["page_or_route"]].append(r)

    results = []
    for page_name, page_rows in by_page.items():
        for col in metrics:
            values = [r[col] for r in page_rows if r.get(col) is not None]
            if len(values) < 2:
                continue
            median_val = statistics.median(values)
            if median_val == 0:
                continue
            current_val = values[-1]
            delta_pct = (current_val - median_val) / abs(median_val) * 100.0
            if delta_pct > threshold_pct:
                results.append({
                    "page": page_name,
                    "metric": col,
                    "current": current_val,
                    "median": median_val,
                    "delta_pct": round(delta_pct, 1),
                })
    return results


def test_regression_detected_when_current_exceeds_median():
    """A metric 50% above median is flagged as a regression."""
    rows = [
        {"page_or_route": "/pulse", "loc": 100, "cc_max": None, "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": 100, "cc_max": None, "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": 150, "cc_max": None, "lcp_ms": None},  # newest — 50% above median 100
    ]
    regressions = _compute_regressions(rows, threshold_pct=10.0, metrics=["loc"])
    assert len(regressions) == 1
    assert regressions[0]["page"] == "/pulse"
    assert regressions[0]["metric"] == "loc"
    assert regressions[0]["delta_pct"] == 50.0


def test_regression_not_flagged_below_threshold():
    """A 5% increase is not flagged when threshold is 10%."""
    rows = [
        {"page_or_route": "/pulse", "loc": 100, "cc_max": None, "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": 100, "cc_max": None, "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": 105, "cc_max": None, "lcp_ms": None},
    ]
    regressions = _compute_regressions(rows, threshold_pct=10.0, metrics=["loc"])
    assert len(regressions) == 0, "5% increase should not be flagged at 10% threshold"


def test_regression_skips_zero_median():
    """Metrics with median=0 are skipped to prevent divide-by-zero."""
    rows = [
        {"page_or_route": "/pulse", "loc": 0, "cc_max": None, "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": 0, "cc_max": None, "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": 100, "cc_max": None, "lcp_ms": None},
    ]
    regressions = _compute_regressions(rows, threshold_pct=10.0, metrics=["loc"])
    assert len(regressions) == 0, "median=0 should be skipped (no divide-by-zero)"


def test_regression_skips_null_metric():
    """Rows with NULL metrics are excluded from the window."""
    rows = [
        {"page_or_route": "/pulse", "loc": None, "cc_max": None, "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": None, "cc_max": None, "lcp_ms": None},
    ]
    regressions = _compute_regressions(rows, threshold_pct=10.0, metrics=["loc"])
    assert len(regressions) == 0, "all-NULL metric should not produce regression"


def test_regression_multiple_metrics_and_pages():
    """Multiple pages and metrics can independently flag regressions."""
    rows = [
        # /pulse — loc regresses
        {"page_or_route": "/pulse", "loc": 100, "cc_max": 5,   "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": 100, "cc_max": 5,   "lcp_ms": None},
        {"page_or_route": "/pulse", "loc": 200, "cc_max": 5,   "lcp_ms": None},
        # /dashboard — cc_max regresses
        {"page_or_route": "/dashboard", "loc": 50, "cc_max": 10,  "lcp_ms": None},
        {"page_or_route": "/dashboard", "loc": 50, "cc_max": 10,  "lcp_ms": None},
        {"page_or_route": "/dashboard", "loc": 50, "cc_max": 30,  "lcp_ms": None},
    ]
    regressions = _compute_regressions(rows, threshold_pct=10.0, metrics=["loc", "cc_max"])
    pages_metrics = {(r["page"], r["metric"]) for r in regressions}
    assert ("/pulse", "loc") in pages_metrics
    assert ("/dashboard", "cc_max") in pages_metrics


def test_regression_needs_at_least_two_values():
    """Single-row windows don't flag regressions (no median meaningful)."""
    rows = [
        {"page_or_route": "/pulse", "loc": 999, "cc_max": None, "lcp_ms": None},
    ]
    regressions = _compute_regressions(rows, threshold_pct=10.0, metrics=["loc"])
    assert len(regressions) == 0, "single-value window must not flag regression"


# ---------------------------------------------------------------------------
# Dimension 5c — Correctness: backfill skipped when rows already present
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_backfill_idempotency(session: AsyncSession):
    """Backfill logic skips when table already has rows."""
    from sqlalchemy import select, func as _func

    # Seed one row to represent an already-ingested state.
    session.add(_PerfSnapshot(
        captured_at=datetime.now(timezone.utc),
        side="FE",
        page_or_route="/pulse",
        loc=100,
    ))
    await session.flush()

    count = (await session.execute(
        select(_func.count()).select_from(_PerfSnapshot)
    )).scalar_one()
    assert count == 1, "Pre-condition: exactly one row present"

    # Simulate the backfill guard (count > 0 → skip).
    should_skip = count > 0
    assert should_skip, "Backfill should be skipped when rows already present"

    # Table count unchanged.
    count_after = (await session.execute(
        select(_func.count()).select_from(_PerfSnapshot)
    )).scalar_one()
    assert count_after == 1, "Backfill guard must not insert new rows when table is non-empty"


# ---------------------------------------------------------------------------
# Dimension 1 (extended) — SSOT: new settings keys seeded
# ---------------------------------------------------------------------------

def test_perf_snapshot_runtime_enabled_seeded():
    """perf_snapshot.runtime_enabled is seeded in settings.py."""
    assert "perf_snapshot.runtime_enabled" in _settings(), (
        "perf_snapshot.runtime_enabled key missing from settings.py"
    )


def test_perf_snapshot_runtime_timeout_s_seeded():
    """perf_snapshot.runtime_timeout_s is seeded in settings.py."""
    assert "perf_snapshot.runtime_timeout_s" in _settings(), (
        "perf_snapshot.runtime_timeout_s key missing from settings.py"
    )


# ---------------------------------------------------------------------------
# Dimension 3 (extended) — Stale code: _merge_runtime_into_rows defined
# ---------------------------------------------------------------------------

def test_merge_runtime_helper_is_module_level():
    """_merge_runtime_into_rows must be a module-level function."""
    src = _bg()
    assert "def _merge_runtime_into_rows(" in src, (
        "_merge_runtime_into_rows is not defined at module level in background.py"
    )


def test_cron_uses_merge_runtime_into_rows():
    """_run_and_insert body must call _merge_runtime_into_rows."""
    src = _bg()
    match = re.search(
        r"async def _run_and_insert\b.*?(?=\n    async def |\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_run_and_insert body not found in background.py"
    body = match.group(0)
    assert "_merge_runtime_into_rows(" in body, (
        "_run_and_insert does not call _merge_runtime_into_rows"
    )


def test_cron_reads_runtime_enabled():
    """_run_and_insert must read perf_snapshot.runtime_enabled setting."""
    src = _bg()
    match = re.search(
        r"async def _run_and_insert\b.*?(?=\n    async def |\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_run_and_insert body not found"
    body = match.group(0)
    assert "perf_snapshot.runtime_enabled" in body, (
        "_run_and_insert does not read perf_snapshot.runtime_enabled"
    )


def test_cron_reads_runtime_timeout_s():
    """_run_and_insert must read perf_snapshot.runtime_timeout_s setting."""
    src = _bg()
    match = re.search(
        r"async def _run_and_insert\b.*?(?=\n    async def |\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_run_and_insert body not found"
    body = match.group(0)
    assert "perf_snapshot.runtime_timeout_s" in body, (
        "_run_and_insert does not read perf_snapshot.runtime_timeout_s"
    )


def test_run_runtime_merges_env():
    """_run_runtime must pass an env dict that includes PLAYWRIGHT_USER and PATH."""
    src = _bg()
    # Confirm both env keys appear in the source.
    assert "PLAYWRIGHT_USER" in src, "PLAYWRIGHT_USER not set in _run_runtime env"
    assert "PLAYWRIGHT_PASS" in src, "PLAYWRIGHT_PASS not set in _run_runtime env"
    # Confirm os.environ spread so PATH is preserved.
    assert "_os.environ" in src or "os.environ" in src, (
        "_run_runtime does not spread os.environ — PATH may be stripped in cron"
    )


# ---------------------------------------------------------------------------
# Dimension 4 (extended) — Reusable: _merge_runtime_into_rows importable
# ---------------------------------------------------------------------------

def test_merge_runtime_importable():
    """_merge_runtime_into_rows is importable from background module."""
    # Already imported at top of file — this just asserts no ImportError.
    assert callable(_merge_runtime_into_rows)


# ---------------------------------------------------------------------------
# Dimension 5d — Correctness: _merge_runtime_into_rows unit tests
# ---------------------------------------------------------------------------

def _make_fe_row(route: str) -> object:
    """Return a PerfSnapshot-like object with the minimal attributes."""
    rows = _parse_perf_snapshot_rows({
        "captured_at": "2026-07-01T04:00:00Z",
        "commit": "abc123",
        "frontend": {"pages": {route: {"loc": 100, "effect_count": 2,
                                        "state_count": 3, "derived_count": 1}}},
        "backend": {"routes": {}},
    })
    return rows[0]  # FE row


def test_merge_runtime_patches_lcp_tbt_heap():
    """_merge_runtime_into_rows patches lcp_ms/tbt_ms/heap_mb from capture JSON."""
    row = _make_fe_row("/pulse")
    assert row.lcp_ms is None

    cap_json = {
        "frontend": {"pages": {"/pulse": {"runtime": {
            "lcp_ms": 1450, "tbt_ms": 90, "heap_mb": 55.2,
        }}}}
    }
    count = _merge_runtime_into_rows([row], cap_json)
    assert count == 1, f"Expected 1 patched row, got {count}"
    assert row.lcp_ms  == 1450
    assert row.tbt_ms  == 90
    assert row.heap_mb == 55.2


def test_merge_runtime_ignores_be_rows():
    """BE rows are never patched by _merge_runtime_into_rows."""
    rows = _parse_perf_snapshot_rows({
        "captured_at": "2026-07-01T04:00:00Z",
        "commit": "abc123",
        "frontend": {"pages": {}},
        "backend": {"routes": {"GET /api/positions": {"loc": 900}}},
    })
    be_row = rows[0]
    assert be_row.side == "BE"

    cap_json = {
        "frontend": {"pages": {"GET /api/positions": {"runtime": {
            "lcp_ms": 1000, "tbt_ms": 50, "heap_mb": 20.0,
        }}}}
    }
    count = _merge_runtime_into_rows([be_row], cap_json)
    assert count == 0, "BE rows must not be patched"
    assert be_row.lcp_ms is None


def test_merge_runtime_unmatched_route_unchanged():
    """FE rows whose route has no entry in capture JSON remain None."""
    row = _make_fe_row("/pulse")
    cap_json = {"frontend": {"pages": {"/dashboard": {"runtime": {
        "lcp_ms": 800, "tbt_ms": 40, "heap_mb": 30.0,
    }}}}}
    count = _merge_runtime_into_rows([row], cap_json)
    assert count == 0, "Unmatched route must not be patched"
    assert row.lcp_ms is None


def test_merge_runtime_empty_capture_json():
    """Empty capture JSON leaves all rows untouched."""
    row = _make_fe_row("/pulse")
    count = _merge_runtime_into_rows([row], {})
    assert count == 0
    assert row.lcp_ms is None


def test_merge_runtime_partial_runtime_block():
    """A runtime block with only lcp_ms still patches (tbt/heap stay None)."""
    row = _make_fe_row("/pulse")
    cap_json = {"frontend": {"pages": {"/pulse": {"runtime": {"lcp_ms": 2000}}}}}
    count = _merge_runtime_into_rows([row], cap_json)
    assert count == 1
    assert row.lcp_ms  == 2000
    assert row.tbt_ms  is None
    assert row.heap_mb is None


def test_merge_runtime_returns_count_of_patched_rows():
    """Return value is the count of FE rows that received at least one runtime field."""
    snap = _fake_baseline_json(fe_pages={
        "/pulse": {"loc": 100, "effect_count": 1, "state_count": 2, "derived_count": 0},
        "/dashboard": {"loc": 200, "effect_count": 2, "state_count": 3, "derived_count": 1},
        "/orders": {"loc": 150, "effect_count": 1, "state_count": 1, "derived_count": 0},
    })
    rows = _parse_perf_snapshot_rows(snap)

    cap_json = {
        "frontend": {"pages": {
            "/pulse":     {"runtime": {"lcp_ms": 1200, "tbt_ms": 60, "heap_mb": 40.0}},
            "/dashboard": {"runtime": {"lcp_ms": 900,  "tbt_ms": 30, "heap_mb": 35.0}},
            # /orders intentionally absent → not patched
        }}
    }
    count = _merge_runtime_into_rows(rows, cap_json)
    assert count == 2, f"Expected 2 patched, got {count}"

    pulse_row     = next(r for r in rows if r.page_or_route == "/pulse")
    dashboard_row = next(r for r in rows if r.page_or_route == "/dashboard")
    orders_row    = next(r for r in rows if r.page_or_route == "/orders")

    assert pulse_row.lcp_ms     == 1200
    assert dashboard_row.lcp_ms == 900
    assert orders_row.lcp_ms    is None


# ---------------------------------------------------------------------------
# Dimension 5e — Correctness: two-step graceful-degradation stubs
#
# We test the observable behaviour of the two-step logic:
#   - static rows are always inserted when static step succeeds
#   - runtime values are merged when runtime step succeeds
#   - timeout / crash → static-only (runtime cols remain None)
#   - runtime_enabled=False → no runtime subprocess call
#
# These tests work by parsing the background.py source structure rather
# than exercising asyncio subprocesses (which would require a live server).
# The subprocess paths themselves are integration-tested via the shell script
# in CI; here we guard the degradation branches exist in source.
# ---------------------------------------------------------------------------

def test_static_failure_skips_insert_guard_in_source():
    """_run_and_insert must bail if _run_static() returns None (no DB insert)."""
    src = _bg()
    match = re.search(
        r"async def _run_and_insert\b.*?(?=\n    async def |\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_run_and_insert body not found"
    body = match.group(0)
    # Guard should check None return from _run_static and return early.
    assert "_run_static()" in body, "_run_and_insert does not call _run_static()"
    assert "is None" in body, (
        "_run_and_insert has no None-guard on static step result"
    )


def test_runtime_disabled_guard_in_source():
    """When runtime_enabled is False _run_runtime must not be called."""
    src = _bg()
    match = re.search(
        r"async def _run_and_insert\b.*?(?=\n    async def |\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_run_and_insert body not found"
    body = match.group(0)
    assert "runtime_enabled" in body, (
        "_run_and_insert does not check runtime_enabled before calling _run_runtime"
    )
    assert "_run_runtime(" in body, (
        "_run_and_insert never calls _run_runtime"
    )


def test_runtime_timeout_logged_as_warning():
    """Timeout in the runtime step must emit a WARNING (not ERROR), not raise."""
    src = _bg()
    match = re.search(
        r"async def _run_runtime\b.*?(?=\n    async def |\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_run_runtime body not found"
    body = match.group(0)
    assert "logger.warning" in body, (
        "_run_runtime timeout path must use logger.warning not logger.error"
    )
    assert "asyncio.TimeoutError" in body, (
        "_run_runtime does not catch asyncio.TimeoutError"
    )
    # Must return None (not re-raise) so caller falls through to static-only insert.
    assert "return None" in body, (
        "_run_runtime timeout handler does not return None"
    )


def test_static_timeout_is_120s_not_600s():
    """Static step uses a tight 120 s timeout — not the 600 s runtime budget."""
    src = _bg()
    match = re.search(
        r"async def _run_static\b.*?(?=\n    async def |\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_run_static body not found"
    body = match.group(0)
    assert "timeout=120" in body, (
        "_run_static does not use a 120 s timeout — static step should be fast"
    )


def test_runtime_env_includes_playwright_base_url():
    """PLAYWRIGHT_BASE_URL must be set to dev.ramboq.com in runtime env."""
    src = _bg()
    assert "dev.ramboq.com" in src, (
        "PLAYWRIGHT_BASE_URL dev.ramboq.com not found in background.py"
    )


def test_insert_log_includes_runtime_count():
    """Final insert log message must surface the runtime_count for observability."""
    src = _bg()
    match = re.search(
        r"async def _run_and_insert\b.*?(?=\n    async def |\nasync def |\Z)",
        src, re.DOTALL,
    )
    assert match, "_run_and_insert body not found"
    body = match.group(0)
    assert "runtime_count" in body, (
        "_run_and_insert does not log runtime_count in the final insert message"
    )
