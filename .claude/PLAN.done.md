# Plan: Flash tier from day % (not tick delta)

## Context

The 3-tier flash system currently reads `absPct` from `_ltpFlashPctMap`, which stores
the tick-to-tick % delta from the SSE bus. This means the tier reflects "how much did
the price just tick" — a tiny corrective tick on a big day-mover flashes `sm`, while
a large tick on a flat stock flashes `lg`. 

The correct signal is "how significant is this symbol's move today" = day %.
A stock up 3% on the day should always flash `lg` whenever its LTP ticks, regardless
of the size of the individual tick. A flat stock should always flash `sm`.

Where day % is not pre-computed on the row (derivatives spot, CandidateLegRow),
compute independently: `Math.abs((ltp − prevClose) / prevClose × 100)`.

## Task

Four targeted changes, all frontend:

### Fix 1 — `_ltpFlashClass` reads day % from row data (pulseColumns.js)

**File:** `frontend/src/lib/data/pulseColumns.js`

Add `rowData` parameter to `_ltpFlashClass` (currently `(sym, getLtpFlashUp, getLtpFlashDown, getLtpFlashPct)`):

```javascript
function _ltpFlashClass(sym, getLtpFlashUp, getLtpFlashDown, getLtpFlashPct, rowData) {
  if (getLtpFlashUp?.().has(sym)) {
    const absPct = rowData?.change_pct != null ? Math.abs(rowData.change_pct)
                 : rowData?.day_pnl_pct != null ? Math.abs(rowData.day_pnl_pct)
                 : getLtpFlashPct?.()?.get(sym) ?? 1;
    return _tcFlashClass('up', absPct);
  }
  if (getLtpFlashDown?.().has(sym)) {
    const absPct = rowData?.change_pct != null ? Math.abs(rowData.change_pct)
                 : rowData?.day_pnl_pct != null ? Math.abs(rowData.day_pnl_pct)
                 : getLtpFlashPct?.()?.get(sym) ?? 1;
    return _tcFlashClass('down', absPct);
  }
}
```

Update the call site in `mkLtpCol`'s `cellClass` to pass `p.data`:
```javascript
const fc = _ltpFlashClass(sym, getLtpFlashUp, getLtpFlashDown, getLtpFlashPct, p.data);
```

Similarly in `mkPnlCellClass` (lines ~97-102) — where it reads
`getLtpFlashPct().get(symUpper) ?? 1`, change to:
```javascript
const absPct = p.data?.day_pnl_pct != null ? Math.abs(p.data.day_pnl_pct)
             : p.data?.change_pct  != null ? Math.abs(p.data.change_pct)
             : getLtpFlashPct?.()?.get(symUpper) ?? 1;
```

The `_ltpFlashPctMap` (tick delta) remains as the last-resort fallback — useful when
`change_pct` hasn't been populated yet on the row (e.g., first tick before first poll).

### Fix 2 — PerformancePage reads day_change_percentage for tier

**File:** `frontend/src/lib/PerformancePage.svelte`

In `avgVsLtpCls` at ~lines 407-412, change from map to row field:
```javascript
const absPct = Math.abs(params.data?.day_change_percentage ?? _perfLtpFlashPctMap.get(sym) ?? 1);
```

PerformancePage's day % field is `day_change_percentage` (confirmed in column defs at ~line 549).

`_perfLtpFlashPctMap` remains as fallback (for first-tick before first poll).

### Fix 3 — Derivatives Snapshot spot: compute day % independently

**File:** `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

At line ~4772, the spot cell currently uses fixed-tier flash (maps `tf-up/down → ltp-tc-flash-up/down` with no magnitude selection). Change to compute day % from `_ltp` and `_close` which are already in scope in the same `{@const}` block:

```javascript
{@const _spotDayPct = (_ltp != null && _ltp > 0 && _close != null && _close > 0)
    ? Math.abs((_ltp - _close) / _close * 100) : 1}
```

Then use `_tcFlashClass` (already exported from pulseColumns.js — add to import) for
the flash class:

```javascript
{flash.classOf(`${g.underlying}:ltp`) === 'tf-up'   ? _tcFlashClass('up',   _spotDayPct) :
 flash.classOf(`${g.underlying}:ltp`) === 'tf-down' ? _tcFlashClass('down', _spotDayPct) : ''}
```

### Fix 4 — CandidateLegRow LTP: compute day % independently

**File:** `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`

At line ~316, same pattern — currently maps `tf-up/down → ltp-tc-flash-up` (fixed tier).
The leg object `c` has `ltp` and `prev_close` (or equivalent). Compute independently:

```javascript
{@const _legDayPct = (ltp != null && ltp > 0 && c.prev_close > 0)
    ? Math.abs((ltp - c.prev_close) / c.prev_close * 100) : 1}
```

Then apply `_tcFlashClass('up'/'down', _legDayPct)` from the flash class map.

If `c.prev_close` is not available on the leg object, check for `c.close_price` or
`c.change_pct` as alternative sources. If none are available, default to `absPct=1`
(medium tier) — still better than tick delta.

Import `_tcFlashClass` from `$lib/data/pulseColumns.js` if not already imported.

## Agents

- frontend: Implement all four fixes.
- backend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- vitest: no

## Commit message
fix(ui): flash tier from day % instead of tick delta — LTP and spot tier reflects cumulative day move

## Done when
- `_ltpFlashClass` in pulseColumns.js reads `change_pct`/`day_pnl_pct` from row data; `_ltpFlashPctMap` tick delta as fallback only
- `mkPnlCellClass` reads day % from `p.data` for tier
- PerformancePage LTP flash tier reads `day_change_percentage` from row; map as fallback
- Derivatives Snapshot spot flash tier from `(ltp−prevClose)/prevClose×100`
- CandidateLegRow LTP flash tier from day % (or independent compute); not tick delta
- svelte-check exits 0
