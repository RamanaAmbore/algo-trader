# Plan: Vitest unit tests — frontend utility layer

## Context
The frontend has Playwright e2e tests (against dev.ramboq.com) but no unit test runner.
Pure/near-pure utility modules — `nav.js`, `format.js`, `rootOf.js`, `chartPrefs.js` — contain
the logic most likely to cause incidents (Day P&L formula, NAV aggregation, virtual symbol
mapping, number formatters). These have no DOM/Svelte dependency and are ideal for Vitest.
Scope is intentionally narrow: utility modules only, no Svelte component testing.
`chartStore.svelte.js` excluded for now — runes at module scope require Svelte plugin
in Vitest; can be added in a follow-up once the scaffold is proven.

## Agents
- backend: skip
- frontend: Two tasks — scaffold first, then tests.

  **Task A — Vitest scaffold (commit separately):**

  1. Install vitest: `cd frontend && npm install --save-dev vitest`

  2. Create `frontend/vitest.config.js`:
  ```javascript
  import { defineConfig } from 'vitest/config';
  import path from 'path';

  export default defineConfig({
    test: {
      environment: 'node',
      include: ['src/lib/__tests__/**/*.test.js'],
    },
    resolve: {
      alias: {
        $lib: path.resolve('./src/lib'),
      },
    },
  });
  ```

  3. Add scripts to `frontend/package.json`:
  ```json
  "test:unit": "vitest run",
  "test:unit:watch": "vitest"
  ```

  Commit: `chore(test): add Vitest + config — frontend unit test scaffold`

  **Task B — Write test files (single commit):**

  Create `frontend/src/lib/__tests__/data/nav.test.js` — test `baseDayPnlForPosition` (ALL cases),
  `aggregateDayPnlForPositions`, `livePositionDayPnl`, `navTotalRow`:

  - `baseDayPnlForPosition`:
    - `prev_settlement_pnl` finite → returns `pnl - prev_settlement_pnl` (authoritative path)
    - `prev_settlement_pnl` null/NaN, `oq > 0, dcv !== 0` → returns `dcv` (Case 1)
    - `oq > 0, dcv === 0, close > 0` → returns `pnl - oq*(close - avg)` (Case 3)
    - `oq > 0, dcv === 0, close <= 0` → returns `0` (Case 4 — MCX zero-close guard)
    - `oq === 0, pnl !== 0` → returns `pnl` (new intraday position)
    - All zeros → returns `0`
  - `aggregateDayPnlForPositions`: sums correctly, empty array → 0
  - `livePositionDayPnl`: market closed → returns base, market open + liveLtp > 0 → `(liveLtp - close) * qty`,
    new position (`closePx=0, avg>0`) → `(liveLtp - avg) * qty`, liveLtp absent → falls back
  - `navTotalRow`: sums cash/pos_m2m/holdings_mtm/nav, empty array → null

  Create `frontend/src/lib/__tests__/format.test.js` — test all exports from `format.js`:
  - `aggCompact`: `< 1K` (plain), `1000` → `"1.00K"`, `100000` → `"1.00L"`, `10000000` → `"1.00C"`,
    negative values, zero, null/NaN → `"—"`
  - `priceFmt`: 2 decimals, null/NaN/Infinity → `"—"`, negative prices
  - `qtyFmt`: integer grouping, zero, negative
  - `directional`: long (`netQty > 0`) → pass-through, short (`netQty < 0`) → negate
  - `fmtPctScaled`: `5.0 → "5.00%"`, signed=true adds `+`, decimals override
  - `fmtPctFraction`: `0.05 → "5.00%"`, `0 → "0.00%"`

  Create `frontend/src/lib/__tests__/data/rootOf.test.js` — test `rootOf`, `resolveVirtual`,
  `rootOfLabel` using `seedRootMapFromInstruments` to prime the maps:
  - `rootOf`: front-month contract → bare root `"CRUDEOIL"`, back-month → `"CRUDEOIL_NEXT"`,
    far-month → pass-through, equity/options (non-FUT) → pass-through, unknown exchange → pass-through
  - `resolveVirtual`: `"CRUDEOIL"` → front-month tradingsymbol, `"CRUDEOIL_NEXT"` → back-month,
    unknown virtual → undefined
  - `rootOfLabel`: `"_NEXT"` internal → `".NEXT"` display suffix
  - `seedRootMapFromInstruments`: filters by FUT, skips settling-today contracts,
    keeps at most 2 per root ordered by expiry

  Create `frontend/src/lib/__tests__/data/chartPrefs.test.js` — test `readChartPref`
  and `writeChartPref` with a localStorage mock set up via `beforeEach`:
  ```javascript
  // Mock localStorage (Node environment has no window.localStorage)
  const _store = {};
  global.localStorage = {
    getItem: (k) => _store[k] ?? null,
    setItem: (k, v) => { _store[k] = String(v); },
    removeItem: (k) => { delete _store[k]; },
    clear: () => { Object.keys(_store).forEach(k => delete _store[k]); },
  };
  ```
  - `readChartPref`: absent key → default, invalid JSON → default, validation fails → default,
    valid stored value → parsed, null default → null on miss
  - `writeChartPref`: round-trips correctly, quota error (mock setItem throw) → silent no-op

  All four test files in a single commit: `test(frontend): unit tests for nav, format, rootOf, chartPrefs`

- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: no
- svelte-check: yes (confirms no import errors from new config)
- playwright: no
- vitest: yes — run `cd frontend && npm run test:unit` after each commit; must be 0 failures

## Commit message
Two commits:

1. `chore(test): add Vitest + config — frontend unit test scaffold`
2. `test(frontend): unit tests for nav, format, rootOf, chartPrefs`

## Done when
- `npm run test:unit` exits 0 with all tests passing
- `nav.test.js` covers all `baseDayPnlForPosition` cases (including MCX zero-close guard)
- `format.test.js` covers null/Infinity/edge-magnitude paths
- `rootOf.test.js` covers front/back/far-month + reverse lookup
- `chartPrefs.test.js` covers read/write + localStorage mock
- `svelte-check` 0 errors
