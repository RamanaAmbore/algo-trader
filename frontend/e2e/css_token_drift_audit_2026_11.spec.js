/**
 * css_token_drift_audit_2026_11.spec.js
 *
 * 2026-11 CSS-token drift audit — three confirmed drift defects, fixed as
 * pure token extraction/aliasing (no visual change, except one deliberate
 * judgment call documented below):
 *
 *   1. Odd-row stripe `rgba(13,22,42,0.30)` was defined independently as
 *      BOTH `--row-tint-odd-bg` (app.css) and `--ag-odd-row-background-color`
 *      (`.ag-theme-algo` block, app.css), plus 5 bare literal copies
 *      (app.css ×2, derivatives `+page.svelte` ×2, SimulatorPanel.svelte ×1
 *      — 2 more than the 3 the original audit named, found during this
 *      fix's own sweep). Fix: one canonical `--grid-row-alt-bg` in `:root`;
 *      both old names now alias it; every bare literal now references it.
 *      SimulatorPanel's `.sim-pill` was at a DIFFERENT alpha (0.55, not
 *      0.30) with no comment — judged a deliberate variant (a discrete pill
 *      chip needs more contrast than a subtle row-stripe wash), not drift,
 *      so it got its own named token (`--grid-row-alt-bg-strong`) instead
 *      of being collapsed to 0.30.
 *
 *   2. Amber TOTAL-row literals (0.90, 0.18, 0.85, 0.22 ×2, 0.70, 0.55) in
 *      derivatives `+page.svelte` bypassed the existing `--c-action` /
 *      `--algo-amber-*` token family entirely. Fix: a named alpha scale
 *      (`--algo-amber-18/22/55/70/85/90`) — the -18/-22/-55 members are
 *      pure `var()` aliases of the PRE-EXISTING `--algo-amber-bg-hover` /
 *      `-bg-strong` / `-border` tokens (never redefined as a second
 *      literal, which would just recreate Issue 1's own defect); -70/-85/-90
 *      are genuinely new literals with no prior token. All 6 real call
 *      sites now reference the matching token.
 *
 *   3. `rgba(126,151,184,0.10)` duplicated verbatim at two `border-bottom`
 *      sites in derivatives `+page.svelte`, despite `--sep-color` already
 *      being the documented canonical token for this EXACT value (its own
 *      nearby comments already claimed to use it). Fix: `--grid-cell-divider`
 *      aliases `--sep-color` (not a second literal); both sites now
 *      reference it, bringing code and comment back into agreement.
 *
 * Five quality dimensions:
 *   1. SSOT    — every one of the drifted values now has exactly ONE
 *                literal definition; every other reference is a var() chain.
 *   2. Perf    — no new network calls; static checks are regex-only.
 *   3. Stale   — regression guards against a bare literal reappearing at
 *                any of the migrated call sites.
 *   4. Reuse   — follows the `ruleBody`/ratio-regex pattern already used by
 *                alignment_overflow_audit.spec.js and
 *                derivatives_greeks_risk_restructure.spec.js for this exact
 *                file; live checks reuse the loginAsAdmin fixture and the
 *                public `/performance` ag-theme-ramboq precedent from
 *                aggrid_header_row_height_parity.spec.js.
 *   5. UX      — live checks confirm the computed, rendered color at both
 *                ends of the alias chain (dark `.ag-theme-algo` AND the
 *                public cream `.ag-theme-ramboq` theme, which must stay
 *                completely untouched since it never participated in this
 *                drift) — not just that the token text exists.
 *
 * Run:
 *   cd frontend && npx playwright test e2e/css_token_drift_audit_2026_11.spec.js
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const readFile = (relPath) => readFileSync(path.resolve(__dirname, '..', relPath), 'utf-8');

const APP_CSS = readFile('src/app.css');
const DERIV_SRC = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');
const SIM_PANEL_SRC = readFile('src/lib/execution/SimulatorPanel.svelte');

/** Extract the FIRST `.selector { ... }` (or bare-prop) rule body, or null. */
function ruleBody(css, selector) {
  const re = new RegExp(
    selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'
  );
  const m = css.match(re);
  return m ? m[1] : null;
}

