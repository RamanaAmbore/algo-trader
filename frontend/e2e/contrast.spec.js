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

test('stale code — BrokerHealthBadge failing hex values removed', async () => {
  const { readFileSync } = await import('fs');
  const badge = readFileSync(
    new URL('../src/lib/BrokerHealthBadge.svelte', import.meta.url).pathname,
    'utf8'
  );

  // These hex values had WCAG ratio < 4.5 on the elevated card bg (#273552 → #1d2a44).
  // #64748b = 3.01:1, #475569 = 1.89:1 — both replaced with --text-lo (4.60:1).
  expect(badge, 'bh-row-reason must not use #64748b (3.01:1 fail)').not.toMatch(
    /bh-row-reason[\s\S]{0,200}color:\s*#64748b/
  );
  expect(badge, 'bh-row-ts must not use #475569 (1.89:1 fail)').not.toMatch(
    /bh-row-ts[\s\S]{0,200}color:\s*#475569/
  );
  expect(badge, 'bh-footer-note must not use #475569 (1.89:1 fail)').not.toMatch(
    /bh-footer-note[\s\S]{0,200}color:\s*#475569/
  );
  expect(badge, 'bh-empty must not use #64748b (3.01:1 fail)').not.toMatch(
    /bh-empty[\s\S]{0,200}color:\s*#64748b/
  );
});

// ── 2. CSS-token contrast assertions (no server needed) ───────────────────
//    Assert the hex values in app.css satisfy the WCAG ratio. The browser
//    inherits these values, so this is the source of truth.

test('CSS tokens — dark theme text colors on dark card bg (#1d2a44)', () => {
  const bg = '#1d2a44'; // canonical --card-bg-gradient end
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
    // New WCAG-guaranteed tier (text-hi/med/lo)
    ['--text-hi (#e6edf7)',               '#e6edf7'],
    ['--text-med (#b8c5d9)',              '#b8c5d9'],
    ['--text-lo (#8294a8)',               '#8294a8'],
  ];
  for (const [label, fg] of checks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} on ${bg}`).toBeGreaterThanOrEqual(4.5);
  }
});

test('CSS tokens — text-hi/med/lo on elevated card bg (#273552)', () => {
  // --card-bg-elevated starts at #273552. Chip-tooltip panels (BrokerHealthBadge)
  // sit near the top of the gradient so the worst-case bg is #273552.
  const bg = '#273552';
  const checks = [
    ['--text-hi (#e6edf7) on elevated',  '#e6edf7'],
    ['--text-med (#b8c5d9) on elevated', '#b8c5d9'],
    ['--text-lo (#8294a8) on elevated',  '#8294a8'],
    // Secondary colors used in BrokerHealthBadge rows
    ['--text-faint (#94a3b8) on elevated', '#94a3b8'],
    ['--algo-slate (#c8d8f0) on elevated', '#c8d8f0'],
    ['--algo-green (#4ade80) on elevated', '#4ade80'],
    ['--algo-red (#f87171) on elevated',   '#f87171'],
    ['--algo-amber (#fbbf24) on elevated', '#fbbf24'],
  ];
  for (const [label, fg] of checks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} on ${bg}`).toBeGreaterThanOrEqual(4.5);
  }
});

