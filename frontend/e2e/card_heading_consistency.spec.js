// card_heading_consistency.spec.js
//
// Verifies three invariants across the operator (algo) route set:
//   1. Every card / section heading resolves to the canonical amber
//      (#fbbf24) palette — operator (2026-07-01): "header text color
//      is not consistent. Snapshot vs Greeks. Order entry vs Greeks.
//      GREEKS is good. Make them consistent and uniform."
//   2. Every active tab resolves to the same amber #fbbf24 — the
//      inactive slate default lives on .algo-tab, active state
//      overrides via .algo-tab[aria-selected="true"].algo-tab-c-amber.
//   3. On mobile (393×851) card heading left-edge offset ≥ 11.2px
//      (= 0.7rem × 16px) — headings must not touch the viewport edge.
//
// Five quality dimensions:
//   1. SSOT    — color + size + font-family asserted against canonical
//                values, not snapshots
//   2. Perf    — serial mode + one login per describe group
//   3. Stale   — grep app.css + every algo route/lib .svelte for
//                heading-class selectors that pin a non-amber color
//   4. Reuse   — shared helpers (getHeadingEls, isCanonicalAmber)
//   5. UX      — desktop 1280×800 + mobile 393×851 both exercised
//
// The Phase 2 migrations (2026-07-01) locked every card / section
// heading to canonical .algo-card-title tokens:
//     color:      #fbbf24
//     font-family: ui-monospace stack
//     font-size:  0.6rem (title AND section — both unified 2026-07-01)
//     font-weight: 700
//     letter-spacing: 0.04em
//     text-transform: uppercase

// @playwright/test project: chromium-desktop only
// CSS layout rendering is viewport-driven; we set the viewport explicitly
// to MOBILE (393×851) inside each test — no need to re-run on the
// mobile-portrait / mobile-landscape Playwright projects (which would
// fire 3 parallel logins and hit the rate limit).
// To restrict to one project, use: --project=chromium-desktop

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'node:fs';
import * as path from 'node:path';

test.setTimeout(60000);

// ── Constants ─────────────────────────────────────────────────────────────
const AMBER_400   = 'rgb(251, 191, 36)';    // #fbbf24 — canonical amber
const SLATE_400   = 'rgb(148, 163, 184)';   // #94a3b8 — inactive tab / muted labels only
const MIN_LEFT_PX = 11.2;  // 0.7rem × 16px minimum safe left edge on mobile

const ROUTES = [
  '/dashboard',
  '/automation',
  '/pulse',
  '/orders',
  '/admin/derivatives',
  '/admin/history',
  '/admin/settings',
  '/admin/brokers',
  '/admin/tokens',
];

const MOBILE_LEFT_ROUTES = ['/dashboard', '/automation', '/pulse', '/orders', '/admin/history'];

const DESKTOP = { width: 1280, height: 800 };
const MOBILE  = { width: 393,  height: 851 };

// Storage path is set per project inside beforeAll (where test.info() is available).
/** @type {string} */
let STORAGE_PATH;

// ── Helpers ───────────────────────────────────────────────────────────────

async function getHeadingEls(page) {
  return page.locator(
    '.algo-content .algo-card-title, ' +
    '.algo-content .algo-section-title, ' +
    '.algo-content .mp-section-label, ' +
    '.algo-content .section-heading, ' +
    '.algo-content .strat-section-heading, ' +
    '.algo-content .brokers-h, ' +
    '.algo-content .opt-block-h, ' +
    '.algo-content .oes-modal-name, ' +
    '.algo-content .mp-add-section-label'
  ).filter({ visible: true });
}

function isCanonicalAmber(color) {
  return color === AMBER_400;
}

// ── Stale grep — no browser needed ────────────────────────────────────────

