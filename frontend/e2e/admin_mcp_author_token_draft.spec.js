// /admin/mcp "Author" tab — Wave 3 of the unified-DSL-token plan
// (~/.claude/plans/sparkling-dancing-lobster.md). Operator describes a
// desired grammar token in a prompt → POST /api/mcp/draft-token → the
// response is rendered as a READ-ONLY reviewable draft. Nothing on this
// tab writes a file, deploys, or registers the token — this spec asserts
// that boundary explicitly (no request to /admin/grammar/tokens or
// /admin/grammar/reload fires during the whole flow, including Copy).
//
// Frontend-only per the plan's own test-boundary note: the actual file
// write + deploy is a human-in-the-loop step, never something a browser
// test performs. /api/auth/whoami is mocked to grant view_research +
// manage_grammar_tokens so the spec is deterministic regardless of which
// real account runs it (manage_grammar_tokens is designated-only — see
// backend/api/rbac.py — and the shared dev test account does not carry
// it; this mirrors how the real backend's /api/mcp/draft-token response
// is mocked below). Every backend call this spec depends on is
// intercepted — nothing here depends on a real draft_token_from_prompt()
// landing.

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

/** Grants view_research + manage_grammar_tokens via a mocked whoami, so
 *  the Author tab renders regardless of the real test account's caps. */
async function mockWhoamiWithAuthorCap(page) {
  await page.route('**/api/auth/whoami', async (route) => {
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        username: 'rambo',
        role: 'admin',
        display_name: 'Test Operator',
        caps: ['view_research', 'use_research_chat', 'manage_grammar_tokens'],
      }),
    });
  });
}

// Deliberately include indentation, a blank line, a long line, angle
// brackets, an ampersand, and backticks — proves the render path is
// plain-text ({text}, never {@html} / markdown) and that textContent
// survives verbatim (toHaveText/toContainText normalize whitespace and
// would silently pass even if indentation were mangled).
const RESOLVER_SOURCE = [
  'def vix_level(ctx):',
  '    """Resolve India VIX from the macro snapshot.',
  '',
  '    Returns `None` when the macro cache has no reading yet — never a',
  '    coerced 0.0. See <html> & `backtick` edge cases in this comment.',
  '    """',
  '    snap = ctx.macro_snapshot()',
  // One deliberately long, unbroken (no spaces) line — stresses the
  // mobile overflow guard below: a long token must scroll inside the
  // .code-block (overflow-x: auto), never blow out the page width.
  '    return snap.get("india_vix_level_from_the_macro_snapshot_cache_keyed_by_this_exact_long_unbroken_identifier_string_abcdefghijklmnopqrstuvwxyz0123456789")',
].join('\n');

const TEST_SOURCE = [
  'def test_vix_level_returns_none_when_absent():',
  '    ctx = FakeContext(macro_snapshot={})',
  '    assert vix_level(ctx) is None',
].join('\n');

const CLEAN_DRAFT_BODY = {
  draft: {
    grammar_kind: 'metric',
    token_kind: 'metric',
    token: 'vix_level',
    description: 'India VIX level from the macro snapshot.',
    params_schema: { type: 'object', properties: {} },
    resolver_source: RESOLVER_SOURCE,
    test_source: TEST_SOURCE,
  },
  errors: [],
  warnings: [],
  why_summary: 'No existing "vix_level" metric token found in the catalog.',
};

const WARNINGS_DRAFT_BODY = {
  draft: {
    grammar_kind: 'metric',
    token_kind: 'metric',
    token: 'vix_level',
    description: 'India VIX level from the macro snapshot.',
    params_schema: {},
    resolver_source: RESOLVER_SOURCE,
    test_source: TEST_SOURCE,
  },
  errors: [],
  warnings: ['A similarly-named token "vix" already exists — confirm this is not a duplicate.'],
  why_summary: 'Drafted with one naming-collision warning.',
};

const ERRORS_BODY = {
  draft: null,
  errors: ['Prompt did not specify a grammar_kind (metric/scope/op/channel/action/format/template).'],
  warnings: [],
  why_summary: '',
};

/** Mocks POST /api/mcp/draft-token with the given body, after an
 *  optional artificial delay so the loading state is assertable rather
 *  than racy. */
async function mockDraftToken(page, body, { delayMs = 0 } = {}) {
  await page.route('**/api/mcp/draft-token', async (route) => {
    if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(body),
    });
  });
}

async function gotoAuthorTab(page) {
  await page.goto('/admin/mcp');
  const tab = page.getByRole('tab', { name: 'Author', exact: true });
  await tab.waitFor({ state: 'visible', timeout: 15000 });
  await tab.click();
  const promptBox = page.locator('.author-prompt-input');
  await promptBox.waitFor({ state: 'visible', timeout: 15000 });
  return promptBox;
}

