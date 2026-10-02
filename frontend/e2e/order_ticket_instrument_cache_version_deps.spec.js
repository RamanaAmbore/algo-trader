/**
 * order_ticket_instrument_cache_version_deps.spec.js
 *
 * D2-class fix (2026-10) — several `$derived.by(...)` blocks in
 * OrderTicket.svelte read `getInstrument()` / `listFutures()` /
 * `listExpiries()` / `listStrikes()` / `findOption()` /
 * `findNearestFuture()` / `listExchangesForSymbol()`, all of which read
 * the module-level instruments index (`instruments.js`). That index is a
 * plain `let`, invisible to Svelte's reactivity on its own — any
 * `$derived` block that touches it WITHOUT also reading
 * `$instrumentsCacheVersion` (the writable bump signal `instruments.js`
 * increments once the cache populates) computes ONCE at first access and
 * never re-fires, even after the cache warms up moments later. Confirmed
 * cold-cache symptoms before this fix: `_tickSize` frozen at the 0.05
 * NSE-equity default forever for MCX commodities (wrong tick → Kite
 * "invalid price" rejection), `_resolvedExchange` frozen at the generic
 * 'NSE' fallback forever for MCX/CDS contracts (wrong `?exchange=` on
 * the OrderDepth quote poll).
 *
 * `LegLabel.svelte` already has the correct, working pattern (see its
 * `_virtualLabel` / `monthDisplay` blocks) — this fix threads the exact
 * same one-line dependency read into every affected OrderTicket.svelte
 * block.
 *
 * Testing approach: this codebase has no jsdom/testing-library wired
 * into Vitest (environment: 'node', no Svelte component rendering), so
 * per existing precedent for structural guards in this file (see
 * order_ticket_cold_cache_close_qty.spec.js for the live-DOM sibling
 * covering the SAME file's D2-class qty bug, and
 * crudeoil_add_sell.spec.js's "3-Stale" bundle-grep test), this spec
 * asserts directly against the SOURCE FILE: every `$derived.by` block
 * that reads the instruments index also reads `$instrumentsCacheVersion`
 * inside its own body. This is deliberately structural coverage, not a
 * live-DOM behavioral assertion — each fenced block below is sliced by
 * name so the check is scoped to the ACTUAL changed lines, not a single
 * whole-file grep count.
 *
 * Run:
 *   npx playwright test e2e/order_ticket_instrument_cache_version_deps.spec.js \
 *   --project=chromium-desktop --workers=1
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const SRC = readFileSync(
  new URL('../src/lib/order/OrderTicket.svelte', import.meta.url),
  'utf-8'
);

/**
 * Slice the source from a `const <name> = $derived.by(() => {` opener to
 * its matching closing `});` (brace-depth tracked, naive but sufficient
 * for this file's formatting) so each assertion is scoped to ONE block.
 * @param {string} name
 * @returns {string}
 */
function sliceDerivedBlock(name) {
  const re = new RegExp(`const ${name} = \\$derived\\.by\\(\\(\\) => \\{`);
  const m = SRC.match(re);
  expect(m, `could not locate "const ${name} = $derived.by(...)" in OrderTicket.svelte`).toBeTruthy();
  const start = m.index;
  let depth = 0;
  let i = start;
  let seenOpen = false;
  for (; i < SRC.length; i++) {
    if (SRC[i] === '{') { depth++; seenOpen = true; }
    else if (SRC[i] === '}') {
      depth--;
      if (seenOpen && depth === 0) { i++; break; }
    }
  }
  return SRC.slice(start, i);
}

const BLOCKS_READING_INSTRUMENTS_CACHE = [
  'kind',
  '_expiryChoices',
  '_strikeChoices',
  '_resolvedSymbol',
  '_resolvedExchange',
  'exchangeOptions',
  '_tickSize',
  '_dte',
  '_strike',
  '_optType',
];

test.describe('OrderTicket.svelte — $derived blocks reading the instruments cache depend on $instrumentsCacheVersion', () => {
  for (const name of BLOCKS_READING_INSTRUMENTS_CACHE) {
    test(`"${name}" reads $instrumentsCacheVersion inside its own $derived.by body`, () => {
      const block = sliceDerivedBlock(name);
      expect(
        block.includes('$instrumentsCacheVersion'),
        `"${name}" block reads the instruments cache (getInstrument/listFutures/etc) ` +
        `but does not read $instrumentsCacheVersion — it will freeze at its ` +
        `pre-cache-warm value forever once computed. Block body:\n${block}`
      ).toBe(true);
    });
  }

  test('sanity: _underlyingSnap (liveSnap-based, already correctly reactive) is untouched by this fix', () => {
    // This block is deliberately NOT in the list above — liveSnap() is
    // tick-reactive via symbolStore.svelte.js's own mechanism, unrelated
    // to the instruments-cache-version bug class. Confirms the fix was
    // scoped correctly and didn't touch an unrelated, already-working block.
    const re = /const _underlyingSnap\s*=\s*\$derived\(_rootSym \? liveSnap\(_rootSym\) : null\);/;
    expect(re.test(SRC)).toBe(true);
  });

  test('instrumentsCacheVersion is imported from $lib/data/instruments', () => {
    expect(SRC.includes('instrumentsCacheVersion')).toBe(true);
    expect(/import\s*\{[^}]*instrumentsCacheVersion[^}]*\}\s*from\s*['"]\$lib\/data\/instruments['"]/.test(SRC))
      .toBe(true);
  });
});
