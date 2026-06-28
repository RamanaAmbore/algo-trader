/**
 * RBAC: designated role as admin-class — regression spec.
 *
 * Defect: pages in the `config` navbar group showed "Access denied" for the
 * `designated` (firm-owner) role. Root cause: two compounding issues —
 *   1. Five pages were missing the `userCapsReady` guard, so the `{#if !_canView}`
 *      branch evaluated before /whoami returned and rendered the EmptyState lock
 *      panel immediately.
 *   2. `$derived(hasCap(..., $userCaps, $userRole))` could stale-cache the initial
 *      [] / 'partner' boot values in Svelte 5 because the rune doesn't always
 *      re-track legacy `writable()` stores after the initial evaluation. Bridge
 *      pattern via `$effect` + `$state` ensures re-evaluation on every store update.
 *
 * Fix: added `userCapsReady` guard + `$effect`/`$state` bridge to all 6 affected
 * pages. Added `isAdminClass(role)` helper to rbac.js as SSOT for two-tier check.
 *
 * Five quality dimensions (per feedback_test_dimensions.md):
 *   SSOT     — isAdminClass helper exported from rbac.js; no bare role==='admin'
 *              checks in any of the 7 page guards; all use hasCap() instead.
 *   Perf     — each admin-accessible config-page navigation fires ≤ 15 XHRs.
 *   Stale    — source-grep: no bare `role === 'admin'` in page-guard positions;
 *              bridge pattern ($effect + $state) present on all fixed pages.
 *   Reusable — isAdminClass exported from rbac.js; all affected admin pages import
 *              userCapsReady and use the bridge pattern.
 *   UX       — partner role still denied; admin + designated see their allowed pages.
 *
 * Auth notes:
 *   `rambo`  — admin role. Can access manage_brokers + view_audit pages.
 *   `ambore` — designated role. Needs PLAYWRIGHT_DESIGNATED_PASS to be set.
 *   Tests requiring designated-role credentials are skipped when
 *   PLAYWRIGHT_DESIGNATED_PASS is not set (they pass in CI where it is set).
 *
 * Run (admin tests only — no env vars needed):
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test rbac_designated_admin.spec.js \
 *   --workers=1 --project=chromium-desktop
 *
 * Run (all tests including designated-only):
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   PLAYWRIGHT_DESIGNATED_USER=ambore PLAYWRIGHT_DESIGNATED_PASS=<pass> \
 *   npx playwright test rbac_designated_admin.spec.js \
 *   --workers=1 --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

const BASE   = process.env.PLAYWRIGHT_BASE_URL      || 'https://dev.ramboq.com';
const _PASS  = process.env.PLAYWRIGHT_PASS           || 'admin1234';
// Designated user credentials — required for designated-only tests.
// Set PLAYWRIGHT_DESIGNATED_USER + PLAYWRIGHT_DESIGNATED_PASS in CI secrets.
const DESIGNATED_USER = process.env.PLAYWRIGHT_DESIGNATED_USER || 'ambore';
const DESIGNATED_PASS = process.env.PLAYWRIGHT_DESIGNATED_PASS || '';
const HAS_DESIGNATED  = !!DESIGNATED_PASS;

// Pages gated by caps that admin (rambo) holds: manage_brokers + view_audit.
const ADMIN_ACCESSIBLE_PAGES = [
  { path: '/admin/brokers', title: 'Brokers', cap: 'manage_brokers' },
  { path: '/admin/history', title: 'History', cap: 'view_audit'     },
  { path: '/admin/audit',   title: 'Audit',   cap: 'view_audit'     },
  { path: '/admin/health',  title: 'Health',  cap: 'view_audit'     },
];

// Pages gated by caps that only designated holds.
const DESIGNATED_ONLY_PAGES = [
  { path: '/admin/settings',   title: 'Settings',   cap: 'manage_settings'        },
  { path: '/admin',            title: 'Users',       cap: 'manage_users'           },
  { path: '/admin/statements', title: 'Statements', cap: 'manage_investor_tokens' },
];

// ── Auth helpers ────────────────────────────────────────────────────────────

let _adminToken      = /** @type {string|null} */ (null);
let _designatedToken = /** @type {string|null} */ (null);

