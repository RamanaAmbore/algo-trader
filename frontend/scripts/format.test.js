/**
 * format.test.js — Unit tests for percentage formatter consolidation.
 *
 * Tier 2 / A6 — three pages used to ship their own `fmtPct`:
 *   - lib/PnlAnalysis.svelte (no scaling)
 *   - routes/(algo)/admin/derivatives/+page.svelte (× 100)
 *   - routes/(algo)/automation/templates/+page.svelte (toFixed + sign)
 *
 * Asserts:
 *  1. SSOT  — fmtPctScaled + fmtPctFraction exported from format.js
 *  2. Each variant produces the exact rendering its source page used
 *     to produce (golden-value reference tests).
 *  3. Stale — no other file in src/ still inlines `function fmtPct`
 *     (regex grep guard).
 *
 * Run with:  node --test frontend/scripts/format.test.js
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, resolve, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { fmtPctScaled, fmtPctFraction } from '../src/lib/format.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = resolve(__dirname, '..', 'src');


describe('fmtPctScaled — input already in %', () => {
  test('two-decimal default rule for |v| < 100', () => {
    assert.equal(fmtPctScaled(5.0), '5.00%');
  });

  test('respects custom decimals', () => {
    assert.equal(fmtPctScaled(5.0, 1), '5.0%');
    assert.equal(fmtPctScaled(5.0, 0), '5%');
  });

  test('signed=true prepends + for positive and zero', () => {
    assert.equal(fmtPctScaled(5.0, 1, true), '+5.0%');
    assert.equal(fmtPctScaled(0.0, 1, true), '+0.0%');
    assert.equal(fmtPctScaled(-5.0, 1, true), '-5.0%');
  });

  test('returns — on null / NaN / Infinity', () => {
    assert.equal(fmtPctScaled(null), '—');
    assert.equal(fmtPctScaled(NaN), '—');
    assert.equal(fmtPctScaled(Infinity), '—');
  });

  test('default-rule >= 100 collapses to 0 decimals', () => {
    // pctFmt's |v| ≥ 100 → 0-decimal rule
    assert.equal(fmtPctScaled(150), '150%');
  });
});


describe('fmtPctFraction — input is a fraction', () => {
  test('0.05 fraction → 5%', () => {
    assert.equal(fmtPctFraction(0.05), '5.00%');
    assert.equal(fmtPctFraction(0.05, 1), '5.0%');
  });

  test('negative fraction preserves sign', () => {
    assert.equal(fmtPctFraction(-0.03, 1), '-3.0%');
  });

  test('null and NaN propagate to —', () => {
    assert.equal(fmtPctFraction(null), '—');
    assert.equal(fmtPctFraction(NaN), '—');
  });
});


describe('Legacy parity — each page now produces the same output', () => {
  test('PnlAnalysis legacy: pctFmt(v) + "%" matches fmtPctScaled(v)', () => {
    // pctFmt is _decFmt, which is 2dp for |v|<100, 0dp for ≥100.
    // fmtPctScaled(v) uses the same rule.
    assert.equal(fmtPctScaled(2.5), '2.50%');     // pctFmt(2.5) = "2.50", +"%"
    assert.equal(fmtPctScaled(125), '125%');      // pctFmt(125) = "125",  +"%"
  });

  test('derivatives legacy: pctFmt(v*100) + "%" matches fmtPctFraction(v)', () => {
    assert.equal(fmtPctFraction(0.025), '2.50%');
    assert.equal(fmtPctFraction(1.5),   '150%');   // ≥100 → 0dp
  });

  test('templates legacy: toFixed(1) + sign matches fmtPctScaled(v, 1, true)', () => {
    assert.equal(fmtPctScaled(1.5, 1, true),  '+1.5%');
    assert.equal(fmtPctScaled(-2.0, 1, true), '-2.0%');
    assert.equal(fmtPctScaled(0, 1, true),    '+0.0%');
  });
});


// ── Stale — no inline `function fmtPct(` left in src/ ───────────────────────

function walk(dir, acc = []) {
  for (const entry of readdirSync(dir)) {
    const p = join(dir, entry);
    const st = statSync(p);
    if (st.isDirectory()) walk(p, acc);
    else if (
      p.endsWith('.svelte') || p.endsWith('.js') || p.endsWith('.ts')
    ) acc.push(p);
  }
  return acc;
}

describe('SSOT — no inline fmtPct definitions remain in src/', () => {
  test('only the format module declares fmtPctScaled/Fraction', () => {
    const offenders = [];
    for (const p of walk(FRONTEND_SRC)) {
      if (p.endsWith('lib/format.js')) continue;
      if (p.includes('node_modules')) continue;
      const src = readFileSync(p, 'utf-8');
      // Match `function fmtPct(` and `const fmtPct = function`
      // and arrow-form `const fmtPct = (` — three legacy shapes.
      // `const fmtPct = fmtPctScaled` (alias) is allowed.
      const inlineFn = /function\s+fmtPct\s*\(/.test(src);
      const constArrow = /const\s+fmtPct\s*=\s*\(/.test(src);
      const constFnExpr = /const\s+fmtPct\s*=\s*function/.test(src);
      if (inlineFn || constArrow || constFnExpr) {
        // Allow the consumer-side `function fmtPct(v) { return fmtPctScaled(...); }`
        // single-line passthrough (matches the templates page pattern).
        const passthrough = /function\s+fmtPct\s*\([^)]*\)\s*\{\s*return\s+fmtPct(Scaled|Fraction)/.test(src);
        if (!passthrough) offenders.push(p);
      }
    }
    assert.deepEqual(offenders, [], (
      `Inline fmtPct definitions still present. ` +
      `Each should call fmtPctScaled or fmtPctFraction from $lib/format. ` +
      `Offenders: ${JSON.stringify(offenders, null, 2)}`
    ));
  });
});
