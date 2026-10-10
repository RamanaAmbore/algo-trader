/**
 * commandLineTabAgentDispatch.test.js — Sprint 9 console dispatch layer.
 *
 * CommandLineTab.svelte has no existing component-mount harness in this
 * repo (documented in its own `onDestroy` comment, and in
 * orderSourceVocabulary.test.js's header) — this file follows that same
 * established source-audit pattern: import the raw `.svelte` text via
 * `?raw`, slice to the exact function, and assert on it directly, rather
 * than inventing a mount harness for one new branch.
 *
 * What this guards: `runRaw` (CommandLineTab's catch-all for any typed
 * line that isn't a recognized buy/sell/cancel/modify order) used to
 * fall through EVERYTHING else — including a WHEN/ALERT/DO agent
 * statement or a bare order(...)/place_order(...)/bracket(...) call — to
 * `POST /api/admin/exec`, i.e. ran it as a real shell command on the
 * server. The Sprint 9 fix intercepts agent-family text via
 * `classifyConsoleStatement` (consoleDispatch.js, covered directly in
 * consoleDispatch.test.js) BEFORE that shell branch and returns early.
 * This test asserts the intercept exists, fires on a real `return`
 * (never falls through), and is positioned ahead of the shell-exec call
 * in source order — so a future edit that reorders these two blocks
 * fails loudly instead of silently reopening the hazard.
 */

import { describe, it, expect } from 'vitest';
import COMMAND_LINE_SRC from '../order/CommandLineTab.svelte?raw';

describe('CommandLineTab.svelte — runRaw intercepts agent-family text before shell-exec', () => {
  const start = COMMAND_LINE_SRC.indexOf('async function runRaw');
  const end   = COMMAND_LINE_SRC.indexOf('function _buildOrderLeg', start);
  const block = COMMAND_LINE_SRC.slice(start, end);

  it('runRaw exists and is non-empty (sanity check on the slice bounds)', () => {
    expect(start).toBeGreaterThan(-1);
    expect(end).toBeGreaterThan(start);
  });

  it('imports classifyConsoleStatement from the dispatch module', () => {
    expect(COMMAND_LINE_SRC).toMatch(/import\s*\{\s*classifyConsoleStatement\s*\}\s*from\s*'\$lib\/command\/consoleDispatch'/);
  });

  it('runRaw checks onAgentStatement + classifyConsoleStatement(...) === \'agents\'', () => {
    expect(block).toMatch(/onAgentStatement\s*&&\s*classifyConsoleStatement\(raw\)\s*===\s*'agents'/);
  });

  it('the agent-family intercept returns early (never falls through to the agent/shell branches)', () => {
    const interceptIdx = block.search(/onAgentStatement\s*&&\s*classifyConsoleStatement/);
    expect(interceptIdx).toBeGreaterThan(-1);
    const afterIntercept = block.slice(interceptIdx, interceptIdx + 200);
    expect(afterIntercept).toMatch(/onAgentStatement\(raw\);[\s\S]*return;/);
  });

  it('the intercept sits BEFORE the shell-exec POST /api/admin/exec call', () => {
    const interceptIdx = block.search(/onAgentStatement\s*&&\s*classifyConsoleStatement/);
    const shellIdx = block.indexOf("fetch('/api/admin/exec'");
    expect(interceptIdx).toBeGreaterThan(-1);
    expect(shellIdx).toBeGreaterThan(-1);
    expect(interceptIdx).toBeLessThan(shellIdx);
  });

  it('the intercept sits BEFORE the "agent" verb interpret branch too', () => {
    const interceptIdx = block.search(/onAgentStatement\s*&&\s*classifyConsoleStatement/);
    const agentVerbIdx = block.indexOf("verb === 'agent'");
    expect(interceptIdx).toBeGreaterThan(-1);
    expect(agentVerbIdx).toBeGreaterThan(-1);
    expect(interceptIdx).toBeLessThan(agentVerbIdx);
  });
});
