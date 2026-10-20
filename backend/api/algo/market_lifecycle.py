"""
Market lifecycle event system — per-exchange open/close state machine.

A single in-memory singleton tracks per-exchange (`nse` / `mcx` / `cds`)
open/close state. On each 30s `poll()` call, transitions fire registered
async callbacks. Three event flavours per exchange:

  - `<exchange>:open`           — fired at session open (calendar-aware).
  - `<exchange>:close`          — fired at session close.
  - `<exchange>:close_settled`  — fired 15 minutes AFTER `<exchange>:close`.
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
from datetime import datetime, time as dtime, timedelta
from typing import Awaitable, Callable, Optional

from backend.shared.helpers.date_time_utils import (
    is_market_open, timestamp_indian,
)
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import config

logger = get_logger(__name__)


# Settled-close offset — operator-configurable. Default = 15 minutes.
# Kite publishes the adjusted close (weighted-avg-last-30-min) within
# ~10-15 min after session close; operator-approved to fire the
# `close_settled` handler at T+15 rather than T+45 so the daily snapshot
# lands sooner. Runtime override via /admin/settings ->
# `market_lifecycle.settled_offset_min` (previous value 45 still valid;
# raise it back if a broker delays the adjusted-close publish).
_DEFAULT_SETTLED_OFFSET_MIN = 15


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

        # One-shot guard for _hydrate_from_audit() — restart hydration
        # runs at most once per process lifetime, called from inside the
        # first poll().
        self._hydrated: bool = False

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
        await self._hydrate_from_audit()

        now_ist = timestamp_indian()

        # No calendar-date-rollover reset here by design (2026-10-20 fix) —
        # resetting `_settled_fired` on a bare midnight date flip caused
        # `close_settled` to fire a SECOND time immediately after midnight
        # for every exchange that was already closed (and had already
        # settled) well before midnight, since `_last_close_at` is left
        # untouched across rollover and the settled-offset window
        # (`now_ist >= last_close + offset`) is trivially satisfied the
        # instant the flag is cleared. That double-fire wrote a second,
        # spuriously-dated `daily_book` snapshot row every single night
        # (confirmed: 115/115 positions+holdings rows corrupted on
        # 2026-10-20), silently masked on ordinary days only because the
        # 08:00 IST repair job ran before anyone looked. The ONLY correct
        # re-arm point for `_settled_fired` is the open-transition reset
        # below (`<exch>:open`) — an exchange that reopens tomorrow clears
        # its own flag then, exactly once, tied to a REAL state transition
        # rather than the wall clock rolling over a date boundary.
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
            #
            # Belt-and-suspenders — if a segment defined multiple sessions
            # (currently unused, but shape supports it) OR the exchange
            # reopens via the ``evening_open_on_holidays`` rule inside the
            # settled window, skip the `close_settled` event entirely.
            # Firing during a live session would let the snapshot handler
            # capture mid-session LTPs as "settled close" values, which
            # poisons the close-override path in positions.py.
            if not is_open_now and not self._settled_fired.get(exch_lower, False):
                last_close = self._last_close_at.get(exch_lower)
                if last_close is not None:
                    offset = _settled_offset_minutes()
                    if now_ist >= last_close + timedelta(minutes=offset):
                        # Re-probe right now — if the exchange has reopened
                        # (evening session on a holiday) skip the settled
                        # event; it will not fire again this close cycle.
                        try:
                            reopened = _exchange_is_open(segment, now_ist)
                        except Exception:
                            reopened = False
                        if reopened:
                            logger.info(
                                f"market_lifecycle: skipping {exch_lower}:close_settled "
                                "— exchange has reopened (evening session on holiday?)"
                            )
                            self._settled_fired[exch_lower] = True
                        else:
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

    async def _hydrate_from_audit(self) -> None:
        """Restore ``_last_close_at`` / ``_settled_fired`` from
        ``MarketLifecycleEvent`` rows on process restart (one-shot,
        guarded by ``self._hydrated``, called from inside ``poll()``).

        Mirrors ``agent_engine.py``'s ``_v2_hydrate_latch()`` pattern: a
        pure, unit-testable replay helper (``_hydrate_lifecycle_from_rows``)
        plus a thin DB-querying async wrapper that applies the one
        time-dependent decision (see below) and installs the result.

        Without this, a restart landing inside ``[last_close, last_close +
        settled_offset)`` loses the in-memory ``_last_close_at`` entirely
        — a cold ``None`` — and the ``close_settled`` event for that close
        episode can silently never fire at all (a missed fire; the
        mirror-image risk of the double-fire D1a fixes).

        Lookback window is 26h (wide enough to cover MCX's 23:30 close
        plus a generously-raised settled_offset — the module's own
        constant doc notes 45 min is still a valid operator value — plus
        margin), NOT a bare "today's IST calendar date" bound, which
        would wrongly exclude a close fired late on the PRIOR calendar
        day for a cycle that is still open right now (e.g. a restart at
        00:10 IST recovering a 23:30 MCX close).

        Must-not-retroactively-fire rule: if the settled window has
        ALREADY elapsed by the time hydration runs (restart happened a
        long time after the close, and no ``close_settled`` row was ever
        recorded — e.g. the process crashed before it could fire), this
        marks that exchange as settled anyway rather than letting the
        very next ``poll()`` fire a stale snapshot write hours/days late.
        This matches the EXISTING cold-boot philosophy for the
        un-hydrated case (see ``_last_close_at``'s own docstring: "the
        settled event will not fire spuriously on a fresh boot after the
        close time has already elapsed") — hydration must not introduce
        a NEW way to fire late that didn't exist before this method did.
        """
        if self._hydrated:
            return
        self._hydrated = True
        try:
            from backend.api.database import async_session
            from backend.api.models import MarketLifecycleEvent
            from sqlalchemy import select as _select

            now_ist = timestamp_indian()
            lookback = now_ist - timedelta(hours=26)
            async with async_session() as session:
                result = await session.execute(
                    _select(
                        MarketLifecycleEvent.exchange,
                        MarketLifecycleEvent.event_type,
                        MarketLifecycleEvent.fired_at,
                    )
                    .where(MarketLifecycleEvent.fired_at >= lookback)
                    .where(MarketLifecycleEvent.event_type.in_(
                        ("open", "close", "close_settled")))
                    .order_by(MarketLifecycleEvent.fired_at.asc())
                )
                rows = result.all()
            last_close, settled = _hydrate_lifecycle_from_rows(rows)

            offset = timedelta(minutes=_settled_offset_minutes())
            for exch, ts in last_close.items():
                if settled.get(exch):
                    continue
                if now_ist >= ts + offset:
                    # Window already elapsed with no recorded fire —
                    # treat as handled, never fire retroactively.
                    settled[exch] = True

            for exch, ts in last_close.items():
                self._last_close_at[exch] = ts
            for exch, flag in settled.items():
                self._settled_fired[exch] = flag
            if last_close or settled:
                logger.info(
                    "market_lifecycle: hydrated from audit — "
                    "last_close_at=%s settled_fired=%s",
                    {k: v.isoformat() for k, v in last_close.items()}, settled,
                )
        except Exception as e:
            logger.warning(
                f"market_lifecycle: audit hydration failed (starting cold): {e}"
            )

    # ------------------------------------------------------------------
    # Testing helpers
    # ------------------------------------------------------------------

    def _reset_for_test(self) -> None:
        """Wipe state — test fixtures use this between cases."""
        self._open_state.clear()
        self._last_close_at.clear()
        self._settled_fired.clear()
        self._callbacks.clear()
        self._hydrated = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settled_offset_minutes() -> int:
    """Operator-tunable settled-close offset. Default 15 min.

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

    Each per-exchange entry carries:
      • name                   — segment key from yaml
      • exchange               — upper-case exchange name (NSE/MCX/CDS)
      • sessions               — list[{start: dtime, end: dtime}] from yaml.
                                 Falls back to a single [(hours_start,
                                 hours_end)] entry when the yaml still uses
                                 the legacy shape.
      • hours_start / hours_end — first session bookends. Kept because
                                 `_task_market_lifecycle` derives its poll
                                 cadence from the earliest / latest window.
      • evening_open_on_holidays — MCX-style rule (calendar-closed day
                                 with 17:00+ evening session).
    """
    raw = config.get("market_segments", {}) or {}
    out: dict[str, dict] = {}
    for seg_name, seg_cfg in raw.items():
        exch = (seg_cfg or {}).get("holiday_exchange", "NSE")
        sessions_raw = (seg_cfg or {}).get("sessions") or None
        parsed_sessions: list[dict] = []
        if sessions_raw:
            for s in sessions_raw:
                try:
                    parsed_sessions.append({
                        "start": _parse_time(s.get("start", "09:15")),
                        "end":   _parse_time(s.get("end",   "15:30")),
                    })
                except Exception:
                    continue
        if not parsed_sessions:
            parsed_sessions = [{
                "start": _parse_time((seg_cfg or {}).get("hours_start", "09:15")),
                "end":   _parse_time((seg_cfg or {}).get("hours_end",   "15:30")),
            }]
        first_start = parsed_sessions[0]["start"]
        last_end    = parsed_sessions[-1]["end"]
        out[exch.lower()] = {
            "name":             seg_name,
            "exchange":         exch,
            "sessions":         parsed_sessions,
            "hours_start":      first_start,
            "hours_end":        last_end,
            "evening_open_on_holidays": bool(
                (seg_cfg or {}).get("evening_open_on_holidays", False)
            ),
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
            "sessions":    list(equity["sessions"]),
            "hours_start": equity["hours_start"],
            "hours_end":   equity["hours_end"],
            "evening_open_on_holidays": False,
        }
    return out


def _hydrate_lifecycle_from_rows(
    rows: "list[tuple[str, str, datetime]]",
) -> "tuple[dict[str, datetime], dict[str, bool]]":
    """Pure helper — replay ``(exchange, event_type, fired_at)`` rows in
    ASCENDING ``fired_at`` order and reconstruct ``(last_close_at,
    settled_fired)`` dicts. Split out from ``_hydrate_from_audit`` (the
    DB-querying wrapper) so this can be unit-tested with plain tuples —
    no DB session needed, mirroring ``agent_engine.py``'s
    ``_hydrate_latch_from_rows`` convention.

    Replay semantics per exchange:
      - ``"open"``          — clears both (a fresh cycle begins; the
                               close episode before it is over).
      - ``"close"``         — sets ``last_close_at``, clears
                               ``settled_fired`` (a new close episode).
      - ``"close_settled"`` — sets ``settled_fired = True`` for the
                               CURRENT close episode.

    Rows must already be filtered to a bounded lookback window by the
    caller (no date/time logic here — purely the state-machine replay).
    """
    last_close: dict[str, datetime] = {}
    settled: dict[str, bool] = {}
    for exch, event_type, fired_at in rows:
        if not exch or fired_at is None:
            continue
        if event_type == "open":
            last_close.pop(exch, None)
            settled[exch] = False
        elif event_type == "close":
            last_close[exch] = fired_at
            settled[exch] = False
        elif event_type == "close_settled":
            settled[exch] = True
    return last_close, settled


def _exchange_is_open(segment: dict, now_ist: datetime) -> bool:
    """Per-exchange open verdict using the canonical is_market_open().

    Holiday set comes from broker_apis.fetch_holidays() — same cache
    that the existing `_task_close` uses, so no extra API quota. On
    fetch failure we fall back to an empty holiday set (open on
    holidays — better than spurious-closed transitions).

    Passes the session list + evening-open flag through so a single MCX
    holiday-evening reopen (calendar-closed day, ``now.time() >= 17:00``)
    is detected correctly.
    """
    try:
        from backend.brokers.broker_apis import fetch_holidays
        h_set = fetch_holidays(segment["exchange"])
    except Exception:
        h_set = set()
    try:
        from backend.brokers.broker_apis import fetch_special_sessions
        special = fetch_special_sessions(segment["exchange"])
    except Exception:
        special = []
    sessions_config = [
        {"start": f"{w['start'].hour:02d}:{w['start'].minute:02d}",
         "end":   f"{w['end'].hour:02d}:{w['end'].minute:02d}"}
        for w in segment.get("sessions", [])
    ] or None
    return is_market_open(
        now_ist,
        h_set,
        market_start=segment["hours_start"],
        market_end=segment["hours_end"],
        exchange=segment["exchange"],
        sessions=sessions_config,
        evening_open_on_holidays=segment.get("evening_open_on_holidays", False),
        special_sessions=special,
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
