/**
 * brokerHealth.test.js
 *
 * Unit tests for `brokerHealthWorstState()`, now the exported helper in
 * `$lib/data/brokerHealthState.js` (moved out of `stores.js` 2026-10-24
 * alongside the new `ticker_idle` extension — see
 * `__tests__/data/brokerHealthState.test.js` for the fuller suite,
 * including `toBrokerHealthState`/`brokerChipState`).
 *
 * This file used to hand-maintain a local REPLICA of the logic (it lived
 * unexported in stores.js) — repointed to import the real function so a
 * regression in the actual precedence logic fails here too, not only in
 * a parallel copy that can silently drift.
 *
 * Five quality dimensions:
 *   1. SSOT   — imports the real `brokerHealthWorstState`, no local replica
 *   2. Perf   — pure unit, no DOM / network / stores, sub-millisecond
 *   3. Stale  — off-market (all-inactive) case now returns 'green', not 'amber'
 *   4. Reuse  — BrokerAccountHealth shape matches the JSDoc typedef in
 *               brokerHealthState.js
 *   5. UX     — chip colour: green off-market, amber on stale, red on failed account
 */

import { describe, it, expect } from 'vitest';
import { brokerHealthWorstState as _brokerHealthWorstState } from '$lib/data/brokerHealthState.js';

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
