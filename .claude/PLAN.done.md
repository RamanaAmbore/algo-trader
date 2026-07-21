# Plan: P1 close-order intent + payoff race + timestamp + ChartModal z-index

## Task

Four bugs to fix across backend and frontend:

**A — P1 Backend: `intent="close"` dropped in ticket→chase path**
`orders_place.py:_ticket_place_or_chase_live` calls `_start_live_chase` without forwarding `intent`.
The `5ae02ccd` commit fixed three other close-intent paths but missed this one.
Result: close SELL via LIMIT+chase sends `intent=None` to Kite → 50-lot ceiling enforced →
large equity positions (>50 shares) rejected. Fix: thread `intent` through
`_ticket_place_or_chase_live` → `_start_live_chase` → `_live_chase_config` → `ChaseConfig`.

**B — P2 Frontend: payoff `loadStrategy` race condition**
`loadStrategy()` is called from three sites: underlying-change `$effect` (line 1561),
legs-change `$effect` (line 2385), and `marketAwareInterval` (every 5 s). No stale-response
guard exists — a slower earlier fetch can overwrite a fresher one. Fix: add a generation
counter; discard any response whose generation doesn't match the latest.

**C — P2 Frontend: `lastRefreshAt` stamp too late**
`dashboard/loadHero`: stamp moved to AFTER `fundsStore.load()` + `_fetchNifty()` — if
nifty quote is slow, AlgoTimestamp appears delayed even though positions/holdings loaded.
Fix: move stamp back to after the first `Promise.all` (positions/holdings/events).
`MarketPulse/loadPulse` force=true path: stamp gated on `batchQuoteChunked` (100+ calls).
Fix: stamp immediately after `pulsePositionsStore.load()` + `pulseHoldingsStore.load()`
complete inside the `if (force)` block, so the timestamp updates when primary data arrives.

**E — P3 Frontend: fullscreen modal grid/card height not filling available space**
Activity modal and Dashboard activity card go fullscreen but the inner content keeps scroll bars
instead of expanding. Root cause: `.card-body` inside `.dash-activity.fs-card-on` retains its
normal `max-height` constraint and no flex expansion is applied. Pattern already works on
`/orders` (`.bucket-card-activity.fs-card-on .oc-act-body { flex: 1 1 0; min-height: 0; }`)
and MarketPulse buckets (hard-coded `height: calc(100vh - 8rem)` on `.bucket-grid`).
Fix: add `flex: 1 1 0; min-height: 0; max-height: none; overflow: hidden;` to
`.dash-activity.fs-card-on > .card-body` in dashboard page scoped CSS.
Also audit `ActivityLogModal.svelte` and any other fullscreen cards identified in
the sweep (see agent notes) for the same pattern gap.

**F — P3 Backend: positions refresh retry after order fill (Option B)**
After `_postback_broadcast_fanout` fires the `position_filled` WS event, kick off a background
task that polls `broker.positions()` every 1 s for up to 5 s, stopping as soon as the filled
symbol appears in the result (or a changed quantity is detected). On success, invalidate the
positions raw cache (`_raw_cache_invalidate`) and push a second WS event `positions_refreshed`
with the confirmed row. Frontend MarketPulse already subscribes to `position_filled` and calls
`loadPulse({ force: true })` — on `positions_refreshed`, call the same path again so the grid
updates with broker-authoritative data within 1-3 s instead of waiting for the 10 s poll tick.

**D — P3 Frontend: ChartModal hidden behind SymbolPanel + close unresponsive**
SymbolPanel renders as `position:fixed z-index:10500` (creates stacking context). ChartModal
rendered as sibling AFTER the overlay divs (correct), but both use the same CSS class
`canonical-modal-overlay` at z-index 10500 — so they share the same paint layer and ChartModal
close button doesn't reliably receive clicks. Fix: give ChartModal's overlay a higher z-index
(10600 via a scoped CSS rule `.cm-overlay { z-index: 10600; }` or CSS variable `--z-command-top`).
Also add `pointer-events: auto` guard on the ChartModal overlay itself so backdrop clicks close it.

## Agents

