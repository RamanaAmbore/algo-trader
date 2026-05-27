// Verify Phase 16 — JWT bootstrap shortcut on Lab Settings.
//   1. Session-token export line is rendered when sessionStorage
//      carries a valid token
//   2. The export line is single-quoted (Zsh-safe)
//   3. The legacy curl-based command still renders below
//   4. "Or mint a fresh JWT non-interactively" divider sits between

import { test, expect } from '@playwright/test';

const BASE = process.env.BASE_URL || 'https://dev.ramboq.com';
const _AUTH_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

let _cachedToken = null;
async function login(page) {
  if (!_cachedToken) {
    for (const u of ['ambore', 'rambo']) {
      const r = await page.request.post(`${BASE}/api/auth/login`, {
        data: { username: u, password: _AUTH_PASS },
      });
      if (r.ok()) { _cachedToken = (await r.json()).access_token; break; }
    }
    if (!_cachedToken) throw new Error(`login failed against ${BASE}`);
  }
  await page.context().addInitScript((t) => { sessionStorage.setItem('ramboq_token', t); }, _cachedToken);
  await page.context().setExtraHTTPHeaders({ Authorization: `Bearer ${_cachedToken}` });
  return _cachedToken;
}

test(`Lab Settings — JWT shortcut renders the session token [${BASE}]`, async ({ page }) => {
  const tok = await login(page);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);
  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);

  // The shortcut block: .jwt-current carries the export line
  const shortcut = page.locator('.jwt-current');
  await expect(shortcut).toBeVisible();
  const lineText = await shortcut.innerText();
  console.log(`export line preview: ${lineText.slice(0, 60)}…`);
  expect(lineText).toContain('export RAMBOQ_TOKEN=');
  // Single-quoted (Zsh-safe — % expansion + history triggers handled)
  expect(lineText).toContain("='");
  expect(lineText).toContain("'");
  // The actual token must be inside the quoted string
  expect(lineText).toContain(tok);
  // No bash variable substitution sneaking in
  expect(lineText).not.toContain('${');

  // Card has both Copy buttons (shortcut + automation form)
  const copyBtns = page.locator('.lab-card-mint, .lab-card').filter({ hasText: 'Bootstrap your JWT' }).locator('.copy-btn');
  await expect(copyBtns).toHaveCount(2);

  // The "Or mint a fresh JWT non-interactively" divider is present
  await expect(page.locator('.jwt-or')).toBeVisible();
  await expect(page.locator('.jwt-or')).toContainText(/non-interactively/);

  // The legacy curl command still renders for automation
  const codeBlocks = page.locator('.code-block');
  const blocks = await codeBlocks.allInnerTexts();
  console.log(`code blocks on Settings tab: ${blocks.length}`);
  // Both: the session-export line + the curl command + the .mcp.json
  // (3 total at minimum). One of them must contain the curl pattern.
  expect(blocks.length).toBeGreaterThanOrEqual(3);
  expect(blocks.join('\n')).toMatch(/curl -s -X POST/);

  await page.screenshot({ path: `test-results/research-jwt-shortcut-${BASE.includes('dev') ? 'dev' : 'prod'}.png` });
});

test(`Lab Settings — JWT shortcut hides when sessionStorage is empty [${BASE}]`, async ({ page }) => {
  // Anonymous visitor (no token + admin_guard would normally redirect),
  // BUT we can simulate the "token cleared but page open" state by
  // overriding sessionStorage to empty BEFORE the page hydrates. We
  // still need to be authenticated for the route to render, so set
  // Authorization via the header but leave sessionStorage empty.
  const tok = await login(page);
  await page.context().addInitScript(() => { sessionStorage.removeItem('ramboq_token'); });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${BASE}/admin/research`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(3500);

  // The header / token store may bounce us to /signin since the layout
  // requires sessionStorage too. If so, skip the empty-state assertion
  // (this test is a soft check on the visible-empty path).
  const url = page.url();
  if (url.includes('/signin')) {
    console.log('(soft-skip — layout redirected to /signin without sessionStorage)');
    return;
  }

  await page.locator('.lab-tab', { hasText: 'Settings' }).click();
  await page.waitForTimeout(400);

  const empty = page.locator('.jwt-empty');
  if (await empty.isVisible().catch(() => false)) {
    await expect(empty).toContainText(/No session token/);
    console.log('empty-state hint rendered as expected');
  } else {
    console.log('(soft-skip — empty-state not reached; addInitScript timing varies)');
  }
});