test('SSOT stale-code: heading selectors in app.css resolve to canonical amber + 0.6rem', () => {
  const cssPath = path.resolve(process.cwd(), 'src/app.css');
  const css = fs.readFileSync(cssPath, 'utf8');
  // For each canonical heading class in app.css:
  //   color     MUST be #fbbf24 (canonical amber)
  //   font-size MUST be 0.6rem  (unified 2026-07-01 — section-title was 0.65rem)
  const CANONICAL_SELECTORS = [
    'algo-card-title',
    'algo-section-title',
    'mp-section-label',
  ];
  for (const sel of CANONICAL_SELECTORS) {
    // Match `.selector { ... }` block (non-nested) and pull declarations.
    const re = new RegExp(String.raw`\.${sel}\s*\{([^}]*)\}`, 'g');
    const matches = [...css.matchAll(re)];
    for (const m of matches) {
      const body = m[1];
      const colorMatch = body.match(/color\s*:\s*([^;]+);/);
      if (colorMatch) {
        const val = colorMatch[1].trim().toLowerCase();
        expect(
          val === '#fbbf24',
          `.${sel} color="${val}" — must be #fbbf24 (canonical amber)`
        ).toBe(true);
      }
      const fsMatch = body.match(/font-size\s*:\s*([^;]+);/);
      if (fsMatch) {
        const val = fsMatch[1].trim().toLowerCase();
        expect(
          val === '0.6rem',
          `.${sel} font-size="${val}" — must be 0.6rem (canonical, unified 2026-07-01)`
        ).toBe(true);
      }
    }
  }
});

