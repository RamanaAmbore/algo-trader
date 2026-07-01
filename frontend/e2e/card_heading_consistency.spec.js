// card_heading_consistency.spec.js
//
// Verifies two invariants across the operator (algo) route set:
//   1. Card and section headings use SSOT color: slate-400 (#94a3b8)
//      or amber (#fbbf24) only when an interactive/alerting semantic
//      class is present (.is-active, .is-alerting, .mp-section-label).
//   2. On mobile (393×851) card heading left-edge offset ≥ 11.2px
//      (= 0.7rem × 16px) — headings must not touch the viewport edge.
//
// Five quality dimensions:
//   1. SSOT    — color + size asserted against canonical values, not snapshots
//   2. Perf    — serial mode + one login per describe group (storageState reuse)
//   3. Stale   — no inline color:#fbbf24 on heading-class selectors (grep)
//   4. Reuse   — shared helpers (getHeadingEls, isAllowedColor)
//   5. UX      — desktop 1280×800 + mobile 393×851 both exercised

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
const SLATE_400   = 'rgb(148, 163, 184)';   // #94a3b8 — canonical slate
const AMBER_400   = 'rgb(251, 191, 36)';    // #fbbf24 — canonical amber (active)
const AMBER_MUTED = 'rgba(251, 191, 36, 0.698)'; // mp-section-label (0.7 opacity)
const MIN_LEFT_PX = 11.2;  // 0.7rem × 16px minimum safe left edge on mobile

const ROUTES = [
  '/dashboard',
  '/automation',
  '/pulse',
  '/orders',
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
    '.algo-content .bucket-subheader, ' +
    '.algo-content .hcard-title'
  ).filter({ visible: true });
}

function isAllowedColor(color, classes) {
  if (color === SLATE_400) return true;
  // Canonical amber role — mp-section-label is always amber
  if (classes.includes('mp-section-label')) return true;
  // Interactive / alerting state amber
  if (
    (color === AMBER_400 || color === AMBER_MUTED) &&
    (classes.includes('is-active') || classes.includes('is-alerting'))
  ) return true;
  return false;
}

// ── Stale grep — no browser needed ────────────────────────────────────────

test('SSOT stale-code: no inline color:#fbbf24 on heading-class selectors in app.css', () => {
  const cssPath = path.resolve(process.cwd(), 'src/app.css');
  const css = fs.readFileSync(cssPath, 'utf8');
  const pat =
    /\.(algo-card-title|algo-section-title|section-heading|brokers-h|hcard-title)\s*\{[^}]*color\s*:\s*#fbbf24[^}]*\}/gs;
  const violations = [...css.matchAll(pat)].map(m => m[0].trim());
  expect(
    violations,
    `amber color still set on heading class selectors:\n${violations.join('\n')}`
  ).toHaveLength(0);
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
            isAllowedColor(color, cls),
            `[desktop ${route}] "${cls.trim()}" color="${color}" — expected slate-400 or canonical amber`
          ).toBe(true);
        }

        // Font-size check for SSOT classes only
        const fsHeadings = await page.locator(
          '.algo-content .algo-card-title, .algo-content .algo-section-title'
        ).filter({ visible: true });
        const fsCount = await fsHeadings.count();
        for (let i = 0; i < fsCount; i++) {
          const el   = fsHeadings.nth(i);
          const fsPx = await el.evaluate(n => parseFloat(getComputedStyle(n).fontSize));
          const cls  = await el.evaluate(n => n.className);
          // 0.6rem × 16 = 9.6px (algo-card-title), 0.65rem × 16 = 10.4px (algo-section-title)
          const ok = [9.6, 10.4].some(s => Math.abs(fsPx - s) <= 0.5);
          expect(ok, `[desktop ${route}] "${cls.trim()}" font-size=${fsPx}px — expected 9.6 or 10.4px`).toBe(true);
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
            isAllowedColor(color, cls),
            `[mobile ${route}] "${cls.trim()}" color="${color}" — expected slate-400 or canonical amber`
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
      } finally {
        await ctx.close();
      }
    });
  }
});
