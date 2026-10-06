// Sprint 3 — fragment impact preview on the agent templates page.
//
// Source-level guard, no server or auth needed. Reads the real source files.
//
// Five quality dimensions:
//   1. SSOT    — the impact list comes from the backend endpoint, not a client-side walk.
//   2. Perf    — pure fs read, no browser.
//   3. Stale   — guards the save, deactivate, and delete checks against silent removal.
//   4. Reuse   — mirrors acu5195_removed.spec.js path resolution.
//   5. UX      — slugs are escaped before they reach the confirm modal.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const read = (rel) => readFileSync(new URL(rel, import.meta.url).pathname, 'utf8');
const page = read('../src/routes/(algo)/automation/agent-templates/+page.svelte');
const api = read('../src/lib/api.js');
const backend = read('../../backend/api/routes/agent_templates.py');

test.describe('fragment impact preview', () => {
  test('client calls the backend references endpoint', () => {
    expect(api).toMatch(/fetchFragmentReferences = \(id\) =>/);
    expect(api).toMatch(/\/admin\/fragments\/\$\{id\}\/references/);
    expect(backend).toMatch(/@get\("\/\{frag_id:int\}\/references"/);
  });

  test('save, deactivate, and delete each check impact first', () => {
    expect(page).toMatch(/confirmAgents\(current, 'Save', agents\)/);
    expect(page).toMatch(/confirmAgents\(f, 'Deactivate', agents\)/);
    expect(page).toMatch(/const agents = await impactFor\(f\);\n    if \(agents === null\) return;/);
  });

  test('a failed impact check blocks the change', () => {
    expect(page).toMatch(/agents === null \|\| !\(await confirmAgents/);
  });

  test('slugs and names are escaped before reaching the confirm modal', () => {
    expect(page).toMatch(/escapeHtml\(a\.slug\)/);
    expect(page).toMatch(/escapeHtml\(f\.name\)/);
  });

  test('a fragment with no referencing agents is not prompted', () => {
    expect(page).toMatch(/if \(agents\.length === 0\) return true;/);
  });
});
