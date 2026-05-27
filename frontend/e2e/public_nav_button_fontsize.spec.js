// Verify the public navbar right-side buttons (.pub-nav-algo-btn,
// .pub-nav-signin) render at the same font-size as the main nav
// links (.pub-nav-btn). Previously they were 0.78 / 0.82 rem vs the
// nav-btn 0.88 rem — visually smaller.

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';

test(`right-side public nav buttons match nav-link font-size [${BASE}]`, async ({ page }) => {
  // Set a wide enough viewport that the desktop nav (.pub-nav-btn)
  // renders. On the mobile-portrait project the desktop nav is hidden
  // behind a hamburger; we still want to verify the right-side
  // buttons but compare against a known target — 0.88rem = 14.08px
  // at the default 16px root font.
  const TARGET_PX = '14.08px';   // 0.88rem

  await page.goto(BASE, { waitUntil: 'networkidle' });

  const algoBtnLoc = page.locator('.pub-nav-algo-btn').first();
  await algoBtnLoc.waitFor({ state: 'attached', timeout: 15_000 });
  const algoBtnSize = await algoBtnLoc.evaluate(el => getComputedStyle(el).fontSize);

  // Sign In button is only there when anonymous (no token).
  let signinSize = null;
  if (await page.locator('.pub-nav-signin').count() > 0) {
    signinSize = await page.locator('.pub-nav-signin').first().evaluate(
      el => getComputedStyle(el).fontSize
    );
  }

  // The desktop nav-btn row is hidden under the hamburger on mobile
  // viewports — only assert against it when at least one is visible.
  let navBtnSize = null;
  const navBtnLoc = page.locator('.pub-nav-btn').first();
  if (await navBtnLoc.count() > 0) {
    const visible = await navBtnLoc.isVisible().catch(() => false);
    if (visible) {
      navBtnSize = await navBtnLoc.evaluate(el => getComputedStyle(el).fontSize);
    }
  }

  console.log(`nav-btn=${navBtnSize}  algo-btn=${algoBtnSize}  signin=${signinSize ?? 'n/a'}`);

  // The right-side buttons must hit the target on EVERY viewport
  // (they remain visible even when the main nav row collapses).
  expect(algoBtnSize).toBe(TARGET_PX);
  if (signinSize !== null) {
    expect(signinSize).toBe(TARGET_PX);
  }
  // When the main nav is visible, confirm it matches too — that's
  // the whole point (right-side buttons now equal the nav-link size).
  if (navBtnSize !== null) {
    expect(algoBtnSize).toBe(navBtnSize);
  }
});
