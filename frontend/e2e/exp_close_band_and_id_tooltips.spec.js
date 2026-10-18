/**
 * exp_close_band_and_id_tooltips.spec.js
 *
 * 2026-10-17 feature — operator: "the only thing in exp close to add is
 * making c1, c2 labels info tips like p1, p2, etc. Also the labels
 * netted, to be closed text to act as tooltips" + "the info is
 * different for mcx and non-mcx symbols."
 *
 * CandidateLegRow.svelte's Exp Close-only chips were plain, non-
 * interactive text (`.expiry-band-label`, `.expiry-id-chip`) with only a
 * native hover `title=`. Per the house tooltip convention (a plain-text
 * label that isn't otherwise interactive becomes the click target
 * itself — no separate "(i)" icon), both are now clickable buttons that
 * open an InfoHint popup. Content must differ for MCX (commodity) vs
 * non-MCX (equity) rows, since the underlying risk differs (physical
 * delivery vs unhedged-residual settlement) — `_bandInfoText` branches
 * on `c._segment`, and the id chip's `_closeIdInfoText` reuses `c._reason`
 * (already segment-aware from derivativesMath.js's computeExpiryBands).
 *
 * A static source check — same rationale as exp_close_no_pair_badge.spec.js
 * and the existing derivatives_expiry_bands.spec.js SSOT checks: the real
 * band classification (ITM/OTM/netted) depends on live spot resolution
 * that's impractical to mock deterministically for a live-render assertion.
 *
 * Run:
 *   npx playwright test e2e/exp_close_band_and_id_tooltips.spec.js
 */

import { test, expect } from '@playwright/test';
import * as fs from 'fs';
import * as path from 'path';

const SRC_PATH = path.resolve(
  process.cwd(),
  'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte'
);

function readSrc() {
  return fs.readFileSync(SRC_PATH, 'utf8');
}

test('expiry-band-label is a clickable button wired to an InfoHint popup', () => {
  const src = readSrc();
  const btnStart = src.indexOf('class="expiry-band-label"');
  expect(btnStart, '.expiry-band-label markup must exist').toBeGreaterThan(0);
  const tagStart = src.lastIndexOf('<button', btnStart);
  expect(tagStart, '.expiry-band-label must be a <button>').toBeGreaterThan(0);
  const block = src.slice(tagStart, btnStart + 400);
  expect(block.includes('_bandInfoOpen = !_bandInfoOpen')).toBe(true);
  expect(block.includes('bind:this={_bandInfoAnchor}')).toBe(true);
  expect(block.includes('stopPropagation')).toBe(true);

  // The InfoHint mount must sit right after, bound to the same anchor/open pair.
  const mountIdx = src.indexOf('<InfoHint', btnStart);
  expect(mountIdx, 'InfoHint must be mounted for the band label').toBeGreaterThan(btnStart);
  const mount = src.slice(mountIdx, mountIdx + 300);
  expect(mount.includes('anchor={_bandInfoAnchor}')).toBe(true);
  expect(mount.includes('bind:open={_bandInfoOpen}')).toBe(true);
  expect(mount.includes('text={_bandInfoText}')).toBe(true);
});

test('_bandInfoText branches on c._segment === "commodity" for close and otm bands (MCX vs non-MCX wording differs)', () => {
  const src = readSrc();
  const start = src.indexOf('const _bandInfoText = $derived.by');
  expect(start, '_bandInfoText must exist').toBeGreaterThan(0);
  const end = src.indexOf('});', start);
  const block = src.slice(start, end);

  expect(block.includes("c._segment === 'commodity'")).toBe(true);
  // close band: distinct commodity vs equity copy
  expect(block).toMatch(/Unhedged ITM commodity/);
  expect(block).toMatch(/physical settlement risk/i);
  // otm band: distinct commodity vs equity copy
  expect(block).toMatch(/Commodity option is out-of-the-money/);
  expect(block).toMatch(/Equity option is out-of-the-money/);
  // netted is MCX-only (equity never nets) — single generic MCX copy, no equity branch needed
  expect(block).toMatch(/MCX ITM positions/);
});

test('expiry-id-chip (C1/C2/N1/N2) is a clickable button wired to an InfoHint popup carrying the row\'s own (segment-aware) reason', () => {
  const src = readSrc();
  const closeBtnStart = src.indexOf('class="expiry-id-chip expiry-id-close"');
  const nettedBtnStart = src.indexOf('class="expiry-id-chip expiry-id-netted"');
  expect(closeBtnStart, 'close-band id chip must exist').toBeGreaterThan(0);
  expect(nettedBtnStart, 'netted-band id chip must exist').toBeGreaterThan(0);

  const closeTag = src.lastIndexOf('<button', closeBtnStart);
  const closeBlock = src.slice(closeTag, closeBtnStart + 400);
  expect(closeBlock.includes('_closeIdInfoOpen = !_closeIdInfoOpen')).toBe(true);
  expect(closeBlock.includes('bind:this={_closeIdInfoAnchor}')).toBe(true);
  expect(closeBlock.includes('stopPropagation')).toBe(true);

  const infoTextStart = src.indexOf('const _closeIdInfoText = $derived.by');
  expect(infoTextStart, '_closeIdInfoText must exist').toBeGreaterThan(0);
  const infoTextEnd = src.indexOf('});', infoTextStart);
  const infoTextBlock = src.slice(infoTextStart, infoTextEnd);
  // Reuses c._reason — already segment-aware (equity vs commodity wording)
  // from derivativesMath.js's computeExpiryBands, no duplicated logic here.
  expect(infoTextBlock.includes('c._reason')).toBe(true);

  const mountIdx = src.indexOf('<InfoHint', nettedBtnStart);
  expect(mountIdx, 'InfoHint must be mounted for the id chip').toBeGreaterThan(nettedBtnStart);
  const mount = src.slice(mountIdx, mountIdx + 300);
  expect(mount.includes('anchor={_closeIdInfoAnchor}')).toBe(true);
  expect(mount.includes('bind:open={_closeIdInfoOpen}')).toBe(true);
  expect(mount.includes('text={_closeIdInfoText}')).toBe(true);
});

test('derivativesMath.js: the id chip\'s underlying _reason text already differs for equity vs commodity close-band rows', () => {
  const mathPath = path.resolve(process.cwd(), 'src/lib/data/derivativesMath.js');
  const src = fs.readFileSync(mathPath, 'utf8');
  expect(src).toMatch(/ITM equity — physical settlement risk/);
  expect(src).toMatch(/Unhedged ITM commodity \(residual qty/);
});
