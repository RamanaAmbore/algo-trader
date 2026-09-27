// FAQ page — SSR crash regression guard
//
// Bug: a raw (non-hydrated) request to /faq returned HTTP 500 —
// `ReferenceError: document is not defined` at faq/+page.svelte:98, inside
// an `onDestroy` callback that unconditionally set `document.body.style.
// overflow`. Root cause: unlike `onMount` (client-only), Svelte's
// `onDestroy` also runs during SSR — the server has no real "component
// destroyed" event, so the callback fires immediately after render to
// mimic teardown. `document` doesn't exist in that context, so any
// unguarded reference blows up EVERY non-JS request (Googlebot, direct-link
// previews, curl) even though real browser navigation was unaffected
// (client-side nav hydrates before the destroy path can run).
//
// Fix: `import { browser } from '$app/environment'` + `if (browser)` guard
// around the `document` reference inside `onDestroy`.
//
// A browser-based (JS-enabled) Playwright test would NOT catch this class
// of bug — Playwright's default browser context always hydrates. This spec
// uses the `request` fixture instead, which performs a plain HTTP GET with
// no JS execution, exercising the exact SSR path a crawler or curl hits.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — hits the real route, not a mocked render.
//   2. Perf    — a single unauthenticated GET; no login, no navigation.
//   3. Stale   — grep guard: onDestroy's document reference must stay
//                browser-guarded.
//   4. Reuse   — plain `request` fixture, the standard SvelteKit SSR-check
//                pattern (no bespoke harness).
//   5. UX      — asserts real page content rendered server-side (not just
//                "no 500"), so a swallowed error masked as a 200 shell
//                would still be caught.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

test('stale code — faq onDestroy document reference stays browser-guarded', () => {
  const src = readFileSync(
    new URL('../src/routes/(public)/faq/+page.svelte', import.meta.url).pathname,
    'utf8'
  );
  expect(src, "faq +page.svelte must import browser from $app/environment")
    .toMatch(/import\s*\{\s*browser\s*\}\s*from\s*'\$app\/environment'/);
  expect(src, 'onDestroy must guard the document reference with `if (browser)`')
    .toMatch(/onDestroy\(\(\)\s*=>\s*\{\s*if\s*\(browser\)\s*document\.body\.style\.overflow/);
});

test('SSR — raw (non-JS) request to /faq renders 200, not 500', async ({ request }) => {
  const res = await request.get('/faq');
  expect(res.status(), 'raw /faq request must not 500').toBe(200);

  const body = await res.text();
  // Proves the server actually rendered FAQ content server-side (not just
  // an empty shell that swallowed the error) — first FAQ question is
  // baked into the initial SSR markup.
  expect(body, 'SSR markup must contain rendered FAQ content')
    .toContain('What is RamboQuant Analytics LLP?');
});

test('SSR — /faq request with JS disabled renders without a client-side crash banner', async ({ browser: browserType }) => {
  // Belt-and-suspenders: a real browser context with JS off still goes
  // through the SSR render path (no hydration masks it), giving an
  // end-to-end check beyond the raw `request` fixture above.
  const ctx = await browserType.newContext({ javaScriptEnabled: false });
  const page = await ctx.newPage();
  const res = await page.goto('/faq', { waitUntil: 'load' });
  expect(res.status(), 'no-JS /faq navigation must not 500').toBe(200);
  await expect(page.getByText('What is RamboQuant Analytics LLP?')).toBeVisible();
  await ctx.close();
});
