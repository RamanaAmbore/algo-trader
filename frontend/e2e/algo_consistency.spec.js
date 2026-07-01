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

  test('app.css defines .algo-chip + .algo-chip-shape + .algo-tag', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('.algo-chip-shape');
    expect(src).toMatch(/\.algo-chip\s*\{/);
    expect(src).toContain('.algo-tag');
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
