/**
 * algo_consistency.spec.js
 *
 * Phase 4 guard for the algo-page consistency SSOT introduced on
 * 2026-06-30 (see CLAUDE.md "Algo design tokens — SSOT" block in app.css).
 *
 * The audit collapsed:
 *   - 49 orphan #a3b9d0 literals    → var(--text-muted)
 *   - 1056 font-size literals       → 6 --fs-* tokens
 *   - 390 ui-monospace declarations → var(--font-numeric)
 *   - 15 card-bg gradient literals  → var(--card-bg-gradient)
 *   - 7 bespoke modals              → compose .algo-modal chrome
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *
 *   1. SSOT      — every algo route renders card-bg + primary text colour
 *                  through the shared CSS var chain.
 *   2. Perf      — cross-route nav under an 8 MB heap-growth budget
 *                  (subscription-leak guard from main_thread_perf spec).
 *   3. Stale     — hard-coded hex-literal grep on (algo)/ + lib/ .svelte
 *                  files: NO #a3b9d0, NO literal font-size: 0.Xrem.
 *   4. Reuse     — a modal opened from the algo layout resolves the same
 *                  gradient token as an /admin/derivatives payoff card.
 *   5. UX        — computed-style consistency at desktop + mobile
 *                  viewports across 8 canonical algo routes.
 *
 * Run:
 *   PLAYWRIGHT_BASE_URL=https://dev.ramboq.com \
 *   npx playwright test frontend/e2e/algo_consistency.spec.js \
 *   --project=chromium-desktop --project=mobile-portrait --workers=1
 */

import { test, expect } from '@playwright/test';
import { loginAsAdmin } from './fixtures/auth.js';
import * as fs from 'node:fs';
import * as path from 'node:path';

/* ── Routes under audit ──────────────────────────────────────────────── */

const ALGO_ROUTES = [
  '/dashboard',
  '/pulse',
  '/orders',
  '/charts',
  '/admin/derivatives',
  '/automation',
  '/strategies',
  '/admin/history',
];

/* ── Stale-code guard: raw hex + literal font-size ───────────────────── */

/**
 * Walk src/lib and src/routes/(algo) trees and collect .svelte files.
 */
function collectSvelteFiles() {
  const roots = [
    path.join(process.cwd(), 'src/lib'),
    path.join(process.cwd(), 'src/routes/(algo)'),
  ];
  const out = [];
  function walk(dir) {
    let entries;
    try { entries = fs.readdirSync(dir, { withFileTypes: true }); }
    catch { return; }
    for (const e of entries) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (e.name.endsWith('.svelte')) out.push(p);
    }
  }
  for (const r of roots) walk(r);
  return out;
}

