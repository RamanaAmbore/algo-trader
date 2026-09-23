# Plan: SSE liveness watchdog + hibernation refire + SSOT derivation cleanup

## Task

Three interrelated frontend fixes addressing LTP staleness during market hours, stale data on tab
return, and SSOT derivation drift where portfolio aggregates are computed inline in PositionStrip
instead of in portfolioStore.

**Fix 1 — LTP heartbeat watchdog** (`quoteStream.js`):
- `_lastHbAt` module-level timestamp; stamped in `_onHeartbeat`, `_onSnapshot`, `_onTick`
- 45s silence watchdog via `visibleInterval` (not raw setInterval — avoids leak on hot-reload)
  checking `Date.now() - _lastHbAt > 45_000 && !_stopped` → call `restartQuoteStream()`
- Backend heartbeat cadence is 30s; 45s = 1.5× gives one missed beat as margin (industry standard)
- Guard the unconditional `visibilitychange` reconnect at line 78-83: only restart if
  `readyState === EventSource.CLOSED || Date.now() - _lastHbAt > 45_000`
  (avoids tearing down a healthy stream on every brief tab switch)
- Watchdog is torn down in `stopMarketGatedQuoteStream()` / `stopQuoteStream()`

**Fix 2 — Hibernation threshold** (`stores.js`):
- Change default `_hibernationIdleMs` from 5 minutes → 90 seconds
  (keeps the existing refire path — no new wake-up path)
- At 90s: mobile-throttled pollers that stalled (1-min browser cap) get an immediate refire on
  return without needing a third independent `visibilitychange` listener
- `setHibernationIdleMinutes()` still works from settings; `polling.idle_timeout_min` config still
  overrides this at runtime — no API change

**Fix 3 — SSOT derivation cleanup** (`portfolioStore.svelte.js`, `PositionStrip.svelte`):
- Move 7 inline `$derived.by()` portfolio aggregates out of PositionStrip into `portfolioStore`:
  `livePositionsPnl`, `liveHoldingsTotal`, `liveHoldingsValue`, `liveCashTotal`,
  `longOptionsCashPaid`, `marginAvail`, `marginTotal`
  (PositionStrip:486,503,527,619,643,674,679 per architect audit)
- Expose as named getters on `portfolioStore` following existing pattern (`portfolioStore.positions.*`)
- Each getter must gate on the 4Hz `_tick` counter + read symbolStore inside `untrack()`
  (same pattern as `_posTier2` / `_posAgg` — CRITICAL: no raw `$derived(getSnapshot(sym))`)
- Eliminate the `MarketPulse→holdingsDayPnlStore.setFromPulse` write-back
  (MarketPulse:2997-3013 per architect audit): `holdingsDayPnlStore.total` should derive from
  `portfolioStore` only, not be overridden by a component push. Holdings aggregate must be
  computed inside `portfolioStore` directly from raw `holdingsStore.value` rows
- PositionStrip reads the new getters instead of re-deriving

## Agents
- frontend: Fix 1 (quoteStream.js watchdog + visibilitychange guard) + Fix 2 (stores.js default
  threshold) + Fix 3 (portfolioStore getters + PositionStrip reads + holdingsDayPnlStore write-back removal)
- backend: skip
- broker: skip
- doc: skip (CLAUDE.md + PULSE_SPEC minor mention if at all — non-critical)
- backend-test: skip
- playwright: Add tests: (1) SSE reconnect fires on heartbeat timeout, (2) tab-return after 90s
  triggers book-poll refire, (3) PositionStrip aggregates match portfolioStore getters

## Tests
- pytest: no
- svelte-check: yes
- playwright: yes

## Commit message
fix(reactivity): SSE liveness watchdog, 90s hibernation refire, SSOT portfolio aggregates

- quoteStream: stamp _lastHbAt on heartbeat/snapshot/tick; 45s silence watchdog via visibleInterval;
  guard visibilitychange reconnect (only if CLOSED or stale heartbeat)
- stores: lower default hibernation threshold 5min→90s; no new wake-up path added
- portfolioStore: add livePositionsPnl/liveHoldingsTotal/liveHoldingsValue/liveCashTotal/
  longOptionsCashPaid/marginAvail/marginTotal as named getters with 4Hz _tick gate
- holdingsDayPnlStore: remove setFromPulse write-back; derive exclusively from portfolioStore
- PositionStrip: read new portfolioStore getters; remove 7 inline $derived.by() aggregates

## Done when
- `svelte-check` 0 errors
- PositionStrip no longer has inline `$derived.by()` for any of the 7 portfolio aggregates
- `holdingsDayPnlStore.setFromPulse` method is gone (or no longer called from MarketPulse)
- `quoteStream.js` heartbeat handler stamps `_lastHbAt`; watchdog interval exists and checks 45s
- `_hibernationIdleMs` default is 90_000 in stores.js
- Playwright: heartbeat-stale restart test green
