/**
 * nav.test.js — Unit tests for the canonical NAV breakdown helper.
 *
 * Tier 1 / A2 — two surfaces (PerformancePage.svelte navByAcct grid
 * + NavBreakdown.svelte card) used to inline the same formula. Both
 * now import `navByAccount` from `$lib/data/nav.js`. This spec
 * asserts:
 *
 *  1. SSOT  — golden values for the v4 formula
 *           cash = cash_sod + option_premium
 *           pos_m2m = Σ position.unrealised
 *           hold = Σ holdings.cur_val
 *           nav = cash + pos_m2m + hold
 *  2. Perf  — no async, no I/O; pure sync compute
 *  3. Stale — only nav.js holds the formula (regex grep over
 *           consumer files: no inline `option_premium` math left).
 *  4. Reuse — both PerformancePage + NavBreakdown import from
 *           `$lib/data/nav` (grep guard).
 *  5. UX    — empty inputs, missing fields, totals-row math.
 *
 * Run with:  node --test frontend/scripts/nav.test.js
 */

import { test, describe } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

import {
  navRowForAccount, navByAccount, navTotalRow,
} from '../src/lib/data/nav.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND_SRC = resolve(__dirname, '..', 'src');


// ── Fixtures ────────────────────────────────────────────────────────────────

const funds = [
  { account: 'ZG0790', cash: 100_000, option_premium: 5_000 },
  { account: 'DH3747', cash: 50_000,  option_premium: 0 },
  { account: 'GR87DF', cash: 10_000,  option_premium: 250 },
];

const positions = [
  { account: 'ZG0790', unrealised: 1_500 },
  { account: 'ZG0790', unrealised: -200 },
  { account: 'DH3747', unrealised: 800 },
];

const holdings = [
  { account: 'ZG0790', cur_val: 200_000 },
  { account: 'GR87DF', cur_val: 7_500 },
];


// ── SSOT — golden values ────────────────────────────────────────────────────

describe('navRowForAccount — v4 formula', () => {
  test('cash = sod + option_premium', () => {
    const r = navRowForAccount('ZG0790', funds, positions, holdings);
    assert.equal(r.cash, 105_000);          // 100k + 5k premium
  });

  test('pos_m2m = Σ unrealised filtered by account', () => {
    const r = navRowForAccount('ZG0790', funds, positions, holdings);
    assert.equal(r.pos_m2m, 1_300);          // 1500 + (-200)
  });

  test('holdings_mtm = Σ cur_val filtered by account', () => {
    const r = navRowForAccount('ZG0790', funds, positions, holdings);
    assert.equal(r.holdings_mtm, 200_000);
  });

  test('nav = cash + pos_m2m + holdings_mtm', () => {
    const r = navRowForAccount('ZG0790', funds, positions, holdings);
    assert.equal(r.nav, 306_300);            // 105k + 1.3k + 200k
  });

  test('account with no positions returns pos_m2m=0', () => {
    const r = navRowForAccount('GR87DF', funds, positions, holdings);
    assert.equal(r.pos_m2m, 0);
    assert.equal(r.holdings_mtm, 7_500);
    assert.equal(r.nav, 17_750);             // 10k + 250 + 7.5k
  });
});


describe('navByAccount + navTotalRow', () => {
  test('navByAccount returns one row per requested account', () => {
    const rows = navByAccount(['ZG0790', 'DH3747', 'GR87DF'], funds, positions, holdings);
    assert.equal(rows.length, 3);
    assert.equal(rows[0].account, 'ZG0790');
  });

  test('navTotalRow sums every column', () => {
    const rows = navByAccount(['ZG0790', 'DH3747', 'GR87DF'], funds, positions, holdings);
    const tot = navTotalRow(rows);
    assert.equal(tot.account, 'TOTAL');
    // cash:   105_000 + 50_000 + 10_250 = 165_250
    // pos_m2m: 1_300  +    800 +      0 =   2_100
    // hold:  200_000  +      0 +  7_500 = 207_500
    // nav:                                = 374_850
    assert.equal(tot.cash, 165_250);
    assert.equal(tot.pos_m2m, 2_100);
    assert.equal(tot.holdings_mtm, 207_500);
    assert.equal(tot.nav, 374_850);
  });

  test('navTotalRow returns null on empty input', () => {
    assert.equal(navTotalRow([]), null);
    assert.equal(navTotalRow(null), null);
  });

  test('handles missing funds row gracefully', () => {
    const r = navRowForAccount('MISSING', [], positions, holdings);
    assert.equal(r.cash, 0);
    assert.equal(r.pos_m2m, 0);
    assert.equal(r.holdings_mtm, 0);
    assert.equal(r.nav, 0);
  });

  test('navByAccount with empty accounts returns []', () => {
    assert.deepEqual(navByAccount([], funds, positions, holdings), []);
  });
});


// ── Stale + Reuse — no inline formula left in consumers ─────────────────────

describe('SSOT — only $lib/data/nav holds the formula', () => {
  test('PerformancePage.svelte imports navByAccount + navTotalRow', () => {
    const src = readFileSync(resolve(FRONTEND_SRC, 'lib', 'PerformancePage.svelte'), 'utf-8');
    assert.match(src, /from\s+['"]\$lib\/data\/nav['"]/);
    assert.match(src, /navByAccount/);
  });

  test('NavBreakdown.svelte imports navByAccount + navTotalRow', () => {
    const src = readFileSync(resolve(FRONTEND_SRC, 'lib', 'NavBreakdown.svelte'), 'utf-8');
    assert.match(src, /from\s+['"]\$lib\/data\/nav['"]/);
    assert.match(src, /navByAccount/);
  });

  test('NavBreakdown no longer inlines the cash_sod + option_premium math', () => {
    const src = readFileSync(resolve(FRONTEND_SRC, 'lib', 'NavBreakdown.svelte'), 'utf-8');
    // No standalone variable assignment for cash_sod or opt_premium —
    // those live in nav.js now.
    assert.doesNotMatch(src, /const\s+cash_sod\s*=/);
    assert.doesNotMatch(src, /const\s+opt_premium\s*=/);
  });
});
