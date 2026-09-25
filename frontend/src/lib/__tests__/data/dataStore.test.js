/**
 * dataStore.test.js — Vitest unit tests for dataStore.svelte.js's
 * real-money "0 instead of last-known-good" guard (2026-09).
 *
 * dataStore.svelte.js uses Svelte 5 runes ($state) inside createDataStore()
 * and can't be exercised end-to-end in this harness (no svelte-compiler
 * plugin registered in vitest.config.js — see portfolioStore.test.js's
 * established local-mirror precedent). Two of the new exports —
 * isEmptyValue() and extractStaleMeta() — are plain functions with NO
 * rune usage (hoisted to module scope specifically so they're directly
 * importable/testable here); this file exercises those directly, plus a
 * pure-function mirror of the degraded/empty guard predicate
 * (_applyRaw's `dropEmpty` condition) for the parts that DO depend on
 * $state (`_value`, `keepStaleOnEmpty`, `staleMeta.degraded`).
 *
 * Five quality dimensions:
 *  1. SSOT   — isEmptyValue/extractStaleMeta imported directly from the
 *              shipped source, not re-implemented
 *  2. Perf   — pure functions, no I/O, no timers
 *  3. Stale  — covers exactly the real-money bug: a backend response tagged
 *              degraded (non-empty stale_accounts) must not overwrite a
 *              populated store with an empty array; a genuinely empty +
 *              non-degraded response must still be allowed to zero out
 *  4. Reuse  — mirrors the "dropEmpty" predicate that both _fetch() and
 *              ingest() (via _applyRaw) share in the real store
 *  5. UX     — asserts store.meta shape drives PositionStrip's degraded
 *              vs confirmed-empty branch (see extractStaleMeta doc)
 */

import { describe, it, expect } from 'vitest';
import { isEmptyValue, extractStaleMeta } from '$lib/data/dataStore.svelte.js';

// ── isEmptyValue ─────────────────────────────────────────────────────────

describe('isEmptyValue', () => {
  it('treats null/undefined as empty', () => {
    expect(isEmptyValue(null)).toBe(true);
    expect(isEmptyValue(undefined)).toBe(true);
  });

  it('treats an empty array as empty', () => {
    expect(isEmptyValue([])).toBe(true);
  });

  it('treats a non-empty array as NOT empty', () => {
    expect(isEmptyValue([{ tradingsymbol: 'NIFTY25JANFUT' }])).toBe(false);
  });

  it('treats an empty plain object as empty', () => {
    expect(isEmptyValue({})).toBe(true);
  });

  it('treats a non-empty plain object as NOT empty', () => {
    expect(isEmptyValue({ NIFTY: [1, 2, 3] })).toBe(false);
  });

  it('treats primitives (0, false, "") as NOT empty', () => {
    // Only null/undefined/[]/{} are "empty" in this sense — a scalar 0 is
    // a legitimate value, not an absence of data.
    expect(isEmptyValue(0)).toBe(false);
    expect(isEmptyValue(false)).toBe(false);
    expect(isEmptyValue('')).toBe(false);
  });
});

// ── extractStaleMeta ─────────────────────────────────────────────────────