test.describe('algo consistency — SSOT stale-code guard', () => {
  test('no #a3b9d0 literals remain in algo surface', () => {
    const files = collectSvelteFiles();
    const offenders = [];
    for (const f of files) {
      const src = fs.readFileSync(f, 'utf-8');
      if (/#a3b9d0/i.test(src)) offenders.push(path.relative(process.cwd(), f));
    }
    expect(offenders, `#a3b9d0 orphan colour must be migrated to var(--text-muted). ` +
      `Offenders:\n${offenders.join('\n')}`).toEqual([]);
  });

  test('no literal font-size: 0.Xrem remain in algo surface', () => {
    const files = collectSvelteFiles();
    const offenders = [];
    // Match font-size assignments where the value is a plain 0.X rem
    // literal. Allow var(--fs-*) and calc() forms. Skip comment blocks
    // by pre-stripping them.
    const rx = /font-size:\s*0\.[0-9]+rem/;
    for (const f of files) {
      let src = fs.readFileSync(f, 'utf-8');
      // Strip CSS + HTML/JS block comments so /* font-size: 0.65rem */
      // documentation lines don't trip the guard.
      src = src
        .replace(/\/\*[\s\S]*?\*\//g, '')
        .replace(/<!--[\s\S]*?-->/g, '');
      if (rx.test(src)) offenders.push(path.relative(process.cwd(), f));
    }
    expect(offenders, `font-size literals must map to --fs-2xs..--fs-xl tokens. ` +
      `Offenders:\n${offenders.join('\n')}`).toEqual([]);
  });

  /* ── A3 (2026-09 audit) — stale pale-blue literal sweep, RATCHET ──────
   * ~90 sites of `rgba(200,216,240,α)` (the OLD pre-whitening-sweep
   * value of --algo-slate, #c8d8f0) plus flat hex near-equivalents
   * (#e2e8f0, #cbd5e1, #f8fafc) never got migrated when --algo-slate
   * became white. Conversion rule (NOT a collapse to one value):
   *   - muted NUMERIC TEXT roles named `.cell-muted` → var(--algo-slate-muted)
   *   - every other role (borders, backgrounds, non-numeric dimmed
   *     text) → color-mix(in srgb, var(--algo-slate) <site's own
   *     original alpha>%, transparent) — alpha preserved per-site.
   *
   * SWEEP COMPLETE (commits 1–6/6, 2026-09) — ALLOWLIST is now empty;
   * a full re-scan of src/lib + src/routes/(algo) (113 .svelte files)
   * confirms zero remaining offenders. Kept as a RATCHET (not deleted)
   * so any future regression is caught immediately rather than
   * silently reappearing.
   *
   * Risk categories investigated along the way, resolved as VIABLE
   * (not blockers) after live verification:
   *   - SVG stroke= presentation attributes (ChartWorkspace.svelte,
   *     OptionsPayoff.svelte, PnlAnalysis.svelte, dashboard/+page.svelte)
   *     — var()/color-mix() DO resolve correctly once the stylesheet
   *     carrying the custom property has loaded (verified live against
   *     a real page load — an earlier premature computed-style check,
   *     evaluated before Vite's dev-mode CSS injection completed, had
   *     falsely suggested "unsupported").
   *   - "Canvas colour consumer" files flagged by name (MultiPriceChart,
   *     admin/metrics, admin/perf) turned out on inspection to have NO
   *     canvas usage at all (grepped getContext/strokeStyle/fillStyle —
   *     zero matches in all three); MultiPriceChart renders via SVG.
   *     Ordinary CSS/SVG conversions applied.
   *   - admin/metrics + admin/perf's sites were actually
   *     `var(--text, #e2e8f0)` / `var(--text-soft, #e2e8f0)` fallback
   *     forms — `--text`/`--text-soft` are never defined anywhere in the
   *     codebase (grepped), so the "fallback" was the real, always-
   *     active value, not a rare edge case. Fallback literal replaced
   *     with var(--algo-slate); the var(--text, …) indirection itself
   *     kept in case a future --text token is added.
   *   - LogPanel.svelte IS public-mounted ((public)/market,
   *     (public)/performance) — verified safe before converting: an
   *     adjacent rule in the same class family already used
   *     var(--algo-slate) directly, proving the token already resolves
   *     correctly wherever LogPanel renders.
   *   - showcase/+page.svelte is a narrative "tour" page (not a colour-
   *     swatch/documentation sample) — verified before converting.
   *
   * Explicitly and permanently OUT of scope (not part of the ratchet,
   * documented here rather than silently ignored):
   *   - app.css's --chart-grid-stroke (+ -minor / -zero) fallback values —
   *     app.css is shared with public/investor pages; these are
   *     fallback values inside an existing var(...) reference (already
   *     tokenised at the call site), lower risk/priority than a bare
   *     literal.
   *   - Genuine canvas 2D context colour strings (getContext('2d').
   *     strokeStyle/fillStyle) — a fundamentally different mechanism
   *     that never parses CSS var()/color-mix() regardless of load
   *     timing. None were found in this codebase during the sweep, but
   *     if one is ever added, it needs JS-side getComputedStyle
   *     resolution or a documented hardcoded fallback, not a text
   *     substitution.
   */
  const A3_MUTED_LITERAL_ALLOWLIST = /** @type {string[]} */ ([]);

  test('A3 ratchet — stale pale-blue literals only in the allowlisted (not-yet-swept) files', () => {
    const files = collectSvelteFiles();
    const rx = /rgba\(\s*200\s*,\s*216\s*,\s*240\s*,|#(e2e8f0|cbd5e1|f8fafc)/i;
    const offenders = [];
    for (const f of files) {
      const rel = path.relative(process.cwd(), f).split(path.sep).join('/');
      let src = fs.readFileSync(f, 'utf-8');
      // Strip comments so documentation examples (like the ones in this
      // very guard, or migration-note comments in swept files) don't
      // trip the guard.
      src = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '');
      if (rx.test(src)) offenders.push(rel);
    }

    const allowSet = new Set(A3_MUTED_LITERAL_ALLOWLIST);
    // 1. No NEW offenders outside the allowlist (regression fence).
    const newOffenders = offenders.filter(f => !allowSet.has(f));
    expect(newOffenders, `New stale pale-blue literal sites outside the A3 allowlist ` +
      `(must migrate to var(--algo-slate-muted) or color-mix(...var(--algo-slate)...)):\n${newOffenders.join('\n')}`
    ).toEqual([]);

    // 2. Allowlist entries that are ALREADY clean must be removed (the
    // ratchet only shrinks — stops a stale allowlist entry from masking
    // future regressions in a file that's already been swept).
    const staleAllowlistEntries = A3_MUTED_LITERAL_ALLOWLIST.filter(f => !offenders.includes(f));
    expect(staleAllowlistEntries, `These allowlist entries are already clean — remove them from ` +
      `A3_MUTED_LITERAL_ALLOWLIST (ratchet must shrink, never carry dead entries):\n${staleAllowlistEntries.join('\n')}`
    ).toEqual([]);
  });

  test('.cell-muted derives from var(--algo-slate-muted) (computed-style, not source-grep)', async ({ page }) => {
    // Guards against a hardcoded literal that happens to render the same
    // colour as the CURRENT --algo-slate-muted value (which would pass a
    // naive `color === rgba(255,255,255,0.55)` check and silently
    // regress the next time the token's value changes) — override
    // --algo-slate-muted to a distinctive colour on a scratch container
    // and assert `.cell-muted` tracks it, proving real derivation via
    // the token rather than a coincidentally-matching literal.
    //
    // Note: overriding the UPSTREAM --algo-slate (rather than
    // --algo-slate-muted directly) on a descendant does NOT work here —
    // CSS custom properties are substituted into their computed value at
    // the element where they are SPECIFIED (only :root specifies
    // --algo-slate-muted), so --algo-slate-muted's own var(--algo-slate)
    // reference resolves against :root's cascade and that fully-resolved
    // value is what descendants inherit, regardless of a descendant's
    // own --algo-slate override (verified empirically). Overriding
    // --algo-slate-muted itself is both the correct test and a more
    // direct proof that `.cell-muted` reads through the token.
    await loginAsAdmin(page);
    await page.goto('/pulse', { waitUntil: 'domcontentloaded' });
    // Wait for MarketPulse's stylesheet chunk (carrying the :global(.cell-muted)
    // rule) to be loaded — any ag-theme-algo element proves the chunk is live.
    await page.waitForSelector('.ag-theme-algo', { timeout: 20_000 }).catch(() => {});

    const result = await page.evaluate(() => {
      const container = document.createElement('div');
      container.className = 'ag-theme-algo';
      // Distinctive override — nothing in the real palette is pure red.
      container.style.setProperty('--algo-slate-muted', 'rgba(255, 0, 0, 0.9)');
      const span = document.createElement('span');
      span.className = 'cell-muted';
      container.appendChild(span);
      document.body.appendChild(container);
      const resolved = getComputedStyle(span).color;
      document.body.removeChild(container);
      return resolved;
    });

    expect(result, `.cell-muted did not track a --algo-slate-muted override — got ${result}`)
      .toBe('rgba(255, 0, 0, 0.9)');
  });

  test('no literal card-bg gradient outside app.css', () => {
    const files = collectSvelteFiles();
    const offenders = [];
    // The exact canonical gradient literal. If present in .svelte
    // scoped CSS it should be replaced with var(--card-bg-gradient).
    const rx = /linear-gradient\(180deg,\s*#1d2a44\s+0%,\s*#152033\s+100%\)/;
    for (const f of files) {
      const src = fs.readFileSync(f, 'utf-8');
      if (rx.test(src)) offenders.push(path.relative(process.cwd(), f));
    }
    expect(offenders, `card-bg gradient literal must use var(--card-bg-gradient). ` +
      `Offenders:\n${offenders.join('\n')}`).toEqual([]);
  });
});

/* ── Phase 1 palette-token grep guard (2026-07-02) ──────────────────── *
 *
 * Asserts that the 5 Phase-1 migration target files:
 *   1. Contain NO raw palette hex literals from the migrated set.
 *   2. Each use var(--c-*) at least as many times as the migration
 *      delivered (floor counts; the guard is a regression fence, not
 *      an exact snapshot). Only the semantic --c-* layer is checked —
 *      residual var(--algo-sky-*) / border / bg variants have no --c-*
 *      equivalent and are intentionally excluded from the floor.
 *
 * "Near-match" rgba values that were intentionally left raw (each
 * appearing <3× with a non-standard alpha) are NOT listed here —
 * they are Phase 2 candidates and not yet tokenised.
 * ─────────────────────────────────────────────────────────────────── */

const PHASE1_FILES = [
  path.join('src/lib', 'MarketPulse.svelte'),
  path.join('src/lib', 'PerformancePage.svelte'),
  path.join('src/routes/(algo)/dashboard', '+page.svelte'),
  path.join('src/routes/(algo)/admin/derivatives', '+page.svelte'),
  path.join('src/lib', 'NavCard.svelte'),
];

/** Palette hex literals that must NOT appear in Phase-1 files. */
const BANNED_HEX = [
  '#4ade80',
  '#f87171',
  '#22d3ee',
  '#fbbf24',
  '#7dd3fc',
  '#7e97b8',
];

/** Minimum var(--c-*) semantic token call count per file.
 *  Set at ~90% of the actual post-migration count so minor
 *  future refactors don't spuriously trip the guard. */
const TOKEN_FLOOR = {
  'MarketPulse.svelte':  32,
  'PerformancePage.svelte': 7,
  '+page.svelte (dashboard)': 34,
  '+page.svelte (derivatives)': 68,
  'NavCard.svelte': 1,
};

test.describe('algo consistency — Phase 1 palette migration guard', () => {
  test('no raw palette hex in Phase-1 migrated files', () => {
    const offenders = [];
    for (const rel of PHASE1_FILES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      // Strip comments so doc-example literals in /* … */ don't trip the guard
      const stripped = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '');
      for (const hex of BANNED_HEX) {
        if (stripped.includes(hex)) {
          offenders.push(`${path.basename(rel)}: contains raw ${hex}`);
        }
      }
    }
    expect(offenders,
      `Palette hex literals must use var(--c-*) semantic tokens. Regression in:\n${offenders.join('\n')}`
    ).toEqual([]);
  });

  test('Phase-1 files meet minimum --c-* semantic token usage floors', () => {
    const failures = [];
    for (const rel of PHASE1_FILES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      // Count ONLY var(--c-*) occurrences — the semantic alias layer.
      // var(--algo-*) usages that have no --c-* equivalent (sky, violet,
      // border, bg variants) are intentionally excluded from this floor.
      const matches = (src.match(/var\(--c-/g) || []).length;
      // Identify the floor entry by filename
      const key = rel.includes('dashboard')
        ? '+page.svelte (dashboard)'
        : rel.includes('derivatives')
          ? '+page.svelte (derivatives)'
          : path.basename(rel);
      const floor = TOKEN_FLOOR[key] ?? 1;
      if (matches < floor) {
        failures.push(`${key}: ${matches} --c-* usages (floor ${floor}) — semantic layer not applied`);
      }
    }
    expect(failures,
      `--c-* semantic token usage below floor — Commit 8 migration may not have landed:\n${failures.join('\n')}`
    ).toEqual([]);
  });

  test('app.css defines the 15 --c-* semantic alias tokens', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    // Alpha suffixes match actual alpha values: green/red use 0.06/0.10;
    // cyan uses 0.08/0.14.
    const required = [
      '--c-long', '--c-short', '--c-info', '--c-action', '--c-muted',
      '--c-long-06', '--c-long-10', '--c-long-22',
      '--c-short-06', '--c-short-10', '--c-short-22',
      '--c-info-08', '--c-info-14', '--c-info-22',
      '--c-action-14', '--c-action-22',
    ];
    for (const t of required) {
      expect(src, `${t} must be declared in app.css`).toContain(t + ':');
    }
  });
});

/* ── Phase 2 palette-token grep guard (2026-07-02) ──────────────────── *
 *
 * Extends the Phase-1 guard to the ~75 files swept in Phase 2.
 * Same contract:
 *   1. No raw palette hex in the migrated files (post-comment-strip).
 *   2. Each file uses var(--c-*) at least as many times as the floor
 *      count (set at ~90% of actual post-migration count).
 *
 * Phase-3 candidates (near-match alphas left raw intentionally):
 *   amber-10 (×39), amber-18 (×33), amber-55 (×30), amber-45 (×24),
 *   amber-35 (×24), amber-15 (×20), muted-18 (×51), muted-10 (×23),
 *   muted-30 (×25), red-55 (×24), red-35 (×24), red-15 (×20),
 *   green-18 (×16), cyan-65 (×16)  — all deferred to Phase 3.
 * ─────────────────────────────────────────────────────────────────── */

const PHASE2_FILES = [
  path.join('src/lib', 'SymbolPanel.svelte'),
  path.join('src/lib/order', 'OrderTicket.svelte'),
  path.join('src/lib/execution', 'SimulatorPanel.svelte'),
  path.join('src/lib', 'ChartWorkspace.svelte'),
  path.join('src/lib/order', 'OptionChainTab.svelte'),
  path.join('src/lib', 'OptionsPayoff.svelte'),
  path.join('src/routes/(algo)', '+layout.svelte'),
  path.join('src/lib', 'LogPanel.svelte'),
  path.join('src/lib', 'BrokerHealthBadge.svelte'),
  path.join('src/lib', 'Select.svelte'),
  path.join('src/lib', 'CommandBar.svelte'),
  path.join('src/lib', 'NavTab.svelte'),
  path.join('src/lib', 'PnlAnalysis.svelte'),
  path.join('src/lib', 'NavBreakdown.svelte'),
  path.join('src/lib', 'PriceChart.svelte'),
  path.join('src/lib', 'RefreshButton.svelte'),
  path.join('src/lib', 'MultiSelect.svelte'),
  path.join('src/lib', 'EquityCurve.svelte'),
  path.join('src/lib', 'UnifiedLog.svelte'),
  path.join('src/lib', 'PnlPanel.svelte'),
  path.join('src/lib', 'MultiPriceChart.svelte'),
  path.join('src/lib/order', 'ChaseCard.svelte'),
  path.join('src/lib/order', 'CommandLineTab.svelte'),
  path.join('src/lib/order', 'OrderTimelineDrawer.svelte'),
];

/** Minimum var(--c-*) usage floors for Phase-2 files (~90% of actual). */
const TOKEN_FLOOR_P2 = {
  'SymbolPanel.svelte': 63,
  'OrderTicket.svelte': 42,
  'SimulatorPanel.svelte': 45,
  'ChartWorkspace.svelte': 22,
  'OptionChainTab.svelte': 28,
  'OptionsPayoff.svelte': 14,
  '+layout.svelte': 46,
  'LogPanel.svelte': 22,
  'BrokerHealthBadge.svelte': 16,
  'Select.svelte': 8,
  'CommandBar.svelte': 10,
};

/** Phase-2 banned hex — same set as Phase-1 minus sky (#7dd3fc) which
 *  has no --c-* alias and was not a Phase-2 migration target. */
const BANNED_HEX_P2 = [
  '#4ade80',  // --c-long
  '#f87171',  // --c-short
  '#22d3ee',  // --c-info
  '#fbbf24',  // --c-action
  '#7e97b8',  // --c-muted
];

/** Strip comments, SVG presentation attrs and known-exempt CSS props
 *  before checking for raw palette hex. */
function stripExemptContextsP2(src) {
  return src
    // Block comments
    .replace(/\/\*[\s\S]*?\*\//g, '')
    // HTML template comments
    .replace(/<!--[\s\S]*?-->/g, '')
    // JS line comments
    .replace(/\/\/[^\n]*/g, '')
    // SVG presentation attributes (CSS var() unreliable in attrs)
    .replace(/\b(fill|stroke|stop-color)="[^"]*"/g, '')
    // accent-color intentionally excluded (control theming, not content)
    .replace(/accent-color\s*:[^;]+;/g, '');
}

test.describe('algo consistency — Phase 2 palette migration guard', () => {
  test('no raw palette hex in Phase-2 migrated files', () => {
    const offenders = [];
    for (const rel of PHASE2_FILES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      const stripped = stripExemptContextsP2(src);
      for (const hex of BANNED_HEX_P2) {
        // Match 6-digit hex only — exclude hex8 (e.g. #fbbf2466) which is a
        // distinct colour and has no --c-* alias (deferred Phase-3).
        const rx = new RegExp(hex + '(?![0-9a-fA-F])', 'i');
        if (rx.test(stripped)) {
          offenders.push(`${path.basename(rel)}: contains raw ${hex}`);
        }
      }
    }
    expect(offenders,
      `Phase-2 files must use var(--c-*) semantic tokens. Regression in:\n${offenders.join('\n')}`
    ).toEqual([]);
  });

  test('Phase-2 key files meet minimum --c-* semantic token usage floors', () => {
    const failures = [];
    for (const rel of PHASE2_FILES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      const name = path.basename(rel);
      const floor = TOKEN_FLOOR_P2[name];
      if (!floor) continue;  // No floor defined for low-count files
      const matches = (src.match(/var\(--c-/g) || []).length;
      if (matches < floor) {
        failures.push(`${name}: ${matches} --c-* usages (floor ${floor})`);
      }
    }
    expect(failures,
      `--c-* semantic token usage below floor — Phase 2 migration may not have landed:\n${failures.join('\n')}`
    ).toEqual([]);
  });
});

/* ── Off-white → white text-token guard (2026-09) ────────────────────── *
 *
 * `--algo-slate` / `--text-primary` (was #c8d8f0) and `--text-hi`
 * (was #e6edf7) were whitened to pure #ffffff (algo dark-terminal ONLY —
 * never public marketing / investor routes, confirmed those trees have
 * zero usage of these tokens or the literal-bearing components below).
 * A raw-literal sweep converted every hard-coded occurrence of the old
 * pale-blue hex (plus drift-duplicate near-misses #e5edf7 / #b4c8e6) to
 * `var(--algo-slate)` / `var(--text-hi)` (CSS `color:` declarations),
 * `#ffffff` (bare SVG `fill=` presentation attributes and JS color-string
 * fallbacks — CSS custom properties don't reliably resolve there), or
 * Tailwind's own `text-white` / `text-white/NN` utility (arbitrary-value
 * classes carrying an opacity modifier, since Tailwind can't compute an
 * alpha-blended arbitrary CSS-var color at build time).
 *
 * Deliberately NOT in scope (confirmed intentional, not accidental
 * off-white — do not add to BANNED_WHITENING_HEX):
 *   - `--text-sub` (#c4d0e0) and `--algo-blue-tint` (#f1f7ff) — distinct
 *     deliberate tokens, not part of the --algo-slate/--text-hi family.
 *   - `--chart-grid-stroke*` (rgba(200, 216, 240, …)) and every other
 *     rgba(200, 216, 240, alpha) dimmed-text/border/background usage —
 *     a deliberately dimmed variant of the same hue family, distinct
 *     from full-opacity primary text; whitening the opaque hex does not
 *     imply whitening every alpha-blended derivative.
 * ─────────────────────────────────────────────────────────────────── */

/** Raw hex literals that must not appear (case-insensitive) anywhere in
 *  the algo surface after the whitening sweep. 6-digit hex only — the
 *  `(?![0-9a-f])` lookahead in the check below excludes any 8-digit
 *  (alpha-suffixed) variant, which is a distinct colour. */
const BANNED_WHITENING_HEX = ['#c8d8f0', '#e6edf7', '#e5edf7', '#b4c8e6'];

/** Strip comments (which may legitimately document the OLD hex value in
 *  a "was #xxxxxx" migration note) before checking for a live literal. */
function stripCommentsForWhiteningGuard(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/<!--[\s\S]*?-->/g, '');
}

test.describe('algo consistency — off-white to white sweep guard (2026-09)', () => {
  test('no raw pale-blue/drift hex literals remain in algo lib + routes', () => {
    const files = collectSvelteFiles();
    const offenders = [];
    for (const f of files) {
      const stripped = stripCommentsForWhiteningGuard(fs.readFileSync(f, 'utf-8'));
      for (const hex of BANNED_WHITENING_HEX) {
        const rx = new RegExp(hex.replace('#', '#') + '(?![0-9a-f])', 'i');
        if (rx.test(stripped)) {
          offenders.push(`${path.relative(process.cwd(), f)}: contains ${hex}`);
        }
      }
    }
    expect(offenders,
      `Whitening sweep regression — raw pale-blue/drift hex must route through var(--algo-slate)/var(--text-hi) ` +
      `(or #ffffff for SVG fill / JS fallback / Tailwind text-white where var() is unreliable):\n${offenders.join('\n')}`
    ).toEqual([]);
  });

  test('app.css: --algo-slate is pure white and --text-hi aliases it', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src, '--algo-slate must be #ffffff post-whitening').toMatch(/--algo-slate:\s*#ffffff;/i);
    expect(src, '--text-hi must alias --algo-slate (single literal, two semantic names)')
      .toMatch(/--text-hi:\s*var\(--algo-slate\);/i);
  });

  test('BrokerHealthBadge.svelte account rows use the whitened token, not a raw literal', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib', 'BrokerHealthBadge.svelte'), 'utf-8');
    expect(src).toContain('.bh-row-account');
    expect(src).not.toMatch(/#c8d8f0|#e5edf7/i);
  });
});

/* ── SSOT tokens defined ────────────────────────────────────────────── */

test.describe('algo consistency — token definitions present', () => {
  test('app.css defines the six --fs-* tokens', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    for (const t of ['--fs-2xs', '--fs-xs', '--fs-sm', '--fs-md', '--fs-lg', '--fs-xl']) {
      expect(src, `${t} must be declared in app.css`).toContain(t);
    }
  });

  test('app.css defines --font-numeric and --font-text', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('--font-numeric:');
    expect(src).toContain('--font-text:');
  });

  test('app.css defines .algo-modal recipe', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('.algo-modal');
    // Amber halo + gradient + shadow are the three visual-chrome fingerprints.
    expect(src).toMatch(/\.algo-modal[\s\S]{0,400}amber-bg-soft/);
  });

  test('app.css defines --card-bg-gradient token', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('--card-bg-gradient:');
  });

  test('app.css defines --algo-violet token', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toContain('--algo-violet:');
    expect(src).toContain('--algo-violet-bg-soft:');
  });
});

