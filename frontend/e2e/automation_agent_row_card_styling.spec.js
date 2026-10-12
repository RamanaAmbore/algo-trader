// 2026-10 — operator ask: "make agents row background look similar to
// agent templates in automation." automation/+page.svelte's agent rows use
// the shared global .algo-status-card class, which app.css gives no
// background/border/radius of its own (only [data-status="..."] variants
// setting --st-fg/--st-bg/--st-border for the nested .algo-status-pill
// badge) — so rows rendered with no visible card chrome. Fix: a scoped
// .algo-status-card rule inside automation/+page.svelte's own <style>
// block, matching agent-templates/+page.svelte's .frag-row look exactly.
// Source-level guard, same pattern as held_orders_card_styling.spec.js.
//
// This is a REGRESSION GUARD: if a future edit "fixes" this globally in
// app.css instead of per-page, this test catches the drift by asserting
// the rule lives in automation/+page.svelte's own <style> block, and that
// app.css's base (non-attribute-selector) .algo-status-card rule still has
// no background/border of its own.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const pageSrc = readFileSync(
  new URL('../src/routes/(algo)/automation/+page.svelte', import.meta.url).pathname,
  'utf8'
);
const appCss = readFileSync(
  new URL('../src/app.css', import.meta.url).pathname,
  'utf8'
);

