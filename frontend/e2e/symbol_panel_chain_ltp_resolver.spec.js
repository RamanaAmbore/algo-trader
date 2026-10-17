/**
 * symbol_panel_chain_ltp_resolver.spec.js
 *
 * Regression guard for a 2026-10-17 operator-reported fix ("when chain
 * is pressed the ltp field is getting erased... show ltp of underlying
 * in its place"): on the Chain tab, `SymbolPanel.svelte`'s `_ltpSym`
 * used to be the BARE root (`_parseRoot(_localSymbol)`, e.g. "NIFTY")
 * fed directly into `liveSnap()`. That's not itself a live-ticked quote
 * key for an index underlying (Kite ticks it under "NIFTY 50") or a
 * cold/unheld MCX commodity (no virtual-root alias published yet) —
 * `liveSnap(bareRoot)` silently resolved to null, erasing the `.oes-tab-
 * ltp` field the whole time Chain was active.
 *
 * Fix: resolve the root through `resolveUnderlyingTradingsymbol()` (the
 * SAME SSOT Snapshot/Payoff/NavStrip already use — see CLAUDE.md
 * "Underlying spot resolution") before calling `liveSnap()`, falling
 * back to the bare root so today's working case (a commodity WITH a
 * published virtual-root alias) stays unaffected. A `$derived.by` that
 * reads `findNearestFuture()` (a non-reactive module-level cache) must
 * also read `$instrumentsCacheVersion` or it freezes at whatever it
 * resolved on first access — same D2-class bug class covered by
 * `order_ticket_instrument_cache_version_deps.spec.js`'s precedent,
 * which this spec follows: a structural source-check (no jsdom/
 * testing-library wired into Vitest for this codebase, and a live-DOM
 * assertion would need a real index tick under "NIFTY 50" to be
 * present — flaky outside live market hours). The resolver function
 * itself (`resolveUnderlyingTradingsymbol`/`pickUnderlyingSpot`) is
 * already unit-tested in `resolveUnderlying.test.js` /
 * `resolveUnderlyingMcx.test.js` — this spec only guards that
 * SymbolPanel actually calls it correctly.
 *
 * Run:
 *   npx playwright test e2e/symbol_panel_chain_ltp_resolver.spec.js \
 *   --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const SRC = readFileSync(
  new URL('../src/lib/SymbolPanel.svelte', import.meta.url),
  'utf-8'
);

function sliceDerivedBlock(/** @type {string} */ name) {
  const re = new RegExp(`const ${name} = \\$derived(?:\\.by)?\\(`);
  const m = SRC.match(re);
  expect(m, `could not locate "const ${name} = $derived..." in SymbolPanel.svelte`).toBeTruthy();
  const start = m.index;
  let depth = 0;
  let i = start;
  let seenOpen = false;
  for (; i < SRC.length; i++) {
    if (SRC[i] === '(') { depth++; seenOpen = true; }
    else if (SRC[i] === ')') {
      depth--;
      if (seenOpen && depth === 0) { i++; break; }
    }
  }
  return SRC.slice(start, i);
}

test.describe('SymbolPanel Chain-tab LTP resolution (2026-10-17 fix)', () => {
  test('imports resolveUnderlyingTradingsymbol + findNearestFuture + instrumentsCacheVersion', () => {
    expect(SRC).toMatch(/import\s*\{\s*resolveUnderlyingTradingsymbol\s*\}\s*from\s*['"]\$lib\/data\/resolveUnderlying\.js['"]/);
    expect(SRC).toMatch(/import\s*\{\s*findNearestFuture,\s*instrumentsCacheVersion\s*\}\s*from\s*['"]\$lib\/data\/instruments\.js['"]/);
  });

  test('_ltpSym resolves the Chain-tab root through resolveUnderlyingTradingsymbol, not the bare root alone', () => {
    const block = sliceDerivedBlock('_ltpSym');
    expect(block).toMatch(/resolveUnderlyingTradingsymbol\(root,\s*findNearestFuture\)/);
    // Bare-root fallback must still exist (today's working MCX
    // virtual-root-alias case must not regress).
    expect(block).toMatch(/\|\|\s*root/);
  });

  test('_ltpSym depends on $instrumentsCacheVersion (D2-class cache-reactivity guard)', () => {
    const block = sliceDerivedBlock('_ltpSym');
    expect(block).toMatch(/\$instrumentsCacheVersion/);
  });

  test('_ltp falls back to the bare-root liveSnap when the resolved tradingsymbol has no live tick yet', () => {
    const idx = SRC.indexOf('const _ltp = $derived(');
    expect(idx, 'could not locate "const _ltp = $derived(..." in SymbolPanel.svelte').toBeGreaterThan(-1);
    let depth = 0, i = idx + 'const _ltp = $derived('.length - 1, seenOpen = false;
    for (; i < SRC.length; i++) {
      if (SRC[i] === '(') { depth++; seenOpen = true; }
      else if (SRC[i] === ')') { depth--; if (seenOpen && depth === 0) { i++; break; } }
    }
    const block = SRC.slice(idx, i);
    expect(block).toMatch(/liveSnap\(_ltpSym\)\?\.ltp/);
    expect(block).toMatch(/liveSnap\(_parseRoot\(_localSymbol\)\s*\|\|\s*_localSymbol\)\?\.ltp/);
  });
});
