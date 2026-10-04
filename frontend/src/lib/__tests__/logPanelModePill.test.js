/**
 * logPanelModePill.test.js
 *
 * Unit tests for the `_modePill(mode)` fix in LogPanel.svelte.
 *
 * Bug: any unrecognized mode (including 'draft', a real AlgoOrder.mode
 * value) fell through to LIVE_PILL — rendering the text "LIVE" on a
 * draft order that was never placed and never hit a real broker. Fix
 * adds explicit draft/armed branches and a genuine unknown fallback;
 * 'live' is now its own branch, not the catch-all.
 *
 * These tests replicate `_modePill` as a standalone pure function —
 * matching the existing pure-function test pattern in
 * ChaseCard.inflight.test.js and orderStatusPredicates.test.js — since
 * the function is a `<script>`-scoped const inside LogPanel.svelte, not
 * an importable module.
 *
 * Five quality dimensions:
 *   1. SSOT   — one function backs every mode pill rendered anywhere in
 *               the Order tab; this test locks its exact branch order.
 *   2. Perf   — pure unit tests, no DOM / network.
 *   3. Stale  — the old (buggy) fallthrough-to-LIVE behaviour is tested
 *               explicitly first to document the bug, then the fixed
 *               branches are verified.
 *   4. Reuse  — one factory exercised against every known mode plus an
 *               unrecognized one.
 *   5. UX     — a draft/unrecognized order must never render the LIVE
 *               pill — that's an actively misleading label for an order
 *               that never reached (or never will reach) a real broker.
 */

import { describe, it, expect } from 'vitest';

// Replicated from LogPanel.svelte's fixed _modePill — keep in sync.
const SIM_PILL     = '<span class="mode-pill mode-pill-sim">SIM</span>';
const LIVE_PILL    = '<span class="mode-pill mode-pill-live">LIVE</span>';
const PAPER_PILL   = '<span class="mode-pill mode-pill-paper">PAPER</span>';
const REPLAY_PILL  = '<span class="mode-pill mode-pill-replay">REPLAY</span>';
const SHADOW_PILL  = '<span class="mode-pill mode-pill-shadow">SHADOW</span>';
const DRAFT_PILL   = '<span class="mode-pill mode-pill-draft">DRAFT</span>';
const ARMED_PILL   = '<span class="mode-pill mode-pill-armed">ARMED</span>';
const UNKNOWN_PILL = '<span class="mode-pill mode-pill-unknown">UNKNOWN</span>';

function _modePill(mode) {
  if (mode === 'sim')    return SIM_PILL;
  if (mode === 'paper')  return PAPER_PILL;
  if (mode === 'replay') return REPLAY_PILL;
  if (mode === 'shadow') return SHADOW_PILL;
  if (mode === 'live')   return LIVE_PILL;
  if (mode === 'draft')  return DRAFT_PILL;
  if (mode === 'armed')  return ARMED_PILL;
  return UNKNOWN_PILL;
}

// Mirrors the OLD (buggy) implementation — any unrecognized mode fell
// through to LIVE_PILL, including 'draft'.
function _modePillOldBuggy(mode) {
  if (mode === 'sim')    return SIM_PILL;
  if (mode === 'paper')  return PAPER_PILL;
  if (mode === 'replay') return REPLAY_PILL;
  if (mode === 'shadow') return SHADOW_PILL;
  return LIVE_PILL;
}

describe('_modePill — pre-fix behaviour (documents the bug)', () => {
  it('a draft order falsely rendered the LIVE pill', () => {
    expect(_modePillOldBuggy('draft')).toBe(LIVE_PILL);
  });

  it('any unrecognized mode falsely rendered the LIVE pill', () => {
    expect(_modePillOldBuggy('bogus')).toBe(LIVE_PILL);
    expect(_modePillOldBuggy(undefined)).toBe(LIVE_PILL);
  });
});

describe('_modePill — fixed behaviour', () => {
  it('known modes map to their own pills', () => {
    expect(_modePill('sim')).toBe(SIM_PILL);
    expect(_modePill('paper')).toBe(PAPER_PILL);
    expect(_modePill('replay')).toBe(REPLAY_PILL);
    expect(_modePill('shadow')).toBe(SHADOW_PILL);
    expect(_modePill('live')).toBe(LIVE_PILL);
  });

  it('draft renders its own pill, never LIVE', () => {
    expect(_modePill('draft')).toBe(DRAFT_PILL);
    expect(_modePill('draft')).not.toBe(LIVE_PILL);
  });

  it('armed renders its own pill, never LIVE (no rows carry this mode yet)', () => {
    expect(_modePill('armed')).toBe(ARMED_PILL);
    expect(_modePill('armed')).not.toBe(LIVE_PILL);
  });

  it('an unrecognized mode renders UNKNOWN, never LIVE', () => {
    expect(_modePill('bogus')).toBe(UNKNOWN_PILL);
    expect(_modePill('bogus')).not.toBe(LIVE_PILL);
  });

  it('a missing/undefined mode (never passed `|| \'live\'` at the call site) renders UNKNOWN, never LIVE', () => {
    expect(_modePill(undefined)).toBe(UNKNOWN_PILL);
    expect(_modePill(null)).toBe(UNKNOWN_PILL);
  });
});

// ── Call-site normalization (LogPanel.svelte _orderRowHtml) ──────────────
//
// Broker-only rows (direct Kite book, no AlgoOrder counterpart) carry no
// `mode` field at all. The call site normalizes with `o.mode || 'live'`
// — the same convention already used elsewhere in LogPanel.svelte
// (_applyModeFilter, _gatingMode filters) — so a real live broker order
// still renders LIVE, not UNKNOWN.
describe('_modePill call-site normalization — broker rows with no mode field', () => {
  it('a broker row with no mode field still renders LIVE via the `|| \'live\'` normalization', () => {
    const brokerRow = { order_id: 'ORD1', status: 'COMPLETE' }; // no `mode` key
    expect(_modePill(brokerRow.mode || 'live')).toBe(LIVE_PILL);
  });
});
