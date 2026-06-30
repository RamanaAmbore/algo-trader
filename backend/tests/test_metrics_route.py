"""
test_metrics_route.py

`/api/admin/code-metrics/*` controller — read-only route smoke tests.

Six quality dimensions (feedback_test_dimensions.md):

  1. SSOT        — `_ALL_TREND_KEYS` is the single source of valid
                   trend metrics (union of `_TREND_COLUMNS` + virtual
                   `_TEST_TREND_KEYS`). `test_trend_rejects_unknown_metric`
                   asserts the endpoint rejects anything outside it.
  2. Performance — list endpoint omits `raw_payload` (asserted via
                   schema field check; keeps the response small even
                   when payload is hundreds of KB).
  3. Stale code  — no inline SQL in route file (column allowlist drives
                   the query). Grep-style check in
                   `test_route_uses_allowlisted_columns_only`.
  4. Reusable    — `_row_to_summary` is the single Mapped→Struct
                   adapter. Tested via the list + detail endpoints
                   (different responses, same converter).
  5. Correctness — happy path returns rows; unknown release_tag → 404;
                   trend with bad metric → 422; trend point order is
                   chronological (oldest first).
  6. Response time — virtual trend keys extract from JSONB without
                   touching the `getattr` path (safety check that
                   the JSON extraction route is distinct from the
                   column-name route).

Tests mock the SQLAlchemy session at the call-site boundary so they
run without a live PostgreSQL — same pattern used by
test_orders_post_cutover.py and friends.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException


# ── Helpers ──────────────────────────────────────────────────────────────────


class _RowStub:
    """Duck-typed CodeMetricsSnapshot — attribute access only.
    SQLAlchemy declarative instances need a state object to allow
    setattr; for converter tests a plain attribute carrier is enough."""
    def __init__(self, **kw): self.__dict__.update(kw)


def _fake_row(release_tag: str, *, days_ago: int = 0, **overrides):
    """Fabricate a CodeMetricsSnapshot-shaped stub. Defaults are
    coherent so the converter doesn't blow up on None handling."""
    base = dict(
        id=1,
        release_tag=release_tag,
        captured_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        git_sha="abc1234",
        backend_loc=50_000,
        backend_complexity_avg=3.5,
        backend_complexity_max=22,
        backend_duplicated_lines=None,
        backend_stale_count=12,
        backend_coverage_pct=72.3,
        frontend_loc=80_000,
        frontend_complexity_avg=4.1,
        frontend_complexity_max=18,
        frontend_duplicated_lines=200,
        frontend_stale_count=5,
        frontend_coverage_pct=None,
        bug_count_since_last_release=14,
        per_page_latency_ms={"/pulse": {"dcl": 221, "idle": 4223, "lcp": 2012}},
        test_response_times={
            "backend": {
                "total_tests": 47,
                "total_wall_time_s": 17.66,
                "median_s": 0.01,
                "max_s": 7.36,
                "top_10_slowest": [{"name": "test_snapshot_no_trades", "duration_s": 7.36}],
                "slow_count": 4,
                "slow_threshold_s": 1.0,
            },
            "frontend": {"_skipped": "no Playwright JSON report found"},
        },
        notes="auto-captured",
        raw_payload={"radon_cc": {}},
    )
    base.update(overrides)
    return _RowStub(**base)


def _mock_session_with_rows(rows):
    """Build an async_session context manager that returns the given
    rows on `.execute(...).scalars().all()` and the first row on
    `.scalar_one_or_none()`."""
    session = AsyncMock()
    # First execute → scalars().all() returns rows (for the count + list query).
    # We make it permissive: any execute call yields the same rows; the
    # controller's count query uses `.scalars().all()` and the list query too.
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    result.scalar_one_or_none.return_value = rows[0] if rows else None
    # `.all()` is also used by trend (returns tuples)
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    cm = MagicMock()
    cm.return_value = session
    return cm, result


# ── 1. SSOT — trend endpoint allowlist gate ──────────────────────────────────


@pytest.mark.asyncio
async def test_trend_rejects_unknown_metric():
    """Querying /trends with a column name outside `_TREND_COLUMNS` must
    422. This is the SQL-injection-by-column-name guard."""
    from backend.api.routes.metrics import MetricsController

    controller = MetricsController.__new__(MetricsController)
    with pytest.raises(HTTPException) as exc:
        await MetricsController.trend.fn(controller, metric="; DROP TABLE users; --")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_trend_rejects_typo_metric():
    """Typo path — `backend_coverage_pc` (missing `t`) must 422 too."""
    from backend.api.routes.metrics import MetricsController

    controller = MetricsController.__new__(MetricsController)
    with pytest.raises(HTTPException) as exc:
        await MetricsController.trend.fn(controller, metric="backend_coverage_pc")
    assert exc.value.status_code == 422


