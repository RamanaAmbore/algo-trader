"""
Sim replay driver — re-emits a recorded sim run at configurable speed.

A `SimRecording` (produced by SimDriver when record_mode=True) carries
a `payload.events` list — every state-mutating event from the original
run, each carrying a relative `t` (seconds since the recording started).
This driver consumes that list and replays the events into the same
surfaces the original run wrote to:

  • driver._tick_log              — Simulator tab log
  • driver._gtt_book              — sim GTT lifecycle
  • driver._paper                 — paper-trade chase orders
  • driver._underlying_history    — chart line data
  • driver._price_history         — chart line data

Operator's screen during playback looks IDENTICAL to the original run
because the same downstream consumers (Simulator panel, mini-charts,
LogPanel) are re-fed the same events.

Speed control: tick rate = original_dt × speed_factor. 1.0× plays at
real time; 5.0× compresses 5 seconds into 1. Pause + step interact
with the playhead like a media player.

The replay driver shares the SimDriver SINGLETON's surface attrs (so
the existing /api/simulator/status snapshot Just Works during replay).
A `_replay_active` flag distinguishes "live sim" from "replay" so the
real run path can never step on the playhead.

Industry analogue: NinjaTrader Market Replay's playback engine.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# Default speeds offered in the UI. The driver accepts any positive
# float; these are convenient presets.
PLAYBACK_SPEEDS: list[float] = [0.5, 1.0, 2.0, 5.0, 10.0]


class SimReplayDriver:
    """Singleton replay driver. Reuses SimDriver._tick_log / _gtt_book /
    _paper so the operator's UI surfaces see identical state during
    playback as during the original run."""

    _instance: Optional["SimReplayDriver"] = None
    _initialized: bool = False

    def __new__(cls) -> "SimReplayDriver":
        # Return the existing instance for every construction call so that
        # SimReplayDriver() and SimReplayDriver.instance() are interchangeable.
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self) -> None:
        # Guard: __init__ runs every time SimReplayDriver() is called directly.
        # The canonical entry point is SimReplayDriver.instance(), but direct
        # calls must never reset already-initialised replay state mid-run.
        if self.__class__._initialized:
            return
        self.__class__._initialized = True

        self.active: bool = False
        self.recording_id: Optional[int] = None
        self.recording_label: str = ""
        self.events: list[dict] = []
        self.cursor: int = 0           # next event index to emit
        self.speed: float = 1.0        # playback speed multiplier
        self.paused: bool = False
        self.started_at: Optional[datetime] = None
        self._task: Optional[asyncio.Task] = None
        # Recording-event count cached for snapshot.
        self.total_events: int = 0

    @classmethod
    def instance(cls) -> "SimReplayDriver":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Snapshot ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            "active":          self.active,
            "recording_id":    self.recording_id,
            "recording_label": self.recording_label,
            "cursor":          self.cursor,
            "total_events":    self.total_events,
            "progress":        (self.cursor / self.total_events)
                                if self.total_events else 0.0,
            "speed":           self.speed,
            "paused":          self.paused,
            "started_at":      self.started_at.isoformat() if self.started_at else None,
        }

    # ── Lifecycle ────────────────────────────────────────────────────

    async def start(self, recording_id: int, speed: float = 1.0) -> dict:
        """Load the recording from DB and start playback in a background task."""
        if self.active:
            raise RuntimeError("Replay already running — stop it first.")

        from sqlalchemy import select
        from backend.api.database import async_session
        from backend.api.models import SimRecording

        async with async_session() as s:
            row = await s.execute(
                select(SimRecording).where(SimRecording.id == recording_id)
            )
            rec = row.scalars().first()
        if rec is None:
            raise RuntimeError(f"Recording id={recording_id} not found")

        events = (rec.payload or {}).get("events") or []
        if not events:
            raise RuntimeError(f"Recording id={recording_id} has no events")

        # Wipe the SimDriver's display state so the operator's screen
        # starts from a clean slate. Replay re-emits events into the
        # same buffers the original run wrote to.
        from backend.api.algo.sim.driver import SimDriver
        sim = SimDriver.instance()
        sim._tick_log.clear()
        sim._gtt_book.reset()
        sim._paper.reset()
        sim._price_history.clear()
        sim._underlying_history.clear()

        self.active = True
        self.paused = False
        self.recording_id = recording_id
        self.recording_label = rec.label or f"recording #{recording_id}"
        self.events = events
        self.total_events = len(events)
        self.cursor = 0
        self.speed = max(0.05, float(speed))   # guard against 0/negative
        self.started_at = datetime.now()

        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"[REPLAY] starting #{recording_id} ({self.total_events} events) "
            f"at {self.speed}x"
        )
        return self.snapshot()

    def reset(self) -> None:
        """Reset to idle state. Called by tests and stop() to clear stale playhead."""
        if self._task and not self._task.done():
            self._task.cancel()
        self.active = False
        self.recording_id = None
        self.recording_label = ""
        self.events = []
        self.cursor = 0
        self.speed = 1.0
        self.paused = False
        self.started_at = None
        self._task = None
        self.total_events = 0

    async def stop(self) -> dict:
        if not self.active:
            return self.snapshot()
        self.reset()
        logger.info(f"[REPLAY] stopped")
        return self.snapshot()

    def pause(self) -> dict:
        if self.active:
            self.paused = True
        return self.snapshot()

    def resume(self) -> dict:
        if self.active:
            self.paused = False
        return self.snapshot()

    async def step(self) -> dict:
        """Emit exactly one event then pause. Useful for deterministic
        debugging — operator clicks Step in the UI and watches each
        event land one at a time."""
        if not self.active or self.cursor >= self.total_events:
            return self.snapshot()
        self.paused = True
        await self._emit_one()
        return self.snapshot()

    # ── Run loop ─────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        try:
            prev_t: float = 0.0
            while self.active and self.cursor < self.total_events:
                if self.paused:
                    await asyncio.sleep(0.1)
                    continue
                next_t = float(self.events[self.cursor].get("t") or 0.0)
                wait = max(0.0, (next_t - prev_t) / self.speed)
                if wait > 0:
                    await asyncio.sleep(wait)
                if self.paused or not self.active:
                    continue
                await self._emit_one()
                prev_t = next_t
            # End of recording — auto-stop with a terminal log entry.
            if self.cursor >= self.total_events:
                from backend.api.algo.sim.driver import SimDriver
                sim = SimDriver.instance()
                sim._tick_log.append({
                    "ts":         datetime.now().isoformat(timespec="seconds"),
                    "tick_index": self.cursor,
                    "scenario":   f"REPLAY:{self.recording_label}",
                    "kind":       "replay-complete",
                    "moves":      [],
                    "changes":    [],
                    "note":       f"replay #{self.recording_id} complete",
                })
                self.active = False
                logger.info(f"[REPLAY] completed #{self.recording_id}")
        except asyncio.CancelledError:
            logger.info("[REPLAY] task cancelled")
        except Exception as e:
            logger.error(f"[REPLAY] _run_loop crashed: {e}")
            self.active = False

    async def _emit_one(self) -> None:
        """Process one event into the SimDriver's display buffers."""
        if self.cursor >= self.total_events:
            return
        evt = self.events[self.cursor]
        self.cursor += 1

        from backend.api.algo.sim.driver import SimDriver
        sim = SimDriver.instance()

        kind = str(evt.get("kind") or "")
        payload = evt.get("payload") or {}

        # Every event lands in the tick_log so the Simulator tab shows
        # the same stream the original run produced.
        sim._tick_log.append({
            "ts":         datetime.now().isoformat(timespec="seconds"),
            "tick_index": self.cursor,
            "scenario":   f"REPLAY:{self.recording_label}",
            "kind":       kind,
            "moves":      payload.get("moves") or [],
            "changes":    payload.get("changes") or [],
            "note":       payload.get("note"),
            "order":      payload.get("order"),
        })

        # Surface-specific projections — replay the GTT book lifecycle
        # so the GTT strip in the UI shows the same arc as the original.
        if kind == "gtt_placed":
            try:
                sim._gtt_book.place(
                    account=payload["account"],
                    tradingsymbol=payload["tradingsymbol"],
                    exchange=payload["exchange"],
                    trigger_type=payload["trigger_type"],
                    trigger_values=list(payload.get("trigger_values") or []),
                    orders=list(payload.get("orders") or []),
                    last_price=float(payload.get("last_price") or 0.0),
                    pair_with=payload.get("pair_with"),
                    template_id=payload.get("template_id"),
                    parent_order_id=payload.get("parent_order_id"),
                    tag=payload.get("tag"),
                )
            except Exception as e:
                logger.warning(f"[REPLAY] gtt_placed replay failed: {e}")
        elif kind == "gtt_cancelled":
            gtt_id = payload.get("gtt_id")
            if gtt_id:
                sim._gtt_book.cancel(gtt_id, reason=payload.get("reason", "replay"))
        elif kind == "gtt_triggered":
            # Mark the GTT triggered in the book so its status pill
            # flips visually. We don't re-fire the chase order here
            # (that would double up — the chase_placed event handles it).
            gtt_id = payload.get("gtt_id")
            gtt = sim._gtt_book.get(gtt_id) if gtt_id else None
            if gtt is not None and gtt.is_active():
                from backend.api.algo.sim.gtt_book import GTT_STATUS_TRIGGERED
                gtt.status = GTT_STATUS_TRIGGERED
                gtt.triggered_at = datetime.now()
                gtt.triggered_leg_index = payload.get("leg_index")


# ── Module-level singleton accessor ─────────────────────────────────

def get_replay_driver() -> SimReplayDriver:
    return SimReplayDriver.instance()
