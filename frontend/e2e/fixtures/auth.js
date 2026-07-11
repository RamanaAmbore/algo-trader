/**
 * Authenticate a Playwright page as an operator.
 *
 * FAST PATH (default): globalSetup cached an auth token to e2e/.auth/state.json.
 * Inject it into sessionStorage + Authorization header — no browser, no form, no rate-limit hit.
 *
 * SLOW PATH (fallback): Cache miss or stale token. Fall back to driving the real /signin form
 * (Username + Password + Sign In button). The form goes through /api/auth/login like a normal
 * user, then the SvelteKit auth store populates from the response.
 *
 * Defaults (rambo / admin1234) match the local-dev setup; override
 * via PLAYWRIGHT_USER / PLAYWRIGHT_PASS env vars (e.g. ambore + their
 * real password when testing against prod).
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

  // FAST PATH: Reuse cached token from globalSetup — no form, no rate-limit hit.
  try {
    const { readFileSync } = await import('fs');
    const saved = JSON.parse(readFileSync('e2e/.auth/state.json', 'utf-8'));
    if (saved?.token && saved?.user === user) {
      // Inject token into sessionStorage via addInitScript (runs before page.goto)
      await page.addInitScript((tok) => {
        sessionStorage.setItem('ramboq_token', tok);
      }, saved.token);
      // Attach JWT to APIRequestContext for spec-level page.request calls
      await page.context().setExtraHTTPHeaders({
        Authorization: `Bearer ${saved.token}`,
      });
      return { user_id: user, token: saved.token };
    }
  } catch (_) {
    // No cached state, file unreadable, user mismatch, or user changed defaults.
    // Fall through to slow-path form login below.
  }

  // SLOW PATH: Full form login (fallback when cache missing or stale).

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
    await page.locator('input[name="username"], input#username, input#s-user').first().fill(user);
    await page.locator('input[name="password"], input#password, input#s-pass').first().fill(pass);
    // Use .btn-primary to target the form's own submit button.
    // The nav bar also has buttons with type="submit" and text "Sign In" that
    // come earlier in the DOM and match the generic selectors — .btn-primary
    // is the correct class for the form's Sign In button on this page.
    await page.locator('button.btn-primary, button[type="submit"].btn-primary').first().click();

    // Wait for either redirect away from /signin (success) or for an
    // error banner to render. The signin page navigates to /dashboard
    // on successful login for admin/designated roles.
    try {
      await page.waitForURL(/^(?!.*\/signin).*$/, { timeout: 15000 });
      // The SvelteKit auth store writes ramboq_token to sessionStorage
      // synchronously in the onMount / auth callback after the API response.
      // Poll for the token to appear rather than blocking on page load
      // (which can take 30+ s on prod's heavy dashboard).
      for (let i = 0; i < 10; i++) {
        const hasTok = await page.evaluate(() => !!sessionStorage.getItem('ramboq_token'));
        if (hasTok) break;
        await new Promise((res) => setTimeout(res, 300));
      }
      break;
    } catch (_) {
      // Still on /signin — check for an error banner or rate-limit text.
      // Use a short timeout so this doesn't block for the full test duration.
      // .pub-banner-error is the prod /signin page's error div class.
      const banner = await page.locator('.pub-banner-error, .error, .signin-error, [role="alert"]').first()
        .textContent({ timeout: 2000 }).catch(() => '');
      lastError = banner || `signin did not redirect after ${user}`;
      // On prod, anonymous sessions receive "Demo mode — feature unavailable."
      // when the backend returns 429 (rate-limited), because api.js _friendlyError
      // masks any non-404 error as the demo message for unauthenticated visitors.
      // Treat the demo-mode banner as a rate-limit indicator so we retry.
      if (!/(rate|429|too many|demo mode|unavailable)/i.test(lastError)) {
        // Non-rate, non-demo error (e.g. wrong credentials) — fail fast.
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
