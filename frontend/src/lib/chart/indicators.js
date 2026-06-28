/**
 * indicators.js — Pure stateless indicator functions for ChartWorkspace.
 *
 * Each function accepts an OHLCV bars array
 * (objects with at least {close, open, high, low, volume, ts} fields)
 * and returns a series array of {ts, value} objects.
 *
 * Conventions:
 *   - The first (n-1) entries that cannot be computed return null for `value`.
 *   - N <= 0 throws a RangeError.
 *   - If bars has fewer entries than needed to produce even one value, the
 *     function returns an array of the same length with all-null values.
 *
 * These functions are pure (no side-effects, no imports) so they can be
 * used in unit tests without a DOM or Svelte runtime.
 */

// ── Validation helper ─────────────────────────────────────────────────────────
function _assertN(n) {
  if (typeof n !== 'number' || !Number.isInteger(n) || n <= 0) {
    throw new RangeError(`Indicator period must be a positive integer, got: ${n}`);
  }
}

// ── SMA (Simple Moving Average) ───────────────────────────────────────────────
/**
 * SMA over `n` periods using close prices.
 * @param {Array<{ts:string,close:number|string}>} bars
 * @param {number} n
 * @returns {Array<{ts:string,value:number|null}>}
 */
export function sma(bars, n) {
  _assertN(n);
  return bars.map((b, i) => {
    if (i < n - 1) return { ts: b.ts, value: null };
    let sum = 0;
    for (let j = i - n + 1; j <= i; j++) sum += Number(bars[j].close);
    return { ts: b.ts, value: sum / n };
  });
}

// ── EMA (Exponential Moving Average) ─────────────────────────────────────────
/**
 * EMA over `n` periods.  Seed = SMA of the first n bars.
 * k = 2/(n+1)  (Wilder multiplier; same as TradingView default).
 * @param {Array<{ts:string,close:number|string}>} bars
 * @param {number} n
 * @returns {Array<{ts:string,value:number|null}>}
 */
export function ema(bars, n) {
  _assertN(n);
  const out = [];
  let emaVal = 0;
  const k = 2 / (n + 1);
  for (let i = 0; i < bars.length; i++) {
    if (i < n - 1) {
      out.push({ ts: bars[i].ts, value: null });
      emaVal += Number(bars[i].close);
      continue;
    }
    if (i === n - 1) {
      // Seed: SMA of first n bars
      emaVal = (emaVal + Number(bars[i].close)) / n;
    } else {
      emaVal = Number(bars[i].close) * k + emaVal * (1 - k);
    }
    out.push({ ts: bars[i].ts, value: emaVal });
  }
  return out;
}

// ── VWAP (Volume-Weighted Average Price) ──────────────────────────────────────
/**
 * VWAP — cumulative (TP×V) / cumulative V from bar[0] onward.
 * Typical Price = (high + low + close) / 3.
 * Returns a value for every bar (no warmup period needed).
 * @param {Array<{ts:string,high:number|string,low:number|string,close:number|string,volume:number|string}>} bars
 * @returns {Array<{ts:string,value:number|null}>}
 */
export function vwap(bars) {
  const out = [];
  let cumTPV = 0;
  let cumVol = 0;
  for (const b of bars) {
    const tp  = (Number(b.high) + Number(b.low) + Number(b.close)) / 3;
    const vol = Number(b.volume || 0);
    cumTPV += tp * vol;
    cumVol += vol;
    out.push({ ts: b.ts, value: cumVol > 0 ? cumTPV / cumVol : null });
  }
  return out;
}

// ── Bollinger Bands ───────────────────────────────────────────────────────────
/**
 * Bollinger Bands: middle = SMA(n), upper = mid + k×σ, lower = mid - k×σ.
 * Uses population std dev (σ = sqrt(Σ(x-μ)²/N)) — TradingView standard.
 * @param {Array<{ts:string,close:number|string}>} bars
 * @param {number} n  period (default 20)
 * @param {number} k  multiplier (default 2)
 * @returns {Array<{ts:string,mid:number|null,upper:number|null,lower:number|null}>}
 */
export function bollinger(bars, n = 20, k = 2) {
  _assertN(n);
  return bars.map((b, i) => {
    if (i < n - 1) return { ts: b.ts, mid: null, upper: null, lower: null };
    let sum = 0;
    for (let j = i - n + 1; j <= i; j++) sum += Number(bars[j].close);
    const mid = sum / n;
    let variance = 0;
    for (let j = i - n + 1; j <= i; j++) {
      const d = Number(bars[j].close) - mid;
      variance += d * d;
    }
    const sd = Math.sqrt(variance / n);
    return { ts: b.ts, mid, upper: mid + k * sd, lower: mid - k * sd };
  });
}

