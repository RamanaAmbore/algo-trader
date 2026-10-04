/**
 * payoffDraftHelpers.test.js — Vitest unit tests for payoffDraftHelpers.js
 * (the pure row↔entry conversion logic backing payoffDrafts.svelte.js's
 * server-persisted draft CRUD, 2026-10).
 *
 * payoffDrafts.svelte.js itself has module-level Svelte 5 $state calls
 * and can't be imported directly in this harness (no svelte-compiler
 * plugin registered in vitest.config.js — same established constraint as
 * dataStore.svelte.js / portfolioStore.svelte.js). The pure conversion
 * logic was deliberately split into payoffDraftHelpers.js (no runes) so
 * it's directly importable here; a source-grep block below additionally
 * verifies the REAL payoffDrafts.svelte.js wires load()/add()/update()/
 * remove() through these helpers + api.js correctly, without needing to
 * execute its $state-bearing module body.
 *
 * Five quality dimensions:
 *  1. SSOT   — imports the real draftRowToEntry/draftEntryToBody, not a
 *              hand-copied mirror
 *  2. Perf   — pure functions, no I/O, no timers
 *  3. Stale  — source-grep confirms the real store calls deleteDraft and
 *              treats 404 as success (the exact "don't surface a 404 as
 *              an operator-visible error" requirement)
 *  4. Reuse  — draftRowToEntry/draftEntryToBody are the SAME helpers
 *              load()/add()/update() all funnel through — no duplicated
 *              conversion logic anywhere in the real store
 *  5. UX     — sign-convention tests guard the exact bug class this
 *              feature depends on (BUY positive / SELL negative qty,
 *              matching the derivatives page's local draft shape)
 */

import { describe, it, expect } from 'vitest';
import { draftRowToEntry, draftEntryToBody } from '$lib/data/payoffDraftHelpers.js';
import payoffDraftsSrc from '$lib/data/payoffDrafts.svelte.js?raw';

// ── draftRowToEntry ──────────────────────────────────────────────────────

describe('draftRowToEntry', () => {
  it('converts a BUY row to a positive signed qty', () => {
    const entry = draftRowToEntry({
      id: 42, symbol: 'niftY26OCT25000ce', exchange: 'NFO',
      transaction_type: 'BUY', quantity: 75, initial_price: 120.5, account: 'ZG0790',
    });
    expect(entry.id).toBe(42);
    expect(entry.symbol).toBe('NIFTY26OCT25000CE');
    expect(entry.qty).toBe(75);
    expect(entry.avg_cost).toBe(120.5);
    expect(entry.account).toBe('ZG0790');
    expect(entry.transaction_type).toBe('BUY');
    expect(entry.is_draft).toBe(true);
  });

  it('converts a SELL row to a negative signed qty', () => {
    const entry = draftRowToEntry({
      id: 7, symbol: 'NIFTY26OCT25000PE', exchange: 'NFO',
      transaction_type: 'SELL', quantity: 75, initial_price: null, account: '',
    });
    expect(entry.qty).toBe(-75);
    expect(entry.avg_cost).toBeNull();
    expect(entry.account).toBe('');
  });

  it('derives option_type and strike from the symbol text', () => {
    const entry = draftRowToEntry({
      id: 1, symbol: 'NIFTY26OCT25000CE', exchange: 'NFO',
      transaction_type: 'BUY', quantity: 75,
    });
    expect(entry.option_type).toBe('CE');
    expect(entry.strike).toBe(25000);
  });

  it('defaults transaction_type to BUY when missing/garbage', () => {
    const entry = draftRowToEntry({ id: 2, symbol: 'RELIANCE', exchange: 'NSE', quantity: 10 });
    expect(entry.transaction_type).toBe('BUY');
    expect(entry.qty).toBe(10);
  });

  it('round-trips the real backend id unchanged (not re-generated)', () => {
    const entry = draftRowToEntry({ id: 1088, symbol: 'NIFTY26OCTFUT', exchange: 'NFO', transaction_type: 'BUY', quantity: 50 });
    expect(entry.id).toBe(1088);
  });
});

// ── draftEntryToBody ─────────────────────────────────────────────────────

