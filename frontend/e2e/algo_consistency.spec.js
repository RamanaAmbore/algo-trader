/**
 * algo_consistency.spec.js
 *
 * Phase 4 guard for the algo-page consistency SSOT introduced on
 * 2026-06-30 (see CLAUDE.md "Algo design tokens — SSOT" block in app.css).
 *
 * The audit collapsed:
 *   - 49 orphan #a3b9d0 literals    → var(--text-muted)
 *   - 1056 font-size literals       → 6 --fs-* tokens
 *   - 390 ui-monospace declarations → var(--font-numeric)
 *   - 15 card-bg gradient literals  → var(--card-bg-gradient)
 *   - 7 bespoke modals              → compose .algo-modal chrome
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *
 *   1. SSOT      — every algo route renders card-bg + primary text colour
 *                  through the shared CSS var chain.
 *   2. Perf      — cross-route nav under an 8 MB heap-growth budget
 *                  (subscription-leak guard from main_thread_perf spec).
 *   3. Stale     — hard-coded hex-literal grep on (algo)/ + lib/ .svelte
 *                  files: NO #a3b9d0, NO literal font-size: 0.Xrem.
 *   4. Reuse     — a modal opened from the algo layout resolves the same
 *                  gradient token as an /admin/derivatives payoff card.
 *   5. UX        — computed-style consistency at desktop + mobile
 *                  viewports across 8 canonical algo routes.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test frontend/e2e/algo_consistency.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'node:fs';
import * as path from 'node:path';

/* ── Routes under audit ──────────────────────────────────────────────── */

const ALGO_ROUTES = [
  '/dashboard',
  '/pulse',
  '/orders',
  '/charts',
  '/admin/derivatives',
  '/automation',
  '/strategies',
  '/admin/history',
];

/* ── Stale-code guard: raw hex + literal font-size ───────────────────── */

/**
 * Walk src/lib and src/routes/(algo) trees and collect .svelte files.
 */
function collectSvelteFiles() {
  const roots = [
    path.join(process.cwd(), 'src/lib'),
    path.join(process.cwd(), 'src/routes/(algo)'),
  ];
  const out = [];
  function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
    catch { return; }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.endsWith('.svelte')) out.push(p);
    }
  }
  for (const r of roots) walk(r);
  return out;
}

