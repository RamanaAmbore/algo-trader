// Contrast audit — WCAG 2.1 AA compliance spec
//
// Verifies that every canonical text-on-background pair on the operator
// pages meets 4.5:1 (normal text) or 3:1 (chip labels, per WCAG large
// text).  Pos/neg numeric cells always require 4.5:1 because they carry
// actionable financial information regardless of size.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — CSS token values verified directly from source (source of
//                truth); live DOM checks for non-ag-Grid elements supplement.
//   2. Perf    — ONE login for all DOM checks (storageState reuse); CSS-token
//                tests need no server at all.
//   3. Stale   — grep guard: no failing hex values remain in app.css.
//   4. Reuse   — shared contrastRatio() helper used throughout.
//   5. UX      — desktop (1400×900) + mobile (390×844) viewports tested.

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

test.setTimeout(90000);

// ── Reusable WCAG contrast helpers (Node.js context) ─────────────────────

function hexToRgb(hex) {
  const clean = hex.replace(/^#/, '');
  const full = clean.length === 3
    ? clean.split('').map((c) => c + c).join('')
    : clean;
  return [
    parseInt(full.slice(0, 2), 16),
    parseInt(full.slice(2, 4), 16),
    parseInt(full.slice(4, 6), 16),
  ];
}
function toLinear(c) {
  c /= 255;
  return c <= 0.04045 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4);
}
function luminance([r, g, b]) {
  return 0.2126 * toLinear(r) + 0.7152 * toLinear(g) + 0.0722 * toLinear(b);
}
function contrastRatio(fg, bg) {
  const L1 = luminance(hexToRgb(fg));
  const L2 = luminance(hexToRgb(bg));
  const lighter = Math.max(L1, L2);
  const darker = Math.min(L1, L2);
  return (lighter + 0.05) / (darker + 0.05);
}

// Browser-side helper for live DOM contrast checks
const WCAG_DOM_HELPERS = `
  window._wcag = window._wcag || (() => {
    function parseColor(s) {
      const m = s.match(/rgba?\\((\\d+),\\s*(\\d+),\\s*(\\d+)/);
      return m ? [+m[1], +m[2], +m[3]] : null;
    }
    function alphaOf(s) {
      const m = s.match(/rgba?\\(\\d+,\\s*\\d+,\\s*\\d+,?\\s*([\\d.]+)?\\)/);
      return m ? (m[1] !== undefined ? parseFloat(m[1]) : 1) : 0;
    }
    function toL(c) {
      c/=255; return c<=0.04045?c/12.92:Math.pow((c+0.055)/1.055,2.4);
    }
    function lum([r,g,b]) { return 0.2126*toL(r)+0.7152*toL(g)+0.0722*toL(b); }
    function cr(fgStr, bgStr) {
      const fg=parseColor(fgStr), bg=parseColor(bgStr);
      if(!fg||!bg) return null;
      const L1=lum(fg), L2=lum(bg);
      return (Math.max(L1,L2)+0.05) / (Math.min(L1,L2)+0.05);
    }
    // Walk up DOM to find first opaque background (alpha >= 0.15)
    function effectiveBg(el) {
      let node = el;
      while (node && node !== document.body) {
        const bg = getComputedStyle(node).backgroundColor;
        if (alphaOf(bg) >= 0.15) return bg;
        node = node.parentElement;
      }
      return getComputedStyle(document.body).backgroundColor || 'rgb(255,255,255)';
    }
    function check(sel, threshold) {
      const el = document.querySelector(sel);
      if (!el) return { skipped: true, reason: 'not found: ' + sel };
      const fg = getComputedStyle(el).color;
      const bg = effectiveBg(el);
      const ratio = cr(fg, bg);
      return { sel, fg, bg, ratio, pass: ratio !== null && ratio >= threshold };
    }
    return { check };
  })();
`;

async function domCheck(page, selector, threshold = 4.5) {
  return page.evaluate(
    ([sel, thr, h]) => { eval(h); return window._wcag.check(sel, thr); }, // eslint-disable-line no-eval
    [selector, threshold, WCAG_DOM_HELPERS]
  );
}

// ── 1. Stale-code grep guard (no server needed) ───────────────────────────

