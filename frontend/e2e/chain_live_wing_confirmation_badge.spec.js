/**
 * chain_live_wing_confirmation_badge.spec.js
 *
 * Chain tab's read-only "wing fired live" confirmation badge. When a
 * Bracket template's wing/hedge leg is placed LIVE — asynchronously, on
 * PARENT FILL, by the backend's `_fire_template_attach_on_fill` ->
 * `apply_template_to_order` -> `_chase_wing` (template_attach.py), never
 * at Submit time — the Chain strike grid shows a small read-only badge
 * on that wing's own strike/opt-type cell so the operator can see "yes,
 * this actually fired" without leaving Chain.
 *
 * This is purely additive/display. It must NEVER let the wing leg
 * re-enter the pre-submit staging array (`chainBasket`/`basketLegs`) —
 * a confirmed-live wing leg ending up there could get re-placed as a
 * duplicate live order on the next Submit click.
 *
 * Why this spec is a hybrid of real-module execution + source-scan,
 * matching the established pattern for this exact component (see
 * chain_atm_softening_and_leg_badge.spec.js, option_chain_order.spec.js,
 * order_ticket_bracket_scope_ssot_and_clear_policy.spec.js — all either
 * source-scan-only or extract-and-execute real logic rather than
 * reimplementing it): the Chain tab needs live quotes + a logged-in
 * session + a real template catalog to even render an interactive
 * strike grid, which this suite's existing specs document as
 * impractical in CI (`chain_atm_softening_and_leg_badge.spec.js`'s own
 * header: "staging logic itself is covered here structurally"). The
 * matching/pruning logic itself (`computeLiveWingState`,
 * `liveWingBadge.js`) is plain, Svelte-independent JS — it is imported
 * and RUN here directly (the real production function, not a
 * reimplementation), which is a stronger guarantee than a pure source
 * grep for the behavioural assertions (#1, #2 below). The component
 * wiring (`_liveWingByKey` vs `_basketLegByKey` separation, the +/-
 * buttons staying untouched, the submit-path tracking gate) is
 * source-scanned (#3, #4 below), since those are structural properties
 * of the shipped files, not standalone functions.
 *
 * Covers:
 *  1. Stage-and-submit-mock -> poll-mock -> badge: a wing order whose
 *     `parent_order_id` matches the just-submitted parent's resolved
 *     algo id produces a badge on that wing's OWN strike/optType cell.
 *  2. The wing's strike/optType row shows the LIVE badge, never the
 *     staged-leg badge — and a take-profit child sharing the same
 *     `parent_order_id` (the real discriminator risk: `parent_order_id`
 *     is not wing-exclusive) never produces one.
 *  3. `chainBasket`/`basketLegs` never contains an entry for the wing
 *     leg at any point — proven both structurally (the resolver takes
 *     no basket array at all) and via source-scan of the real
 *     component wiring.
 *  4. `_liveWingByKey` and `_basketLegByKey` are genuinely separate
 *     variables, not aliases of the same data, and the staging
 *     functions (`_pushToBasket`/`_mergeIntoBasket`/`_netAgainstBasket`/
 *     `addOptionToBasket`) never read `_liveWingByKey` — so +/- on a
 *     live-wing-badged cell behaves exactly as if nothing were staged.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';
import { computeLiveWingState, WING_TRACK_PRUNE_MS } from '../src/lib/data/liveWingBadge.js';
import { decomposeSymbol } from '../src/lib/data/decomposeSymbol.js';

const DIR = import.meta.dirname ?? new URL('.', import.meta.url).pathname;
const CHAIN_TAB_PATH   = path.resolve(DIR, '../src/lib/order/OptionChainTab.svelte');
const SYMBOL_PANEL_PATH = path.resolve(DIR, '../src/lib/SymbolPanel.svelte');
const POLLER_PATH      = path.resolve(DIR, '../src/lib/data/orderFillPoller.js');
const BADGE_PATH       = path.resolve(DIR, '../src/lib/data/liveWingBadge.js');
const TRACKER_PATH     = path.resolve(DIR, '../src/lib/data/liveWingTracker.svelte.js');

function findOptionStub(scopeMap) {
  return (underlying, optType, strike, expiry) => {
    const sym = scopeMap[`${underlying}|${expiry}|${strike}:${optType}`];
    return sym ? { s: sym, ls: 75 } : null;
  };
}
function getInstrumentStub(lotSizes) {
  return (sym) => (lotSizes[sym] != null ? { ls: lotSizes[sym] } : null);
}

/** Strip /* ... *\/ block comments before a containment check — these
 *  two modules' own doc comments legitimately NAME chainBasket/
 *  basketLegs while explaining that the real CODE never touches them;
 *  the structural guarantee this spec checks is about executable code,
 *  not prose. */
function stripBlockComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '');
}

test.describe('Chain live-wing badge — functional (real production resolver)', () => {
  const NOW = 1_800_000_000_000;

  // Simulates: operator submits a Bracket order on Chain for
  // BANKNIFTY26NOV50000CE; the mocked basket-submit response returns the
  // broker's order_id; a later mocked orders-poll response shows the
  // async wing leg (BANKNIFTY26NOV49600PE, parent_order_id = the
  // resolved parent algo id) now OPEN.
  const brokerOrderIdFromSubmitResponse = '990011223344';
  const parentRowFromPoll = {
    id: 777, account: 'ZG0790', symbol: 'BANKNIFTY26NOV50000CE',
    broker_order_id: brokerOrderIdFromSubmitResponse,
    superseded_broker_order_ids: [], source: null, parent_order_id: null,
    status: 'OPEN',
  };
  const wingRowFromPoll = {
    id: 778, account: 'ZG0790', symbol: 'BANKNIFTY26NOV49600PE',
    parent_order_id: 777, source: 'template_wing',
    transaction_type: 'SELL', quantity: 75, status: 'OPEN',
  };
  // Decoy — a take-profit CHILD of the exact same parent. Must never
  // badge: `parent_order_id` alone is not wing-exclusive (TP children
  // use it too), the discriminator is `source === 'template_wing'`.
  const takeProfitDecoy = {
    id: 779, account: 'ZG0790', symbol: 'BANKNIFTY26NOV50000CE', // parent's own contract
    parent_order_id: 777, source: 'take_profit',
    transaction_type: 'SELL', quantity: 75, status: 'OPEN',
  };

  const findOption = findOptionStub({
    'BANKNIFTY|2026-11-25|49600:PE': 'BANKNIFTY26NOV49600PE',
  });
  const getInstrument = getInstrumentStub({ BANKNIFTY26NOV49600PE: 75 });

  test('1. tracked parent + mocked poll row -> badge at the wing\'s own strike/optType key', () => {
    const trackedParents = [{
      account: 'ZG0790', symbol: 'BANKNIFTY26NOV50000CE',
      brokerOrderId: brokerOrderIdFromSubmitResponse,
      templateName: 'Hedge-Bracket', submittedAt: NOW, lastSeenAt: NOW,
    }];
    const { badgeMap } = computeLiveWingState({
      trackedParents,
      algoRows: [parentRowFromPoll, wingRowFromPoll, takeProfitDecoy],
      chainUnderlying: 'BANKNIFTY', chainExpiry: '2026-11-25',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });

    // 2. Badge lands on the WING's own cell (49600 PE) …
    const wingBadge = badgeMap.get('49600:PE');
    expect(wingBadge).toBeTruthy();
    expect(wingBadge.lots).toBe(1); // 75 qty / 75 lot size
    expect(wingBadge.side).toBe('SELL');
    expect(wingBadge.templateName).toBe('Hedge-Bracket');
    expect(wingBadge.algoOrderId).toBe(778);

    // … and ONLY there — the parent's own strike (50000 CE, where the
    // TP decoy also lives) gets no live-wing badge at all.
    expect(badgeMap.has('50000:CE')).toBe(false);
    expect(badgeMap.size).toBe(1);
  });

  test('a wing resolved to a dead status (rejected) never produces a badge', () => {
    const trackedParents = [{
      account: 'ZG0790', symbol: 'BANKNIFTY26NOV50000CE',
      brokerOrderId: brokerOrderIdFromSubmitResponse,
      templateName: 'Hedge-Bracket', submittedAt: NOW, lastSeenAt: NOW,
    }];
    const { badgeMap, trackedParents: next } = computeLiveWingState({
      trackedParents,
      algoRows: [parentRowFromPoll, { ...wingRowFromPoll, status: 'REJECTED' }],
      chainUnderlying: 'BANKNIFTY', chainExpiry: '2026-11-25',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(badgeMap.size).toBe(0);
    expect(next).toHaveLength(0); // tracking dropped, not left to expire on a timer
  });

  test('a stale, never-matched tracked parent is pruned after WING_TRACK_PRUNE_MS', () => {
    const longAgo = NOW - WING_TRACK_PRUNE_MS - 1;
    const trackedParents = [{
      account: 'ZG0790', symbol: 'BANKNIFTY26NOV50000CE',
      brokerOrderId: 'never-seen-again', templateName: 'Hedge-Bracket',
      submittedAt: longAgo, lastSeenAt: longAgo,
    }];
    const { trackedParents: next } = computeLiveWingState({
      trackedParents, algoRows: [],
      chainUnderlying: 'BANKNIFTY', chainExpiry: '2026-11-25',
      decompose: decomposeSymbol, findOption, getInstrument, nowMs: NOW,
    });
    expect(next).toHaveLength(0);
  });
});

test.describe('Chain live-wing badge — wiring + staging-array isolation (source-scan)', () => {
  let chainSrc = '';
  let panelSrc = '';
  let pollerSrc = '';
  let badgeSrc = '';
  let trackerSrc = '';

  test.beforeAll(() => {
    chainSrc   = readFileSync(CHAIN_TAB_PATH, 'utf8');
    panelSrc   = readFileSync(SYMBOL_PANEL_PATH, 'utf8');
    pollerSrc  = readFileSync(POLLER_PATH, 'utf8');
    badgeSrc   = readFileSync(BADGE_PATH, 'utf8');
    trackerSrc = readFileSync(TRACKER_PATH, 'utf8');
  });

  // ── Requirement 4: genuinely separate maps, not aliases ────────────
  test('_liveWingByKey and _basketLegByKey are declared as two independent derivations', () => {
    expect(chainSrc).toMatch(/let _liveWingByKey = \$state\(new Map\(\)\);/);
    expect(chainSrc).toMatch(/const _basketLegByKey = \$derived\.by\(/);
    // Never assigned from one another.
    expect(chainSrc).not.toMatch(/_liveWingByKey\s*=\s*_basketLegByKey/);
    expect(chainSrc).not.toMatch(/_basketLegByKey\s*=\s*_liveWingByKey/);
  });

  test('the resolver module (liveWingBadge.js) never references chainBasket/basketLegs/_localBasket in executable code', () => {
    const code = stripBlockComments(badgeSrc);
    expect(code).not.toContain('chainBasket');
    expect(code).not.toContain('basketLegs');
    expect(code).not.toContain('_localBasket');
    // It does not even accept a basket array as a parameter — proves
    // requirement 3 structurally: there is nothing a wing match could
    // write into.
    expect(code).not.toMatch(/function computeLiveWingState\([^)]*basket/i);
  });

  test('the session tracker module never references chainBasket/basketLegs/_localBasket in executable code', () => {
    const code = stripBlockComments(trackerSrc);
    expect(code).not.toContain('chainBasket');
    expect(code).not.toContain('basketLegs');
    expect(code).not.toContain('_localBasket');
  });

  // ── Requirement 4 (continued): staging functions never read the live-wing map ──
  for (const fn of ['_pushToBasket', '_mergeIntoBasket', '_netAgainstBasket', 'addOptionToBasket']) {
    test(`${fn}() in OptionChainTab.svelte never references _liveWingByKey`, () => {
      const start = chainSrc.indexOf(`function ${fn}(`);
      expect(start, `${fn} must exist`).toBeGreaterThan(-1);
      // Grab a generous slice — these are all short functions in this file.
      const body = chainSrc.slice(start, start + 2000);
      const closeIdx = body.indexOf('\n  }\n');
      const fnBody = closeIdx > -1 ? body.slice(0, closeIdx) : body;
      expect(fnBody).not.toContain('_liveWingByKey');
    });
  }

  // The +/- buttons must be untouched — they still unconditionally call
  // addOptionToBasket regardless of whether a live-wing badge is showing
  // on that cell, i.e. a live-wing-badged cell behaves exactly like an
  // empty one for staging purposes.
  test('CE/PE +/- buttons are unconditional — no ceLive/peLive guard on the staging buttons', () => {
    const buyCe = chainSrc.match(/onclick=\{\(\) => addOptionToBasket\(k, 'CE', 'long'\)\}/g) || [];
    const sellCe = chainSrc.match(/onclick=\{\(\) => addOptionToBasket\(k, 'CE', 'short'\)\}/g) || [];
    expect(buyCe.length).toBe(2);  // ATM row + non-ATM row
    expect(sellCe.length).toBe(2);
    // None of the four button definitions reference ceLive/peLive.
    expect(chainSrc).not.toMatch(/disabled=\{[^}]*ceLive/);
    expect(chainSrc).not.toMatch(/disabled=\{[^}]*peLive/);
  });

  // ── Requirement 2: live badge renders in an else-branch, never alongside the staged badge ──
  test('the live-wing badge is an {:else if} off the staged-leg {#if}, for both CE and PE, both rows', () => {
    const ceBranches = [...chainSrc.matchAll(/\{#if ceLeg\}[\s\S]{0,700}?\{:else if ceLive\}/g)];
    const peBranches = [...chainSrc.matchAll(/\{#if peLeg\}[\s\S]{0,700}?\{:else if peLive\}/g)];
    expect(ceBranches.length).toBe(2);
    expect(peBranches.length).toBe(2);
  });

  test('.chain-leg-badge-live is a distinct CSS class from the staged .chain-leg-badge-tmpl', () => {
    expect(chainSrc).toMatch(/\.chain-leg-badge-live\s*\{/);
    expect(chainSrc).toContain('chain-leg-badge chain-leg-badge-live');
    // Visually distinct recipe, not a bare color swap of the staged class.
    const liveRule = chainSrc.match(/\.chain-leg-badge-live\s*\{[^}]*\}/)?.[0] ?? '';
    const tmplRule = chainSrc.match(/\.chain-leg-badge-tmpl\s*\{[^}]*\}/)?.[0] ?? '';
    expect(liveRule).not.toBe('');
    expect(liveRule).not.toBe(tmplRule);
  });

  // ── Submission-side wiring: tracked only on a real LIVE success ────
  test('SymbolPanel.submitBasket() tracks a submission only inside the success branch, gated on live mode', () => {
    expect(panelSrc).toContain("import { trackChainParentSubmission } from '$lib/data/liveWingTracker.svelte.js';");
    const callIdx = panelSrc.indexOf('trackChainParentSubmission({');
    expect(callIdx).toBeGreaterThan(-1);
    const guardSlice = panelSrc.slice(Math.max(0, callIdx - 400), callIdx);
    expect(guardSlice).toMatch(/basketMode2 === 'live'/);
    // Must sit inside the success (`ok++`) branch, not the failure branch.
    const okIdx = panelSrc.lastIndexOf('ok++;', callIdx);
    const failIdx = panelSrc.lastIndexOf('failedIdx.add(origIdx);', callIdx);
    expect(okIdx).toBeGreaterThan(failIdx);
  });

  // ── No new polling loop — reuse of the existing poll only ──────────
  test('OptionChainTab subscribes to the EXISTING poller store, no new interval/poll added', () => {
    expect(chainSrc).toContain("import { latestAlgoOrderRows } from '$lib/data/orderFillPoller.js';");
    expect(chainSrc).toContain('$latestAlgoOrderRows');
    // No direct network fetch for this feature inside OptionChainTab's
    // own live-wing effect — only the already-fetched store is read.
    // 2026-11-03: the call site now reads `const next = refreshLiveWingState(`
    // (guarded against an unnecessary reassign + wrapped in try/catch —
    // see that fix's own comment) rather than assigning directly into
    // `_liveWingByKey`, so match on the call itself, not the old
    // direct-assignment shape.
    const effectIdx = chainSrc.indexOf('refreshLiveWingState(');
    expect(effectIdx).toBeGreaterThan(-1);
    const effectSlice = chainSrc.slice(Math.max(0, effectIdx - 300), effectIdx + 100);
    expect(effectSlice).not.toMatch(/fetchAlgoOrdersRecent|fetchOrders\(/);
  });

  test('orderFillPoller.js sets latestAlgoOrderRows before the broker-failure early return (freeze-to-last-good)', () => {
    const setIdx = pollerSrc.indexOf('latestAlgoOrderRows.set(');
    const returnIdx = pollerSrc.indexOf("if (brokerResp.status !== 'fulfilled') return;");
    expect(setIdx).toBeGreaterThan(-1);
    expect(returnIdx).toBeGreaterThan(-1);
    expect(setIdx).toBeLessThan(returnIdx);
    // On a failed algo fetch, the store is left alone (no .set call in
    // that branch) — proven by only ONE latestAlgoOrderRows.set( call
    // in the whole module, guarded by the fulfilled+array check.
    const setCalls = pollerSrc.match(/latestAlgoOrderRows\.set\(/g) || [];
    expect(setCalls.length).toBe(1);
  });
});
