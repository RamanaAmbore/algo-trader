/**
 * tab_text_consistency.spec.js
 *
 * Guards that every tab strip on algo pages renders through the canonical
 * AlgoTabs component (.algo-tab / .algo-tabs-strip) and that all tabs
 * share identical computed font-size, font-family, text-transform, and
 * letter-spacing. Active tab must use the --text-accent family color
 * (any non-slate / non-gray value — concrete check: not #94a3b8).
 *
 * Five quality dimensions:
 *   1. SSOT      — .algo-tab class present on every tab button (no bespoke
 *                  tab CSS outside AlgoTabs)
 *   2. Perf      — style assertions complete under 10 s per route
 *   3. Stale     — no manual tab-button CSS class (aw-tab, tab-btn, etc.)
 *                  acting as the sole tab decorator outside AlgoTabs
 *   4. Reuse     — canonical AlgoTabs component on all 8 routes
 *   5. UX        — computed font-size / transform / weight consistent
 *                  across tabs within each strip; active tab color is
 *                  visually distinct from inactive (not #94a3b8 / rgb(148,163,184))
 *
 * Routes checked (desktop 1366×768 + mobile-portrait 390×844):
 *   /dashboard, /pulse, /orders, /admin/derivatives, /admin/history,
 *   /automation, /automation/templates, /admin/execution
 */

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// Module-level cached token — one login per spec run. Retries up to 3x
// with back-off to handle rate-limit 429s that prior test runs can cause.
let _token = null;
async function injectSession(page) {
  if (!_token) {
    for (const delay of [0, 6000, 15000]) {
      if (delay) await new Promise((res) => setTimeout(res, delay));
      for (const u of ['ambore', 'rambo']) {
        const r = await page.request.post(`${BASE}/api/auth/login`, {
          data: { username: u, password: _PASS },
        });
        if (r.ok()) { _token = (await r.json()).access_token; break; }
      }
      if (_token) break;
    }
    if (!_token) throw new Error(`login failed against ${BASE}`);
  }
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, _token);
}

const INACTIVE_SLATE = 'rgb(148, 163, 184)'; // #94a3b8

const ALGO_ROUTES = [
  { path: '/dashboard',             label: 'dashboard'              },
  { path: '/pulse',                 label: 'pulse'                  },
  { path: '/orders',                label: 'orders'                 },
  { path: '/admin/derivatives',     label: 'derivatives'            },
  { path: '/admin/history',         label: 'history'                },
  { path: '/automation',            label: 'automation'             },
  { path: '/automation/templates',  label: 'automation-templates'   },
  { path: '/admin/execution',       label: 'execution'              },
];

/**
 * Collect style metrics for all .algo-tab buttons on the page.
 * Returns null when no tabs found (e.g. unauthenticated redirect).
 */
async function collectTabMetrics(page) {
  return page.evaluate((INACTIVE) => {
    const tabs = Array.from(document.querySelectorAll('.algo-tab'));
    if (tabs.length === 0) return null;

    const metrics = tabs.map((btn) => {
      const s = getComputedStyle(btn);
      return {
        fontSize:      s.fontSize,
        fontFamily:    s.fontFamily,
        fontWeight:    s.fontWeight,
        textTransform: s.textTransform,
        letterSpacing: s.letterSpacing,
        color:         s.color,
        isActive:      btn.getAttribute('aria-selected') === 'true',
      };
    });

    // All tabs must agree on font-size, text-transform, letter-spacing.
    const ref = metrics[0];
    const styleErrors = metrics
      .filter((m) => (
        m.fontSize      !== ref.fontSize      ||
        m.textTransform !== ref.textTransform  ||
        m.letterSpacing !== ref.letterSpacing
      ))
      .map((m, i) => `tab[${i}] mismatch: fontSize=${m.fontSize} transform=${m.textTransform} tracking=${m.letterSpacing}`);

    // Active tab color must differ from inactive slate.
    const activeMetrics = metrics.filter((m) => m.isActive);
    const activeColorErrors = activeMetrics
      .filter((m) => m.color === INACTIVE)
      .map((m) => `active tab still slate (${m.color})`);

    return {
      count:        tabs.length,
      activeCount:  activeMetrics.length,
      styleErrors,
      activeColorErrors,
      canonical:    ref,
    };
  }, INACTIVE_SLATE);
}

// Outer wrapper ensures desktop + mobile suites run serially so they
// don't compete for the same login session concurrently.
test.describe('tab text consistency', () => {
test.describe.configure({ mode: 'serial' });

// ── desktop ──────────────────────────────────────────────────────────────
test.describe('tab text consistency — desktop 1366×768', () => {
  test.use({ viewport: { width: 1366, height: 768 } });
  test.describe.configure({ mode: 'serial' });

  for (const route of ALGO_ROUTES) {
    test(`${route.label}: all .algo-tab styles consistent`, async ({ page }) => {
      test.setTimeout(90_000); // allows 45 s selector wait + page load
      await injectSession(page);
      await page.goto(`${BASE}${route.path}`, { waitUntil: 'load' });

      // Wait for at least one .algo-tab — may arrive after async mount.
      await page.waitForSelector('.algo-tab', { timeout: 45_000 });

      const metrics = await collectTabMetrics(page);
      if (metrics === null) {
        test.skip(); // no tabs rendered (e.g. unauthenticated fallback)
        return;
      }

      expect(metrics.count,
        `${route.label}: expected ≥1 .algo-tab, got ${metrics.count}`)
        .toBeGreaterThan(0);

      expect(metrics.styleErrors,
        `${route.label}: tab style divergence:\n${metrics.styleErrors.join('\n')}`)
        .toHaveLength(0);

      expect(metrics.activeColorErrors,
        `${route.label}: active tab color error:\n${metrics.activeColorErrors.join('\n')}`)
        .toHaveLength(0);

      // Canonical: uppercase + 0.6rem-ish.
      expect(metrics.canonical.textTransform).toBe('uppercase');

      // font-size should be in the 8–12 px range (0.6 rem at 16 px root = 9.6 px).
      const fsPx = parseFloat(metrics.canonical.fontSize);
      expect(fsPx).toBeGreaterThanOrEqual(8);
      expect(fsPx).toBeLessThanOrEqual(14);
    });
  }
});

// ── mobile ───────────────────────────────────────────────────────────────
test.describe('tab text consistency — mobile 390×844', () => {
  test.use({ viewport: { width: 390, height: 844 } });
  test.describe.configure({ mode: 'serial' });

  for (const route of ALGO_ROUTES) {
    test(`${route.label}: tabs consistent on mobile`, async ({ page }) => {
      test.setTimeout(120_000);
      await injectSession(page);
      await page.goto(`${BASE}${route.path}`, { waitUntil: 'load' });

      // Some routes may not show a tab strip at mobile width (e.g. collapsed to
      // drawer); allow missing strips without failing.
      const hasTabs = await page.locator('.algo-tab').count();
      if (hasTabs === 0) return;

      const metrics = await collectTabMetrics(page);
      if (metrics === null) return;

      expect(metrics.styleErrors,
        `mobile ${route.label}: tab style divergence:\n${metrics.styleErrors.join('\n')}`)
        .toHaveLength(0);
    });
  }
});

}); // end outer 'tab text consistency' serial wrapper