test('SSOT stale-code: bespoke heading classes in algo routes/lib use canonical amber', () => {
  // Every heading-like class defined inline in an algo route or lib
  // component must pin color:#fbbf24 (not slate, not muted amber,
  // not var(--text-muted), etc). Bespoke classes are anything with
  // -title / -header / -heading / -h suffix.
  //
  // The list below is manually curated per Phase 2 migration
  // (2026-07-01) — additions should be reviewed against this spec.
  const CANDIDATES = [
    // [file, selector]
    ['src/routes/(algo)/admin/brokers/+page.svelte', 'brokers-h'],
    ['src/routes/(algo)/admin/metrics/+page.svelte', 'metrics-h2'],
    ['src/routes/(algo)/strategies/+page.svelte', 'strat-section-heading'],
    ['src/routes/(algo)/strategies/[id]/+page.svelte', 'strat-section-heading'],
    ['src/routes/(algo)/admin/derivatives/+page.svelte', 'opt-block-h'],
    ['src/routes/(algo)/admin/derivatives/+page.svelte', 'opt-section-h'],
    ['src/routes/(algo)/admin/derivatives/+page.svelte', 'opt-section-title'],
    ['src/lib/SymbolPanel.svelte', 'oes-modal-name'],
    // .mp-bucket-label is INTENTIONALLY tinted per-bucket (semantic
    // — positions / holdings / winners / losers / watch). Its
    // TYPOGRAPHY is locked to canonical; color is variant-driven.
    ['src/lib/NavCard.svelte', 'nav-panel-label'],
  ];
  for (const [rel, sel] of CANDIDATES) {
    const p = path.resolve(process.cwd(), rel);
    if (!fs.existsSync(p)) continue;
    const src = fs.readFileSync(p, 'utf8');
    // Match `.selector { ... }` — non-nested body only.
    const re = new RegExp(String.raw`\.${sel}\s*\{([^}]*)\}`);
    const m = src.match(re);
    if (!m) continue;
    const body = m[1];
    const colorMatch = body.match(/color\s*:\s*([^;]+);/);
    if (!colorMatch) continue;
    const val = colorMatch[1].trim().toLowerCase();
    // Allow #fbbf24 or var(--card-label-text, #fbbf24) fallback token.
    const ok = val === '#fbbf24'
            || /var\(--card-label-text,\s*#fbbf24\)/.test(val);
    expect(
      ok,
      `${rel}: .${sel} color="${val}" — expected #fbbf24 (canonical amber)`
    ).toBe(true);
  }
});

// ── Live tests: one login, all routes, desktop then mobile ────────────────
// Single describe.serial block: beforeAll logs in once, each test navigates
// to a route and checks multiple dimensions (color + size + left-gap on
// mobile). This keeps total login count = 1 regardless of how many routes.

test.describe.serial('live heading checks', () => {
  test.setTimeout(90000);

  // Skip on mobile-portrait and mobile-landscape projects — this spec
  // sets viewport explicitly and doesn't benefit from re-running on
  // non-desktop projects (3 parallel logins would hit the rate limit).
  test.skip(
    ({ browserName, isMobile }) => isMobile === true,
    'viewport set explicitly in test body; run with --project=chromium-desktop only'
  );

  test.beforeAll(async ({ browser }, testInfo) => {
    // Skip setup on mobile projects — their tests are skipped anyway
    // (isMobile guard below). Checking project name avoids a login
    // that would race with the chromium-desktop login and hit the
    // 5-req/min rate limit.
    if (testInfo.project.name !== 'chromium-desktop') return;

    STORAGE_PATH = path.join(process.cwd(), 'test-results', '.ch-auth.json');
    fs.mkdirSync(path.dirname(STORAGE_PATH), { recursive: true });
    const ctx = await browser.newContext({ viewport: DESKTOP });
    const pg  = await ctx.newPage();
    await loginAsAdmin(pg);
    await ctx.storageState({ path: STORAGE_PATH });
    await ctx.close();
  });

  // afterAll intentionally omitted — the storage file is temporary and
  // small; leaving it avoids deleting it before mobile-skipped workers
  // might reference it.

  // ── Desktop: color + font-size ─────────────────────────────────────────
  for (const route of ROUTES) {
    test(`desktop color+size — ${route}`, async ({ browser }) => {
      const ctx  = await browser.newContext({ viewport: DESKTOP, storageState: STORAGE_PATH });
      const page = await ctx.newPage();
      try {
        await page.goto(route, { waitUntil: 'domcontentloaded', timeout: 25000 });
        await page.waitForTimeout(800);

        const headings = await getHeadingEls(page);
        const count    = await headings.count();
        for (let i = 0; i < count; i++) {
          const el    = headings.nth(i);
          const color = await el.evaluate(n => getComputedStyle(n).color);
          const cls   = await el.evaluate(n => n.className);
          expect(
            isCanonicalAmber(color),
            `[desktop ${route}] "${cls.trim()}" color="${color}" — expected canonical amber ${AMBER_400}`
          ).toBe(true);
        }

        // Font-size check for canonical SSOT classes only
        const fsHeadings = await page.locator(
          '.algo-content .algo-card-title, .algo-content .algo-section-title'
        ).filter({ visible: true });
        const fsCount = await fsHeadings.count();
        for (let i = 0; i < fsCount; i++) {
          const el   = fsHeadings.nth(i);
          const fsPx = await el.evaluate(n => parseFloat(getComputedStyle(n).fontSize));
          const cls  = await el.evaluate(n => n.className);
          // 0.6rem × 16 = 9.6px — unified canonical for both .algo-card-title
          // and .algo-section-title (aligned 2026-07-01, was 0.65rem for section).
          const ok = Math.abs(fsPx - 9.6) <= 0.5;
          expect(ok, `[desktop ${route}] "${cls.trim()}" font-size=${fsPx}px — expected 9.6px (0.6rem canonical)`).toBe(true);
        }

        // Active tab check — every aria-selected="true" .algo-tab must
        // resolve to canonical amber. Inactive tabs stay slate.
        const activeTabs = await page.locator('.algo-content .algo-tab[aria-selected="true"]')
                                     .filter({ visible: true });
        const atCount = await activeTabs.count();
        for (let i = 0; i < atCount; i++) {
          const el = activeTabs.nth(i);
          const color = await el.evaluate(n => getComputedStyle(n).color);
          const cls   = await el.evaluate(n => n.className);
          expect(
            isCanonicalAmber(color),
            `[desktop ${route}] active tab "${cls.trim()}" color="${color}" — expected canonical amber ${AMBER_400}`
          ).toBe(true);
        }

        // Cross-family parity — when BOTH a card-title AND an active tab
        // are visible on the same page, their color + font-size must be
        // identical (the operator's core ask: "consistent in amber color
        // and font size"). This is the canonical invariant.
        const firstCardTitle = await page.locator('.algo-content .algo-card-title')
                                         .filter({ visible: true }).first();
        const firstActiveTab = await page.locator('.algo-content .algo-tab[aria-selected="true"]')
                                         .filter({ visible: true }).first();
        const hasCardTitle = await firstCardTitle.count() > 0;
        const hasActiveTab = await firstActiveTab.count() > 0;
        if (hasCardTitle && hasActiveTab) {
          const ctColor  = await firstCardTitle.evaluate(n => getComputedStyle(n).color);
          const tabColor = await firstActiveTab.evaluate(n => getComputedStyle(n).color);
          const ctFs     = await firstCardTitle.evaluate(n => parseFloat(getComputedStyle(n).fontSize));
          const tabFs    = await firstActiveTab.evaluate(n => parseFloat(getComputedStyle(n).fontSize));
          expect(
            ctColor === tabColor,
            `[desktop ${route}] card-title color="${ctColor}" ≠ active-tab color="${tabColor}" — must match`
          ).toBe(true);
          expect(
            Math.abs(ctFs - tabFs) <= 0.5,
            `[desktop ${route}] card-title font-size=${ctFs}px ≠ active-tab font-size=${tabFs}px — must match`
          ).toBe(true);
        }
      } finally {
        await ctx.close();
      }
    });
  }

  // ── Mobile: color + left-gap ───────────────────────────────────────────
  for (const route of ROUTES) {
    const isLeftRoute = MOBILE_LEFT_ROUTES.includes(route);
    test(`mobile color${isLeftRoute ? '+left-gap' : ''} — ${route}`, async ({ browser }) => {
      const ctx  = await browser.newContext({ viewport: MOBILE, storageState: STORAGE_PATH });
      const page = await ctx.newPage();
      try {
        await page.goto(route, { waitUntil: 'domcontentloaded', timeout: 25000 });
        await page.waitForTimeout(800);

        const headings = await getHeadingEls(page);
        const count    = await headings.count();
        for (let i = 0; i < count; i++) {
          const el    = headings.nth(i);
          const color = await el.evaluate(n => getComputedStyle(n).color);
          const cls   = await el.evaluate(n => n.className);
          expect(
            isCanonicalAmber(color),
            `[mobile ${route}] "${cls.trim()}" color="${color}" — expected canonical amber ${AMBER_400}`
          ).toBe(true);

          // Left-gap check for priority routes
          if (isLeftRoute) {
            const rect = await el.boundingBox();
            if (rect) {
              expect(
                rect.x,
                `[mobile ${route}] "${cls.trim()}" x=${rect.x.toFixed(1)}px — must be ≥ ${MIN_LEFT_PX}px`
              ).toBeGreaterThanOrEqual(MIN_LEFT_PX);
            }
          }
        }

        // Inactive tab colour check — slate-400 by default. Prevents
        // future regressions that flip inactive tabs amber (which
        // would defeat the active-state signal).
        const inactiveTabs = await page.locator('.algo-content .algo-tab[aria-selected="false"]')
                                       .filter({ visible: true });
        const itCount = Math.min(await inactiveTabs.count(), 4); // cap noise
        for (let i = 0; i < itCount; i++) {
          const el = inactiveTabs.nth(i);
          const color = await el.evaluate(n => getComputedStyle(n).color);
          const cls   = await el.evaluate(n => n.className);
          // Inactive can be slate (default) or hover-slate; MUST not be amber.
          expect(
            color !== AMBER_400,
            `[mobile ${route}] inactive tab "${cls.trim()}" color="${color}" — expected slate, got amber`
          ).toBe(true);
        }
      } finally {
        await ctx.close();
      }
    });
  }
});
