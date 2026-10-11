/**
 * LogPanel Agents tab — Level + Kind filters (Sprint G addendum, `debug(x)`).
 *
 * The `debug(x)` CLI statement compiles to the existing `emit_log` action
 * with `level="debug"` and lands as an ordinary AgentEvent row (via
 * `_log_action_success` writing `detail=params` for every action type,
 * backend/api/algo/actions.py). Used carelessly it clutters the Agents
 * tab, so two new, AND-combined filters narrow what's shown:
 *   - Level (multi-select toggle chips, DEBUG..CRITICAL) — DEBUG hidden
 *     by default, other levels on.
 *   - Kind (single-select narrowing chips, agent/alert/bracket/template)
 *     — defaults to 'All', never hides anything by default.
 * Both persist to localStorage across a reload.
 *
 * Mocks `/api/agents/events/recent` with five deterministic rows spanning
 * every level/kind combination so filtering can be asserted by row COUNT
 * (the Agents tab renders `trigger_condition` chips as its row content,
 * not the action's own `detail.message` — so distinguishing rows by text
 * is unreliable; count-based assertions sidestep that entirely).
 *
 * Five quality dimensions (per feedback_test_dimensions.md):
 *   SSOT     — level/kind both read off the SAME `_agentLevel`/
 *              `_agentKindBucket` helpers the row-rendering map() uses;
 *              no second parallel filter definition.
 *   Perf     — a single intercepted fetch per test, no polling wait
 *              beyond the panel's own first render.
 *   Stale    — source-scan confirms the filter bar + both tab-id arrays
 *              are wired into LogPanel.svelte (guards future refactors).
 *   Reusable — filter chips reuse the existing `.om-chip` class/shape
 *              from the Order-tab mode strip, not a bespoke control.
 *   UX       — DEBUG is off by default (no clutter); Kind defaults to
 *              All (no accidental hiding); mobile viewports get the
 *              same chip row (LogPanel has no desktop-only branch here).
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname  = path.dirname(__filename);

const NOW = new Date().toISOString();

// Five rows spanning every (level, kind) bucket this spec exercises.
// `agent_kind` is the ASSUMED backend field name (see LogPanel.svelte's
// `_agentKindBucket` comment) — a parallel backend change was still
// landing this field as of this spec's authoring; if the real field
// name differs, only the mock payload below needs updating.
const MOCK_ROWS = [
  { id: 1, agent_id: 10, event_type: 'action_success', trigger_condition: null,
    detail: { level: 'debug', message: 'dbg' }, timestamp: NOW, sim_mode: false, agent_kind: 'event' },      // level=debug, kind=alert
  { id: 2, agent_id: 11, event_type: 'action_success', trigger_condition: null,
    detail: {}, timestamp: NOW, sim_mode: false, agent_kind: 'cycle' },                                       // level=info,  kind=agent
  { id: 3, agent_id: 12, event_type: 'action_failed', trigger_condition: null,
    detail: {}, timestamp: NOW, sim_mode: false, agent_kind: 'bracket' },                                     // level=error, kind=bracket
  { id: 4, agent_id: 13, event_type: 'cooldown', trigger_condition: null,
    detail: {}, timestamp: NOW, sim_mode: false, agent_kind: 'template' },                                    // level=warning, kind=template
  { id: 5, agent_id: 14, event_type: 'action_success', trigger_condition: null,
    detail: { level: 'critical' }, timestamp: NOW, sim_mode: false, agent_kind: 'cycle' },                    // level=critical, kind=agent
];

/** @param {import('@playwright/test').Page} page */
async function mockAgentEvents(page) {
  await page.route('**/api/agents/events/recent*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_ROWS) });
  });
  await page.route('**/api/simulator/events/recent*', async (route) => {
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) });
  });
}

/** @param {import('@playwright/test').Page} page */
async function openAgentsTab(page) {
  await loginAsAdmin(page);
  await mockAgentEvents(page);
  await page.goto('/automation', { waitUntil: 'domcontentloaded' });
  const strip = page.locator('.lp-header-wrap').first();
  await expect(strip).toBeVisible({ timeout: 15_000 });
  await strip.getByRole('tab', { name: 'Agents' }).click();
  await expect(page.locator('[data-testid="agent-filter-bar"]')).toBeVisible({ timeout: 10_000 });
  // Row count stabilises once the mocked fetch resolves and _agentRows
  // recomputes — wait for the default-filtered count (4, DEBUG hidden)
  // rather than an arbitrary timeout.
  await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(4, { timeout: 10_000 });
}