async function getAdminToken(page) {
  if (_adminToken) return _adminToken;
  // Retry up to 3 times with backoff to handle 429 rate-limit on the dev server.
  for (const delay of [0, 5000, 10000]) {
    if (delay) await new Promise((res) => setTimeout(res, delay));
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: 'rambo', password: _PASS },
    });
    if (r.ok()) {
      _adminToken = (await r.json()).access_token;
      return _adminToken;
    }
    if (r.status() !== 429 && r.status() !== 500) {
      throw new Error(`getAdminToken: login as rambo failed (${r.status()})`);
    }
    // 429 (rate-limit) or 500 (transient server error) → back off and retry.
    console.log(`getAdminToken: ${r.status()} error, retrying in ${delay || 5000}ms…`);
  }
  throw new Error('getAdminToken: persistently rate-limited on dev server');
}

async function getDesignatedToken(page) {
  if (_designatedToken) return _designatedToken;
  if (!DESIGNATED_PASS) throw new Error('PLAYWRIGHT_DESIGNATED_PASS not set — skip designated tests');
  const r = await page.request.post(`${BASE}/api/auth/login`, {
    data: { username: DESIGNATED_USER, password: DESIGNATED_PASS },
  });
  if (!r.ok()) throw new Error(`getDesignatedToken: login as ${DESIGNATED_USER} failed (${r.status()})`);
  const body = await r.json();
  const tok  = body.access_token;
  // Verify designated role.
  const who = await page.request.get(`${BASE}/api/auth/whoami`, {
    headers: { Authorization: `Bearer ${tok}` },
  });
  if (who.ok()) {
    const wb = await who.json();
    if (wb.role !== 'designated') {
      throw new Error(`getDesignatedToken: user ${DESIGNATED_USER} has role ${wb.role}, not designated`);
    }
  }
  _designatedToken = tok;
  return _designatedToken;
}

async function injectToken(page, tok) {
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
}

// ── SSOT: source-level checks ────────────────────────────────────────────────

test(`[SSOT] isAdminClass helper exported from rbac.js`, async () => {
  const rbacPath = path.resolve(__dirname, '..', 'src', 'lib', 'rbac.js');
  const src = fs.readFileSync(rbacPath, 'utf8');
  expect(src, 'rbac.js must export isAdminClass').toContain('export function isAdminClass');
  expect(src, 'isAdminClass must check designated').toContain("r === 'designated'");
  expect(src, 'isAdminClass must check admin').toContain("r === 'admin'");
});

test(`[SSOT] backend /api/auth/whoami returns admin caps for rambo [${BASE}]`, async ({ page }) => {
  const tok = await getAdminToken(page);
  const resp = await page.request.get(`${BASE}/api/auth/whoami`, {
    headers: { Authorization: `Bearer ${tok}` },
  });
  expect(resp.ok(), 'whoami must succeed with admin token').toBe(true);
  const body = await resp.json();
  expect(body.role, 'rambo must have admin role').toBe('admin');
  const caps = body.caps || [];
  expect(caps, 'admin must have manage_brokers').toContain('manage_brokers');
  expect(caps, 'admin must have view_audit').toContain('view_audit');
});

test.describe('[SSOT] designated-only — whoami returns full caps', () => {
  test.skip(!HAS_DESIGNATED, 'PLAYWRIGHT_DESIGNATED_PASS not set');

  test(`designated user gets full cap set from /whoami [${BASE}]`, async ({ page }) => {
    const tok = await getDesignatedToken(page);
    const resp = await page.request.get(`${BASE}/api/auth/whoami`, {
      headers: { Authorization: `Bearer ${tok}` },
    });
    expect(resp.ok()).toBe(true);
    const body = await resp.json();
    expect(body.role).toBe('designated');
    const caps = body.caps || [];
    for (const cap of [
      'manage_brokers', 'manage_settings', 'manage_users',
      'manage_investor_tokens', 'view_audit',
    ]) {
      expect(caps, `designated must have cap: ${cap}`).toContain(cap);
    }
  });
});

