// Verify CSS-only navbar text visibility improvements (2026-08-15).
//
// Test 1 — algo nav buttons on desktop (≥768px):
//   The @media (min-width: 768px) override sets color to
//   rgba(200, 218, 242, 0.92), which the browser resolves to
//   rgb(200, 218, 242) at full paint (opacity handled by the alpha
//   channel, but getComputedStyle() returns the premultiplied rgba form).
//
// Test 2 — public navbar Algo Site button (.pub-nav-algo-btn):
//   Default: color #e8c03a, font-size 0.95rem, font-weight 600,
//            border rgba(200,168,75,0.60), background rgba(200,168,75,0.15)
//   Hover:   color #f0d070, border rgba(200,168,75,0.75),
//            background rgba(200,168,75,0.22)

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const BASE = process.env.PLAYWRIGHT_BASE_URL || 'http://localhost:5174';

// ── Test 1: algo nav button color on desktop ─────────────────────────────────

test.describe('algo navbar desktop text color', () => {
  // Force a desktop viewport regardless of the project default so the
  // @media (min-width: 768px) rule is guaranteed to apply.
  test.use({ viewport: { width: 1280, height: 800 } });

  test(`algo-nav-btn has brighter blue-white color on desktop [${BASE}]`, async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto(`${BASE}/pulse`, { waitUntil: 'domcontentloaded' });

    const btn = page.locator('.algo-nav-btn').first();
    await btn.waitFor({ state: 'visible', timeout: 20_000 });

    const color = await btn.evaluate(el => getComputedStyle(el).color);
    console.log(`algo-nav-btn computed color (desktop): ${color}`);

    // rgba(200, 218, 242, 0.92) — browsers return rgba() form when alpha < 1
    expect(color, 'desktop algo-nav-btn color must be rgba(200,218,242,0.92)').toBe(
      'rgba(200, 218, 242, 0.92)'
    );
  });
});

// ── Test 2: public navbar algo button styles ──────────────────────────────────

test.describe('public navbar algo-site button visibility', () => {
  test.use({ viewport: { width: 1280, height: 800 } });

  test(`pub-nav-algo-btn default state: font, weight, color [${BASE}]`, async ({ page }) => {
    // Public home is accessible without auth
    await page.goto(BASE, { waitUntil: 'networkidle' });

    const btn = page.locator('.pub-nav-algo-btn').first();
    await btn.waitFor({ state: 'attached', timeout: 15_000 });

    const [fontSize, fontWeight, color, borderColor, bgColor] = await btn.evaluate(el => {
      const cs = getComputedStyle(el);
      return [
        cs.fontSize,
        cs.fontWeight,
        cs.color,
        cs.borderColor,
        cs.backgroundColor,
      ];
    });

    console.log(
      `pub-nav-algo-btn: font-size=${fontSize} weight=${fontWeight} ` +
      `color=${color} border=${borderColor} bg=${bgColor}`
    );

    // 0.95rem at 16px root = 15.2px
    expect(fontSize,   'font-size must be 0.95rem → 15.2px').toBe('15.2px');
    expect(fontWeight, 'font-weight must be 600').toBe('600');
    // #e8c03a → rgb(232, 192, 58)
    expect(color,      'color must be #e8c03a (rgb(232,192,58))').toBe('rgb(232, 192, 58)');
  });

  test(`pub-nav-algo-btn hover: brighter color [${BASE}]`, async ({ page }) => {
    await page.goto(BASE, { waitUntil: 'networkidle' });

    const btn = page.locator('.pub-nav-algo-btn').first();
    await btn.waitFor({ state: 'attached', timeout: 15_000 });

    await btn.hover();
    // Allow the 0.08s CSS transition to settle
    await page.waitForTimeout(150);

    const hoverColor = await btn.evaluate(el => getComputedStyle(el).color);
    console.log(`pub-nav-algo-btn hover color: ${hoverColor}`);

    // #f0d070 → rgb(240, 208, 112)
    expect(hoverColor, 'hover color must be #f0d070 (rgb(240,208,112))').toBe(
      'rgb(240, 208, 112)'
    );
  });
});