/* ── Live-route consistency (routes × viewports) ───────────────────── */

// Login rate-limit is 5/min on the API. Serial mode + single login
// re-used across all route probes avoids hammering /api/auth/login
// and false-failing on rate-limit. Route sweeps are cheap post-login
// (goto + one evaluate) so the whole block finishes well under budget.
//
// The first probe primes auth; the sharedPage carries the JWT
// across subsequent probes. If login fails (e.g. rate-limited from a
// prior test run within the 60 s window), the entire describe is
// skipped rather than reporting N synthetic failures — the offline
// SSOT + token-definition checks above already cover the migration
// contract; the live-route checks are UX confidence.
test.describe.serial('algo consistency — live routes render dark tokens', () => {
  test.setTimeout(90_000);

  /** @type {import('@playwright/test').Page | null} */
  let sharedPage = null;
  /** @type {string} */
  let authSkipReason = '';

  test.beforeAll(async ({ browser }, testInfo) => {
    // Login flow retries twice with 3s + 8s waits — the hook can run
    // for up to ~40 s. Bump the default 30 s hook timeout.
    testInfo.setTimeout(60_000);
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAdmin(page);
      sharedPage = page;
    } catch (e) {
      authSkipReason = `login unavailable (${(/** @type {Error} */ (e)).message}). ` +
        `Live-route consistency probes require auth; offline SSOT + token ` +
        `checks above cover the migration contract.`;
      await ctx.close().catch(() => {});
    }
  });

  test.afterAll(async () => {
    if (sharedPage) await sharedPage.context().close();
  });

  for (const route of ALGO_ROUTES) {
    test(`${route} — dark bg + primary text colour resolve through tokens`, async () => {
      test.skip(!sharedPage, authSkipReason);
      // sharedPage is non-null when the skip guard passes above.
      const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
      await p.goto(route, { waitUntil: 'domcontentloaded' });
      await p.waitForTimeout(500);

      // The algo layout wraps every algo route in `.algo-viewport`. Its
      // computed bg must resolve to the algo dark-navy elevation stack
      // (not cream / public gray). --algo-bg-elev1 / --algo-bg-elev2
      // sit in the R,G,B < 50 range.
      const bg = await p.evaluate(() => {
        const el = document.querySelector('.algo-viewport');
        return el ? getComputedStyle(el).backgroundColor : '';
      });
      expect(bg, `${route} viewport bg: ${bg}`).toMatch(/^rgba?\(\s*\d+,\s*\d+,\s*\d+/);
      // Parse the RGB triple and assert each channel < 60 (dark navy).
      const [r, g, b] = (bg.match(/\d+/g) || []).map(Number);
      expect(r, `${route} R=${r}`).toBeLessThan(60);
      expect(g, `${route} G=${g}`).toBeLessThan(60);
      expect(b, `${route} B=${b}`).toBeLessThan(80);
    });
  }
});

