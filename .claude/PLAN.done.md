# Plan: Fix P.Close = 0 in derivatives Legs/Exp-close grid — read previous_close not close_price

## Context
`CandidateLegRow.svelte` renders `c.prev_close` for the P.Close column. This value is populated in `frontend/src/lib/derivatives/pageLoad.js` by mapping the API's `close_price` field → `prev_close`. But the API returns two fields:
- `close_price` — Kite's stale overnight BHAV copy price (can be 0 after close until next day)
- `previous_close` — Frozen, authoritative prior-session settlement LTP from `daily_book`

Pulse correctly reads `Number(r.previous_close) || Number(r.close_price) || 0` (prioritising the authoritative field). The derivatives page accidentally reads only `close_price`, so P.Close shows 0 whenever Kite's stale field is 0.

## Task
Fix `frontend/src/lib/derivatives/pageLoad.js` — two lines:

**Line ~75 (positions mapping in `buildPositionRowFromBroker`):**
Change:
```javascript
prev_close: p?.close_price != null ? Number(p.close_price) : null,
```
To:
```javascript
prev_close: Number(p?.previous_close) || Number(p?.close_price) || null,
```

**Line ~107 (holdings mapping in `buildHoldingRowFromBroker`):**
Change:
```javascript
prev_close: h?.close_price != null ? Number(h.close_price) : null,
```
To:
```javascript
prev_close: Number(h?.previous_close) || Number(h?.close_price) || null,
```

No other files need changing. `CandidateLegRow.svelte` already renders `c.prev_close` correctly.

## Agents
- backend: skip
- frontend: In `frontend/src/lib/derivatives/pageLoad.js`, fix the two `prev_close` mapping lines as shown above (positions line ~75, holdings line ~107). For every file you change, you MUST write or update at least one Vitest test covering the fix.
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(derivatives): read previous_close (not close_price) for P.Close in Legs/Exp-close grid — matches Pulse SSOT

## Done when
- `buildPositionRowFromBroker` maps `previous_close || close_price` → `prev_close`
- `buildHoldingRowFromBroker` maps `previous_close || close_price` → `prev_close`
- Test asserts that a row with `previous_close=500, close_price=0` produces `prev_close=500`
- `npx svelte-check` passes with 0 errors
