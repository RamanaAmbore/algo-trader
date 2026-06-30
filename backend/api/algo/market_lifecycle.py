"""
Market lifecycle event system — per-exchange open/close state machine.

A single in-memory singleton tracks per-exchange (`nse` / `mcx` / `cds`)
open/close state. On each 30s `poll()` call, transitions fire registered
async callbacks. Three event flavours per exchange:

  - `<exchange>:open`           — fired at session open (calendar-aware).
  - `<exchange>:close`          — fired at session close.
  - `<exchange>:close_settled`  — fired 45 minutes AFTER `<exchange>:close`.
                                  This is the hook that catches the
                                  broker's adjusted close_price (Kite
                                  weighted-avg-last-30-min) which lands
                                  late in the broker response and is
                                  what the close-override path in
                                  positions.py needs to capture.

Handlers are async, fire-and-forget per event. A handler exception is
caught + logged + counted via the audit row's `handlers_failed` column;
the other handlers in the chain still run.

The singleton is **not** the SSOT for "is the market open right now?" —
that remains `is_market_open()` in `date_time_utils.py`. This module only
detects transitions and dispatches the events. Snapshot handlers + frontend
polling-gate callbacks subscribe via `register()`.

Per-exchange windows are pulled from `market_segments` in backend_config.yaml:
the `holiday_exchange` field is mapped to lower-case event prefix
(`NSE -> nse`, `MCX -> mcx`, `CDS -> cds`). Holiday-aware close-detection
uses the same `fetch_holidays(exchange)` + `weekday() < 5` gate as the
existing close-summary task.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time as dtime, timedelta
from typing import Awaitable, Callable, Optional

from backend.shared.helpers.date_time_utils import (
    is_market_open, timestamp_indian,
)
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config

logger = get_logger(__name__)


# Settled-close offset — operator-configurable. Default = 45 minutes per
# Kite's documented weighted-avg-last-30-min close-price calculation,
# with 15 min of slack so the broker has time to publish the adjusted
# value. Read fresh from settings on every poll so a /admin/settings
# change lands without restart.
_DEFAULT_SETTLED_OFFSET_MIN = 45


# Callback signature accepted by `register()`. Handlers are awaited
# directly inside `poll()`; they should be quick (kick off a snapshot,
# return) and any heavy lifting should be off-loaded via
# `asyncio.create_task`.
HandlerCB = Callable[[str, str], Awaitable[None]]


class MarketLifecycle:
    """Per-exchange open/close state machine. Singleton."""

    _instance: Optional["MarketLifecycle"] = None

    def __new__(cls) -> "MarketLifecycle":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init_state()
        return cls._instance

    def _init_state(self) -> None:
        # Per-exchange state: True = currently open, False = currently closed.
        # Lazily populated by the first poll() call.
        self._open_state: dict[str, bool] = {}

        # Per-exchange last-close timestamp (IST). Drives the 45-min
        # `close_settled` window — None means "no close seen yet this
        # process lifetime" so the settled event will not fire spuriously
        # on a fresh boot after the close time has already elapsed.
        self._last_close_at: dict[str, Optional[datetime]] = {}

        # Per-exchange `close_settled` already-fired marker for the
        # current close cycle. Cleared on every `<exchange>:open`.
        self._settled_fired: dict[str, bool] = {}

        # event -> list[handler]. Event keys: `<exchange>:open`,
        # `<exchange>:close`, `<exchange>:close_settled`.
        self._callbacks: dict[str, list[HandlerCB]] = {}

        # Last-seen calendar date for resetting daily state on rollover.
        self._last_seen_date: Optional[date] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self, event: str, callback: HandlerCB) -> None:
        """Register an async callback for an event like ``nse:close``.

        Callbacks are invoked with positional args ``(exchange, event_type)``:
            await callback("nse", "close")

        Multiple callbacks per event are allowed; insertion order is
        preserved. Duplicate registrations (same callable object) are
        silently de-duped so module-import idempotency holds.
        """
        if not event or ":" not in event:
            raise ValueError(f"event must be '<exchange>:<type>', got {event!r}")
        bucket = self._callbacks.setdefault(event, [])
        if callback not in bucket:
            bucket.append(callback)

    def get_state(self) -> dict:
        """Return a snapshot of current per-exchange state.

        Schema::

            {
                "open":              {"nse": bool, "mcx": bool, "cds": bool},
                "last_close_at":     {"nse": "2026-06-28T15:30:00+05:30", ...},
                "settled_fired":     {"nse": bool, "mcx": bool, "cds": bool},
                "handler_counts":    {"nse:close": int, ...},
            }
        """
        return {
            "open": dict(self._open_state),
            "last_close_at": {
                k: (v.isoformat() if v else None)
                for k, v in self._last_close_at.items()
            },
            "settled_fired": dict(self._settled_fired),
            "handler_counts": {ev: len(cbs) for ev, cbs in self._callbacks.items()},
        }

    async def poll(self) -> dict:
        """Detect transitions, fire callbacks, persist audit rows.

        Returns a dict listing the events that fired this tick:
            {"events": [{"exchange": "nse", "event_type": "close", ...}, ...]}

        Designed to be called once per ~30 s by ``_task_market_lifecycle``
        in background.py. Exception-safe: a handler that raises is
        logged + counted; other handlers in the same event still run,
        and the per-event audit row records ``handlers_failed > 0``.
        """
        now_ist = timestamp_indian()
        today = now_ist.date()

        # Date rollover — reset daily state so a fresh open transition
        # can fire tomorrow.
        if self._last_seen_date is None:
            self._last_seen_date = today
        elif self._last_seen_date != today:
            self._last_seen_date = today
            # Reset settled-fired markers; last_close_at stays so the
            # 45-min window can still fire across midnight (rare — MCX
            # close 23:30 + 45 min = 00:15 next day).
            self._settled_fired = {k: False for k in self._settled_fired}

        fired: list[dict] = []
        exchanges = _enumerate_exchanges()

        for exch_lower, segment in exchanges.items():
            # Resolve current open-state for this exchange via the
            # canonical is_market_open() helper. Holidays come from
            # cached fetch_holidays(); call is cheap (in-process LRU
            # keyed on (exchange, date)).
            try:
                is_open_now = _exchange_is_open(segment, now_ist)
            except Exception as e:
                logger.warning(
                    f"market_lifecycle: state probe failed for {exch_lower}: {e}"
                )
                continue

            prev_open = self._open_state.get(exch_lower)
            # Cold start — first poll seeds state without firing a
            # transition (otherwise every reboot would falsely emit
            # `:open` mid-session or `:close` overnight).
            if prev_open is None:
                self._open_state[exch_lower] = is_open_now
                self._settled_fired.setdefault(exch_lower, False)
                self._last_close_at.setdefault(exch_lower, None)
                continue

            # ── Open transition ──────────────────────────────────────
            if is_open_now and not prev_open:
                self._open_state[exch_lower] = True
                self._settled_fired[exch_lower] = False
                event_key = f"{exch_lower}:open"
                ran, failed = await self._dispatch(event_key, exch_lower, "open")
                fired.append({
                    "exchange":       exch_lower,
                    "event_type":     "open",
                    "handlers_run":   ran,
                    "handlers_failed": failed,
                })

            # ── Close transition ─────────────────────────────────────
            elif (not is_open_now) and prev_open:
                self._open_state[exch_lower] = False
                self._last_close_at[exch_lower] = now_ist
                event_key = f"{exch_lower}:close"
                ran, failed = await self._dispatch(event_key, exch_lower, "close")
                fired.append({
                    "exchange":       exch_lower,
                    "event_type":     "close",
                    "handlers_run":   ran,
                    "handlers_failed": failed,
                })

            # ── Settled-close window ─────────────────────────────────
            # Fire ONCE per close, `_settled_offset` minutes after the
            # close timestamp. The `not is_open_now` guard prevents the
            # event firing again after the next session's `open`.
            if not is_open_now and not self._settled_fired.get(exch_lower, False):
                last_close = self._last_close_at.get(exch_lower)
                if last_close is not None:
                    offset = _settled_offset_minutes()
                    if now_ist >= last_close + timedelta(minutes=offset):
                        self._settled_fired[exch_lower] = True
                        event_key = f"{exch_lower}:close_settled"
                        ran, failed = await self._dispatch(
                            event_key, exch_lower, "close_settled",
                        )
                        fired.append({
                            "exchange":       exch_lower,
                            "event_type":     "close_settled",
                            "handlers_run":   ran,
                            "handlers_failed": failed,
                        })

        # Persist audit rows (one per event fired). Fire-and-forget — a
        # DB outage must not stall the poll loop.
        if fired:
            asyncio.create_task(_persist_audit_rows(fired, now_ist))

        return {"events": fired}

    async def _dispatch(
        self, event_key: str, exchange: str, event_type: str,
    ) -> tuple[int, int]:
        """Run all handlers for ``event_key``. Returns (ran, failed)."""
        cbs = list(self._callbacks.get(event_key, []))
        if not cbs:
            return 0, 0
        ran = 0
        failed = 0
        for cb in cbs:
            try:
                await cb(exchange, event_type)
                ran += 1
            except Exception as e:
                failed += 1
                ran += 1
                logger.error(
                    f"market_lifecycle: handler {cb!r} for {event_key} "
                    f"raised: {e}"
                )
        logger.info(
            f"market_lifecycle: dispatched {event_key} — "
            f"ran={ran} failed={failed}"
        )
        return ran, failed

    # ------------------------------------------------------------------
    # Testing helpers
    # ------------------------------------------------------------------

    def _reset_for_test(self) -> None:
        """Wipe state — test fixtures use this between cases."""
        self._open_state.clear()
        self._last_close_at.clear()
        self._settled_fired.clear()
        self._callbacks.clear()
        self._last_seen_date = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settled_offset_minutes() -> int:
    """Operator-tunable settled-close offset. Default 45 min.

    Reads `market_lifecycle.settled_offset_min` from /admin/settings if
    present, otherwise the in-code default. Re-read per poll so a
    settings change lands without restart.
    """
    try:
        from backend.shared.helpers.settings import get_int
        return int(get_int(
            "market_lifecycle.settled_offset_min",
            _DEFAULT_SETTLED_OFFSET_MIN,
        ))
    except Exception:
        return _DEFAULT_SETTLED_OFFSET_MIN


def _parse_time(t: str) -> dtime:
    """`'09:15' -> dtime(9, 15)`. Defensive: malformed strings fall through
    to a wide-open default so the lifecycle never crashes on bad YAML."""
    try:
        h, m = (int(x) for x in str(t).split(":", 1))
        return dtime(h, m)
    except Exception:
        return dtime(9, 15)


def _enumerate_exchanges() -> dict[str, dict]:
    """Read market_segments from config and return the per-exchange
    segment dict keyed by lower-case exchange name.

    market_segments maps segment-name -> config. We invert this to
    exchange-name -> config, so the lifecycle tracks `nse` / `mcx` /
    `cds` independently. The `holiday_exchange` value is the canonical
    exchange identifier.
    """
    raw = config.get("market_segments", {}) or {}
    out: dict[str, dict] = {}
    for seg_name, seg_cfg in raw.items():
        exch = (seg_cfg or {}).get("holiday_exchange", "NSE")
        out[exch.lower()] = {
            "name":             seg_name,
            "exchange":         exch,
            "hours_start":      _parse_time((seg_cfg or {}).get("hours_start", "09:15")),
            "hours_end":        _parse_time((seg_cfg or {}).get("hours_end",   "15:30")),
        }
    # CDS shares the equity 09:15-15:30 window; backend_config.yaml lumps
    # it into the equity segment via `exchanges: [..., CDS]`. Surface it
    # as a distinct lifecycle exchange so frontend polling can gate on
    # `cds` independently of `nse`. Inherit equity hours when present.
    if "nse" in out and "cds" not in out:
        equity = out["nse"]
        out["cds"] = {
            "name":        "currency",
            "exchange":    "CDS",
            "hours_start": equity["hours_start"],
            "hours_end":   equity["hours_end"],
        }
    return out


def _exchange_is_open(segment: dict, now_ist: datetime) -> bool:
    """Per-exchange open verdict using the canonical is_market_open().

    Holiday set comes from broker_apis.fetch_holidays() — same cache
    that the existing `_task_close` uses, so no extra API quota. On
    fetch failure we fall back to an empty holiday set (open on
    holidays — better than spurious-closed transitions).
    """
    try:
        from backend.brokers.broker_apis import fetch_holidays
        h_set = fetch_holidays(segment["exchange"])
    except Exception:
        h_set = set()
    return is_market_open(
        now_ist,
        h_set,
        market_start=segment["hours_start"],
        market_end=segment["hours_end"],
        exchange=segment["exchange"],
    )


async def _persist_audit_rows(events: list[dict], now_ist: datetime) -> None:
    """Append one row per fired event to ``market_lifecycle_events``.

    Fire-and-forget. A DB outage logs and drops — the audit table is
    informational; missing rows do not block the lifecycle dispatch.
    """
    try:
        from backend.api.database import async_session
        from backend.api.models import MarketLifecycleEvent
    except Exception as e:
        logger.warning(f"market_lifecycle: audit imports unavailable: {e}")
        return
    try:
        async with async_session() as s:
            for ev in events:
                row = MarketLifecycleEvent(
                    exchange=ev["exchange"],
                    event_type=ev["event_type"],
                    handlers_run=int(ev.get("handlers_run", 0)),
                    handlers_failed=int(ev.get("handlers_failed", 0)),
                )
                s.add(row)
            await s.commit()
    except Exception as e:
        logger.warning(f"market_lifecycle: audit row persist failed: {e}")


# ---------------------------------------------------------------------------
# Singleton handle
# ---------------------------------------------------------------------------

market_lifecycle = MarketLifecycle()