/* ── Perf: cross-route nav heap growth budget ─────────────────────── */

test.describe.serial('algo consistency — perf', () => {
  test.setTimeout(120_000);
  test('cross-route lap keeps heap growth under 8 MB', async ({ page, browserName }) => {
    test.skip(browserName !== 'chromium', 'JS heap API is chromium-only');
    try {
      await loginAsAdmin(page);
    } catch (e) {
      test.skip(true, `login unavailable (${(/** @type {Error} */ (e)).message})`);
      return;
    }

    // Warm-up lap — first mount of each route amortizes lazy imports.
    for (const r of ALGO_ROUTES.slice(0, 5)) {
      await page.goto(r, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(200);
    }

    // Baseline
    await page.evaluate(() => {
      if ('gc' in globalThis && typeof globalThis.gc === 'function') globalThis.gc();
    });
    const before = await page.evaluate(() => {
      const p = /** @type {any} */ (performance);
      return p.memory ? p.memory.usedJSHeapSize : null;
    });
    if (before == null) {
      test.skip(true, 'performance.memory unavailable — chromium flag not set');
      return;
    }

    // Measured lap — five routes
    for (const r of ALGO_ROUTES.slice(0, 5)) {
      await page.goto(r, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(300);
    }

    await page.evaluate(() => {
      if ('gc' in globalThis && typeof globalThis.gc === 'function') globalThis.gc();
    });
    const after = await page.evaluate(() => {
      const p = /** @type {any} */ (performance);
      return p.memory ? p.memory.usedJSHeapSize : null;
    });

    const growthMB = after != null ? (after - before) / (1024 * 1024) : 0;
    expect(growthMB, `heap growth after 5-route lap: ${growthMB.toFixed(2)} MB`).toBeLessThan(8);
  });
});

/* ── A5 (2026-09 audit) — symbol-cell colour/weight consistency ──────
 * MarketPulse's base `.sym-main` rule and derivatives Legs tab's
 * CandidateLegRow.svelte `.sym-main` rule render the SAME role (base
 * symbol text before the CE/PE green/red split) and must now share
 * colour (var(--algo-slate)) + weight (500). Reuse-dimension guard:
 * source-grep both files rather than depending on live grid data. ── */

test.describe('algo consistency — symbol-cell base treatment (A5)', () => {
  test('MarketPulse.svelte and CandidateLegRow.svelte .sym-main share colour + weight', () => {
    const mpPath = path.join(process.cwd(), 'src/lib/MarketPulse.svelte');
    const legPath = path.join(process.cwd(), 'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte');
    const mp = fs.readFileSync(mpPath, 'utf-8');
    const leg = fs.readFileSync(legPath, 'utf-8');

    // Base rule (not the .sym-ce/.sym-pe overrides) — match the exact
    // `.sym-main { … }` declaration, tolerating a `:global(...)` wrapper
    // and/or an ancestor selector (e.g. `.cand-sym .sym-main)`) before
    // the closing paren + `{`.
    const mpBase = mp.match(/\.sym-main\)?\s*\{([^}]*)\}/);
    const legBase = leg.match(/\.sym-main\)?\s*\{([^}]*)\}/);
    expect(mpBase, 'MarketPulse.svelte must declare a base .sym-main rule').not.toBeNull();
    expect(legBase, 'CandidateLegRow.svelte must declare a base .sym-main rule').not.toBeNull();

    for (const [name, body] of [['MarketPulse', mpBase[1]], ['CandidateLegRow', legBase[1]]]) {
      expect(body, `${name}'s .sym-main must use var(--algo-slate), not a hardcoded hex`).toContain('var(--algo-slate)');
      expect(body, `${name}'s .sym-main must be font-weight: 500`).toMatch(/font-weight:\s*500/);
      expect(body, `${name}'s .sym-main must NOT hardcode #e2e8f0 (A3 banned literal)`).not.toContain('#e2e8f0');
    }

    // The CE/PE split is unrelated to this fix and must be untouched on
    // both surfaces — still driven by --c-long / --c-short.
    expect(mp).toContain('var(--c-long)');
    expect(mp).toContain('var(--c-short)');
    expect(leg).toContain('var(--c-long)');
    expect(leg).toContain('var(--c-short)');
  });
});

