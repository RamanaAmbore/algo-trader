// Admin Settings page: Global Switches panel (PATCH /api/admin/global-switches).
// paper_trading_mode is a real risk switch (prod-wide, every account) — gets
// an explicit danger-styled warning + ConfirmModal gate. default_agent_trade_mode
// is a softer per-create default, saved directly via Select.
// Source-level guard: reads the real page source + api.js wrapper.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const pageSrc = readFileSync(
  new URL('../src/routes/(algo)/admin/settings/+page.svelte', import.meta.url).pathname, 'utf8'
);
const apiSrc = readFileSync(
  new URL('../src/lib/api.js', import.meta.url).pathname, 'utf8'
);

test.describe('admin/settings — Global Switches panel', () => {
  test('api.js exposes a PATCH wrapper following the existing _patch convention', () => {
    expect(apiSrc).toMatch(/export const updateGlobalSwitches = \(payload\) =>\s*\n\s*_patch\('\/admin\/global-switches', payload, \{ auth: true \}\);/);
  });

  test('api.js exposes the renderer-list GET for the automation builder', () => {
    expect(apiSrc).toMatch(/export const fetchAgentRenderers = \(\) => _get\('\/agents\/renderers', \{ auth: true \}\);/);
  });

  test('panel section exists, gated the same way as the rest of the settings page', () => {
    expect(pageSrc).toMatch(/<h3 class="section-heading">Global Switches<\/h3>/);
  });

  test('paper_trading_mode shows an explicit prod-wide risk warning', () => {
    const section = pageSrc.slice(
      pageSrc.indexOf('Global Switches —'),
      pageSrc.indexOf('{#if execRows.length}')
    );
    expect(section).toMatch(/font-mono text-\[#7dd3fc\]">paper_trading_mode</);
    expect(section).toMatch(/Affects every account, prod-wide — flips real-money execution\./);
  });

  test('paper_trading_mode flip is gated behind ConfirmModal with danger:true', () => {
    expect(pageSrc).toMatch(/<ConfirmModal bind:this=\{_globalSwitchConfirmRef\} \/>/);
    const fn = pageSrc.slice(
      pageSrc.indexOf('async function toggleGlobalPaperMode()'),
      pageSrc.indexOf('async function toggleGlobalPaperMode()') + 900
    );
    expect(fn).toMatch(/_globalSwitchConfirmRef\?\.ask\(\{/);
    expect(fn).toMatch(/danger: true,/);
    expect(fn).toMatch(/await updateGlobalSwitches\(\{ paper_trading_mode: next \}\);/);
  });

  test('default_agent_trade_mode reads its current value from the canonical GET, writes via PATCH', () => {
    expect(pageSrc).toMatch(/value=\{globalSwitches\?\.default_agent_trade_mode \|\| 'paper'\}/);
    expect(pageSrc).toMatch(/await updateGlobalSwitches\(\{ default_agent_trade_mode: v \}\);/);
  });

  test('globalSwitches state is sourced from the real GET /admin/global-switches, not inferred', () => {
    expect(apiSrc).toMatch(/export const fetchGlobalSwitches = \(\) => _get\('\/admin\/global-switches', \{ auth: true \}\);/);
    expect(pageSrc).toMatch(/globalSwitches = await fetchGlobalSwitches\(\);/);
    // Both mutators re-sync local state from the PATCH response so the
    // panel never drifts from the server's own post-write truth.
    expect(pageSrc).toMatch(/const updated = await updateGlobalSwitches\(\{ paper_trading_mode: next \}\);\s*\n\s*globalSwitches = updated;/);
  });
});