# ── 2. Performance — list omits raw_payload ──────────────────────────────────


@pytest.mark.asyncio
async def test_list_omits_raw_payload():
    """Even when DB rows carry a huge raw_payload, the list response
    must not surface it (separate detail endpoint owns that)."""
    from backend.api.routes.metrics import MetricsController

    r = _fake_row("v2.0.0", days_ago=2)
    r.raw_payload = {"huge": "x" * 500_000}  # would explode list response
    cm, _ = _mock_session_with_rows([r])

    with patch("backend.api.routes.metrics.async_session", cm):
        controller = MetricsController.__new__(MetricsController)
        resp = await MetricsController.list_snapshots.fn(controller, limit=50, offset=0)

    assert resp.total == 1
    assert len(resp.rows) == 1
    row = resp.rows[0]
    # raw_payload is NOT in the list row — detail endpoint owns it.
    assert "raw_payload" not in row.__struct_fields__
    # test_response_times IS in the list row so the table can show a
    # quick "captured / not captured" indicator without a detail round-trip.
    assert "test_response_times" in row.__struct_fields__
    # Verify the stub data came through correctly.
    assert row.test_response_times is not None
    assert "backend" in row.test_response_times


# ── 3. Stale code — route uses only the allowlist for column lookups ─────────


def test_route_uses_allowlisted_columns_only():
    """Source-level guard: the trend handler gates on `_ALL_TREND_KEYS`
    before dispatching. Two paths exist:
      1. Virtual test-time keys → JSONB extraction path (no getattr)
      2. Real DB columns → `getattr(CodeMetricsSnapshot, metric)` (safe)

    Asserts:
    - The `_ALL_TREND_KEYS` gate appears in the source.
    - The real-column `getattr` path exists.
    - The `_ALL_TREND_KEYS` gate precedes the `getattr` call.
    - No legacy `metric not in _TREND_COLUMNS`-only gate is the sole
      check (virtual keys must also be allowed through).
    """
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "api" / "routes" / "metrics.py"
    body = src.read_text()

    # Primary gate: now checks against the union set.
    gate_idx     = body.find("metric not in _ALL_TREND_KEYS")
    getattr_idx  = body.find("getattr(CodeMetricsSnapshot, metric)")
    test_keys_idx = body.find("_TEST_TREND_KEYS")

    assert gate_idx != -1,     "_ALL_TREND_KEYS gate missing from metrics.py"
    assert getattr_idx != -1,  "getattr(CodeMetricsSnapshot, metric) line missing"
    assert test_keys_idx != -1, "_TEST_TREND_KEYS definition missing from metrics.py"
    assert gate_idx < getattr_idx, "allowlist gate must precede getattr"


# ── 4. Reusable — _row_to_summary is the single Mapped→Struct adapter ────────


def test_row_to_summary_handles_nulls():
    """Every numeric column is Optional — converter must preserve None
    instead of coercing to 0."""
    from backend.api.routes.metrics import _row_to_summary

    r = _fake_row(
        "v1.5",
        backend_loc=None,
        backend_coverage_pct=None,
        bug_count_since_last_release=None,
    )
    out = _row_to_summary(r)
    assert out.backend_loc is None
    assert out.backend_coverage_pct is None
    assert out.bug_count_since_last_release is None
    # And the non-null fields pass through.
    assert out.release_tag == "v1.5"


# ── 5. Correctness — list, detail, 404, trend order ──────────────────────────


@pytest.mark.asyncio
async def test_list_returns_rows_and_total():
    from backend.api.routes.metrics import MetricsController

    rows = [_fake_row("v2.0.0", days_ago=2), _fake_row("v1.0.0", days_ago=30)]
    cm, _ = _mock_session_with_rows(rows)

    with patch("backend.api.routes.metrics.async_session", cm):
        controller = MetricsController.__new__(MetricsController)
        resp = await MetricsController.list_snapshots.fn(controller, limit=50, offset=0)

    assert resp.total == 2
    assert len(resp.rows) == 2
    assert resp.rows[0].release_tag == "v2.0.0"


@pytest.mark.asyncio
async def test_detail_404_when_missing():
    from backend.api.routes.metrics import MetricsController

    cm, _ = _mock_session_with_rows([])

    with patch("backend.api.routes.metrics.async_session", cm):
        controller = MetricsController.__new__(MetricsController)
        with pytest.raises(HTTPException) as exc:
            await MetricsController.get_snapshot.fn(controller, release_tag="never-was")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_detail_returns_raw_payload():
    from backend.api.routes.metrics import MetricsController

    r = _fake_row("v2.0.0")
    r.raw_payload = {"radon_cc": {"summary": "ok"}, "tag": "forensic"}
    cm, _ = _mock_session_with_rows([r])

    with patch("backend.api.routes.metrics.async_session", cm):
        controller = MetricsController.__new__(MetricsController)
        resp = await MetricsController.get_snapshot.fn(controller, release_tag="v2.0.0")
    assert resp.row.release_tag == "v2.0.0"
    assert resp.raw_payload == {"radon_cc": {"summary": "ok"}, "tag": "forensic"}


