/**
 * orderTicketDraftLifecycle.test.js
 *
 * Source-grep regression guard for two 2026-10 payoff-draft bug fixes in
 * OrderTicket.svelte:
 *
 *   Bug A (delete-before-confirmation) — submit() used to call
 *   `payoffDrafts.remove(initialDraftId)` BEFORE the real broker/paper
 *   placement call, so a failed placement still lost the draft. Fixed by
 *   threading `draftId` into `placeCtx`/`buildPlacePayload` (covered
 *   separately in orderTicketSubmit.test.js's "draft_id threading"
 *   block) and only calling `payoffDrafts.forget()` AFTER a confirmed
 *   successful `placeTicketOrder()` call.
 *
 *   Bug B (close-while-editing discards the draft) — `_handleClose()`
 *   used to call `payoffDrafts.remove(initialDraftId)` unconditionally
 *   when editing an existing draft. Fixed by making _handleClose() a
 *   pure pass-through to `onClose()`.
 *
 * Neither fix has a reachable Playwright path today: both relied on UI
 * affordances (`.ot-close`, the DRAFT checkbox) that live inside
 * `{#if standalone}` / `{#if showLimit && !modeChaseHidden}` blocks —
 * SymbolPanel (the ONLY <OrderTicket> mount site in the app) hardcodes
 * `standalone={false}` and `modeChaseHidden={true}` unconditionally, so
 * neither block ever renders through the shipped UI tree. This is a
 * pre-existing, separately-scoped defect (not introduced by this fix) —
 * see the handback notes for the full writeup. Source-grep is the
 * correct-weight test here, same convention as orderSourceVocabulary.test.js
 * (bounded-slice regex against the real .svelte source via `?raw`) and
 * draft-positions.spec.ts's own "stale-code checked via grep" precedent
 * for UI that can't be driven in this harness.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT   — reads the real shipped OrderTicket.svelte, not a copy
 *  2. Perf   — pure text/regex, no DOM, no network
 *  3. Stale  — each slice is bounded to the exact function (submit() /
 *              _handleClose()), so a regression that re-adds the
 *              premature remove() elsewhere in the file (but not inside
 *              these functions) is correctly ignored, and one added
 *              INSIDE them is correctly caught
 *  4. Reuse  — reuses the same placeCtx/buildPlacePayload plumbing every
 *              other submit() call site already depends on
 *  5. UX     — n/a (no new UI paradigm; this guards removed UI behaviour)
 */

import { describe, it, expect } from 'vitest';
import ORDER_TICKET_SRC from '../order/OrderTicket.svelte?raw';

describe('OrderTicket.svelte — _handleClose (Bug B fix)', () => {
  const start = ORDER_TICKET_SRC.indexOf('function _handleClose() {');
  const end   = ORDER_TICKET_SRC.indexOf('$effect(() => {', start);
  const block = ORDER_TICKET_SRC.slice(start, end);

  it('_handleClose exists and is non-empty (sanity check on the slice bounds)', () => {
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
  });

  it('_handleClose is a pure pass-through to onClose() — no draft mutation', () => {
    // The function body itself (after the closing `{`) must not call
    // payoffDrafts.remove/forget — only the preceding comment block may
    // mention it (as history/rationale), so slice strictly from the
    // opening brace of the function body.
    const bodyStart = block.indexOf('{', block.indexOf('function _handleClose()'));
    const body = block.slice(bodyStart);
    expect(body).not.toMatch(/payoffDrafts\.(remove|forget)\(/);
    expect(body).toContain('onClose();');
  });
});

describe('OrderTicket.svelte — submit() draft lifecycle (Bug A fix)', () => {
  const start = ORDER_TICKET_SRC.indexOf('async function submit() {');
  const end   = ORDER_TICKET_SRC.indexOf('onMount(() => {', start);
  const block = ORDER_TICKET_SRC.slice(start, end);

  it('submit() exists and is non-empty (sanity check on the slice bounds)', () => {
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
  });

  it('never calls payoffDrafts.remove() on the real-placement path', () => {
    // The draft-mode branch legitimately calls payoffDrafts.add()/
    // update() (tested in payoffDraftHelpers.test.js) — remove() must
    // not appear anywhere in submit() at all post-fix (the premature
    // "clean up stale draft" call this bug fix deleted was the only
    // remove() call that ever lived in this function).
    const codeOnly = block
      .split('\n')
      .filter((line) => !line.trim().startsWith('//'))
      .join('\n');
    expect(codeOnly).not.toMatch(/payoffDrafts\.remove\(/);
  });

  it('threads draftId into placeCtx only when !_draftMode && initialDraftId', () => {
    expect(block).toMatch(/draftId:\s*\(!_draftMode\s*&&\s*initialDraftId\)\s*\?\s*initialDraftId\s*:\s*null/);
  });

  it('forget()s the draft only AFTER a confirmed successful placeTicketOrder() call', () => {
    const placeIdx  = block.indexOf('await placeTicketOrder(');
    const forgetIdx = block.indexOf('payoffDrafts.forget(');
    const catchIdx  = block.indexOf('} catch (e) {', placeIdx);
    expect(placeIdx, 'placeTicketOrder() call must exist').toBeGreaterThan(-1);
    expect(forgetIdx, 'payoffDrafts.forget() call must exist').toBeGreaterThan(-1);
    expect(catchIdx, 'a catch block must exist after the placement call').toBeGreaterThan(-1);
    // forget() must sit strictly between the await (placement already
    // resolved without throwing) and the catch block (a throw would
    // skip straight past forget() to here) — i.e. only reached on
    // confirmed success.
    expect(forgetIdx).toBeGreaterThan(placeIdx);
    expect(forgetIdx).toBeLessThan(catchIdx);
  });

  it('draft-mode branch (add/update) uses update() for an existing draft, not remove-then-add', () => {
    const draftBranchIdx = block.indexOf("if (_draftMode && !isEquity) {");
    const draftBranchEnd = block.indexOf('NOTE (2026-10 fix)', draftBranchIdx);
    const draftBlock = block.slice(draftBranchIdx, draftBranchEnd);
    expect(draftBlock).toContain('payoffDrafts.update(initialDraftId, draftEntry)');
    expect(draftBlock).toContain('payoffDrafts.add(draftEntry)');
    expect(draftBlock).not.toMatch(/payoffDrafts\.remove\(/);
  });
});
