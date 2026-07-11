# Replay Driver Specification

Single source of truth for simulation playback. Defines replay state, playhead control,
event projection, and synchronization with SimDriver surfaces.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/api/algo/sim/replay_driver.py` · `backend/api/routes/replay.py` · `backend/api/models.py` (SimRecording) · `frontend/src/lib/ReplayScrubber.svelte`

---

## Contents

1. [SimReplayDriver Singleton](#1-simreplaydriver-singleton)
2. [Playback Mechanics](#2-playback-mechanics)
3. [Event Projection](#3-event-projection)
4. [Playhead Control](#4-playhead-control)
5. [Speed Control](#5-speed-control)
6. [Snapshot and State Sync](#6-snapshot-and-state-sync)
7. [SimRecording Schema](#7-simrecording-schema)
8. [Surface Synchronization](#8-surface-synchronization)
9. [Edge Cases](#9-edge-cases)
10. [Test Coverage Map](#10-test-coverage-map)

---

## 1. SimReplayDriver Singleton

SimReplayDriver is a separate module-level singleton that reuses SimDriver's shared surface
state (`_tick_log`, `_gtt_book`, `_paper`, `_price_history`, `_underlying_history`). Only one
replay can run per process.

### State

| Field | Type | Purpose |
|---|---|---|
| `active` | bool | True while replay is running |
| `recording_id` | int | Database row ID of the SimRecording |
| `recording_label` | str | Human-friendly name (from SimRecording.label) |
| `events` | list[dict] | Loaded event list (in-memory) |
| `cursor` | int | Next event index to emit (0 ≤ cursor ≤ total_events) |
| `speed` | float | Playback speed multiplier (1.0 = real-time) |
| `paused` | bool | True if playback paused |
| `total_events` | int | Length of events list (cached for snapshots) |
| `started_at` | datetime | Wall-clock time replay started |
| `_task` | asyncio.Task | Background tick loop coroutine |

### Lifecycle guard

`__init__()` is idempotent — multiple calls to `SimReplayDriver()` return the same instance
without resetting state mid-run. Canonical entry point is `SimReplayDriver.instance()`.

---

## 2. Playback Mechanics

### Load and initialize

On `start(recording_id, speed=1.0)`:

1. Query `SimRecording` table for the row
2. Parse `payload.events` (list of dicts with `t`, `kind`, `payload`)
3. Wipe SimDriver display state (`_tick_log`, `_gtt_book`, `_paper` reset)
4. Set `cursor = 0`, `active = True`
5. Start background `_run_loop()` task

### _run_loop() — playback engine

```
while active:
    if not paused:
        emit event at cursor
        cursor += 1
    if cursor >= total_events:
        active = False
        break
    wait = (next_event.t - current_event.t) / speed
    await asyncio.sleep(wait)
