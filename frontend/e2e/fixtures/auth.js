/**
 * Authenticate a Playwright page as an operator by driving the real
 * /signin form (Username + Password + Sign In button). The form goes
 * through /api/auth/login like a normal user, then the SvelteKit auth
 * store populates from the response — no sessionStorage stuffing.
 *
 * Defaults (rambo / admin1234) match the local-dev setup; override
 * via PLAYWRIGHT_USER / PLAYWRIGHT_PASS env vars (e.g. ambore + their
 * real password when testing against prod).
 *
 * Why this over the previous API-only shortcut? The operator wants
 * the test to behave as close to a real session as possible — the
 * signin form is the first surface a visitor sees, and bypassing it
 * would miss regressions in the form / auth-store / redirect chain.
 */

const DEFAULT_USER = process.env.PLAYWRIGHT_USER || 'rambo';
const DEFAULT_PASS = process.env.PLAYWRIGHT_PASS || 'admin1234';

/**
 * @param {import('@playwright/test').Page} page
 * @param {{ user?: string, pass?: string }} [opts]
 * @returns {Promise<{ user_id: string, token: string }>}
 */
export async function loginAsAdmin(page, opts = {}) {
  const user = opts.user || DEFAULT_USER;
  const pass = opts.pass || DEFAULT_PASS;

  // Start at /signin like a real visitor. Submitting the form
  // triggers /api/auth/login → the auth store populates → the page
  // redirects to /dashboard (admin/designated) or /signin (failure).
  // We retry a couple of times to cover the 5/min rate-limit window.
  /** @type {string} */
  let lastError = '';
  for (const delay of [0, 3000, 8000]) {
    if (delay) await new Promise((res) => setTimeout(res, delay));

    await page.goto('/signin', { waitUntil: 'domcontentloaded' });
    // The form fields are <input type="text"> for username and
    // <input type="password"> for password. Selector text matches
    // the labels on the form.
    await page.locator('input[name="username"], input#username').first().fill(user);
    await page.locator('input[name="password"], input#password').first().fill(pass);
    await page.locator('button:has-text("Sign In"), button[type="submit"]').first().click();

    // Wait for either redirect away from /signin (success) or for an
    // error banner to render. The signin page navigates to /dashboard
    // on successful login for admin/designated roles.
    try {
      await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 8000 });
      break;
    } catch (_) {
      // Still on /signin — check for an error banner or rate-limit text.
      const banner = await page.locator('.error, .signin-error, [role="alert"]').first().textContent().catch(() => '');
      lastError = banner || `signin did not redirect after ${user}`;
      if (!/(rate|429|too many)/i.test(lastError)) {
        // Non-rate error — fail fast.
        break;
      }
    }
  }

  // Pull the token out of sessionStorage so callers that need it for
  // API calls still get it. The store has it after the form submit
  // either way.
  const tokenInfo = await page.evaluate(() => {
    const tok = sessionStorage.getItem('ramboq_token');
    const usr = sessionStorage.getItem('ramboq_user');
    return { tok, usr: usr ? JSON.parse(usr) : null };
  });

  if (!tokenInfo.tok) {
    throw new Error(`loginAsAdmin failed: ${lastError || 'no token in sessionStorage after signin'}`);
  }

  // Attach the JWT to APIRequestContext so spec-level page.request
  // calls authenticate without having to thread headers manually.
  await page.context().setExtraHTTPHeaders({
    Authorization: `Bearer ${tokenInfo.tok}`,
  });

  return { user_id: user, token: tokenInfo.tok };
}

/**
 * Anonymous visitor — no token, hits the public flow / demo path on prod.
 * @param {import('@playwright/test').Page} page
 */
export async function visitAnonymous(page) {
  await page.goto('/');
  await page.evaluate(() => sessionStorage.removeItem('ramboq_token'));
}