describe('draftEntryToBody', () => {
  it('builds a BUY body from a positive signed qty', () => {
    const body = draftEntryToBody({ symbol: 'nifty26oct25000ce', exchange: 'NFO', qty: 75, avg_cost: 120.5, account: 'ZG0790' });
    expect(body).toEqual({
      symbol: 'NIFTY26OCT25000CE', exchange: 'NFO',
      transaction_type: 'BUY', quantity: 75, price: 120.5, account: 'ZG0790',
    });
  });

  it('builds a SELL body from a negative signed qty, sending unsigned quantity', () => {
    const body = draftEntryToBody({ symbol: 'NIFTY26OCT25000PE', exchange: 'NFO', qty: -75, avg_cost: null, account: null });
    expect(body.transaction_type).toBe('SELL');
    expect(body.quantity).toBe(75);
    expect(body.price).toBeNull();
    expect(body.account).toBeNull();
  });

  it('falls back to exchange=NFO and quantity=1 when unset', () => {
    const body = draftEntryToBody({ symbol: 'NIFTY26OCT25000CE', qty: 0 });
    expect(body.exchange).toBe('NFO');
    expect(body.quantity).toBe(1);
  });

  it('is the exact inverse of draftRowToEntry for a round trip', () => {
    const original = { id: 9, symbol: 'BANKNIFTY26OCT50000CE', exchange: 'NFO', transaction_type: 'SELL', quantity: 25, initial_price: 310, account: 'ZJ6294' };
    const entry = draftRowToEntry(original);
    const body = draftEntryToBody(entry);
    expect(body.symbol).toBe(original.symbol);
    expect(body.exchange).toBe(original.exchange);
    expect(body.transaction_type).toBe(original.transaction_type);
    expect(body.quantity).toBe(original.quantity);
    expect(body.price).toBe(original.initial_price);
    expect(body.account).toBe(original.account);
  });
});

// ── Source-grep guard — real payoffDrafts.svelte.js wiring ──────────────

describe('payoffDrafts.svelte.js — source-grep guard (real file, not the helpers)', () => {
  const src = payoffDraftsSrc;

  it('load() calls fetchDrafts() and populates the Map via draftRowToEntry', () => {
    const idx = src.indexOf('async load()');
    expect(idx, 'load() must exist').toBeGreaterThan(0);
    const block = src.slice(idx, src.indexOf('},', idx));
    expect(block).toContain('fetchDrafts()');
    expect(block).toContain('draftRowToEntry(row)');
  });

  it('add() calls createDraft() and stores the entry under the real returned id', () => {
    const idx = src.indexOf('async add(entry)');
    expect(idx, 'add() must exist').toBeGreaterThan(0);
    const block = src.slice(idx, src.indexOf('},', idx));
    expect(block).toContain('createDraft(body)');
    expect(block).toContain('resp?.id');
    expect(block).toContain('next.set(id,');
  });

  it('update() calls updateDraft(id, body) and merges the PATCH response', () => {
    const idx = src.indexOf('async update(id, entry)');
    expect(idx, 'update() must exist').toBeGreaterThan(0);
    const block = src.slice(idx, src.indexOf('},', idx));
    expect(block).toContain('updateDraft(id, body)');
    expect(block).toContain('draftRowToEntry(resp)');
  });

  it('remove() calls deleteDraft(id) and treats a 404 as success, not an error', () => {
    const idx = src.indexOf('async remove(id)');
    expect(idx, 'remove() must exist').toBeGreaterThan(0);
    const block = src.slice(idx, src.indexOf('},', idx));
    expect(block).toContain('deleteDraft(id)');
    // Must special-case 404 → fall through to the local delete instead
    // of bailing out as if it were a real failure.
    expect(block).toMatch(/status\s*!==\s*404/);
  });

  it('forget() removes the LOCAL entry only — no network call', () => {
    const idx = src.indexOf('forget(id)');
    expect(idx, 'forget() must exist').toBeGreaterThan(0);
    const block = src.slice(idx, src.indexOf('},', idx));
    expect(block).not.toMatch(/deleteDraft|fetchDrafts|createDraft|updateDraft/);
    expect(block).toContain('next.delete(id)');
  });
});
