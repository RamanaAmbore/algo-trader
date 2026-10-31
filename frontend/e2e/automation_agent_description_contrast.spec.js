/**
 * automation_agent_description_contrast.spec.js — 2026-10-31, operator:
 * "the off white text description in agent is not clearly visible."
 *
 * Root cause: the agent description text (both the collapsed row's
 * `.aw-row-desc` and the expanded view's description `<div>`, in
 * `automation/+page.svelte`) used `--c-muted` (aliases `--algo-muted`,
 * #7e97b8) — documented in app.css's "WCAG-guaranteed text tier" block
 * as only 4.08:1 contrast on the elevated card surface, below the
 * 4.5:1 WCAG minimum. `--text-lo` (#90a2b2, 4.66-5.45:1) is the token
 * app.css itself names as the correct replacement for exactly this
 * case ("muted row secondary — reason, timestamp, footer notes").
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

  test('collapsed-row description (.aw-row-desc) uses --text-lo, not --c-muted', () => {
    const match = content.match(/<span class="aw-row-desc[^"]*"/);
    expect(match, '.aw-row-desc span must exist').not.toBeNull();
    expect(match[0]).toMatch(/text-\[var\(--text-lo\)\]/);
    expect(match[0]).not.toMatch(/text-\[var\(--c-muted\)\]/);
  });

  test('expanded-view agent.description div uses --text-lo, not --c-muted', () => {
    const idx = content.indexOf('{:else if agent.description}');
    expect(idx, 'expanded agent.description branch must exist').toBeGreaterThan(-1);
    const snippet = content.slice(idx, idx + 700);
    expect(snippet).toMatch(/text-\[var\(--text-lo\)\]/);
    expect(snippet).not.toMatch(/<div class="[^"]*text-\[var\(--c-muted\)\][^"]*">\{agent\.description\}/);
  });
});
