"""
perf_stats.py — Dev-only per-route latency + query-count capture.

Gated behind the `RAMBOQ_PERF_STATS=1` env var. When unset (production
default), this file is imported but no middleware is registered and no
SQLAlchemy event listener is attached — zero cost per request.

When enabled:
- Every non-suppressed HTTP request is timed via `time.perf_counter`.
- Every SQLAlchemy `execute` fires an event that increments a per-request
  query counter (keyed off contextvar).
- On process shutdown OR every 5 minutes (whichever comes first), the
  in-memory accumulator is serialised to `.log/perf_stats.json` with
  count / p50 / p95 / p99 / avg-query-count per route label.

Schema:

    {
      "GET /api/positions": {
        "count":       245,
        "p50_ms":      120,
        "p95_ms":      380,
        "p99_ms":      720,
        "queries_avg": 4.2
      },
      ...
    }

Route label uses `method + normalised_path` (path IDs collapsed to `:id`
so /api/orders/123 and /api/orders/456 group into one row).

Not intended for prod. Not intended for CI. Wire in manually when
diagnosing a specific latency regression, then unset the flag.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from litestar.middleware import ASGIMiddleware
from litestar.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────

# Flag check happens at module load AND at every request (route registered
# only when set at load, so runtime check is defensive).
_FLAG_ENV = "RAMBOQ_PERF_STATS"


def is_enabled() -> bool:
    """True when the RAMBOQ_PERF_STATS env var is set to any truthy value.

    Defined as a function so tests can patch os.environ at runtime without
    reloading the module.
    """
    return os.environ.get(_FLAG_ENV, "").lower() in ("1", "true", "yes", "on")


# Log dir (siblings of `.log/perf_baseline_*.json`).
_ROOT = Path(__file__).resolve().parents[3]
_LOG_DIR = _ROOT / ".log"
_OUT_PATH = _LOG_DIR / "perf_stats.json"

# Flush cadence when running as a long-lived process.
_FLUSH_INTERVAL_SEC = 300

# Suppress the same paths audit does — health polls + auth pings
# dominate any signal otherwise.
_SUPPRESS_PREFIXES = (
    "/api/health",
    "/api/auth/refresh",
    "/api/auth/whoami",
    "/api/auth/me",
)

# Path IDs to collapse. UUID-ish and pure-digit segments.
_RE_UUID = re.compile(
    r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_RE_INT_SEG = re.compile(r"/\d+(?=/|$)")


def _normalise_path(path: str) -> str:
    p = _RE_UUID.sub("/:uuid", path)
    p = _RE_INT_SEG.sub("/:id", p)
    return p


# ── Per-request query counter (contextvar-scoped) ──────────────────────────

_current_query_count: contextvars.ContextVar[int] = contextvars.ContextVar(
    "ramboq_perf_stats_qc", default=0
)


def _on_sqlalchemy_execute(*args: Any, **kwargs: Any) -> None:
    """SQLAlchemy `before_cursor_execute` handler. Increments the
    contextvar so the middleware can read the total for the request.
    Kept intentionally cheap — no logging, no allocation."""
    try:
        _current_query_count.set(_current_query_count.get() + 1)
    except LookupError:
        # No context (e.g. background task) — silently drop.
        pass


# ── Accumulator ────────────────────────────────────────────────────────────

# route_label → { "durations_ms": [float], "queries": [int] }
_ACC: dict[str, dict[str, list]] = {}


def _record(label: str, duration_ms: float, queries: int) -> None:
    row = _ACC.get(label)
    if row is None:
        row = {"durations_ms": [], "queries": []}
        _ACC[label] = row
    row["durations_ms"].append(duration_ms)
    row["queries"].append(queries)


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * pct
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def snapshot() -> dict[str, dict[str, float | int]]:
    """Compute the summary blob from _ACC without draining it."""
    out: dict[str, dict[str, float | int]] = {}
    for label, row in _ACC.items():
        durs = sorted(row["durations_ms"])
        qs = row["queries"]
        if not durs:
            continue
        out[label] = {
            "count":       len(durs),
            "p50_ms":      round(_percentile(durs, 0.50), 1),
            "p95_ms":      round(_percentile(durs, 0.95), 1),
            "p99_ms":      round(_percentile(durs, 0.99), 1),
            "queries_avg": round(sum(qs) / len(qs), 2) if qs else 0.0,
        }
    return out


def flush(reason: str = "manual") -> Path | None:
    """Write the current snapshot to `.log/perf_stats.json`. Returns the
    path on success, None if the accumulator is empty."""
    snap = snapshot()
    if not snap:
        return None
    _LOG_DIR.mkdir(exist_ok=True, parents=True)
    payload = {"reason": reason, "routes": snap}
    _OUT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True))
    logger.info("[perf_stats] flushed %d routes → %s (reason=%s)",
                len(snap), _OUT_PATH, reason)
    return _OUT_PATH


# ── Middleware ─────────────────────────────────────────────────────────────

class PerfStatsMiddleware(ASGIMiddleware):
    """Times every HTTP request and records the count of SQL statements
    that ran inside the request's context. No effect on non-HTTP scopes
    (WebSocket, lifespan) — those pass through untouched."""

    async def handle(self, scope: Scope, receive: Receive, send: Send,
                     next_app: ASGIApp) -> None:
        if scope.get("type") != "http":
            await next_app(scope, receive, send)
            return

        path = (scope.get("path") or "").rstrip("/") or "/"
        if any(path.startswith(p) for p in _SUPPRESS_PREFIXES):
            await next_app(scope, receive, send)
            return

        method = scope.get("method") or "GET"
        label = f"{method} {_normalise_path(path)}"

        # Reset the counter for this request. We MUST call set() (not
        # just `.get()`), otherwise concurrent requests bleed counts
        # into one another when they share a context chain.
        _current_query_count.set(0)

        t0 = time.perf_counter()
        try:
            await next_app(scope, receive, send)
        finally:
            duration_ms = (time.perf_counter() - t0) * 1000.0
            queries = _current_query_count.get()
            _record(label, duration_ms, queries)


# ── Wiring ─────────────────────────────────────────────────────────────────

_LISTENER_ATTACHED = False
_FLUSH_TASK: asyncio.Task | None = None


def attach_sqlalchemy_listener() -> None:
    """Attach the `before_cursor_execute` listener to the shared engine.
    Idempotent — safe to call multiple times.

    Kept as a separate function (not module-level) so importing this
    module does NOT touch the engine when the flag is off. The wiring
    call happens once from `backend/api/app.py` inside the flag branch.
    """
    global _LISTENER_ATTACHED
    if _LISTENER_ATTACHED:
        return
    try:
        from sqlalchemy import event
        from backend.api.database import engine
        # Async engine — hook the sync-side pool for the raw cursor event.
        event.listen(engine.sync_engine, "before_cursor_execute",
                     _on_sqlalchemy_execute)
        _LISTENER_ATTACHED = True
        logger.info("[perf_stats] SQLAlchemy event listener attached")
    except Exception as e:
        # Never let a diagnostic listener take down the API.
        logger.warning("[perf_stats] listener attach failed: %s", e)


async def periodic_flush_loop() -> None:
    """5-minute flush loop — call from startup hook so the JSON file
    stays fresh even without a graceful shutdown."""
    while True:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL_SEC)
            flush(reason="interval")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("[perf_stats] periodic flush error: %s", e)


def start_background_flusher() -> None:
    """Register the periodic flusher on the running event loop.
    No-op if already started."""
    global _FLUSH_TASK
    if _FLUSH_TASK and not _FLUSH_TASK.done():
        return
    try:
        loop = asyncio.get_running_loop()
        _FLUSH_TASK = loop.create_task(periodic_flush_loop())
    except RuntimeError:
        # No loop yet (called during import) — startup hook will retry.
        pass


def shutdown_flush() -> None:
    """Final flush at shutdown so the last window's data isn't lost."""
    flush(reason="shutdown")