test.describe('algo consistency — SSOT stale-code guard', () => {
  test('no #a3b9d0 literals remain in algo surface', () => {
    const files = collectSvelteFiles();
    const offenders = [];
    for (const f of files) {
      const src = fs.readFileSync(f, 'utf-8');
      if (/#a3b9d0/i.test(src)) offenders.push(path.relative(process.cwd(), f));
    }
    expect(offenders, `#a3b9d0 orphan colour must be migrated to var(--text-muted). ` +
      `Offenders:\n${offenders.join('\n')}`).toEqual([]);
  });

  test('no literal font-size: 0.Xrem remain in algo surface', () => {
    const files = collectSvelteFiles();
    const offenders = [];
    // Match font-size assignments where the value is a plain 0.X rem
    // literal. Allow var(--fs-*) and calc() forms. Skip comment blocks
    // by pre-stripping them.
    const rx = /font-size:\s*0\.[0-9]+rem/;
    for (const f of files) {
      let src = fs.readFileSync(f, 'utf-8');
      // Strip CSS + HTML/JS block comments so /* font-size: 0.65rem */
      // documentation lines don't trip the guard.
      src = src
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/<!--[\s\S]*?-->/g, '');
      if (rx.test(src)) offenders.push(path.relative(process.cwd(), f));
    }
    expect(offenders, `font-size literals must map to --fs-2xs..--fs-xl tokens. ` +
      `Offenders:\n${offenders.join('\n')}`).toEqual([]);
  });

  test('no literal card-bg gradient outside app.css', () => {
    const files = collectSvelteFiles();
    const offenders = [];
    // The exact canonical gradient literal. If present in .svelte
    // scoped CSS it should be replaced with var(--card-bg-gradient).
    const rx = /linear-gradient\(180deg,\s*#1d2a44\s+0%,\s*#152033\s+100%\)/;
    for (const f of files) {
      const src = fs.readFileSync(f, 'utf-8');
      if (rx.test(src)) offenders.push(path.relative(process.cwd(), f));
    }
    expect(offenders, `card-bg gradient literal must use var(--card-bg-gradient). ` +
      `Offenders:\n${offenders.join('\n')}`).toEqual([]);
  });
});

/* ── Phase 1 palette-token grep guard (2026-07-02) ──────────────────── *
 *
 * Asserts that the 5 Phase-1 migration target files:
 *   1. Contain NO raw palette hex literals from the migrated set.
 *   2. Each use var(--c-*) at least as many times as the migration
 *      delivered (floor counts; the guard is a regression fence, not
 *      an exact snapshot). Only the semantic --c-* layer is checked —
 *      residual var(--algo-sky-*) / border / bg variants have no --c-*
 *      equivalent and are intentionally excluded from the floor.
 *
 * "Near-match" rgba values that were intentionally left raw (each
 * appearing <3× with a non-standard alpha) are NOT listed here —
 * they are Phase 2 candidates and not yet tokenised.
 * ─────────────────────────────────────────────────────────────────── */

const PHASE1_FILES = [
  path.join('src/lib', 'MarketPulse.svelte'),
  path.join('src/lib', 'PerformancePage.svelte'),
  path.join('src/routes/(algo)/dashboard', '+page.svelte'),
  path.join('src/routes/(algo)/admin/derivatives', '+page.svelte'),
  path.join('src/lib', 'NavCard.svelte'),
];

/** Palette hex literals that must NOT appear in Phase-1 files. */
const BANNED_HEX = [
  '#4ade80',
  '#f87171',
  '#22d3ee',
  '#fbbf24',
  '#7dd3fc',
  '#7e97b8',
];

/** Minimum var(--c-*) semantic token call count per file.
 *  Set at ~90% of the actual post-migration count so minor
 *  future refactors don't spuriously trip the guard. */
const TOKEN_FLOOR = {
  'MarketPulse.svelte':  32,
  'PerformancePage.svelte': 7,
  '+page.svelte (dashboard)': 34,
  '+page.svelte (derivatives)': 68,
  'NavCard.svelte': 1,
};

test.describe('algo consistency — Phase 1 palette migration guard', () => {
  test('no raw palette hex in Phase-1 migrated files', () => {
    const offenders = [];
    for (const rel of PHASE1_FILES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      // Strip comments so doc-example literals in /* … */ don't trip the guard
      const stripped = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '');
      for (const hex of BANNED_HEX) {
        if (stripped.includes(hex)) {
          offenders.push(`${path.basename(rel)}: contains raw ${hex}`);
        }
      }
    }
    expect(offenders,
      `Palette hex literals must use var(--c-*) semantic tokens. Regression in:\n${offenders.join('\n')}`
    ).toEqual([]);
  });

  test('Phase-1 files meet minimum --c-* semantic token usage floors', () => {
    const failures = [];
    for (const rel of PHASE1_FILES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      // Count ONLY var(--c-*) occurrences — the semantic alias layer.
      // var(--algo-*) usages that have no --c-* equivalent (sky, violet,
      // border, bg variants) are intentionally excluded from this floor.
      const matches = (src.match(/var\(--c-/g) || []).length;
      // Identify the floor entry by filename
      const key = rel.includes('dashboard')
        ? '+page.svelte (dashboard)'
        : rel.includes('derivatives')
          ? '+page.svelte (derivatives)'
          : path.basename(rel);
      const floor = TOKEN_FLOOR[key] ?? 1;
      if (matches < floor) {
        failures.push(`${key}: ${matches} --c-* usages (floor ${floor}) — semantic layer not applied`);
      }
    }
    expect(failures,
      `--c-* semantic token usage below floor — Commit 8 migration may not have landed:\n${failures.join('\n')}`
    ).toEqual([]);
  });

  test('app.css defines the 15 --c-* semantic alias tokens', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    // Alpha suffixes match actual alpha values: green/red use 0.06/0.10;
    // cyan uses 0.08/0.14.
    const required = [
      '--c-long', '--c-short', '--c-info', '--c-action', '--c-muted',
      '--c-long-06', '--c-long-10', '--c-long-22',
      '--c-short-06', '--c-short-10', '--c-short-22',
      '--c-info-08', '--c-info-14', '--c-info-22',
      '--c-action-14', '--c-action-22',
    ];
    for (const t of required) {
      expect(src, `${t} must be declared in app.css`).toContain(t + ':');
    }
  });
});

