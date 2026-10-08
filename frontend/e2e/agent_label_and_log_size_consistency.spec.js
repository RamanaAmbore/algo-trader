/**
 * agent_label_and_log_size_consistency.spec.js
 *
 * Source-scan regression guard for a 2026-10 operator-reported
 * consistency fix (no browser needed — same pattern as
 * algo_consistency.spec.js's literal-grep tests):
 *
 *   1. The Agents page's `agent.long_name` label ("when: ... — do: ...")
 *      previously used `--algo-slate-muted` (55%-opacity translucent
 *      white, low contrast). The Agent Templates page's comparable
 *      name/description text used hardcoded off-palette
 *      `rgba(180,200,230,α)` literals with inconsistent alpha across
 *      sites — meaning the two pages rendered visibly different colors
 *      for the same semantic role. Both now use the shared `--c-muted`
 *      token.
 *   2. Agent Templates' `.frag-name` was `--fs-md` (0.65rem) while the
 *      Agents page's comparable primary label was `--fs-lg` (0.72rem) —
 *      now both `--fs-lg`.
 *   3. LogPanel's shared `.log-row-msg` rule was `--fs-base` (14px), a
 *      noticeably bigger jump than the rest of the app's compact-density
 *      scale — now `--fs-lg`, propagating to every one of its ~25 mount
 *      sites (OrderBook, OrderTicket, SymbolPanel, /activity, /dashboard,
 *      /orders, /console, etc.) via the one shared component.
 */

import { test, expect } from '@playwright/test';
import fs from 'fs';
import path from 'path';

const AGENTS_PAGE = fs.readFileSync(
  path.join('src/routes/(algo)/automation', '+page.svelte'),
  'utf-8',
);
const AGENT_TEMPLATES_PAGE = fs.readFileSync(
  path.join('src/routes/(algo)/automation/agent-templates', '+page.svelte'),
  'utf-8',
);
const LOG_PANEL = fs.readFileSync(path.join('src/lib', 'LogPanel.svelte'), 'utf-8');

test.describe('Agent label color/size consistency (2026-10 fix)', () => {
  test('Agents page long_name label uses --c-muted, not the low-contrast --algo-slate-muted', () => {
    expect(AGENTS_PAGE).toMatch(/\{agent\.long_name\}/);
    const styleLine = AGENTS_PAGE.split('\n').find((l) => l.includes('{agent.long_name}')) ?? '';
    expect(styleLine, 'long_name <span> line').toMatch(/color:\s*var\(--c-muted\)/);
    expect(styleLine).not.toMatch(/--algo-slate-muted/);
  });

  test('Agent Templates page has zero remaining hardcoded rgba(180,200,230,α) text-color literals', () => {
    expect(AGENT_TEMPLATES_PAGE).not.toMatch(/color:\s*rgba\(180,\s*200,\s*230/);
  });

  test('.frag-name matches the Agents page primary-label size (--fs-lg, not --fs-md)', () => {
    // Anchored to a line starting with exactly ".frag-name {" (2-space
    // indent) so the compound selector ".frag-row-system .frag-name { ... }"
    // (a different, one-line rule earlier in the file) is never matched.
    const rule = AGENT_TEMPLATES_PAGE.match(/\n  \.frag-name \{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.frag-name rule').not.toBe('');
    expect(rule).toMatch(/font-size:\s*var\(--fs-lg\)/);
  });

  test('.frag-desc, .filter-label/.filter-btn/.filter-hint, .form-row span/.form-readonly, and .muted all resolve through --c-muted', () => {
    for (const selector of [
      '.filter-label',
      '.filter-btn',
      '.filter-hint',
      '.frag-desc',
      '.form-row span',
      '.form-readonly',
      '.muted',
    ]) {
      const escaped = selector.replace(/[.\s]/g, (m) => (m === ' ' ? '\\s+' : '\\.'));
      const rx = new RegExp(`${escaped}\\s*\\{[\\s\\S]*?\\n  \\}`);
      const rule = AGENT_TEMPLATES_PAGE.match(rx)?.[0] ?? '';
      expect(rule, `${selector} rule should exist`).not.toBe('');
      expect(rule, `${selector} should use var(--c-muted)`).toMatch(/color:\s*var\(--c-muted\)/);
    }
  });
});

test.describe('LogPanel message text size (2026-10 fix)', () => {
  test('.log-row-msg uses --fs-lg, not the oversized --fs-base', () => {
    const rule = LOG_PANEL.match(/:global\(\.log-panel\.log-rows \.log-row-msg\)\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.log-row-msg base rule').not.toBe('');
    expect(rule).toMatch(/font-size:\s*var\(--fs-lg\)/);
    expect(rule).not.toMatch(/--fs-base/);
  });
});