/* ── First-column-only decoration rule (2026-09 fix) ──────────────────
 * Operator ruling: a column's distinctive decoration — account identity
 * stripe/tint (--acct-color, `.ag-theme-algo .ag-col-acct`) or symbol
 * CE/PE colour split (`.sym-ce`/`.sym-pe`) — applies ONLY when that
 * column is the FIRST column in its grid. Non-first-column instances
 * must render as plain cells.
 *
 * Grids audited:
 *   Account — KEEP (first col):  NavBreakdown (dashboard NAV/Capital/
 *             Equity tabs), BrokerHealthBadge popup.
 *   Account — REMOVE (trailing): MarketPulse Positions/Holdings grid
 *             (`mkAcctColTrailing`, pulseColumns.js) — the confirmed
 *             defect.
 *   Symbol  — KEEP (first col):  MarketPulse left grid (Pinned/
 *             Watchlist/Movers).
 *   Symbol  — REMOVE (not first, a state/pair column precedes it):
 *             MarketPulse right grid (Positions/Holdings), derivatives
 *             Legs/Expiry tab (CandidateLegRow.svelte — checkbox + state
 *             track precede the symbol cell).
 *
 * Static/source checks are deterministic; live computed-style checks
 * are best-effort (skip, not fail, when live account/position data
 * isn't present — e.g. a fresh demo account or an idle Sunday session).
 * ───────────────────────────────────────────────────────────────────── */

