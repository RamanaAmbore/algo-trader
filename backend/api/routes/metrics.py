"""
Code-metrics read API — admin surface for `/admin/metrics`.

Endpoints (all admin-guarded):
  GET  /api/admin/code-metrics/                  — list all snapshots newest first
  GET  /api/admin/code-metrics/{release_tag}     — single snapshot detail (raw_payload included)
  GET  /api/admin/code-metrics/trends            — time series for a single metric

Writes are NEVER served from HTTP — the only producer of rows is the
out-of-band capture script `scripts/capture_metrics.py`, run either
manually by the operator or from `webhook/deploy.sh` post-merge. Keeping
the route read-only avoids accidentally double-counting a release if
the page is visited mid-deploy.

Source-of-truth column-name allowlist (`_TREND_COLUMNS`) gates the
`/trends` endpoint so the operator can't ask for a column the model
doesn't expose (defends against SQL-injection-by-column-name).
"""

from __future__ import annotations

from typing import Optional

import msgspec
from litestar import Controller, get
from litestar.exceptions import HTTPException
from sqlalchemy import select, desc

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import CodeMetricsSnapshot
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# Numeric columns the operator can chart over time. Anything not in
# this set is rejected by `/trends` — defends both against typos and
# SQL-injection-by-column-name (we interpolate the name into a
# `getattr(model, name)`, which is safe only when the name is
# allowlisted).
_TREND_COLUMNS: tuple[str, ...] = (
    "backend_loc",
    "backend_complexity_avg",
    "backend_complexity_max",
    "backend_duplicated_lines",
    "backend_stale_count",
    "backend_coverage_pct",
    "frontend_loc",
    "frontend_complexity_avg",
    "frontend_complexity_max",
    "frontend_duplicated_lines",
    "frontend_stale_count",
    "frontend_coverage_pct",
    "bug_count_since_last_release",
)

# Virtual trend keys extracted from the JSONB `test_response_times`
# column. These are NOT real DB columns — the `/trends` endpoint
# handles them via a special path that reads the JSON field and
# extracts the sub-key.  Allowlisted here for the same security
# reason as `_TREND_COLUMNS`.
_TEST_TREND_KEYS: tuple[str, ...] = (
    "test_backend_max_s",
    "test_backend_total_wall_time_s",
    "test_backend_slow_count",
    "test_frontend_max_s",
    "test_frontend_total_wall_time_s",
)

_ALL_TREND_KEYS: frozenset[str] = frozenset(_TREND_COLUMNS) | frozenset(_TEST_TREND_KEYS)


# ---------------------------------------------------------------------------
# Schemas (msgspec.Struct — 10x faster than pydantic)
# ---------------------------------------------------------------------------


class MetricsSnapshotRow(msgspec.Struct):
    """List row — same shape as detail row but raw_payload is omitted to
    keep `/code-metrics/` cheap. Operators drill into detail via the
    per-release endpoint when they need forensics."""
    id: int
    release_tag: str
    captured_at: str
    git_sha: Optional[str]
    # Backend
    backend_loc: Optional[int]
    backend_complexity_avg: Optional[float]
    backend_complexity_max: Optional[int]
    backend_duplicated_lines: Optional[int]
    backend_stale_count: Optional[int]
    backend_coverage_pct: Optional[float]
    # Frontend
    frontend_loc: Optional[int]
    frontend_complexity_avg: Optional[float]
    frontend_complexity_max: Optional[int]
    frontend_duplicated_lines: Optional[int]
    frontend_stale_count: Optional[int]
    frontend_coverage_pct: Optional[float]
    # Cross-cutting
    bug_count_since_last_release: Optional[int]
    per_page_latency_ms: Optional[dict]
    # Test execution times — populated when --with-test-times is passed.
    # Structure: {backend: {total_tests, total_wall_time_s, median_s,
    #   max_s, top_10_slowest, slow_count, slow_threshold_s},
    #   frontend: same}. NULL = not yet captured.
    test_response_times: Optional[dict]
    notes: Optional[str]


class MetricsListResponse(msgspec.Struct):
    rows: list[MetricsSnapshotRow]
    total: int


class MetricsDetailResponse(msgspec.Struct):
    row: MetricsSnapshotRow
    raw_payload: Optional[dict]


class TrendPoint(msgspec.Struct):
    release_tag: str
    captured_at: str
    value: Optional[float]


class TrendResponse(msgspec.Struct):
    metric: str
    points: list[TrendPoint]   # oldest → newest (chart-friendly order)


def _row_to_summary(r: CodeMetricsSnapshot) -> MetricsSnapshotRow:
    return MetricsSnapshotRow(
        id=r.id,
        release_tag=r.release_tag,
        captured_at=r.captured_at.isoformat() if r.captured_at else "",
        git_sha=r.git_sha,
        backend_loc=r.backend_loc,
        backend_complexity_avg=r.backend_complexity_avg,
        backend_complexity_max=r.backend_complexity_max,
        backend_duplicated_lines=r.backend_duplicated_lines,
        backend_stale_count=r.backend_stale_count,
        backend_coverage_pct=r.backend_coverage_pct,
        frontend_loc=r.frontend_loc,
        frontend_complexity_avg=r.frontend_complexity_avg,
        frontend_complexity_max=r.frontend_complexity_max,
        frontend_duplicated_lines=r.frontend_duplicated_lines,
        frontend_stale_count=r.frontend_stale_count,
        frontend_coverage_pct=r.frontend_coverage_pct,
        bug_count_since_last_release=r.bug_count_since_last_release,
        per_page_latency_ms=r.per_page_latency_ms,
        test_response_times=r.test_response_times,
        notes=r.notes,
    )


