/**
 * basket_submit_timeout_no_false_rejection.spec.js
 *
 * HIGH-severity audit finding (2026-11-05): a basket submit's client-
 * side 15s timeout reported every leg as rejected, keeping them all in
 * the basket for "retry" — even though the server had very often
 * already placed them (each live leg pays for preflight, a broker
 * margin call, and `_start_live_chase`'s own confirm window, which
 * alone can run up to 15s). A resubmit after that false "all legs
 * rejected" banner could duplicate every already-placed live order.
 * Same D3 bug class `placeTicketOrder` was already fixed for in
 * 2026-09 (`frontend/src/lib/__tests__/apiAbortError.test.js` covers
 * `placeBasket`'s own `throwOnTimeout` opt-in at the api.js layer).
 *
 * Fixed: SymbolPanel.svelte's `submitBasket()` now has a dedicated
 * `TimeoutError` branch in its catch block — renders "still
 * processing", leaves `basketLegs` COMPLETELY untouched (no leg is
 * marked failed), and returns before the ok/fails-driven success/
 * partial-failure/all-failed banner logic, which assumes a real, known
 * outcome.
 *
 * Source-grep regression guard only — same precedent as
 * order_ticket_draft_submit_label_and_chase_hidden.spec.js: driving a
 * real 15s client timeout through a live basket submit is impractical
 * to automate deterministically in CI.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const SYMBOL_PANEL_PATH = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');

function submitBasketBlock(src) {
  const start = src.indexOf('async function submitBasket');
  const end = src.indexOf('function handleParsedOrder', start);
  return src.slice(start, end);
}

test.describe('SymbolPanel submitBasket — a client-side timeout is never treated as a known rejection', () => {
  test('the catch block has a dedicated TimeoutError branch, checked before the generic failure path', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const block = submitBasketBlock(src);
    const timeoutIdx = block.indexOf("(e)?.name === 'TimeoutError'");
    const genericFailIdx = block.indexOf('basketLegs.forEach((_, i) => failedIdx.add(i));');
    expect(timeoutIdx).toBeGreaterThan(-1);
    expect(genericFailIdx).toBeGreaterThan(-1);
    expect(timeoutIdx).toBeLessThan(genericFailIdx);
  });

  test('the TimeoutError branch returns before reaching the generic failedIdx mutation', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const block = submitBasketBlock(src);
    const timeoutBranch = block.match(
      /if \(\/\*\* @type \{any\} \*\/ \(e\)\?\.name === 'TimeoutError'\) \{[\s\S]*?\n {6}\}/,
    )?.[0] ?? '';
    expect(timeoutBranch).toContain('return;');
    expect(timeoutBranch).not.toContain('failedIdx.add');
  });

  test('the timeout branch shows a "still processing" message, not a rejection message', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    const block = submitBasketBlock(src);
    const timeoutBranch = block.match(
      /if \(\/\*\* @type \{any\} \*\/ \(e\)\?\.name === 'TimeoutError'\) \{[\s\S]*?\n {6}\}/,
    )?.[0] ?? '';
    expect(timeoutBranch).toMatch(/still processing/i);
    expect(timeoutBranch).not.toMatch(/rejected/i);
  });
});