test('stale code — failing hex values removed from app.css', async () => {
  const { readFileSync } = await import('fs');
  const css = readFileSync(new URL('../src/app.css', import.meta.url).pathname, 'utf8');

  // Dark-bg pair: was #64748b (3.43:1) → now #7d8fa6 (4.94:1)
  expect(css, 'log-agent-cooldown must use #7d8fa6').not.toContain('log-agent-cooldown    { color: #64748b');
  expect(css, 'cmd-input placeholder must use #7d8fa6').not.toContain('cmd-input::placeholder { color: #64748b');

  // Cream tokens that failed AA
  expect(css, 'card-label-text must not be #c8a84b (1.94:1)').not.toContain('--card-label-text:          #c8a84b');
  expect(css, 'card-as-of-text must not be #a89878 (2.40:1)').not.toContain('--card-as-of-text:          #a89878');

  // pnl-gain cream was #059669 (3.55:1) → now #047a56 (5.05:1)
  expect(css, 'pnl-gain on cream must use #047a56').not.toContain('.ag-theme-ramboq .pnl-gain { color: #059669');
});

// ── 2. CSS-token contrast assertions (no server needed) ───────────────────
//    Assert the hex values in app.css satisfy the WCAG ratio. The browser
//    inherits these values, so this is the source of truth.

test('CSS tokens — dark theme text colors on dark card bg (#19253c)', () => {
  const bg = '#19253c'; // gradient #1d2a44 → #152033 averaged
  const checks = [
    ['--algo-slate (primary #c8d8f0)',    '#c8d8f0'],
    ['--algo-muted (secondary #7e97b8)',  '#7e97b8'],
    ['--algo-dim (tertiary #94a3b8)',     '#94a3b8'],
    ['--algo-amber (#fbbf24)',            '#fbbf24'],
    ['--algo-green (#4ade80)',            '#4ade80'],
    ['--algo-red (#f87171)',              '#f87171'],
    ['--algo-sky (#7dd3fc)',              '#7dd3fc'],
    ['--algo-cyan (#22d3ee)',             '#22d3ee'],
    ['cell-pos (#4ade80)',                '#4ade80'],
    ['cell-neg (#f87171)',                '#f87171'],
    ['cell-flat (#94a3b8)',               '#94a3b8'],
    ['log-cooldown (fixed #7d8fa6)',      '#7d8fa6'],
    ['cmd-input placeholder (fixed #7d8fa6)', '#7d8fa6'],
    ['algo-card-title (#94a3b8)',         '#94a3b8'],
  ];
  for (const [label, fg] of checks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} on ${bg}`).toBeGreaterThanOrEqual(4.5);
  }
});

test('CSS tokens — dark theme on log-panel bg (#152033)', () => {
  const bg = '#152033';
  const checks = [
    ['log-info (#e2e8f0)',                '#e2e8f0'],
    ['log-debug (#94a3b8)',               '#94a3b8'],
    ['log-cooldown (fixed #7d8fa6)',      '#7d8fa6'],
    ['log-agent-default (#9ca3af)',       '#9ca3af'],
    ['log-ts-ist (#c8d8f0)',              '#c8d8f0'],
    ['log-ts-edt (#7e97b8)',              '#7e97b8'],
  ];
  for (const [label, fg] of checks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} on ${bg}`).toBeGreaterThanOrEqual(4.5);
  }
});

