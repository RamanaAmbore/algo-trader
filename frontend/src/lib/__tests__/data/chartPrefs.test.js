import { describe, it, expect, beforeEach } from 'vitest';
import { readChartPref, writeChartPref } from '$lib/data/chartPrefs.js';

/**
 * The global localStorage mock is installed in vitest.setup.js before any
 * module is imported, so _LS_AVAILABLE (module-level const in chartPrefs.js)
 * evaluates to true and the read/write helpers are active.
 *
 * We clear the shared mock store before each test so tests are independent.
 */
beforeEach(() => {
  localStorage.clear();
});

// ── readChartPref ────────────────────────────────────────────────────────────

describe('readChartPref', () => {
  it('absent key → returns defaultValue', () => {
    expect(readChartPref('rbq.test.absent', 30)).toBe(30);
  });

  it('absent key, null default → returns null', () => {
    expect(readChartPref('rbq.test.absent', null)).toBeNull();
  });

  it('valid stored value → returns parsed value', () => {
    localStorage.setItem('rbq.test.range', JSON.stringify(90));
    expect(readChartPref('rbq.test.range', 30)).toBe(90);
  });

  it('stored string value round-trips', () => {
    localStorage.setItem('rbq.test.type', JSON.stringify('candlestick'));
    expect(readChartPref('rbq.test.type', 'line')).toBe('candlestick');
  });

  it('stored object value round-trips', () => {
    const obj = { indicators: ['ema', 'sma'], period: 14 };
    localStorage.setItem('rbq.test.obj', JSON.stringify(obj));
    expect(readChartPref('rbq.test.obj', {})).toEqual(obj);
  });

  it('invalid JSON stored → returns defaultValue', () => {
    localStorage.setItem('rbq.test.bad', 'not-valid-json{{{');
    expect(readChartPref('rbq.test.bad', 42)).toBe(42);
  });

  it('validation passes → returns stored value', () => {
    localStorage.setItem('rbq.test.range', JSON.stringify(90));
    const result = readChartPref('rbq.test.range', 30, v => [30, 60, 90, 180].includes(v));
    expect(result).toBe(90);
  });

  it('validation fails → returns defaultValue', () => {
    localStorage.setItem('rbq.test.range', JSON.stringify(999));
    const result = readChartPref('rbq.test.range', 30, v => [30, 60, 90, 180].includes(v));
    expect(result).toBe(30);
  });

  it('stored null → returns defaultValue', () => {
    localStorage.setItem('rbq.test.null', JSON.stringify(null));
    expect(readChartPref('rbq.test.null', 50)).toBe(50);
  });
});

// ── writeChartPref ───────────────────────────────────────────────────────────

describe('writeChartPref', () => {
  it('stores a number that can be read back', () => {
    writeChartPref('rbq.test.num', 90);
    expect(readChartPref('rbq.test.num', 30)).toBe(90);
  });

  it('stores a string that can be read back', () => {
    writeChartPref('rbq.test.str', 'ohlc');
    expect(readChartPref('rbq.test.str', 'line')).toBe('ohlc');
  });

  it('stores a boolean that can be read back', () => {
    writeChartPref('rbq.test.bool', true);
    expect(readChartPref('rbq.test.bool', false)).toBe(true);
  });

  it('stores an object that can be read back', () => {
    const prefs = { zoom: 1.5, pan: 100 };
    writeChartPref('rbq.test.obj', prefs);
    expect(readChartPref('rbq.test.obj', {})).toEqual(prefs);
  });

  it('overwrites previous value', () => {
    writeChartPref('rbq.test.overwrite', 30);
    writeChartPref('rbq.test.overwrite', 90);
    expect(readChartPref('rbq.test.overwrite', 30)).toBe(90);
  });

  it('quota error (mock setItem throw) → silent no-op', () => {
    const original = localStorage.setItem.bind(localStorage);
    localStorage.setItem = () => { throw new DOMException('QuotaExceededError'); };
    // Should not throw
    expect(() => writeChartPref('rbq.test.quota', 999)).not.toThrow();
    // Restore
    localStorage.setItem = original;
  });
});

// ── round-trip ────────────────────────────────────────────────────────────────

describe('round-trip', () => {
  it('write then read returns the same value', () => {
    const key = 'rbq.test.roundtrip';
    const val = { series: 'candlestick', overlays: ['ema20'], intraday: true };
    writeChartPref(key, val);
    expect(readChartPref(key, {})).toEqual(val);
  });
});
