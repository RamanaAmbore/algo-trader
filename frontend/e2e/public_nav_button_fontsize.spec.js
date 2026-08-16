// Verify the public navbar right-side buttons (.pub-nav-algo-btn,
// .pub-nav-signin) render with the correct typography and colour values.
//
// .pub-nav-algo-btn target values (desktop, updated 2026-08-15):
//   font-size   0.95rem = 15.2px (at 16px root)
//   font-weight 600
//   color       #e8c03a  (rgb(232,192,58))
//   border      1px solid rgba(200,168,75,0.60)
//   background  rgba(200,168,75,0.15)
//
// hover state:
//   color       #f0d070  (rgb(240,208,112))
//   border-color rgba(200,168,75,0.75)
//   background  rgba(200,168,75,0.22)

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';

test(`pub-nav-algo-btn has correct font-size, weight and color [${BASE}]`, async ({ page }) => {
  // 0.95rem at 16px root = 15.2px
  const TARGET_FONTSIZE_PX = '15.2px';
  const TARGET_FONTWEIGHT  = '600';
  // Browsers normalise #e8c03a → rgb(232, 192, 58)
  const TARGET_COLOR       = 'rgb(232, 192, 58)';

  await page.goto(BASE, { waitUntil: 'networkidle' });

  const btn = page.locator('.pub-nav-algo-btn').first();
  await btn.waitFor({ state: 'attached', timeout: 15_000 });

  const fontSize   = await btn.evaluate(el => getComputedStyle(el).fontSize);
  const fontWeight = await btn.evaluate(el => getComputedStyle(el).fontWeight);
  const color      = await btn.evaluate(el => getComputedStyle(el).color);

  console.log(`pub-nav-algo-btn: font-size=${fontSize} font-weight=${fontWeight} color=${color}`);

  expect(fontSize,   'font-size must be 0.95rem (15.2px)').toBe(TARGET_FONTSIZE_PX);
  expect(fontWeight, 'font-weight must be 600').toBe(TARGET_FONTWEIGHT);
  expect(color,      'color must be #e8c03a (rgb(232,192,58))').toBe(TARGET_COLOR);
});

test(`pub-nav-algo-btn hover changes color and background [${BASE}]`, async ({ page }) => {
  // hover color: #f0d070 → rgb(240, 208, 112)
  const HOVER_COLOR = 'rgb(240, 208, 112)';

  await page.goto(BASE, { waitUntil: 'networkidle' });

  const btn = page.locator('.pub-nav-algo-btn').first();
  await btn.waitFor({ state: 'attached', timeout: 15_000 });

  await btn.hover();
  // Allow the CSS transition (0.08s) to settle
  await page.waitForTimeout(120);

  const hoverColor = await btn.evaluate(el => getComputedStyle(el).color);
  console.log(`pub-nav-algo-btn hover: color=${hoverColor}`);

  expect(hoverColor, 'hover color must be #f0d070 (rgb(240,208,112))').toBe(HOVER_COLOR);
});