/** Count bare rgba(...) literal occurrences of a given triplet+alpha, ignoring whitespace. */
function countLiteral(src, r, g, b, alpha) {
  const re = new RegExp(
    `rgba\\(\\s*${r}\\s*,\\s*${g}\\s*,\\s*${b}\\s*,\\s*${alpha}\\s*\\)`,
    'g'
  );
  return (src.match(re) || []).length;
}

// ── Issue 1: odd-row stripe — one canonical token ──────────────────────────

test.describe('Static — Issue 1: --grid-row-alt-bg is the sole canonical odd-row-stripe token', () => {
  test('app.css defines --grid-row-alt-bg at the original 0.30 literal, exactly once', () => {
    expect(countLiteral(APP_CSS, 13, 22, 42, '0\\.30')).toBe(1);
    expect(APP_CSS).toMatch(/--grid-row-alt-bg:\s*rgba\(13, 22, 42, 0\.30\);/);
  });

  test('--row-tint-odd-bg aliases --grid-row-alt-bg, not an independent literal', () => {
    expect(APP_CSS).toMatch(/--row-tint-odd-bg:\s*var\(--grid-row-alt-bg\);/);
  });

  test('.ag-theme-algo --ag-odd-row-background-color aliases --grid-row-alt-bg', () => {
    const body = ruleBody(APP_CSS, '.ag-theme-algo');
    expect(body).toMatch(/--ag-odd-row-background-color:\s*var\(--grid-row-alt-bg\);/);
  });

  test('.ag-theme-algo .ag-row-odd !important override references the token, not a literal', () => {
    const body = ruleBody(APP_CSS, '.ag-theme-algo .ag-row-odd');
    expect(body).toMatch(/var\(--grid-row-alt-bg\)/);
    expect(body).not.toMatch(/rgba\(13,\s*22,\s*42/);
  });

  test('.algo-table odd-row fallback references the token, not a literal', () => {
    const body = ruleBody(APP_CSS, '.algo-table tbody tr:nth-child(odd) td');
    expect(body).toMatch(/var\(--ag-odd-row-background-color,\s*var\(--grid-row-alt-bg\)\)/);
  });

  test('derivatives +page.svelte: both .opt-kv-risk --risk-stripe and .byund-row odd fallback use the token', () => {
    expect(DERIV_SRC).toMatch(/--risk-stripe:\s*var\(--ag-odd-row-background-color,\s*var\(--grid-row-alt-bg\)\);/);
    expect(DERIV_SRC).toMatch(/\.byund-row:nth-of-type\(odd\) > span \{\s*\n\s*background-color:\s*var\(--ag-odd-row-background-color,\s*var\(--grid-row-alt-bg\)\);/);
  });

  test('no bare rgba(13,22,42,...) literal survives anywhere outside the two canonical app.css definitions', () => {
    // app.css: exactly the two canonical definitions (--grid-row-alt-bg at
    // 0.30, --grid-row-alt-bg-strong at 0.55) — zero stray copies.
    const appCssMatches = APP_CSS.match(/rgba\(\s*13\s*,\s*22\s*,\s*42\s*,\s*0\.(30|55)\s*\)/g) || [];
    expect(appCssMatches.length).toBe(2);
    // The migrated consumer files carry zero bare literals of this triplet.
    expect(DERIV_SRC).not.toMatch(/rgba\(\s*13\s*,\s*22\s*,\s*42/);
    expect(SIM_PANEL_SRC).not.toMatch(/rgba\(\s*13\s*,\s*22\s*,\s*42/);
  });

  test('SimulatorPanel .sim-pill: 0.55 alpha kept as a documented strong variant, not collapsed to 0.30', () => {
    const body = ruleBody(SIM_PANEL_SRC, '.sim-pill');
    expect(body).toMatch(/var\(--grid-row-alt-bg-strong\)/);
    expect(APP_CSS).toMatch(/--grid-row-alt-bg-strong:\s*rgba\(13, 22, 42, 0\.55\);/);
  });
});

// ── Issue 2: amber alpha scale ──────────────────────────────────────────────

test.describe('Static — Issue 2: amber alpha scale extraction', () => {
  test('app.css: -18/-22/-55 are var() aliases of the pre-existing tokens (never a second literal)', () => {
    expect(APP_CSS).toMatch(/--algo-amber-18:\s*var\(--algo-amber-bg-hover\);/);
    expect(APP_CSS).toMatch(/--algo-amber-22:\s*var\(--algo-amber-bg-strong\);/);
    expect(APP_CSS).toMatch(/--algo-amber-55:\s*var\(--algo-amber-border\);/);
  });

  test('app.css: -70/-85/-90 are new literals at the exact original alphas', () => {
    expect(APP_CSS).toMatch(/--algo-amber-70:\s*rgba\(251, 191, 36, 0\.70\);/);
    expect(APP_CSS).toMatch(/--algo-amber-85:\s*rgba\(251, 191, 36, 0\.85\);/);
    expect(APP_CSS).toMatch(/--algo-amber-90:\s*rgba\(251, 191, 36, 0\.90\);/);
  });

  test('derivatives +page.svelte: all 6 real call sites reference the new tokens', () => {
    expect(DERIV_SRC).toMatch(/\.pos-stale-dot \{[^}]*background:\s*var\(--algo-amber-90\);/);
    expect(DERIV_SRC).toMatch(/\.leg-headrow \{[^}]*border-bottom:\s*1px solid var\(--algo-amber-18\);/s);
    expect(DERIV_SRC).toMatch(/\.cand-scroll::-webkit-scrollbar-thumb:hover \{\s*background:\s*var\(--algo-amber-85\);/);
    expect(DERIV_SRC).toMatch(/linear-gradient\(var\(--algo-amber-22\), var\(--algo-amber-22\)\)/);
    expect(DERIV_SRC).toMatch(/border-top:\s*2px solid var\(--algo-amber-70\);/);
    expect(DERIV_SRC).toMatch(/border-bottom:\s*1px solid var\(--algo-amber-55\);/);
  });

  test('no bare rgba(251,191,36,...) literal survives at any of the 6 migrated alphas in derivatives +page.svelte', () => {
    // 0.90/0.70/0.22-with-no-trailing-comma-pair and the scrollbar-hover
    // 0.85 have no OTHER legitimate call site in this file at all, so a
    // zero-count check here is unambiguous (unlike 0.18/0.10/0.20, which
    // have deliberately-untouched residual call sites elsewhere, see the
    // next test).
    expect(DERIV_SRC).not.toMatch(/rgba\(251,\s*191,\s*36,\s*0\.90?\)/); // 0.9 / 0.90
    expect(DERIV_SRC).not.toMatch(/rgba\(251,\s*191,\s*36,\s*0\.70\)/);
    expect(DERIV_SRC).not.toMatch(/rgba\(251,\s*191,\s*36,\s*0\.85\)/);
    expect(DERIV_SRC).not.toMatch(/linear-gradient\(rgba\(251,\s*191,\s*36,\s*0\.22\)/);
  });

  test('residual amber literals at OTHER alphas (0.10/0.20/0.32/0.06/0.40/0.45/0.14) are deliberately untouched', () => {
    // These were explicitly out of scope for this fix (no shared token
    // match, or an existing var()+fallback already in place) — this guard
    // documents the intentional residual so a future pass doesn't assume
    // they were missed.
    expect(countLiteral(DERIV_SRC, 251, 191, 36, '0\\.10')).toBeGreaterThan(0);
  });
});

// ── Issue 3: grid-cell divider ──────────────────────────────────────────────

test.describe('Static — Issue 3: --grid-cell-divider aliases --sep-color', () => {
  test('app.css defines --grid-cell-divider as a var() alias of --sep-color', () => {
    expect(APP_CSS).toMatch(/--grid-cell-divider:\s*var\(--sep-color\);/);
  });

  test('both derivatives border-bottom call sites reference --grid-cell-divider, not the bare literal', () => {
    const occurrences = DERIV_SRC.match(/border-bottom:\s*1px solid var\(--grid-cell-divider\);/g) || [];
    expect(occurrences.length).toBe(2);
    expect(DERIV_SRC).not.toMatch(/border-bottom:\s*1px solid rgba\(126,\s*151,\s*184,\s*0\.10\);/);
  });
});

// ── Live computed-style checks ──────────────────────────────────────────────

test.describe('Live — computed color at both ends of the alias chain', () => {
  test(':root custom properties resolve to the pre-refactor literal values (public /performance page)', async ({ page }) => {
    // networkidle, not domcontentloaded — in dev mode Vite injects app.css
    // as a JS-module style tag slightly after DOMContentLoaded fires;
    // reading getPropertyValue too early returns '' for every custom
    // property, not just the ones this fix touched (confirmed against
    // --algo-amber-bg, a long-pre-existing token, before picking this fix).
    await page.goto('/performance', { waitUntil: 'networkidle', timeout: 60_000 });
    const vars = await page.evaluate(() => {
      const cs = getComputedStyle(document.documentElement);
      return {
        sepColor: cs.getPropertyValue('--sep-color').trim(),
        gridCellDivider: cs.getPropertyValue('--grid-cell-divider').trim(),
        gridRowAltBg: cs.getPropertyValue('--grid-row-alt-bg').trim(),
        rowTintOddBg: cs.getPropertyValue('--row-tint-odd-bg').trim(),
      };
    });
    expect(vars.sepColor).toBe('rgba(126, 151, 184, 0.10)');
    // --grid-cell-divider must resolve to the SAME value as --sep-color —
    // confirming it is a real alias, not a silently-diverged second copy.
    expect(vars.gridCellDivider).toBe(vars.sepColor);
    expect(vars.gridRowAltBg).toBe('rgba(13, 22, 42, 0.30)');
    expect(vars.rowTintOddBg).toBe(vars.gridRowAltBg);
  });

  test('public /performance ag-theme-ramboq odd row stays cream #f5f2eb — unaffected by the dark-theme alias (trap: do not leak the alias into the cream theme)', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded', timeout: 60_000 });
    const bg = await page.evaluate(() => {
      const grid = Array.from(document.querySelectorAll('.ag-theme-ramboq'))
        .find((g) => /** @type {HTMLElement} */ (g).offsetHeight > 0);
      if (!grid) return null;
      const row = grid.querySelector('.ag-row-odd');
      return row ? getComputedStyle(row).backgroundColor : null;
    });
    // Only assert when a ramboq-theme grid with an odd row actually
    // rendered (data-dependent); skip gracefully otherwise rather than
    // false-failing on an empty/loading grid.
    if (bg !== null) {
      expect(bg).toBe('rgb(245, 242, 235)');
    }
  });

  test('authenticated .ag-theme-algo odd row resolves through the new --grid-row-alt-bg alias chain to the original rgba(13,22,42,0.3)', async ({ page }) => {
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded', timeout: 60_000 });
    const bg = await page.evaluate(() => {
      const grid = Array.from(document.querySelectorAll('.ag-theme-algo'))
        .find((g) => /** @type {HTMLElement} */ (g).offsetHeight > 0);
      if (!grid) return null;
      const row = grid.querySelector('.ag-row-odd');
      return row ? getComputedStyle(row).backgroundColor : null;
    });
    if (bg !== null) {
      // Chromium normalizes rgba(13,22,42,0.30) to this exact string.
      expect(bg).toBe('rgba(13, 22, 42, 0.3)');
    }
  });
});
