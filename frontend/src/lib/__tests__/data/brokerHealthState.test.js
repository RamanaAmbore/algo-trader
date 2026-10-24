/**
 * brokerHealthState.test.js — Vitest unit tests for
 * `$lib/data/brokerHealthState.js`, the pure helpers backing the broker-chip
 * color + `BrokerHealthBadge` popup (`stores.js`, `+layout.svelte`).
 *
 * Covers the 2026-10-24 `ticker_idle`/`ticker_idle_reason` extension to
 * `GET /api/admin/broker-health` (calendar-gated ticker-connect status,
 * distinct from each account's own REST-fetch `state`).
 *
 * Five quality dimensions:
 *   1. SSOT   — imports the REAL module (not a hand-maintained replica like
 *               the sibling `__tests__/brokerHealth.test.js` used to), so a
 *               regression in the actual precedence logic fails this file.
 *   2. Perf   — pure unit, no DOM / network / stores, sub-millisecond.
 *   3. Stale  — a missing `ticker_idle`/`ticker_idle_reason` field (older
 *               cached response shape) must default to not-idle, never
 *               read as idle by absence.
 *   4. Reuse  — `toBrokerHealthState` is the one mapper both `stores.js`'s
 *               poller and any future consumer should call; no duplicate
 *               shape-mapping logic should exist elsewhere.
 *   5. UX     — the core invariant: a genuine red account failure always
 *               wins the chip color over `ticker_idle`, on every other
 *               worstState value `tickerIdle` takes the neutral idle
 *               treatment ahead of amber (the explicit brief rule — amber
 *               is EXPECTED staleness on a confirmed-closed day, not a
 *               problem, so idle must still show instead of amber).
 */

import { describe, it, expect } from 'vitest';
import {
  brokerHealthWorstState,
  toBrokerHealthState,
  brokerChipState,
} from '$lib/data/brokerHealthState.js';

// ── brokerHealthWorstState — same cases the old replica-based test had,
//    now against the real implementation ───────────────────────────────────

describe('brokerHealthWorstState', () => {
  it('all accounts inactive → green (off-market expected state, not a problem)', () => {
    expect(brokerHealthWorstState([{ state: 'inactive' }, { state: 'inactive' }])).toBe('green');
  });

  it('one red account → red (red beats amber)', () => {
    const accounts = [{ state: 'green' }, { state: 'amber' }, { state: 'red' }];
    expect(brokerHealthWorstState(accounts)).toBe('red');
  });

  it('one amber account → amber', () => {
    expect(brokerHealthWorstState([{ state: 'green' }, { state: 'amber' }])).toBe('amber');
  });

  it('empty / null / undefined → green (no data is not a problem)', () => {
    expect(brokerHealthWorstState([])).toBe('green');
    expect(brokerHealthWorstState(null)).toBe('green');
    expect(brokerHealthWorstState(undefined)).toBe('green');
  });
});

// ── toBrokerHealthState — the response → store-shape mapper ─────────────────

describe('toBrokerHealthState', () => {
  it('maps ticker_idle=true + ticker_idle_reason through', () => {
    const data = {
      accounts: [{ state: 'amber' }],
      ticker_idle: true,
      ticker_idle_reason: 'market calendar confirms closed today — ticker not connected by design',
    };
    const result = toBrokerHealthState(data);
    expect(result.tickerIdle).toBe(true);
    expect(result.tickerIdleReason).toBe(
      'market calendar confirms closed today — ticker not connected by design',
    );
    expect(result.worstState).toBe('amber');
    expect(result.accounts).toEqual([{ state: 'amber' }]);
  });

  it('ticker_idle=false + empty reason maps through as falsy/empty', () => {
    const data = { accounts: [{ state: 'green' }], ticker_idle: false, ticker_idle_reason: '' };
    const result = toBrokerHealthState(data);
    expect(result.tickerIdle).toBe(false);
    expect(result.tickerIdleReason).toBe('');
  });

  it('missing ticker_idle/ticker_idle_reason fields (older response shape) default to not-idle', () => {
    const data = { accounts: [{ state: 'green' }] };
    const result = toBrokerHealthState(data);
    expect(result.tickerIdle).toBe(false);
    expect(result.tickerIdleReason).toBe('');
  });

  it('missing accounts array defaults to empty, worstState green', () => {
    const result = toBrokerHealthState({});
    expect(result.accounts).toEqual([]);
    expect(result.worstState).toBe('green');
    expect(result.tickerIdle).toBe(false);
  });

  it('null/undefined data does not throw and defaults safely', () => {
    expect(() => toBrokerHealthState(null)).not.toThrow();
    expect(() => toBrokerHealthState(undefined)).not.toThrow();
    expect(toBrokerHealthState(null)).toEqual({
      accounts: [], worstState: 'green', tickerIdle: false, tickerIdleReason: '',
    });
  });
});

// ── brokerChipState — the precedence rule: red > idle > amber > green ──────

describe('brokerChipState', () => {
  it('red + idle=true → down (idle must never mask a real red failure)', () => {
    expect(brokerChipState('red', true)).toBe('down');
  });

  it('red + idle=false → down', () => {
    expect(brokerChipState('red', false)).toBe('down');
  });

  it('amber + idle=true → idle (amber is expected staleness on a closed day, not a problem)', () => {
    expect(brokerChipState('amber', true)).toBe('idle');
  });

  it('green + idle=true → idle', () => {
    expect(brokerChipState('green', true)).toBe('idle');
  });

  it('amber + idle=false → partial', () => {
    expect(brokerChipState('amber', false)).toBe('partial');
  });

  it('green + idle=false → ok', () => {
    expect(brokerChipState('green', false)).toBe('ok');
  });

  it('unrecognized worstState + idle=false falls back to partial (defensive, mirrors old ternary)', () => {
    expect(brokerChipState('unknown', false)).toBe('partial');
  });

  it('unrecognized worstState + idle=true → idle (idle still takes precedence over an unknown value)', () => {
    expect(brokerChipState('unknown', true)).toBe('idle');
  });
});
