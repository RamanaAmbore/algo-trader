/**
 * Footer credits — both layout footers show both author names.
 *
 * Validates:
 * 1. Public layout footer (/, /faq, etc.) — Ramana R. Ambore AND Gopi Podicheti
 * 2. Algo layout footer (/pulse, /dashboard, etc.) — Ramana R. Ambore AND Gopi Podicheti
 * 3. Both names appear inline in the same "Built by" phrase
 * 4. Ramana name is a link; Gopi is plain text following "&"
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin, visitAnonymous } from './fixtures/auth.js';

const TIMEOUT = 20_000;

test.describe('Footer credits — Ramana R. Ambore & Gopi Podicheti', () => {
  test('public layout (/) shows both names in footer', async ({ page }) => {
    await visitAnonymous(page);
    await page.goto('/', { waitUntil: 'domcontentloaded' });

    // The public layout has two footer paragraphs (desktop + mobile).
    // On mobile viewports the desktop paragraph is CSS-hidden (hidden md:block)
    // and on desktop the mobile paragraph is hidden (md:hidden).
    // Use toBeAttached so the check is viewport-agnostic; the textContent
    // assertions below confirm both names appear in the DOM at every breakpoint.
    const footerLinks = page.locator('footer a.pub-footer-link');
    await expect(footerLinks.first()).toBeAttached({ timeout: TIMEOUT });

    // Ramana link points to the correct href (check attached, not visibility)
    const ramanaLink = footerLinks.filter({ hasText: 'Ramana R. Ambore' });
    await expect(ramanaLink.first()).toBeAttached();
    await expect(ramanaLink.first()).toHaveAttribute('href', 'https://ramanaambore.me');

    // Gopi appears as plain text adjacent to the link
    // Look for the enclosing element that contains the full phrase
    const builtByParagraphs = page.locator('footer p').filter({ hasText: /Gopi Podicheti/ });
    const count = await builtByParagraphs.count();
    expect(count).toBeGreaterThanOrEqual(1);

    // Both the desktop paragraph (hidden on mobile) and the mobile paragraph
    // must contain Gopi Podicheti so neither breakpoint is missing the credit.
    const allParagraphs = await page.locator('footer p').all();
    let builtByCount = 0;
    for (const p of allParagraphs) {
      const text = await p.textContent();
      if (text && text.includes('Built by')) {
        builtByCount++;
        expect(text).toContain('Ramana R. Ambore');
        expect(text).toContain('Gopi Podicheti');
      }
    }
    expect(builtByCount).toBeGreaterThanOrEqual(1);
  });

  test('algo layout (/pulse) shows both names in footer', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    // Wait for the algo layout to settle
    const footer = page.locator('footer').last();
    await expect(footer).toBeVisible({ timeout: TIMEOUT });

    const footerText = await footer.textContent();
    expect(footerText).toContain('Ramana R. Ambore');
    expect(footerText).toContain('Gopi Podicheti');

    // Ramana link should point to ramanaambore.me
    const ramanaLink = footer.locator('a.algo-footer-link', { hasText: 'Ramana R. Ambore' });
    await expect(ramanaLink).toBeVisible();
    await expect(ramanaLink).toHaveAttribute('href', 'https://ramanaambore.me');
  });

  test('algo layout (/dashboard) shows both names in footer', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded' });

    const footer = page.locator('footer').last();
    await expect(footer).toBeVisible({ timeout: TIMEOUT });

    const footerText = await footer.textContent();
    expect(footerText).toContain('Ramana R. Ambore');
    expect(footerText).toContain('Gopi Podicheti');
  });

  test('Gopi name appears after & separator, not as a link', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });

    const footer = page.locator('footer').last();
    await expect(footer).toBeVisible({ timeout: TIMEOUT });

    // Gopi should NOT be an anchor tag
    const gopiLink = footer.locator('a', { hasText: 'Gopi Podicheti' });
    await expect(gopiLink).toHaveCount(0);

    // The text node containing Gopi should be in the footer
    const footerText = await footer.textContent();
    expect(footerText).toMatch(/Ramana R\. Ambore\s*&\s*Gopi Podicheti/);
  });
});
