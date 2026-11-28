/**
 * optionChainTabHedgeWiring.test.js — pins the Part-A hedge-leg wiring
 * inside `OptionChainTab.svelte`: `_maybeStageHedgeLeg` must call
 * `resolveHedgeStrike` and go straight to `_pushToBasket` on success
 * (never through `_mergeIntoBasket`/`_netAgainstBasket`, which exist
 * for manual basket-click dedup, not a fresh hedge push); and both of
 * those functions' match predicates must exclude any leg carrying
 * `_hedgeParentKey` from being a merge/net TARGET.
 *
 * Same `?raw` source-grep convention as
 * optionChainTabSpreadGateWiring.test.js — the component can't be
 * mounted under this project's Vitest config (top-level Svelte runes
 * need the compiler plugin, not registered here). The pure logic
 * itself (`resolveHedgeStrike`) is fully covered directly by
 * spreadGate.test.js; this file only guards that the component
 * actually wires it in correctly.
 */

import { describe, it, expect } from 'vitest';
// @ts-ignore — Vite raw-import suffix has no TS module declaration here.
import src from '$lib/order/OptionChainTab.svelte?raw';

describe('OptionChainTab.svelte — hedge-leg wiring (Part A)', () => {
  it('imports resolveHedgeStrike and isCloseIntentLeg from spreadGate.js', () => {
    const m = src.match(/import\s*\{([^}]*)\}\s*from\s*['"]\$lib\/data\/spreadGate\.js['"]/);
    expect(m).toBeTruthy();
    expect(m[1]).toMatch(/\bresolveHedgeStrike\b/);
    expect(m[1]).toMatch(/\bisCloseIntentLeg\b/);
  });

  it('declares hedgeOn/hedgeBuyPct/hedgeSellPct as local $state (not lifted props)', () => {
    expect(src).toMatch(/let hedgeOn\s*=\s*\$state\(false\)/);
    expect(src).toMatch(/let hedgeBuyPct\s*=\s*\$state\(50\)/);
    expect(src).toMatch(/let hedgeSellPct\s*=\s*\$state\(25\)/);
  });

  it('_maybeStageHedgeLeg calls resolveHedgeStrike and isCloseIntentLeg, and pushes straight to _pushToBasket — never through _mergeIntoBasket/_netAgainstBasket', () => {
    const fnMatch = src.match(/function _maybeStageHedgeLeg\([\s\S]*?\n  \}/);
    expect(fnMatch).toBeTruthy();
    const body = fnMatch[0];
    expect(body).toMatch(/resolveHedgeStrike\(/);
    expect(body).toMatch(/isCloseIntentLeg\(/);
    expect(body).toMatch(/_pushToBasket\(/);
    expect(body).not.toMatch(/_mergeIntoBasket\(/);
    expect(body).not.toMatch(/_netAgainstBasket\(/);
    expect(body).not.toMatch(/\baddOptionToBasket\(/);
  });

  it('_maybeStageHedgeLeg no-ops on a non-NFO exchange, on Place mode, and when Hedge is off', () => {
    const fnMatch = src.match(/function _maybeStageHedgeLeg\([\s\S]*?\n  \}/);
    const body = fnMatch[0];
    expect(body).toMatch(/if \(!hedgeOn\) return;/);
    expect(body).toMatch(/if \(_placeMode\) return;/);
    expect(body).toMatch(/parentLeg\.exchange !== 'NFO'/);
  });

  it('_maybeStageHedgeLeg sets basketError (never throws/blocks) on a resolveHedgeStrike failure', () => {
    const fnMatch = src.match(/function _maybeStageHedgeLeg\([\s\S]*?\n  \}/);
    const body = fnMatch[0];
    expect(body).toMatch(/if \(result\.ok === false\) \{/);
    expect(body).toMatch(/basketError = `Hedge not staged/);
  });

  it('a fresh hedge leg carries _hedgeParentKey and a snapshot _hedgeParentSig', () => {
    const fnMatch = src.match(/function _maybeStageHedgeLeg\([\s\S]*?\n  \}/);
    const body = fnMatch[0];
    expect(body).toMatch(/_hedgeParentKey:\s*parentLeg\.key/);
    expect(body).toMatch(/_hedgeParentSig:\s*`\$\{parentLeg\.sym\}\|\$\{parentLeg\.side\}\|\$\{parentLeg\.lots\}\|\$\{parentLeg\.account\}`/);
  });

  it('_mergeIntoBasket\'s match predicate excludes any leg carrying _hedgeParentKey', () => {
    const fnMatch = src.match(/function _mergeIntoBasket\([\s\S]*?\n  \}/);
    expect(fnMatch).toBeTruthy();
    expect(fnMatch[0]).toMatch(/!b\._hedgeParentKey/);
  });

  it('_netAgainstBasket\'s match predicate excludes any leg carrying _hedgeParentKey', () => {
    const fnMatch = src.match(/function _netAgainstBasket\([\s\S]*?\n  \}/);
    expect(fnMatch).toBeTruthy();
    expect(fnMatch[0]).toMatch(/!b\._hedgeParentKey/);
  });

  it('addOptionToBasket calls _maybeStageHedgeLeg only after the netAgainstBasket/mergeIntoBasket early-return paths (fresh-push path only)', () => {
    const fnMatch = src.match(/function addOptionToBasket\([\s\S]*?\n  \}/);
    expect(fnMatch).toBeTruthy();
    const body = fnMatch[0];
    const hedgeIdx = body.indexOf('_maybeStageHedgeLeg(');
    const netIdx = body.indexOf('_netAgainstBasket(');
    const mergeIdx = body.indexOf('_mergeIntoBasket(');
    expect(hedgeIdx).toBeGreaterThan(-1);
    expect(netIdx).toBeGreaterThan(-1);
    expect(mergeIdx).toBeGreaterThan(-1);
    expect(hedgeIdx).toBeGreaterThan(netIdx);
    expect(hedgeIdx).toBeGreaterThan(mergeIdx);
  });

  it('a resize/removal $effect watches chainBasket for stale hedge legs keyed on _hedgeParentKey/_hedgeParentSig', () => {
    // 2026-11-27 audit fix: a parent lot-size change now RESIZES its
    // paired hedge leg (via resolveHedgeParentChange) instead of
    // silently deleting it outright. The effect tracks both outcomes
    // via toResize/toRemove, not a single `stale` filter.
    expect(src).toMatch(/const toResize = \[\];/);
    expect(src).toMatch(/const toRemove = \[\];/);
    expect(src).toMatch(/resolveHedgeParentChange\(l\._hedgeParentSig, parent\)/);
    expect(src).toMatch(/l\._hedgeParentKey/);
    expect(src).toMatch(/_hedgeParentSig/);
  });

  it('the HEDGE toggle and its percent inputs render in .oct-toolbar, gated on hedgeOn', () => {
    expect(src).toMatch(/class="oct-hedge-toggle"/);
    expect(src).toMatch(/\{#if hedgeOn\}/);
    expect(src).toMatch(/bind:value=\{hedgeBuyPct\}/);
    expect(src).toMatch(/bind:value=\{hedgeSellPct\}/);
  });
});
