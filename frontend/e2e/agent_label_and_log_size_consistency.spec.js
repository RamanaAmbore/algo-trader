/**
 * agent_label_and_log_size_consistency.spec.js
 *
 * Source-scan regression guard for a 2026-10 operator-reported
 * consistency fix (no browser needed — same pattern as
 * algo_consistency.spec.js's literal-grep tests):
 *
 *   1. (2026-10-17) The Agents page's collapsed row used to show a
 *      `{agent.long_name}` label ("when: ... — do: ...") directly under
 *      the agent name — operator: "I just want to see name and
 *      description which is already displayed... it is cluttering the
 *      card." The `{agent.long_name}` span was removed from the
 *      collapsed row header entirely; the `long_name` field itself is
 *      untouched (still used by the Edit form / decompile flow). This
 *      test now guards against reintroducing it there.
 *   2. Agent Templates page had hardcoded off-palette
 *      `rgba(180,200,230,α)` literals with inconsistent alpha across
 *      sites for name/description text — meaning the two pages
 *      rendered visibly different colors for the same semantic role.
 *      Now uses the shared `--c-muted` token.
 *   3. Agent Templates' `.frag-name` was `--fs-md` (0.65rem) while the
 *      Agents page's comparable primary label was `--fs-lg` (0.72rem) —
 *      now both `--fs-lg`.
 *   4. LogPanel's shared `.log-row-msg` rule was `--fs-base` (14px), a
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
const LOG_PANEL = fs.readFileSync(path.join('src/lib', 'LogPanel.svelte'), 'utf-8');

test.describe('Agent label color/size consistency (2026-10 fix)', () => {
  test('Agents page collapsed row no longer shows the long_name ("when:...do:...") label — declutter fix', () => {
    expect(AGENTS_PAGE).not.toMatch(/\{row\.long_name\}/);
    expect(AGENTS_PAGE).not.toMatch(/\{agent\.long_name\}/);
  });

  // Agent Templates' own page (and its .frag-name/.frag-desc/.filter-*
  // palette assertions) was folded into the Automation page's in-page tabs
  // in e1d1a4e1 (Sprint 9) and no longer exists, so those assertions were
  // removed rather than retargeted: there is no templates-only stylesheet
  // left to guard.
});

test.describe('LogPanel message text size (2026-10 fix)', () => {
  test('.log-row-msg uses --fs-lg, not the oversized --fs-base', () => {
    const rule = LOG_PANEL.match(/:global\(\.log-panel\.log-rows \.log-row-msg\)\s*\{[\s\S]*?\n  \}/)?.[0] ?? '';
    expect(rule, '.log-row-msg base rule').not.toBe('');
    expect(rule).toMatch(/font-size:\s*var\(--fs-lg\)/);
    expect(rule).not.toMatch(/--fs-base/);
  });
});