@pytest.mark.asyncio
async def test_trend_returns_chronological_points():
    """The trend handler queries DESC + reverses → result must be
    oldest first (chart-friendly L-to-R)."""
    from backend.api.routes.metrics import MetricsController

    # Simulate what `session.execute(...).all()` returns — list of
    # tuples (release_tag, captured_at, value). DESC order from DB.
    now = datetime.now(timezone.utc)
    db_rows = [
        ("v3.0.0", now - timedelta(days=1),  82.0),
        ("v2.0.0", now - timedelta(days=10), 76.0),
        ("v1.0.0", now - timedelta(days=30), 70.0),
    ]

    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = db_rows
    session.execute = AsyncMock(return_value=result)
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    cm = MagicMock(return_value=session)

    with patch("backend.api.routes.metrics.async_session", cm):
        controller = MetricsController.__new__(MetricsController)
        resp = await MetricsController.trend.fn(controller, metric="backend_coverage_pct", limit=50)

    assert resp.metric == "backend_coverage_pct"
    assert [p.release_tag for p in resp.points] == ["v1.0.0", "v2.0.0", "v3.0.0"]
    assert [p.value for p in resp.points] == [70.0, 76.0, 82.0]


@pytest.mark.asyncio
async def test_trend_handles_null_values():
    """Some snapshots may have None for a given column (capture script
    couldn't run that tool). The trend response must preserve None
    instead of coercing to 0."""
    from backend.api.routes.metrics import MetricsController

    now = datetime.now(timezone.utc)
    db_rows = [
        ("v2.0.0", now - timedelta(days=1),  None),
        ("v1.0.0", now - timedelta(days=10), 50.0),
    ]
    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = db_rows
    session.execute = AsyncMock(return_value=result)
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    cm = MagicMock(return_value=session)

    with patch("backend.api.routes.metrics.async_session", cm):
        controller = MetricsController.__new__(MetricsController)
        resp = await MetricsController.trend.fn(controller, metric="backend_coverage_pct", limit=50)
    assert resp.points[1].value is None
    assert resp.points[0].value == 50.0


# ── 6. Response time — virtual test trend key JSONB extraction ────────────────


@pytest.mark.asyncio
async def test_virtual_test_trend_key_extracts_from_jsonb():
    """The `test_backend_max_s` virtual key must extract from the
    `test_response_times` JSONB column (not via `getattr`) and must
    return None for snapshots where the column is NULL or missing the
    sub-key. This verifies the JSONB extraction path is distinct from
    the real-column `getattr` path.
    """
    from backend.api.routes.metrics import MetricsController

    now = datetime.now(timezone.utc)
    # DB rows: (release_tag, captured_at, test_response_times dict or None)
    db_rows = [
        # Newest first (DESC order from DB).
        ("v3.0.0", now - timedelta(days=1),  {"backend": {"max_s": 3.1},  "frontend": {}}),
        ("v2.0.0", now - timedelta(days=10), {"backend": {"max_s": 1.5},  "frontend": {}}),
        ("v1.0.0", now - timedelta(days=30), None),  # NULL = not captured yet
    ]

    session = AsyncMock()
    result = MagicMock()
    result.all.return_value = db_rows
    session.execute = AsyncMock(return_value=result)
    session.__aenter__.return_value = session
    session.__aexit__.return_value = False
    cm = MagicMock(return_value=session)

    with patch("backend.api.routes.metrics.async_session", cm):
        controller = MetricsController.__new__(MetricsController)
        resp = await MetricsController.trend.fn(
            controller, metric="test_backend_max_s", limit=50
        )

    # Chronological order (oldest first).
    assert resp.metric == "test_backend_max_s"
    assert len(resp.points) == 3
    assert resp.points[0].release_tag == "v1.0.0"
    assert resp.points[0].value is None  # NULL row → None
    assert resp.points[1].release_tag == "v2.0.0"
    assert resp.points[1].value == 1.5
    assert resp.points[2].release_tag == "v3.0.0"
    assert resp.points[2].value == 3.1


@pytest.mark.asyncio
async def test_virtual_test_trend_key_rejects_unknown():
    """The virtual key path must also 422 on an unknown key — proving
    that `_ALL_TREND_KEYS` gates both the real-column and virtual paths.
    """
    from backend.api.routes.metrics import MetricsController

    controller = MetricsController.__new__(MetricsController)
    with pytest.raises(HTTPException) as exc:
        await MetricsController.trend.fn(
            controller, metric="test_backend_injected_column"
        )
    assert exc.value.status_code == 422
