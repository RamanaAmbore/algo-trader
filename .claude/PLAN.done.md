# Plan: Fix cadence/dependency audit findings (P2 + P3)

## Task
Fix four inconsistencies found in the cadence/dependency audit:
1. P2: holdingsDayPnlStore reads holdingsStore, but H pill display reads pulseHoldingsStore — unify to pulseHoldingsStore
2. P3a: _dataChangedTick comment is stale (says "only on fingerprint change" but pre-increments on every fill for UX)
3. P3b: positionsDayPnlStore hardcodes `marketOpen: true` — should read isMarketOpen() at call time
4. P3c: recompute_row_percentages called with boolean Series mask instead of pd.Index in holdings.py

## Agents
- frontend: Fix P2 (holdingsDayPnlStore → pulseHoldingsStore) + P3a (stale comment) + P3b (marketOpen)
- backend: Fix P3c (mask type in holdings.py)
- backend-test: skip
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(cadence): unify H pill to pulseHoldingsStore; fix positionsDayPnlStore marketOpen; fix holdings mask type

## Done when
- holdingsDayPnlStore imports pulseHoldingsStore and reads pulseHoldingsStore.value
- PositionStrip fingerprint uses pulseHoldingsStore.value for holdings half
- positionsDayPnlStore uses isMarketOpen() not hardcoded true
- holdings.py recompute_row_percentages receives pd.Index not boolean Series
- all tests pass