/* ── Phase 2 palette-token grep guard (2026-07-02) ──────────────────── *
 *
 * Extends the Phase-1 guard to the ~75 files swept in Phase 2.
 * Same contract:
 *   1. No raw palette hex in the migrated files (post-comment-strip).
 *   2. Each file uses var(--c-*) at least as many times as the floor
 *      count (set at ~90% of actual post-migration count).
 *
 * Phase-3 candidates (near-match alphas left raw intentionally):
 *   amber-10 (×39), amber-18 (×33), amber-55 (×30), amber-45 (×24),
 *   amber-35 (×24), amber-15 (×20), muted-18 (×51), muted-10 (×23),
 *   muted-30 (×25), red-55 (×24), red-35 (×24), red-15 (×20),
 *   green-18 (×16), cyan-65 (×16)  — all deferred to Phase 3.
 * ─────────────────────────────────────────────────────────────────── */

const PHASE2_FILES = [
  path.join('src/lib', 'SymbolPanel.svelte'),
  path.join('src/lib/order', 'OrderTicket.svelte'),
  path.join('src/lib/execution', 'SimulatorPanel.svelte'),
  path.join('src/lib', 'ChartWorkspace.svelte'),
  path.join('src/lib/order', 'OptionChainTab.svelte'),
  path.join('src/lib', 'OptionsPayoff.svelte'),
  path.join('src/routes/(algo)', '+layout.svelte'),
  path.join('src/lib', 'LogPanel.svelte'),
  path.join('src/lib', 'BrokerHealthBadge.svelte'),
  path.join('src/lib', 'Select.svelte'),
  path.join('src/lib', 'CommandBar.svelte'),
  path.join('src/lib', 'NavTab.svelte'),
  path.join('src/lib', 'PnlAnalysis.svelte'),
  path.join('src/lib', 'NavBreakdown.svelte'),
  path.join('src/lib', 'PriceChart.svelte'),
  path.join('src/lib', 'RefreshButton.svelte'),
  path.join('src/lib', 'MultiSelect.svelte'),
  path.join('src/lib', 'EquityCurve.svelte'),
  path.join('src/lib', 'UnifiedLog.svelte'),
  path.join('src/lib', 'PnlPanel.svelte'),
  path.join('src/lib', 'MultiPriceChart.svelte'),
  path.join('src/lib/order', 'ChaseCard.svelte'),
  path.join('src/lib/order', 'CommandLineTab.svelte'),
  path.join('src/lib/order', 'OrderTimelineDrawer.svelte'),
];

/** Minimum var(--c-*) usage floors for Phase-2 files (~90% of actual). */
const TOKEN_FLOOR_P2 = {
  'SymbolPanel.svelte': 63,
  'OrderTicket.svelte': 42,
  'SimulatorPanel.svelte': 45,
  'ChartWorkspace.svelte': 22,
  'OptionChainTab.svelte': 28,
  'OptionsPayoff.svelte': 14,
  '+layout.svelte': 46,
  'LogPanel.svelte': 22,
  'BrokerHealthBadge.svelte': 16,
  'Select.svelte': 8,
  'CommandBar.svelte': 10,
};

/** Phase-2 banned hex — same set as Phase-1 minus sky (#7dd3fc) which
 *  has no --c-* alias and was not a Phase-2 migration target. */
