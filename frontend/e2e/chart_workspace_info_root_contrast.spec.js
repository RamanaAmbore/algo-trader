// 2026-10 P0 audit fix — ChartWorkspace.svelte's `.cw-info-root` (the
// parenthetical root-symbol label in the chart info strip, e.g.
// "(NIFTY)" next to the front-month contract) used a hardcoded
// `color: #4a5a7a`. At `--fs-xs` against the `.cw-root` card background
// (`var(--card-bg-gradient)`, #1d2a44 -> #152033), that measures roughly
// 2.07:1 contrast against the lighter #1d2a44 stop — close to
// unreadable. Fixed to `var(--algo-muted)` (~4.78:1 against #1d2a44,
// and higher still against the darker #152033 stop the strip actually
// sits nearer to), matching the sibling `.cw-info-meta` / `.cw-meta-text`
// de-emphasized annotations already in the same info strip.
//
// Source-level guard — color tokens aren't reliably readable via
// getComputedStyle without a full page mount, so this asserts the
// source no longer contains the failing literal and does contain the
// fixed token.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const src = readFileSync(
  new URL('../src/lib/ChartWorkspace.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('ChartWorkspace.svelte — .cw-info-root contrast fix', () => {
  test('the failing #4a5a7a literal is no longer used as a color value', () => {
    // Only the explanatory code comment may still mention the old hex;
    // it must not appear as an actual `color:` declaration.
    expect(src).not.toMatch(/color:\s*#4a5a7a/);
  });

  test('.cw-info-root now uses var(--algo-muted)', () => {
    const rule = src.match(/\.cw-info-root\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/color:\s*var\(--algo-muted\)/);
  });

  test('.cw-info-root still sets --fs-xs (font-size unchanged by this fix)', () => {
    const rule = src.match(/\.cw-info-root\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/font-size:\s*var\(--fs-xs\)/);
  });
});
