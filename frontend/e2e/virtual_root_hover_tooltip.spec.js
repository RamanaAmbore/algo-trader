/**
 * virtual_root_hover_tooltip.spec.js
 *
 * Validates that virtual MCX/CDS root display labels (GOLDM, GOLDM.NEXT,
 * CRUDEOIL, etc.) carry a native `title` attribute showing the actual
 * resolved contract (e.g. "GOLDM26JULFUT") on hover.
 *
 * Five quality dimensions:
 *  1. SSOT     — resolveVirtual from rootOf.js is the sole resolver;
 *                grep confirms symRenderer calls it and not a local copy.
 *  2. Perf     — assertions are static DOM checks (no polling loops).
 *  3. Stale    — grep confirms no inline virtual→contract resolution
 *                outside rootOf.js in symRenderer path.
 *  4. Reusable — LegLabel.svelte uses the same `title={sym}` pattern;
 *                grep confirms it.
 *  5. UX       — non-virtual equity row (RELIANCE) has no redundant
 *                `title` attribute on the sym-main span.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test e2e/virtual_root_hover_tooltip.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';

const USER = process.env.PLAYWRIGHT_USER || 'rambo';
const PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

// ---------------------------------------------------------------------------
// Static source-code checks (no server needed)
// ---------------------------------------------------------------------------

test('SSOT: symRenderer imports resolveVirtual from rootOf.js', async () => {
  const { readFileSync } = await import('fs');
  const src = readFileSync('src/lib/MarketPulse.svelte', 'utf8');
  // Import line must include resolveVirtual from rootOf.js
  expect(src).toMatch(/import\s*\{[^}]*resolveVirtual[^}]*\}\s*from\s*['"].*rootOf\.js['"]/);
});

test('SSOT: symRenderer uses resolveVirtual for title computation', async () => {
  const { readFileSync } = await import('fs');
  const src = readFileSync('src/lib/MarketPulse.svelte', 'utf8');
  // The symRenderer function body must call resolveVirtual
  const fnStart = src.indexOf('function symRenderer(');
  expect(fnStart).toBeGreaterThan(0);
  const fnEnd = src.indexOf('\n  }', fnStart + 1);
  const fn = src.slice(fnStart, fnEnd + 4);
  expect(fn).toContain('resolveVirtual(');
  // Title attribute must be conditionally emitted
  expect(fn).toContain('_symTitle');
  expect(fn).toContain('title=');
});

test('SSOT: LegLabel.svelte has title={sym} on virtual label span', async () => {
  const { readFileSync } = await import('fs');
  const src = readFileSync('src/lib/LegLabel.svelte', 'utf8');
  // The virtual branch span must carry title={sym}
  expect(src).toMatch(/\{#if _virtualLabel\}[\s\S]*?<span[^>]*title=\{sym\}[^>]*>/);
});

test('SSOT: PerformancePage symRenderers set title when display differs from raw', async () => {
  const { readFileSync } = await import('fs');
  const src = readFileSync('src/lib/PerformancePage.svelte', 'utf8');
  // Both renderer functions must set title attribute
  expect(src).toContain("setAttribute('title', sym)");
});

test('SSOT: derivatives page sym-main has title on virtual rows', async () => {
  const { readFileSync } = await import('fs');
  const src = readFileSync('src/routes/(algo)/admin/derivatives/+page.svelte', 'utf8');
  // The refactored block must contain title={c.symbol} for virtual branches
  expect(src).toContain('title={c.symbol}');
});

test('Stale: no inline virtual resolution in symRenderer outside resolveVirtual call', async () => {
  const { readFileSync } = await import('fs');
  const src = readFileSync('src/lib/MarketPulse.svelte', 'utf8');
  const fnStart = src.indexOf('function symRenderer(');
  const fnEnd = src.indexOf('\n  }', fnStart + 1);
  const fn = src.slice(fnStart, fnEnd + 4);
  // Should not contain manual _NEXT string substitution or hardcoded root map access
  expect(fn).not.toMatch(/_mcx\s*\[|_cds\s*\[/);
  // Should not duplicate the rootOf logic inline
  expect(fn).not.toMatch(/slots\[0\]|slots\[1\]/);
});

// ---------------------------------------------------------------------------
// Runtime DOM checks
// ---------------------------------------------------------------------------

async function signIn(page) {
  await page.goto('/signin', { waitUntil: 'domcontentloaded' });
  await page.locator('input[name="username"], input#username, input#s-user').first().fill(USER);
  await page.locator('input[name="password"], input#password, input#s-pass').first().fill(PASS);
  await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();
  await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15_000 });
  for (let i = 0; i < 10; i++) {
    const has = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
    if (has) break;
    await new Promise((r) => setTimeout(r, 300));
  }
}

test.describe('Pulse virtual-root symbol cells', () => {
  test.setTimeout(60_000);

  test('MCX front-month virtual symbol cell has title = resolved contract', async ({ page }) => {
    await signIn(page);
    await page.goto('/pulse', { waitUntil: 'networkidle' });

    // Wait for the grid to paint at least one row
    await page.waitForSelector('.ag-row', { timeout: 20_000 });

    // Find any sym-main span whose text is a bare alpha MCX virtual root
    // (e.g. "GOLDM", "CRUDEOIL", "SILVER") — these are the virtual labels.
    // They must carry a title attribute pointing to the actual contract.
    const virtualCells = await page.locator('.sym-main').filter({
      hasText: /^(GOLDM|CRUDEOIL|SILVER|NATURALGAS|COPPER|ZINC|LEAD|ALUMINIUM|NICKEL|USDINR|EURINR|GBPINR|JPYINR)$/
    }).all();

    // Only assert if the instruments cache has resolved at least one virtual row.
    // If zero such rows exist (closed hours, no positions/movers), skip gracefully.
    if (virtualCells.length === 0) {
      console.log('No virtual MCX root rows found on /pulse — skipping runtime DOM check.');
      return;
    }

    for (const cell of virtualCells.slice(0, 3)) {
      const titleAttr = await cell.getAttribute('title');
      // Title must be set and must look like a raw Kite futures contract
      // (contains digits, ends with FUT).
      expect(titleAttr).toBeTruthy();
      expect(titleAttr).toMatch(/\d{2}[A-Z]{3}FUT$/i);
    }
  });

  test('MCX back-month (.NEXT) virtual symbol cell has title = back-month contract', async ({ page }) => {
    await signIn(page);
    await page.goto('/pulse', { waitUntil: 'networkidle' });
    await page.waitForSelector('.ag-row', { timeout: 20_000 });

    // Back-month cells display as "GOLDM.NEXT", "CRUDEOIL.NEXT", etc.
    const nextCells = await page.locator('.sym-main').filter({
      hasText: /\.NEXT$/
    }).all();

    if (nextCells.length === 0) {
      console.log('No .NEXT virtual rows found on /pulse — skipping runtime check.');
      return;
    }

    for (const cell of nextCells.slice(0, 3)) {
      const titleAttr = await cell.getAttribute('title');
      expect(titleAttr).toBeTruthy();
      expect(titleAttr).toMatch(/\d{2}[A-Z]{3}FUT$/i);
    }
  });

  test('UX: non-virtual equity row (RELIANCE or NIFTY) has no title on sym-main', async ({ page }) => {
    await signIn(page);
    await page.goto('/pulse', { waitUntil: 'networkidle' });
    await page.waitForSelector('.ag-row', { timeout: 20_000 });

    // Find equity / index cells — these should NOT have a title attribute
    // since display label equals the tradingsymbol.
    const equityCells = await page.locator('.sym-main').filter({
      hasText: /^(RELIANCE|INFY|TCS|HDFCBANK|NIFTY 50)$/
    }).all();

    if (equityCells.length === 0) {
      console.log('No equity rows found on /pulse — skipping UX check.');
      return;
    }

    for (const cell of equityCells.slice(0, 3)) {
      const titleAttr = await cell.getAttribute('title');
      // Equity sym-main must NOT have a title (no redundant tooltip)
      expect(titleAttr).toBeNull();
    }
  });
});
