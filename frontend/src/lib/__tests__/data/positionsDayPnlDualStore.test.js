/**
 * positionsDayPnlDualStore.test.js
 *
 * Unit tests for `mergePositionStores` — the pure helper extracted from
 * `positionsDayPnlStore.svelte.js` to merge `positionsStore.value` (cross-page
 * 5s poller) with `pulsePositionsStore.value` (Pulse-isolated poller).
 *
 * Root-cause context:
 *   On first mount, `loadPulse()` populates `pulsePositionsStore` while
 *   `positionsStore` may still be empty (different dedup key, different load
 *   cycle). `positionsDayPnlStore.byKey` therefore had no entries → NavStrip
 *   showed `total = 0` while Pulse had valid data.
 *
 * Fix: `mergePositionStores(p1, p2)` merges both arrays, deduplicating by
 * `tradingsymbol|symbol : account`. When the same key appears in both, the
 * row with non-zero `day_change_val` is preferred over the zero-dcv row.
 *
 * Five quality dimensions:
 *   1. SSOT   — tests the exported `mergePositionStores` helper (the SSOT for
 *               the merge logic used inside `$derived.by`)
 *   2. Perf   — pure unit, no DOM / network / Svelte runtime
 *   3. Stale  — edge cases: both empty, one-sided, duplicate with dcv=0/≠0
 *   4. Reuse  — function is exported and importable independently of the store
 *   5. UX     — NavStrip total and Pulse per-symbol override are both covered
 */

import { describe, it, expect } from 'vitest';
// Import from the pure utility module — no SvelteKit virtual deps ($app/environment etc.)
import { mergePositionStores } from '$lib/data/mergePositionStores.js';

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeRow(overrides = {}) {
  return {
    tradingsymbol: 'TESTFUT',
    account: 'ACCT1',
    quantity: 1,
    overnight_quantity: 1,
    day_change_val: 0,
    pnl: 0,
    last_price: 100,
    close_price: 100,
    average_price: 100,
    ...overrides,
  };
}

// ── Scenario A: positionsStore empty, pulsePositionsStore has data ───────────

describe('mergePositionStores — Scenario A: positionsStore empty', () => {
  it('returns rows from pulsePositionsStore when positionsStore is empty', () => {
    const p1 = [];
    const p2 = [
      makeRow({
        tradingsymbol: 'A',
        account: 'X',
        day_change_val: -6000,
        quantity: 1,
        overnight_quantity: 1,
        pnl: -6000,
      }),
    ];

    const rows = mergePositionStores(p1, p2);

    expect(rows).toHaveLength(1);
    expect(rows[0].tradingsymbol).toBe('A');
    expect(rows[0].day_change_val).toBe(-6000);
  });

  it('resulting rows have correct day_change_val (non-zero from pulse store)', () => {
    // Simulates the fix: NavStrip total must be non-zero when pulse has data
    // but the cross-page poller is still empty on first mount.
    const p1 = [];
    const p2 = [
      makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: -6000 }),
    ];

    const rows = mergePositionStores(p1, p2);
    const totalDcv = rows.reduce((s, r) => s + Number(r.day_change_val), 0);

    expect(totalDcv).toBe(-6000);
  });
});

// ── Scenario B: same symbol in both stores, positionsStore has dcv=0 ─────────

describe('mergePositionStores — Scenario B: prefer non-zero dcv', () => {
  it('prefers pulse row (dcv=-6000) over positionsStore row (dcv=0)', () => {
    const p1 = [makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: 0 })];
    const p2 = [makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: -6000 })];

    const rows = mergePositionStores(p1, p2);

    expect(rows).toHaveLength(1);
    expect(rows[0].day_change_val).toBe(-6000);
  });

  it('keeps positionsStore row when it already has non-zero dcv', () => {
    // First-wins unless the existing row has dcv=0 and the incoming has dcv≠0.
    const p1 = [makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: 500 })];
    const p2 = [makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: -6000 })];

    const rows = mergePositionStores(p1, p2);

    expect(rows).toHaveLength(1);
    // p1 row already has dcv≠0 → kept; p2 not promoted
    expect(rows[0].day_change_val).toBe(500);
  });

  it('keeps the later row when both have dcv=0 (both stale)', () => {
    // Neither side has a better value — first encountered is kept.
    const p1 = [makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: 0 })];
    const p2 = [makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: 0 })];

    const rows = mergePositionStores(p1, p2);

    expect(rows).toHaveLength(1);
    expect(rows[0].day_change_val).toBe(0);
  });
});

