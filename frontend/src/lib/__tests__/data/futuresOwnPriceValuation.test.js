/**
 * futuresOwnPriceValuation.test.js
 *
 * §4 (derivatives Exp P&L / futures valuation unification): a future
 * position/leg's Exp P&L must use ITS OWN contract's live price, not the
 * root's front-month resolution — these only coincide when the held future
 * IS the front-month contract.
 *
 * Fix (item-9 audit): this file previously tested a LOCAL MIRROR
 * (`resolveExpiryAnchor` defined inline in this test file), not the
 * shipped code — a regression in either call site's actual logic could
 * pass this suite while the real bug reappeared. The decision tree is now
 * extracted as a real, shared, exported pure function —
 * `resolveExpiryAnchor` in `$lib/data/expiryPnl.js` — imported directly
 * here AND called from both production call sites:
 *   - portfolioStore.svelte.js's _posTier2 (exp_pnl/extrinsic block)
 *   - derivatives/+page.svelte's _legExpPnlDisplay(c, spot)
 * expiryPnl.js is a plain .js module (no Svelte runes), so it imports
 * cleanly into Vitest without the local-mirror workaround
 * positionsDerivedStore.test.js / portfolioStore.test.js still use for
 * their $state/$derived-heavy modules.
 *
 * Five quality dimensions:
 *   1. SSOT   — imports and exercises the actual shared exported function,
 *               not a hand-copied mirror
 *   2. Perf   — pure synchronous function, no DOM / network
 *   3. Stale  — guards against silently reverting to root-spot-for-futures;
 *               structural checks confirm BOTH call sites import and call
 *               resolveExpiryAnchor (not a re-inlined copy)
 *   4. Reuse  — parameterised over isOpt so options' unchanged behaviour is
 *               also covered (no regression on the options path)
 *   5. UX     — a far-month future's Exp P&L must diverge from front-month
 *               when its own price differs (the exact bug being fixed)
 */

import { describe, it, expect } from 'vitest';
import { resolveExpiryAnchor } from '$lib/data/expiryPnl.js';

describe('futures own-price valuation — anchor selection (§4, resolveExpiryAnchor)', () => {
  it('front-month future: own live price happens to equal root spot — no visible divergence', () => {
    const anchor = resolveExpiryAnchor({
      isOpt: false, rootSpot: 100, ownLiveLtp: 100, ownPolledLtp: 100,
    });
    expect(anchor).toBe(100);
  });

  it('far-month future in contango: own live price is HIGHER than root spot — uses own price, not root', () => {
    // e.g. CRUDEOIL front-month spot=5800, but the held far-month contract
    // trades at 5900 (contango) — Exp P&L must price at 5900, not 5800.
    const anchor = resolveExpiryAnchor({
      isOpt: false, rootSpot: 5800, ownLiveLtp: 5900, ownPolledLtp: 5895,
    });
    expect(anchor).toBe(5900);
    expect(anchor).not.toBe(5800);
  });

  it('far-month future in backwardation: own live price is LOWER than root spot — uses own price', () => {
    const anchor = resolveExpiryAnchor({
      isOpt: false, rootSpot: 6500, ownLiveLtp: 6400, ownPolledLtp: 6410,
    });
    expect(anchor).toBe(6400);
    expect(anchor).not.toBe(6500);
  });

  it('no live tick yet for the own contract: falls back to own POLLED ltp, not root spot', () => {
    const anchor = resolveExpiryAnchor({
      isOpt: false, rootSpot: 5800, ownLiveLtp: 0, ownPolledLtp: 5850,
    });
    expect(anchor).toBe(5850);
    expect(anchor).not.toBe(5800);
  });

  it('no live tick AND no own polled ltp: falls back to root spot (last resort, e.g. cold MCX cache)', () => {
    const anchor = resolveExpiryAnchor({
      isOpt: false, rootSpot: 5800, ownLiveLtp: 0, ownPolledLtp: 0,
    });
    expect(anchor).toBe(5800);
  });

  it('options are unaffected: always use root spot regardless of any "own price" signal', () => {
    const anchor = resolveExpiryAnchor({
      isOpt: true, rootSpot: 23150, ownLiveLtp: 999999, ownPolledLtp: 888888,
    });
    expect(anchor).toBe(23150);
  });

  it('ownLiveLtp/ownPolledLtp default to 0 when omitted (futures, no signal at all → root spot)', () => {
    const anchor = resolveExpiryAnchor({ isOpt: false, rootSpot: 5800 });
    expect(anchor).toBe(5800);
  });
});

// ── Cross-surface regression guard: reads the actual source to confirm
// BOTH call sites import and call the SHARED resolveExpiryAnchor function
// (not a re-inlined copy of the decision tree) — a structural check
// standing in for a Svelte-runtime integration test this harness can't
// run directly against the $state/$derived-heavy .svelte.js/.svelte files.
describe('futures own-price valuation — landed in both call sites via the shared function (structural)', () => {
  it('portfolioStore.svelte.js: imports resolveExpiryAnchor and calls it in the futures branch', async () => {
    // Vite `?raw` import — reads the source as a plain string without
    // Node's `fs`/`path` (not available under this project's Vitest node
    // types config).
    // @ts-ignore — Vite raw-import suffix has no TS module declaration here.
    const { default: src } = await import('$lib/data/portfolioStore.svelte.js?raw');
    expect(src).toMatch(/import\s*\{[^}]*\bresolveExpiryAnchor\b[^}]*\}\s*from\s*'\$lib\/data\/expiryPnl\.js'/);
    expect(src).toMatch(/resolveExpiryAnchor\(\{\s*isOpt,\s*rootSpot:\s*spot,\s*ownLiveLtp:\s*futLive/);
    expect(src).toMatch(/liveSnap\(p\._sym\)/);
  });

  it('derivatives/+page.svelte: _legExpPnlDisplay imports and calls resolveExpiryAnchor for fut-kind legs', async () => {
    // @ts-ignore — Vite raw-import suffix has no TS module declaration here.
    const { default: src } = await import(
      /* @vite-ignore */ '../../../routes/(algo)/admin/derivatives/+page.svelte?raw'
    );
    expect(src).toMatch(/import\s*\{[^}]*\bresolveExpiryAnchor\b[^}]*\}\s*from\s*'\$lib\/data\/expiryPnl'/);
    const fnMatch = src.match(/function _legExpPnlDisplay\(c, spot\) \{[\s\S]*?\n  \}/);
    expect(fnMatch).not.toBeNull();
    expect(fnMatch[0]).toMatch(/c\.kind === 'fut'/);
    expect(fnMatch[0]).toMatch(/liveSnap\(/);
    expect(fnMatch[0]).toMatch(/resolveExpiryAnchor\(\{\s*isOpt:\s*false/);
  });
});