// ── RSI (Wilder's Smoothed RSI) ───────────────────────────────────────────────
/**
 * RSI with Wilder's smoothing.
 * Seed: SMA of the first n gains and losses.
 * Smoothed: avgGain = (prevAvgGain × (n-1) + gain) / n.
 * Returns null for the first n bars (no close delta at bar[0]).
 * @param {Array<{ts:string,close:number|string}>} bars
 * @param {number} n  period (default 14)
 * @returns {Array<{ts:string,value:number|null}>}
 */
export function rsi(bars, n = 14) {
  _assertN(n);
  const out = [];
  // bar[0] has no prior close — always null
  out.push({ ts: bars[0]?.ts ?? '', value: null });

  let avgGain = 0;
  let avgLoss = 0;

  for (let i = 1; i < bars.length; i++) {
    const ch = Number(bars[i].close) - Number(bars[i - 1].close);
    const g  = ch > 0 ? ch : 0;
    const l  = ch < 0 ? -ch : 0;

    if (i < n) {
      // Accumulate for seed
      avgGain += g;
      avgLoss += l;
      out.push({ ts: bars[i].ts, value: null });
      continue;
    }
    if (i === n) {
      // Seed
      avgGain = (avgGain + g) / n;
      avgLoss = (avgLoss + l) / n;
    } else {
      // Wilder smoothing
      avgGain = (avgGain * (n - 1) + g) / n;
      avgLoss = (avgLoss * (n - 1) + l) / n;
    }
    const rs  = avgLoss === 0 ? 100 : avgGain / avgLoss;
    const val = 100 - 100 / (1 + rs);
    out.push({ ts: bars[i].ts, value: val });
  }
  return out;
}

// ── MACD ──────────────────────────────────────────────────────────────────────
/**
 * MACD: {macd, signal, histogram} per bar.
 *   macd      = EMA(fast) − EMA(slow)
 *   signal    = EMA(macd, signalN)
 *   histogram = macd − signal
 *
 * Returns null for bars where either EMA hasn't warmed up yet.
 * Defaults: fast=12, slow=26, signal=9 (standard).
 * @param {Array<{ts:string,close:number|string}>} bars
 * @param {number} fast   (default 12)
 * @param {number} slow   (default 26)
 * @param {number} signal (default 9)
 * @returns {Array<{ts:string,macd:number|null,signal:number|null,histogram:number|null}>}
 */
export function macd(bars, fast = 12, slow = 26, signal = 9) {
  _assertN(fast);
  _assertN(slow);
  _assertN(signal);
  if (fast >= slow) throw new RangeError('MACD fast period must be less than slow period');

  // Compute the two EMAs
  const fastEma = ema(bars, fast);
  const slowEma = ema(bars, slow);

  // MACD line: only defined when slow EMA is defined (i >= slow-1)
  /** @type {Array<number|null>} */
  const macdLine = fastEma.map((f, i) => {
    const s = slowEma[i];
    if (f.value == null || s.value == null) return null;
    return f.value - s.value;
  });

  // Signal line: EMA(signal) of the MACD line
  // Build a synthetic bars array from macdLine (skipping nulls for seed)
  /** @type {Array<number|null>} */
  const signalLine = new Array(bars.length).fill(null);
  // Find first non-null MACD index
  const firstMacd = macdLine.findIndex((v) => v !== null);
  if (firstMacd >= 0) {
    const kSig = 2 / (signal + 1);
    let sigEma = 0;
    let sigCount = 0;
    for (let i = firstMacd; i < bars.length; i++) {
      if (macdLine[i] == null) continue;
      sigCount++;
      if (sigCount < signal) {
        sigEma += /** @type {number} */ (macdLine[i]);
        continue;
      }
      if (sigCount === signal) {
        sigEma = (sigEma + /** @type {number} */ (macdLine[i])) / signal;
      } else {
        sigEma = /** @type {number} */ (macdLine[i]) * kSig + sigEma * (1 - kSig);
      }
      signalLine[i] = sigEma;
    }
  }

  return bars.map((b, i) => {
    const m = macdLine[i];
    const s = signalLine[i];
    const h = m != null && s != null ? m - s : null;
    return { ts: b.ts, macd: m, signal: s, histogram: h };
  });
}
