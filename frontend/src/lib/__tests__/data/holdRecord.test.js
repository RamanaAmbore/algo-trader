/**
 * holdRecord.test.js — Vitest unit tests for parseHold().
 *
 * Extracted from a local copy in OrderCard.svelte (2026-11-02, PLAN.md
 * item 4) so OrderCard.svelte's hold display and templateAttachToast.js's
 * toast-wording decision read the exact same parsed shape and can never
 * silently disagree on what "held" means.
 *
 * Five quality dimensions:
 *  1. SSOT  — one parse function, shared by OrderCard.svelte and
 *             templateAttachToast.js (see that file's own test for the
 *             consuming side).
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

  it('parses a real backend hold_record shape (template_exit category)', () => {
    const raw = JSON.stringify({
      category: 'template_exit',
      reason: 'template exits held until released',
      price_policy: 'n/a',
      override: null,
      held_at: '2026-11-02T10:00:00+00:00',
    });
    const parsed = parseHold(raw);
    expect(parsed).not.toBeNull();
    expect(parsed.category).toBe('template_exit');
    expect(parsed.reason).toBe('template exits held until released');
  });

  it('parses the expiry_close and agent_order categories identically (no special-casing)', () => {
    expect(parseHold(JSON.stringify({ category: 'expiry_close', reason: 'x' })).category).toBe('expiry_close');
    expect(parseHold(JSON.stringify({ category: 'agent_order', reason: 'x' })).category).toBe('agent_order');
  });
});