test.describe('LogPanel Agents tab — Level + Kind filters', () => {
  test.setTimeout(60_000);

  test('DEBUG is hidden by default and revealed when toggled on', async ({ page }) => {
    await openAgentsTab(page);

    // Default: info/warning/error/critical visible, debug hidden → 4 rows.
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(4);
    const debugChip = page.locator('[data-testid="agent-level-chip-debug"]');
    await expect(debugChip).toHaveAttribute('aria-pressed', 'false');

    await debugChip.click();
    await expect(debugChip).toHaveAttribute('aria-pressed', 'true');
    // All 5 mock rows now pass the level filter.
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(5);

    // Toggling back off hides it again.
    await debugChip.click();
    await expect(debugChip).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(4);
  });

  test('Kind filter narrows to exactly one kind', async ({ page }) => {
    await openAgentsTab(page);

    const bracketChip = page.locator('[data-testid="agent-kind-chip-bracket"]');
    await bracketChip.click();
    await expect(bracketChip).toHaveAttribute('aria-pressed', 'true');
    // Only row #3 (kind=bracket, level=error — error is on by default).
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(1);

    // Switching to Template narrows to row #4 instead.
    await page.locator('[data-testid="agent-kind-chip-template"]').click();
    await expect(bracketChip).toHaveAttribute('aria-pressed', 'false');
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(1);

    // Back to All restores the default-level-filtered count.
    await page.locator('[data-testid="agent-kind-chip-all"]').click();
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(4);
  });

  test('Level and Kind filters combine with AND, not OR', async ({ page }) => {
    await openAgentsTab(page);

    // Kind=Agent alone matches rows #2 (info) and #5 (critical) → 2 rows.
    await page.locator('[data-testid="agent-kind-chip-agent"]').click();
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(2);

    // Narrow Level to ONLY critical (toggle off info/warning/error) —
    // combined with Kind=Agent, only row #5 (critical, agent) should
    // remain. If the two filters were OR'd instead of AND'd, toggling
    // off info/warning/error would still leave row #2 visible (it's an
    // 'agent'-kind row) purely via the kind match — this assertion is
    // the one that would catch that regression.
    await page.locator('[data-testid="agent-level-chip-info"]').click();
    await page.locator('[data-testid="agent-level-chip-warning"]').click();
    await page.locator('[data-testid="agent-level-chip-error"]').click();
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(1);
  });

  test('filter state persists across a reload', async ({ page }) => {
    await openAgentsTab(page);

    await page.locator('[data-testid="agent-level-chip-debug"]').click();
    await page.locator('[data-testid="agent-kind-chip-bracket"]').click();
    await expect(page.locator('[data-testid="agent-level-chip-debug"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('[data-testid="agent-kind-chip-bracket"]')).toHaveAttribute('aria-pressed', 'true');

    await mockAgentEvents(page); // route handlers don't survive reload — re-arm
    await page.reload({ waitUntil: 'domcontentloaded' });
    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });
    await strip.getByRole('tab', { name: 'Agents' }).click();
    await expect(page.locator('[data-testid="agent-filter-bar"]')).toBeVisible({ timeout: 10_000 });

    await expect(page.locator('[data-testid="agent-level-chip-debug"]')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.locator('[data-testid="agent-kind-chip-bracket"]')).toHaveAttribute('aria-pressed', 'true');
    // Kind=bracket (row #3, level=error — matches even with debug ALSO
    // enabled, since debug only adds rows, never removes the error one).
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(1);
  });
});

test.describe('[STALE] source-scan — Agents tab filter wiring present', () => {
  test('LogPanel defines both filter tab arrays and the AND-combined predicate', () => {
    const logPanelPath = path.resolve(__dirname, '..', 'src', 'lib', 'LogPanel.svelte');
    const src = fs.readFileSync(logPanelPath, 'utf8');
    expect(src, 'must define the 5-level vocabulary').toMatch(/_LOG_LEVELS\s*=\s*\[.*'debug'.*'critical'.*\]/s);
    expect(src, 'must define the 4-kind filter tabs').toMatch(/_AGENT_KIND_TABS\s*=\s*\[/);
    expect(src, 'default level filter must hide debug').toMatch(/_agentLevelFilter\s*=\s*\$state\(new Set\(\['info', 'warning', 'error', 'critical'\]\)\)/);
    expect(src, 'default kind filter must be All').toMatch(/_agentKindFilter\s*=\s*\$state\('all'\)/);
    expect(src, 'level + kind filters must AND inside _agentRows').toMatch(/_agentLevelFilter\.has\(_agentLevel\(e\)\)/);
    expect(src, 'kind filter must AND inside _agentRows').toMatch(/_agentKindBucket\(e\) !== _agentKindFilter/);
  });

  test('both filters persist via localStorage with the CollapseButton-style try/catch pattern', () => {
    const logPanelPath = path.resolve(__dirname, '..', 'src', 'lib', 'LogPanel.svelte');
    const src = fs.readFileSync(logPanelPath, 'utf8');
    expect(src).toMatch(/_AGENT_LEVEL_LS_KEY\s*=\s*'ramboq\.logpanel\.agentLevelFilter'/);
    expect(src).toMatch(/_AGENT_KIND_LS_KEY\s*=\s*'ramboq\.logpanel\.agentKindFilter'/);
  });
});
