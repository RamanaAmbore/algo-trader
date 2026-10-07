// Automation page: per-card "Global: X" override display.
// Threshold agents — the override IS the existing trade_mode field (no
// separate override mechanism invented); this only adds the informational
// "Global: <default>" label next to it, in both the editor and the normal
// expanded view. Event agents — the override IS the existing per-channel
// `events[].enabled` toggle (already wired via toggleChannel/eventEditorBody);
// there is no backend concept of a "global default channel set" to compare
// against, so no separate "Global: X" label is invented for event agents.
// Also covers: _buildEditPayload already forwarding tier/topic (plan item 4).
// Source-level guard: reads the real page source.

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';

const src = readFileSync(
  new URL('../src/routes/(algo)/automation/+page.svelte', import.meta.url).pathname, 'utf8'
);

test.describe('automation — per-card global-default override display', () => {
  test('global default trade mode is fetched once on mount, not hardcoded', () => {
    expect(src).toMatch(/let globalDefaultTradeMode = \$state\('paper'\);/);
    expect(src).toMatch(/async function loadGlobalDefaultTradeMode\(\)/);
    expect(src).toMatch(/fetchSetting\('execution\.default_agent_trade_mode'\)/);
    expect(src).toMatch(/loadGlobalDefaultTradeMode\(\);/);
  });

  test('the threshold editor shows Global default next to the trade_mode Select', () => {
    const body = src.slice(
      src.indexOf("{#snippet thresholdEditorBody("),
      src.indexOf("{#snippet eventEditorBody(")
    );
    expect(body).toMatch(/Global default: <b>\{globalDefaultTradeMode\.toUpperCase\(\)\}<\/b>/);
    expect(body).toMatch(/\{#if editForm\.trade_mode !== globalDefaultTradeMode\}/);
    expect(body).toMatch(/\(overridden\)/);
    expect(body).toMatch(/\(inherited\)/);
  });

  test('the normal (non-editing) expanded view also shows Mode + global default, threshold only', () => {
    // agent.kind on READ is always the DB's real vocabulary ('cycle' or
    // 'event' — see backend/api/routes/agents.py:_agent_to_info /
    // _age_normalize_kind), never the write-side 'threshold' synonym, so
    // the gate is an inequality against 'event', not an equality against
    // 'threshold' (which would never match any real agent row).
    expect(src).toMatch(/\{#if agent\.kind !== 'event'\}\s*\n\s*<span class="mx-1">\|<\/span>/);
    expect(src).toMatch(/Mode: \{\(agent\.trade_mode \|\| 'paper'\)\.toUpperCase\(\)\}/);
    expect(src).toMatch(/\(global: \{globalDefaultTradeMode\.toUpperCase\(\)\}\)/);
  });

  test('event agents do not get an invented "global channel default" — the per-channel toggle IS the override', () => {
    const eventBody = src.slice(
      src.indexOf("{#snippet eventEditorBody("),
      src.indexOf("{#snippet eventCreateBuilder(")
    );
    expect(eventBody).not.toMatch(/Global default/);
    // The per-channel enabled checkbox already reuses the generic helper —
    // no second, parallel override control was added for event agents.
    expect(eventBody).toMatch(/checked=\{isChannelEnabled\(ch\.id\)\}/);
  });

  test('no duplicate trade_mode override mechanism was added — Select stays the only control', () => {
    // Exactly one Select bound to editForm.trade_mode inside thresholdEditorBody.
    const body = src.slice(
      src.indexOf("{#snippet thresholdEditorBody("),
      src.indexOf("{#snippet eventEditorBody(")
    );
    const matches = body.match(/bind:value=\{editForm\.trade_mode\}/g) || [];
    expect(matches.length).toBe(1);
  });

  test('_buildEditPayload already forwards tier and topic (plan item 4 — verification only)', () => {
    const fn = src.slice(src.indexOf('function _buildEditPayload('), src.indexOf('async function saveEdit('));
    expect(fn).toMatch(/tier:\s*editForm\.tier\s*\|\|\s*'medium',/);
    expect(fn).toMatch(/topic:\s*editForm\.topic\s*\|\|\s*'general',/);
  });

  test('create path also sends kind alongside the same _buildEditPayload fields (tier/topic included)', () => {
    const fn = src.slice(src.indexOf('async function saveEdit('), src.indexOf('function connectWS('));
    expect(fn).toMatch(/kind: 'threshold',\s*\n\s*\.\.\._buildEditPayload\(tagsList, bwResult\.value\),/);
  });
});
