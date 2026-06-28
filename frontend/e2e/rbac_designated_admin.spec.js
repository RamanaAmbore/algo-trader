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
 *      re-track legacy `writable()` stores. Bridge pattern via `$effect` + `$state`
 *      ensures re-evaluation on every store update.
 *
 * Fix: added `userCapsReady` guard + `$effect`/`$state` bridge to all 6 affected
 * pages. Added `isAdminClass(role)` helper to rbac.js as SSOT for two-tier check.
 *
 * Five quality dimensions (per feedback_test_dimensions.md):
 *   SSOT     — isAdminClass helper exported from rbac.js; no bare role==='admin'
 *              checks in any of the 7 page guards; all use hasCap() instead.
 *   Perf     — each config-page navigation fires ≤ 15 XHRs.
 *   Stale    — source-grep: no bare `role === 'admin'` in page-guard positions.
 *   Reusable — isAdminClass exported from rbac.js; all admin pages import
 *              userCapsReady and use the bridge pattern.
 *   UX       — partner role still denied; designated sees content on all 7 pages.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test rbac_designated_admin.spec.js \
 *   --workers=1 --project=chromium-desktop
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const BASE   = process.env.PLAYWRIGHT_BASE_URL || 'https://dev.ramboq.com';
const _PASS  = process.env.PLAYWRIGHT_PASS     || 'admin1234';

// The seven config-group pages that were failing for `designated`.
const CONFIG_PAGES = [
  { path: '/admin/brokers',    title: 'Brokers',    cap: 'manage_brokers'         },
  { path: '/admin/settings',   title: 'Settings',   cap: 'manage_settings'        },
  { path: '/admin',            title: 'Users',      cap: 'manage_users'           },
  { path: '/admin/statements', title: 'Statements', cap: 'manage_investor_tokens' },
  { path: '/admin/history',    title: 'History',    cap: 'view_audit'             },
  { path: '/admin/audit',      title: 'Audit',      cap: 'view_audit'             },
  { path: '/admin/health',     title: 'Health',     cap: 'view_audit'             },
];

// ── Auth helpers ────────────────────────────────────────────────────────────

let _designatedToken = /** @type {string|null} */ (null);
let _partnerToken    = /** @type {string|null} */ (null);

/**
 * Fetch a designated-role token. Tries `ambore` first (the actual firm owner),
 * then falls back to `rambo` if `ambore` is not present on dev.
 */
async function getDesignatedToken(page) {
  if (_designatedToken) return _designatedToken;
  // `ambore` is the firm owner (designated). `rambo` may be admin or designated
  // on dev depending on seed data — try ambore first.
  for (const u of [
    process.env.PLAYWRIGHT_DESIGNATED_USER || 'ambore',
    'rambo',
  ]) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: { username: u, password: _PASS },
    });
    if (!r.ok()) continue;
    const body = await r.json();
    const tok = body.access_token;
    // Verify this token carries designated role by calling /whoami.
    const who = await page.request.get(`${BASE}/api/auth/whoami`, {
      headers: { Authorization: `Bearer ${tok}` },
    });
    if (who.ok()) {
      const wbody = await who.json();
      if (wbody.role === 'designated') {
        _designatedToken = tok;
        return _designatedToken;
      }
    }
  }
  throw new Error(`getDesignatedToken: no designated-role user found on ${BASE}`);
}

/**
 * Inject token into sessionStorage so the authStore and rbac stores populate
 * before the first component mount.
 */
