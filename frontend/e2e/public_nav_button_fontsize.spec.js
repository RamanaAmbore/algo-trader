// Verify the public navbar right-side buttons (.pub-nav-algo-btn,
// .pub-nav-signin) render at the same font-size as the main nav
// links (.pub-nav-btn). Previously they were 0.78 / 0.82 rem vs the
// nav-btn 0.88 rem — visually smaller.

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';

test(`right-side public nav buttons match nav-link font-size [${BASE}]`, async ({ page }) => {
  await page.goto(BASE);
  // Wait for the public navbar to be present
  await page.waitForSelector('.pub-nav-btn', { state: 'visible', timeout: 10_000 });

  const navBtnSize = await page.locator('.pub-nav-btn').first().evaluate(
    el => getComputedStyle(el).fontSize
  );

  // The Algo Site / Rambo Terminal pill
  const algoBtnSize = await page.locator('.pub-nav-algo-btn').first().evaluate(
    el => getComputedStyle(el).fontSize
  );

  // Sign In button is only there when anonymous (no token).
  const signinLocator = page.locator('.pub-nav-signin');
  const signinCount = await signinLocator.count();
  let signinSize = null;
  if (signinCount > 0) {
    signinSize = await signinLocator.first().evaluate(
      el => getComputedStyle(el).fontSize
    );
  }

  console.log(`nav-btn=${navBtnSize}  algo-btn=${algoBtnSize}  signin=${signinSize ?? 'n/a'}`);

  expect(algoBtnSize).toBe(navBtnSize);
  if (signinSize !== null) {
    expect(signinSize).toBe(navBtnSize);
  }
});