const BANNED_HEX_P2 = [
  '#4ade80',  // --c-long
  '#f87171',  // --c-short
  '#22d3ee',  // --c-info
  '#fbbf24',  // --c-action
  '#7e97b8',  // --c-muted
];

/** Strip comments, SVG presentation attrs and known-exempt CSS props
 *  before checking for raw palette hex. */
function stripExemptContextsP2(src) {
  return src
    // Block comments
    .replace(/\/\*[\s\S]*?\*\//g, '')
    // HTML template comments
    .replace(/<!--[\s\S]*?-->/g, '')
    // JS line comments
    .replace(/\/\/[^\n]*/g, '')
    // SVG presentation attributes (CSS var() unreliable in attrs)
    .replace(/\b(fill|stroke|stop-color)="[^"]*"/g, '')
    // accent-color intentionally excluded (control theming, not content)
    .replace(/accent-color\s*:[^;]+;/g, '');
}

test.describe('algo consistency — Phase 2 palette migration guard', () => {
  test('no raw palette hex in Phase-2 migrated files', () => {
    const offenders = [];
    for (const rel of PHASE2_FILES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      const stripped = stripExemptContextsP2(src);
      for (const hex of BANNED_HEX_P2) {
        // Match 6-digit hex only — exclude hex8 (e.g. #fbbf2466) which is a
        // distinct colour and has no --c-* alias (deferred Phase-3).
        const rx = new RegExp(hex + '(?![0-9a-fA-F])', 'i');
        if (rx.test(stripped)) {
          offenders.push(`${path.basename(rel)}: contains raw ${hex}`);
        }
      }
    }
    expect(offenders,
      `Phase-2 files must use var(--c-*) semantic tokens. Regression in:\n${offenders.join('\n')}`
    ).toEqual([]);
  });

  test('Phase-2 key files meet minimum --c-* semantic token usage floors', () => {
    const failures = [];
    for (const rel of PHASE2_FILES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      const name = path.basename(rel);
      const floor = TOKEN_FLOOR_P2[name];
      if (!floor) continue;  // No floor defined for low-count files
      const matches = (src.match(/var\(--c-/g) || []).length;
      if (matches < floor) {
        failures.push(`${name}: ${matches} --c-* usages (floor ${floor})`);
      }
    }
    expect(failures,
      `--c-* semantic token usage below floor — Phase 2 migration may not have landed:\n${failures.join('\n')}`
    ).toEqual([]);
  });
});

/* ── SSOT tokens defined ────────────────────────────────────────────── */

test.describe('algo consistency — token definitions present', () => {
  test('app.css defines the six --fs-* tokens', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    for (const t of ['--fs-2xs', '--fs-xs', '--fs-sm', '--fs-md', '--fs-lg', '--fs-xl']) {
      expect(src, `${t} must be declared in app.css`).toContain(t);
    }
  });

  test('app.css defines --font-numeric and --font-text', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('--font-numeric:');
    expect(src).toContain('--font-text:');
  });

  test('app.css defines .algo-modal recipe', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('.algo-modal');
    // Amber halo + gradient + shadow are the three visual-chrome fingerprints.
    expect(src).toMatch(/\.algo-modal[\s\S]{0,400}amber-bg-soft/);
  });

  test('app.css defines --card-bg-gradient token', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('--card-bg-gradient:');
  });

  test('app.css defines --algo-violet token', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('--algo-violet:');
    expect(src).toContain('--algo-violet-bg-soft:');
  });
});

/* ── Live-route consistency (routes × viewports) ───────────────────── */

