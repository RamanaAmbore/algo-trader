// Port of backend/api/algo/derivatives.py — Black-Scholes risk-neutral helpers
//   normCdf  — Abramowitz & Stegun 7.1.26 approximation
//   probAbove(S, K, T, σ) — P(S_T ≥ K) under risk-neutral lognormal
//   expectedValueOnCurve — trapezoid ∫ expiry_value × pdf
//   multilegPopOnCurve — sum over contiguous profit segments

export const RISK_FREE_R = 0.07; // matches backend default

export function normCdf(x) {
  const t = 1 / (1 + 0.2316419 * Math.abs(x));
  const d = 0.3989422804 * Math.exp(-x * x / 2);
  let p = d * t * (0.31938153
    + t * (-0.356563782
    + t * (1.781477937
    + t * (-1.821255978
    + t * 1.330274429))));
  return x > 0 ? 1 - p : p;
}

export function probAbove(S, K, T, sigma, r = RISK_FREE_R) {
  if (K <= 0 || S <= 0 || T <= 0 || sigma <= 0) return 0;
  const d2 = (Math.log(S / K) + (r - sigma * sigma / 2) * T)
           / (sigma * Math.sqrt(T));
  return normCdf(d2);
}

export function expectedValueOnCurve(curve, S, T, sigma, r = RISK_FREE_R) {
  if (!curve || curve.length < 2 || S <= 0 || T <= 0 || sigma <= 0) return 0;
  function pdf(ST) {
    if (ST <= 0) return 0;
    const lnRatio = Math.log(ST / S);
    const denom   = ST * sigma * Math.sqrt(2 * Math.PI * T);
    const expArg  = -Math.pow(
      lnRatio - (r - sigma * sigma / 2) * T, 2
    ) / (2 * sigma * sigma * T);
    return Math.exp(expArg) / denom;
  }
  let ev = 0;
  for (let i = 1; i < curve.length; i++) {
    const a = curve[i - 1], b = curve[i];
    const dx = b.spot - a.spot;
    ev += 0.5 * dx * (a.expiry_value * pdf(a.spot) + b.expiry_value * pdf(b.spot));
  }
  return ev;
}

export function multilegPopOnCurve(curve, S, T, sigma, r = RISK_FREE_R) {
  if (!curve || curve.length < 2 || S <= 0 || T <= 0 || sigma <= 0) return null;
  let pop = 0;
  let segStart = null;
  let segStartIdx = -1;
  for (let i = 0; i < curve.length; i++) {
    const v = curve[i].expiry_value;
    if (v > 0 && segStart === null) {
      segStart = curve[i].spot;
      segStartIdx = i;
    } else if (v <= 0 && segStart !== null) {
      // Segment ended at curve[i-1]. Lower bound = 1.0 when the
      // segment touches the curve's left edge (open-ended downward).
      const lower = segStartIdx === 0 ? 1.0 : probAbove(S, segStart, T, sigma, r);
      const upper = probAbove(S, curve[i - 1].spot, T, sigma, r);
      pop += lower - upper;
      segStart = null;
      segStartIdx = -1;
    }
  }
  if (segStart !== null) {
    // Open-ended upward segment touching the curve's right edge —
    // upper bound = 0 since P(S_T → ∞) = 0.
    const lower = segStartIdx === 0 ? 1.0 : probAbove(S, segStart, T, sigma, r);
    pop += lower;
  }
  return Math.max(0, Math.min(1, pop));
}
