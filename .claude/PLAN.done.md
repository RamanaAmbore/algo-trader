# Plan: NavStrip popup + pulse grid visual polish + server restart sync

## Task
Follow-up fixes after the 2026-09-22 visual-fixes deploy (7 visual fixes) plus one backend
correctness fix: server restart between MCX EOD snapshot and 08:00 IST fires a spurious
broker API call that displaces the correct EOD snapshot with stale BHAV data, causing Day
P&L to show wrong values until the next 08:00 CloseReset.

Visual fixes:
1. Suppress underlying 1px cell border on symbol columns (right border inconsistency).
2. Remove account left-side vertical border stripe from all grids.
3. Fix BrokerHealthBadge popup body height so data is not hidden.
4. Move canonical modal header (title + ✕) from inside NavBreakdown to PositionStrip wrapper.
5. Reduce NavBreakdown column widths by ~20%.

## Fixes

### Fix 1 — Symbol column right border consistency
**File:** `frontend/src/app.css`

**Root cause:** `.ag-theme-algo .ag-cell { border-right: 1px solid rgba(126,151,184,0.10) }` (line 1197) is never overridden on sym cells. The `::after` 2px direction bar is positioned inside the cell's padding box (`right: 0` = inner edge). The 1px cell border is outside the padding box. Together they create ~3px visual width in directional state vs. 1px in neutral state.