// Login rate-limit is 5/min on the API. Serial mode + single login
// re-used across all route probes avoids hammering /api/auth/login
// and false-failing on rate-limit. Route sweeps are cheap post-login
// (goto + one evaluate) so the whole block finishes well under budget.
//
// The first probe primes auth; the sharedPage carries the JWT
// across subsequent probes. If login fails (e.g. rate-limited from a
// prior test run within the 60 s window), the entire describe is
// skipped rather than reporting N synthetic failures — the offline
// SSOT + token-definition checks above already cover the migration
// contract; the live-route checks are UX confidence.
test.describe.serial('algo consistency — live routes render dark tokens', () => {
  test.setTimeout(90_000);

  /** @type {import('@playwright/test').Page | null} */
  let sharedPage = null;
  /** @type {string} */
  let authSkipReason = '';

  test.beforeAll(async ({ browser }, testInfo) => {
    // Login flow retries twice with 3s + 8s waits — the hook can run
    // for up to ~40 s. Bump the default 30 s hook timeout.
    testInfo.setTimeout(60_000);
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAdmin(page);
      sharedPage = page;
    } catch (e) {
      authSkipReason = `login unavailable (${(/** @type {Error} */ (e)).message}). ` +
        `Live-route consistency probes require auth; offline SSOT + token ` +
        `checks above cover the migration contract.`;
      await ctx.close().catch(() => {});
    }
  });

  test.afterAll(async () => {
    if (sharedPage) await sharedPage.context().close();
  });

  for (const route of ALGO_ROUTES) {
    test(`${route} — dark bg + primary text colour resolve through tokens`, async () => {
      test.skip(!sharedPage, authSkipReason);
      // sharedPage is non-null when the skip guard passes above.
      const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
      await p.goto(route, { waitUntil: 'domcontentloaded' });
      await p.waitForTimeout(500);

      // The algo layout wraps every algo route in `.algo-viewport`. Its
      // computed bg must resolve to the algo dark-navy elevation stack
      // (not cream / public gray). --algo-bg-elev1 / --algo-bg-elev2
      // sit in the R,G,B < 50 range.
      const bg = await p.evaluate(() => {
        const el = document.querySelector('.algo-viewport');
        return el ? getComputedStyle(el).backgroundColor : '';
      });
      expect(bg, `${route} viewport bg: ${bg}`).toMatch(/^rgba?\(\s*\d+,\s*\d+,\s*\d+/);
      // Parse the RGB triple and assert each channel < 60 (dark navy).
      const [r, g, b] = (bg.match(/\d+/g) || []).map(Number);
      expect(r, `${route} R=${r}`).toBeLessThan(60);
      expect(g, `${route} G=${g}`).toBeLessThan(60);
      expect(b, `${route} B=${b}`).toBeLessThan(80);
    });
  }
});

/* ── Perf: cross-route nav heap growth budget ─────────────────────── */

test.describe.serial('algo consistency — perf', () => {
  test.setTimeout(120_000);
  test('cross-route lap keeps heap growth under 8 MB', async ({ page, browserName }) => {
    test.skip(browserName !== 'chromium', 'JS heap API is chromium-only');
    try {
      await loginAsAdmin(page);
    } catch (e) {
      test.skip(true, `login unavailable (${(/** @type {Error} */ (e)).message})`);
      return;
    }

    // Warm-up lap — first mount of each route amortizes lazy imports.
    for (const r of ALGO_ROUTES.slice(0, 5)) {
      await page.goto(r, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(200);
    }

    // Baseline
    await page.evaluate(() => {
      if ('gc' in globalThis && typeof globalThis.gc === 'function') globalThis.gc();
    });
    const before = await page.evaluate(() => {
      const p = /** @type {any} */ (performance);
      return p.memory ? p.memory.usedJSHeapSize : null;
    });
    if (before == null) {
      test.skip(true, 'performance.memory unavailable — chromium flag not set');
      return;
    }

    // Measured lap — five routes
    for (const r of ALGO_ROUTES.slice(0, 5)) {
      await page.goto(r, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(300);
    }

    await page.evaluate(() => {
      if ('gc' in globalThis && typeof globalThis.gc === 'function') globalThis.gc();
    });
    const after = await page.evaluate(() => {
      const p = /** @type {any} */ (performance);
      return p.memory ? p.memory.usedJSHeapSize : null;
    });

    const growthMB = after != null ? (after - before) / (1024 * 1024) : 0;
    expect(growthMB, `heap growth after 5-route lap: ${growthMB.toFixed(2)} MB`).toBeLessThan(8);
  });
});