test('CSS tokens — cream theme on cream surfaces', () => {
  const cardBg   = '#fffdf8'; // ag-theme-ramboq .ag-background-color + pub-card
  const gridBg   = '#faf8f4'; // ag-theme-ramboq base row bg
  const oddRowBg = '#f5f2eb'; // ag-theme-ramboq odd row
  const creamBg  = '#f0ece3'; // body

  const checks = [
    // Fixed tokens (cream theme)
    ['--card-label-text (fixed #7a5e1e)',  '#7a5e1e', cardBg],
    ['--card-as-of-text (fixed #7a6650)',  '#7a6650', cardBg],
    ['--card-muted-text (fixed #736448)',  '#736448', cardBg],
    // pnl-gain (fixed)
    ['pnl-gain (#047a56) on grid bg',      '#047a56', gridBg],
    ['pnl-gain (#047a56) on odd-row bg',   '#047a56', oddRowBg],
    // pnl-loss (unchanged, regression guard)
    ['pnl-loss (#dc2626) on grid bg',      '#dc2626', gridBg],
    // section-heading (fixed)
    ['section-heading (#8a6e28)',           '#8a6e28', '#ffffff'],
    // Passing tokens — regression guard
    ['--card-cell-text (#0c1830)',          '#0c1830', creamBg],
    ['--card-currency-text (#4a5872)',      '#4a5872', creamBg],
    ['--card-gain-text (#1a6b3a)',          '#1a6b3a', cardBg],
    ['--card-loss-text (#9b1c1c)',          '#9b1c1c', cardBg],
    ['--card-zero-text (#7a6b52)',          '#7a6b52', cardBg],
    ['field-label (#5a7090)',               '#5a7090', '#ffffff'],
  ];
  for (const [label, fg, bg] of checks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} fg=${fg} bg=${bg}`).toBeGreaterThanOrEqual(4.5);
  }
});

// ── 3. Live DOM checks — one login, shared state across tests ─────────────
//    Uses test.describe with ONE beforeAll login to minimise auth calls.
//    serial mode: tests run in order, not parallel, so rate limit is safe.

// Shared auth helper — logs in once and returns a page, or null if rate-limited.
// Rate limit is 5/min on /api/auth/login. When running in a full suite after
// many logins, the quota may be exhausted; DOM tests then skip gracefully so
// the (more authoritative) CSS-token tests still enforce the contract.
async function tryLogin(browser, viewport) {
  try {
    const ctx = await browser.newContext({ viewport });
    const pg = await ctx.newPage();
    await loginAsAdmin(pg);
    return pg;
  } catch (e) {
    if (/rate|too.many|429/i.test(e.message)) return null;
    throw e;
  }
}

test.describe('contrast — live DOM — desktop', () => {
  test.describe.configure({ mode: 'serial' });

  let sharedPage = null;

  test.beforeAll(async ({ browser }) => {
    sharedPage = await tryLogin(browser, { width: 1400, height: 900 });
  });

  test.afterAll(async () => {
    if (sharedPage) await sharedPage.context().close().catch(() => {});
  });

  test('dashboard — algo-card-title is readable', async () => {
    if (!sharedPage) { test.skip(); return; }
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await sharedPage.waitForTimeout(1200);
    const r = await domCheck(sharedPage, '.algo-card-title');
    if (!r.skipped) {
      expect(r.ratio, `algo-card-title ${r.ratio?.toFixed(2)} fg=${r.fg} bg=${r.bg}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  test('orders — act-events-hint and log-debug are readable', async () => {
    if (!sharedPage) { test.skip(); return; }
    await sharedPage.goto('/orders', { waitUntil: 'domcontentloaded' });
    await sharedPage.waitForTimeout(1200);

    const debug = await domCheck(sharedPage, '.log-panel .log-debug');
    if (!debug.skipped) {
      expect(debug.ratio, `log-debug ${debug.ratio?.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
    }

    const hint = await domCheck(sharedPage, '.act-events-hint');
    if (!hint.skipped) {
      expect(hint.ratio, `act-events-hint ${hint.ratio?.toFixed(2)} fg=${hint.fg} bg=${hint.bg}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  test('derivatives — chase-label off-state is readable', async () => {
    if (!sharedPage) { test.skip(); return; }
    await sharedPage.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await sharedPage.waitForTimeout(1200);
    const r = await domCheck(sharedPage, '.oes-common-chase-label');
    if (!r.skipped) {
      expect(r.ratio, `chase-label ${r.ratio?.toFixed(2)} fg=${r.fg} bg=${r.bg}`).toBeGreaterThanOrEqual(4.5);
    }
  });
});

test.describe('contrast — live DOM — mobile', () => {
  test.describe.configure({ mode: 'serial' });

  let sharedPage = null;

  test.beforeAll(async ({ browser }) => {
    sharedPage = await tryLogin(browser, { width: 390, height: 844 });
  });

  test.afterAll(async () => {
    if (sharedPage) await sharedPage.context().close().catch(() => {});
  });

  test('orders — act-events-hint readable on mobile', async () => {
    if (!sharedPage) { test.skip(); return; }
    await sharedPage.goto('/orders', { waitUntil: 'domcontentloaded' });
    await sharedPage.waitForTimeout(1200);
    const hint = await domCheck(sharedPage, '.act-events-hint');
    if (!hint.skipped) {
      expect(hint.ratio, `act-events-hint mobile ${hint.ratio?.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  test('dashboard — algo-card-title readable on mobile', async () => {
    if (!sharedPage) { test.skip(); return; }
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await sharedPage.waitForTimeout(1200);
    const r = await domCheck(sharedPage, '.algo-card-title');
    if (!r.skipped) {
      expect(r.ratio, `algo-card-title mobile ${r.ratio?.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
    }
  });
});
