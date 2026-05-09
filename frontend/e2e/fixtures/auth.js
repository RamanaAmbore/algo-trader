/**
 * Pre-authenticate a Playwright page as the operator admin.
 *
 * Skips the /signin form by hitting /api/auth/login directly, then
 * stuffs the JWT into sessionStorage at the page origin. Every spec
 * that needs admin context calls `await loginAsAdmin(page)` once and
 * navigates straight to its target page.
 *
 * Defaults (rambo / admin1234) match the operator's setup; can be
 * overridden via PLAYWRIGHT_USER / PLAYWRIGHT_PASS env vars when
 * targeting a different environment.
 *
 * Mode-aware: this only works on dev or localhost. On prod the same
 * call would authenticate but writes are guarded by the prod
 * paper/live rails described in CLAUDE.md.
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
  const r = await page.request.post('/api/auth/login', {
    data: { username: user, password: pass },
  });
  if (!r.ok()) {
    throw new Error(`loginAsAdmin failed: ${r.status()} ${await r.text()}`);
  }
  const j = await r.json();
  // Navigate to the origin first so the sessionStorage write lands
  // in the right window. Subsequent goto() calls keep the session.
  await page.goto('/');
  // The (algo) layout's redirect logic gates on $authStore.user (not
  // just the token). Stash both keys so the auth store re-reads a
  // populated session on the next route load — otherwise dev-branch
  // routes redirect to /signin.
  const userRecord = {
    user_id: j.username || user,
    username: j.username || user,
    role: j.role || 'admin',
    display_name: j.display_name || user,
  };
  await page.evaluate(({ tok, usr }) => {
    sessionStorage.setItem('ramboq_token', tok);
    sessionStorage.setItem('ramboq_user', JSON.stringify(usr));
  }, { tok: j.access_token, usr: userRecord });
  return { user_id: user, token: j.access_token };
}

/**
 * Anonymous visitor — no token, hits the public flow / demo path on prod.
 * @param {import('@playwright/test').Page} page
 */
export async function visitAnonymous(page) {
  await page.goto('/');
  await page.evaluate(() => sessionStorage.removeItem('ramboq_token'));
}
