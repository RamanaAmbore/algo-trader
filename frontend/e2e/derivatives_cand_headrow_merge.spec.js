/**
 * derivatives_cand_headrow_merge.spec.js
 *
 * Wave 2c consistency-fix pass (follow-up to the read-only audit,
 * operator approved "go ahead"). Two unrelated fixes, both scoped to
 * `/admin/derivatives`'s `+page.svelte` + `CandidateLegRow.svelte`:
 *
 * Fix 1 — `.cand-headrow` was declared as TWO separate rule blocks
 * with a conflicting `font-size` (one `var(--fs-sm)`, one
 * `var(--fs-md)`). Equal-specificity plain-class selectors mean only
 * the LAST declaration in source order ever won the cascade — verified
 * empirically (see method note below) that the live-rendered value was
 * `var(--fs-md)` (0.65rem = 10.4px at the default 16px root). Fixed by
 * consolidating into ONE `.cand-headrow` rule, keeping every other
 * property from both blocks and dropping only the losing `var(--fs-sm)`
 * declaration. `.cand-headrow > .num` is a SEPARATE, non-conflicting
 * duplicate (overflow/truncation props in one block, text-align/
 * justify-self in the other) — intentionally left alone, out of scope.
 *
 * Verification method for the pre-fix winner: compiled the two
 * `.cand-headrow {...}` blocks' raw text (extracted from source, in
 * source order) into a standalone HTML fixture with `--fs-sm`/`--fs-md`
 * defined to their real app.css values (0.6rem / 0.65rem respectively),
 * loaded via Playwright `page.setContent`, and read
 * `getComputedStyle(...).fontSize` — returned 10.4px, confirming
 * `var(--fs-md)` was the live winner. (A login-gated live read against
 * the real running app would hit the same DOM node; this fixture
 * approach needs no auth and isolates the exact cascade question.)
 *
 * Fix 2 — the Snapshot grid's underlying chip (`.byund-und`) used
 * `acctColor(g.underlying) + '1a'` as its background — running a
 * SYMBOL string through the function meant to assign colours to
 * ACCOUNT codes (`$lib/account.js`). An underlying could coincidentally
 * land on the exact same hue as some unrelated operator account,
 * implying a false "this is account X" meaning. No dedicated per-symbol
 * colour utility exists elsewhere in this codebase (checked
 * OptionsPayoff.svelte, pulseColumns.js). Fixed by dropping per-symbol
 * colouring entirely in favour of a flat neutral tint
 * (`rgba(126,151,184,0.10)`, the same `--sep-color` RGB triplet already
 * used for "informational, not alarming" chips elsewhere in this file,
 * e.g. `.cand-hidden-hint`) — structurally can never alias with the
 * account/direction/action colour spaces. `acctColor` import removed
 * from `+page.svelte` (now unused there); `CandidateLegRow.svelte`'s
 * own legitimate per-ACCOUNT `acctColor` usage is untouched.
 */

import { test, expect } from '@playwright/test';
import { chromium } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const readFile = (relPath) => {
  const abs = path.resolve(__dirname, '..', relPath);
  return readFileSync(abs, 'utf-8');
};

const PAGE_PATH = 'src/routes/(algo)/admin/derivatives/+page.svelte';
const page_src = readFile(PAGE_PATH);

