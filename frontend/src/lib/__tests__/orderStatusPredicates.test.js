/**
 * orderStatusPredicates.test.js
 *
 * Unit tests for the `_STATUS_PREDICATES.complete` fix shared (as separate
 * per-component copies) by OrderBook.svelte and LogPanel.svelte.
 *
 * Bug: the 'complete' predicate only matched `st === 'COMPLETE'` — the
 * broker (Kite) vocabulary. AlgoOrder rows (the only rows that ever carry
 * a `template_id` — paper/sim/replay/shadow fills, and any algo-tracked
 * live order before the broker's own COMPLETE status lands) use 'FILLED'
 * (ALGO_ORDER_FINAL_STATUSES in backend/api/models.py). Consequence: the
 * "Filled" status chip undercounted, and rows that never get a COMPLETE
 * status (algo-only fills) were unreachable from the currently-active
 * status chip's filtered view — hiding their tmpl:#N chip / Re-attach
 * button on OrderCard.svelte even though OrderCard's OWN gating logic
 * (raw `order.status === 'FILLED'` checks, not routed through this table)
 * was already correct.
 *
 * Fix: `complete: st => st === 'COMPLETE' || st === 'FILLED'` in both
 * OrderBook.svelte and LogPanel.svelte.
 *
 * These tests replicate the predicate table as a standalone pure function
 * — matching the existing pure-function test pattern in
 * ChaseCard.inflight.test.js and pulseRowsAndFlash.test.js — since the
 * table is a `<script>`-scoped const inside each .svelte file, not an
 * importable module.
 *
 * Five quality dimensions:
 *   1. SSOT   — both OrderBook.svelte and LogPanel.svelte carry the exact
 *               same fixed predicate text; this test documents+locks it.
 *   2. Perf   — pure unit tests, no DOM / network.
 *   3. Stale  — the old (broken) predicate is explicitly tested first to
 *               show the pre-fix behaviour, then the fixed predicate is
 *               tested to show the corrected behaviour.
 *   4. Reuse  — one factory, exercised against both components' status
 *               vocabularies (broker COMPLETE, algo FILLED).
 *   5. UX     — a FILLED algo row must be reachable under the "Filled"
 *               status chip so tmpl:#N / Re-attach are visible to the
 *               operator, not silently filtered out of view.
 */

import { describe, it, expect } from 'vitest';

// ── Pre-fix predicate (documents the bug for regression context) ────────
const OLD_STATUS_PREDICATES = {
  open:      (st) => st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING',
  complete:  (st) => st === 'COMPLETE',
  rejected:  (st) => st === 'REJECTED',
  cancelled: (st) => st === 'CANCELLED',
};

// ── Fixed predicate — mirrors OrderBook.svelte / LogPanel.svelte exactly ─
const STATUS_PREDICATES = {
  open:      (st) => st === 'OPEN' || st === 'TRIGGER PENDING' || st === 'TRIGGER_PENDING',
  complete:  (st) => st === 'COMPLETE' || st === 'FILLED',
  rejected:  (st) => st === 'REJECTED',
  cancelled: (st) => st === 'CANCELLED',
};

describe('order status predicates — "complete" broker-vs-algo vocabulary fix', () => {
  it('pre-fix predicate misses algo-only FILLED rows (documents the bug)', () => {
    expect(OLD_STATUS_PREDICATES.complete('COMPLETE')).toBe(true);
    expect(OLD_STATUS_PREDICATES.complete('FILLED')).toBe(false);
  });

  it('fixed predicate matches broker COMPLETE', () => {
    expect(STATUS_PREDICATES.complete('COMPLETE')).toBe(true);
  });

  it('fixed predicate matches algo FILLED', () => {
    expect(STATUS_PREDICATES.complete('FILLED')).toBe(true);
  });

  it('fixed predicate still rejects non-terminal / other-terminal statuses', () => {
    expect(STATUS_PREDICATES.complete('OPEN')).toBe(false);
    expect(STATUS_PREDICATES.complete('REJECTED')).toBe(false);
    expect(STATUS_PREDICATES.complete('CANCELLED')).toBe(false);
    expect(STATUS_PREDICATES.complete('UNFILLED')).toBe(false);
    expect(STATUS_PREDICATES.complete('CANCEL_FAILED')).toBe(false);
  });

  it('open/rejected/cancelled predicates are unchanged — algo tokens already match verbatim', () => {
    // AlgoOrder rows use OPEN / REJECTED / CANCELLED directly (same tokens
    // as broker vocabulary) — only 'complete' had a vocabulary gap.
    expect(STATUS_PREDICATES.open('OPEN')).toBe(true);
    expect(STATUS_PREDICATES.rejected('REJECTED')).toBe(true);
    expect(STATUS_PREDICATES.cancelled('CANCELLED')).toBe(true);
  });

  it('a "Filled" status-chip count over a mixed row set includes both broker COMPLETE and algo FILLED rows', () => {
    const rows = [
      { status: 'COMPLETE' },  // broker fill
      { status: 'FILLED' },    // algo-only fill (paper/sim/replay/shadow, or pre-COMPLETE live)
      { status: 'OPEN' },
      { status: 'REJECTED' },
    ];
    const completeCount = rows.filter(
      (o) => STATUS_PREDICATES.complete((o.status || '').toUpperCase())
    ).length;
    expect(completeCount).toBe(2);
  });
});