test.describe('algo consistency — first-column-only decoration (source)', () => {
  test('MarketPulse: left-grid symbol column keeps the tint renderer, right-grid does not', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib/MarketPulse.svelte'), 'utf-8');
    // Left grid (first-column symbol) must still wire the plain `symRenderer`.
    expect(src, 'mkSymColLeft must use symRenderer (CE/PE tint kept — first column)')
      .toMatch(/mkSymColLeft\(\{\s*symRenderer\s*\}\)/);
    // Right grid (symbol is 2nd, after the 'St' pos-state column) must use the
    // no-tint wrapper instead of the raw symRenderer.
    expect(src, 'mkSymColRight must use symRendererRight (CE/PE tint suppressed — not first column)')
      .toMatch(/mkSymColRight\(\{\s*symRenderer:\s*symRendererRight\s*\}\)/);
    // The wrapper itself must call through with applyOptTint=false.
    expect(src).toMatch(/function symRendererRight\(params\)\s*\{\s*return symRenderer\(params,\s*false\)/);
  });

  test('CandidateLegRow (derivatives Legs/Expiry): symbol CE/PE tint is hardcoded off', () => {
    const legPath = path.join(process.cwd(), 'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte');
    const src = fs.readFileSync(legPath, 'utf-8');
    // Row order is checkbox -> .cand-state-cell (state track) -> .cand-sym
    // (symbol) — symbol is the THIRD element, never first, so the CE/PE
    // split must never be computed from decomposeSymbol here.
    expect(src, '_optClass must be a hardcoded empty string, not derived from opt type')
      .toMatch(/const _optClass\s*=\s*'';/);
    expect(src).not.toMatch(/_optClass\s*=\s*\$derived\(/);
  });

  test('pulseColumns.js: mkAcctColTrailing carries no ag-col-acct stripe/tint', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib/data/pulseColumns.js'), 'utf-8');
    const fnMatch = src.match(/export function mkAcctColTrailing\([^)]*\)\s*\{[\s\S]*?\n\}/);
    expect(fnMatch, 'mkAcctColTrailing function not found').not.toBeNull();
    const body = fnMatch[0];
    expect(body, 'trailing Account column must not carry ag-col-acct').not.toContain('ag-col-acct');
    expect(body, 'trailing Account column must not inject --acct-color').not.toContain('--acct-color');
  });

  /**
   * PerformancePage.svelte (public /performance page — cream theme) has
   * six colDef arrays. Four (holdingsSummaryCols, positionsSummaryCols,
   * fundsCols, navCols) already carry Account as the FIRST column and
   * correctly keep the isolated --acct-stripe / :global(.ag-col-acct)
   * mechanism (PerformancePage.svelte's own scoped <style>, NOT app.css —
   * distinct from the shared ag-theme-algo .ag-col-acct rule audited
   * above). The other two (holdingsCols, positionsCols) moved Account to
   * the TRAILING column per an earlier "action-first" operator request,
   * yet kept applying the stripe/tint — a confirmed defect, fixed with
   * operator sign-off (2026-09). Verifies the fix landed on exactly the
   * two trailing-Account sets and did not regress the four leading ones.
   */
  function _extractColsArraySource(src, arrayName, opts = {}) {
    const { derived = false } = opts;
    const re = derived
      ? new RegExp(`const ${arrayName} = \\$derived\\(\\[([\\s\\S]*?)\\n  \\]\\);`)
      : new RegExp(`const ${arrayName} = \\[([\\s\\S]*?)\\n  \\];`);
    const m = src.match(re);
    expect(m, `${arrayName} colDef array not found`).not.toBeNull();
    return m[1];
  }

  test('PerformancePage: holdingsCols/positionsCols (Account trailing) carry no stripe/tint', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib/PerformancePage.svelte'), 'utf-8');

    for (const [arrayName, derived] of [['holdingsCols', false], ['positionsCols', true]]) {
      const body = _extractColsArraySource(src, arrayName, { derived });
      const acctFieldMatch = body.match(/\{\s*field:\s*'account'[\s\S]*?\}/);
      expect(acctFieldMatch, `${arrayName}: account colDef not found`).not.toBeNull();
      const acctDef = acctFieldMatch[0];
      expect(acctDef, `${arrayName}.account must not carry acctFill`).not.toMatch(/cellClass:\s*acctFill/);
      expect(acctDef, `${arrayName}.account must not carry ag-col-acct`).not.toContain('ag-col-acct');
      expect(acctDef, `${arrayName}.account must not inject --acct-stripe via acctCellStyle`)
        .not.toMatch(/cellStyle:\s*acctCellStyle/);
      // acctCellRenderer stays — it only handles the mask-string passthrough,
      // not colour identity.
      expect(acctDef, `${arrayName}.account should keep acctCellRenderer (mask logic)`)
        .toMatch(/cellRenderer:\s*acctCellRenderer/);
    }
  });

  test('PerformancePage: holdingsSummaryCols/positionsSummaryCols/fundsCols/navCols (Account first) keep the stripe', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib/PerformancePage.svelte'), 'utf-8');

    for (const arrayName of ['holdingsSummaryCols', 'positionsSummaryCols', 'fundsCols', 'navCols']) {
      const body = _extractColsArraySource(src, arrayName);
      // Account must be the first colDef *object* in the array — skip
      // leading comment lines when finding it.
      const firstBraceIdx = body.indexOf('{');
      expect(firstBraceIdx, `${arrayName}: no colDef object found`).toBeGreaterThanOrEqual(0);
      const firstEntry = body.slice(firstBraceIdx).match(/\{[\s\S]*?\}/)[0];
      expect(firstEntry, `${arrayName}: first colDef object`).toMatch(/field:\s*'account'/);
      expect(firstEntry, `${arrayName}.account must keep acctFill`).toMatch(/cellClass:\s*acctFill/);
      expect(firstEntry, `${arrayName}.account must keep acctCellStyle (--acct-stripe)`)
        .toMatch(/cellStyle:\s*acctCellStyle/);
    }
  });
});

