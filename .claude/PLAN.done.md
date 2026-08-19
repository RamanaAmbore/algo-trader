# Plan: Fix day P&L correctness — close_price freeze + holdings/positions split + NavCard drift

## Context

Extended operator discussion (2026-08-18) established three canonical invariants that are now
permanently recorded in CLAUDE.md. Several code gaps exist where the implementation violates
these invariants. This plan captures what needs to be fixed.

---

## Invariants (now in CLAUDE.md — do not deviate)

### 1. close_price / ltp lifecycle
- `close_price` = previous session's settlement LTP. **Frozen** until next market open.
- `ltp` updates to settlement price at session close. Only moment `close_price` changes = market open.
- Works identically across 1-day gaps, weekends, multi-day holidays.

### 2. Day P&L formula by position type
| Row type | Formula |
|---|---|
| Overnight open (oq>0, qty>0) | `(ltp − close) × qty` |
| Closed overnight (oq>0, qty=0) | `(exit_price − close) × qty` → Case 2: `pnl − (close − avg) × oq` |
| New today (oq=0, qty>0) | `(ltp − entry_price) × qty` → Case 1: dcv = broker pnl |

### 3. Holdings sold → P&L splits to positions
- Holdings shows **remaining `quantity` only** — not `opening_quantity`
- Holdings day P&L, inv_val, cur_val all computed on `quantity` (remaining)
- Sold portion's day P&L lives exclusively in the CNC positions row (Case 2)
- `opening_quantity` is a reference field only — not used in any P&L or value computation

---

## Gaps to fix

### Gap 1 — Holdings enrichment uses `opening_quantity` instead of `quantity` (P1)
**File**: `backend/brokers/broker_apis.py:1537` — `_build_holdings_dcv_expr`
**File**: `backend/brokers/broker_apis.py:1602` — `_enrich_holdings` inv_val
**File**: `backend/api/routes/holdings.py:128` — snapshot path day_change_val

Current: `_qty = _col_f64(lf, "opening_quantity")` everywhere
Fix: use `quantity` for day P&L, inv_val, cur_val. `opening_quantity` retained as display field only.

Risk: if Kite returns `quantity=0` for a fully sold holding (row still present), day P&L
correctly becomes 0 (sold portion is in positions). Need to verify Kite API behaviour.

### Gap 2 — Holdings missing `_override_stale_close_from_snapshot` (P1)
**File**: `backend/api/routes/holdings.py` — `_fetch()` function
Positions have `_override_stale_close_from_snapshot` which replaces broker `close_price`
with daily_book snapshot LTP captured before 08:00 IST. Holdings have no equivalent.
Result: holdings day P&L shows 0 during off-market hours when Kite updates `close_price`
to settlement price (making `ltp − close = 0`).
Fix: add equivalent close_price override to holdings route.

### Gap 3 — NavCard day P&L drifts from PositionStrip P slot (P2)
**NavCard** (`frontend/src/lib/NavCard.svelte`): polls `fetchMyNav` every 60s.
Backend reads `_intraday_equity` deque (written by performance cadence, 5 min) or
does own direct broker fetch. No live LTP enhancement.

**PositionStrip P slot**: reads `positionsDayPnlStore` → book cadence (30s) + live LTP (~1s).

Drift: up to 5 min on quantities, formula difference (NavCard uses raw `day_change_val`,
PositionStrip applies live LTP via `livePositionDayPnl()`).

Fix: align NavCard `firm_day_pnl` to read from `positionsDayPnlStore.total` +
holdings day P&L from book cadence, rather than the performance deque.

