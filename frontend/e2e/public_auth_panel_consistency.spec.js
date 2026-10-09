// Public auth-page panel consistency — P1 style-recipe audit follow-up
// (2026-10)
//
// /signin already retired its local .signin-panel / .signin-header /
// .signin-body rules in favor of the shared .pub-form-panel family
// (app.css) — see the "retired" comment in signin/+page.svelte. The two
// other single-form auth pages (/auth/reset, /auth/change-password)
// never got the memo and kept duplicate local copies of those rules,
// free to silently drift from the shared ones. This spec pins both
// pages to the shared class family and guards against regression.
//
// Also covers the sibling P1 fixes from the same audit pass:
//   - .pub-form-panel's own teal-theme leftovers (sage-grey border,
//     teal-tinted shadow) replaced with the cream-theme surface-border
//     token + a neutral shadow tint.
//   - Primary-CTA radius convergence: .contact-send-btn and
//     .ip-statement-btn now use the same pill radius as /about's
//     .cta-btn-primary (the established "primary CTA" shape), closing
//     the remaining shape inconsistency left after the earlier P0
//     color/contrast fix. .btn-primary (generic, used across many
//     non-hero buttons) and .market-empty-retry (distinct retry
//     affordance) are deliberately left unchanged — not asserted here.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — source-level regex against the actual files, not a
//                hardcoded assumption of what "should" be there.
//   2. Perf    — no login/network needed for the source checks; the
//                live-render checks are plain public-page loads.
//   3. Stale   — explicitly asserts the retired selectors are GONE.
//   4. Reuse   — asserts both auth pages reference the one shared
//                .pub-form-panel* class family, not page-local copies.
//   5. UX      — live-render smoke confirms both pages still render a
//                usable, styled panel after the markup swap.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const AUTH_PAGES = [
  '../src/routes/(public)/auth/reset/+page.svelte',
  '../src/routes/(public)/auth/change-password/+page.svelte',
];

for (const relPath of AUTH_PAGES) {
  test(`stale code — ${relPath} no longer declares local .signin-panel/.signin-header/.signin-body rules`, async () => {
    const src = readFileSync(new URL(relPath, import.meta.url).pathname, 'utf8');
    expect(src, `${relPath} must not declare .signin-panel { ... }`).not.toMatch(/\.signin-panel\s*\{/);
    expect(src, `${relPath} must not declare .signin-header { ... }`).not.toMatch(/\.signin-header\s*\{/);
    expect(src, `${relPath} must not declare .signin-body { ... }`).not.toMatch(/\.signin-body\s*\{/);
  });

  test(`reuse — ${relPath} markup references the shared .pub-form-panel family`, async () => {
    const src = readFileSync(new URL(relPath, import.meta.url).pathname, 'utf8');
    expect(src, `${relPath} must mount .pub-form-panel`).toMatch(/class="pub-form-panel"/);
    expect(src, `${relPath} must mount .pub-form-panel-header`).toMatch(/class="pub-form-panel-header"/);
    expect(src, `${relPath} must mount .pub-form-panel-title`).toMatch(/class="pub-form-panel-title"/);
    expect(src, `${relPath} must mount .pub-form-panel-body`).toMatch(/class="pub-form-panel-body"/);
  });
}

test('stale code — .pub-form-panel no longer carries teal-theme border/shadow leftovers', async () => {
  const css = readFileSync(new URL('../src/app.css', import.meta.url).pathname, 'utf8');
  const match = css.match(/\.pub-form-panel\s*\{([\s\S]*?)\n\s*\}/);
  expect(match, 'app.css must still declare .pub-form-panel { ... }').not.toBeNull();
  // Strip /* ... */ comments before checking — the fix's own explanatory
  // comment mentions the retired hex/rgba values by name, which would
  // otherwise produce a false-positive match against the live rule body.
  const body = match[1].replace(/\/\*[\s\S]*?\*\//g, '');
  expect(body, 'sage-grey teal-leftover border #b4c0bc must be gone').not.toMatch(/#b4c0bc/);
  expect(body, 'teal-tinted shadow rgba(22,53,53,...) must be gone').not.toMatch(/rgba\(\s*22,\s*53,\s*53/);
  expect(body, 'border must use the shared card-surface-border token').toMatch(/var\(--card-surface-border/);
});

test('reuse — CTA pill radius converged: .contact-send-btn and .ip-statement-btn match .cta-btn-primary shape', async () => {
  const contact = readFileSync(
    new URL('../src/routes/(public)/contact/+page.svelte', import.meta.url).pathname, 'utf8'
  );
  const investor = readFileSync(
    new URL('../src/routes/investor/[token]/+page.svelte', import.meta.url).pathname, 'utf8'
  );
  const contactBtn = contact.match(/\.contact-send-btn\s*\{([\s\S]*?)\n\s*\}/);
  const ipBtn = investor.match(/\.ip-statement-btn\s*\{([\s\S]*?)\n\s*\}/);
  expect(contactBtn, '.contact-send-btn rule not found').not.toBeNull();
  expect(ipBtn, '.ip-statement-btn rule not found').not.toBeNull();
  expect(contactBtn[1], '.contact-send-btn must use pill radius (9999px)').toMatch(/border-radius:\s*9999px/);
  expect(ipBtn[1], '.ip-statement-btn must use pill radius (9999px)').toMatch(/border-radius:\s*9999px/);
});

// /auth/change-password bounces to /signin without a logged-in session
// (its onMount guard requires authStore.user/token) — not exercised
// live here since this spec runs unauthenticated; the source-level
// checks above already cover its markup/CSS. /auth/reset has no such
// guard (token comes from the query string, not the session) so it's
// the one page in this pair safe to render live unauthenticated.
for (const [path, title] of [
  ['/auth/reset', 'Reset Password'],
]) {
  test(`live render — ${path} renders the shared panel chrome with correct border radius/color`, async ({ page }) => {
    await page.goto(path, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(300);
    const panel = page.locator('.pub-form-panel');
    await expect(panel).toBeVisible();
    await expect(panel.locator('.pub-form-panel-title')).toHaveText(title);
    const panelStyle = await panel.evaluate((el) => {
      const s = getComputedStyle(el);
      return { borderRadius: s.borderRadius, borderColor: s.borderColor };
    });
    // 6px per .pub-form-panel; confirms the shared rule actually applied
    // (not a page-local override reintroducing drift).
    expect(panelStyle.borderRadius).toBe('6px');
    // #ddd8ce → rgb(221, 216, 206); confirms the teal-leftover border is gone.
    expect(panelStyle.borderColor).toBe('rgb(221, 216, 206)');
  });
}
