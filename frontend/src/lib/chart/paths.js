// Pure chart indicator computations — extracted from ChartWorkspace.svelte (Phase 1 refactor).
// All functions take explicit deps as args (no Svelte reactive state captured).
// Callers are responsible for show-flag guards; length guards live here alongside
// the computation since they depend only on `bars`.

import { vwap as calcVwap, macd as calcMacd } from '$lib/chart/indicators.js';

/**
 * Compute an SVG path string for a Simple Moving Average.
 * @param {Array<{ts:string,close:string|number}>} bars
 * @param {number} window - period length
 * @param {(t:number)=>number} xOf
 * @param {(v:number)=>number} yOf
 * @returns {string}
 */
export function smaPath(bars, window, xOf, yOf) {
  if (!bars.length || bars.length < window) return '';
  let d = '';
  for (let i = window - 1; i < bars.length; i++) {
    let sum = 0;
    for (let j = i - window + 1; j <= i; j++) sum += Number(bars[j].close);
    const avg = sum / window;
    const t   = Date.parse(bars[i].ts);
    if (!Number.isFinite(t)) continue;
    const x = xOf(t), y = yOf(avg);
    d += (d === '' ? `M${x.toFixed(2)},${y.toFixed(2)}` : ` L${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return d;
}

/**
 * Compute an SVG path string for an Exponential Moving Average.
 * Classic EMA: EMA_t = close_t × k + EMA_{t-1} × (1−k), k = 2/(N+1).
 * Seed is the SMA of the first N bars. Returns '' when not enough bars.
 * @param {Array<{ts:string,close:string|number}>} bars
 * @param {number} n - period length
 * @param {(t:number)=>number} xOf
 * @param {(v:number)=>number} yOf
 * @returns {string}
 */
export function emaPath(bars, n, xOf, yOf) {
  if (bars.length < n) return '';
  const k = 2 / (n + 1);
  let ema = bars.slice(0, n).reduce((s, b) => s + Number(b.close), 0) / n;
  let d = '';
  for (let i = n - 1; i < bars.length; i++) {
    if (i > n - 1) ema = Number(bars[i].close) * k + ema * (1 - k);
    const t = Date.parse(bars[i].ts);
    if (!Number.isFinite(t)) continue;
    const x = xOf(t), y = yOf(ema);
    d += (d ? ` L${x.toFixed(2)},${y.toFixed(2)}` : `M${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return d;
}

/**
 * Compute an SVG path string for VWAP (cumulative, from bar[0] to current).
 * Returns '' when bars is empty. Skips points where calcVwap returns null.
 * @param {Array<{ts:string,high:string|number,low:string|number,close:string|number,volume:string|number}>} bars
 * @param {(t:number)=>number} xOf
 * @param {(v:number)=>number} yOf
 * @returns {string}
 */
export function vwapPath(bars, xOf, yOf) {
  if (!bars.length) return '';
  const series = calcVwap(bars);
  let d = '';
  for (const pt of series) {
    if (pt.value == null) continue;
    const t = Date.parse(pt.ts);
    if (!Number.isFinite(t)) continue;
    const x = xOf(t), y = yOf(pt.value);
    d += (d ? ` L${x.toFixed(2)},${y.toFixed(2)}` : `M${x.toFixed(2)},${y.toFixed(2)}`);
  }
  return d;
}

/**
 * Compute Bollinger Bands SVG path strings (20-period, ±2σ).
 * Returns { mid, upper, lower, fill } path strings.
 * Returns empty strings for all when bars.length < 20.
 * @param {Array<{ts:string,close:string|number}>} bars
 * @param {(t:number)=>number} xOf
 * @param {(v:number)=>number} yOf
 * @returns {{ mid: string, upper: string, lower: string, fill: string }}
 */
export function bbPaths(bars, xOf, yOf) {
  if (bars.length < 20) return { mid: '', upper: '', lower: '', fill: '' };
  const N = 20, K = 2;
  let mid = '', upper = '', lower = '';
  /** @type {Array<{x:number,yU:number,yL:number}>} */
  const ribbon = [];
  for (let i = N - 1; i < bars.length; i++) {
    let sum = 0;
    for (let j = i - N + 1; j <= i; j++) sum += Number(bars[j].close);
    const m = sum / N;
    let v = 0;
    for (let j = i - N + 1; j <= i; j++) {
      const diff = Number(bars[j].close) - m;
      v += diff * diff;
    }
    const sd = Math.sqrt(v / N);
    const u = m + K * sd, l = m - K * sd;
    const t = Date.parse(bars[i].ts);
    if (!Number.isFinite(t)) continue;
    const x = xOf(t);
    mid   += (mid   ? ` L${x.toFixed(2)},${yOf(m).toFixed(2)}` : `M${x.toFixed(2)},${yOf(m).toFixed(2)}`);
    upper += (upper ? ` L${x.toFixed(2)},${yOf(u).toFixed(2)}` : `M${x.toFixed(2)},${yOf(u).toFixed(2)}`);
    lower += (lower ? ` L${x.toFixed(2)},${yOf(l).toFixed(2)}` : `M${x.toFixed(2)},${yOf(l).toFixed(2)}`);
    ribbon.push({ x, yU: yOf(u), yL: yOf(l) });
  }
  // Shaded fill — upper line forward, lower reversed, closed.
  let fill = '';
  if (ribbon.length) {
    fill = `M${ribbon[0].x.toFixed(2)},${ribbon[0].yU.toFixed(2)}`;
    for (let i = 1; i < ribbon.length; i++) fill += ` L${ribbon[i].x.toFixed(2)},${ribbon[i].yU.toFixed(2)}`;
    for (let i = ribbon.length - 1; i >= 0; i--) fill += ` L${ribbon[i].x.toFixed(2)},${ribbon[i].yL.toFixed(2)}`;
    fill += ' Z';
  }
  return { mid, upper, lower, fill };
}

/**
 * Compute RSI series using Wilder's smoothed RSI.
 * Returns a series of {ts, rsi} points for sub-panel rendering.
 * The sub-panel has its own y-scale 0–100 (independent of price).
 * Returns [] when bars.length < n + 1.
 * @param {Array<{ts:string,close:string|number}>} bars
 * @param {number} [n=14] - RSI period
 * @returns {Array<{ts:string,rsi:number}>}
 */
export function rsiSeries(bars, n = 14) {
  if (bars.length < n + 1) return /** @type {Array<{ts:string,rsi:number}>} */ ([]);
  /** @type {Array<{ts:string,rsi:number}>} */
  const out = [];
  let avgGain = 0, avgLoss = 0;
  // Seed: first n changes
  for (let i = 1; i <= n; i++) {
    const ch = Number(bars[i].close) - Number(bars[i - 1].close);
    if (ch >= 0) avgGain += ch; else avgLoss -= ch;
  }
  avgGain /= n; avgLoss /= n;
  for (let i = n; i < bars.length; i++) {
    if (i > n) {
      const ch = Number(bars[i].close) - Number(bars[i - 1].close);
      const g = ch > 0 ? ch : 0;
      const l = ch < 0 ? -ch : 0;
      avgGain = (avgGain * (n - 1) + g) / n;
      avgLoss = (avgLoss * (n - 1) + l) / n;
    }
    const rs  = avgLoss === 0 ? 100 : avgGain / avgLoss;
    const rsi = 100 - (100 / (1 + rs));
    out.push({ ts: bars[i].ts, rsi });
  }
  return out;
}

/**
 * Compute MACD series (12/26/9) using calcMacd from indicators.js.
 * Returns [] when bars.length < 27 (minimum for signal to appear).
 * @param {Array<{ts:string,close:string|number}>} bars
 * @returns {Array<{ts:string,macd:number|null,signal:number|null,histogram:number|null}>}
 */
export function macdSeries(bars) {
  if (bars.length < 27) return /** @type {Array<{ts:string,macd:number|null,signal:number|null,histogram:number|null}>} */ ([]);
  return calcMacd(bars, 12, 26, 9);
}
