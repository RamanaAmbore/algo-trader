// Public mobile header — tagline must never wrap (B9, 2026-09)
//
// Bug (operator-confirmed via real-device screenshot): on the mobile
// header bar in (public)/+layout.svelte, "INVEST · GROW · COMPOUND"
// wrapped to 2-3 lines when logged in, because the `.pub-user-pill`
// (username + role badge) sat in the same `justify-content: space-between`
// row and — via default flex-shrink:1 — squeezed `.pub-brand-mobile`
// below its natural content width. The pill's own text is
// white-space:nowrap so it hit its min-content floor immediately; all
// the deficit landed on the brand block, whose wrappable tagline text
// absorbed the squeeze.
//
// Fix: `.pub-brand-mobile` is pinned to `flex-shrink: 0` (never shrinks
// below natural width) and the tagline gets `white-space: nowrap` as a
// belt-and-suspenders guard. The pill instead yields space: its wrapper
// gets `min-width: 0`, the pill itself gets `min-width: 0` +
// `overflow: hidden` + `text-overflow: ellipsis` so a long username
// ellipsizes instead of forcing overflow, and the role badge
// ("DESIGNATED"/"ADMIN") hides below 480px (recovered via CSS display,
// not a font-size reduction — the 0.7rem legibility floor on
// `.pub-brand-tagline` is untouched, per the B2 invariant).
//
// No real login is required: the (public) layout's authStore rebuilds
// `$authStore.user` purely client-side from the JWT payload (see
// `_readSession` in stores.js), so a locally-signed fake JWT dropped into
// sessionStorage before navigation is sufficient to render the
// `.pub-user-pill` — this also sidesteps rate-limited /api/auth/login
// calls against dev.ramboq.com.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — asserts against the live rendered DOM (bounding boxes /
//                computed styles), not a hardcoded viewport assumption.
//   2. Perf    — no real backend auth round-trip (fake JWT, client-only).
//   3. Stale   — n/a (new coverage); doesn't duplicate
//                public_typography_floor.spec.js (that file only covers
//                the logged-out sweep + 360px logged-out overflow).
//   4. Reuse   — reuses the mobile-portrait project's 360px viewport
//                convention already used elsewhere in this suite.
//   5. UX      — checks both auth states, checks the hamburger tap
//                target + pill don't overlap or get clipped, checks no
//                page-level horizontal overflow.

import { test, expect } from '@playwright/test';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';
const WIDTHS = [360, 390, 412];
// One line at 0.7rem (11.2px) computed font-size / line-height is
// ~11.2px tall; allow generous headroom before calling it a wrap.
const ONE_LINE_MAX_PX = 16;

function fakeJwt(payload) {
  const b64url = (obj) => Buffer.from(JSON.stringify(obj)).toString('base64')
    .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  return `${b64url({ alg: 'none' })}.${b64url(payload)}.fakesig`;
}

async function setLoggedIn(page, username = 'ambore') {
  const token = fakeJwt({ sub: username, role: 'designated', display_name: username });
  await page.context().addInitScript((t) => {
    sessionStorage.setItem('ramboq_token', t);
  }, token);
}

async function readHeaderMetrics(page) {
  return page.evaluate(() => {
    const mobileBar = document.querySelector('.pub-nav-inner.md\\:hidden');
    const tl = mobileBar?.querySelector('.pub-brand-tagline');
    const pill = mobileBar?.querySelector('.pub-user-pill');
    const hamburger = mobileBar?.querySelector('.pub-hamburger');
    const tlRect = tl ? tl.getBoundingClientRect() : null;
    const pillRect = pill ? pill.getBoundingClientRect() : null;
    const hamRect = hamburger ? hamburger.getBoundingClientRect() : null;
    return {
      taglineHeight: tlRect ? tlRect.height : null,
      taglineText: tl ? tl.textContent.trim() : null,
      pillPresent: !!pill,
      pillRight: pillRect ? pillRect.right : null,
      hamburgerLeft: hamRect ? hamRect.left : null,
      hamburgerRight: hamRect ? hamRect.right : null,
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    };
  });
}

for (const width of WIDTHS) {
  test(`logged out @ ${width}px — tagline single line, no overflow`, async ({ page }) => {
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`${BASE}/about`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(300);
    const m = await readHeaderMetrics(page);
    expect(m.taglineText).toBe('INVEST · GROW · COMPOUND');
    expect(m.taglineHeight, `tagline wrapped (height=${m.taglineHeight}px)`).toBeLessThan(ONE_LINE_MAX_PX);
    expect(m.pillPresent, 'no user pill expected when logged out').toBe(false);
    expect(m.scrollWidth).toBeLessThanOrEqual(m.clientWidth + 1);
  });

  test(`logged in @ ${width}px — tagline single line despite user pill`, async ({ page }) => {
    await setLoggedIn(page);
    await page.setViewportSize({ width, height: 800 });
    await page.goto(`${BASE}/about`, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(300);
    const m = await readHeaderMetrics(page);
    expect(m.taglineText).toBe('INVEST · GROW · COMPOUND');
    // The regression: this was 2-3 lines (>22px) before the fix.
    expect(m.taglineHeight, `tagline wrapped (height=${m.taglineHeight}px)`).toBeLessThan(ONE_LINE_MAX_PX);
    // Pill must actually be present (not vacuously passing because the
    // logged-in state silently failed to render).
    expect(m.pillPresent, 'user pill must render when logged in').toBe(true);
    // Pill must not overlap or get pushed past the hamburger, and the
    // hamburger tap target must stay fully on-screen.
    expect(m.pillRight).toBeLessThanOrEqual(m.hamburgerLeft + 1);
    expect(m.hamburgerRight).toBeLessThanOrEqual(m.clientWidth + 1);
    expect(m.scrollWidth, `page overflowed horizontally at ${width}px`).toBeLessThanOrEqual(m.clientWidth + 1);
  });
}

test('logged in, long username @ 360px — pill ellipsizes instead of wrapping the tagline or overflowing', async ({ page }) => {
  await setLoggedIn(page, 'verylongusername');
  await page.setViewportSize({ width: 360, height: 800 });
  await page.goto(`${BASE}/about`, { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(300);
  const m = await readHeaderMetrics(page);
  expect(m.taglineHeight, `tagline wrapped (height=${m.taglineHeight}px)`).toBeLessThan(ONE_LINE_MAX_PX);
  expect(m.pillPresent).toBe(true);
  expect(m.pillRight).toBeLessThanOrEqual(m.hamburgerLeft + 1);
  expect(m.scrollWidth).toBeLessThanOrEqual(m.clientWidth + 1);
});