test.describe('Fix 1 — .cand-headrow declared exactly once, conflicting font-size resolved', () => {
  test('no duplicate `.cand-headrow {` selector block remains', () => {
    // Deliberately a plain-text selector match (not `.cand-headrow > .num`,
    // which is a different selector and legitimately still duplicated —
    // out of scope per the task).
    const openings = page_src.match(/(^|\n)\s*\.cand-headrow\s*\{/g) ?? [];
    expect(openings.length, '.cand-headrow { opening count').toBe(1);
  });

  test('consolidated rule keeps the live-rendered winner (var(--fs-md)) and drops the loser (var(--fs-sm))', () => {
    const m = page_src.match(/\n\s*\.cand-headrow\s*\{([\s\S]*?)\n\s*\}/);
    expect(m, '.cand-headrow rule body').toBeTruthy();
    const body = m[1];
    expect(body).toContain('font-size: var(--fs-md)');
    expect(body).not.toContain('var(--fs-sm)');
  });

  test('merge lost nothing — every property from both original blocks survives in the single rule', () => {
    const m = page_src.match(/\n\s*\.cand-headrow\s*\{([\s\S]*?)\n\s*\}/);
    const body = m[1];
    // From the original layout block
    expect(body).toMatch(/display:\s*grid/);
    expect(body).toMatch(/grid-template-columns:\s*subgrid/);
    expect(body).toMatch(/font-family:\s*monospace/);
    expect(body).toMatch(/font-variant-numeric:\s*tabular-nums/);
    // From the original typography/sticky block
    expect(body).toMatch(/font-weight:\s*800/);
    expect(body).toMatch(/text-transform:\s*uppercase/);
    expect(body).toMatch(/position:\s*sticky/);
    expect(body).toMatch(/#1d2a44/);
  });

  test('padding shorthand/override merge trap avoided — shorthand precedes the bottom-only override, not the other way round', () => {
    const m = page_src.match(/\n\s*\.cand-headrow\s*\{([\s\S]*?)\n\s*\}/);
    const body = m[1];
    // The original two blocks had `padding: 0.1rem 0.2rem;` (block 1)
    // then `padding-bottom: 0.15rem;` (block 2). Merged into a single
    // three-value shorthand so bottom padding isn't silently lost by
    // appearing before a later shorthand that would re-set it.
    expect(body).toMatch(/padding:\s*0\.1rem\s+0\.2rem\s+0\.15rem/);
    expect(body).not.toMatch(/padding-bottom:/);
  });

  test('empirical cascade check — isolated fixture confirms var(--fs-md) (0.65rem) wins, not var(--fs-sm)', async () => {
    // This check is intentionally decoupled from the merge above: it
    // re-derives the winner from whatever `.cand-headrow` text is
    // currently in source (works whether it's one merged block or two),
    // so it keeps acting as a real regression guard even if someone
    // reintroduces a second conflicting block later.
    const blocks = page_src.match(/\.cand-headrow\s*\{[^}]*\}/g) ?? [];
    expect(blocks.length).toBeGreaterThan(0);
    const css = blocks.join('\n');
    const html = `<!doctype html><html><head><style>
      :root { --fs-sm: 0.6rem; --fs-md: 0.65rem; }
      ${css}
    </style></head><body><div class="cand-headrow">hi</div></body></html>`;

    const browser = await chromium.launch();
    try {
      const pw = await browser.newPage();
      await pw.setContent(html);
      const fontSize = await pw.evaluate(
        () => getComputedStyle(document.querySelector('.cand-headrow')).fontSize
      );
      expect(fontSize).toBe('10.4px'); // var(--fs-md) = 0.65rem at 16px root
    } finally {
      await browser.close();
    }
  });
});

test.describe('Fix 2 — underlying chip no longer borrows the account colour palette', () => {
  test('`.byund-und` markup no longer calls acctColor()', () => {
    const m = page_src.match(/<span class="byund-und"[^>]*>\{g\.underlying\}<\/span>/);
    expect(m, 'byund-und underlying chip markup').toBeTruthy();
    expect(m[0]).not.toContain('acctColor');
    expect(m[0]).not.toContain('style=');
  });

  test('+page.svelte no longer imports acctColor (it was the only consumer in this file)', () => {
    expect(page_src).not.toMatch(/import\s*\{\s*acctColor\s*\}\s*from\s*['"]\$lib\/account['"]/);
  });

  test('`.byund-und` CSS rule carries the flat neutral tint, not a per-symbol hash/palette', () => {
    const m = page_src.match(/\.byund-und\s*\{([\s\S]*?)\n\s*\}/);
    expect(m, '.byund-und rule body').toBeTruthy();
    const body = m[1];
    expect(body).toContain('background: rgba(126,151,184,0.10)');
  });

  test('CandidateLegRow.svelte keeps its own legitimate per-ACCOUNT acctColor usage untouched', () => {
    const legRowSrc = readFile(
      'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte'
    );
    expect(legRowSrc).toMatch(/import\s*\{\s*acctColor\s*\}\s*from\s*['"]\$lib\/account['"]/);
    expect(legRowSrc).toMatch(/acctColor\(c\.account\)/);
  });

  test('the neutral tint cannot alias any account colour — it is not drawn from ACCT_PALETTE', () => {
    const accountSrc = readFile('src/lib/account.js');
    const paletteMatch = accountSrc.match(/export const ACCT_PALETTE = \[([\s\S]*?)\]/);
    expect(paletteMatch, 'ACCT_PALETTE').toBeTruthy();
    // The neutral tint's own hue (126,151,184) must not appear as one of
    // the account palette's hex entries' RGB triplet.
    expect(paletteMatch[1]).not.toContain('126,151,184');
    expect(paletteMatch[1].toLowerCase()).not.toContain('#7e97b8'); // --algo-muted hex form, nearest visual neighbour
  });
});
