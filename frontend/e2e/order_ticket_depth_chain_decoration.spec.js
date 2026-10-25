/**
 * order_ticket_depth_chain_decoration.spec.js
 *
 * Operator: "apply background and border decoration of chain to order
 * ticket depth" — then simplified mid-task to "just subtle border and
 * background. the last row with buy levels text can have a different
 * background", and "it should be subtle in sync with chain" (reuse
 * Chain's own literal alpha values, not new numbers picked by eye).
 *
 * Source-grep only, matching this codebase's established convention
 * for pinning exact CSS decoration values (see
 * chain_atm_softening_and_leg_badge.spec.js) — these are static CSS
 * rules, not data-dependent rendering, so a live-server computed-style
 * probe would add flakiness (market hours / broker depth availability)
 * for no additional coverage over a direct source assertion.
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'fs';
import path from 'path';

const DEPTH_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OrderDepth.svelte',
);
const CHAIN_TAB_PATH = path.resolve(
  import.meta.dirname ?? new URL('.', import.meta.url).pathname,
  '../src/lib/order/OptionChainTab.svelte',
);

test.describe('OrderDepth — row divider + wash reuse Chain\'s own literal alpha values', () => {
  test('Chain\'s row-divider alpha (0.025) is a real, current value in OptionChainTab.svelte', () => {
    // Guards the premise: if Chain's own divider alpha ever changes,
    // this test (and the depth ladder's copy below) should be revisited
    // in lockstep.
    const chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
    expect(chainSrc).toMatch(/\.chain-row > td \{[^}]*border-bottom: 1px solid rgba\(255,255,255,0\.025\)/);
  });

  test('.ot-depth-cell carries a border-bottom divider at Chain\'s exact 0.025 alpha', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-cell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/border-bottom:\s*1px solid rgba\(255,255,255,0\.025\)/);
  });

  test('.ot-depth-cell carries a subtle background wash — neutral, not a per-side ITM/OTM color mapping', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-cell\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/background-color:\s*rgba\(255,255,255,0\.03\)/);
    // The operator explicitly rejected the earlier elaborate per-side
    // bid=green/ask=orange wash design — confirm no colored (non-white)
    // background was reintroduced on the base cell rule.
    expect(rule).not.toMatch(/background-color:\s*rgba\(74,222,128/);
    expect(rule).not.toMatch(/background-color:\s*rgba\(251,146,60/);
    expect(rule).not.toMatch(/background-color:\s*rgba\(248,113,113/);
  });

  test('the final depth row drops the divider, mirroring Chain\'s .chain-row:last-child > td { border-bottom: 0 }', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).toMatch(/class:ot-depth-cell-last-row=\{isLastRow\}/);
    const rule = src.match(/\.ot-depth-cell-last-row\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/border-bottom:\s*none/);
  });

  test('isLastRow is computed from the real row index, not hardcoded', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).toMatch(/\{@const isLastRow = i === buyRows\.length - 1\}/);
  });
});

test.describe('OrderDepth — "the last row with buy levels text" gets a different background', () => {
  test('the rendered "Buy levels" label sits inside .ot-depth-diag — confirms this is the targeted row', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    // The literal rendered span, not a comment mentioning the same words.
    const labelIdx = src.indexOf('<span class="ot-depth-diag-lbl">Buy levels</span>');
    expect(labelIdx).toBeGreaterThan(-1);
    const diagOpenIdx = src.indexOf('<div class="ot-depth-diag"');
    const diagCloseIdx = src.indexOf('</div>', diagOpenIdx);
    expect(diagOpenIdx).toBeGreaterThan(-1);
    expect(labelIdx).toBeGreaterThan(diagOpenIdx);
    expect(labelIdx).toBeLessThan(diagCloseIdx);
    // And it must NOT sit inside the bid/ask grid above (.ot-depth-grid).
    const gridOpenIdx = src.indexOf('<div class="ot-depth-grid">');
    const gridCloseIdx = src.indexOf('</div>', src.indexOf('{/each}', gridOpenIdx));
    expect(labelIdx).toBeGreaterThan(gridCloseIdx);
  });

  test('.ot-depth-diag background reuses Chain\'s ATM-row literal alpha (0.05), distinct from the 0.03 data-row wash', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    const rule = src.match(/\.ot-depth-diag\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/background-color:\s*rgba\(255,255,255,0\.05\)/);
    // Must genuinely differ from the base row wash, not accidentally
    // match it.
    expect(rule).not.toMatch(/background-color:\s*rgba\(255,255,255,0\.03\)/);
  });

  test('Chain\'s own ATM-row shade alpha (0.05) is a real, current value — the depth ladder\'s copy stays in sync', () => {
    const chainSrc = readFileSync(CHAIN_TAB_PATH, 'utf8');
    const rule = chainSrc.match(/\.chain-row-atm\s*>\s*td\s*\{[^}]*\}/)?.[0] ?? '';
    expect(rule).toMatch(/background-image:\s*linear-gradient\(rgba\(255,255,255,0\.05\),\s*rgba\(255,255,255,0\.05\)\)/);
  });
});

test.describe('Stale-code: no residual elaborate per-side / best-row-marker design', () => {
  // The operator's first brief asked for Chain's full ITM/OTM-style
  // wash mapping (bid=green/ask=orange) plus an ATM-style box-shadow
  // "best row" marker on row 0. That was explicitly simplified away
  // mid-task — confirm none of the scaffolding for the elaborate
  // version survived the simplification.
  test('no .ot-depth-row-bg scaffolding (the dropped bid/ask-half background div design) remains', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).not.toContain('ot-depth-row-bg');
  });

  test('no best-row box-shadow marker remains on the data rows', () => {
    const src = readFileSync(DEPTH_PATH, 'utf8');
    expect(src).not.toContain('ot-depth-row-bg-best');
    expect(src).not.toMatch(/box-shadow:\s*inset 0 1px 0 rgba\(251,191,36/);
  });
});
