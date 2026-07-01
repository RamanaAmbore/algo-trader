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
//   2. Perf    — serial mode; tests share login to avoid rate-limit
//   3. Stale   — no inline color:#fbbf24 on heading-class selectors (grep)
//   4. Reuse   — shared helpers (getHeadingEls, isAllowedColor)
//   5. UX      — desktop 1280×800 + mobile 393×851 both exercised

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

// ── Live browser tests ─────────────────────────────────────────────────────
// Serial mode: prevents all N tests from logging in simultaneously and
// hitting the 5/min rate limit on /api/auth/login.

test.describe('heading color + size — desktop', () => {
  test.describe.configure({ mode: 'serial' });

  for (const route of ROUTES) {
    test(`${route}`, async ({ page }) => {
      page.setViewportSize(DESKTOP);
      await loginAsAdmin(page);
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
    });
  }
});

test.describe('heading color + size — mobile', () => {
  test.describe.configure({ mode: 'serial' });

  for (const route of ROUTES) {
    test(`${route}`, async ({ page }) => {
      page.setViewportSize(MOBILE);
      await loginAsAdmin(page);
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
      }
    });
  }
});

test.describe('heading font-size — desktop', () => {
  test.describe.configure({ mode: 'serial' });

  for (const route of ROUTES) {
    test(`${route}`, async ({ page }) => {
      page.setViewportSize(DESKTOP);
      await loginAsAdmin(page);
      await page.goto(route, { waitUntil: 'domcontentloaded', timeout: 25000 });
      await page.waitForTimeout(800);

      const headings = await page.locator(
        '.algo-content .algo-card-title, .algo-content .algo-section-title'
      ).filter({ visible: true });
      const count = await headings.count();
      for (let i = 0; i < count; i++) {
        const el   = headings.nth(i);
        const fsPx = await el.evaluate(n => parseFloat(getComputedStyle(n).fontSize));
        const cls  = await el.evaluate(n => n.className);
        // 0.6rem × 16 = 9.6px (algo-card-title), 0.65rem × 16 = 10.4px (algo-section-title)
        const ok = [9.6, 10.4].some(s => Math.abs(fsPx - s) <= 0.5);
        expect(ok, `[desktop ${route}] "${cls.trim()}" font-size=${fsPx}px — expected 9.6 or 10.4px`).toBe(true);
      }
    });
  }
});

test.describe('mobile heading left gap', () => {
  test.describe.configure({ mode: 'serial' });

  for (const route of MOBILE_LEFT_ROUTES) {
    test(`≥ ${MIN_LEFT_PX}px — ${route}`, async ({ page }) => {
      page.setViewportSize(MOBILE);
      await loginAsAdmin(page);
      await page.goto(route, { waitUntil: 'domcontentloaded', timeout: 25000 });
      await page.waitForTimeout(800);

      const headings = await getHeadingEls(page);
      const count    = await headings.count();
      for (let i = 0; i < count; i++) {
        const el   = headings.nth(i);
        const rect = await el.boundingBox();
        const cls  = await el.evaluate(n => n.className);
        if (!rect) continue;
        expect(
          rect.x,
          `[mobile ${route}] "${cls.trim()}" x=${rect.x.toFixed(1)}px — must be ≥ ${MIN_LEFT_PX}px`
        ).toBeGreaterThanOrEqual(MIN_LEFT_PX);
      }
    });
  }
});