**Fix:** Immediately after the `position: relative` rule for `.ag-col-sym-left, .ag-col-sym` (lines 864–866) add:
```css
.ag-theme-algo .ag-col-sym-left,
.ag-theme-algo .ag-col-sym {
  border-right: 0 !important;
}
```
This leaves only the 2px `::after` bar as the right-edge indicator when direction is active, and no border in neutral state (the sparkline column's amber left-boundary provides the column separator).

### Fix 2 — Remove account left-side vertical border
**File:** `frontend/src/app.css` (line 1319)

Change:
```css
.ag-col-acct {
  border-left: 3px solid var(--acct-stripe, transparent) !important;
}
```
To:
```css
.ag-col-acct {
  border-left: 0 !important;
}
```
Keep the class + `--acct-stripe` injection in column defs (harmless, preserves the infrastructure for future reuse). This affects all grids: PerformancePage, NavBreakdown, and MarketPulse trailing account.

### Fix 3 — BrokerHealthBadge popup body height
**File:** `frontend/src/lib/BrokerHealthBadge.svelte`

**Root cause:** `.bh-modal-body` has `flex: 1; overflow-y: auto` but missing `min-height: 0`. Without it, flex items use `min-height: auto` (natural content height) by default — the body won't shrink below grid height, overflows the modal bounds, and data is clipped.

**Fix:** Add `min-height: 0` to `.bh-modal-body`:
```css
.bh-modal-body {
  flex: 1;
  min-height: 0;      /* allow flex shrink so overflow-y: auto kicks in */
  overflow-y: auto;
  padding: 0;
}
```

### Fix 4 — Move canonical header from NavBreakdown to PositionStrip
**Files:** `frontend/src/lib/PositionStrip.svelte`, `frontend/src/lib/NavBreakdown.svelte`

**Root cause:** The `nav-bd-header canonical-modal-header` (title + ✕) lives inside NavBreakdown directly above each slot's ag-Grid, making the X appear to be part of the grid content area.

**Fix:**
- In `PositionStrip.svelte`: add a `<div class="ps-bd-header canonical-modal-header">` INSIDE `ps-breakdown-panel` above the `<NavBreakdown>` tag. Map slot title inline: `P → "Positions P&L"`, `M → "Margin"`, `C → "Cash"`, `H → "Holdings"`. Put the close button here.
- In `NavBreakdown.svelte`: remove the `onClose` prop, remove all `nav-bd-header canonical-modal-header` divs and the `nav-bd-close` button from every render branch (ready/error/timeout/empty/loading). Remove `_slotTitle` derived and the `nav-bd-*` CSS rules that were added. Keep `activeSlot` prop (used for column switching and tooltips).

### Fix 5 — NavBreakdown column widths −20%
**File:** `frontend/src/lib/NavBreakdown.svelte`

Reduce all column `width`, `minWidth`, `maxWidth` values by ~20% (round to nearest 4px):

| Column | Before | After |
|---|---|---|
| account `width` | 76 | 60 |
| account `minWidth` | 60 | 48 |
| account `maxWidth` | 92 | 74 |
| flex cols `minWidth` | 80 | 64 |
| `utilPct` `minWidth` | 64 | 52 |

### Fix 6 — Underlying root symbol cell: add right-edge bar (like legs)
**File:** `frontend/src/app.css`

**Context:** In the positions/snapshot grid, rows have:
- `row-und`: underlying root (NIFTY, BANKNIFTY) — has purple bg tint `rgba(192,132,252,0.10)` on sym cell but NO `::after` bar
- `row-pos-orphan`: unmanaged position leg — has amber bg + amber `::after` bar (line 923–929)
- `row-pos-paired`: managed position leg — has cyan bg + cyan `::after` bar (line 931–937)

**Fix:** Add `::after` right-edge bar to `row-und` sym cells using the matching purple color:
```css
.ag-theme-algo .ag-row.row-und .ag-col-sym::after {
  background: rgba(192, 132, 252, 0.80);
}
```
Insert immediately after the `row-und` background rule (around line 789). No JS changes needed — `row-und` class is already applied by the grid.

### Fix 7 — Legs + exp-close symbol cell directional background
**File:** `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`

**Root cause:** `.cand-sym-acct` currently has only `color-mix(in srgb, var(--cand-acct-color, transparent) 14%, transparent)` — a 14% per-account color tint that looks faded because it varies per account and has no directional semantic. Pulse positions uses `pos-long/pos-short .ag-col-sym { background-color: rgba(74,222,128/248,113,113, 0.10) }` — a fixed directional 10% tint that reads immediately.

**Fix:** Add directional background rules to match pulse positions (insert after the existing `.cand-sym-acct` rule):
```css
.cand-row.cand-row-long  .cand-sym-acct { background-color: rgba(74,  222, 128, 0.10) !important; }
.cand-row.cand-row-short .cand-sym-acct { background-color: rgba(248, 113, 113, 0.10) !important; }
.cand-row.expiry-band-close .cand-sym-acct { background-color: rgba(251, 191,  36, 0.12) !important; }
```
The `::after` direction bar (green/red 2px) is already present and stays unchanged. The account-color tint falls under the directional background.

### Fix 8 — Server restart sync: restore sentinels from DB before SessionGuard
**File:** `backend/api/background.py`

**Root cause:** `_snapshot_fired_today` is in-process memory only — it resets to `{"NON-MCX": None, "MCX": None}` on every process start. On a restart between MCX EOD snapshot time and 08:00 IST:

1. `_snapshot_fired_today` starts as `{NON-MCX: None, MCX: None}`
2. `_session_guard()` checks `_snapshot_fired_today.get("MCX") == today` → False (None ≠ today) → fires `_snapshot_fire("mcx", market_open=False)` → **broker API call at 02:00 IST with stale BHAV** → `close_price = 0` → `day_pnl = ltp - 0 = ltp` (wrong) → overwrites the correct MCX EOD snapshot
3. Sets sentinel = today; `_ds_startup_snapshot` then also skips (too late — damage done)

**Fix (two parts):**

**Part A — new function `_preload_snapshot_sentinels()`** to add before `_session_guard()` in `on_startup()`:

```python
async def _preload_snapshot_sentinels() -> None:
    """Restore _snapshot_fired_today from daily_book on startup.

    If today's EOD snapshots already exist (written by a prior process run),
    seed the sentinels so SessionGuard skips re-firing them and overwriting
    correct EOD data with stale BHAV values.
    """
    from backend.api.database import async_session
    from sqlalchemy import text as _sql
    from backend.shared.helpers.date_time_utils import timestamp_indian
    from datetime import timedelta

    now = timestamp_indian()
    today = now.date()
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    tomorrow_midnight = today_midnight + timedelta(days=1)
    try:
        async with async_session() as session:
            result = await session.execute(_sql("""
                SELECT
                  bool_or(exchange != 'MCX') AS has_non_mcx,
                  bool_or(exchange  = 'MCX') AS has_mcx
                FROM daily_book
                WHERE kind = 'positions'
                  AND captured_at >= :today_start
                  AND captured_at <  :tomorrow
            """).bindparams(today_start=today_midnight, tomorrow=tomorrow_midnight))
            row = result.one_or_none()
        if row:
            if row.has_non_mcx:
                _snapshot_fired_today["NON-MCX"] = today
            if row.has_mcx:
                _snapshot_fired_today["MCX"] = today
            logger.info("[STARTUP] snapshot sentinels restored: %s", _snapshot_fired_today)
    except Exception as exc:
        logger.warning("[STARTUP] sentinel restore failed (non-fatal): %s", exc)
```

**Part B — call it in `on_startup()` before `_session_guard()`:**

```python
# Restore _snapshot_fired_today from DB so SessionGuard doesn't re-fire
# EOD snapshots that already exist from a prior process run.
try:
    await _preload_snapshot_sentinels()
except Exception as _pre_exc:
    logger.warning("Background: sentinel preload failed (non-fatal) — %s", _pre_exc)
await _session_guard()
```

Also revert the incomplete Fix 8 that was previously added to `_ds_startup_snapshot` (lines 2277–2286) — it is now redundant since SessionGuard itself won't re-fire when sentinels are pre-populated. Remove those lines to keep `_ds_startup_snapshot` clean.

## Agents
- frontend: Implement Fixes 1–7
- backend: Implement Fix 8 in `backend/api/background.py:_ds_startup_snapshot`

## Tests
- pytest: yes (Fix 8 — add a test that verifies `_ds_startup_snapshot` skips when both EOD sentinels are set for today)
- svelte-check: yes
- playwright: no (visual-only changes for frontend; existing spec covers component presence)

## Commit message
fix(pulse+navstrip+bg): sym border consistency, account stripe, popup height, header in strip, col widths, und root sym bar, startup snapshot eod guard

## Done when
- Symbol column right boundary is uniform (2px direction bar in directional state, no border in neutral) across all pulse grids
- Account columns have no left-side vertical stripe in any grid
- BrokerHealthBadge popup body scrolls correctly when data exceeds visible area
- NavBreakdown shows data grids only (no header/close button inside); PositionStrip renders the canonical header above NavBreakdown
- NavBreakdown columns are ~20% narrower
- Underlying root (row-und) symbol cells show a purple right-edge bar matching the orphan/paired leg pattern
- Server restart between MCX snapshot and 08:00 IST does not overwrite EOD daily_book data
- pytest passes for the new startup-snapshot guard test
- svelte-check passes with 0 errors
