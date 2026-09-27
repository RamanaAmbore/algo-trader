// About/landing page copy — B3 (2026-09) [dev-only]
//
// Rewrites the /about "Platform" paragraph away from internal engineering
// documentation ("UDS service," "shared-memory tick distribution," "three-
// tier persistence caching") toward investor concerns (track record, risk
// discipline, transparency), and aligns the principal's bio + credential
// display on /about and the landing page with the canonical credentials
// list (verbatim, no invention/alteration) supplied for this pass:
//   FRM (GARP, 2022); CFA Level 3 candidate (NOT Level 2); Master's,
//   Computer Science; Six Sigma Green Belt; IBM Certified DB2 DBA (2003);
//   Sun Certified Java Programmer. Roles: Fidelity Investments — Principal
//   System Analyst (day job, 19-year tenure); RamboQuant LLP — Platform
//   Architect & Quantitative Developer (AI-augmented, built with Claude
//   Code). AVOID: "founder"/"Founding Engineer", "designated partner", any
//   wording implying the operator does trading.
//
// **Ships to dev only — do NOT merge to main until the operator reviews
// the rendered page.** This spec is a content/regression guard, not a
// merge gate by itself.
//
// Five quality dimensions (feedback_test_dimensions.md):
//   1. SSOT    — reads the real rendered page + source files, not a
//                hardcoded assumption about what the copy says.
//   2. Perf    — no login (public pages).
//   3. Stale   — the "must not contain" assertions ARE the stale-code
//                guard (old "founder"/"XLRI"/"CFA II"/"CFA Level III"
//                wording can't silently creep back in).
//   4. Reuse   — same source-grep pattern as acu5195_removed.spec.js.
//   5. UX      — checks the rendered page (not just source) so a build-
//                time templating bug can't silently ship stale text.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

test.describe('about page copy — source-level guards', () => {
  const aboutSrc = () => readFileSync(
    new URL('../src/routes/(public)/about/+page.svelte', import.meta.url).pathname, 'utf8'
  );
  const landingSrc = () => readFileSync(
    new URL('../src/routes/(public)/+page.svelte', import.meta.url).pathname, 'utf8'
  );

  test('about page: no internal-architecture jargon in investor copy', () => {
    const src = aboutSrc();
    for (const term of ['UDS service', 'shared-memory tick distribution', 'three-tier persistence caching']) {
      expect(src, `about page must not mention "${term}"`).not.toContain(term);
    }
  });

  test('about + landing: no "founder" wording (AVOID list — conflict of interest with Fidelity role)', () => {
    for (const src of [aboutSrc(), landingSrc()]) {
      expect(src, 'must not use "founder" wording').not.toMatch(/founder/i);
    }
  });

  test('about + landing: no personal "active investing"/"hands-on trading" framing', () => {
    // The FIRM's track-record figures (22% XIRR, 25+ years) stay — only
    // wording that attributes personal trading/active-investing activity
    // to the individual is disallowed.
    const src = aboutSrc();
    expect(src, 'must not claim personal "active investing" success').not.toMatch(
      /proven success in active investing/i
    );
  });

  test('about + landing: canonical credentials verbatim, no stale/incorrect ones', () => {
    for (const src of [aboutSrc(), landingSrc()]) {
      // Stale/incorrect credentials that must be gone.
      expect(src, 'must not say "CFA Level III" (was incorrect — candidate at Level 3, not a completed Level III)')
        .not.toMatch(/CFA Level III/);
      expect(src, 'must not say "CFA II" (was wrong level entirely)').not.toMatch(/CFA II\b/);
      expect(src, 'must not mention XLRI/PGCBM (not on the canonical credentials list)')
        .not.toMatch(/XLRI|PGCBM/);
    }
    const aboutOnly = aboutSrc();
    // Canonical credentials that MUST appear (verbatim per the plan).
    expect(aboutOnly).toMatch(/FRM \(GARP, 2022\)/);
    expect(aboutOnly).toMatch(/CFA Level 3 candidate/);
    expect(aboutOnly).toMatch(/Six Sigma Green Belt/);
    expect(aboutOnly).toMatch(/IBM Certified DB2 DBA/);
    expect(aboutOnly).toMatch(/Sun Certified Java Programmer/);
    expect(aboutOnly).toMatch(/NTT Innovation Award/);
    expect(aboutOnly).toMatch(/Principal System Analyst at Fidelity Investments/);
    expect(aboutOnly).toMatch(/19 years/);
    expect(aboutOnly).toMatch(/30\+ years/);
  });

  test('about page: primary CTA points at /contact (no new lead-capture form)', () => {
    const src = aboutSrc();
    expect(src).toMatch(/href="\/contact" class="cta-btn cta-btn-primary"/);
  });
});

test.describe('about page copy — rendered page', () => {
  test('renders the rewritten Platform paragraph and credentials, no console errors', async ({ page }) => {
    const consoleErrors = [];
    page.on('console', (msg) => { if (msg.type() === 'error') consoleErrors.push(msg.text()); });
    page.on('pageerror', (err) => consoleErrors.push(`UNCAUGHT: ${err.message}`));

    await page.goto('/about', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);

    await expect(page.getByText('algorithmically driven investment program', { exact: false })).toBeVisible();
    await expect(page.getByText('Platform Architect & Quantitative Developer, RamboQuant LLP', { exact: false })).toBeVisible();
    await expect(page.getByText('FRM (GARP, 2022)', { exact: false })).toBeVisible();
    await expect(page.getByText('NTT Innovation Award', { exact: false })).toBeVisible();

    const contactCta = page.locator('a.cta-btn-primary[href="/contact"]');
    await expect(contactCta).toBeVisible();
    await expect(contactCta).toHaveText('Contact us');

    const meaningfulErrors = consoleErrors.filter((e) => !/favicon|ResizeObserver/i.test(e));
    expect(meaningfulErrors, `console errors: ${meaningfulErrors.join(' | ')}`).toEqual([]);
  });

  test('landing page trust-strip no longer reads "Founder credentials"', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);
    await expect(page.getByText('Founder credentials')).toHaveCount(0);
    await expect(page.getByText('Principal credentials')).toBeVisible();
  });
});