// ── Deduplication key: symbol+account ────────────────────────────────────────

describe('mergePositionStores — dedup key uses symbol AND account', () => {
  it('same symbol, different accounts → two rows', () => {
    const p1 = [makeRow({ tradingsymbol: 'A', account: 'ACCT1', day_change_val: 100 })];
    const p2 = [makeRow({ tradingsymbol: 'A', account: 'ACCT2', day_change_val: 200 })];

    const rows = mergePositionStores(p1, p2);

    expect(rows).toHaveLength(2);
    const totDcv = rows.reduce((s, r) => s + Number(r.day_change_val), 0);
    expect(totDcv).toBe(300);
  });

  it('different symbols, same account → two rows', () => {
    const p1 = [makeRow({ tradingsymbol: 'NIFTY25AUGFUT', account: 'X', day_change_val: 1000 })];
    const p2 = [makeRow({ tradingsymbol: 'BANKNIFTY25AUGFUT', account: 'X', day_change_val: -500 })];

    const rows = mergePositionStores(p1, p2);

    expect(rows).toHaveLength(2);
  });
});

// ── Edge cases ────────────────────────────────────────────────────────────────

describe('mergePositionStores — edge cases', () => {
  it('both arrays empty → returns empty array', () => {
    expect(mergePositionStores([], [])).toEqual([]);
  });

  it('p1 populated, p2 empty → returns p1 unchanged', () => {
    const p1 = [makeRow({ tradingsymbol: 'X', account: 'Y', day_change_val: 999 })];
    const rows = mergePositionStores(p1, []);
    expect(rows).toHaveLength(1);
    expect(rows[0].day_change_val).toBe(999);
  });

  it('row with symbol field (not tradingsymbol) is keyed correctly', () => {
    // Some pulse rows may use `symbol` instead of `tradingsymbol`.
    const p1 = [];
    const p2 = [{ symbol: 'ZOMATO', account: 'ACCT1', day_change_val: -250 }];

    const rows = mergePositionStores(p1, p2);

    expect(rows).toHaveLength(1);
    expect(rows[0].day_change_val).toBe(-250);
  });

  it('negative day_change_val (loss) — not treated as zero', () => {
    // Ensure `Number(r.day_change_val) !== 0` treats negative values correctly.
    const p1 = [makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: 0 })];
    const p2 = [makeRow({ tradingsymbol: 'A', account: 'X', day_change_val: -100 })];

    const rows = mergePositionStores(p1, p2);

    expect(rows[0].day_change_val).toBe(-100);
  });

  it('multiple symbols: aggregate dcv is correct', () => {
    const p1 = [
      makeRow({ tradingsymbol: 'S1', account: 'A', day_change_val: 100 }),
      makeRow({ tradingsymbol: 'S2', account: 'A', day_change_val: 0 }),
    ];
    const p2 = [
      makeRow({ tradingsymbol: 'S2', account: 'A', day_change_val: -200 }),
      makeRow({ tradingsymbol: 'S3', account: 'A', day_change_val: 50 }),
    ];

    const rows = mergePositionStores(p1, p2);

    // S1 → from p1 (dcv=100), S2 → p2 wins (p1 dcv=0 < p2 dcv=-200 ≠ 0),
    // S3 → from p2 (dcv=50)
    expect(rows).toHaveLength(3);
    const byKey = Object.fromEntries(rows.map((r) => [r.tradingsymbol, r.day_change_val]));
    expect(byKey['S1']).toBe(100);
    expect(byKey['S2']).toBe(-200);
    expect(byKey['S3']).toBe(50);

    const total = rows.reduce((s, r) => s + Number(r.day_change_val), 0);
    expect(total).toBe(-50);
  });
});