```

Waits are computed from relative timestamps in the recording (`t` is seconds since recording start).
Speed multiplier compresses or stretches the timeline.

### End-of-recording

When `cursor >= total_events`:
1. `active` flag set to False
2. Background task exits
3. Final event written to tick_log with tag `REPLAY:<label>:completed`
4. Operator sees empty playhead; can start a new recording or stop

---

## 3. Event Projection

### Event kinds and projections

| Kind | Projection | State Change |
|---|---|---|
| `gtt_placed` | `_gtt_book.place(gtt_order_id, symbol, trigger_price, ...)` | GTT added to pending book |
| `gtt_triggered` | GTT state → `triggered` | GTT matching set updated |
| `gtt_cancelled` | `_gtt_book.cancel(gtt_order_id)` | GTT removed from pending |
| `tick` | Append to `_tick_log`, update price_history | Chart data updated |
| `position_updated` | Re-fetch from broker (no sim mutation) | Positions row synced |
| `paper_fill` | Paper order status → FILLED | Order grid refreshed |
| `paper_unfilled` | Paper order status → UNFILLED | Order grid refreshed |

### No re-trigger logic

When `gtt_triggered` event is replayed:
- GTT order state changes to `triggered`
- It is NOT re-evaluated for a price cross
- No matter how many times the same event is replayed, the GTT does not fire again

This differs from live GTT evaluation, which runs price checks every tick. Replay is deterministic —
each event fires exactly once at its recorded `t`.

### Tick_log tagging

Every event is written to `_tick_log` with the format:
```
{"kind": "<original_kind>", "payload": <payload>, "replay_label": "<label>"}
```

So the UI's Simulator tab shows `REPLAY:<label>` prefix on all rows during playback.

---

## 4. Playhead Control

### step() — manual advance

When operator clicks Step (or presses a keyboard shortcut):

1. `if paused and cursor < total_events:` emit the event at cursor
2. `cursor += 1`
3. Pause remains True (no auto-resume)

Repeating step() walks through events one at a time.

### pause() — freeze playhead

Pauses the background `_run_loop()` without cancelling the task. The task remains alive
and respects the `paused` flag on the next loop iteration.

### resume() — continue playback

Sets `paused = False`. Background task resumes emitting events.

### stop() — terminate playback

1. Cancel the background `_run_loop()` task
2. Clear `events`, `cursor`, `active = False`
3. Cursor position lost (replay must start fresh)

### reset() — wipe state

Stops playback and clears all SimDriver surface state:
1. `_run_loop()` task cancelled
2. `_tick_log` wiped
3. `_gtt_book` cleared
4. `_paper` orders closed (UNFILLED)
5. `active`, `cursor`, `events` all reset to defaults

Used by test fixtures and operator to clean up between runs.

---

## 5. Speed Control

### Speed multiplier

`speed` is a float: 1.0 = real-time, 0.5 = 50% speed (half rate), 2.0 = 2x speed.

Presets offered to operator: `[0.5, 1.0, 2.0, 5.0, 10.0]`

### Wait recalculation

Each inter-event delay is recomputed as:
```
wait = (next_event.t - current_event.t) / speed
```

So speed changes take effect on the next event boundary, not mid-sleep.

### Practical implications

- Speed 0.5: 100-second recording plays in 200s
- Speed 5.0: 100-second recording plays in 20s
- Speed 10.0: 100-second recording plays in 10s

Pausing lets the operator stare at a particular frame; resuming continues at the set speed.

---

## 6. Snapshot and State Sync

### snapshot() method

Returns a dict matching the UI's expected state:
```json
{
  "active": true,
  "recording_id": 42,
  "recording_label": "Scenario: generic-crash",
  "cursor": 127,
  "total_events": 300,
  "progress": 0.423,
  "speed": 1.0,
  "paused": false,
  "started_at": "2026-07-11T14:30:00Z"
}
```

**Progress** is computed as `cursor / total_events` (0.0 to 1.0 range).

### UI synchronization

The frontend's `ReplayScrubber.svelte` polls `/api/simulator/status` (which includes replay
snapshot) every 500ms. No state divergence — UI always shows the playhead's real position.

### SimDriver surface sharing

During replay, `SimDriver.snapshot()` is NOT called (replay owns the playhead). Instead,
the UI reads `/api/simulator/status` + `/api/simulator/ticks/recent` to display:
- Tick log (from shared `_tick_log`)
- Chart price history (from shared `_price_history`)
- GTT book state (from shared `_gtt_book`)

All surfaces auto-update as events are replayed because the underlying data is the same.

---

## 7. SimRecording Schema

### Database row (`backend/api/models.py`)

| Column | Type | Purpose |
|---|---|---|
| `id` | int | Primary key |
| `scenario_name` | str | Original scenario used (e.g., "generic-crash") |
| `label` | str | User-friendly label (e.g., "Crash run 2026-07-11 14:30") |
| `payload` | JSONB | `{events: [{t: float, kind: str, payload: dict}, ...]}` |
| `created_at` | datetime | Timestamp |
| `duration_seconds` | float | Total span of recording (max `t` value) |

### Event payload shape

```json
{
  "events": [
    {
      "t": 0.0,
      "kind": "position_updated",
      "payload": {"symbol": "NIFTY24500CE", "last_price": 200.0}
    },
    {
      "t": 2.1,
      "kind": "paper_fill",
      "payload": {"algo_order_id": 99, "filled_price": 199.5}
    },
    {
      "t": 4.5,
      "kind": "gtt_triggered",
      "payload": {"gtt_order_id": 7, "trigger_price": 195.0}
    }
  ]
}
```

Relative timestamps (`t`) make replays reproducible at any speed.

---

## 8. Surface Synchronization

### Replay as transparent to downstream

When replay is active, all downstream readers (chart, order grid, Simulator tab) see
SimDriver's state exactly as if the original sim were re-running. No code branches needed.

### Simulator tab behavior

The Simulator tab consumes `_tick_log` (shared between SimDriver and SimReplayDriver):
- During live sim: sees real-time ticks tagged `[SIMULATOR]`
- During replay: sees replayed ticks tagged `REPLAY:<label>`
- Color/styling identical; only the tag differs

### Chart synchronization

Price history is built from replayed events, so the chart shows the original run's
price moves during replay. Operator can overlay replay runs for comparison (future feature).

### Order grid

AlgoOrder rows are NOT replayed (they're terminal state from the original run). Instead,
the grid shows the original orders + their original fill statuses. New orders cannot be placed
during replay — it's read-only playback mode.

---

## 9. Edge Cases

### Empty events list

`SimRecording.payload.events = []`:
1. `total_events = 0`
2. `cursor >= total_events` immediately true
3. `active` set to False on first loop iteration
4. UI shows "no events in recording"
5. Operator must stop and load a different recording

### Paused when cursor reaches end

If operator pauses replay at event N of N:
1. `cursor = N`
2. `paused = True`
3. Next loop iteration checks `cursor >= total_events` → sets `active = False`
4. UI shows "replay complete"

### Speed < 0.05

The driver does NOT clamp speed; operator can set arbitrarily low values.
Wait times become very long; playback appears frozen. No guard against this.

### Cursor overflow

If `cursor > total_events` (should never happen, but guard anyway):
`progress` division returns > 1.0. UI clamps progress bar to 100%.

### Pause during signal handling

If operator calls `pause()` while `_run_loop()` is in `await asyncio.sleep()`:
The task pauses immediately on the next loop check (respects `paused` flag). No race condition.

---

## 10. Test Coverage Map

### Backend — core

- **Load and initialize** — Recording fetched from DB, events parsed, cursor zeroed
- **step()** — Cursor advances, event projected, paused remains True
- **resume/pause** — Flag toggled, background task respects new state
- **speed control** — Wait times recalculated per speed multiplier
- **reset()** — All state cleared, _task cancelled, surfaces wiped

### Backend — event projection

- **gtt_placed** — GTT added to _gtt_book, no state change to existing orders
- **gtt_triggered** — GTT state transitions, not re-evaluated on subsequent ticks
- **tick_log tagging** — REPLAY:<label> prefix applied to all events

### Backend — snapshot

- **snapshot()** — Returns all required fields, progress computed correctly
- **progress precision** — 0.0 on start, 1.0 on end, linear between
- **no state divergence** — snapshot() and /api/simulator/status return identical state

### Frontend

- **ReplayScrubber** — Displays playhead, speed selector, play/pause/step buttons
- **Cursor visualization** — Progress bar fills left-to-right as cursor advances
- **Speed presets** — Radio buttons for [0.5x, 1.0x, 2.0x, 5.0x, 10.0x]

### Gaps

- Missing: Recording creation from live sim (producer side)
- Missing: Replay comparison view (two recordings side-by-side)
- Missing: Trimmed replay (operator selects start/end time from original recording)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
