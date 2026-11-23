/**
 * optionChainTabSpreadGateWiring.test.js — pins `OptionChainTab.svelte`'s
 * `_buildGatePlan` to the extracted `buildSpreadGatePlan` helper
 * (spreadGate.js), instead of the old inline CE/PE-filter loop it used
 * to run directly.
 *
 * The component can't be mounted under this project's Vitest config (same
 * established limitation documented in `fundsAggregate.js`'s own header
 * comment — top-level Svelte runes need the compiler plugin, which isn't
 * registered here). A `?raw` source-grep of the real shipped file is the
 * established workaround (same convention as `legsViewMergedCheckboxWiring
 * .test.js`'s `+page.svelte?raw` scan and `fillWatchLayoutWiring.test.js`'s
 * `+layout.svelte?raw` scan) — the pure logic itself (`isCloseIntentLeg`/
 * `buildSpreadGatePlan`) is fully covered directly by spreadGate.test.js;
 * this file only guards that `OptionChainTab.svelte` actually calls it.
 */

import { describe, it, expect } from 'vitest';
// @ts-ignore — Vite raw-import suffix has no TS module declaration here.
import src from '$lib/order/OptionChainTab.svelte?raw';

describe('OptionChainTab.svelte — pre-submit spread gate wiring', () => {
  it('imports buildSpreadGatePlan from spreadGate.js', () => {
    expect(src).toMatch(/import\s*\{\s*createSpreadGate,\s*buildSpreadGatePlan\s*\}\s*from\s*['"]\$lib\/data\/spreadGate\.js['"]/);
  });

  it('imports portfolioStore from portfolioStore.svelte.js', () => {
    expect(src).toMatch(/import\s*\{\s*portfolioStore\s*\}\s*from\s*['"]\$lib\/data\/portfolioStore\.svelte\.js['"]/);
  });

  it('_buildGatePlan reads portfolioStore.positions.rows inside untrack(), and delegates to buildSpreadGatePlan — not the old inline CE/PE-filter loop', () => {
    const fnMatch = src.match(/function _buildGatePlan\(\) \{[\s\S]*?\n  \}/);
    expect(fnMatch).toBeTruthy();
    const body = fnMatch[0];

    expect(body).toMatch(/untrack\(\(\) => portfolioStore\.positions\.rows\)/);
    expect(body).toMatch(/buildSpreadGatePlan\(\s*chainBasket,\s*_account,\s*positionRows,\s*_effMaxSpreadPct\(\)\s*\)/);

    // The old inline implementation built `items` itself and pushed a
    // `{tradingsymbol: leg.sym, ...}` entry per CE/PE leg directly inside
    // this function — confirm that's genuinely gone, not just that the
    // new call was added alongside it.
    expect(body).not.toMatch(/items\.push\(\{\s*tradingsymbol:\s*leg\.sym/);
    expect(body).not.toMatch(/const\s+items\s*=\s*\[\]/);
  });
});