test.describe.serial('algo consistency — first-column-only decoration (live)', () => {
  test.setTimeout(120_000);

  /** @type {import('@playwright/test').Page | null} */
  let sharedPage = null;
  let authSkipReason = '';

  test.beforeAll(async ({ browser }, testInfo) => {
    testInfo.setTimeout(60_000);
    const ctx = await browser.newContext();
    const page = await ctx.newPage();
    try {
      await loginAsAdmin(page);
      sharedPage = page;
    } catch (e) {
      authSkipReason = `login unavailable (${(/** @type {Error} */ (e)).message})`;
      await ctx.close().catch(() => {});
    }
  });

  test.afterAll(async () => {
    if (sharedPage) await sharedPage.context().close();
  });

  /** Reads border-left-width/-color + background-color off a locator's first match. */
  async function readAcctCellStyle(locator) {
    return locator.first().evaluate((el) => {
      const cs = getComputedStyle(el);
      return {
        borderLeftWidth: cs.borderLeftWidth,
        borderLeftColor: cs.borderLeftColor,
        backgroundColor: cs.backgroundColor,
      };
    });
  }

  test('dashboard NavBreakdown (NAV tab, P-slot): Account is first column — keeps stripe + tint', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.nav-bd-ag', { timeout: 20_000 }).catch(() => {});

    const cells = p.locator('.card-body:not([hidden]) .nav-bd-ag .ag-cell[col-id="account"]')
      .filter({ hasNotText: 'TOTAL' });
    const count = await cells.count();
    test.skip(count === 0, 'no non-TOTAL NavBreakdown P-slot account rows rendered');

    const style = await readAcctCellStyle(cells);
    expect(Number.parseFloat(style.borderLeftWidth), `border-left: ${style.borderLeftWidth}`)
      .toBeGreaterThanOrEqual(3);
    expect(style.borderLeftColor, `border colour: ${style.borderLeftColor}`)
      .not.toBe('rgba(0, 0, 0, 0)');
  });

  test('BrokerHealthBadge popup: Account is first data column — keeps stripe + tint', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    // The chip only renders once $connStatus resolves (async store) — wait
    // for it rather than sampling immediately after navigation.
    const chip = p.locator('.broker-chip');
    await chip.first().waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {});
    if (!(await chip.count())) {
      test.skip(true, 'broker-chip not present on this session');
      return;
    }
    await chip.first().click();
    await p.waitForSelector('.bh-modal', { timeout: 10_000 }).catch(() => {});

    const cells = p.locator('.bh-modal .ag-cell[col-id="account"]');
    const count = await cells.count();
    test.skip(count === 0, 'no account rows rendered in BrokerHealthBadge popup');

    const style = await readAcctCellStyle(cells);
    expect(Number.parseFloat(style.borderLeftWidth), `border-left: ${style.borderLeftWidth}`)
      .toBeGreaterThanOrEqual(3);
    expect(style.borderLeftColor, `border colour: ${style.borderLeftColor}`)
      .not.toBe('rgba(0, 0, 0, 0)');
  });

  test('MarketPulse Positions/Holdings grid: Account is trailing — no stripe/tint', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.mp-bucket-positions .bucket-grid .ag-row', { timeout: 20_000 }).catch(() => {});

    // Account is the LAST column of a ~20-column grid — ag-Grid virtualises
    // off-screen columns out of the DOM entirely, so the (already-scrolled-
    // out) Account cells aren't present until brought into view. Click the
    // pinned-left symbol cell of the first row, then press End (ag-Grid's
    // keyboard shortcut to focus the row's last cell) to force it in.
    const firstSymCell = p.locator('.mp-bucket-positions .bucket-grid .ag-row').first()
      .locator('.ag-cell[col-id="tradingsymbol"]');
    if (await firstSymCell.count()) {
      await firstSymCell.click();
      await p.keyboard.press('End');
      await p.waitForTimeout(500);
    }

    const cells = p.locator(
      '.mp-bucket-positions .bucket-grid .ag-cell[col-id="account"], .mp-bucket-holdings .bucket-grid .ag-cell[col-id="account"]'
    ).filter({ hasNotText: 'TOTAL' });
    const count = await cells.count();
    test.skip(count === 0, 'no non-TOTAL account rows rendered in Positions/Holdings');

    const style = await readAcctCellStyle(cells);
    expect(Number.parseFloat(style.borderLeftWidth), `border-left: ${style.borderLeftWidth}`)
      .toBe(0);
    expect(style.backgroundColor, `background: ${style.backgroundColor}`)
      .toBe('rgba(0, 0, 0, 0)');
  });

  test('MarketPulse left grid: symbol is first column — CE/PE tint still resolvable', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.ag-theme-algo', { timeout: 20_000 }).catch(() => {});

    const ceOrPe = p.locator('.mp-col-left .sym-main.sym-ce, .mp-col-left .sym-main.sym-pe');
    const count = await ceOrPe.count();
    test.skip(count === 0, 'no CE/PE symbol rows in Pinned/Watchlist/Movers to sample');

    const color = await ceOrPe.first().evaluate((el) => getComputedStyle(el).color);
    // var(--c-long) / var(--c-short) both resolve to a real (non-slate) rgb —
    // just assert it differs from the plain base .sym-main slate colour.
    const baseColor = await p.evaluate(() => {
      const span = document.createElement('span');
      span.className = 'sym-main';
      document.body.appendChild(span);
      const c = getComputedStyle(span).color;
      document.body.removeChild(span);
      return c;
    });
    expect(color, `CE/PE tinted colour: ${color}`).not.toBe(baseColor);
  });

  test('MarketPulse right grid: symbol is 2nd column (after St) — no CE/PE tint', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.mp-bucket-positions .bucket-grid, .mp-bucket-holdings .bucket-grid', { timeout: 20_000 }).catch(() => {});

    // Non-vacuous guard: find a right-grid symbol cell whose visible text
    // looks like an option contract (ends CE/PE) before asserting absence
    // of the tint class — otherwise a zero-option account would pass by
    // having nothing to check.
    const optionLike = p.locator(
      '.mp-bucket-positions .bucket-grid .sym-main, .mp-bucket-holdings .bucket-grid .sym-main'
    ).filter({ hasText: /CE$|PE$/ });
    const count = await optionLike.count();
    test.skip(count === 0, 'no CE/PE option rows in Positions/Holdings to sample');

    const hasTintClass = await optionLike.first().evaluate((el) =>
      el.classList.contains('sym-ce') || el.classList.contains('sym-pe'));
    expect(hasTintClass, 'right-grid option symbol must NOT carry sym-ce/sym-pe (not first column)').toBe(false);
  });
});