test.describe('automation agent rows — card chrome matches agent-templates .frag-row', () => {
  test('automation/+page.svelte has its own scoped .algo-status-card rule in <style>', () => {
    const styleBlock = pageSrc.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? '';
    expect(styleBlock).toMatch(/\.algo-status-card\s*\{/);
  });

  test('the scoped rule matches agent-templates .frag-row gradient, with a deliberately bumped border alpha', () => {
    // 2026-10 follow-up: .frag-row's own 0.10 border read as near-
    // invisible against this dark gradient (operator: "rows ... can have
    // a subtle border around them") — bumped to var(--card-divider)
    // (0.20) for actual visibility. Gradient/radius/overflow still match
    // .frag-row exactly; only the border alpha is an intentional
    // divergence from the original "match exactly" goal.
    const styleBlock = pageSrc.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? '';
    const rule = styleBlock.match(/\.algo-status-card\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/linear-gradient\(180deg,\s*#0f1729 0%,\s*#0a1020 100%\)/);
    expect(rule).toMatch(/border:\s*1px solid var\(--card-divider\)/);
    expect(rule).toMatch(/border-radius:\s*0\.3rem/);
    expect(rule).toMatch(/overflow:\s*hidden/);
  });

  test('the scoped rule has a hover border-color matching .frag-row:hover', () => {
    const styleBlock = pageSrc.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? '';
    const hoverRule = styleBlock.match(/\.algo-status-card:hover\s*\{[^}]*\}/)?.[0] ?? '';
    expect(hoverRule).toMatch(/rgba\(251,\s*191,\s*36,\s*0\.25\)/);
  });

  test('the scoped rule does not set padding (inline style="padding: 0" on the element already handles it)', () => {
    const styleBlock = pageSrc.match(/<style>([\s\S]*)<\/style>/)?.[1] ?? '';
    const rule = styleBlock.match(/\.algo-status-card\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).not.toMatch(/padding/);
  });

  test('app.css base .algo-status-card rule (no [data-status] selector) still has no background/border of its own', () => {
    // Match a bare `.algo-status-card {...}` rule — NOT `.algo-status-card[data-status=...]`
    // and NOT `.algo-status-card-2x`. Negative lookahead excludes both.
    const baseRuleMatch = appCss.match(/\.algo-status-card(?![-\[])\s*\{[^}]*\}/);
    // app.css deliberately has NO bare base rule for .algo-status-card at all
    // (confirms this fix stayed scoped to automation/+page.svelte and app.css
    // was not touched to add one).
    expect(baseRuleMatch).toBeNull();
  });

  test('app.css [data-status] variants only set --st-fg/--st-bg/--st-border custom properties', () => {
    const variantRules = appCss.match(/\.algo-status-card\[data-status="[^"]+"\]\s*\{[^}]*\}/g) ?? [];
    expect(variantRules.length).toBeGreaterThan(0);
    for (const rule of variantRules) {
      expect(rule).toMatch(/--st-fg:/);
      expect(rule).toMatch(/--st-bg:/);
      expect(rule).toMatch(/--st-border:/);
      expect(rule).not.toMatch(/\bbackground:\s*(?!var\(--st)/);
    }
  });
});

// ── 2026-10 follow-up: status-keyed border color + row text size ──────────
// Fix 1: the rows above all got a *visible* card (background/border/radius)
// but every row rendered with the SAME flat border regardless of status —
// `data-status={row.status}` fed the global app.css [data-status] block,
// which only sets --st-fg/--st-bg/--st-border for a nested .algo-status-pill
// badge this page never renders, so no color ever reached the card itself.
// Fix adds LOCAL [data-status] border-left variants covering the real agent
// vocabulary (statusDot(), above) and widens the data-status fallback to
// `row.status ?? (row.is_active ? 'active' : 'inactive')` so Brackets/
// Templates rows (no native `status` field — see automationKinds.js's
// bracketAgentToTemplateOut, which only sets is_active) also get a color.
// Fix 2: row.name/row.long_name dropped from --fs-lg to --fs-sm to match
// the rest of the app's row-text convention (Activity/LogPanel/Config
// tables use --fs-xs/--fs-sm, not --fs-lg).
import { loginAsAdmin } from './fixtures/auth.js';

test.describe('automation agent rows — status-keyed border color + row text size (live render)', () => {
  test.use({ projects: ['chromium-desktop'] });

  const TIMEOUT = 20_000;

  const BASE_FIELDS = {
    long_name: null, description: '', events: [], actions: [], scope: 'total',
    schedule: 'market_hours', cooldown_minutes: 30, fire_at_time: null,
    last_triggered_at: null, trigger_count: 0, last_error: null, is_system: false,
    lifespan_type: 'persistent', lifespan_max_fires: null, lifespan_expires_at: null,
    debounce_minutes: 0, tags: [], blackout_windows: [], trade_mode: 'paper',
    tier: 'medium', topic: 'general', group: null, cli_source: null,
  };

  const ACTIVE_AGENT = {
    ...BASE_FIELDS, id: 301, slug: 'active-agent-card-color-e2e', name: 'Active Agent Card Color E2E',
    conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1 },
    kind: 'cycle', status: 'active',
  };
  const INACTIVE_AGENT = {
    ...BASE_FIELDS, id: 302, slug: 'inactive-agent-card-color-e2e', name: 'Inactive Agent Card Color E2E',
    conditions: { metric: 'pnl', scope: 'positions.total', op: '<=', value: -1 },
    kind: 'cycle', status: 'inactive',
  };
  // No native `status` field at all once reshaped by bracketAgentToTemplateOut
  // — only `is_active` (derived server-side from status==='active'). Carries
  // `status: 'active'` here only because this mock represents the RAW
  // `GET /api/agents/` row (pre-adapter); the rendered row object the
  // component actually reads is the post-adapter shape with no `status` key.
  const ACTIVE_BRACKET = {
    ...BASE_FIELDS, id: 303, slug: 'bracket-401', name: 'Active Bracket Card Color E2E',
    conditions: {}, kind: 'bracket', status: 'active', legacy_template_id: 401,
    bracket_config: {
      applies_to: 'both', tp_pct: 30, sl_pct: -20, wing_premium_pct: null,
      wing_strike_offset: null, wing_max_spread_pct: null, tp_order_type: 'LIMIT',
      tp_scales_json: null, sl_trail_pct: null, is_default: false,
    },
  };

  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
    await page.route('**/api/agents/', async (route) => {
      if (route.request().method() !== 'GET') { await route.continue(); return; }
      await route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify([ACTIVE_AGENT, INACTIVE_AGENT, ACTIVE_BRACKET]),
      });
    });
  });

  test('active agent row border color differs from inactive agent row, and an active bracket row (no native status field) matches the active agent color', async ({ page }) => {
    // domcontentloaded, not networkidle — this page's WS reconnect loop
    // (connectWS) keeps the network non-idle indefinitely (same reason
    // playwright.config.js's own timeout comment calls out "SSE keeps
    // networkidle busy"), so wait on a concrete element instead.
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card[data-kind="agents"]')).toBeVisible({ timeout: TIMEOUT });

    await page.locator('.kind-card[data-kind="agents"]').click();
    // Playwright's `hasText` string filter is a case-insensitive substring
    // match, so filtering by 'Active Agent...' also matches the row whose
    // name is 'Inactive Agent...' (contains "active" as a substring).
    // Only one row of each status renders under the Agents card here, so
    // the data-status attribute is an unambiguous, collision-free selector.
    const activeAgentRow = page.locator('[role="listitem"][data-status="active"]');
    const inactiveAgentRow = page.locator('[role="listitem"][data-status="inactive"]');
    await expect(activeAgentRow).toBeVisible({ timeout: TIMEOUT });
    await expect(inactiveAgentRow).toBeVisible({ timeout: TIMEOUT });

    const borderColorOf = (locator) => locator.evaluate((el) => {
      const cs = getComputedStyle(el);
      // Implementation may use either a full border or a border-left
      // accent — read whichever is non-transparent/non-default.
      return cs.borderLeftColor !== cs.borderTopColor ? cs.borderLeftColor : cs.borderColor;
    });

    const activeColor = await borderColorOf(activeAgentRow);
    const inactiveColor = await borderColorOf(inactiveAgentRow);
    expect(activeColor, 'active vs inactive agent row border color').not.toBe(inactiveColor);

    // Kind cards stay visible always (2026-10-12 revision — no more
    // drill-down/Back navigation) — switching kinds is just another click.
    await page.locator('.kind-card[data-kind="brackets"]').click();
    // Only one row renders under the Brackets card — data-status is
    // enough on its own, same collision-avoidance reasoning as above.
    const bracketRow = page.locator('[role="listitem"][data-status="active"]');
    await expect(bracketRow).toBeVisible({ timeout: TIMEOUT });
    const bracketColor = await borderColorOf(bracketRow);
    expect(bracketColor, 'active bracket row (is_active fallback) vs active agent row border color').toBe(activeColor);
  });

  test('agent row name renders at --fs-sm, not --fs-lg', async ({ page }) => {
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('.kind-card[data-kind="agents"]')).toBeVisible({ timeout: TIMEOUT });
    await page.locator('.kind-card[data-kind="agents"]').click();

    // `.truncate` is the innermost name span's own class (see markup
    // around +page.svelte:758) — row.long_name is null in this fixture so
    // it's the only `.truncate` span in the row, avoiding the outer
    // wrapper `<span>` (which also contains the name as descendant text).
    const nameEl = page.locator('[role="listitem"][data-status="active"]').locator('span.truncate').first();
    await expect(nameEl).toBeVisible({ timeout: TIMEOUT });
    await expect(nameEl).toHaveText('Active Agent Card Color E2E');

    // Same technique as algo_consistency.spec.js's tokenDerivationCheck —
    // override the CSS var to a distinctive value and confirm the
    // computed font-size tracks it (proving the rule derives from the
    // var, not a frozen literal), then confirm --fs-lg has no effect.
    const underSm = await nameEl.evaluate((el) => {
      document.documentElement.style.setProperty('--fs-sm', '37px');
      const resolved = getComputedStyle(el).fontSize;
      document.documentElement.style.removeProperty('--fs-sm');
      return resolved;
    });
    expect(underSm, 'row name font-size under --fs-sm override').toBe('37px');

    const underLg = await nameEl.evaluate((el) => {
      document.documentElement.style.setProperty('--fs-lg', '41px');
      const resolved = getComputedStyle(el).fontSize;
      document.documentElement.style.removeProperty('--fs-lg');
      return resolved;
    });
    expect(underLg, 'row name font-size must not track --fs-lg').not.toBe('41px');
  });
});
