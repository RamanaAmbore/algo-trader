/**
 * log_panel_agent_detail_rendering.spec.js
 *
 * Replaces log_panel_agent_level_kind_filters.spec.js (deleted 2026-10-18).
 * The old spec targeted a Level/Kind filter-chip UI (`data-testid="agent-
 * filter-bar"` / `agent-level-chip-*` / `agent-kind-chip-*`) that had
 * ALREADY been removed from LogPanel.svelte's template in a prior
 * cleanup (see LogPanel.svelte's own "Agents-tab Level/Kind filter bar
 * CSS removed" comment) — so every UI-waiting assertion in that spec was
 * already dead on arrival, independent of this task. It also mocked
 * `detail` as a plain JS OBJECT and `agent_kind` as a TOP-LEVEL field —
 * the real backend shape (`AgentEventInfo.detail: str | None`,
 * backend/api/routes/agents.py) is a JSON-ENCODED STRING with no
 * top-level `agent_kind` at all (it rides inside the parsed `detail`,
 * backend/api/algo/actions.py:315,346).
 *
 * This spec instead exercises the REAL fix (2026-10-18, CLAUDE.md items
 * 9-11): `_agentLevel()` now reads `.level` from `parseJsonObject(e.detail)`
 * (a JSON string) instead of the always-undefined `e.detail.level`, and
 * the Agents tab now renders the detail's own useful fields (via the
 * shared `chipsFromJson`) instead of a bare event name with no context.
 *
 * Five quality dimensions (feedback_test_dimensions.md):
 *   SSOT     — detail parsing/rendering goes through the one shared
 *              $lib/logChips helper (locked in by logChips.test.js +
 *              this spec's wiring source-grep in wave3_css_fontsize_
 *              consistency.spec.js); this spec only proves END-TO-END
 *              behavior through a real mocked fetch + render.
 *   Perf     — a single intercepted fetch, no polling wait beyond the
 *              panel's own first render.
 *   Stale    — uses the REAL AgentEventInfo field shape (JSON-string
 *              detail, no top-level agent_kind), not a speculative one.
 *   Reusable — reuses the panel's own row rendering, no bespoke markup.
 *   UX       — DEBUG is hidden by default (no clutter) even though the
 *              bug that made it ALWAYS hidden (regardless of payload) is
 *              now fixed to the real, data-driven threshold.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/log_panel_agent_detail_rendering.spec.js
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';

const NOW = new Date().toISOString();

// Realistic AgentEventInfo rows — `detail` is a JSON-ENCODED STRING
// (matching backend/api/routes/agents.py's real struct + backend/api/
// algo/events.py's json.dumps()), with NO top-level `agent_kind`.
const MOCK_ROWS = [
  {
    id: 1, agent_id: 10, event_type: 'action_success', trigger_condition: null,
    detail: JSON.stringify({ level: 'debug', message: 'dbg', agent_kind: 'event' }),
    timestamp: NOW, sim_mode: false,
  },
  {
    id: 2, agent_id: 11, event_type: 'action_success', trigger_condition: null,
    detail: JSON.stringify({ account: 'ZG0790', qty: 50, agent_kind: 'cycle' }),
    timestamp: NOW, sim_mode: false,
  },
  {
    id: 3, agent_id: 12, event_type: 'action_failed', trigger_condition: null,
    detail: JSON.stringify({ reason: 'preflight blocked', agent_kind: 'bracket' }),
    timestamp: NOW, sim_mode: false,
  },
  {
    id: 4, agent_id: 13, event_type: 'cooldown', trigger_condition: null,
    detail: null, timestamp: NOW, sim_mode: false,
  },
  {
    id: 5, agent_id: 14, event_type: 'action_success', trigger_condition: null,
    detail: JSON.stringify({ level: 'critical', symbol: 'NIFTY26MAY22000CE' }),
    timestamp: NOW, sim_mode: false,
  },
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

test.describe('LogPanel Agents tab — detail parsing + rendering (2026-10-18 fix)', () => {
  test.setTimeout(60_000);

  test('DEBUG row (level parsed out of the JSON-string detail) is hidden by default; the other 4 render', async ({ page }) => {
    await loginAsAdmin(page);
    await mockAgentEvents(page);
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });
    await strip.getByRole('tab', { name: 'Agents' }).click();

    // Row #1 (level=debug, parsed correctly from the JSON-string detail)
    // is hidden by the default level filter; rows #2-#5 render.
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(4, { timeout: 10_000 });
  });

  test('a row with account/qty in its detail renders those fields as readable chips, not a bare event name', async ({ page }) => {
    await loginAsAdmin(page);
    await mockAgentEvents(page);
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });
    await strip.getByRole('tab', { name: 'Agents' }).click();
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(4, { timeout: 10_000 });

    const rowsText = await page.locator('.log-panel.log-rows .log-row').allInnerTexts();
    const joined = rowsText.join(' | ');
    // Row #2's account shows up as real readable text (item 10 fix).
    expect(joined).toContain('ZG0790');
  });

  test('agent_kind is never rendered as a visible chip (it is plumbing for the removed Kind filter, not operator context)', async ({ page }) => {
    await loginAsAdmin(page);
    await mockAgentEvents(page);
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });
    await strip.getByRole('tab', { name: 'Agents' }).click();
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(4, { timeout: 10_000 });

    const rowsText = await page.locator('.log-panel.log-rows .log-row').allInnerTexts();
    const joined = rowsText.join(' | ');
    expect(joined).not.toContain('agent_kind');
  });

  test('detail renders as human-readable chips, never a raw JSON blob', async ({ page }) => {
    await loginAsAdmin(page);
    await mockAgentEvents(page);
    await page.goto('/automation', { waitUntil: 'domcontentloaded' });
    const strip = page.locator('.lp-header-wrap').first();
    await expect(strip).toBeVisible({ timeout: 15_000 });
    await strip.getByRole('tab', { name: 'Agents' }).click();
    await expect(page.locator('.log-panel.log-rows .log-row')).toHaveCount(4, { timeout: 10_000 });

    const rowsText = await page.locator('.log-panel.log-rows .log-row').allInnerTexts();
    const joined = rowsText.join(' | ');
    expect(joined).not.toContain('{"');
  });
});