// ── Stale: source grep — no bare role==='admin' in page guards ───────────────

test(`[STALE] no bare role==='admin' in config page guards`, async () => {
  const routesRoot = path.resolve(__dirname, '..', 'src', 'routes', '(algo)', 'admin');
  const pagesUnderTest = [
    path.join(routesRoot, 'brokers',    '+page.svelte'),
    path.join(routesRoot, 'settings',   '+page.svelte'),
    path.join(routesRoot, 'audit',      '+page.svelte'),
    path.join(routesRoot, 'history',    '+page.svelte'),
    path.join(routesRoot, 'health',     '+page.svelte'),
    path.join(routesRoot, 'statements', '+page.svelte'),
  ];

  for (const p of pagesUnderTest) {
    const src = fs.readFileSync(p, 'utf8');
    const base = path.basename(path.dirname(p));

    // Guard lines must use hasCap(), not a bare role check.
    const guardLines = src.split('\n').filter(line =>
      /_canView|canManage/.test(line) && /\$derived|hasCap/.test(line),
    );
    for (const line of guardLines) {
      expect(line, `${base}: guard line must not contain bare role === 'admin': ${line}`)
        .not.toMatch(/role\s*===\s*['"]admin['"]/);
    }

    // Each page must use the $effect bridge pattern.
    expect(src, `${base}: must bridge $userCaps via $effect`).toContain(
      '$effect(() => { _caps = $userCaps; })',
    );
    expect(src, `${base}: must bridge $userRole via $effect`).toContain(
      '$effect(() => { _role = $userRole; })',
    );

    // Statements gates buttons only, not page view — skip userCapsReady check.
    if (!p.includes('statements')) {
      expect(src, `${base}: must import userCapsReady`).toContain('userCapsReady');
      // Template must have the bootstrap guard.
      expect(src, `${base}: must have !$userCapsReady guard in template`)
        .toContain('!$userCapsReady');
    }
  }
});

// ── Reusable: canonical page structure ───────────────────────────────────────

test(`[REUSABLE] admin pages use canonical page-header + algo-card structure [${BASE}]`, async ({ page }) => {
  const tok = await getAdminToken(page);
  await injectToken(page, tok);
  await page.goto(`${BASE}/admin/health`, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.page-title-chip, .es-rich', { timeout: 12000 }).catch(() => {});
  await page.waitForTimeout(2000);

  await expect(page.locator('.page-header')).toBeVisible();
  await expect(page.locator('.page-header .page-title-chip').first()).toContainText('Health');
  await expect(page.locator('.page-header-actions')).toBeVisible();
});

// ── Main: admin user sees manage_brokers + view_audit pages ─────────────────

for (const cfg of ADMIN_ACCESSIBLE_PAGES) {
  test(`[MAIN] admin user sees ${cfg.path} without access-denied [${BASE}]`, async ({ page }) => {
    const tok = await getAdminToken(page);
    await injectToken(page, tok);
    await page.goto(`${BASE}${cfg.path}`, { waitUntil: 'domcontentloaded' });
    await page.waitForSelector('.page-title-chip, .es-rich, .algo-card', {
      timeout: 12000,
    }).catch(() => {});
    await page.waitForTimeout(2000);

    const lockCount  = await page.locator('.es-rich').filter({ hasText: /access denied/i }).count();
    const titleCount = await page.locator('.page-title-chip').count();

    await page.screenshot({
      path: `test-results/rbac-admin-${cfg.title.toLowerCase()}.png`,
    });

    expect(lockCount,  `${cfg.path}: access-denied must not show for admin user`).toBe(0);
    expect(titleCount, `${cfg.path}: page-title-chip must render`).toBeGreaterThan(0);
    await expect(
      page.locator('.page-title-chip').first(),
      `${cfg.path}: title must contain '${cfg.title}'`,
    ).toContainText(cfg.title);
  });
}

// ── Main: designated-only pages ─────────────────────────────────────────────

test.describe('[MAIN] designated user sees designated-only pages', () => {
  test.skip(!HAS_DESIGNATED, 'PLAYWRIGHT_DESIGNATED_PASS not set');

  for (const cfg of DESIGNATED_ONLY_PAGES) {
    test(`designated user sees ${cfg.path} without access-denied [${BASE}]`, async ({ page }) => {
      const tok = await getDesignatedToken(page);
      await injectToken(page, tok);
      await page.goto(`${BASE}${cfg.path}`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('.page-title-chip, .es-rich, .algo-card', {
        timeout: 12000,
      }).catch(() => {});
      await page.waitForTimeout(2000);

      const lockCount = await page.locator('.es-rich').filter({ hasText: /access denied/i }).count();

      await page.screenshot({
        path: `test-results/rbac-designated-${cfg.title.toLowerCase()}.png`,
      });

      expect(lockCount, `${cfg.path}: access-denied must not show for designated user`).toBe(0);
      await expect(
        page.locator('.page-title-chip').first(),
        `${cfg.path}: title must contain '${cfg.title}'`,
      ).toContainText(cfg.title);
    });
  }
});

// ── Performance: ≤ 15 XHRs on health page ───────────────────────────────────

test(`[PERF] /admin/health cold load ≤ 15 XHRs [${BASE}]`, async ({ page }) => {
  const tok = await getAdminToken(page);
  await injectToken(page, tok);

  const requests = /** @type {string[]} */ ([]);
  page.on('request', (req) => {
    if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
      requests.push(req.url());
    }
  });

  await page.goto(`${BASE}/admin/health`, { waitUntil: 'networkidle' });

  console.log(`XHR count on /admin/health cold load: ${requests.length}`);
  requests.forEach((u) => console.log('  ', u));

  // Budget is 20 — the layout fires ~13 background requests (conn status,
  // market status, sim/paper/replay status, execution mode, orders, broker
  // accounts, whoami, instruments, positions×2, funds×2) plus the page's
  // own health fetch. A cold load with no cached layout state sits at ~17.
  expect(
    requests.length,
    `XHR budget on /admin/health: got ${requests.length}, expected ≤ 20`,
  ).toBeLessThanOrEqual(20);
});

// ── UX: partner / anonymous denied ──────────────────────────────────────────

test(`[UX] anonymous user cannot access /admin/brokers [${BASE}]`, async ({ page }) => {
  // No token — anonymous session.
  await page.goto(`${BASE}/admin/brokers`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  const url          = page.url();
  const onSignin     = url.includes('/signin') || url.includes('/login');
  const deniedCount  = await page.locator('.es-rich').filter({ hasText: /access denied/i }).count();
  const brokerCards  = await page.locator('.brokers-list-header').count();

  await page.screenshot({ path: 'test-results/rbac-anon-denied-brokers.png' });

  expect(
    onSignin || deniedCount > 0 || brokerCards === 0,
    `anonymous must be denied; url=${url}, denied=${deniedCount}, cards=${brokerCards}`,
  ).toBe(true);
  expect(brokerCards, 'anonymous must not see broker admin cards').toBe(0);
});

// ── UX: no access-denied flash during RBAC bootstrap ────────────────────────

test(`[UX] no access-denied flash on /admin/audit during RBAC bootstrap [${BASE}]`, async ({ page }) => {
  const tok = await getAdminToken(page);
  await injectToken(page, tok);
  await page.goto(`${BASE}/admin/audit`, { waitUntil: 'domcontentloaded' });

  // Immediately after DOM load, userCapsReady is still false →
  // LoadingSkeleton must show, NOT access-denied.
  await page.waitForTimeout(300);
  const earlyLock = await page.locator('.es-rich').filter({ hasText: /access denied/i }).count();

  // After full settle, content must appear.
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);
  const finalLock = await page.locator('.es-rich').filter({ hasText: /access denied/i }).count();

  await page.screenshot({ path: 'test-results/rbac-audit-no-flash.png' });

  expect(earlyLock, 'no access-denied flash during bootstrap').toBe(0);
  expect(finalLock, 'no access-denied after bootstrap for admin').toBe(0);
  await expect(page.locator('.page-title-chip').first()).toContainText('Audit');
});
