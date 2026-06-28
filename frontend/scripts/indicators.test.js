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
import {
  sma, ema, vwap, bollinger, rsi, macd,
  emaSignals, vwapSignals, bollingerSignals, rsiSignals, macdSignals,
} from '../src/lib/chart/indicators.js';

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

// ── Signal detection — emaSignals ───────────────────────────────────────────
describe('emaSignals', () => {
  test('detects golden cross when fast rises above slow', () => {
    // Fixture: fast climbs while slow stays flat — exactly one buy event.
    const slow = [10, 10, 10, 10, 10];
    const fast = [ 8,  9, 10, 11, 12];
    // i=2 fast==slow (not strictly above), i=3 fast(11) > slow(10) → buy
    const sigs = emaSignals(fast, slow);
    assert.equal(sigs.length, 1, `expected exactly one signal, got ${sigs.length}`);
    assert.equal(sigs[0].i, 3);
    assert.equal(sigs[0].type, 'buy');
  });

  test('detects death cross when fast falls below slow', () => {
    const slow = [10, 10, 10, 10, 10];
    const fast = [12, 11, 10,  9,  8];
    // i=3 fast(9) < slow(10) → sell
    const sigs = emaSignals(fast, slow);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].i, 3);
    assert.equal(sigs[0].type, 'sell');
  });

  test('accepts {value} shape (real ema output)', () => {
    const slow = [{value:10},{value:10},{value:10}];
    const fast = [{value: 9},{value:10},{value:12}];
    const sigs = emaSignals(fast, slow);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'buy');
  });

  test('skips bars with null indicators', () => {
    const slow = [null, 10, 10, 10];
    const fast = [null,  9, 11, 12];
    // No cross detected at i=1 (prev=null) — first valid pair is (i=1,i=2).
    const sigs = emaSignals(fast, slow);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].i, 2);
  });
});

// ── Signal detection — vwapSignals ──────────────────────────────────────────
describe('vwapSignals', () => {
  test('buy when close crosses ABOVE vwap from below', () => {
    const closes = [100, 101, 103];
    const vwap_  = [102, 102, 102];
    // i=2 close(103) > vwap(102), prev close(101) <= vwap(102) → buy
    const sigs = vwapSignals(closes, vwap_);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'buy');
    assert.equal(sigs[0].i, 2);
  });

  test('sell when close crosses BELOW vwap from above', () => {
    const closes = [104, 103, 101];
    const vwap_  = [102, 102, 102];
    const sigs = vwapSignals(closes, vwap_);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'sell');
  });

  test('accepts {close} bar shape', () => {
    const bars = [{close:100},{close:101},{close:103}];
    const v    = [{value:102},{value:102},{value:102}];
    const sigs = vwapSignals(bars, v);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'buy');
  });
});

// ── Signal detection — bollingerSignals ─────────────────────────────────────
describe('bollingerSignals', () => {
  test('buy when close pierces lower band', () => {
    const closes = [100, 100, 95];
    const bb = [
      { upper: 105, lower: 95 },
      { upper: 105, lower: 95 },
      { upper: 105, lower: 95 },
    ];
    const sigs = bollingerSignals(closes, bb);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'buy');
    assert.equal(sigs[0].i, 2);
  });

  test('sell when close pierces upper band', () => {
    const closes = [100, 100, 106];
    const bb = [
      { upper: 105, lower: 95 },
      { upper: 105, lower: 95 },
      { upper: 105, lower: 95 },
    ];
    const sigs = bollingerSignals(closes, bb);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'sell');
  });

  test('multi-bar lower break only fires once (throttled)', () => {
    const closes = [100, 95, 94, 93, 100];
    const bb = Array.from({length: 5}, () => ({ upper: 105, lower: 95 }));
    const sigs = bollingerSignals(closes, bb);
    // Single buy at i=1 — multiple consecutive bars below lower don't stack.
    const buys = sigs.filter(s => s.type === 'buy');
    assert.equal(buys.length, 1);
    assert.equal(buys[0].i, 1);
  });

  test('skips bars with null bands', () => {
    const closes = [100, 95];
    const bb = [{ upper: null, lower: null }, { upper: 105, lower: 95 }];
    const sigs = bollingerSignals(closes, bb);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].i, 1);
    assert.equal(sigs[0].type, 'buy');
  });
});

// ── Signal detection — rsiSignals ───────────────────────────────────────────
describe('rsiSignals', () => {
  test('buy on cross above 30 from oversold', () => {
    // Sequence 25, 28, 32, 35 — buy when 28 → 32 crosses 30
    const arr = [25, 28, 32, 35];
    const sigs = rsiSignals(arr);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'buy');
    assert.equal(sigs[0].i, 2);
  });

  test('sell on cross below 70 from overbought', () => {
    const arr = [75, 72, 68, 65];
    const sigs = rsiSignals(arr);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'sell');
    assert.equal(sigs[0].i, 2);
  });

  test('accepts {value} shape', () => {
    const arr = [{value:25},{value:28},{value:32}];
    const sigs = rsiSignals(arr);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'buy');
  });

  test('no signal when RSI stays in middle zone', () => {
    const arr = [45, 50, 55, 60, 65];
    const sigs = rsiSignals(arr);
    assert.equal(sigs.length, 0);
  });

  test('custom thresholds (20/80) shift fire points', () => {
    const arr = [15, 18, 22, 25];
    const sigsDefault = rsiSignals(arr);  // crosses 30 — buy at i=2 (22>30? no — none)
    const sigsCustom  = rsiSignals(arr, 20, 80);
    assert.equal(sigsDefault.length, 0);
    assert.equal(sigsCustom.length, 1);
    assert.equal(sigsCustom[0].type, 'buy');
  });
});

// ── Signal detection — macdSignals ──────────────────────────────────────────
describe('macdSignals', () => {
  test('buy when MACD line crosses ABOVE signal line', () => {
    const macdLine   = [-1,  0,  1];
    const signalLine = [ 0,  0,  0];
    // i=2: prev(0)<=prev_sig(0), cur(1)>cur_sig(0) → buy
    const sigs = macdSignals(macdLine, signalLine);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'buy');
    assert.equal(sigs[0].i, 2);
  });

  test('sell when MACD line crosses BELOW signal line', () => {
    const macdLine   = [ 1,  0, -1];
    const signalLine = [ 0,  0,  0];
    const sigs = macdSignals(macdLine, signalLine);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'sell');
  });

  test('accepts macd() output shape ({macd,signal})', () => {
    // The macd() function returns an array of {ts,macd,signal,histogram}
    const series = [
      { ts: 'a', macd: -1, signal: 0, histogram: -1 },
      { ts: 'b', macd:  0, signal: 0, histogram:  0 },
      { ts: 'c', macd:  1, signal: 0, histogram:  1 },
    ];
    const sigs = macdSignals(series, series);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].type, 'buy');
  });

  test('skips bars with null lines', () => {
    const macdLine   = [null, 0, 1];
    const signalLine = [null, 0, 0];
    const sigs = macdSignals(macdLine, signalLine);
    assert.equal(sigs.length, 1);
    assert.equal(sigs[0].i, 2);
  });
});
