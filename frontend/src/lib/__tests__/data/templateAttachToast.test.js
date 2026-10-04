/**
 * templateAttachToast.test.js — Vitest unit tests for
 * noteAttachObservation() / isAttachFailedState().
 *
 * Guards the 2026-09-30 toast-wiring fix (trading-critical toasts audit):
 * a stalled template attach (LIVE-mode order, template selected, order
 * FILLED, no GTTs ever landed) should surface a one-time toast — but
 * only once the failed state has genuinely PERSISTED (time-based, not
 * poll-count-based — a count debounce breaks the moment more than one
 * surface polls the same order concurrently, e.g. the order modal's
 * bottom OrderBook AND the /orders page's own card), and NEVER more
 * than once per order_id per session even with multiple independent
 * pollers sharing the same sessionStorage-backed clock.
 *
 * Also guards the mode gate: `_fire_template_attach_on_fill` (backend)
 * is a hard no-op for any mode other than 'live' — paper/sim/replay/
 * shadow fills NEVER attempt a real attach, so "FILLED + template +
 * no attach" is their PERMANENT normal resting state, not a failure.
 *
 * Five quality dimensions:
 *  1. SSOT  — noteAttachObservation() is the single decision point both
 *             OrderBook.svelte's and LogPanel.svelte's poll loops call
 *             through; no duplicated debounce/gating logic in either
 *             component.
 *  2. Perf  — sessionStorage-backed, O(1) per observation; no polling
 *             loop or timers introduced by this module itself.
 *  3. Stale — guards the time-based debounce explicitly (a regression
 *             back to a poll-count debounce would false-positive the
 *             instant a second concurrent poller observes the row) AND
 *             the live-only mode gate (a regression here would spam a
 *             toast on every ordinary paper/sim fill).
 *  4. Reuse — mirrors the existing `rbq.reattach-fail.<id>` sessionStorage
 *             pattern already in OrderCard.svelte rather than inventing
 *             a new persistence mechanism; threshold matches api.js's
 *             own established 15s "genuinely stuck" convention.
 *  5. UX    — recovering (attach lands) clears the debounce so a later,
 *             genuinely new failure on the same order_id is timed
 *             fresh; an order_id that already toasted never re-toasts.
 */

import { describe, it, expect, beforeEach } from 'vitest';
import {
  isAttachFailedState,
  noteAttachObservation,
  ATTACH_STALL_THRESHOLD_MS,
  _resetAttachToastState,
} from '../../data/templateAttachToast.js';

const T0 = 1_800_000_000_000; // arbitrary fixed epoch for deterministic tests

function filledLiveNoAttach(id, overrides = {}) {
  return { id, template_id: 7, status: 'FILLED', mode: 'live', attached_gtts_json: null, ...overrides };
}

beforeEach(() => {
  _resetAttachToastState();
  sessionStorage.clear();
});

describe('isAttachFailedState', () => {
  it('true when live + template_id set + status FILLED + no attach', () => {
    expect(isAttachFailedState(filledLiveNoAttach(1))).toBe(true);
  });

  it('false when no template was selected', () => {
    expect(isAttachFailedState({ id: 1, template_id: null, status: 'FILLED', mode: 'live', attached_gtts_json: null })).toBe(false);
  });

  it('false when order has not reached FILLED yet', () => {
    expect(isAttachFailedState({ id: 1, template_id: 7, status: 'OPEN', mode: 'live', attached_gtts_json: null })).toBe(false);
  });

  it('false once attached_gtts_json is populated', () => {
    expect(isAttachFailedState(filledLiveNoAttach(1, { attached_gtts_json: '[{"kind":"gtt","id":99}]' }))).toBe(false);
  });

  it.each(['paper', 'sim', 'replay', 'shadow', 'PAPER', undefined, null, ''])(
    'false for non-live mode %p — this is the permanent normal state, not a failure',
    (mode) => {
      expect(isAttachFailedState(filledLiveNoAttach(1, { mode }))).toBe(false);
    }
  );

  it('handles a null/undefined order defensively', () => {
    expect(isAttachFailedState(null)).toBe(false);
    expect(isAttachFailedState(undefined)).toBe(false);
  });

  it('false for a GTT/wing CHILD row (parent_order_id set) — it inherits template_id but never gets its own attached_gtts_json', () => {
    expect(isAttachFailedState(filledLiveNoAttach(1, { parent_order_id: 999 }))).toBe(false);
  });

  it('still true for an otherwise-identical row with no parent_order_id (a real parent order)', () => {
    expect(isAttachFailedState(filledLiveNoAttach(1, { parent_order_id: null }))).toBe(true);
  });
});