- backend: 
  (A) In `backend/api/routes/orders_place.py` — in `_ticket_place_or_chase_live` (line 1327), add `intent=getattr(data, "intent", None)` to the `_start_live_chase` call. In `backend/api/routes/orders_helpers.py` — add `intent: str | None = None` parameter to `_start_live_chase` (line 249), pass it into `_live_chase_config`; in `_live_chase_config` (line 219) add `intent` param and set `cfg.intent = intent` before returning. Add a unit test asserting that `_live_chase_config(aggressiveness="low", intent="close")` produces ChaseConfig with `intent="close"`.
  (F) In `backend/api/routes/orders.py` (postback handler / `_postback_broadcast_fanout`): after pushing the `position_filled` WS event, spawn an asyncio background task `_positions_refresh_after_fill(account, tradingsymbol, expected_qty_delta)` that polls `broker.positions()` every 1 s up to 5 iterations, stops when the symbol's quantity changes by at least `expected_qty_delta`, calls `_raw_cache_invalidate` on the positions key, then broadcasts a `positions_refreshed` WS event (same channel as `position_filled`). In `frontend/src/lib/MarketPulse.svelte` WebSocket handler (around line 1491): on `msg.event === "positions_refreshed"`, call `loadPulse({ force: true })` (same as `position_filled`).
- frontend: 
  (B) In `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`, in `loadStrategy` (line ~3449): add module-level `let _stratGen = 0`. At the top of the fetch path (after the memo early-return), do `const _thisGen = ++_stratGen`. After `fetchStrategyAnalytics` resolves, check `if (_thisGen !== _stratGen) return` before assigning `strategy = resp` and `_stratLastKey = legsKey`. Same guard in the catch block before incrementing `_stratFails`.
  (C) In `frontend/src/routes/(algo)/dashboard/+page.svelte` `loadHero()`: move `lastRefreshAt.set(Date.now())` to immediately after `_heroLoadedAt = clientTimestamp()`, before the second `await Promise.all([fundsStore.load(), _fetchNifty()])`. In `frontend/src/lib/MarketPulse.svelte` `loadPulse()`: inside the `if (force)` block at line 2593 (after the `await Promise.allSettled([pulsePositionsStore.load(), pulseHoldingsStore.load()])`), add `pulseLastUpdate = Date.now(); _lastPulseAt = pulseLastUpdate; lastRefreshAt.set(pulseLastUpdate);` — this stamps early for the primary data; the existing stamp inside `if (allKeys.size)` then updates again when quotes arrive (harmless double-stamp).
  (E) In `frontend/src/routes/(algo)/dashboard/+page.svelte` scoped CSS: add `.dash-activity.fs-card-on > .card-body { flex: 1 1 0; min-height: 0; max-height: none; overflow: hidden; }`. In `frontend/src/lib/ActivityLogModal.svelte` (and any other fullscreen card wrappers missing the pattern): apply the same flex-expansion rule scoped to the fullscreen state. The LogPanel's `.lp-body-wrap` already uses `display: contents` so the fix only needs to unlock the containing `.card-body`.
  (D) In `frontend/src/lib/ChartModal.svelte`: add a scoped CSS rule `.cm-overlay { z-index: 10600; pointer-events: auto; }` (or increment `--z-command` by 100 via inline style). Ensure the overlay's `onclick` handler calls `onClose()` when clicking the backdrop (not just the close button), matching the SymbolPanel pattern. Verify the close button has `pointer-events: auto` and is not obscured by any parent layer.
- broker: skip
- doc: skip
- backend-test: skip (covered inline in backend agent task)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Notes
- Agent A and E are independent — backend agent can run both in sequence
- Agent B, C, D are all in separate files — frontend agent runs all three
- Backend (A+F) and frontend (B+C+D+E) agents can run in parallel
- E (fullscreen height) is pure CSS — no logic changes, lowest risk

## Commit message
fix(multi): close-order intent threading + payoff race guard + timestamp stamp + ChartModal z-index

A: _start_live_chase now receives intent="close" so 50-lot ceiling bypass
   applies to large equity position closes via the chase path
B: loadStrategy generation counter prevents stale API response from
   overwriting a fresher payoff curve/overlay result
C: lastRefreshAt stamped after positions/holdings load, not after slow
   secondary fetches (fundsStore, _fetchNifty, batchQuoteChunked)
D: ChartModal overlay z-index raised to 10600 so it paints above
   SymbolPanel's stacking context; close button and backdrop now respond

## Done when
- A: A LIMIT close SELL order routes correctly through chase with intent="close"; unit test passes; adapter 50-lot ceiling bypassed for close
- B: Rapid leg changes or concurrent polls no longer leave the payoff curve/overlay in a stale state
- C: AlgoTimestamp refresh timestamp updates as soon as positions/holdings data arrives, not waiting for nifty quote or batchQuoteChunked
- D: Clicking the chart button on order modal shows ChartModal visually on top; clicking close or backdrop dismisses it
- E: Dashboard Activity card in fullscreen expands its grid to fill the card; no scroll bars when space is available
- svelte-check 0 errors, pytest passes