class MetricsController(Controller):
    """`/api/admin/code-metrics/*` — read-only snapshot store.

    Writes happen out-of-band via `scripts/capture_metrics.py`. Keeping
    the surface read-only prevents accidental double-capture from a
    re-deploy mid-flight."""
    path = "/api/admin/code-metrics"
    guards = [admin_guard]

    @get("/")
    async def list_snapshots(self, limit: int = 50, offset: int = 0) -> MetricsListResponse:
        """List snapshots newest-first. The list view excludes
        raw_payload to keep the payload small (a full radon dump is
        a few hundred KB); operators drill into detail when they need
        the raw tool output."""
        limit = max(1, min(int(limit or 50), 200))
        offset = max(0, int(offset or 0))
        async with async_session() as session:
            total_rows = (await session.execute(
                select(CodeMetricsSnapshot.id)
            )).scalars().all()
            total = len(total_rows)

            rows = (await session.execute(
                select(CodeMetricsSnapshot)
                .order_by(desc(CodeMetricsSnapshot.captured_at))
                .limit(limit).offset(offset)
            )).scalars().all()

        return MetricsListResponse(
            rows=[_row_to_summary(r) for r in rows],
            total=total,
        )

    @get("/trends")
    async def trend(self, metric: str = "backend_coverage_pct", limit: int = 50) -> TrendResponse:
        """Time series of `metric` across captured snapshots. Returned in
        chronological order (oldest first) so the front-end can plot
        left-to-right without re-sorting. `limit` is the maximum number
        of points returned — defaults to 50 (typical year of weekly
        releases).

        Accepts both real numeric DB columns (listed in `_TREND_COLUMNS`)
        and virtual sub-keys extracted from the JSONB `test_response_times`
        column (listed in `_TEST_TREND_KEYS`, e.g.
        `test_backend_max_s` → `test_response_times['backend']['max_s']`).
        """
        metric = (metric or "").strip()
        if metric not in _ALL_TREND_KEYS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown metric '{metric}'. "
                    f"Allowed: {', '.join(sorted(_ALL_TREND_KEYS))}"
                ),
            )
        limit = max(1, min(int(limit or 50), 200))

        # Virtual test-time sub-keys: read `test_response_times` JSONB and
        # extract the nested value in Python rather than via SQL (keeps the
        # query simple and avoids JSONB operator injection risks).
        if metric in _TEST_TREND_KEYS:
            # Mapping: test_backend_max_s → ("backend", "max_s")
            # test_frontend_total_wall_time_s → ("frontend", "total_wall_time_s")
            _key_map = {
                "test_backend_max_s":              ("backend",  "max_s"),
                "test_backend_total_wall_time_s":  ("backend",  "total_wall_time_s"),
                "test_backend_slow_count":         ("backend",  "slow_count"),
                "test_frontend_max_s":             ("frontend", "max_s"),
                "test_frontend_total_wall_time_s": ("frontend", "total_wall_time_s"),
            }
            side, sub = _key_map[metric]
            async with async_session() as session:
                recent = (await session.execute(
                    select(
                        CodeMetricsSnapshot.release_tag,
                        CodeMetricsSnapshot.captured_at,
                        CodeMetricsSnapshot.test_response_times,
                    )
                    .order_by(desc(CodeMetricsSnapshot.captured_at))
                    .limit(limit)
                )).all()

            points = []
            for r in reversed(recent):
                trt = r[2]  # test_response_times dict or None
                value: Optional[float] = None
                if isinstance(trt, dict):
                    side_data = trt.get(side)
                    if isinstance(side_data, dict):
                        raw_val = side_data.get(sub)
                        if raw_val is not None:
                            try:
                                value = float(raw_val)
                            except (TypeError, ValueError):
                                value = None
                points.append(TrendPoint(
                    release_tag=r[0],
                    captured_at=r[1].isoformat() if r[1] else "",
                    value=value,
                ))
            return TrendResponse(metric=metric, points=points)

        # Real DB column path.
        # `getattr(model_class, allowlisted_name)` is safe — `metric`
        # was validated above against a hardcoded frozenset.
        col = getattr(CodeMetricsSnapshot, metric)
        async with async_session() as session:
            # Take the most-recent N then reverse for chart order. This
            # keeps the query bounded even when 5+ years of releases
            # accumulate.
            recent = (await session.execute(
                select(
                    CodeMetricsSnapshot.release_tag,
                    CodeMetricsSnapshot.captured_at,
                    col,
                )
                .order_by(desc(CodeMetricsSnapshot.captured_at))
                .limit(limit)
            )).all()

        points = [
            TrendPoint(
                release_tag=r[0],
                captured_at=r[1].isoformat() if r[1] else "",
                value=(float(r[2]) if r[2] is not None else None),
            )
            for r in reversed(recent)
        ]
        return TrendResponse(metric=metric, points=points)

    @get("/{release_tag:str}")
    async def get_snapshot(self, release_tag: str) -> MetricsDetailResponse:
        """Single snapshot detail — includes raw_payload for forensics
        (full radon JSON, vulture stdout, jscpd report, coverage
        snippet). The list view omits raw_payload for size; drill in
        here when an alert demands "why did the complexity max jump?"."""
        tag = (release_tag or "").strip()
        if not tag:
            raise HTTPException(status_code=422, detail="release_tag required")
        async with async_session() as session:
            r = (await session.execute(
                select(CodeMetricsSnapshot)
                .where(CodeMetricsSnapshot.release_tag == tag)
            )).scalar_one_or_none()
        if r is None:
            raise HTTPException(status_code=404, detail=f"Snapshot '{tag}' not found")
        return MetricsDetailResponse(
            row=_row_to_summary(r),
            raw_payload=r.raw_payload,
        )