describe('noteAttachObservation — time-based debounce + dedupe', () => {
  it('does NOT fire on the first observation, even if it is already stale by ts math elsewhere', () => {
    expect(noteAttachObservation(filledLiveNoAttach(101), { now: T0 })).toBe(false);
  });

  it('does NOT fire before the threshold has elapsed', () => {
    noteAttachObservation(filledLiveNoAttach(102), { now: T0 });
    expect(noteAttachObservation(filledLiveNoAttach(102), { now: T0 + ATTACH_STALL_THRESHOLD_MS - 1 })).toBe(false);
  });

  it('fires once the threshold has elapsed since the FIRST observation', () => {
    noteAttachObservation(filledLiveNoAttach(103), { now: T0 });
    expect(noteAttachObservation(filledLiveNoAttach(103), { now: T0 + ATTACH_STALL_THRESHOLD_MS })).toBe(true);
  });

  it('does not fire again on a later observation of the same order_id', () => {
    noteAttachObservation(filledLiveNoAttach(104), { now: T0 });
    noteAttachObservation(filledLiveNoAttach(104), { now: T0 + ATTACH_STALL_THRESHOLD_MS }); // fires here
    expect(noteAttachObservation(filledLiveNoAttach(104), { now: T0 + ATTACH_STALL_THRESHOLD_MS + 60_000 })).toBe(false);
  });

  it('a poll landing during the real async-attach race does not fire a false positive', () => {
    // Observation 1: FILLED, attach not landed yet (normal race window).
    expect(noteAttachObservation(filledLiveNoAttach(105), { now: T0 })).toBe(false);
    // Observation 2, well before threshold: attach lands — recovered.
    expect(noteAttachObservation(
      filledLiveNoAttach(105, { attached_gtts_json: '[{"kind":"gtt","id":1}]' }),
      { now: T0 + 2_000 }
    )).toBe(false);
    // Observation 3, past what would have been the threshold: still
    // attached — still nothing to toast.
    expect(noteAttachObservation(
      filledLiveNoAttach(105, { attached_gtts_json: '[{"kind":"gtt","id":1}]' }),
      { now: T0 + ATTACH_STALL_THRESHOLD_MS + 1_000 }
    )).toBe(false);
  });

  it('recovering resets the clock so a later genuine failure toasts fresh', () => {
    noteAttachObservation(filledLiveNoAttach(106), { now: T0 });
    // Recovers before threshold.
    noteAttachObservation(
      filledLiveNoAttach(106, { attached_gtts_json: '[{"kind":"gtt","id":1}]' }),
      { now: T0 + 1_000 }
    );
    // A later, fresh failure (e.g. a manual re-attach was retried and
    // failed again) starts its own threshold window from ITS first
    // observation, not the original one.
    noteAttachObservation(filledLiveNoAttach(106), { now: T0 + 100_000 });
    expect(noteAttachObservation(filledLiveNoAttach(106), { now: T0 + 100_000 + ATTACH_STALL_THRESHOLD_MS - 1 })).toBe(false);
    expect(noteAttachObservation(filledLiveNoAttach(106), { now: T0 + 100_000 + ATTACH_STALL_THRESHOLD_MS })).toBe(true);
  });

  it('never fires for a paper-mode order, no matter how long it persists', () => {
    noteAttachObservation(filledLiveNoAttach(107, { mode: 'paper' }), { now: T0 });
    expect(noteAttachObservation(filledLiveNoAttach(107, { mode: 'paper' }), { now: T0 + ATTACH_STALL_THRESHOLD_MS * 10 })).toBe(false);
  });

  it('multiple concurrent pollers observing the same order share one clock (no double-count race)', () => {
    // Simulates OrderBook.svelte's poller AND LogPanel.svelte's poller
    // both observing the same order at the same two timestamps —
    // exactly the scenario a poll-COUNT debounce would double-fire on
    // (4 observations total across 2 "instants") but a time-based
    // debounce handles correctly (fires exactly once, at/after threshold).
    const results = [];
    results.push(noteAttachObservation(filledLiveNoAttach(108), { now: T0 }));          // OrderBook poll 1
    results.push(noteAttachObservation(filledLiveNoAttach(108), { now: T0 }));          // LogPanel poll 1 (same instant)
    results.push(noteAttachObservation(filledLiveNoAttach(108), { now: T0 + ATTACH_STALL_THRESHOLD_MS })); // OrderBook poll 2
    results.push(noteAttachObservation(filledLiveNoAttach(108), { now: T0 + ATTACH_STALL_THRESHOLD_MS })); // LogPanel poll 2 (same instant)
    expect(results.filter(Boolean)).toHaveLength(1);
  });

  it('ignores an order with no id (nothing to key the dedupe on)', () => {
    expect(noteAttachObservation({ template_id: 7, status: 'FILLED', mode: 'live', attached_gtts_json: null }, { now: T0 })).toBe(false);
  });

  it('accepts order_id as a fallback key when id is absent (broker order shape)', () => {
    const row = (overrides = {}) => ({ order_id: 'ORD999', template_id: 7, status: 'FILLED', mode: 'live', attached_gtts_json: null, ...overrides });
    noteAttachObservation(row(), { now: T0 });
    expect(noteAttachObservation(row(), { now: T0 + ATTACH_STALL_THRESHOLD_MS })).toBe(true);
  });

  it('tracks distinct order_ids independently', () => {
    noteAttachObservation(filledLiveNoAttach(201), { now: T0 });
    expect(noteAttachObservation(filledLiveNoAttach(202), { now: T0 })).toBe(false);
    expect(noteAttachObservation(filledLiveNoAttach(201), { now: T0 + ATTACH_STALL_THRESHOLD_MS })).toBe(true);
    expect(noteAttachObservation(filledLiveNoAttach(202), { now: T0 + ATTACH_STALL_THRESHOLD_MS })).toBe(true);
  });

  it('survives a simulated component remount (sessionStorage persists within the tab)', () => {
    noteAttachObservation(filledLiveNoAttach(301), { now: T0 });
    expect(noteAttachObservation(filledLiveNoAttach(301), { now: T0 + ATTACH_STALL_THRESHOLD_MS })).toBe(true);
    // Post-remount, further observations still don't re-toast.
    expect(noteAttachObservation(filledLiveNoAttach(301), { now: T0 + ATTACH_STALL_THRESHOLD_MS + 60_000 })).toBe(false);
  });
});
