// Popup background parity — BrokerHealthBadge vs NavBreakdown
//
// Operator report: the connection-chip popup (BrokerHealthBadge) and the
// NavBreakdown popup (opened from PositionStrip's P/M/C/H pill bar) showed
// account-cell backgrounds "not in sync." Root cause: the two popup
// CONTAINERS used different background tokens — .algo-modal (BrokerHealth-
// Badge) painted `var(--card-bg-gradient)` directly (the convention used by
// ~10 other dark-theme popup/card surfaces app-wide), while NavBreakdown's
// panel (.ps-breakdown-panel in PositionStrip.svelte) and its own caption/
// empty-state rows used the theme-agnostic `var(--card-bg, ...)` indirection
// — unnecessary since these panels only ever render inside
// .algo-viewport.card-theme-dark and never the cream theme. This test
// verifies the CSS cascade in isolation (no backend/auth needed — it loads
// the real app.css into a static fixture) so a future edit that
// reintroduces divergent tokens fails here rather than only being caught
// by eyeballing a live popup.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — reads the real app.css source, not a hardcoded literal.
//   2. Perf    — no login, no navigation; pure CSS-cascade check.
//   3. Stale   — grep guard for the old `var(--card-bg,` indirection.
//   4. Reuse   — mirrors contrast.spec.js's page.setContent-free, Node-only
//                style for pieces that don't need a live DOM, and its
//                page.setContent pattern for the piece that does.
//   5. UX      — both popups' surfaces rendered as they actually nest
//                (BrokerHealthBadge outside .algo-viewport; NavBreakdown's
//                panel inside it) so DOM-nesting differences can't hide a
//                real divergence.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

function readAppCss() {
  return readFileSync(new URL('../src/app.css', import.meta.url).pathname, 'utf8');
}

// Both popup containers' rules live in their OWNING component's scoped
// <style> block (PositionStrip.svelte / NavBreakdown.svelte), not in
// app.css. Svelte's scoping only ADDS a hash class + attribute selector on
// top of the plain class selectors already in the source — extracting the
// raw <style> text and injecting it as-is still applies to bare
// `.ps-breakdown-panel` etc. elements in a static fixture with no
// Svelte-added scoping attribute present.
function readScopedStyle(relPath) {
  const src = readFileSync(new URL(relPath, import.meta.url).pathname, 'utf8');
  const m = src.match(/<style>([\s\S]*?)<\/style>/);
  if (!m) throw new Error(`no <style> block found in ${relPath}`);
  return m[1];
}

test('stale code — popup containers no longer use the --card-bg indirection', () => {
  const positionStrip = readFileSync(
    new URL('../src/lib/PositionStrip.svelte', import.meta.url).pathname, 'utf8'
  );
  const navBreakdown = readFileSync(
    new URL('../src/lib/NavBreakdown.svelte', import.meta.url).pathname, 'utf8'
  );
  expect(positionStrip, '.ps-breakdown-panel must not use the --card-bg indirection')
    .not.toMatch(/\.ps-breakdown-panel\s*\{[^}]*var\(--card-bg,/);
  expect(navBreakdown, '.nav-bd-caption must not use the --card-bg indirection')
    .not.toMatch(/\.nav-bd-caption\s*\{[^}]*var\(--card-bg,/);
  expect(navBreakdown, '.nav-bd-empty must not use the --card-bg indirection')
    .not.toMatch(/\.nav-bd-empty\s*\{[^}]*var\(--card-bg,/);
});

test('computed style — BrokerHealthBadge and NavBreakdown popup containers resolve to the same background', async ({ page }) => {
  const css = readAppCss();
  const positionStripCss = readScopedStyle('../src/lib/PositionStrip.svelte');
  const navBreakdownCss  = readScopedStyle('../src/lib/NavBreakdown.svelte');

  // Minimal fixture reproducing each popup's real DOM nesting (class names
  // only — no ag-Grid runtime needed since this checks the CONTAINER
  // background, which is pure CSS cascade off .algo-modal / .card-theme-dark
  // + the two components' own scoped <style> rules, extracted verbatim).
  const html = `<!DOCTYPE html>
<html><head><style>${css}</style><style>${positionStripCss}</style><style>${navBreakdownCss}</style></head>
<body class="bg-bg text-text">
  <!-- BrokerHealthBadge: mounted as a sibling BEFORE .algo-viewport in the
       real layout (see (algo)/+layout.svelte) — reproduced here the same
       way so nesting order can't accidentally paper over a real divergence. -->
  <div class="bh-modal algo-modal" id="bh-modal">
    <div class="bh-modal-body">popup body</div>
  </div>

  <div class="algo-viewport card-theme-dark">
    <div class="ps-breakdown-panel" id="ps-panel">
      <div class="nav-bd-wrap">
        <div class="nav-bd-caption" id="nav-bd-caption"><span>caption</span></div>
        <div class="nav-bd-empty" id="nav-bd-empty">empty state</div>
      </div>
    </div>
  </div>
</body></html>`;

  await page.setContent(html, { waitUntil: 'load' });

  const bg = (sel) => page.locator(sel).evaluate((el) => getComputedStyle(el).backgroundImage || getComputedStyle(el).background);

  const bhModalBg   = await bg('#bh-modal');
  const psPanelBg    = await bg('#ps-panel');
  const navCaptionBg = await bg('#nav-bd-caption');
  const navEmptyBg   = await bg('#nav-bd-empty');

  expect(psPanelBg, `ps-panel bg (${psPanelBg}) must match algo-modal bg (${bhModalBg})`).toBe(bhModalBg);
  expect(navCaptionBg, `nav-bd-caption bg (${navCaptionBg}) must match algo-modal bg (${bhModalBg})`).toBe(bhModalBg);
  expect(navEmptyBg, `nav-bd-empty bg (${navEmptyBg}) must match algo-modal bg (${bhModalBg})`).toBe(bhModalBg);
});
