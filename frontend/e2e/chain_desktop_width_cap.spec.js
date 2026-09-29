/**
 * chain_desktop_width_cap.spec.js
 *
 * Guards the 2026-09-29 fix: "mobile chain looks better than desktop
 * chain which looks very cluttered". Root cause was `.chain-grid-wrap`
 * having no max-width — on a wide desktop modal the fixed 44%/12%/44%
 * columns stretched to match, but content (small quote text + tiny
 * buttons) didn't scale up, leaving huge unused space inside each cell
 * while controls stayed clustered in a narrow middle band. Mobile's
 * naturally narrow viewport never had this problem, so it read as
 * comparatively tighter/cleaner. Capped desktop to ~30rem, centered.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const CHAIN_TAB_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OptionChainTab.svelte',
);

test.describe('Stale-code: chain grid capped to content-width on desktop', () => {
  test('.chain-grid-wrap gets a max-width + centering under a min-width:640px media query', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const block = src.match(/@media \(min-width:\s*640px\)\s*\{\s*\.chain-grid-wrap\s*\{[^}]*\}/)?.[0] ?? '';
    expect(block).toMatch(/max-width:\s*30rem/);
    expect(block).toMatch(/margin:\s*0 auto/);
  });

  test('mobile (no media query match) keeps the unconstrained wrap — table-layout/overflow rules untouched', () => {
    const src = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const baseRule = src.match(/\.chain-grid-wrap\s*\{[^}]*\}/)?.[0] ?? '';
    expect(baseRule).toContain('table-layout');
    // base rule is unscoped by the media query (comes first in source),
    // so mobile viewports never see the max-width cap.
    expect(baseRule).not.toMatch(/max-width/);
  });
});
