"""
Perf-snapshot admin endpoints.

GET /api/admin/perf/history?page=<name>&days=<N>
    Time series for one page/route. Returns [{captured_at, loc, cc_max,
    cc_avg, lcp_ms, tbt_ms, heap_mb, route_p50_ms, route_p95_ms,
    route_qps}, …] ordered by captured_at ASC.

GET /api/admin/perf/latest
    One row per (side, page_or_route) — the latest snapshot for each.

GET /api/admin/perf/regressions?days=7&threshold_pct=10
    Pages/routes where a numeric metric rose more than threshold_pct%
    above the N-day median. Returns [{page, metric, current, median,
    delta_pct}, …].

All endpoints are admin-guarded.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone, timedelta
from typing import Optional

import msgspec
from litestar import Controller, get
from sqlalchemy import select, func, text

from backend.api.auth_guard import admin_guard
from backend.api.database import async_session
from backend.api.models import PerfSnapshot
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

# Metrics we track for regression detection. Each entry is
# (column_name, higher_is_worse). All values NULL → skipped.
_REGRESSION_METRICS: list[tuple[str, bool]] = [
    ("loc",          True),
    ("cc_max",       True),
    ("cc_avg",       True),
    ("lcp_ms",       True),
    ("tbt_ms",       True),
    ("heap_mb",      True),
    ("route_p50_ms", True),
    ("route_p95_ms", True),
]


# ── Response schemas ─────────────────────────────────────────────────────────

class PerfHistoryRow(msgspec.Struct):
    captured_at: str
    commit_sha: Optional[str]
    loc: Optional[int]
    effect_count: Optional[int]
    state_count: Optional[int]
    derived_count: Optional[int]
    cc_max: Optional[int]
    cc_avg: Optional[float]
    lcp_ms: Optional[int]
    tbt_ms: Optional[int]
    heap_mb: Optional[float]
    route_p50_ms: Optional[int]
    route_p95_ms: Optional[int]
    route_qps: Optional[float]


class PerfLatestRow(msgspec.Struct):
    side: str
    page_or_route: str
    captured_at: str
    commit_sha: Optional[str]
    loc: Optional[int]
    cc_max: Optional[int]
    cc_avg: Optional[float]
    lcp_ms: Optional[int]
    tbt_ms: Optional[int]
    heap_mb: Optional[float]
    route_p50_ms: Optional[int]
    route_p95_ms: Optional[int]
    route_qps: Optional[float]


class PerfRegressionRow(msgspec.Struct):
    page: str
    side: str
    metric: str
    current: float
    median: float
    delta_pct: float


class PerfHistoryResponse(msgspec.Struct):
    page_or_route: str
    rows: list[PerfHistoryRow]


class PerfLatestResponse(msgspec.Struct):
    rows: list[PerfLatestRow]


class PerfRegressionResponse(msgspec.Struct):
    threshold_pct: float
    days: int
    regressions: list[PerfRegressionRow]


def _fmt_ts(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _row_to_history(snap: PerfSnapshot) -> PerfHistoryRow:
    return PerfHistoryRow(
        captured_at=_fmt_ts(snap.captured_at),
        commit_sha=snap.commit_sha,
        loc=snap.loc,
        effect_count=snap.effect_count,
        state_count=snap.state_count,
        derived_count=snap.derived_count,
        cc_max=snap.cc_max,
        cc_avg=snap.cc_avg,
        lcp_ms=snap.lcp_ms,
        tbt_ms=snap.tbt_ms,
        heap_mb=snap.heap_mb,
        route_p50_ms=snap.route_p50_ms,
        route_p95_ms=snap.route_p95_ms,
        route_qps=snap.route_qps,
    )


# ── Controller ───────────────────────────────────────────────────────────────

class PerfController(Controller):
    path = "/api/admin/perf"
    guards = [admin_guard]

    @get("/history")
    async def perf_history(
        self,
        page: str,
        days: int = 30,
    ) -> PerfHistoryResponse:
        """Time series for a single page/route (oldest → newest)."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        async with async_session() as session:
            q = (
                select(PerfSnapshot)
                .where(
                    PerfSnapshot.page_or_route == page,
                    PerfSnapshot.captured_at >= cutoff,
                )
                .order_by(PerfSnapshot.captured_at.asc())
            )
            result = (await session.execute(q)).scalars().all()
        return PerfHistoryResponse(
            page_or_route=page,
            rows=[_row_to_history(r) for r in result],
        )

    @get("/latest")
    async def perf_latest(self) -> PerfLatestResponse:
        """Latest snapshot per (side, page_or_route)."""
        async with async_session() as session:
            # DISTINCT ON requires ordering by the same leading columns.
            q = text(
                "SELECT DISTINCT ON (side, page_or_route) "
                "  id, side, page_or_route, captured_at, commit_sha, "
                "  loc, cc_max, cc_avg, lcp_ms, tbt_ms, heap_mb, "
                "  route_p50_ms, route_p95_ms, route_qps "
                "FROM perf_snapshots "
                "ORDER BY side, page_or_route, captured_at DESC"
            )
            rows_raw = (await session.execute(q)).mappings().all()

        rows: list[PerfLatestRow] = []
        for r in rows_raw:
            rows.append(PerfLatestRow(
                side=r["side"],
                page_or_route=r["page_or_route"],
                captured_at=_fmt_ts(r["captured_at"]),
                commit_sha=r["commit_sha"],
                loc=r["loc"],
                cc_max=r["cc_max"],
                cc_avg=r["cc_avg"],
                lcp_ms=r["lcp_ms"],
                tbt_ms=r["tbt_ms"],
                heap_mb=r["heap_mb"],
                route_p50_ms=r["route_p50_ms"],
                route_p95_ms=r["route_p95_ms"],
                route_qps=r["route_qps"],
            ))
        return PerfLatestResponse(rows=rows)

    @get("/regressions")
    async def perf_regressions(
        self,
        days: int = 7,
        threshold_pct: float = 10.0,
    ) -> PerfRegressionResponse:
        """Pages/routes where a metric exceeded threshold_pct% above N-day median.

        For each (page_or_route, metric) we compute:
          - The median of all readings in the last `days` days.
          - The most recent reading in the same window.
          - If (current - median) / |median| * 100 > threshold_pct → regression.

        Metrics with median=0 are skipped to avoid divide-by-zero.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        regressions: list[PerfRegressionRow] = []

        async with async_session() as session:
            # Fetch all rows in window; group by page in Python to avoid
            # complex SQL that's hard to read and optimise later.
            q = (
                select(PerfSnapshot)
                .where(PerfSnapshot.captured_at >= cutoff)
                .order_by(PerfSnapshot.page_or_route, PerfSnapshot.captured_at.asc())
            )
            all_rows = (await session.execute(q)).scalars().all()

        # Group by page_or_route.
        from collections import defaultdict
        by_page: dict[str, list[PerfSnapshot]] = defaultdict(list)
        for row in all_rows:
            by_page[row.page_or_route].append(row)

        for page_name, page_rows in by_page.items():
            side = page_rows[-1].side
            for col, _higher_is_worse in _REGRESSION_METRICS:
                values: list[float] = [
                    getattr(r, col)
                    for r in page_rows
                    if getattr(r, col) is not None
                ]
                if len(values) < 2:
                    continue
                median_val = statistics.median(values)
                if median_val == 0:
                    continue
                current_val = values[-1]
                delta_pct = (current_val - median_val) / abs(median_val) * 100.0
                if delta_pct > threshold_pct:
                    regressions.append(PerfRegressionRow(
                        page=page_name,
                        side=side,
                        metric=col,
                        current=current_val,
                        median=round(median_val, 2),
                        delta_pct=round(delta_pct, 1),
                    ))

        # Sort by severity (largest delta first).
        regressions.sort(key=lambda r: -r.delta_pct)
        return PerfRegressionResponse(
            threshold_pct=threshold_pct,
            days=days,
            regressions=regressions,
        )
