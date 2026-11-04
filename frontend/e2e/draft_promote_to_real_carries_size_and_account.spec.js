/**
 * draft_promote_to_real_carries_size_and_account.spec.js
 *
 * 2026-11-04 — operator: "audit draft conversion to real position to
 * showing order ticket with correct buy/sell waiting for submit.
 * basically the draft position details to be used to place the order
 * in the same direction." Follow-up audit found BUY/SELL side was
 * already correct, but two real gaps in "use the draft position
 * details": quantity was hardcoded to 1 lot on BOTH promote entry
 * points (MarketPulse's `_openTicketFromDraftRow`, derivatives'
 * `executeDraft`), and the derivatives page additionally dropped the
 * draft's own account entirely (its `_allDrafts` mapping never
 * included `account` from the `payoffDrafts` entry in the first
 * place, so `pageLoad.js`'s candidate builder had nothing to carry
 * forward even before `executeDraft` read it).
 *
 * Fixed:
 *  - MarketPulse.svelte: `qty` now carries the draft's own unsigned
 *    contract size (`Math.abs(r.qty_pos)`), falling back to one lot
 *    only when the draft's qty is missing/zero.
 *  - derivatives/+page.svelte `_allDrafts`: now includes `account`
 *    from the payoffDrafts entry (previously omitted entirely).
 *  - derivatives/pageLoad.js: the draft-candidate push now carries a
 *    `draft_account` field (additive — the existing `account: ''` on
 *    the same object stays unchanged, since several consumers read
 *    that as "no account filter"/"unassigned" for a draft row).
 *  - derivatives/+page.svelte `executeDraft`: `qty` carries the
 *    draft's own size (same convention as MarketPulse); `account`
 *    prefers `draft_account`, falling back to the page default only
 *    when the draft was never assigned one.
 *
 * Source-grep regression guards only — same precedent as
 * order_ticket_draft_submit_label_and_chase_hidden.spec.js: live
 * automation of "click a specific draft row, inspect the resulting
 * ticket's pre-filled qty/account" is impractical to drive
 * deterministically in CI, so these pin the exact conditional text.
 * Confirmed to fail against the pre-fix code via git stash before
 * landing.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const MARKET_PULSE_PATH = path.resolve(DIR, '../src/lib/MarketPulse.svelte');
const DERIV_PAGE_PATH   = path.resolve(DIR, '../src/routes/(algo)/admin/derivatives/+page.svelte');
const PAGE_LOAD_PATH    = path.resolve(DIR, '../src/lib/derivatives/pageLoad.js');

test.describe('MarketPulse — promote-to-real carries the draft\'s own quantity', () => {
  test('_openTicketFromDraftRow qty is derived from the draft\'s signed qty, not a bare lot default', () => {
    const src = readFileSync(MARKET_PULSE_PATH, 'utf8');
    const fn = src.match(/async function _openTicketFromDraftRow\([\s\S]*?\n  \}\n/)?.[0] ?? '';
    expect(fn).toMatch(/qty:\s*Math\.abs\(Number\(r\.qty_pos\) \|\| 0\) \|\| lot \|\| 1,/);
    // The old unconditional "qty: lot || 1" (ignoring the draft's own
    // size entirely) must be gone.
    expect(fn).not.toMatch(/qty:\s*lot \|\| 1,/);
  });
});

test.describe('derivatives pageLoad.js — draft candidates carry their own account separately from the no-filter account field', () => {
  test('the draft-candidate push includes draft_account from the entry\'s own account', () => {
    const src = readFileSync(PAGE_LOAD_PATH, 'utf8');
    expect(src).toMatch(/draft_account:\s*String\(d\.account \|\| ''\)/);
    // The pre-existing `account: ''` no-filter sentinel on the same
    // object must be untouched — this is an ADDITIVE fix.
    expect(src).toMatch(/symbol:\s*sym,\s*account:\s*'',\s*qty,\s*avg_cost:\s*cost,\s*ltp,/);
  });
});

test.describe('derivatives +page.svelte — promote-to-real carries the draft\'s own quantity and account', () => {
  test('_allDrafts maps account from the payoffDrafts entry (previously dropped entirely)', () => {
    const src = readFileSync(DERIV_PAGE_PATH, 'utf8');
    const block = src.match(/const _allDrafts = \$derived\.by\(\(\) => \{[\s\S]*?\n  \}\);/)?.[0] ?? '';
    expect(block).toMatch(/account:\s*e\.account \?\? '',/);
  });

  test('executeDraft qty is derived from the candidate\'s own qty, not a bare lot default', () => {
    const src = readFileSync(DERIV_PAGE_PATH, 'utf8');
    const fn = src.match(/async function executeDraft\([\s\S]*?\n  \}\n/)?.[0] ?? '';
    expect(fn).toMatch(/const qty\s*=\s*Math\.abs\(Number\(c\.qty\) \|\| 0\) \|\| lot;/);
    // The old unconditional "const qty = lot;" (ignoring the draft's
    // own recorded size) must be gone.
    expect(fn).not.toMatch(/const qty\s*=\s*lot;\s*\n/);
  });

  test('executeDraft account prefers the draft\'s own draft_account over the page default', () => {
    const src = readFileSync(DERIV_PAGE_PATH, 'utf8');
    const fn = src.match(/async function executeDraft\([\s\S]*?\n  \}\n/)?.[0] ?? '';
    expect(fn).toMatch(
      /const acct\s*=\s*\(c\.draft_account && String\(c\.draft_account\)\.trim\(\)\)\s*\n\s*\|\| _ticketAccountDefault\(\);/,
    );
  });
});
