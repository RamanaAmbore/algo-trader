/**
 * holdRecord.test.js — Vitest unit tests for parseHold().
 *
 * Extracted from a local copy in OrderCard.svelte (2026-11-02) so every
 * hold-displaying surface reads the exact same parsed shape and can
 * never silently disagree on what "held" means. Generic across every
 * hold category — unlike `templateAttachToast.js` (deleted 2026-11,
 * Track 3 T3.4 Bracket/Template/GTT removal), this module is NOT
 * Bracket-specific and was kept, per that sprint's explicit instruction.
 *
 * Five quality dimensions:
 *  1. SSOT  — one parse function, shared by every hold-displaying
 *             surface (OrderCard.svelte, HeldOrdersCard.svelte).
 *  2. Perf  — pure, synchronous, O(1).
 *  3. Stale — guards the exact `backend/api/algo/order_hold.py:hold_record`
 *             shape (`category`/`reason`/`price_policy`/`override`/`held_at`)
 *             so a backend field rename is caught here, not silently.
 *  4. Reuse — no duplicated JSON.parse/try-catch anywhere else now that
 *             this is extracted.
 *  5. UX    — malformed/empty input never throws; callers always get a
 *             clean null to branch on.
 */

import { describe, it, expect } from 'vitest';
import { parseHold } from '../../data/holdRecord.js';

describe('parseHold', () => {
  it('returns null for null/undefined/empty input', () => {
    expect(parseHold(null)).toBeNull();
    expect(parseHold(undefined)).toBeNull();
    expect(parseHold('')).toBeNull();
  });

  it('returns null for malformed JSON instead of throwing', () => {
    expect(parseHold('{not valid json')).toBeNull();
  });

  it('returns null for a valid JSON value that is not an object (e.g. a bare number/string)', () => {
    expect(parseHold('42')).toBeNull();
    expect(parseHold('"just a string"')).toBeNull();
    expect(parseHold('null')).toBeNull();
  });

  it('parses a real backend hold_record shape (agent_close category)', () => {
    const raw = JSON.stringify({
      category: 'agent_close',
      reason: 'short option gain target reached, pending release',
      price_policy: 'n/a',
      override: null,
      held_at: '2026-11-02T10:00:00+00:00',
    });
    const parsed = parseHold(raw);
    expect(parsed).not.toBeNull();
    expect(parsed.category).toBe('agent_close');
    expect(parsed.reason).toBe('short option gain target reached, pending release');
  });

  it('parses the expiry_close and agent_order categories identically (no special-casing)', () => {
    expect(parseHold(JSON.stringify({ category: 'expiry_close', reason: 'x' })).category).toBe('expiry_close');
    expect(parseHold(JSON.stringify({ category: 'agent_order', reason: 'x' })).category).toBe('agent_order');
  });

  it('is a pure pass-through — an unrecognised/retired category string (e.g. the removed template_exit) is not special-cased or rejected', () => {
    expect(parseHold(JSON.stringify({ category: 'template_exit', reason: 'x' })).category).toBe('template_exit');
  });
});
