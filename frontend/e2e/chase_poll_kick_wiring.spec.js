/**
 * chase_poll_kick_wiring.spec.js
 *
 * 2026-11-05 live operator report: "chase status is taking time to
 * display. it can show chase immediately after showing the order."
 * ChaseCard.svelte's own poll idles down to a 15-30s cadence once
 * nothing is chasing (deliberate — avoids background network noise on
 * a resting page), so a freshly-armed chase sits behind whatever's
 * left of that window before its badge/countdown appears, even though
 * the order itself renders right away via a faster-polled surface.
 *
 * Fix: `chasePollKick`/`kickChasePoll` ($lib/stores.js) — a shared
 * incrementing-counter store any order-submit success path can bump to
 * force every mounted ChaseCard instance to poll immediately, instead
 * of waiting out its own idle timer.
 *
 * Source-grep regression guard, same precedent as
 * order_ticket_draft_submit_label_and_chase_hidden.spec.js — driving a
 * real live-chase submit end-to-end through a broker sandbox is
 * impractical to automate deterministically in CI. `$lib/stores`
 * itself cannot be imported directly by a plain Vitest unit test in
 * this codebase (it transitively pulls in a Svelte-5-runes `.svelte.js`
 * module that the bare Vitest config doesn't compile), so this is the
 * only practical automated coverage for the wiring.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const STORES_PATH = path.resolve(DIR, '../src/lib/stores.js');
const CHASE_CARD_PATH = path.resolve(DIR, '../src/lib/order/ChaseCard.svelte');
const ORDER_TICKET_PATH = path.resolve(DIR, '../src/lib/order/OrderTicket.svelte');
const SYMBOL_PANEL_PATH = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');

test.describe('chasePollKick wiring — order submit forces an immediate ChaseCard poll', () => {
  test('stores.js exports chasePollKick (writable) and kickChasePoll()', () => {
    const src = readFileSync(STORES_PATH, 'utf8');
    expect(src).toMatch(/export const chasePollKick\s*=\s*writable\(0\)/);
    expect(src).toMatch(/export function kickChasePoll\(\)\s*\{/);
    // Must actually bump the store, not just declare the function.
    const fnBlock = src.match(/export function kickChasePoll\(\)\s*\{[\s\S]*?\n\}/)?.[0] ?? '';
    expect(fnBlock).toMatch(/chasePollKick\.update\(/);
  });

  test('ChaseCard.svelte subscribes to chasePollKick and force-polls on a bump, skipping the mount-time run', () => {
    const src = readFileSync(CHASE_CARD_PATH, 'utf8');
    expect(src).toMatch(/import\s*\{[^}]*chasePollKick[^}]*\}\s*from\s*'\$lib\/stores'/);
    // An $effect block that reads the store and calls _poll (not _load
    // directly — must go through the in-flight-guarded wrapper).
    const effectBlock = src.match(/\$effect\(\(\)\s*=>\s*\{[\s\S]*?\$chasePollKick[\s\S]*?\n {2}\}\);/)?.[0] ?? '';
    expect(effectBlock, 'expected an $effect reading $chasePollKick').not.toBe('');
    expect(effectBlock).toMatch(/_poll/);
    // Must skip its own first run (mount-time onMount() already covers
    // the initial load) — a guard flag pattern, not an unconditional fire.
    expect(effectBlock).toMatch(/_kickedOnce|if\s*\(!_kicked/);
  });

  test('OrderTicket.svelte calls kickChasePoll() after a successful live, chase-eligible submit', () => {
    const src = readFileSync(ORDER_TICKET_PATH, 'utf8');
    expect(src).toMatch(/kickChasePoll/);
    // Gated on live mode + chase eligibility (showLimit && _chase) — must
    // not fire for MARKET/SL-M legs or an explicit chase=off submit.
    const callSite = src.match(/if\s*\(_mode === 'live' && showLimit && _chase\)\s*\{[\s\S]*?kickChasePoll\(\);[\s\S]*?\n {8}\}/)?.[0] ?? '';
    expect(callSite, 'expected a gated kickChasePoll() call site').not.toBe('');
  });

  test('SymbolPanel.svelte calls kickChasePoll() after a successful live basket submit with chase armed', () => {
    const src = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    expect(src).toMatch(/kickChasePoll/);
    const callSite = src.match(/if\s*\(basketMode2 === 'live' && _sharedChase[^)]*\)\s*\{[\s\S]*?kickChasePoll\(\);[\s\S]*?\n {6}\}/)?.[0] ?? '';
    expect(callSite, 'expected a gated kickChasePoll() call site').not.toBe('');
  });
});