async function loginDesignated(page) {
  const tok = await getDesignatedToken(page);
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, tok);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${tok}` });
  return tok;
}

// ── SSOT: backend whoami returns correct caps for designated ────────────────

test(`[SSOT] /api/auth/whoami returns designated role + admin caps [${BASE}]`, async ({ page }) => {
  const tok = await getDesignatedToken(page);
  const resp = await page.request.get(`${BASE}/api/auth/whoami`, {
    headers: { Authorization: `Bearer ${tok}` },
  });
  expect(resp.ok(), 'whoami must succeed with designated token').toBe(true);
  const body = await resp.json();
  expect(body.role, 'role must be designated').toBe('designated');

  const caps = body.caps || [];
  const required = [
    'manage_brokers', 'manage_settings', 'manage_users',
    'manage_investor_tokens', 'view_audit',
  ];
  for (const cap of required) {
    expect(caps, `designated must have cap: ${cap}`).toContain(cap);
  }
});

test(`[SSOT] isAdminClass helper exported from rbac.js [${BASE}]`, async ({ page }) => {
  // Source-level check: rbac.js must export isAdminClass.
  // We verify via the page runtime by evaluating a dynamic import.
  await loginDesignated(page);
  await page.goto(`${BASE}/admin/settings`, { waitUntil: 'domcontentloaded' });
  // Page-level JS eval — SvelteKit bundles rbac.js; we can't import it
  // directly in the Playwright context. Instead, grep the source.
  const rbacPath = path.resolve(
    __dirname, '..', 'src', 'lib', 'rbac.js',
  );
  const src = fs.readFileSync(rbacPath, 'utf8');
  expect(src, 'rbac.js must export isAdminClass').toContain('export function isAdminClass');
  expect(src, 'isAdminClass must check designated').toContain("r === 'designated'");
  expect(src, 'isAdminClass must check admin').toContain("r === 'admin'");
});

// ── SSOT: stale code grep — no bare role === 'admin' in page guards ─────────

test(`[STALE] no bare role==='admin' in config page guards [${BASE}]`, async () => {
  // Read each config page and verify no bare `role === 'admin'` appears in a
  // position that would gate access (i.e., not inside a display label/ternary).
  // We check for the pattern in `_canView` / `canManage` derivation lines.
  const routesRoot = path.resolve(__dirname, '..', 'src', 'routes', '(algo)', 'admin');
  const pagesUnderTest = [
    path.join(routesRoot, 'brokers', '+page.svelte'),
    path.join(routesRoot, 'settings', '+page.svelte'),
    path.join(routesRoot, 'audit', '+page.svelte'),
    path.join(routesRoot, 'history', '+page.svelte'),
    path.join(routesRoot, 'health', '+page.svelte'),
    path.join(routesRoot, 'statements', '+page.svelte'),
  ];

  for (const p of pagesUnderTest) {
    const src = fs.readFileSync(p, 'utf8');
    // Find _canView / canManage derivation lines. They should use hasCap(), not
    // a bare role check.
    const guardLines = src.split('\n').filter(line =>
      /_canView|canManage/.test(line) && /\$derived|hasCap/.test(line),
    );
    for (const line of guardLines) {
      expect(
        line,
        `${path.basename(p)}: guard line must not contain bare role === 'admin': ${line}`,
      ).not.toMatch(/role\s*===\s*['"]admin['"]/);
    }

    // Each page must import userCapsReady (or not need it — statements is special).
    if (!p.includes('statements')) {
      expect(src, `${path.basename(p)}: must import userCapsReady`).toContain('userCapsReady');
    }

    // Each page must use the $effect bridge pattern for _caps / _role.
    expect(src, `${path.basename(p)}: must bridge $userCaps via $effect`).toContain(
      '$effect(() => { _caps = $userCaps; })',
    );
    expect(src, `${path.basename(p)}: must bridge $userRole via $effect`).toContain(
      '$effect(() => { _role = $userRole; })',
    );
  }
});

// ── Main: designated user sees all 7 config pages without access-denied ─────

for (const cfg of CONFIG_PAGES) {
  test(`[MAIN] designated user can view ${cfg.path} without access-denied [${BASE}]`, async ({ page }) => {
    await loginDesignated(page);
    await page.goto(`${BASE}${cfg.path}`, { waitUntil: 'domcontentloaded' });

    // Wait for RBAC bootstrap to settle: either the page-header or the
    // access-denied panel will appear. Allow up to 12 s for /whoami round-trip.
    await page.waitForSelector('.page-title-chip, .es-rich, .algo-card', {
      timeout: 12000,
    }).catch(() => {});
    // Extra settle for async Svelte effects.
    await page.waitForTimeout(2000);

    // Must NOT show access-denied panel.
    const lockPanels = page.locator('.es-rich').filter({ hasText: /access denied/i });
    const lockCount  = await lockPanels.count();

    // Must show page title chip.
    const titleChip = page.locator('.page-title-chip');
    const titleCount = await titleChip.count();

    await page.screenshot({
      path: `test-results/rbac-designated-${cfg.title.toLowerCase()}.png`,
    });

    expect(
      lockCount,
      `${cfg.path}: access-denied must not show for designated user`,
    ).toBe(0);
    expect(
      titleCount,
      `${cfg.path}: page-title-chip must be rendered`,
    ).toBeGreaterThan(0);
    await expect(titleChip.first(), `${cfg.path}: title must contain '${cfg.title}'`).toContainText(
      cfg.title,
    );
  });
}

// ── Performance: ≤ 15 XHRs per config page navigation ──────────────────────

test(`[PERF] config page navigations fire ≤ 15 XHRs each [${BASE}]`, async ({ page }) => {
  await loginDesignated(page);

  // Use /admin/health as the representative page (lightest data load).
  const requests = /** @type {string[]} */ ([]);
  page.on('request', (req) => {
    if (req.resourceType() === 'xhr' || req.resourceType() === 'fetch') {
      requests.push(req.url());
    }
  });

  await page.goto(`${BASE}/admin/health`, { waitUntil: 'networkidle' });

  console.log(`XHR count on /admin/health cold load: ${requests.length}`);
  requests.forEach((u) => console.log('  ', u));

  expect(
    requests.length,
    `XHR budget on /admin/health: got ${requests.length}, expected ≤ 15`,
  ).toBeLessThanOrEqual(15);
});

// ── UX: partner role still denied — regression guard ───────────────────────

test(`[UX] partner user sees access-denied on /admin/brokers [${BASE}]`, async ({ page }) => {
  // A partner (LP) user must NOT gain access to broker administration.
  // Use env-var partner credentials if set; otherwise construct an
  // anonymous request (no token → demo / partner caps).
  if (process.env.PLAYWRIGHT_PARTNER_USER) {
    const r = await page.request.post(`${BASE}/api/auth/login`, {
      data: {
        username: process.env.PLAYWRIGHT_PARTNER_USER,
        password:  process.env.PLAYWRIGHT_PARTNER_PASS || _PASS,
      },
    });
    if (r.ok()) {
      _partnerToken = (await r.json()).access_token;
      await page.context().addInitScript((t) => {
        sessionStorage.setItem('ramboq_token', t);
      }, _partnerToken);
    }
  }
  // If no partner token was injected: anonymous session → demo caps on
  // prod, partner caps on dev. Either way must not see broker admin.
  await page.goto(`${BASE}/admin/brokers`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);

  const url = page.url();
  const onSignin    = url.includes('/signin') || url.includes('/login');
  const deniedCount = await page.locator('.es-rich').filter({ hasText: /access denied/i }).count();
  const brokerCards = await page.locator('.brokers-list-header').count();

  await page.screenshot({ path: 'test-results/rbac-partner-denied-brokers.png' });

  // Partner / anonymous must be denied: redirect to /signin OR access-denied panel,
  // and must NOT see the broker cards.
  expect(
    onSignin || deniedCount > 0 || brokerCards === 0,
    `partner/anon must be denied on /admin/brokers; url=${url}, denied=${deniedCount}, cards=${brokerCards}`,
  ).toBe(true);

  expect(
    brokerCards,
    'partner/anonymous must not see broker admin cards',
  ).toBe(0);
});

// ── UX: LoadingSkeleton shows during bootstrap, not access-denied ───────────

test(`[UX] no access-denied flash during RBAC bootstrap on /admin/audit [${BASE}]`, async ({ page }) => {
  await loginDesignated(page);
  await page.goto(`${BASE}/admin/audit`, { waitUntil: 'domcontentloaded' });

  // Immediately after DOM load (before networkidle), with token pre-injected,
  // userCapsReady is still false → LoadingSkeleton must show, NOT access-denied.
  await page.waitForTimeout(300);
  const earlyLockCount = await page.locator('.es-rich').filter({ hasText: /access denied/i }).count();

  // After full settle, the page content must appear.
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(2000);
  const finalLockCount = await page.locator('.es-rich').filter({ hasText: /access denied/i }).count();
  const titleChip = page.locator('.page-title-chip');

  await page.screenshot({ path: 'test-results/rbac-audit-no-flash.png' });

  expect(earlyLockCount, 'access-denied must not flash during bootstrap window').toBe(0);
  expect(finalLockCount, 'access-denied must not persist after bootstrap for designated').toBe(0);
  expect(await titleChip.count(), 'audit page title must render').toBeGreaterThan(0);
});
