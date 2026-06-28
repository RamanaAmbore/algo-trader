/**
 * indicators.test.js — Unit tests for pure indicator functions.
 *
 * These tests use only the Node.js test runner (no framework dependency).
 * Run with:  node --test frontend/scripts/indicators.test.js
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT  — computed values match hand-calculated reference values
 *  2. Perf  — no async I/O; all compute is synchronous
 *  3. Stale — all indicator math lives here, not duplicated in ChartWorkspace
 *  4. Reuse — same module used by ChartWorkspace (import paths match)
 *  5. UX    — edge cases: fewer bars than N, N=1, negative N
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { sma, ema, vwap, bollinger, rsi, macd } from '../src/lib/chart/indicators.js';

// ── Shared fixture: 30 bars of synthetic OHLCV ───────────────────────────────
// Prices are a simple arithmetic sequence 100, 102, 104, … so we can
// compute reference values by hand without floating-point surprises.
function mkBars(n = 30, step = 2, startClose = 100) {
  return Array.from({ length: n }, (_, i) => ({
    ts: `2026-01-${String(i + 1).padStart(2, '0')}`,
    open:   startClose + i * step - 0.5,
    high:   startClose + i * step + 1,
    low:    startClose + i * step - 1,
    close:  startClose + i * step,
    volume: 1000 + i * 10,
  }));
}

const BARS = mkBars(30);

// ── SMA ───────────────────────────────────────────────────────────────────────
describe('sma', () => {
  test('returns null for first n-1 bars', () => {
    const out = sma(BARS, 5);
    assert.equal(out.length, BARS.length);
    for (let i = 0; i < 4; i++) {
      assert.equal(out[i].value, null, `bar ${i} should be null`);
    }
    assert.ok(out[4].value !== null, 'bar 4 should be defined');
  });

  test('SMA(3) at bar index 2 == (100+102+104)/3 = 102', () => {
    const out = sma(BARS, 3);
    // bars[0].close=100, [1]=102, [2]=104 → avg 102
    assert.equal(out[2].value, 102);
  });

  test('SMA(1) equals close of every bar', () => {
    const out = sma(BARS, 1);
    for (let i = 0; i < BARS.length; i++) {
      assert.equal(out[i].value, BARS[i].close);
    }
  });

  test('fewer bars than n → all null', () => {
    const short = mkBars(3);
    const out = sma(short, 10);
    assert.ok(out.every((p) => p.value === null));
  });

  test('throws for n=0', () => {
    assert.throws(() => sma(BARS, 0), RangeError);
  });

  test('throws for negative n', () => {
    assert.throws(() => sma(BARS, -5), RangeError);
  });

  test('throws for non-integer n', () => {
    assert.throws(() => sma(BARS, 1.5), RangeError);
  });
});

// ── EMA ───────────────────────────────────────────────────────────────────────
describe('ema', () => {
  test('returns null for first n-1 bars', () => {
    const out = ema(BARS, 5);
    for (let i = 0; i < 4; i++) {
      assert.equal(out[i].value, null);
    }
    assert.ok(out[4].value !== null);
  });

  test('EMA(3) seed at bar 2 equals SMA(100,102,104) = 102', () => {
    const out = ema(BARS, 3);
    // Seed: (100+102+104)/3 = 102
    assert.equal(out[2].value, 102);
  });

  test('EMA converges (later values trail current price for rising series)', () => {
    const out = ema(BARS, 5);
    // For an arithmetic series EMA should converge toward current price
    // but lag behind it. So for a steadily rising series, EMA < close.
    const last = out[out.length - 1];
    assert.ok(last.value !== null);
    assert.ok(last.value < BARS[BARS.length - 1].close, 'EMA trails rising price');
  });

  test('throws for n=0', () => {
    assert.throws(() => ema(BARS, 0), RangeError);
  });

  test('fewer bars than n → all null', () => {
    const out = ema(mkBars(2), 10);
    assert.ok(out.every((p) => p.value === null));
  });
});

// ── VWAP ─────────────────────────────────────────────────────────────────────
describe('vwap', () => {
  test('returns value for every bar (no warmup)', () => {
    const out = vwap(BARS);
    assert.equal(out.length, BARS.length);
    assert.ok(out.every((p) => p.value !== null));
  });

  test('VWAP at bar 0 equals typical price of bar 0', () => {
    const b = BARS[0];
    const tp = (Number(b.high) + Number(b.low) + Number(b.close)) / 3;
    const out = vwap(BARS);
    assert.ok(Math.abs(out[0].value - tp) < 1e-9);
  });

  test('VWAP is weighted toward high-volume bars', () => {
    // Two bars: one big volume at high price, one tiny volume at low price.
    const bars = [
      { ts: '2026-01-01', high: 110, low: 100, close: 105, volume: 10000 },
      { ts: '2026-01-02', high: 80,  low: 70,  close: 75,  volume: 1 },
    ];
    const out = vwap(bars);
    const tp0 = (110 + 100 + 105) / 3; // ≈ 105
    // VWAP at bar 1 should be very close to tp0 (bar 0's TP dominates)
    assert.ok(Math.abs(out[1].value - tp0) < 1, 'VWAP dominated by high-volume bar');
  });

  test('zero-volume bars return null (no divide by zero)', () => {
    const bars = [
      { ts: '2026-01-01', high: 100, low: 100, close: 100, volume: 0 },
    ];
    const out = vwap(bars);
    assert.equal(out[0].value, null);
  });
});

// ── Bollinger Bands ───────────────────────────────────────────────────────────
describe('bollinger', () => {
  test('null for first n-1 bars', () => {
    const out = bollinger(BARS, 20, 2);
    for (let i = 0; i < 19; i++) {
      assert.equal(out[i].mid, null);
    }
    assert.ok(out[19].mid !== null);
  });

  test('upper > mid > lower for varying series', () => {
    const out = bollinger(BARS, 5, 2);
    for (let i = 4; i < out.length; i++) {
      assert.ok(out[i].upper > out[i].mid, `upper > mid at ${i}`);
      assert.ok(out[i].mid  > out[i].lower, `mid > lower at ${i}`);
    }
  });

  test('constant price series → zero bandwidth (upper==lower==mid)', () => {
    const flat = Array.from({ length: 25 }, (_, i) => ({
      ts: `2026-01-${i + 1}`, open: 100, high: 100, low: 100, close: 100, volume: 1000,
    }));
    const out = bollinger(flat, 5, 2);
    for (let i = 4; i < out.length; i++) {
      assert.ok(Math.abs(out[i].upper - out[i].mid) < 1e-9, 'upper==mid for flat');
      assert.ok(Math.abs(out[i].lower - out[i].mid) < 1e-9, 'lower==mid for flat');
    }
  });

  test('throws for n=0', () => {
    assert.throws(() => bollinger(BARS, 0), RangeError);
  });
});

// ── RSI ───────────────────────────────────────────────────────────────────────
describe('rsi', () => {
  test('null for first n bars', () => {
    const out = rsi(BARS, 14);
    // bar[0] has no delta; bars[1..13] are seeding; first value at bar[14]
    for (let i = 0; i < 14; i++) {
      assert.equal(out[i].value, null, `bar ${i} should be null`);
    }
    assert.ok(out[14].value !== null, 'bar 14 should have a value');
  });

  test('RSI output in [0, 100]', () => {
    const out = rsi(BARS, 14);
    for (const p of out) {
      if (p.value == null) continue;
      assert.ok(p.value >= 0 && p.value <= 100, `RSI out of range: ${p.value}`);
    }
  });

  test('steadily rising series → RSI > 50', () => {
    // A pure monotone-up series should produce RSI above 50
    const out = rsi(BARS, 14);
    const validValues = out.map((p) => p.value).filter((v) => v != null);
    assert.ok(validValues.every((v) => v > 50), 'RSI > 50 for rising series');
  });

  test('throws for n=0', () => {
    assert.throws(() => rsi(BARS, 0), RangeError);
  });

  test('fewer bars than n+1 → only nulls', () => {
    const out = rsi(mkBars(5), 14);
    assert.ok(out.every((p) => p.value === null));
  });
});

// ── MACD ─────────────────────────────────────────────────────────────────────
describe('macd', () => {
  const LONG_BARS = mkBars(60);

  test('returns array same length as bars', () => {
    const out = macd(LONG_BARS);
    assert.equal(out.length, LONG_BARS.length);
  });

  test('null for first (slow-1) bars', () => {
    // Default: slow=26, so bars[0..24] should have null macd
    const out = macd(LONG_BARS, 12, 26, 9);
    for (let i = 0; i < 25; i++) {
      assert.equal(out[i].macd, null, `bar ${i} macd should be null`);
    }
    assert.ok(out[25].macd !== null, 'bar 25 should have macd value');
  });

  test('signal line defined after slow+signal warmup', () => {
    // signal becomes defined after slow-1 + signal-1 = 25+8 = 33 bars
    const out = macd(LONG_BARS, 12, 26, 9);
    for (let i = 0; i < 33; i++) {
      assert.equal(out[i].signal, null, `bar ${i} signal should be null`);
    }
    // bar 33 should be the first with signal
    assert.ok(out[33].signal !== null, 'bar 33 should have signal');
  });

  test('histogram = macd - signal where both defined', () => {
    const out = macd(LONG_BARS, 12, 26, 9);
    for (const p of out) {
      if (p.macd == null || p.signal == null) {
        assert.equal(p.histogram, null);
      } else {
        assert.ok(
          Math.abs(p.histogram - (p.macd - p.signal)) < 1e-9,
          `histogram mismatch at ${p.ts}`,
        );
      }
    }
  });

  test('throws when fast >= slow', () => {
    assert.throws(() => macd(LONG_BARS, 26, 12), RangeError);
    assert.throws(() => macd(LONG_BARS, 14, 14), RangeError);
  });

  test('throws for n=0', () => {
    assert.throws(() => macd(LONG_BARS, 0, 26, 9), RangeError);
  });

  test('throws for negative signal', () => {
    assert.throws(() => macd(LONG_BARS, 12, 26, -1), RangeError);
  });
});
