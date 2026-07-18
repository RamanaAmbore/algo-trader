# Plan: Fix card header vertical misalignment + 3M/6M/1Y chart load

## Context

Two independent bugs bundled into one deploy:

**Bug A — Card header vertical misalignment:** The Legs card (and 3 other surfaces) show visible vertical misalignment between the left content (chip + tabs), middle content (chips/filter rows), and the right card-controls button group.

**Bug B — 3M/6M/1Y charts never load data:** Charts show spinner indefinitely for longer ranges even after the `_SELF_HEAL_COVERAGE_THRESHOLD` fix (0.70→0.60). Root cause: the backend `historical()` route is missing the `fresh: bool` query parameter, so the frontend's `?fresh=1` cache-bypass retry is silently ignored — the backend returns the same cached `partial: true` result on every retry, creating an infinite loop where the chart never resolves.

---

## Bug A — Card header misalignment

**Root cause:** Header containers with asymmetric `padding-bottom` inflate their own box height. `align-items: center` on the outer row then centers the padded box rather than the visible content, shifting chip/tabs upward relative to the right-side card controls.

### Fix A1 — Legs card (`derivatives/+page.svelte` ~line 5660)
`.legs-header { padding: 0 0.25rem 0.5rem }` — 0.5rem bottom padding shifts the chip+tabs upward.  
**Fix:** `padding: 0 0.25rem 0`

### Fix A2 — Payoff / Snapshot / all `.opt-section-h` (`derivatives/+page.svelte` ~line 5079)
`.opt-section-h { padding: 0 0.25rem 0.25rem }` — same issue. `margin-bottom: 0.4rem` already handles spacing below.  
**Fix:** `padding: 0 0.25rem 0`

### Fix A3 — PnlPanel filter bar (`frontend/src/lib/PnlPanel.svelte` ~line 234)
`.filter-bar { align-items: flex-end }` — labels and pills sit at the bottom instead of center.  
**Fix:** `align-items: center`

### Fix A4 — Templates middle slot (`automation/templates/+page.svelte` ~line 350)
`<div class="flex items-center gap-1 flex-wrap">` — chips wrap to a second row on overflow, making `ch-middle` taller than `ch-left` (title) and `ch-right` (buttons).  
**Fix:** `flex-wrap: nowrap; overflow-x: auto; scrollbar-width: none` — same horizontal-scroll pattern as all other chip rows.

---

## Bug B — 3M/6M/1Y chart load loop

**Root cause:** `backend/api/routes/options.py` — the `historical()` route signature:

```python
async def historical(self, symbol: str = "", days: int = 30,
                     interval: str = "day", exchange: str = "") -> HistoricalResponse:
```

is missing `fresh: bool = False`. Litestar silently ignores `?fresh=1` from the frontend. The cache lookup always fires, returning the same cached `partial: true` response. The frontend retries every 5s with `fresh=true` expecting a cache bypass, but the backend never bypasses — infinite loop.

**Call chain:**
1. User clicks 3M → `_loadHistorical(true, true)` → `fetchOptionsHistorical(sym, { days: 90, fresh: true })`
2. `api.js` appends `?fresh=1` to the request URL
3. Backend receives request, `fresh` param is dropped → cache hit → same `partial: true` returned
4. Frontend schedules another retry in 5s → loop

### Fix B — `backend/api/routes/options.py` (~line 2613)

Add `fresh: bool = False` to the route signature and gate the cache lookup on it:

```python
async def historical(self, symbol: str = "", days: int = 30,
                     interval: str = "day", exchange: str = "",
                     fresh: bool = False) -> HistoricalResponse:
    ...
    cache_key = (sym, (exchange or "NFO").upper(), days, interval)
    if not fresh:
        _cached = _hist_cache_get(cache_key)
        if _cached is not None:
            return _cached
    # rest of the function unchanged
```

---

## Agents

- frontend: Fix A1–A4 (CSS + one template attribute change, no logic)
- backend: Fix B — add `fresh: bool = False` param + cache-skip gate in `historical()` in `backend/api/routes/options.py`
- broker: skip
- doc: skip
- backend-test: Add test asserting that `GET /historical?fresh=1` bypasses the cache and does not return a cached partial result
- playwright: Add/update `frontend/e2e/card-header-scroll.spec.js` — assert Legs card chip + CardControls share the same vertical midpoint (±2px). Update `frontend/e2e/chart-ranges.spec.js` — assert 3M/6M/1Y range buttons eventually clear the spinner and render chart bars (non-empty)

---

## Tests

- pytest: yes (backend-test covers the fresh param)
- svelte-check: yes
- playwright: yes

---

## Commit message

fix(cards+chart): card header alignment + 3M/6M/1Y chart fresh-param

Bug A: remove padding-bottom from .legs-header + .opt-section-h (asymmetric
padding shifted chip/tabs up vs card controls). PnlPanel filter-bar flex-end → center.
Templates middle slot: flex-wrap → nowrap + overflow-x scroll.

Bug B: add fresh:bool param to historical() route — frontend ?fresh=1 retry was
silently ignored, causing infinite partial-result loop for 3M/6M/1Y chart ranges.

---

## Done when

- Legs card: chip + AlgoTabs + CardControls on the same horizontal axis
- Payoff/Snapshot headers: title + chips + controls on same axis
- PnlPanel filter bar: labels and pills centered on same axis
- Templates header: chips scroll horizontally, no second-row wrapping
- 3M/6M/1Y chart ranges: data loads and chart renders (no infinite spinner)
- pytest green | svelte-check 0 errors