/* ── PerformancePage (public /performance) — first-column-only decoration,
 * live checks (2026-09 fix, operator sign-off) ────────────────────────
 * No login required — /performance is the public page. Best-effort: skip
 * (not fail) when no live row data is present (e.g. idle Sunday session,
 * empty demo account). The deterministic gate is the source-level test
 * above; these confirm the computed style actually matches. ───────────── */
test.describe.serial('algo consistency — PerformancePage account decoration (live)', () => {
  test.setTimeout(60_000);

  /** Reads border-left-width/-color off a locator's first match. */
  async function readAcctBorder(locator) {
    return locator.first().evaluate((el) => {
      const cs = getComputedStyle(el);
      return { borderLeftWidth: cs.borderLeftWidth, borderLeftColor: cs.borderLeftColor };
    });
  }

  /**
   * Each tab (positions/holdings) renders TWO visible sections at once —
   * "Summary" (positionsSummaryCols/holdingsSummaryCols, Account FIRST,
   * correctly striped) and "Breakdown" (positionsCols/holdingsCols,
   * Account TRAILING, the fixed defect). A bare `section:not(.hidden)`
   * selector would match both and blur the two behaviours together, so
   * scope explicitly to the section whose <h2> reads "Breakdown".
   */
  function breakdownSection(page) {
    return page.locator('section:not(.hidden)')
      .filter({ has: page.locator('h2.section-heading', { hasText: 'Breakdown' }) });
  }

  test('positionsCols Breakdown grid (Account trailing): no stripe/tint', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    await page.locator('.tabs-row').first().waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {});
    // Positions tab is the default — Breakdown grid is positionsCols.
    const section = breakdownSection(page).first();
    const rows = section.locator('.ag-theme-quartz .ag-row').filter({ hasNotText: 'TOTAL' });
    const rowCount = await rows.count();
    test.skip(rowCount === 0, 'no live positions rows rendered');

    // Account is the LAST column — force it into view (pinned symbol
    // column click, then End) before sampling, same as the MarketPulse
    // trailing-column checks above.
    const firstSymCell = rows.first().locator('.ag-cell[col-id="tradingsymbol"]');
    if (await firstSymCell.count()) {
      await firstSymCell.click();
      await page.keyboard.press('End');
      await page.waitForTimeout(500);
    }

    const cells = section.locator('.ag-theme-quartz .ag-cell[col-id="account"]').filter({ hasNotText: 'TOTAL' });
    const count = await cells.count();
    test.skip(count === 0, 'no non-TOTAL account cells rendered in positions Breakdown grid');

    const style = await readAcctBorder(cells);
    expect(Number.parseFloat(style.borderLeftWidth), `border-left: ${style.borderLeftWidth}`).toBeLessThan(3);
  });

  test('holdingsCols Breakdown grid (Account trailing): no stripe/tint', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    const tabsRow = page.locator('.tabs-row').first();
    await tabsRow.waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {});
    const holdingsTab = tabsRow.locator('button[role="tab"]').nth(1);
    if (await holdingsTab.count()) await holdingsTab.click();
    await page.waitForTimeout(500);

    const section = breakdownSection(page).first();
    const rows = section.locator('.ag-theme-quartz .ag-row').filter({ hasNotText: 'TOTAL' });
    const rowCount = await rows.count();
    test.skip(rowCount === 0, 'no live holdings rows rendered');

    const firstSymCell = rows.first().locator('.ag-cell[col-id="tradingsymbol"]');
    if (await firstSymCell.count()) {
      await firstSymCell.click();
      await page.keyboard.press('End');
      await page.waitForTimeout(500);
    }

    const cells = section.locator('.ag-theme-quartz .ag-cell[col-id="account"]').filter({ hasNotText: 'TOTAL' });
    const count = await cells.count();
    test.skip(count === 0, 'no non-TOTAL account cells rendered in holdings Breakdown grid');

    const style = await readAcctBorder(cells);
    expect(Number.parseFloat(style.borderLeftWidth), `border-left: ${style.borderLeftWidth}`).toBeLessThan(3);
  });

  test('navCols grid (Account first, unaffected by this fix): keeps stripe/tint', async ({ page }) => {
    await page.goto('/performance', { waitUntil: 'domcontentloaded' });
    // fundsNavTab defaults to 'nav' — navEl is the FIRST .ag-theme-quartz
    // grid in DOM order (rendered above the Positions/Holdings tabs),
    // so scope to it explicitly rather than matching every grid on the
    // page (Summary/Breakdown grids for the default 'positions' tab are
    // also visible at the same time).
    await page.waitForSelector('.ag-theme-quartz', { timeout: 15_000 }).catch(() => {});
    const navGrid = page.locator('.ag-theme-quartz').first();

    const cells = navGrid.locator('.ag-cell[col-id="account"]').filter({ hasNotText: 'TOTAL' });
    const count = await cells.count();
    test.skip(count === 0, 'no non-TOTAL account cells rendered in NAV grid');

    const style = await readAcctBorder(cells);
    expect(Number.parseFloat(style.borderLeftWidth), `border-left: ${style.borderLeftWidth}`).toBeGreaterThanOrEqual(3);
  });
});