### Gap 6 — Payoff overlay: spot price and day P&L drift post-close (P2)
**File**: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte:1827,1880,1942`

During market hours: both `liveSpot` and `candidatesDayPnl` are driven by `_throttledTick`
(4Hz, 250ms debounce). Same reactive batch. No drift. ✓

Post-close drift:
- `_throttledTick` freezes when `isMarketOpen() = false` → `liveSpot` frozen at last pre-close tick
- Book poller has no market-hours gate → keeps firing post-close
- When book poller updates `candidatePositions` (settlement `prev_close`/`day_change_val`),
  `candidatesDayPnl` recomputes using frozen `liveSpot`
- Result: overlay shows spot = last live tick before close, day P&L = post-settlement values
  — two different points in time simultaneously

Fix: after market close, `liveSpot` should update from the book poll's `underlying_ltp`
(settlement LTP) when `_throttledTick` is frozen, so both values reflect the same
post-settlement state. Gate the `liveSpot` fallback to `candidatePositions[*].underlying_ltp`
(tier 3) on `!isMarketOpen()` without requiring `_throttledTick` to fire.

### Gap 7 — PositionStrip underline pulses on every poll, not on actual data change (P2)
**File**: `frontend/src/lib/PositionStrip.svelte:806-853`
**File**: `frontend/src/lib/data/marketDataStores.svelte.js` — `bookPollerTick`

Current: `ps-heartbeat` (market open) and `ps-poll-pulse` (market closed) fire on every
`bookPollerTick` increment — every 5s. Backend cache TTL is 30s, so 5 out of 6 pulses
are false signals: underline animates but numbers are identical (served from cache).

Fix: pulse only when data actually changed. Compare incoming positions/holdings payload
to previous payload (e.g. a lightweight hash or `JSON.stringify` diff of key fields:
`day_change_val`, `last_price`, `quantity` totals). Only increment a separate
`dataChangedTick` when a real diff is detected. Drive `ps-heartbeat` / `ps-poll-pulse`
from `dataChangedTick` instead of `bookPollerTick`.

### Gap 8 — PositionStrip does not respond to order fill events (P1)
**File**: `frontend/src/lib/PositionStrip.svelte` — imports `bookPollerTick` but not `bookChanged`

After an order fills:
- `_postback_broadcast_fanout` (orders.py:583-605) immediately invalidates the positions/holdings cache AND broadcasts `fill_event` → `book_changed` events
- `bookChanged.js` coalesces these into a monotonic counter increment (immediate on `fill_event`, 200ms debounce on `book_changed`)
- `derivatives/+page.svelte` and `PerformancePage.svelte` both subscribe to `bookChanged` and call `loadPositions({ fresh: true })` immediately
- **PositionStrip does not import `bookChanged`** — it only refreshes via the 5s `bookPollerTick`
- Result: user sees the fill notification popup immediately but waits up to 5s for the NavStrip P slot to reflect the new position

Fix: import `bookChanged` in `PositionStrip.svelte`, add `$effect` that calls `_load()` when the counter increments. Also increment `_pollCycleStamp` so the heartbeat flash fires on fill (matching the actual data change).

### Gap 9 — Chain-quotes route polls broker every 5s during off-market hours (P2)
**File**: `backend/api/routes/options.py:2539` — `chain_quotes()`

The `chain_quotes` route has no `closed_hours_or_broker` gate. The frontend polls every 5s
via `visibleInterval(withGuard(_refreshChainQuotes), 5000)`. Off-market, broker.quote() returns
stale data (no real depth); the code correctly falls back to `last_price` with `depth_available=False`,
but the backend still fires a full `broker.quote()` round-trip on every poll — unnecessary load.

Fix: wrap the broker quote call in `chain_quotes` with the same `closed_hours_or_broker` gate used
by positions/holdings routes, or return the last cached quote response when market is closed.
Simpler alternative: front-end only — increase poll interval to 60s when `!isMarketOpen()`.

### Gap 10 — Chain ATM highlight not live during session (P2)
**File**: `frontend/src/lib/order/OptionChainTab.svelte:383-398`

`fetchOptionsSpot` is called once when underlying/expiry changes (key-equality guard prevents
re-fetch) and again on `refreshKey` change (tab activation). During volatile sessions, NIFTY
can move 200+ points without the ATM row marker updating — operator sees stale ATM highlight
while bid/ask prices in the grid update every 5s. The disconnect is confusing: grid shows live
quotes but ATM is anchored to the spot at tab-open time.

Fix: add periodic spot refresh alongside the quotes poll — re-fetch spot every 30s during market
hours (or on every 6th quotes poll to avoid a separate interval). Gate the re-fetch on
`isMarketOpen()` since off-market spot is frozen and doesn't need updating.

### Gap 4 — `apply_day_change_backstop` Case 3 oq=0 guard (DONE — b4b83a1f)
Fixed: Case 3 now requires `oq == 0`. Closed overnight positions (qty=0, oq>0) no longer
use total `pnl` as day P&L when `close_price = 0`.

### Gap 5 — `_apply_flat_row_hygiene` narrowed to oq=0 (DONE — ed63b9fe)
Fixed: hygiene only zeros rows where qty=0 AND oq=0. Closed overnight rows preserved.

---

## Agents
- backend: Fix Gap 1 (holdings enrichment quantity vs opening_quantity) + Gap 2 (close_price override for holdings) + Gap 9 (chain-quotes gate off-market broker calls)
- frontend: Fix Gap 3 (NavCard drift — align firm_day_pnl to book cadence + positionsDayPnlStore) + Gap 6 (payoff overlay liveSpot post-close freeze) + Gap 7 (PositionStrip underline pulses on data change not poll) + Gap 8 (PositionStrip subscribe to bookChanged for immediate fill refresh) + Gap 10 (chain ATM spot refresh every 30s during market hours)
- backend-test: Tests for Gap 1 (holdings dcv uses quantity) + Gap 2 (close override)
- broker: skip
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(pnl,chain): holdings quantity fix; close_price override; NavCard align; chain ATM live spot + off-market gate

## Done when
- Holdings day P&L computed on remaining `quantity`, not `opening_quantity`
- Holdings route has close_price snapshot override (same as positions)
- NavCard `firm_day_pnl` agrees with PositionStrip P slot within one book cadence cycle (30s)
- Payoff overlay spot price and day P&L reflect same post-settlement state after market close
- PositionStrip underline only pulses when book data actually changed, not on every 5s poll
- PositionStrip refreshes within 200ms of order fill (via bookChanged subscription), not up to 5s later
- Chain ATM highlight tracks live spot (refreshes every 30s during market hours)
- Chain-quotes route does not fire broker.quote() during off-market hours
- All pytest green, broker cov ≥ 80%, api cov ≥ 45%
