// Automation page: "+ New Agent" entry point, independent of the existing
// Ask-AI flow. Kind selector (Threshold / Notification); Threshold reuses
// the exact inline editor (thresholdEditorBody snippet); Notification opens
// the dedicated event-agent builder (renderer fetched live, never hardcoded).
// Source-level guard: reads the real page source (no live backend route
// for GET /api/agents/renderers was reachable in this sandbox — see report).

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const src = readFileSync(
  new URL('../src/routes/(algo)/automation/+page.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('automation — "+ New Agent" entry point', () => {
  test('a dedicated pill opens the panel, independent of Ask-AI', () => {
    expect(src).toMatch(/class="ai-pill new-agent-pill" onclick=\{toggleNewAgentPanel\}/);
    expect(src).toMatch(/\{creatingKind \? '× Close' : '\+ New Agent'\}/);
    // Ask-AI's own toggle is untouched.
    expect(src).toMatch(/onclick=\{\(\) => aiOpen = !aiOpen\}/);
  });

  test('kind selector offers Threshold and Notification', () => {
    expect(src).toMatch(/onclick=\{\(\) => startCreate\('threshold'\)\}>Threshold</);
    expect(src).toMatch(/onclick=\{\(\) => startCreate\('event'\)\}>Notification</);
  });

  test('Threshold create reuses thresholdEditorBody with a null agent (blank defaults)', () => {
    expect(src).toMatch(/\{@render thresholdEditorBody\(null\)\}/);
    expect(src).toMatch(/function startCreate\(/);
    // startCreate('threshold') seeds editing='__new__' and resets editForm
    // to the same blank defaults the module starts with.
    const fn = src.slice(src.indexOf('function startCreate('), src.indexOf('function saveEventCreate('));
    expect(fn).toMatch(/editing = '__new__'/);
    expect(fn).toMatch(/lifespan_type: 'persistent'/);
  });

  test('threshold create path requires a slug and posts kind:"threshold"', () => {
    expect(src).toMatch(/if \(isCreate && !newAgentSlug\.trim\(\)\)/);
    expect(src).toMatch(/slug: newAgentSlug\.trim\(\),\s*\n\s*kind: 'threshold',/);
  });

  test('Notification create fetches renderers live — no hardcoded list', () => {
    expect(src).toMatch(/import \{[\s\S]*fetchAgentRenderers[\s\S]*\} from '\$lib\/api'/);
    expect(src).toMatch(/async function loadRenderers\(\)/);
    expect(src).toMatch(/const rows = await fetchAgentRenderers\(\);/);
    // A failed fetch must not permanently poison the list with [].
    expect(src).toMatch(/Never clobber a previously-successful list with \[\] on a/);
  });

  test('event create payload shape: kind, conditions.log, actions[0].render', () => {
    const fn = src.slice(src.indexOf('async function saveEventCreate('), src.indexOf('function eventRendererKey('));
    expect(fn).toMatch(/kind: 'event',/);
    expect(fn).toMatch(/tag: f\.tag\.trim\(\),/);
    expect(fn).toMatch(/min_level: f\.minLevel,/);
    expect(fn).toMatch(/actions: \[\{ type: 'render', render: f\.renderer \}\],/);
  });

  test('event create validates renderer, tag, and at least one channel before posting', () => {
    const fn = src.slice(src.indexOf('async function saveEventCreate('), src.indexOf('function eventRendererKey('));
    expect(fn).toMatch(/if \(!f\.renderer\)\s*\{ eventCreateErrors = \['Pick a renderer'\]; return; \}/);
    expect(fn).toMatch(/if \(!f\.tag\.trim\(\)\)\s*\{ eventCreateErrors = \['Log tag is required'\]; return; \}/);
    expect(fn).toMatch(/if \(!enabledIds\.length\) \{ eventCreateErrors = \['Pick at least one channel'\]; return; \}/);
  });

  test('backend 422 detail surfaces via fullMessage, not just the short toast', () => {
    expect(src).toMatch(/eventCreateErrors = \[e\.fullMessage \|\| e\.message\];/);
    expect(src).toMatch(/validationErrors = \[e\.fullMessage \|\| e\.message\];/);
  });
});
