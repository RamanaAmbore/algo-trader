/**
 * draftOrderRow.test.js — Vitest tests for isDraftOrderRow, the shared
 * predicate OrderBook.svelte, LogPanel.svelte, and OrderCard.svelte all
 * use to exclude a draft AlgoOrder row (mode='draft', status='OPEN') from
 * status-chip counts/classification (2026-10 round 2).
 *
 * Five quality dimensions:
 *  1. SSOT  — one predicate, shared by every consumer (the drift risk the
 *             risk council flagged when scoping this fix to two files)
 *  2. Perf  — pure string compare, no I/O
 *  3. Stale — guards the exact case-sensitivity / null-safety that made
 *             this a real backend field worth testing defensively
 *  4. Reuse — same shape AlgoOrderInfo/AlgoOrder rows carry across every
 *             consumer (OrderBook/LogPanel/OrderCard/orders page)
 *  5. UX    — a draft must never be misclassified as any other mode
 */

import { describe, it, expect } from 'vitest';
import { isDraftOrderRow } from '$lib/data/draftOrderRow.js';

describe('isDraftOrderRow', () => {
  it('true for mode="draft"', () => {
    expect(isDraftOrderRow({ mode: 'draft', status: 'OPEN' })).toBe(true);
  });

  it('case-insensitive — backend/DB values are not guaranteed lowercase', () => {
    expect(isDraftOrderRow({ mode: 'DRAFT' })).toBe(true);
    expect(isDraftOrderRow({ mode: 'Draft' })).toBe(true);
  });

  it('false for every other real mode', () => {
    for (const mode of ['live', 'paper', 'sim', 'replay', 'shadow']) {
      expect(isDraftOrderRow({ mode })).toBe(false);
    }
  });

  it('false for a bare broker row with no mode field at all', () => {
    expect(isDraftOrderRow({ status: 'OPEN' })).toBe(false);
  });

  it('never throws on null/undefined input', () => {
    expect(isDraftOrderRow(null)).toBe(false);
    expect(isDraftOrderRow(undefined)).toBe(false);
  });
});