describe('extractStaleMeta', () => {
  it('returns degraded=false when no metaFn is supplied (default, non-book stores)', () => {
    const meta = extractStaleMeta({ rows: [], stale_accounts: ['ZG0790'] }, undefined);
    expect(meta).toEqual({ degraded: false, staleAccounts: [], asOf: null });
  });

  it('returns degraded=false when metaFn reports an empty stale_accounts list', () => {
    const metaFn = (r) => ({ staleAccounts: r?.stale_accounts ?? [], asOf: r?.as_of ?? null });
    const meta = extractStaleMeta({ rows: [{ x: 1 }], stale_accounts: [], as_of: null }, metaFn);
    expect(meta.degraded).toBe(false);
    expect(meta.staleAccounts).toEqual([]);
  });

  it('returns degraded=true when metaFn reports a non-empty stale_accounts list', () => {
    const metaFn = (r) => ({ staleAccounts: r?.stale_accounts ?? [], asOf: r?.as_of ?? null });
    const meta = extractStaleMeta(
      { rows: [], stale_accounts: ['ZG0790'], as_of: null },
      metaFn
    );
    expect(meta.degraded).toBe(true);
    expect(meta.staleAccounts).toEqual(['ZG0790']);
  });

  it('carries through as_of when present (persisted off-hours snapshot)', () => {
    const metaFn = (r) => ({ staleAccounts: r?.stale_accounts ?? [], asOf: r?.as_of ?? null });
    const meta = extractStaleMeta(
      { rows: [], stale_accounts: [], as_of: '2026-09-25T02:30:00+00:00' },
      metaFn
    );
    expect(meta.asOf).toBe('2026-09-25T02:30:00+00:00');
    expect(meta.degraded).toBe(false);
  });

  it('defensively defaults staleAccounts to [] when metaFn returns a non-array', () => {
    // Deliberately malformed return shape (staleAccounts as a string, not
    // an array) to exercise extractStaleMeta's defensive Array.isArray(...)
    // guard at runtime — cast via `any` so TS doesn't flag the intentional
    // mismatch (a real-world malformed backend response wouldn't be typed
    // correctly either — that's exactly the runtime case being guarded).
    const metaFn = /** @type {any} */ (() => ({ staleAccounts: 'not-an-array', asOf: null }));
    const meta = extractStaleMeta({}, metaFn);
    expect(meta.staleAccounts).toEqual([]);
    expect(meta.degraded).toBe(false);
  });

  it('defensively handles metaFn returning null/undefined', () => {
    const metaFn = () => null;
    const meta = extractStaleMeta({}, metaFn);
    expect(meta).toEqual({ degraded: false, staleAccounts: [], asOf: null });
  });
});

// ── _applyRaw's dropEmpty predicate (pure-function mirror) ────────────────
//
// Mirrors the exact condition inside dataStore.svelte.js's _applyRaw:
//   const dropEmpty = (keepStaleOnEmpty || staleMeta.degraded)
//     && _isEmpty(next) && _value != null && !_isEmpty(_value);
// Local mirror (not the rune-bearing real function) per the file-header
// note — see portfolioStore.test.js for the established precedent.

function computeDropEmpty({ keepStaleOnEmpty, degraded, next, prevValue }) {
  return Boolean(
    (keepStaleOnEmpty || degraded)
    && isEmptyValue(next)
    && prevValue != null
    && !isEmptyValue(prevValue)
  );
}

describe('_applyRaw dropEmpty guard — real-money "0 instead of last-known-good" fix', () => {
  const NON_EMPTY_POS = [{ tradingsymbol: 'NIFTY25JANFUT', pnl: 1250 }];

  it('drops an empty+degraded response when a populated prior value exists (the core fix)', () => {
    const drop = computeDropEmpty({
      keepStaleOnEmpty: false, // positions/holdings/funds do NOT set this blanket flag
      degraded: true,          // backend tagged stale_accounts non-empty
      next: [],
      prevValue: NON_EMPTY_POS,
    });
    expect(drop).toBe(true);
  });

  it('does NOT drop a genuinely empty, non-degraded response (confirmed empty book)', () => {
    // e.g. operator closed everything, or the 08:00 daily rollover — a
    // real 0 must be allowed through, per the plan's explicit warning
    // against a blanket keepStaleOnEmpty.
    const drop = computeDropEmpty({
      keepStaleOnEmpty: false,
      degraded: false,
      next: [],
      prevValue: NON_EMPTY_POS,
    });
    expect(drop).toBe(false);
  });

  it('does NOT drop when there is no prior value to fall back to (cold start)', () => {
    const drop = computeDropEmpty({
      keepStaleOnEmpty: false,
      degraded: true,
      next: [],
      prevValue: null,
    });
    expect(drop).toBe(false);
  });

  it('does NOT drop when the fresh response is non-empty, even if degraded (partial substitution)', () => {
    // R1 case: some accounts substituted, others healthy — the response
    // itself carries real rows; the store still writes it through (meta
    // flags it degraded for the UI badge, but the value updates).
    const drop = computeDropEmpty({
      keepStaleOnEmpty: false,
      degraded: true,
      next: [{ tradingsymbol: 'BANKNIFTY25JANFUT', pnl: -400 }],
      prevValue: NON_EMPTY_POS,
    });
    expect(drop).toBe(false);
  });

  it('keepStaleOnEmpty alone (non-degraded) still drops an empty response — existing movers/sparklines behaviour preserved', () => {
    const drop = computeDropEmpty({
      keepStaleOnEmpty: true,
      degraded: false,
      next: [],
      prevValue: [{ tradingsymbol: 'NIFTY' }],
    });
    expect(drop).toBe(true);
  });
});
