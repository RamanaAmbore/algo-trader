/**
 * exp_close_no_pair_badge.spec.js
 *
 * 2026-10-17 fix — operator: "presently exp close is also changed to
 * p1, p2, this has to be reverted needs to be taken to the state it has
 * c1, c2, the netting, in the money, out of money, mcx and non-mcx
 * functionality."
 *
 * CandidateLegRow.svelte is shared between the Legs tab and the Exp
 * Close tab. The St-column auto-pair badge (P1/P2/GTT/pinned/orphan,
 * from positionStateBadge.js) was rendering unconditionally in BOTH
 * tabs — but Exp Close already has its OWN, more specific identity
 * system for the same row: `.expiry-id-chip` showing C1/C2 (the
 * close-action sequence) or N1/N2 (netted-pair adjacency), tied to the
 * band classifier (ITM-on-expiry / NETTED / OTM, MCX vs non-MCX
 * grouping). Showing the unrelated P1/P2 auto-pair label in the same
 * row duplicated/conflicted with that system. Fix: suppress the
 * St-column badge specifically when `legsTab === 'expiry'` — Legs tab
 * is unaffected.
 *
 * A static source check (not a live-data browser render) — the actual
 * band classification (ITM/OTM/netted) depends on live spot resolution
 * that's impractical to fully mock deterministically; pinning the gate
 * in the component source is the stable, low-flake way to guard this
 * fix, matching this suite's own convention in derivatives_expiry_bands.spec.js.
 *
 * Run:
 *   npx playwright test e2e/exp_close_no_pair_badge.spec.js
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const SRC_PATH = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte'
);

test('CandidateLegRow: St-column badge content is gated on legsTab !== "expiry"', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');

  const cellStart = src.indexOf('<span class="cand-state-cell"');
  expect(cellStart, '.cand-state-cell markup must exist').toBeGreaterThan(0);
  const cellEnd = src.indexOf('</span>', cellStart);
  const cell = src.slice(cellStart, cellEnd + '</span>'.length);

  expect(
    cell.includes("legsTab !== 'expiry'"),
    '.cand-state-cell content must be gated on legsTab !== "expiry" so the ' +
    'auto-pair badge (P1/P2/GTT/pinned/orphan) never renders on Exp Close rows'
  ).toBe(true);

  // The background/color/border inline styles (which carry the badge's
  // tint even without the glyph/button) must ALSO be suppressed on Exp
  // Close — not just the glyph — otherwise an empty but colored cell
  // would still visually leak the auto-pair state.
  expect(
    cell.includes("legsTab === 'expiry' ? null"),
    '.cand-state-cell background/color/border must null out on Exp Close, ' +
    'not just hide the glyph/button'
  ).toBe(true);
});

test('CandidateLegRow: the expiry-id-chip (C1/C2/N1/N2) is UNAFFECTED by the badge-suppression fix', () => {
  const src = fs.readFileSync(SRC_PATH, 'utf8');
  expect(
    src.includes("legsTab === 'expiry' && c._band"),
    'the expiry-id-chip gate (C1/C2 close-id, N1/N2 netted pairId) must still exist unchanged'
  ).toBe(true);
  expect(src.includes('expiry-id-chip expiry-id-close')).toBe(true);
  expect(src.includes('expiry-id-chip expiry-id-netted')).toBe(true);
});