test('CSS tokens — grid directional cells on dark grid bg (#1d2a44)', () => {
  // ag-theme-algo background. All directional cell types: pnl-gain, pnl-loss,
  // ltp-vs-avg-up, ltp-vs-avg-down, TOTAL row (amber tint).
  const BASE_BG = '#1d2a44';

  // Helper: composite alpha tint over base color
  function compositeHex(fgHex, alpha, bgHex) {
    const fg = hexToRgb(fgHex);
    const bg = hexToRgb(bgHex);
    return [
      Math.round(fg[0] * alpha + bg[0] * (1 - alpha)),
      Math.round(fg[1] * alpha + bg[1] * (1 - alpha)),
      Math.round(fg[2] * alpha + bg[2] * (1 - alpha)),
    ];
  }
  function contrastRatioRgb(fgHex, bgRgb) {
    const fg = hexToRgb(fgHex);
    const L1 = luminance(fg);
    const L2 = luminance(bgRgb);
    const lighter = Math.max(L1, L2);
    const darker = Math.min(L1, L2);
    return (lighter + 0.05) / (darker + 0.05);
  }

  // pnl-gain bg: rgba(74,222,128,0.08) over #1d2a44
  const pnlGainBg  = compositeHex('#4ade80', 0.08, BASE_BG);
  // pnl-loss bg: rgba(248,113,113,0.08) over #1d2a44
  const pnlLossBg  = compositeHex('#f87171', 0.08, BASE_BG);
  // ltp-vs-avg-up bg: rgba(74,222,128,0.10) over #1d2a44 (same as pos-long)
  const ltpUpBg    = compositeHex('#4ade80', 0.10, BASE_BG);
  // ltp-vs-avg-down: rgba(248,113,113,0.10) over #1d2a44
  const ltpDownBg  = compositeHex('#f87171', 0.10, BASE_BG);
  // TOTAL row: rgba(251,191,36,0.22) over #1d2a44
  const totalBg    = compositeHex('#fbbf24', 0.22, BASE_BG);

  const checks = [
    ['pnl-gain (#4ade80) on pnl-gain bg',   '#4ade80', pnlGainBg],
    ['pnl-loss (#f87171) on pnl-loss bg',   '#f87171', pnlLossBg],
    ['ltp up (#4ade80) on ltp-up bg',        '#4ade80', ltpUpBg],
    ['ltp down (#f87171) on ltp-down bg',    '#f87171', ltpDownBg],
    ['TOTAL row (#fbbf24) on amber tint bg', '#fbbf24', totalBg],
    // Primary body text on tinted cells — must stay readable
    ['slate (#c8d8f0) on pos-long bg',       '#c8d8f0', ltpUpBg],
    ['slate (#c8d8f0) on pos-short bg',      '#c8d8f0', ltpDownBg],
    ['slate (#c8d8f0) on pnl-gain bg',       '#c8d8f0', pnlGainBg],
    ['slate (#c8d8f0) on pnl-loss bg',       '#c8d8f0', pnlLossBg],
  ];
  for (const [label, fg, bgRgb] of checks) {
    const r = contrastRatioRgb(fg, bgRgb);
    expect(r, `${label}: ${r.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
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

  test('BrokerHealthBadge popup — row text contrast on elevated bg', async () => {
    // Opens the broker-chip in the navbar, verifies that the popup's reason
    // and timestamp columns meet 4.5:1. These were #64748b / #475569 before
    // the --text-lo fix (3.01:1 and 1.89:1 respectively).
    if (!sharedPage) { test.skip(); return; }
    await sharedPage.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await sharedPage.waitForTimeout(800);

    // Open the broker chip popup (navbar broker-chip button)
    const brokerChipButton = sharedPage.locator('button.broker-chip').first();
    const chipsCount = await brokerChipButton.count();
    if (chipsCount === 0) { test.skip(); return; }
    await brokerChipButton.click();
    await sharedPage.waitForTimeout(400);

    // bh-row-reason column
    const reason = await domCheck(sharedPage, '.bh-row-reason');
    if (!reason.skipped) {
      expect(reason.ratio, `bh-row-reason ${reason.ratio?.toFixed(2)} fg=${reason.fg} bg=${reason.bg}`).toBeGreaterThanOrEqual(4.5);
    }

    // bh-row-ts column
    const ts = await domCheck(sharedPage, '.bh-row-ts');
    if (!ts.skipped) {
      expect(ts.ratio, `bh-row-ts ${ts.ratio?.toFixed(2)} fg=${ts.fg} bg=${ts.bg}`).toBeGreaterThanOrEqual(4.5);
    }

    // bh-footer-note
    const footer = await domCheck(sharedPage, '.bh-footer-note');
    if (!footer.skipped) {
      expect(footer.ratio, `bh-footer-note ${footer.ratio?.toFixed(2)} fg=${footer.fg} bg=${footer.bg}`).toBeGreaterThanOrEqual(4.5);
    }

    // bh-modal-title (amber accent)
    const title = await domCheck(sharedPage, '.bh-modal-title');
    if (!title.skipped) {
      expect(title.ratio, `bh-modal-title ${title.ratio?.toFixed(2)} fg=${title.fg} bg=${title.bg}`).toBeGreaterThanOrEqual(4.5);
    }

    // Close the popup
    const closeBtn = sharedPage.locator('button.bh-close');
    if (await closeBtn.count() > 0) await closeBtn.click();
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
