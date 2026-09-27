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
//                guard (old "founder"/"CFA II"/"CFA Level III" wording
//                can't silently creep back in; XLRI/PGCBM is a real,
//                operator-confirmed credential and is now a "must
//                contain" assertion instead).
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

  test('about page: Haritha Chikile is Founder, Ramana is never called "founder" (AVOID list — conflict of interest with Fidelity role)', () => {
    const src = aboutSrc();
    expect(src, 'about page must credit Haritha Chikile as Founder').toMatch(/Haritha Chikile/);
    expect(src, 'about page must use "Founder" for Haritha Chikile').toMatch(/"Founder"|Founder, RamboQuant|"founder"/i);
    // Ramana must never be described as "founder" — scan every sentence/JSON
    // value that mentions his name and confirm "founder" isn't in it.
    const ramanaMentions = src.match(/[^.\n{]*Ramana[^.\n}]*/g) || [];
    for (const mention of ramanaMentions) {
      expect(mention, `"founder" must not appear near a Ramana mention: "${mention.trim()}"`)
        .not.toMatch(/founder/i);
    }
  });

  test('landing page: no "founder" wording (unchanged — founder mention lives on /about only)', () => {
    expect(landingSrc(), 'landing page must not use "founder" wording').not.toMatch(/founder/i);
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
      // XLRI/PGCBM (Post Graduate Certificate in Business Management) is a
      // real, operator-confirmed credential — reinstated after an earlier
      // pass removed it as "not on the canonical list" before the operator
      // clarified it should be kept.
      expect(src, 'must mention XLRI (operator-confirmed credential, restored)')
        .toMatch(/XLRI/);
    }
    const aboutOnly = aboutSrc();
    // Canonical credentials that MUST appear (verbatim per the plan).
    expect(aboutOnly).toMatch(/FRM \(GARP, 2022\)/);
    expect(aboutOnly).toMatch(/CFA Level 3 candidate/);
    expect(aboutOnly).toMatch(/PGCBM/);
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

  test('about page: Haritha Chikile (Founder) section precedes Ramana Ambore (Platform Architect) section, each under exactly one heading', () => {
    // Operator instructions (2026-09): (1) keep Haritha Chikile, Founder, at
    // the top of the page, Ramana Ambore below it; (2) Ramana must appear
    // under exactly ONE heading, not split across a separate top-level
    // profile block plus his own prose section. Fix folded the GitHub-chip
    // block into his single "Platform Architect & Quantitative Developer"
    // prose section and removed the standalone .principal-block entirely.
    const src = aboutSrc();

    // No leftover standalone profile-block markup — proves consolidation,
    // not just reordering of two duplicate blocks.
    expect(src, 'standalone .principal-block markup must be removed').not.toMatch(/class="principal-block"/);

    const founderLabelIdx = src.indexOf('>Founder<');
    const architectLabelIdx = src.indexOf('>Platform Architect &amp; Quantitative Developer<');
    expect(founderLabelIdx, 'Founder prose section label must exist').toBeGreaterThan(-1);
    expect(architectLabelIdx, 'Architect prose section label must exist').toBeGreaterThan(-1);
    expect(founderLabelIdx, 'Founder prose section must precede the Architect prose section')
      .toBeLessThan(architectLabelIdx);

    // Each person's full name appears exactly once in the VISUAL markup —
    // one heading, one mention, no duplicate top-level block. Scoped to the
    // body (excludes <svelte:head> JSON-LD, which legitimately names both
    // as founder/employee — machine metadata, not visual duplication).
    const body = src.slice(src.indexOf('</svelte:head>'));
    const haritaMentions = (body.match(/Haritha Chikile/g) || []).length;
    const ramanaMentions = (body.match(/Ramana R Ambore/g) || []).length;
    expect(haritaMentions, 'Haritha Chikile must appear exactly once (single section)').toBe(1);
    expect(ramanaMentions, 'Ramana R Ambore must appear exactly once (single section)').toBe(1);

    // GitHub chip now lives inside the Architect prose section, not a
    // separate top block.
    const githubChipIdx = src.indexOf('https://github.com/RamanaAmbore');
    expect(githubChipIdx, 'GitHub chip must exist').toBeGreaterThan(-1);
    expect(githubChipIdx, 'GitHub chip must be inside the Architect prose section')
      .toBeGreaterThan(architectLabelIdx);
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
    await expect(page.getByText('Platform Architect & Quantitative Developer', { exact: false }).first()).toBeVisible();
    await expect(page.getByText('FRM (GARP, 2022)', { exact: false })).toBeVisible();
    await expect(page.getByText('NTT Innovation Award', { exact: false })).toBeVisible();

    const contactCta = page.locator('a.cta-btn-primary[href="/contact"]');
    await expect(contactCta).toBeVisible();
    await expect(contactCta).toHaveText('Contact us');

    const meaningfulErrors = consoleErrors.filter((e) => !/favicon|ResizeObserver/i.test(e));
    expect(meaningfulErrors, `console errors: ${meaningfulErrors.join(' | ')}`).toEqual([]);
  });

  test('rendered page: Haritha Chikile (Founder) section is first, Ramana Ambore (Architect) section second, each appears once', async ({ page }) => {
    await page.goto('/about', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);

    // No leftover standalone profile-block band.
    await expect(page.locator('.principal-block')).toHaveCount(0);

    // Each name renders in exactly one place — proves consolidation, not
    // just a reorder of two duplicate blocks.
    await expect(page.getByText('Haritha Chikile', { exact: false })).toHaveCount(1);
    await expect(page.getByText('Ramana R Ambore', { exact: false })).toHaveCount(1);

    // Visual order: Founder section label sits above the Architect one.
    const founderLabel = page.locator('.prose-section-label', { hasText: 'Founder' });
    const architectLabel = page.locator('.prose-section-label', { hasText: 'Platform Architect' });
    const founderBox = await founderLabel.boundingBox();
    const architectBox = await architectLabel.boundingBox();
    expect(founderBox.y, 'Founder section must render above the Architect section').toBeLessThan(architectBox.y);

    // GitHub chip lives inside the Architect section, not a separate block.
    const chip = page.locator('a.principal-chip[href="https://github.com/RamanaAmbore"]');
    await expect(chip).toBeVisible();
    const chipBox = await chip.boundingBox();
    expect(chipBox.y, 'GitHub chip must render below the Architect section label').toBeGreaterThan(architectBox.y);
  });

  test('landing page trust-strip no longer reads "Founder credentials"', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(500);
    await expect(page.getByText('Founder credentials')).toHaveCount(0);
    await expect(page.getByText('Principal credentials')).toBeVisible();
  });
});
