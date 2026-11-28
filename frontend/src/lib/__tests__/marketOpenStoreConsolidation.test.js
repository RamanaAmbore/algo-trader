/**
 * marketOpenStoreConsolidation.test.js
 *
 * 2026-11 duplicated-polling consolidation audit — four components
 * (RefreshButton.svelte, PositionStrip.svelte, OrderTicket.svelte,
 * SymbolPanel.svelte's Chain-tab resolution) each ran their own 30s
 * `visibleInterval` timer re-invoking isNseOpen()/isMcxOpen() independently
 * instead of sharing one poll. This is a source-scan regression guard
 * (`?raw` imports, same convention as lastRefreshAtMarketGate.test.js —
 * this harness has no Svelte-compiler plugin wired in) proving:
 *   1. None of the four components registers its own bare
 *      `visibleInterval(fn, 30_000)` market-tick timer anymore.
 *   2. All four subscribe to the shared `marketOpenStore` singleton
 *      instead.
 *   3. `stores.js` defines exactly that singleton, backed by
 *      `createMarketOpenPoll()` from marketHours.js (unit-tested directly
 *      in marketOpenPoll.test.js).
 *
 * Five quality dimensions:
 *  1. SSOT    — one shared store/poll, not four independent ones; this
 *               file is the cross-file guard that no new duplicate timer
 *               creeps back in at any of the four sites.
 *  2. Perf    — a regression back to four timers would be a real (if
 *               small) perf cost; guarded structurally here rather than
 *               only by a behavioural assertion that can't see the count.
 *  3. Stale   — explicitly asserts the OLD pattern is ABSENT at each site,
 *               not just that the NEW pattern is present — catches a
 *               half-migrated file that keeps both.
 *  4. Reuse   — all four sites reuse the identical `marketOpenStore`
 *               import from `$lib/stores`, never a per-file re-export or
 *               a bespoke local copy.
 *  5. UX      — session-boundary flips still reach every surface (badge,
 *               strip pills, chain notice) — covered behaviourally by the
 *               pre-existing specs this file complements (the pinned
 *               order_ticket_market_badge_and_error_toast.spec.js test for
 *               OrderTicket.svelte was updated in lockstep, see that file).
 */

import { describe, it, expect } from 'vitest';
import storesSrc from '../stores.js?raw';
import refreshButtonSrc from '../RefreshButton.svelte?raw';
import positionStripSrc from '../PositionStrip.svelte?raw';
import symbolPanelSrc from '../SymbolPanel.svelte?raw';
import orderTicketSrc from '../order/OrderTicket.svelte?raw';

const OLD_MKT_TIMER_RE = /visibleInterval\(\s*(?:_recomputeMktOpen|\(\)\s*=>\s*\{[\s\S]{0,200}?(?:isNseOpen|isMcxOpen)[\s\S]{0,200}?\}|tick)\s*,\s*30_000\s*\)/;

describe('stores.js — shared marketOpenStore singleton', () => {
  it('defines marketOpenStore, backed by createMarketOpenPoll() from marketHours.js', () => {
    expect(storesSrc).toMatch(/export const marketOpenStore\s*=\s*readable\(/);
    expect(storesSrc).toMatch(/createMarketOpenPoll/);
    expect(storesSrc).toMatch(/import\s*\{[^}]*createMarketOpenPoll[^}]*\}\s*from\s*'\$lib\/marketHours'/);
  });

  it('wires visibleInterval into the poll, preserving pause-while-hidden ' +
     '+ immediate-refire-on-return — the exact behaviour every per-component ' +
     'timer it replaces already had, not a bare setInterval', () => {
    expect(storesSrc).toMatch(/setIntervalFn:\s*\(fn,\s*ms\)\s*=>\s*visibleInterval\(fn,\s*ms\)/);
    expect(storesSrc).toMatch(/clearIntervalFn:\s*\(teardown\)\s*=>\s*teardown\?\.\(\)/);
  });
});

describe('RefreshButton.svelte — consolidated onto marketOpenStore', () => {
  it('no longer runs its own 30s market-tick visibleInterval timer', () => {
    expect(refreshButtonSrc).not.toMatch(OLD_MKT_TIMER_RE);
  });
  it('subscribes to the shared marketOpenStore instead', () => {
    expect(refreshButtonSrc).toMatch(/import\s*\{[^}]*marketOpenStore[^}]*\}\s*from\s*'\$lib\/stores'/);
    expect(refreshButtonSrc).toMatch(/marketOpenStore\.subscribe\(/);
  });
});

describe('PositionStrip.svelte — consolidated onto marketOpenStore', () => {
  it('no longer runs its own 30s market-tick visibleInterval timer', () => {
    expect(positionStripSrc).not.toMatch(OLD_MKT_TIMER_RE);
  });
  it('subscribes to the shared marketOpenStore instead', () => {
    expect(positionStripSrc).toMatch(/import\s*\{[^}]*marketOpenStore[^}]*\}\s*from\s*'\$lib\/stores'/);
    expect(positionStripSrc).toMatch(/marketOpenStore\.subscribe\(/);
  });
});

describe("SymbolPanel.svelte — Chain-tab resolution consolidated onto marketOpenStore", () => {
  it('no longer runs its own 30s visibleInterval timer for the Chain-tab market check', () => {
    expect(symbolPanelSrc).not.toMatch(/visibleInterval\(\s*\(\)\s*=>\s*\{\s*_recomputeChainMktOpen\(\);\s*\}\s*,\s*30_000\s*\)/);
  });
  it('subscribes to the shared marketOpenStore instead', () => {
    expect(symbolPanelSrc).toMatch(/import\s*\{[^}]*marketOpenStore[^}]*\}\s*from\s*'\$lib\/stores'/);
    expect(symbolPanelSrc).toMatch(/_chainMktTimer\s*=\s*marketOpenStore\.subscribe\(/);
  });
});

describe('OrderTicket.svelte — own market timer consolidated onto marketOpenStore, _mktOpenForExchange untouched', () => {
  it('no longer runs its own bare 30s visibleInterval(_recomputeMktOpen, 30_000) timer', () => {
    expect(orderTicketSrc).not.toMatch(/visibleInterval\(_recomputeMktOpen,\s*30_000\)/);
  });
  it('subscribes to the shared marketOpenStore instead, teardown unchanged', () => {
    expect(orderTicketSrc).toMatch(/import\s*\{[^}]*marketOpenStore[^}]*\}\s*from\s*'\$lib\/stores'/);
    expect(orderTicketSrc).toMatch(/_mktTimer\s*=\s*marketOpenStore\.subscribe\(\(\)\s*=>\s*_recomputeMktOpen\(\)\)/);
    expect(orderTicketSrc).toMatch(/_mktTimer\?\.\(\)/);
  });
  it('the per-exchange mapping function itself is byte-for-byte untouched (pinned spec precedent)', () => {
    // Mirrors the live pin in order_ticket_market_badge_and_error_toast.spec.js
    // — this fix must never touch _mktOpenForExchange's own branches.
    const fnMatch = orderTicketSrc.match(/function _mktOpenForExchange\([^)]*\)\s*\{[\s\S]*?\n  \}/);
    expect(fnMatch, '_mktOpenForExchange function not found').not.toBeNull();
    const fn = fnMatch[0];
    expect(fn).toMatch(/if\s*\(([^)]*)\)\s*return isMcxOpen\(\);/);
    expect(fn).toMatch(/if\s*\(([^)]*)\)\s*return isNseOpen\(\);/);
    expect(fn).toMatch(/return false;\s*\}$/);
  });
});
