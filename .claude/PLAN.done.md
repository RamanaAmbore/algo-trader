# Plan: Exchange schedule table + 3 UI bug fixes

## Context

Four independent issues, batched into one deploy:

1. **Exchange schedule** — Exchange timing is scattered across three places: `_EXCHANGE_TO_GATE`
   dict in `snapshot_gate.py`, `market_segments` YAML in `backend_config.yaml`, and hardcoded
   trigger times in `background.py` (`_NSE_SETTLEMENT_H=16`, `_MCX_CLOSE_H=23`, etc.). Adding a
   new exchange requires code changes in all three. Goal: one `exchange_schedule` DB table is the
   single source of truth for ALL timing — open/close windows, snapshot triggers, settlement
   triggers, cutoff timestamps. Default records editable from frontend; date-specific overrides
   for holidays and special sessions. `background.py`, `snapshot_gate.py`, `positions.py`, and
   `holdings.py` all read from the DB-backed cache.

2. **NavStrip heartbeat invisible** — `.ps-strip.ps-heartbeat` only changes
   `border-bottom-color` from `rgba(251,191,36,0.30)` (base `--algo-amber-border-soft`) to
   `rgba(251,191,36,0.75)` — a 0.45 opacity shift on a 1 px border is imperceptible. There
   is no `@keyframes` animation. Reported twice ("I don't see any animation refreshing nav
   strip").

3. **Payoff curve spot price drift** — `liveSpot` in `derivatives/+page.svelte` uses the SSE
   snapshot store (`getSnapshot(anchor)?.ltp`) but for MCX virtual roots ("CRUDEOIL") the
   ticker publishes under the actual contract key ("CRUDEOIL26JUNFUT") — key mismatch causes
   fallthrough to the positions-API `underlying_ltp` which can lag by up to 5 s.

4. **Snapshot day P&L% oscillation** — `_overlay_snapshot_for_closed_exchanges` in
   `positions.py:540` uses `broker_ltp` as fallback when `snap_ltp is None`. After market
   close `broker_ltp` fluctuates slightly, causing `day_change_val` and
   `day_change_percentage` to oscillate on every poll.

---

## New table: `exchange_schedule`

### SQLAlchemy model

```python
class ExchangeSchedule(Base):
    __tablename__ = "exchange_schedule"

    id                  : int         # SERIAL PK
    gate                : str         # "NSE" | "MCX" — logical group name
    exchanges           : list[str]   # TEXT[] — exchanges in this gate
    date                : date | None # NULL = recurring default; specific date = override
    weekdays            : list[int] | None  # INT[] — ISO weekday numbers [1..7]; 1=Mon,7=Sun
                                           # NULL on date-override records
    session_name        : str         # "regular" | "morning" | "evening" | "muhurat" | "closed"
    is_open             : bool        # False → this row marks the gate closed for the day
    open_time           : time | None # IST open  (NULL when is_open=False)
    close_time          : time | None # IST close (NULL when is_open=False)
    snapshot_time       : time | None # IST: when daily_book LTP snapshot is captured
                                     # Only set on the LAST session of the day
    snapshot_reset_time : time | None # IST: when prev_close rolls over to yesterday's
                                     # settlement (replaces hardcoded 08:00 in 4 places)
                                     # Only set on the session carrying the settlement
    reason              : str | None  # "Independence Day", "Diwali Muhurat 2026"
    source              : str         # "legacy_seed" | "operator"

    __table_args__ = (
        UniqueConstraint("gate", "date", "session_name",
                         name="uq_exchange_schedule_gate_date_session"),
        Index("ix_exchange_schedule_gate_date", "gate", "date"),
    )
```

### Default seed records (date=NULL, inserted ON CONFLICT DO NOTHING at startup)

| gate | exchanges | date | weekdays | session_name | is_open | open | close | snapshot | reset |
|---|---|---|---|---|---|---|---|---|---|
| NSE | {NSE,BSE,NFO,BFO,CDS} | NULL | {1,2,3,4,5} | regular | true | 09:15 | 15:30 | 15:31 | 08:00 |
| NSE | {NSE,BSE,NFO,BFO,CDS} | NULL | {1,2,3,4,5} | **settlement** | **false** | NULL | NULL | **16:15** | NULL |
| MCX | {MCX} | NULL | {1,2,3,4,5} | **morning** | true | 09:00 | **17:00** | **NULL** | **NULL** |
| MCX | {MCX} | NULL | {1,2,3,4,5} | **evening** | true | **17:00** | 23:30 | 23:31 | 08:00 |
| MCX | {MCX} | NULL | {1,2,3,4,5} | **settlement** | **false** | NULL | NULL | **00:15** | NULL |

`settlement` rows have `is_open=false` — they are not trading windows. They exist solely to
carry a `snapshot_time` so `background.py` knows when to run the final settlement capture
(NSE BHAV at 16:15; MCX final at 00:15). `background.py` distinguishes close-snapshot vs
settlement-capture by checking `session_name == "settlement"`.
MCX morning window does not trigger a snapshot — only evening + settlement do.
`weekdays={1,2,3,4,5}` = Mon–Fri. Weekends are implicitly closed — no Sat/Sun records needed.

### Date-override record examples (user-entered from settings UI)

**Holiday close (one row suppresses ALL sessions for the gate that day):**

| gate | date | session_name | is_open | open | close | snapshot | reset | reason |
|---|---|---|---|---|---|---|---|---|
| NSE | 2026-08-15 | **closed** | **false** | NULL | NULL | NULL | NULL | Independence Day |
| MCX | 2026-08-15 | **closed** | **false** | NULL | NULL | NULL | NULL | Independence Day |

A single `is_open=false` row with `session_name="closed"` suppresses both MCX morning and
evening for that date — no need to override each session individually.

**Special session (Diwali Muhurat — equity open, F&O closed):**

| gate | exchanges | date | session_name | is_open | open | close | snapshot | reset | reason |
|---|---|---|---|---|---|---|---|---|---|
| NSE | {NSE,BSE} | 2026-11-01 | **muhurat** | true | 17:45 | 18:45 | 18:46 | 08:00 | Diwali Muhurat |

`exchanges={NSE,BSE}` — only equity segments open. NFO, BFO, CDS are NOT in this row's
`exchanges` array, so `resolve_sessions_for("NFO", 2026-11-01)` returns [] → NFO correctly
closed. No separate closed override needed for F&O.

**Adding a new exchange** (e.g., a future "GIFT" exchange with NSE hours): from the settings UI,
add `"GIFT"` to the default row's `exchanges` array → immediately included in NSE schedule.
Or create a new default row with gate="GIFT", exchanges=["GIFT"], own times — zero code changes.

### Runtime lookup algorithm

`gate` is a **grouping label** (used in the UI and for background.py snapshot triggers).
The actual open/close check matches on the `exchanges` TEXT[] column so that an override
row can target a subset of exchanges within a gate (e.g., Muhurat opens NSE+BSE but not NFO).

```
resolve_sessions_for(exchange, today) → list[ExchangeSchedule]:
  # Step 1 — look for date-specific rows that include this exchange
  rows = SELECT * WHERE exchange = ANY(exchanges) AND date = today

  if rows:
    if any(r.is_open == False for r in rows):
      return []        # closed row found for this exchange → closed
    return rows        # open rows only → these are today's sessions

  # Step 2 — no date rows → use recurring default
  defaults = SELECT * WHERE exchange = ANY(exchanges) AND date IS NULL
  if not defaults or today.isoweekday() not in (defaults[0].weekdays or []):
    return []          # weekend or unknown exchange → closed
  return defaults      # MCX gets morning + evening; NSE-family gets regular

is_exchange_open(exchange, at):
  sessions = resolve_sessions_for(exchange, at.date())
  return any(s.open_time <= at.time() < s.close_time for s in sessions
             if s.is_open and s.open_time is not None)

# gate-level resolve used only by background.py snapshot triggers
resolve_sessions_for_gate(gate, today) → list[ExchangeSchedule]:
  # Returns ALL sessions (open + settlement) for a gate — date-override-aware
  rows = SELECT * WHERE gate = gate AND date = today
  if rows:
    return rows
  defaults = SELECT * WHERE gate = gate AND date IS NULL
  if not defaults or today.isoweekday() not in (defaults[0].weekdays or []):
    return []
  return defaults
```

**Why two resolvers**: `resolve_sessions_for(exchange)` drives per-exchange open/close
decisions (routes, snapshot_gate). `resolve_sessions_for_gate(gate)` drives background.py
snapshot/settlement triggers — those fire per gate regardless of which individual exchanges
in the gate have overrides.

### Snapshot trigger logic (fully replaces `background.py` hardcoded times)

```python
# background.py: every minute, check all sessions with snapshot_time == now
sessions_now = exchange_clock.sessions_with_snapshot_time_now()
for session in sessions_now:
    if session.session_name == "settlement":
        await trigger_settlement_capture(session.gate)   # final BHAV/settlement pass
    else:
        await trigger_close_snapshot(session.gate)       # daily_book.ltp capture at close
```

Five triggers per day (Mon–Fri):
- **NSE 15:31** — close snapshot (regular session)
- **NSE 16:15** — NSE BHAV settlement capture (settlement session, is_open=false)
- **MCX 23:31** — close snapshot (evening session)
- **MCX 00:15** — MCX final settlement capture (settlement session, is_open=false)
- (MCX 09:00–17:00 morning: no snapshot — evening session owns that)

### What `snapshot_time` and `snapshot_reset_time` drive

| Field | Meaning | Replaces |
|---|---|---|
| `snapshot_time` | IST time background.py fires close-snapshot or settlement-capture | Hardcoded `_NSE_SETTLEMENT_H/M`, `_MCX_CLOSE_H/M`, `_MCX_SETTLEMENT_H/M` in `background.py`; YAML `market_segments` block |
| `snapshot_reset_time` | `captured_at <` cutoff for daily_book settlement queries | Hardcoded `today_08:00 IST` in `positions.py` (×4) and `holdings.py` (×1) |

### Symbol → gate mapping

```
exchange  →  gate
NSE, BSE, NFO, BFO, CDS  →  "NSE"
MCX                       →  "MCX"
```

### Legacy tables

`market_holidays` and `market_special_sessions` kept intact — seeded into `exchange_schedule`
at startup (`ON CONFLICT DO NOTHING`), then become read-only.
`market_segments` YAML block in `backend_config.yaml` — **removed** after `background.py`
migrates to DB-driven scheduling. No longer read by any code.

---

## New module: `backend/api/helpers/exchange_clock.py`

Module-level async-loaded cache (1-hour TTL). Sync API safe after warm.

```python
# --- Internal helpers ---

EXCHANGE_TO_GATE = {"NSE":"NSE","BSE":"NSE","NFO":"NSE","BFO":"NSE","CDS":"NSE","MCX":"MCX"}

def _gate(exchange: str) -> str:
    return EXCHANGE_TO_GATE.get(exchange.upper(), exchange.upper())

def _resolve_for_exchange(exchange: str, on: date) -> list[ExchangeSchedule]:
    """Per-exchange lookup: filters cache by exchange = ANY(row.exchanges).
    Date-specific rows take priority; falls back to date IS NULL defaults.
    Returns [] if closed (weekend, is_open=False row, or no matching rows)."""

def _resolve_for_gate(gate: str, on: date) -> list[ExchangeSchedule]:
    """Gate-level lookup: filters cache by gate (includes settlement rows).
    Used only by background.py for snapshot/settlement trigger scheduling."""

# --- Public sync API (cache must be warm) ---

def is_exchange_open(exchange: str, *, at: datetime | None = None) -> bool:
    """True if exchange is inside an active session window at `at` (default: now IST).
    Uses _resolve_for_exchange — Muhurat row with exchanges=[NSE,BSE] correctly
    closes NFO even though both share gate=NSE."""

def is_exchange_closed(exchange: str, *, at: datetime | None = None) -> bool:
    return not is_exchange_open(exchange, at=at)

def snapshot_time_for(exchange: str, *, on: date | None = None) -> time | None:
    """IST close-snapshot time for exchange on given date (open sessions only)."""

def snapshot_reset_time_for(exchange: str, *, on: date | None = None) -> time:
    """IST prev_close reset time for exchange. Defaults to 08:00 if not set."""

# --- Public async API ---

async def settlement_cutoff_for(exchange: str) -> datetime:
    """Prior-session settlement boundary (IST).
    = today + snapshot_reset_time if now >= reset_time, else yesterday + reset_time."""

async def settlement_ref_close_map(
    exchange: str,
    kind: str,
    pairs: list[tuple[str, str]],
) -> dict[tuple[str, str], float]:
    """daily_book.ltp WHERE captured_at < settlement_cutoff_for(exchange) for given pairs.
    Replaces hardcoded 08:00 cutoff queries in positions.py and holdings.py."""

def sessions_with_snapshot_time_now(*, at: datetime | None = None) -> list[ExchangeSchedule]:
    """All sessions (open OR settlement, any gate) whose snapshot_time matches
    the current IST minute. Uses _resolve_for_gate internally.
    Used by background.py — gate-level, not per-exchange."""

async def refresh_cache() -> None:
    """Reload exchange_schedule from DB. Called at startup and hourly."""

async def seed_from_config(session: AsyncSession) -> None:
    """Read market_segments YAML + market_holidays + market_special_sessions,
    insert seed rows ON CONFLICT DO NOTHING."""

def apply_settlement_overlay(kind: str):
    """Decorator for async functions returning list[Row].
    For closed-exchange rows: fetches ref_close via settlement_ref_close_map,
    patches day_change_val, day_change_percentage, close_price.
    Only patches when BOTH snap_ltp is not None AND ref_close > 0."""
```

---

## New backend admin route: `backend/api/routes/exchange_schedule.py`

```
GET  /api/admin/exchange-schedule        → list all rows (sorted: defaults first, then by date)
PUT  /api/admin/exchange-schedule        → upsert row: ON CONFLICT (gate, date) DO UPDATE
DELETE /api/admin/exchange-schedule/{id} → delete a date-override row (default rows blocked)
```

`PUT` payload:
```json
{
  "gate": "NSE",
  "exchanges": ["NSE", "BSE"],   // subset of gate's exchanges; null → all exchanges in gate
  "date": "2026-11-01",          // null or omitted → upsert the default record
  "session_name": "muhurat",
  "is_open": true,
  "open_time": "17:45",
  "close_time": "18:45",
  "snapshot_time": "18:46",
  "snapshot_reset_time": null,
  "reason": "Diwali Muhurat 2026"
}
```
When `exchanges` is null/omitted on a PUT, the server fills it with all exchanges for that
gate from the existing default row — so editing a default record doesn't require re-specifying
the exchange list.

After any write, call `exchange_clock.refresh_cache()` so in-memory cache reflects the change immediately.

---

## New frontend settings section: Exchange Schedule

**File**: `frontend/src/routes/(algo)/admin/settings/+page.svelte` (or nearest admin settings page)

### Schedule table (read + manage)

Two groups:
- **Default schedules** (date=NULL) — all sessions per gate; editable; `[+ Add Session]` per gate
- **Date overrides** (date=specific) — sorted by date ascending; filterable by gate

Columns: Gate | Session | Date | Open? | Open | Close | Snapshot | Reset | Reason | Actions

```
Default Schedules                                [+ Add Session]
┌──────┬────────────┬──────┬───────┬──────────┬───────┬────────┐
│ Gate │ Session    │ Open │ Close │ Snapshot │ Reset │        │
├──────┼────────────┼──────┼───────┼──────────┼───────┼────────┤
│ NSE  │ regular    │ 9:15 │ 15:30 │ 15:31    │ 08:00 │ ✏      │
│ NSE  │ settlement │ —    │ —     │ 16:15    │ —     │ ✏      │
│ MCX  │ morning    │ 9:00 │ 17:00 │ —        │ —     │ ✏      │
│ MCX  │ evening    │17:00 │ 23:30 │ 23:31    │ 08:00 │ ✏      │
│ MCX  │ settlement │ —    │ —     │ 00:15    │ —     │ ✏      │
└──────┴────────────┴──────┴───────┴──────────┴───────┴────────┘
(settlement rows show "—" for open/close since is_open=false; snapshot column shows trigger time)

Date Overrides                                   [+ Add Override]
┌──────┬────────────┬─────────┬────────┬──────┬───────┬─────────────┬────┐
│ Gate │ Date       │ Session │ Open?  │ Open │ Close │ Reason      │    │
├──────┼────────────┼─────────┼────────┼──────┼───────┼─────────────┼────┤
│ NSE  │ 2026-08-15 │ closed  │ Closed │ —    │ —     │ Indep. Day  │✏ 🗑│
└──────┴────────────┴─────────┴────────┴──────┴───────┴─────────────┴────┘
```

### Unified add/edit form (single form — handles all cases)

```
Gate:          [ NSE ▾ ]
Date:          [ _________ ]   ← blank = edit default; filled = date override
                               Hint: "Leave blank to edit the recurring default schedule"
Session name:  [ regular   ]   ← free text: "morning" / "evening" / "closed" / "muhurat"
                               Hint: "Use 'closed' with Is open=No to mark a holiday"
Exchanges:     [ ☑ NSE  ☑ BSE  ☐ NFO  ☐ BFO  ☐ CDS  ☐ MCX ]
                               ← multi-select; defaults to all exchanges in the gate
                               Hint: "Uncheck F&O exchanges for Muhurat trading overrides"
Is open:       [ ● Yes  ○ No ]
               ── shown when Is open = Yes ──
Open time:     [ 09:00 ]
Close time:    [ 17:00 ]
Snapshot time: [ ______ ]   ← blank = no snapshot for this session
Reset time:    [ ______ ]   ← blank = no reset for this session
               ─────────────────────────────
Reason:        [ _________ ]
[ Save ]  [ Cancel ]
```

**Behaviour**:
- Exchanges multi-select defaults to all exchanges in the selected gate; operator deselects
  exchanges that should NOT participate (e.g., uncheck NFO/BFO for Muhurat)
- Date blank + session_name = existing session name → upserts default record via `PUT date:null`
- Date filled → upserts date-override; exchanges array is stored as-is
- `UNIQUE (gate, date, session_name)` ON CONFLICT DO UPDATE — safe re-edit
- After save: `exchange_clock.refresh_cache()` called server-side → in-memory cache refreshed immediately
- Default session rows: ✏ edit (no delete — protected); Override rows: ✏ edit + 🗑 delete

---

## Bug Fix 1: NavStrip heartbeat animation

**File**: `frontend/src/lib/PositionStrip.svelte`

Replace the static `.ps-heartbeat` border-color rule (line 1086-1088) with a `@keyframes`
animation that produces a visible amber glow pulse:

```css
@keyframes ps-heartbeat-pulse {
  0%   { border-bottom-color: rgba(251, 191, 36, 0.30);
         box-shadow: 0 1px 0 0 rgba(251, 191, 36, 0.0); }
  30%  { border-bottom-color: rgba(251, 191, 36, 1.00);
         box-shadow: 0 2px 10px 0 rgba(251, 191, 36, 0.55); }
  100% { border-bottom-color: rgba(251, 191, 36, 0.30);
         box-shadow: 0 1px 0 0 rgba(251, 191, 36, 0.0); }
}

.ps-strip.ps-heartbeat {
  animation: ps-heartbeat-pulse 300ms ease-out forwards;
}
```

In `@media (prefers-reduced-motion: reduce)` block: add `animation: none` for `.ps-heartbeat`
alongside the existing `transition: none`.

---

## Bug Fix 2: Payoff curve spot price drift

**File**: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

In the `!stratMatchesSel` branch before reading `candidatePositions.underlying_ltp` (lines
~1915-1929), also try the SSE snapshot store with the resolved contract key:

```javascript
// Try SSE tick with resolved contract key before falling through to positions API
const resolvedKey = resolveUnderlying(selectedUnderlying)?.quoteKey;
if (resolvedKey) {
  const v = Number(untrack(() => getSnapshot(resolvedKey)?.ltp));
  if (Number.isFinite(v) && v > 0) return v;
}
```

Import `resolveUnderlying` from `$lib/data/rootOf.js` if not already imported on the page.

---

## Bug Fix 3: Snapshot day P&L% oscillation

**File**: `backend/api/routes/positions.py` lines 537-550

Change `if ref_close > 0:` → `if ref_close > 0 and snap_ltp is not None:` so that when
no daily_book snapshot exists for a symbol, the broker's day_change values are left
unchanged rather than recomputed against the fluctuating `broker_ltp`.

---

## Changes to existing files

| File | Change |
|---|---|
| `backend/api/models.py` | Add `ExchangeSchedule` model |
| `backend/api/helpers/exchange_clock.py` | **New module** |
| `backend/api/routes/exchange_schedule.py` | **New `ExchangeScheduleController`** (GET/PUT/DELETE) |
| `backend/api/helpers/snapshot_gate.py` | Remove `_EXCHANGE_TO_GATE`; delegate `is_exchange_closed_now` + `_any_segment_open` to `exchange_clock` |
| `backend/api/background.py` | Remove `_build_segments()` + hardcoded 16:15/23:31/00:15 constants; rewrite `_snapshot_probe_nse_mcx()` to read `exchange_clock.sessions_with_snapshot_time_now()` |
| `backend/api/routes/positions.py` | Bug Fix 3 (`snap_ltp is not None` guard) + delegate `_fetch_ref_close_map` and `_override_stale_close_from_snapshot` cutoffs to `exchange_clock.settlement_cutoff_for` |
| `backend/api/routes/holdings.py` | Delegate `today_ist_8am` cutoff to `exchange_clock.settlement_cutoff_for("NSE")` |
| `backend/api/app.py` | Add `exchange_clock.seed_and_warm` to `on_startup` list **before** `bg_startup`; import + add `ExchangeScheduleController` to `route_handlers` |
| `backend/config/backend_config.yaml` | **Remove** `market_segments` block — no longer read by any code |
| `frontend/src/lib/api.js` | Add `fetchExchangeSchedule`, `upsertExchangeSchedule`, `deleteExchangeSchedule` |
| `frontend/src/lib/PositionStrip.svelte` | Bug Fix 1 — `@keyframes` heartbeat animation |
| `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` | Bug Fix 2 — resolveUnderlying key in liveSpot |
| `frontend/src/routes/(algo)/admin/settings/+page.svelte` | Add exchange schedule section |

---

## Agents

- backend:
  (a) `backend/api/models.py` — add `ExchangeSchedule` with:
      - `gate` Mapped[str], `exchanges` Mapped[list[str]] using `ARRAY(String)` from
        `sqlalchemy.dialects.postgresql`, `date` Mapped[date | None], `weekdays`
        Mapped[list[int] | None]` using `ARRAY(Integer)`, `session_name` Mapped[str]
        (NOT NULL, DEFAULT 'regular'), `is_open` Mapped[bool], four optional
        `mapped_column(Time, nullable=True)` for open/close/snapshot/reset,
        `reason` Mapped[str | None]`, `source` Mapped[str]` DEFAULT 'operator'
      - `UniqueConstraint("gate", "date", "session_name", name="uq_exchange_schedule_gate_date_session")`
      - `Index("ix_exchange_schedule_gate_date", "gate", "date")`

  (b) Create `backend/api/helpers/exchange_clock.py` — full module:
      - `EXCHANGE_TO_GATE = {"NSE":"NSE","BSE":"NSE","NFO":"NSE","BFO":"NSE","CDS":"NSE","MCX":"MCX"}`
      - Module-level `_CACHE: list[ExchangeSchedule] = []` and `_cache_loaded = False`
      - `async def refresh_cache() → None` — SELECT * from exchange_schedule, populate `_CACHE`
      - `async def seed_and_warm(session) → None` — seed 5 default rows (NSE/regular +
        NSE/settlement[is_open=false,snapshot_time=16:15] + MCX/morning + MCX/evening +
        MCX/settlement[is_open=false,snapshot_time=00:15]) ON CONFLICT DO NOTHING;
        seed market_holidays as closed overrides,
        market_special_sessions as open overrides; then call `refresh_cache()`
        NOTE: This must be an `on_startup` callable that accepts no args — wrap DB access inside
        using `async with AsyncSession(engine)` so it can be called from Litestar on_startup list
      - `def _resolve_for_exchange(exchange, on) → list` — filter `_CACHE` by
        `exchange in row.exchanges` (NOT by gate). Date-specific rows first; is_open=False
        → return []; fall back to date IS NULL defaults filtered by weekday. A Muhurat row
        with exchanges=["NSE","BSE"] will NOT match NFO → NFO correctly returns [].
      - `def _resolve_for_gate(gate, on) → list` — filter `_CACHE` by `gate` field.
        Includes settlement rows (is_open=False with snapshot_time). Used only by
        background.py via `sessions_with_snapshot_time_now()`.
      - `def is_exchange_open(exchange, *, at=None) → bool` — calls `_resolve_for_exchange`;
        returns True if any returned session has is_open=True and covers `at.time()`
      - `def is_exchange_closed(exchange, *, at=None) → bool` — `not is_exchange_open(...)`
      - `def snapshot_time_for(exchange, *, on=None) → time | None` — returns snapshot_time from
        the first open session that has it set for the gate+date (excludes settlement rows)
      - `def sessions_with_snapshot_time_now(*, at=None) → list[ExchangeSchedule]` — returns all
        sessions (open OR settlement) whose snapshot_time matches current IST minute; used by
        background.py to fire triggers without hardcoded times
      - `def snapshot_reset_time_for(exchange, *, on=None) → time` — returns snapshot_reset_time
        or defaults to `time(8, 0)` if None
      - `async def settlement_cutoff_for(exchange) → datetime` — uses `snapshot_reset_time_for`;
        formula: `today + reset_time if now_ist >= reset_time else yesterday + reset_time`
      - `async def settlement_ref_close_map(exchange, kind, pairs) → dict` — queries
        `daily_book.ltp WHERE captured_at < settlement_cutoff_for(exchange)` for given
        `(account, symbol)` pairs; IN-clause filter; returns `{(account, symbol): ltp}`

  (c) `backend/api/helpers/snapshot_gate.py` — remove `_EXCHANGE_TO_GATE` dict (lines 108-115);
      remove the YAML-based `_segments` reader inside `_any_segment_open` (keep the function
      signature unchanged for backward compat — it's called by `closed_hours_or_broker`);
      rebuild `_any_segment_open(exchanges=None)` body: call `exchange_clock.is_exchange_open(e)`
      for each exchange in the `exchanges` kwarg (or check NSE+MCX gates if None);
      rewrite `is_exchange_closed_now(exchange)` to call `exchange_clock.is_exchange_closed(exchange)`.
      Both use per-exchange lookup (`_resolve_for_exchange`) — NFO will correctly show closed
      during Muhurat even though NSE gate is open.
      NOTE: `background.py:_build_segments()` is removed in the broker agent task — no coordination needed.

  (d) `backend/api/routes/positions.py` — three changes:
      1. **Bug Fix 3** (line 539): `if ref_close > 0:` → `if ref_close > 0 and snap_ltp is not None:`
         — stops `broker_ltp` fallback from oscillating day P&L% after market close.
      2. **`_fetch_ref_close_map` cutoff delegation** (lines 369-419): group `closed_pairs` by
         gate using `exchange_clock.EXCHANGE_TO_GATE[exch]`; call
         `await exchange_clock.settlement_ref_close_map(gate, kind, pairs)` per gate and merge
         results. Replace hardcoded `today_ist_8am` (lines 393-394) — settlement_ref_close_map
         internally calls `settlement_cutoff_for(gate)` which reads `snapshot_reset_time` from
         the DB cache.
      3. **`_override_stale_close_from_snapshot` cutoff delegation** (lines 956-991): replace
         hardcoded `today_ist_8am` cutoffs (lines 976-977) with
         `await exchange_clock.settlement_cutoff_for(exchange)` where exchange is taken from
         the position row's exchange field.

  (e) `backend/api/routes/holdings.py` — in `_override_stale_close_for_holdings` (line 385):
      replace the hardcoded `today_ist_8am = today_ist_midnight + timedelta(hours=8)` /
      `today_ist_cutoff` calculation with `await exchange_clock.settlement_cutoff_for("NSE")`.
      Import `exchange_clock` from `backend.api.helpers.exchange_clock`.

  (f) Create `backend/api/routes/exchange_schedule.py` as a Litestar `Controller`:
      ```python
      class ExchangeScheduleController(Controller):
          path = "/api/admin/exchange-schedule"
          @get() async def list_schedules(self, ...) → list[ExchangeScheduleDTO]
          @put() async def upsert_schedule(self, data: ExchangeScheduleDTO, ...) → ExchangeScheduleDTO
              # INSERT ... ON CONFLICT (gate, date, session_name) DO UPDATE SET ...
              # After write: await exchange_clock.refresh_cache()
          @delete("/{id:int}") async def delete_schedule(self, id: int, ...) → None
              # Block if date IS NULL (default rows protected): raise 400
              # After delete: await exchange_clock.refresh_cache()
      ```
      Requires JWT auth (same as other admin controllers). DTO includes all 11 fields.

  (g) `backend/api/app.py` — import `ExchangeScheduleController` from exchange_schedule route;
      add it to the `route_handlers` list. Import `exchange_clock` module's `seed_and_warm`
      function and add it to `on_startup` BEFORE `bg_startup`:
      `on_startup=[init_db, _rebuild_broker_connections, seed_hedge_proxies,
       exchange_clock.seed_and_warm, _start_kite_ticker, bg_startup, ...]`

  (h) `backend/config/backend_config.yaml` — **remove** the entire `market_segments:` block.
      No code reads it after background.py migrates to DB-driven scheduling.

- broker:
  `backend/api/background.py` — replace YAML-based `_build_segments()` and hardcoded
  trigger constants with `exchange_clock`-driven scheduling:
  - Add `from backend.api.helpers import exchange_clock` import
  - Remove `_build_segments()` function (lines 70-86) and all calls to it
  - Remove constants `_NSE_SETTLEMENT_H`, `_NSE_SETTLEMENT_M`, `_MCX_CLOSE_H`,
    `_MCX_CLOSE_M`, `_MCX_SETTLEMENT_H`, `_MCX_SETTLEMENT_M` (approx lines 2025-2035)
  - Rewrite `_snapshot_probe_nse_mcx()` (lines 1817-1858): every minute, call
    `exchange_clock.sessions_with_snapshot_time_now()` to get sessions whose snapshot_time
    matches now (IST, minute precision). For each returned session:
    - `session.session_name == "settlement"` → run settlement capture logic for `session.gate`
    - otherwise → run close-snapshot capture logic for `session.gate`
  - Remove dedup sentinels `_nse_settlement_done`, `_mcx_close_done` — minute-precision
    match ensures each trigger fires exactly once per day
  - `close_offset` from YAML → read from `exchange_clock.snapshot_reset_time_for(gate)` for
    the relevant gate (defaults to 08:00 if None — same as the hardcoded value)
  - `_any_segment_open()` calls in background.py (if any) already delegate to snapshot_gate.py
    which already delegates to exchange_clock — no change needed for those

- frontend:
  (a) Bug Fix 1 — `@keyframes ps-heartbeat-pulse` + `box-shadow` glow in
      `frontend/src/lib/PositionStrip.svelte` (lines 1086-1088). Remove old static
      `border-bottom-color` rule. Add `animation: none` to `prefers-reduced-motion` block
      (line 1106).
  (b) Bug Fix 2 — in `derivatives/+page.svelte` `liveSpot` `$derived.by()`, in the
      `!stratMatchesSel` branch before the `candidatePositions.underlying_ltp` path (around
      line 1920), try `getSnapshot(resolveUnderlying(selectedUnderlying)?.quoteKey)?.ltp`.
      Import `resolveUnderlying` from `$lib/data/rootOf.js` if not already imported.
  (c) Add to `frontend/src/lib/api.js`:
      - `fetchExchangeSchedule()` → `GET /api/admin/exchange-schedule`
      - `upsertExchangeSchedule(dto)` → `PUT /api/admin/exchange-schedule`
      - `deleteExchangeSchedule(id)` → `DELETE /api/admin/exchange-schedule/{id}`
  (d) Exchange schedule settings section in
      `frontend/src/routes/(algo)/admin/settings/+page.svelte` (513 lines, existing file).
      Add a new card section after the existing settings cards:
      - Load schedule rows on mount via `fetchExchangeSchedule()`
      - Defaults table (date=null rows grouped by gate) with ✏ edit button
      - Overrides table (date-specific rows) with ✏ edit + 🗑 delete
      - Unified add/edit drawer/panel: gate dropdown (NSE|MCX), optional date input,
        session_name text, is_open toggle, time inputs shown only when is_open=true
        (open/close/snapshot/reset), reason text, Save/Cancel
      - Save: `upsertExchangeSchedule({gate, date: date||null, session_name, ...})`
        then re-fetch and show `toast.success(...)`
      - Delete: `deleteExchangeSchedule(id)` then re-fetch
      - Guard: only show section when `hasCap('admin')`

- doc: Update `docs/specs/BROKER_SPEC.md` — add `exchange_schedule` table schema, exchange clock
  API, `apply_settlement_overlay` pattern, frontend settings UI, how to add a new exchange.

- backend-test:
  Write pytest tests for `exchange_clock.py`:
  - `is_exchange_open`: NSE open at 10:00 IST (in regular session), closed at 16:00;
    MCX open at 10:00 (morning) and 18:00 (evening), closed at 17:30 (between sessions);
    close override (is_open=False row) suppresses ALL MCX sessions; muhurat open on Sunday;
    **Muhurat exchange-subset test**: muhurat row with exchanges=["NSE","BSE"] date=2026-11-01
    → NSE open at 18:00, BSE open at 18:00, NFO closed at 18:00, BFO closed at 18:00
  - `snapshot_time_for`: returns 15:31 for NSE regular; 23:31 for MCX evening session;
    None for MCX morning; None on a closed date
  - `sessions_with_snapshot_time_now`: at 15:31 IST → returns NSE/regular; at 16:15 IST
    → returns NSE/settlement (is_open=false); at 23:31 → returns MCX/evening; at 00:15
    → returns MCX/settlement; settlement rows included even though is_open=False
  - `snapshot_reset_time_for`: returns 08:00 for both gates
  - `settlement_cutoff_for`: yesterday_cutoff before 08:00, today_cutoff after
  - `settlement_ref_close_map`: filters by pairs, uses gate-specific cutoff
  - `apply_settlement_overlay`: patches NFO rows when NSE closed, leaves MCX rows untouched
  - `seed_from_config`: inserts 5 default rows (NSE/regular + NSE/settlement + MCX/morning +
    MCX/evening + MCX/settlement), skips on re-run
  - `refresh_cache`: picks up new session row added after first load
  Write pytest tests for `background.py` trigger rewrite:
  - At 15:31 IST → `sessions_with_snapshot_time_now()` returns NSE/regular → close-snapshot
    logic called, not settlement logic
  - At 16:15 IST → returns NSE/settlement → settlement-capture logic called, not snapshot
  - At 00:15 IST → returns MCX/settlement → settlement-capture logic called
  - At 17:30 IST → returns [] → neither called (between MCX sessions, no snapshot_time match)
  Update `test_snapshot_gate.py`: replace `_EXCHANGE_TO_GATE` tests with delegation tests.
  Add test for Bug Fix 3: overlay skipped when `snap_ltp is None`.
  Add test for positions.py cutoff delegation: `_fetch_ref_close_map` groups by gate and
  calls `settlement_ref_close_map` per gate; NSE and MCX pairs use their respective cutoffs.
  Add test for admin route: PUT with date=null+session=morning upserts MCX default;
  PUT with date=2026-08-15+session=closed inserts holiday; DELETE removes override row.

- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
feat(exchange-clock): DB-driven exchange schedule + settings UI + heartbeat glow + payoff spot key + day P&L oscillation fix

## Done when
1. `exchange_schedule` table created with 5 default rows (NSE/regular + NSE/settlement + MCX/morning + MCX/evening + MCX/settlement)
2. Default records AND date overrides viewable/editable/deletable from `/admin/settings`
3. `is_exchange_open()` / `snapshot_time_for()` / `snapshot_reset_time_for()` / `sessions_with_snapshot_time_now()` are DB-driven
4. `_EXCHANGE_TO_GATE` removed from `snapshot_gate.py`; `is_exchange_closed_now` + `_any_segment_open` delegate to `exchange_clock`
5. `background.py` snapshot triggers are DB-driven: NSE/settlement at 16:15, MCX/evening at 23:31, MCX/settlement at 00:15 read from `exchange_schedule`; `_build_segments()` removed
6. `market_segments` YAML block removed from `backend_config.yaml`
7. `_override_stale_close_for_holdings` uses `exchange_clock.settlement_cutoff_for("NSE")` instead of hardcoded 08:00
8. All 4 hardcoded `today_ist_8am` cutoffs in `positions.py` delegated to `exchange_clock.settlement_cutoff_for(exchange)`
9. `snap_ltp is None` no longer falls back to `broker_ltp` in positions overlay (Bug Fix 3)
10. NavStrip heartbeat fires a visible amber glow animation (box-shadow pulse, not just border color)
11. Payoff `liveSpot` tries `getSnapshot(resolveUnderlying(sel)?.quoteKey)` in non-strategy path
12. pytest + svelte-check pass with no regressions
