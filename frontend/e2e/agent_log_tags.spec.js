// Sprint 4 — agent editor: log tags, channel tag filters, and the log-match builder.
//
// Source-level guard, no server or auth needed. Reads the real page source.
//
// Five quality dimensions:
//   1. SSOT    — tags come from the grammar registry (fetchGrammarTokens 'log'), not a hard-coded list.
//   2. Perf    — pure fs read, no browser.
//   3. Stale   — guards the Sprint 4 wiring against silent removal.
//   4. Reuse   — mirrors acu5195_removed.spec.js.
//   5. UX      — the log channel stays (option 1), and chips appear only on enabled channels.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const src = readFileSync(
  new URL('../src/routes/(algo)/automation/+page.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('agent editor — log tags (Sprint 4)', () => {
  test('tags are loaded from the grammar registry, not hard-coded', () => {
    expect(src).toMatch(/fetchGrammarTokens\('log'\)/);
    expect(src).toMatch(/r\.token_kind === 'tag'/);
  });

  test('channel rows keep the log channel and gain a tag filter', () => {
    expect(src).toMatch(/\{ id: 'log',\s+label: 'Log'/);
    expect(src).toMatch(/channel-tags/);
    expect(src).toMatch(/toggleChannelTag\(ch\.id, t\)/);
  });

  test('tag chips appear only for enabled channels', () => {
    expect(src).toMatch(/\{#if isChannelEnabled\(ch\.id\) && logTags\.length\}/);
  });

  test('tags are written to the channel row as a tags array', () => {
    expect(src).toMatch(/const row = \{ \.\.\.list\[idx\], tags: next \};/);
  });

  test('log-match builder wraps the existing condition with AND', () => {
    expect(src).toMatch(/function addLogMatch\(\)/);
    expect(src).toMatch(/const leaf = \{ log: \{ tag: logTagPick, min_level: logMinLevel \} \};/);
    expect(src).toMatch(/\{ all: \[cond, leaf\] \}/);
  });

  test('the log-match button is disabled until a tag is picked', () => {
    expect(src).toMatch(/disabled=\{!logTagPick\}/);
  });
});
