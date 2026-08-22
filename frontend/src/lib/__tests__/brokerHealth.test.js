/**
 * brokerHealth.test.js
 *
 * Unit tests for the `_brokerHealthWorstState` logic in stores.js.
 *
 * `_brokerHealthWorstState` is not exported, so we replicate its logic
 * inline here (same pattern as computeHoldingsDayPnl in holdingsDayPnlStore.test.js).
 * Any change to stores.js must be mirrored here.
 *
 * Five quality dimensions:
 *   1. SSOT   — local replica mirrors the exact logic in stores.js
 *   2. Perf   — pure unit, no DOM / network / stores, sub-millisecond
 *   3. Stale  — off-market (all-inactive) case now returns 'green', not 'amber'
 *   4. Reuse  — BrokerAccountHealth shape matches the JSDoc typedef in stores.js
 *   5. UX     — chip colour: green off-market, amber on stale, red on failed account
 */

import { describe, it, expect } from 'vitest';

// ── Local replica of _brokerHealthWorstState from stores.js ──────────────────
// Keep in sync with stores.js:_brokerHealthWorstState.
// red > amber > green; inactive accounts are treated as healthy (green).
// Empty accounts → 'green' (absence of health data is not a problem).

/**
 * @param {{ state: string }[]} accounts
 * @returns {'green'|'amber'|'red'}
 */
function _brokerHealthWorstState(accounts) {
  if (!accounts || !accounts.length) return 'green';
  const active = accounts.filter(a => a.state !== 'inactive');
  const base = active.length ? active : accounts;
  if (base.some(a => a.state === 'red'))   return 'red';
  if (base.some(a => a.state === 'amber')) return 'amber';
  if (base.every(a => a.state === 'green' || a.state === 'inactive')) return 'green';
  // all inactive (reached only when base===accounts and every item is inactive)
  return 'green';
}

// ── Tests ────────────────────────────────────────────────────────────────────

describe('_brokerHealthWorstState — off-market / inactive', () => {
  it('all accounts inactive → green (off-market expected state, not a problem)', () => {
    const accounts = [
      { state: 'inactive' },
      { state: 'inactive' },
    ];
    expect(_brokerHealthWorstState(accounts)).toBe('green');
  });

  it('mix of inactive and green → green', () => {
    const accounts = [
      { state: 'green' },
      { state: 'inactive' },
      { state: 'green' },
    ];
    expect(_brokerHealthWorstState(accounts)).toBe('green');
  });

  it('single inactive account → green', () => {
    expect(_brokerHealthWorstState([{ state: 'inactive' }])).toBe('green');
  });

  it('empty array → green (no data is not a problem)', () => {
    expect(_brokerHealthWorstState([])).toBe('green');
  });

  it('null/undefined → green (no data is not a problem)', () => {
    expect(_brokerHealthWorstState(null)).toBe('green');
    expect(_brokerHealthWorstState(undefined)).toBe('green');
  });
});

describe('_brokerHealthWorstState — active account degradation', () => {
  it('one amber account → amber', () => {
    const accounts = [
      { state: 'green' },
      { state: 'amber' },
    ];
    expect(_brokerHealthWorstState(accounts)).toBe('amber');
  });

  it('one red account → red (red beats amber)', () => {
    const accounts = [
      { state: 'green' },
      { state: 'amber' },
      { state: 'red' },
    ];
    expect(_brokerHealthWorstState(accounts)).toBe('red');
  });

  it('one red + one inactive → red (inactive excluded from active base)', () => {
    const accounts = [
      { state: 'inactive' },
      { state: 'red' },
    ];
    expect(_brokerHealthWorstState(accounts)).toBe('red');
  });

  it('amber + inactive → amber (inactive excluded from active base)', () => {
    const accounts = [
      { state: 'inactive' },
      { state: 'amber' },
    ];
    expect(_brokerHealthWorstState(accounts)).toBe('amber');
  });

  it('all green → green', () => {
    const accounts = [
      { state: 'green' },
      { state: 'green' },
    ];
    expect(_brokerHealthWorstState(accounts)).toBe('green');
  });
});