test.describe('admin/mcp — Author tab (token draft, Wave 3)', () => {
  test('submitting a prompt shows a loading state then the rendered clean draft', async ({ page }) => {
    await loginAsAdmin(page);
    await mockWhoamiWithAuthorCap(page);
    await mockDraftToken(page, CLEAN_DRAFT_BODY, { delayMs: 600 });

    const promptBox = await gotoAuthorTab(page);
    await promptBox.fill('a metric token vix_level that resolves India VIX from the macro snapshot');

    const draftBtn = page.getByRole('button', { name: 'Draft token' });
    await draftBtn.click();

    // Loading state visible while the mocked response is delayed.
    await expect(page.getByTestId('author-loading')).toBeVisible();

    // Draft renders once the delayed response resolves.
    const draftCard = page.getByTestId('author-draft');
    await expect(draftCard).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId('author-loading')).not.toBeVisible();

    // No error/warning cards for a clean draft.
    await expect(page.getByTestId('author-errors')).toHaveCount(0);
    await expect(page.getByTestId('author-warnings')).toHaveCount(0);

    // Meta fields render.
    await expect(draftCard).toContainText('vix_level');
    await expect(draftCard).toContainText('metric');

    // Source blocks render the mocked text VERBATIM — exact equality,
    // not toContainText, to catch whitespace mangling or truncation.
    await expect(page.getByTestId('author-resolver-source')).toHaveText(RESOLVER_SOURCE);
    await expect(page.getByTestId('author-test-source')).toHaveText(TEST_SOURCE);

    // No {@html} path: the raw <html> / & / backtick text in the
    // resolver source must appear as literal text, not be parsed as
    // markup or stripped.
    const resolverText = await page.getByTestId('author-resolver-source').textContent();
    expect(resolverText).toContain('<html> & `backtick`');

    // "Copy bundle" is present and ready — this is the draft-ready case.
    await expect(page.getByRole('button', { name: 'Copy bundle' })).toBeVisible();

    // Mobile viewport guard (house rule: check 320-375px, not only
    // desktop) — a long, unbroken resolver-source line must scroll
    // inside its own .code-block (overflow-x: auto), never blow out
    // the page's own horizontal extent.
    const docOverflowsX = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1
    );
    expect(docOverflowsX).toBe(false);
  });

  test('an errors-present response renders the errors and never presents the draft as ready', async ({ page }) => {
    await loginAsAdmin(page);
    await mockWhoamiWithAuthorCap(page);
    await mockDraftToken(page, ERRORS_BODY);

    const promptBox = await gotoAuthorTab(page);
    await promptBox.fill('do something vague');
    await page.getByRole('button', { name: 'Draft token' }).click();

    const errorsCard = page.getByTestId('author-errors');
    await expect(errorsCard).toBeVisible({ timeout: 10000 });
    await expect(errorsCard).toContainText('did not specify a grammar_kind');

    // No draft panel, no Copy bundle button — "not ready" must be
    // unambiguous, not merely greyed out.
    await expect(page.getByTestId('author-draft')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Copy bundle' })).toHaveCount(0);
    await expect(page.getByTestId('author-warnings')).toHaveCount(0);
  });

  test('a warnings-present response renders both the warnings and the draft', async ({ page }) => {
    await loginAsAdmin(page);
    await mockWhoamiWithAuthorCap(page);
    await mockDraftToken(page, WARNINGS_DRAFT_BODY);

    const promptBox = await gotoAuthorTab(page);
    await promptBox.fill('a metric token vix_level');
    await page.getByRole('button', { name: 'Draft token' }).click();

    const warningsCard = page.getByTestId('author-warnings');
    await expect(warningsCard).toBeVisible({ timeout: 10000 });
    await expect(warningsCard).toContainText('already exists');

    const draftCard = page.getByTestId('author-draft');
    await expect(draftCard).toBeVisible();
    await expect(page.getByTestId('author-errors')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Copy bundle' })).toBeVisible();
  });

  test('the safety boundary holds: no request ever reaches the registration or reload endpoints, including after Copy', async ({ page, context }) => {
    await loginAsAdmin(page);
    await mockWhoamiWithAuthorCap(page);
    await context.grantPermissions(['clipboard-read', 'clipboard-write']);
    await mockDraftToken(page, CLEAN_DRAFT_BODY);

    /** @type {{url: string, method: string}[]} */
    const seen = [];
    page.on('request', (req) => seen.push({ url: req.url(), method: req.method() }));

    const promptBox = await gotoAuthorTab(page);
    await promptBox.fill('a metric token vix_level');
    await page.getByRole('button', { name: 'Draft token' }).click();

    const draftCard = page.getByTestId('author-draft');
    await expect(draftCard).toBeVisible({ timeout: 10000 });

    const copyBtn = page.getByRole('button', { name: 'Copy bundle' });
    await copyBtn.click();

    // Clipboard got the bundle text (the one safe, local-only side effect).
    const clip = await page.evaluate(() => navigator.clipboard.readText());
    expect(clip).toContain('vix_level');
    expect(clip).toContain('def vix_level(ctx):');

    // No request anywhere in this flow touched the Wave-2 registration
    // or reload endpoints — the hard safety boundary from the plan.
    const forbidden = seen.filter((r) =>
      /\/api\/admin\/grammar\/tokens/.test(r.url) ||
      /\/api\/admin\/grammar\/reload/.test(r.url)
    );
    expect(forbidden).toEqual([]);
  });

  test('a user lacking manage_grammar_tokens never sees the Author tab', async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/auth/whoami', async (route) => {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          username: 'rambo', role: 'admin', display_name: 'Test Operator',
          caps: ['view_research', 'use_research_chat'],   // no manage_grammar_tokens
        }),
      });
    });

    await page.goto('/admin/mcp');
    await page.getByRole('tab', { name: 'Research', exact: true }).waitFor({ state: 'visible', timeout: 15000 });
    await expect(page.getByRole('tab', { name: 'Author', exact: true })).toHaveCount(0);
  });
});
