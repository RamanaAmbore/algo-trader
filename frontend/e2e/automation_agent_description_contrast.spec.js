/**
 * automation_agent_description_contrast.spec.js — 2026-10-31, operator:
 * "the off white text description in agent is not clearly visible," then,
 * after a first --c-muted -> --text-lo contrast fix still read as too dim:
 * "the text is not visible clearly. use the other white text in logs, and
 * use the same color."
 *
 * Root cause (original): the agent description text (both the collapsed
 * row's `.aw-row-desc` and the expanded view's description `<div>`, in
 * `automation/+page.svelte`) used `--c-muted` (aliases `--algo-muted`,
 * #7e97b8) — documented in app.css's "WCAG-guaranteed text tier" block
 * as only 4.08:1 contrast on the elevated card surface, below the
 * 4.5:1 WCAG minimum. The first fix swapped to `--text-lo` (#90a2b2,
 * 4.66-5.45:1).
 *
 * Final fix: the operator asked for the SAME white used by log text.
 * LogPanel's own `.log-row` base text color is `var(--algo-slate)`
 * (#ffffff), aliased as `--text-primary` in app.css. Both description
 * sites now use `--text-primary` instead of `--text-lo`.
 *
 * Static source checks only — no live render needed for a pure color
 * token swap with no layout/behavior change.
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import { join } from 'path';

function readFile(relPath) {
  return readFileSync(join(process.cwd(), relPath), 'utf-8');
}

test.describe('Automation page — agent description text contrast (2026-10-31)', () => {
  const content = readFile('src/routes/(algo)/automation/+page.svelte');

  test('collapsed-row description (.aw-row-desc) uses --text-primary, not --c-muted/--text-lo', () => {
    const match = content.match(/<span class="aw-row-desc[^"]*"/);
    expect(match, '.aw-row-desc span must exist').not.toBeNull();
    expect(match[0]).toMatch(/text-\[var\(--text-primary\)\]/);
    expect(match[0]).not.toMatch(/text-\[var\(--c-muted\)\]/);
    expect(match[0]).not.toMatch(/text-\[var\(--text-lo\)\]/);
  });

  test('expanded-view agent.description div uses --text-primary, not --c-muted/--text-lo', () => {
    const idx = content.indexOf('{:else if agent.description}');
    expect(idx, 'expanded agent.description branch must exist').toBeGreaterThan(-1);
    const snippet = content.slice(idx, idx + 700);
    expect(snippet).toMatch(/text-\[var\(--text-primary\)\]/);
    expect(snippet).not.toMatch(/<div class="[^"]*text-\[var\(--c-muted\)\][^"]*">\{agent\.description\}/);
    expect(snippet).not.toMatch(/<div class="[^"]*text-\[var\(--text-lo\)\][^"]*">\{agent\.description\}/);
  });

  test('description text color matches LogPanel\'s own log-row base text color', () => {
    const logPanel = readFile('src/lib/LogPanel.svelte');
    const logRowMatch = logPanel.match(/:global\(\.log-panel\.log-rows \.log-row\)\s*\{[^}]*color:\s*var\(([^)]+)\)/s);
    expect(logRowMatch, '.log-row base color rule must exist').not.toBeNull();
    expect(logRowMatch[1].trim()).toBe('--algo-slate');

    const appCss = readFile('src/app.css');
    expect(appCss).toMatch(/--text-primary:\s*var\(--algo-slate\)/);
  });
});
