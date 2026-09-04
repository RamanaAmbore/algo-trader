/**
 * orderKnobsAmo.test.js — unit tests for the AMO variety-filter logic
 * that lives in OrderKnobsRow.svelte.
 *
 * The `_amoAllowed` derived + `_varietyOptions` derived are extracted
 * here as pure helper functions so the filtering contract is testable
 * without a Svelte component environment. Playwright's
 * order_knobs_smoke.spec.js covers the rendered dropdown behaviour.
 *
 * Five quality dimensions:
 *  1. SSOT   — mirrors the exact broker_id strings used in capabilities.py
 *  2. Perf   — synchronous, no I/O
 *  3. Stale  — guards the "null caps → show AMO" backward-compat path
 *  4. Reuse  — pure helpers; same logic as the Svelte $derived
 *  5. UX     — verifies AMO is absent for Dhan/Groww so placement never fails
 */

import { describe, it, expect } from 'vitest';

// ── Pure helpers mirroring OrderKnobsRow.$derived logic ──────────────────────

/** @param {any} brokerCaps */
function amoAllowed(brokerCaps) {
  return (
    brokerCaps == null ||
    (brokerCaps.broker_id !== 'dhan' && brokerCaps.broker_id !== 'groww')
  );
}

/** @param {any} brokerCaps */
function varietyOptions(brokerCaps) {
  return [
    { value: 'regular', label: 'REG' },
    ...(amoAllowed(brokerCaps) ? [{ value: 'amo', label: 'AMO' }] : []),
    { value: 'co',      label: 'CO'  },
  ];
}

/** @param {any} brokerCaps @param {string} variety */
function effectiveVariety(brokerCaps, variety) {
  const allowed = varietyOptions(brokerCaps).map(o => o.value);
  return allowed.includes(variety) ? variety : 'regular';
}

// ─────────────────────────────────────────────────────────────────────────────

describe('AMO variety filter — amoAllowed()', () => {
  it('allows AMO when brokerCaps is null (no account selected)', () => {
    expect(amoAllowed(null)).toBe(true);
  });

  it('allows AMO for Kite (zerodha_kite)', () => {
    expect(amoAllowed({ broker_id: 'zerodha_kite' })).toBe(true);
  });

  it('allows AMO for kite legacy alias', () => {
    expect(amoAllowed({ broker_id: 'kite' })).toBe(true);
  });

  it('blocks AMO for Dhan', () => {
    expect(amoAllowed({ broker_id: 'dhan' })).toBe(false);
  });

  it('blocks AMO for Groww', () => {
    expect(amoAllowed({ broker_id: 'groww' })).toBe(false);
  });

  it('allows AMO for unknown future brokers (conservative allow)', () => {
    expect(amoAllowed({ broker_id: 'fyers' })).toBe(true);
  });
});

describe('AMO variety filter — varietyOptions()', () => {
  it('includes AMO when brokerCaps is null', () => {
    const opts = varietyOptions(null);
    expect(opts.map(o => o.value)).toContain('amo');
  });

  it('includes AMO for Kite', () => {
    const opts = varietyOptions({ broker_id: 'zerodha_kite' });
    expect(opts.map(o => o.value)).toContain('amo');
  });

  it('excludes AMO for Dhan', () => {
    const opts = varietyOptions({ broker_id: 'dhan' });
    expect(opts.map(o => o.value)).not.toContain('amo');
  });

  it('excludes AMO for Groww', () => {
    const opts = varietyOptions({ broker_id: 'groww' });
    expect(opts.map(o => o.value)).not.toContain('amo');
  });

  it('always includes regular and co for all brokers', () => {
    for (const bid of ['dhan', 'groww', 'zerodha_kite', 'kite', null]) {
      const caps = bid == null ? null : { broker_id: bid };
      const values = varietyOptions(caps).map(o => o.value);
      expect(values).toContain('regular');
      expect(values).toContain('co');
    }
  });

  it('Dhan options are exactly [regular, co]', () => {
    const opts = varietyOptions({ broker_id: 'dhan' });
    expect(opts.map(o => o.value)).toEqual(['regular', 'co']);
  });

  it('Kite options include all three varieties', () => {
    const opts = varietyOptions({ broker_id: 'zerodha_kite' });
    expect(opts.map(o => o.value)).toEqual(['regular', 'amo', 'co']);
  });
});

describe('AMO variety filter — reset effect (effectiveVariety)', () => {
  it('keeps regular when switching from Kite to Dhan', () => {
    expect(effectiveVariety({ broker_id: 'dhan' }, 'regular')).toBe('regular');
  });

  it('resets amo → regular when switching from Kite to Dhan', () => {
    expect(effectiveVariety({ broker_id: 'dhan' }, 'amo')).toBe('regular');
  });

  it('resets amo → regular when switching from Kite to Groww', () => {
    expect(effectiveVariety({ broker_id: 'groww' }, 'amo')).toBe('regular');
  });

  it('preserves amo when broker is Kite', () => {
    expect(effectiveVariety({ broker_id: 'zerodha_kite' }, 'amo')).toBe('amo');
  });

  it('preserves co for all brokers', () => {
    for (const bid of ['dhan', 'groww', 'zerodha_kite']) {
      expect(effectiveVariety({ broker_id: bid }, 'co')).toBe('co');
    }
  });
});
