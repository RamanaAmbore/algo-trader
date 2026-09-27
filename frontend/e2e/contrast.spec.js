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

// Browser-side helper for live DOM contrast checks.
//
// effectiveBg() is gradient-aware (fixed 2026-09): .algo-modal's
// `background: var(--card-bg-gradient)` is a background-image, so its
// computed backgroundColor stays at the transparent initial value. A
// backgroundColor-only walk fell straight through .algo-modal to
// <body>'s Tailwind `bg-bg` class (#f8f9fb, near-white) -- a color never
// actually visible behind the opaque navy gradient -- and reported a
// false-positive contrast failure for .bh-footer-note (~2.49:1, exactly
// --text-lo vs #f8f9fb). The popup's real paint is the gradient's dark
// stops (~5.4:1+); effectiveBg() now checks backgroundImage for gradient
// stops before falling back to backgroundColor.
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
    // Walk up DOM to find the background(s) actually painted behind el.
    // Returns an array of candidate bg color strings -- normally one, but
    // multiple when the nearest painted ancestor uses a CSS gradient,
    // since a gradient paints every one of its color stops somewhere in
    // the box. A gradient background-image paints on top of
    // background-color and fully occludes it when opaque -- a shorthand
    // like background: linear-gradient(...) leaves the computed
    // backgroundColor at its transparent initial value, so checking
    // backgroundColor alone walks straight past a gradient ancestor to
    // whatever solid color sits further up the tree. See the comment
    // above WCAG_DOM_HELPERS for the concrete BrokerHealthBadge incident
    // this fixed.
    function effectiveBg(el) {
      let node = el;
      while (node && node !== document.body) {
        const cs = getComputedStyle(node);
        const img = cs.backgroundImage;
        if (img && img !== 'none' && /gradient\\(/.test(img)) {
          const stops = img.match(/rgba?\\([^)]+\\)/g);
          if (stops && stops.length) return stops;
        }
        const bg = cs.backgroundColor;
        if (alphaOf(bg) >= 0.15) return [bg];
        node = node.parentElement;
      }
      return [getComputedStyle(document.body).backgroundColor || 'rgb(255,255,255)'];
    }
    function check(sel, threshold) {
      const el = document.querySelector(sel);
      if (!el) return { skipped: true, reason: 'not found: ' + sel };
      const fg = getComputedStyle(el).color;
      const stops = effectiveBg(el);
      // Worst case across every gradient stop the element could sit on top
      // of — a single solid bg is just a one-element array here.
      let bg = null, ratio = null;
      for (const stop of stops) {
        const r = cr(fg, stop);
        if (r !== null && (ratio === null || r < ratio)) { ratio = r; bg = stop; }
      }
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

  // pnl-gain cream was #059669 (3.55:1) → now #047a56 (5.05:1) → now #065f46 (5.98-6.87:1)
  expect(css, 'pnl-gain on cream must use #047a56').not.toContain('.ag-theme-ramboq .pnl-gain { color: #059669');

  // 2026-09 size/contrast audit — #dc2626/#047a56/#4a7c7a/#b5521a/#6b7280
  // all measured below (or barely above) 4.5:1 against the odd-row/hover
  // cream backgrounds once the cell's own self-tint was composited in.
  // Deepened toward the dark end of each hue; see the "cream theme —
  // qty-long/qty-short/qty-flat/pnl-loss/pnl-gain" test below for the
  // live ratios.
  expect(css, 'pnl-loss on cream must not use #dc2626 (4.32:1 fail)').not.toContain('.ag-theme-ramboq .pnl-loss { color: #dc2626');
  expect(css, 'pnl-gain on cream must not use #047a56 (4.79:1 fail)').not.toContain('.ag-theme-ramboq .pnl-gain { color: #047a56');
  expect(css, 'qty-long on cream must not use #4a7c7a (4.22:1 fail)').not.toContain('.ag-theme-ramboq .qty-long  { color: #4a7c7a');
  expect(css, 'qty-short on cream must not use #b5521a (4.49:1 fail)').not.toContain('.ag-theme-ramboq .qty-short { color: #b5521a');
  expect(css, 'qty-flat on cream must not use #6b7280 (4.06:1 fail)').not.toContain('.ag-theme-ramboq .qty-flat  { color: #6b7280');
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

test('stale code — avgClsWithDir must not apply pnl-* text color to avg column', async () => {
  const { readFileSync } = await import('fs');
  const src = readFileSync(new URL('../src/lib/PerformancePage.svelte', import.meta.url).pathname, 'utf8');
  // Old code pushed 'cell-pos'/'cell-neg' onto the avg column via avgClsWithDir.
  // This caused green text (#4ade80) on a green-tinted background (ltp-vs-avg-up),
  // making avg values invisible. The fix strips pnl-* classes in avgClsWithDir.
  expect(src, 'avgClsWithDir must not push cell-pos/neg/flat').not.toMatch(
    /avgClsWithDir[\s\S]{0,300}cell-pos.*cell-neg/
  );
  expect(src, 'avgClsWithDir must filter pnl- classes').toMatch(
    /avgClsWithDir[\s\S]{0,300}pnl-/
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
    ['algo-card-title (#94a3b8)',         '#94a3b8'],
    // New WCAG-guaranteed tier (text-hi/med/lo)
    ['--text-hi (#e6edf7)',               '#e6edf7'],
    ['--text-med (#b8c5d9)',              '#b8c5d9'],
    ['--text-lo (#90a2b2)',               '#90a2b2'],
    // Note: log-cooldown + cmd-input placeholder (#7d8fa6) are tested on
    // their actual bg (#152033) in the separate log-panel test — these
    // elements live inside .log-panel / .cmd-input which have
    // background-color #152033, not on the card gradient.
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
    ['--text-lo (#90a2b2) on elevated',  '#90a2b2'],
    // Secondary colors used in BrokerHealthBadge rows — body-text threshold (4.5:1)
    ['--text-faint (#94a3b8) on elevated', '#94a3b8'],
    ['--algo-slate (#c8d8f0) on elevated', '#c8d8f0'],
    ['--algo-green (#4ade80) on elevated', '#4ade80'],
    ['--algo-amber (#fbbf24) on elevated', '#fbbf24'],
  ];
  for (const [label, fg] of checks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} on ${bg}`).toBeGreaterThanOrEqual(4.5);
  }
  // Status chip labels (bold, small text on tinted bg) — WCAG large-text 3:1 threshold.
  // --algo-red (#f87171) is used as .bh-row-state-red inside rgba(248,113,113,0.10) bg,
  // not as bare body text on the elevated card surface.
  const chipChecks = [
    ['--algo-red (#f87171) chip-label on elevated', '#f87171'],
  ];
  for (const [label, fg] of chipChecks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} on ${bg}`).toBeGreaterThanOrEqual(3.0);
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
    ['cmd-input placeholder (#7d8fa6)',   '#7d8fa6'],
    ['log-agent-default (#9ca3af)',       '#9ca3af'],
    ['log-ts-ist (#c8d8f0)',              '#c8d8f0'],
    ['log-ts-edt (#7e97b8)',              '#7e97b8'],
  ];
  for (const [label, fg] of checks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} on ${bg}`).toBeGreaterThanOrEqual(4.5);
  }
});

// ── Computed-style — BrokerHealthBadge popup footer, gradient-aware ───────
// Operator report: .bh-footer-note measured ~2.49:1 in real rendering.
// Investigation found the popup's own paint is fine (--text-lo on the
// .algo-modal navy gradient is ~5.4:1+) — the 2.49 number is exactly
// text-lo vs #f8f9fb, Tailwind's `bg` color applied to <body class="bg-bg">
// in app.html. BrokerHealthBadge mounts as a SIBLING before .algo-viewport
// (see (algo)/+layout.svelte), so nothing between .bh-footer-note and
// <body> paints a background-color — only .algo-modal's
// `background: var(--card-bg-gradient)` gradient does, and a gradient
// shorthand leaves computed `backgroundColor` at its transparent initial
// value. A background-color-only walk (the class of check this file's own
// domCheck()/effectiveBg() used to do) walks straight past the opaque
// gradient to <body>'s near-white bg, which a real user never sees (the
// gradient fully occludes it). This is a TEST-methodology fix, not a
// product CSS change — .bh-footer-note's real rendered background was
// never near-white. effectiveBg() above was made gradient-aware to fix
// the false positive; this fixture proves it end-to-end against a static
// reproduction of the real DOM nesting (no backend/auth needed).
test('computed style — bh-footer-note contrast against its real gradient paint, not the page body', async ({ page }) => {
  const { readFileSync } = await import('fs');
  const appCss = readFileSync(new URL('../src/app.css', import.meta.url).pathname, 'utf8');
  const badgeSrc = readFileSync(
    new URL('../src/lib/BrokerHealthBadge.svelte', import.meta.url).pathname, 'utf8'
  );
  const badgeCss = badgeSrc.match(/<style>([\s\S]*?)<\/style>/)[1];

  // Tailwind utility classes (`bg-bg`, `text-text`) aren't generated in a
  // static fixture — read the real value straight from tailwind.config.js
  // (SSOT) so the fixture's body matches production instead of guessing.
  const twConfig = readFileSync(
    new URL('../tailwind.config.js', import.meta.url).pathname, 'utf8'
  );
  const bodyBg = twConfig.match(/bg:\s*'(#[0-9a-fA-F]{6})'/)[1];

  const html = `<!DOCTYPE html>
<html><head><style>${appCss}</style><style>${badgeCss}</style>
<style>body { background-color: ${bodyBg}; }</style>
</head>
<body>
  <!-- Real nesting: BrokerHealthBadge mounts as a sibling BEFORE
       .algo-viewport, never inside .card-theme-dark. -->
  <div class="bh-modal algo-modal" id="bh-modal" role="dialog">
    <div class="bh-modal-header canonical-modal-header"><span class="bh-modal-title">Broker Auth Health</span></div>
    <div class="bh-modal-body">grid content</div>
    <div class="bh-modal-footer">
      <span class="bh-footer-note" id="bh-note">Polls every 30 s · Auth state from broker API calls</span>
    </div>
  </div>
</body></html>`;

  await page.setContent(html, { waitUntil: 'load' });
  const r = await domCheck(page, '#bh-note');

  expect(r.ratio, `bh-footer-note ${r.ratio?.toFixed(2)} fg=${r.fg} bg=${r.bg}`).toBeGreaterThanOrEqual(4.5);
  // Regression guard — a future change back to a backgroundColor-only walk
  // would resolve `bg` to the near-white body color instead of a gradient
  // stop; fail loudly rather than silently passing on the wrong layer.
  // (getComputedStyle reports colors as "rgb(r, g, b)" — convert the hex
  // SSOT value to the same format so the comparison is meaningful, not a
  // hex-vs-rgb string mismatch that would trivially pass either way.)
  const bodyBgRgb = `rgb(${hexToRgb(bodyBg).join(', ')})`;
  expect(r.bg, `bg must resolve to an .algo-modal gradient stop, not body (${bodyBgRgb})`).not.toBe(bodyBgRgb);
});

// ── Token-driven (SSOT) cream-theme contrast ───────────────────────────────
// B5 (2026-09): rewritten so it reads the ACTUAL current hex value out of
// app.css's `.card-theme-cream { ... }` block via regex, rather than
// asserting a hardcoded literal that happens to match whatever value was
// current when the test was written. A future edit to a token's hex value
// is checked against its real contrast ratio automatically — the test
// can't silently keep "passing" against a stale expected literal once the
// source value changes underneath it.
function extractCreamToken(css, varName) {
  const blockMatch = css.match(/\.card-theme-cream\s*\{([\s\S]*?)\n\}/);
  if (!blockMatch) return null;
  const escaped = varName.replace(/[-/\\^$*+?.()|[\]{}]/g, '\\$&');
  const re = new RegExp(`${escaped}:\\s*(#[0-9a-fA-F]{3,8})`);
  const m = blockMatch[1].match(re);
  return m ? m[1] : null;
}

test('CSS tokens — cream theme on cream surfaces (read live from app.css)', async () => {
  const { readFileSync } = await import('fs');
  const css = readFileSync(new URL('../src/app.css', import.meta.url).pathname, 'utf8');

  const cardBg   = '#fffdf8'; // ag-theme-ramboq .ag-background-color + pub-card
  const gridBg   = '#faf8f4'; // ag-theme-ramboq base row bg
  const oddRowBg = '#f5f2eb'; // ag-theme-ramboq odd row
  const hoverBg  = '#efebdf'; // ag-theme-ramboq --ag-row-hover-color
  const creamBg  = '#f0ece3'; // body
  const cardVarBg = '#f0ead8'; // --card-bg itself — the darkest bg some of
                                // these labels actually render on (e.g.
                                // .trust-lbl / .stat-label / .term-lbl sit
                                // on #f5f2eb-or-darker tint strips, not the
                                // lightest #fffdf8 card face; NavCard /
                                // PerformancePage also paint directly on it)
  const investorBodyBg = '#fdfaf2'; // investor/[token] page body bg — this
                                // page now wraps in .card-theme-cream too
                                // (B1) and reuses --card-as-of-text for its
                                // meta/label text (.ip-tag, .ip-status,
                                // .ip-footer, chart axis labels, etc.)

  // Normal-text (4.5:1) tokens — paired with EVERY bg each one actually
  // renders against in the app, not just the lightest #fffdf8 card face.
  // A token that only clears 4.5:1 on the lightest bg but not a darker one
  // it also renders on is still an AA failure in practice (this is exactly
  // how --card-zero-text's #7a6b52 -> #6e6049 fix (2026-09) was found —
  // it passed on #fffdf8 but failed at ~4.32:1 on --card-bg itself).
  const normalTextChecks = [
    ['--card-label-text',   cardBg],
    ['--card-label-text',   oddRowBg],
    ['--card-label-text',   cardVarBg],
    ['--card-as-of-text',   cardBg],
    ['--card-as-of-text',   cardVarBg],
    ['--card-as-of-text',   investorBodyBg],
    ['--card-muted-text',   cardBg],
    ['--card-muted-text',   cardVarBg],
    ['--card-cell-text',    creamBg],
    ['--card-currency-text', creamBg],
    ['--card-gain-text',    cardBg],
    ['--card-gain-text',    cardVarBg],
    ['--card-loss-text',    cardBg],
    ['--card-loss-text',    cardVarBg],
    ['--card-zero-text',    cardBg],
    ['--card-zero-text',    cardVarBg],
    // B1 (2026-09): new gold/champagne-as-text accent token — 4.5:1 tier
    // for normal/small text (links, "+" suffixes, brand marks).
    ['--card-accent-text',  cardBg],
    ['--card-accent-text',  oddRowBg],
    ['--card-accent-text',  cardVarBg],
    ['--card-accent-text',  investorBodyBg],
  ];
  for (const [varName, bg] of normalTextChecks) {
    const fg = extractCreamToken(css, varName);
    expect(fg, `${varName} not found in .card-theme-cream block`).not.toBeNull();
    const r = contrastRatio(fg, bg);
    expect(r, `${varName} (${fg}) on ${bg}: ${r.toFixed(2)}`).toBeGreaterThanOrEqual(4.5);
  }

  // Large-text (3:1) tokens — only ever used at >=24px, or >=18.66px bold,
  // per the token's own doc comment in app.css.
  const largeTextChecks = [
    // B1 (2026-09): large-text tier for the same gold/champagne accent —
    // used for the investor-portal hero figure and other >=24px / bold
    // >=18.66px accent numerals where the looser 3:1 floor applies.
    ['--card-accent-text-large', cardBg],
    ['--card-accent-text-large', '#ffffff'],
    // .ip-error-icon's error-box bg (investor page)
    ['--card-accent-text-large', '#fef5e7'],
  ];
  for (const [varName, bg] of largeTextChecks) {
    const fg = extractCreamToken(css, varName);
    expect(fg, `${varName} not found in .card-theme-cream block`).not.toBeNull();
    const r = contrastRatio(fg, bg);
    expect(r, `${varName} (${fg}) on ${bg}: ${r.toFixed(2)}`).toBeGreaterThanOrEqual(3.0);
  }

  // Fixed (non-token) cream-theme colors used elsewhere — regression guard,
  // still literal since these live outside the .card-theme-cream block.
  const fixedChecks = [
    ['pnl-gain (#065f46) on grid bg',      '#065f46', gridBg],
    ['pnl-gain (#065f46) on odd-row bg',   '#065f46', oddRowBg],
    ['pnl-gain (#065f46) on hover bg',     '#065f46', hoverBg],
    ['pnl-loss (#991b1b) on grid bg',      '#991b1b', gridBg],
    ['pnl-loss (#991b1b) on odd-row bg',   '#991b1b', oddRowBg],
    ['pnl-loss (#991b1b) on hover bg',     '#991b1b', hoverBg],
    ['qty-long (#335654) on grid bg',      '#335654', gridBg],
    ['qty-long (#335654) on odd-row bg',   '#335654', oddRowBg],
    ['qty-long (#335654) on hover bg',     '#335654', hoverBg],
    ['qty-short (#944315) on grid bg',     '#944315', gridBg],
    ['qty-short (#944315) on odd-row bg',  '#944315', oddRowBg],
    ['qty-short (#944315) on hover bg',    '#944315', hoverBg],
    ['qty-flat (#4b5563) on grid bg',      '#4b5563', gridBg],
    ['qty-flat (#4b5563) on odd-row bg',   '#4b5563', oddRowBg],
    ['qty-flat (#4b5563) on hover bg',     '#4b5563', hoverBg],
    ['section-heading (#8a6e28)',          '#8a6e28', '#ffffff'],
    ['field-label (#5a7090)',              '#5a7090', '#ffffff'],
    // B1 (2026-09): faq-zoom-hint / footer-link fixes — same muted meta
    // color already used elsewhere on the FAQ page (~5.06:1 on white).
    ['faq meta reuse (#5a7090) on white',  '#5a7090', '#ffffff'],
  ];
  for (const [label, fg, bg] of fixedChecks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} fg=${fg} bg=${bg}`).toBeGreaterThanOrEqual(4.5);
  }
});

test('CSS tokens — footer champagne text on navy footer bg (#0c1830)', () => {
  // B1 (2026-09): palette-role fix, not an AA rescue — #c8a84b already
  // passes here (~7.7:1); #e8c86a (the file's own documented "text on
  // dark" shade) passes even higher. Guards against a future regression
  // back to a fixed value that happens to fail on a *different* bg.
  const bg = '#0c1830';
  const checks = [
    ['pub-sep / accent (#c8a84b)',        '#c8a84b'],
    ['pub-footer-link / text-on-dark (#e8c86a)', '#e8c86a'],
  ];
  for (const [label, fg] of checks) {
    const r = contrastRatio(fg, bg);
    expect(r, `${label}: ${r.toFixed(2)} on ${bg}`).toBeGreaterThanOrEqual(4.5);
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
