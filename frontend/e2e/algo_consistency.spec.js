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

  /* ── A3b (2026-10 P1 audit) — near-miss pale-blue literal ratchet ──────
   * A3 above only matches the exact rgba(200,216,240,α) family. A P1
   * consistency audit found ~20 near-miss variants hand-written in the
   * order-surface files (180,200,230 / 220,230,245 / 160,185,220 /
   * 210,225,255) that the A3 regex never caught. The 10 order-surface
   * files targeted by that audit (OrderCard, SymbolPanel, LogPanel,
   * OrderTimelineDrawer, OrderBook, HeldOrdersCard, ChaseCard, OrderTicket,
   * OptionChainTab, OrderPairModal) were fully swept to var(--text-med) /
   * var(--text-lo) / var(--text-muted) (text roles) or
   * color-mix(in srgb, var(--text-med) <alpha>%, transparent) (border/
   * background roles, alpha preserved per-site) and must stay at zero.
   *
   * Every OTHER file in collectSvelteFiles() scope that already had one
   * of these near-miss literals before this sweep is intentionally NOT
   * touched (out of this audit's scope) and is allowlisted below so this
   * ratchet doesn't block on pre-existing, unrelated literals. Same
   * shrink-only contract as A3 — an allowlist entry that's already clean
   * must be removed. A SEPARATE allowlist from A3_MUTED_LITERAL_ALLOWLIST
   * on purpose: A3's own allowlist is fully swept (empty) and must stay
   * that way; sharing one list would silently re-open that fence. */
  const A3B_NEAR_MISS_LITERAL_ALLOWLIST = /** @type {string[]} */ ([
    'src/lib/Select.svelte',
    'src/lib/MultiSelect.svelte',
    'src/lib/MarketPulse.svelte',
    'src/lib/execution/RecordingsPanel.svelte',
    'src/routes/(algo)/+layout.svelte',
    'src/routes/(algo)/admin/derivatives/+page.svelte',
    'src/routes/(algo)/automation/+page.svelte',
    'src/routes/(algo)/automation/templates/+page.svelte',
    'src/routes/(algo)/automation/agent-templates/+page.svelte',
  ]);

  test('A3b ratchet — near-miss pale-blue literals only in the allowlisted (not-yet-swept) files', () => {
    const files = collectSvelteFiles();
    const rx = /rgba\(\s*180\s*,\s*200\s*,\s*230\s*,|rgba\(\s*220\s*,\s*230\s*,\s*245\s*,|rgba\(\s*160\s*,\s*185\s*,\s*220\s*,|rgba\(\s*210\s*,\s*225\s*,\s*255\s*,/i;
    const offenders = [];
    for (const f of files) {
      const rel = path.relative(process.cwd(), f).split(path.sep).join('/');
      let src = fs.readFileSync(f, 'utf-8');
      src = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '').replace(/\/\/[^\n]*/g, '');
      if (rx.test(src)) offenders.push(rel);
    }

    const allowSet = new Set(A3B_NEAR_MISS_LITERAL_ALLOWLIST);
    const newOffenders = offenders.filter(f => !allowSet.has(f));
    expect(newOffenders, `New near-miss pale-blue literal sites outside the A3b allowlist ` +
      `(must migrate to var(--text-med)/var(--text-lo)/var(--text-muted) or ` +
      `color-mix(...var(--text-med)...)):\n${newOffenders.join('\n')}`
    ).toEqual([]);

    const staleAllowlistEntries = A3B_NEAR_MISS_LITERAL_ALLOWLIST.filter(f => !offenders.includes(f));
    expect(staleAllowlistEntries, `These allowlist entries are already clean — remove them from ` +
      `A3B_NEAR_MISS_LITERAL_ALLOWLIST (ratchet must shrink, never carry dead entries):\n${staleAllowlistEntries.join('\n')}`
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
  // 2026-10 P1 audit fix — were missing from this array, so any raw-hex
  // regression in these 3 files was invisible to the guard below.
  path.join('src/lib/order', 'OrderCard.svelte'),
  path.join('src/lib/order', 'OrderPairModal.svelte'),
  path.join('src/lib', 'HeldOrdersCard.svelte'),
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

/* ── Whisper vertical cell hairline (2026-09 UI polish) ───────────────
 * Dense ₹/P&L numeric grids had no vertical separation between adjacent
 * cell values on `.ag-theme-algo` — app.css's own `.ag-theme-algo
 * .ag-cell` border-right (`rgba(126,151,184,0.10)`, SSOT, same hue/
 * family already used for `.ag-row` border-bottom + `.ag-header-cell`
 * border-right) was being unconditionally zeroed by MarketPulse's
 * `.mp-bucket-wrap` scoped override, which is the grid the operator
 * actually looks at day-to-day. Fix restores the fall-through instead
 * of duplicating the literal; keeps the pre-existing exclusions
 * (symbol cells — own coloured direction-bar edge; last column — no
 * trailing edge against the grid's outer border, keyed off ag-Grid's
 * own `ag-column-last` marker so it's virtualisation/pinning-safe).
 * `.ag-theme-ramboq` (PerformancePage, public/cream theme) is a
 * deliberately isolated namespace — untouched by this commit. ───────── */

/** Parse a computed border-right-color string into an alpha in [0,1].
 *  Chromium may report a plain literal as `rgba(r,g,b,a)` or, for a
 *  `color-mix()`-derived value, as `color(srgb r g b / a)` — handle
 *  both so this guard survives a future token refactor. */
function _parseAlpha(colorStr) {
  if (!colorStr) return null;
  let m = colorStr.match(/rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*([\d.]+)\s*)?\)/);
  if (m) return m[1] === undefined ? 1 : Number.parseFloat(m[1]);
  m = colorStr.match(/color\([^)]*\/\s*([\d.]+)\s*\)/);
  if (m) return Number.parseFloat(m[1]);
  return null;
}

test.describe('algo consistency — whisper cell hairline (source)', () => {
  test('app.css: whisper hairline SSOT + last-column exclusion are declared', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src, 'base .ag-cell border-right whisper token').toContain('border-right: 1px solid rgba(126,151,184,0.10) !important;');
    expect(src, 'ag-column-last exclusion (virtualisation/pinning-safe, not :last-child)')
      .toMatch(/\.ag-theme-algo \.ag-cell\.ag-column-last,\s*\n\s*\.ag-theme-algo \.ag-header-cell\.ag-column-last \{\s*\n\s*border-right:\s*0\s*!important;/);
  });

  test('MarketPulse.svelte: mp-bucket-wrap no longer blanket-zeroes the hairline on every cell', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib/MarketPulse.svelte'), 'utf-8');
    // Regression fence for the OLD rule this commit reverses.
    expect(src, 'must not blanket-zero border-right on every mp-bucket-wrap cell')
      .not.toMatch(/:global\(\.mp-bucket-wrap \.ag-theme-algo \.ag-cell\),\s*\n\s*:global\(\.mp-bucket-wrap \.ag-theme-algo \.ag-header-cell\) \{\s*\n\s*border-right:\s*0/);
    // The symbol-cell exclusion must still be explicit (own colour edge).
    expect(src).toMatch(/:global\(\.mp-bucket-wrap \.ag-theme-algo \.ag-cell\.ag-col-sym\)/);
  });

  /* ── Cross-surface parity fix (2026-09) — operator report: "not all
   * algo grids have subtle cell vertical borders like Pulse". Root
   * causes found:
   *   1. NavBreakdown (dashboard NAV/Margin/Cash/Holdings panels)
   *      already inherits this SSOT automatically — confirmed live
   *      below, no source fix needed there.
   *   2. dashboard's `.dash-mini-grid` (Winners/Losers mini-grids) —
   *      the CSS override was already removed in an earlier fix, but
   *      the `.dash-mini-grid` CLASS is dead: never applied to any
   *      element in the current dashboard template (the W/L mini-grids
   *      were relocated to /pulse). No live element to regress; the
   *      CSS rule is orphaned, not a defect in scope here.
   *   3. derivatives Legs/Exp-close tabs (`CandidateLegRow.svelte`,
   *      `.cand-row`) and the Snapshot tab (`+page.svelte`,
   *      `.byund-row`) are hand-rolled flex/grid rows, NOT ag-Grid —
   *      they never inherited the SSOT rule. Fixed below by adding an
   *      equivalent `border-right: 1px solid var(--sep-color)` (same
   *      rgba(126,151,184,0.10) family as the ag-Grid rule, via the
   *      existing token rather than a new literal) to each.
   *      Exclusions on both: the state/checkbox track, the
   *      symbol/underlying cell, and — critically — the Chg% cell
   *      (`.cand-chg-sep` / `.byund-chg-sep`), whose OWN right edge is
   *      already decorated via `inset -1px 0 0 0` box-shadow (verified
   *      empirically: a negative x-offset inset box-shadow paints a
   *      cell's RIGHT edge, not its left — same mechanism the symbol
   *      cell's own edge uses). Excluding the wrong cell here would
   *      either double that line or leave the LTP→Chg% boundary blank;
   *      excluding `.cand-chg-sep`/`.byund-chg-sep` themselves is what
   *      avoids the double line while still decorating every other
   *      boundary, including LTP's own right edge. */
  test('CandidateLegRow.svelte (Legs/Exp-close): divider rule excludes cells with their own right-edge decoration', () => {
    const src = fs.readFileSync(
      path.join(process.cwd(), 'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte'),
      'utf-8'
    );
    expect(src, 'must reuse the shared --sep-color token, not a new literal')
      .toMatch(/\.cand-row\s*>\s*span:not\(\.cand-state-cell\):not\(\.cand-sym-acct\):not\(\.cand-chg-sep\):not\(:last-child\)\s*\{\s*border-right:\s*1px solid var\(--sep-color\);/);
    // Regression fence — .leg-ltp must NOT be excluded (that was the bug:
    // it left the LTP→Chg% boundary with no divider at all).
    expect(src).not.toMatch(/:not\(\.leg-ltp\)/);
  });

  test('derivatives +page.svelte (Snapshot/byund-row): divider rule excludes cells with their own right-edge decoration', () => {
    const src = fs.readFileSync(
      path.join(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'),
      'utf-8'
    );
    expect(src, 'must reuse the shared --sep-color token, not a new literal')
      .toMatch(/\.byund-row:not\(\.byund-row-total\)\s*>\s*span:not\(\.byund-und\):not\(\.byund-chg-sep\):not\(:last-child\)\s*\{\s*border-right:\s*1px solid var\(--sep-color\);/);
    // Regression fence — .byund-ltp must NOT be excluded (same doubling/
    // gap bug as the Legs grid above).
    expect(src).not.toMatch(/:not\(\.byund-ltp\)/);
  });

  test('app.css: --sep-color resolves to the same rgba the ag-Grid SSOT rule hardcodes', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    expect(src).toMatch(/--sep-color:\s*rgba\(126,\s*151,\s*184,\s*0\.10\);/);
    expect(src).toMatch(/\.ag-theme-algo \.ag-cell\s*\{[\s\S]{0,1500}border-right:\s*1px solid rgba\(126,151,184,0\.10\)\s*!important;/);
  });
});

test.describe.serial('algo consistency — whisper cell hairline (live)', () => {
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

  test('MarketPulse Positions grid: non-last numeric cell shows a low-opacity vertical border', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.mp-bucket-positions .bucket-grid .ag-row', { timeout: 20_000 }).catch(() => {});

    // A middle numeric column (LTP) — not the pinned symbol cell, not the
    // trailing last column.
    const cell = p.locator('.mp-bucket-positions .bucket-grid .ag-row .ag-cell[col-id="ltp"]').first();
    const count = await cell.count();
    test.skip(count === 0, 'no LTP cells rendered in Positions grid');

    const style = await cell.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { width: cs.borderRightWidth, color: cs.borderRightColor, style: cs.borderRightStyle };
    });

    // (a) visible-but-low-opacity vertical border present.
    expect(style.style, `border-right-style: ${style.style}`).toBe('solid');
    expect(Number.parseFloat(style.width), `border-right-width: ${style.width}`).toBeGreaterThan(0);

    // (b) opacity ceiling — must read as a "whisper", not a hard grid line.
    const alpha = _parseAlpha(style.color);
    expect(alpha, `could not parse alpha from ${style.color}`).not.toBeNull();
    expect(alpha, `border-right-color: ${style.color} (alpha ${alpha})`).toBeGreaterThan(0);
    expect(alpha, `border-right-color: ${style.color} (alpha ${alpha}) exceeds whisper ceiling`).toBeLessThanOrEqual(0.25);
  });

  test('MarketPulse Positions grid: pinned symbol cell keeps its own colour edge, not the plain hairline', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.mp-bucket-positions .bucket-grid .ag-row', { timeout: 20_000 }).catch(() => {});

    const symCell = p.locator('.mp-bucket-positions .bucket-grid .ag-row .ag-cell.ag-col-sym').first();
    const count = await symCell.count();
    test.skip(count === 0, 'no symbol cells rendered in Positions grid');

    const width = await symCell.evaluate((el) => getComputedStyle(el).borderRightWidth);
    expect(Number.parseFloat(width), `symbol cell border-right: ${width} — must stay 0 to avoid doubling with its direction-bar edge`).toBe(0);
  });

  test('MarketPulse TOTAL row: cells stay divider-free (restoring the whisper hairline must not regress the earlier TOTAL-row-no-border decision)', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/pulse', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.mp-bucket-positions .bucket-grid .ag-row', { timeout: 20_000 }).catch(() => {});

    const totalCell = p.locator('.ag-theme-algo .mp-total-row .ag-cell').first();
    const count = await totalCell.count();
    test.skip(count === 0, 'no TOTAL row rendered (no positions/holdings in this session)');

    const width = await totalCell.evaluate((el) => getComputedStyle(el).borderRightWidth);
    expect(Number.parseFloat(width), `mp-total-row cell border-right: ${width} — TOTAL stratum must stay divider-free (total_row_muted_colors_no_border.spec.js contract)`).toBe(0);
  });

  test('PerformancePage (public, ag-theme-ramboq): cell border is unaffected by the algo whisper hairline', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/performance', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.ag-theme-ramboq .ag-row', { timeout: 20_000 }).catch(() => {});

    const cell = p.locator('.ag-theme-ramboq .ag-cell').first();
    const count = await cell.count();
    test.skip(count === 0, 'no live cells rendered on /performance');

    const color = await cell.evaluate((el) => getComputedStyle(el).borderRightColor);
    // Cream-theme literal (rgb(209,213,219)) — must NOT have picked up
    // the algo dark-theme's rgba(126,151,184,...) hairline colour.
    expect(color, `.ag-theme-ramboq cell border-right-color: ${color}`).toBe('rgb(209, 213, 219)');
  });
});

/* ── Cross-surface parity fix (2026-09) — LIVE checks, own serial block.
 * Deliberately NOT folded into the "whisper cell hairline (live)" block
 * above: that block's `.serial` mode means ANY earlier test failure
 * (e.g. the pre-existing, unrelated PerformancePage ag-theme-ramboq
 * failure) skips every test queued after it, which would silently
 * prevent these regression checks from ever running. A separate block
 * with its own login/page keeps this fix's checks live regardless of
 * that pre-existing flake. */
test.describe.serial('algo consistency — whisper vertical divider parity, hand-rolled grids (2026-09, live)', () => {
  test.setTimeout(90_000);

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

  /** Synthetic detached `.ag-theme-algo .ag-cell` — same reference
   *  pattern the `.cell-muted` derivation test uses. Doesn't depend on
   *  any real grid having rendered rows. */
  async function ssotDividerValue(p) {
    return p.evaluate(() => {
      const container = document.createElement('div');
      container.className = 'ag-theme-algo';
      const cell = document.createElement('div');
      cell.className = 'ag-cell';
      container.appendChild(cell);
      document.body.appendChild(container);
      const cs = getComputedStyle(cell);
      const out = { width: cs.borderRightWidth, color: cs.borderRightColor };
      document.body.removeChild(container);
      return out;
    });
  }

  test('derivatives Legs tab: LTP cell (own right edge undecorated) gets the divider — catches the "gap" regression', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.cand-row', { timeout: 20_000 }).catch(() => {});

    const ref = await ssotDividerValue(p);
    const cell = p.locator('.cand-row:not(.cand-row-total) > span.leg-ltp').first();
    const count = await cell.count();
    test.skip(count === 0, 'no Legs rows rendered this session (empty book)');

    const got = await cell.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { width: cs.borderRightWidth, color: cs.borderRightColor };
    });
    expect(got.width, `Legs LTP cell border-right-width: ${got.width}`).toBe(ref.width);
    expect(got.color, `Legs LTP cell border-right-color: ${got.color}`).toBe(ref.color);
  });

  test('derivatives Legs tab: Chg% cell keeps its own single edge, not doubled by the new divider', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.cand-row', { timeout: 20_000 }).catch(() => {});

    const cell = p.locator('.cand-row:not(.cand-row-total) > span.cand-chg-sep').first();
    const count = await cell.count();
    test.skip(count === 0, 'no Legs rows rendered this session (empty book)');

    const style = await cell.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { width: cs.borderRightWidth, boxShadow: cs.boxShadow };
    });
    // The new whisper divider must NOT have been added here — this
    // cell's own right edge is already decorated via box-shadow.
    expect(style.width, `Legs Chg% cell border-right-width: ${style.width} — must stay 0 to avoid doubling with its own box-shadow edge`).toBe('0px');
    expect(style.boxShadow, `Legs Chg% cell box-shadow: ${style.boxShadow}`).toMatch(/rgba\(126,\s*151,\s*184,\s*0\.4\)/);
  });

  test('derivatives Snapshot tab: LTP cell gets the divider, Chg% cell stays single-edged', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.byund-row', { timeout: 20_000 }).catch(() => {});

    const ref = await ssotDividerValue(p);

    const ltpCell = p.locator('.byund-row:not(.byund-row-total) > span:not(.byund-und):not(.byund-chg-sep)').first();
    const count = await ltpCell.count();
    test.skip(count === 0, 'no Snapshot rows rendered this session (empty book)');
    const ltpStyle = await ltpCell.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { width: cs.borderRightWidth, color: cs.borderRightColor };
    });
    expect(ltpStyle.width, `Snapshot LTP cell border-right-width: ${ltpStyle.width}`).toBe(ref.width);
    expect(ltpStyle.color, `Snapshot LTP cell border-right-color: ${ltpStyle.color}`).toBe(ref.color);

    const chgCell = p.locator('.byund-row:not(.byund-row-total) > span.byund-chg-sep').first();
    const chgWidth = await chgCell.evaluate((el) => getComputedStyle(el).borderRightWidth);
    expect(chgWidth, `Snapshot Chg% cell border-right-width: ${chgWidth} — must stay 0 to avoid doubling with its own box-shadow edge`).toBe('0px');
  });

  test('derivatives Snapshot TOTAL row: stays undecorated (matches ag-Grid totals-row exclusion)', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.byund-row-total', { timeout: 20_000 }).catch(() => {});

    const cell = p.locator('.byund-row-total > span').first();
    const count = await cell.count();
    test.skip(count === 0, 'no Snapshot TOTAL row rendered this session (empty book)');

    const width = await cell.evaluate((el) => getComputedStyle(el).borderRightWidth);
    expect(width, `TOTAL row border-right-width: ${width}`).toBe('0px');
  });

  test('dashboard NavBreakdown: ag-Grid cell already resolves the shared SSOT border-right', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.nav-bd-ag .ag-cell', { timeout: 20_000 }).catch(() => {});

    const ref = await ssotDividerValue(p);
    const cell = p.locator('.nav-bd-ag .ag-cell:not(.ag-column-last)').first();
    const count = await cell.count();
    test.skip(count === 0, 'no NavBreakdown rows rendered this session');

    const got = await cell.evaluate((el) => {
      const cs = getComputedStyle(el);
      return { width: cs.borderRightWidth, color: cs.borderRightColor };
    });
    expect(got.width, `NavBreakdown cell border-right-width: ${got.width}`).toBe(ref.width);
    expect(got.color, `NavBreakdown cell border-right-color: ${got.color}`).toBe(ref.color);
  });
});

/* ── Close/dismiss icon-button rest-state fill (2026-09) ──────────────
 * Operator complaint: every modal/drawer/popup "×" close button rendered
 * as a bare bordered (or borderless) box at REST, only gaining a
 * background tint on :hover — reads as broken/unfinished. Fix: filled
 * at rest, stepping to a stronger tint on hover, via the shared
 * --close-btn-danger/-info/-neutral-bg(-hover) token pairs (app.css).
 * Each `*-close` button keeps its OWN accent colour (danger red /
 * info cyan / neutral slate) — only the "has a fill at rest" property
 * is standardised, not the colour itself.
 * ─────────────────────────────────────────────────────────────────── */

const CLOSE_BTN_SITES = [
  { file: 'src/lib/BrokerHealthBadge.svelte', cls: '.bh-close', family: 'danger' },
  { file: 'src/lib/PositionStrip.svelte', cls: '.ps-bd-close', family: 'danger' },
  { file: 'src/lib/AgentFireModal.svelte', cls: '.afm-close', family: 'danger' },
  { file: 'src/lib/SymbolPanel.svelte', cls: '.oes-close', family: 'danger' },
  { file: 'src/lib/order/OrderPairModal.svelte', cls: '.opm-close', family: 'danger' },
  { file: 'src/lib/ChartModal.svelte', cls: '.cm-close', family: 'danger' },
  { file: 'src/lib/TourModal.svelte', cls: '.tour-close', family: 'danger' },
  { file: 'src/lib/LogPanel.svelte', cls: '.lp-close-btn', family: 'danger' },
  { file: 'src/lib/MarketPulse.svelte', cls: ':global(.search-close)', family: 'danger' },
  { file: 'src/lib/order/OrderTicket.svelte', cls: '.ot-close', family: 'info' },
  { file: 'src/lib/order/OrderTicket.svelte', cls: '.ot-demo-close', family: 'neutral' },
  { file: 'src/routes/(algo)/admin/+page.svelte', cls: '.ip-modal-x', family: 'neutral' },
  { file: 'src/routes/(algo)/admin/metrics/+page.svelte', cls: '.metrics-modal-close', family: 'neutral' },
  { file: 'src/lib/PnlAnalysis.svelte', cls: '.modal-x', family: 'neutral' },
  { file: 'src/lib/order/OrderTimelineDrawer.svelte', cls: '.otd-close', family: 'neutral' },
  { file: 'src/lib/ShortcutCheatsheet.svelte', cls: '.sc-close', family: 'neutral' },
];

test.describe('algo consistency — close-button rest-state fill (source)', () => {
  test('app.css defines the three close-btn token pairs', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/app.css'), 'utf-8');
    for (const t of [
      '--close-btn-danger-bg', '--close-btn-danger-bg-hover',
      '--close-btn-info-bg', '--close-btn-info-bg-hover',
      '--close-btn-neutral-bg', '--close-btn-neutral-bg-hover',
    ]) {
      expect(src, `${t} must be declared in app.css`).toContain(t + ':');
    }
  });

  test('every close-button site fills its rest state via a --close-btn-* token (no bare transparent/none)', () => {
    const offenders = [];
    for (const site of CLOSE_BTN_SITES) {
      const abs = path.join(process.cwd(), site.file);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { offenders.push(`${site.file}: file not found`); continue; }
      // Isolate the rule body for this exact class (first match — the
      // component-scoped declaration, not any :hover companion rule).
      const escaped = site.cls.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      const ruleRx = new RegExp(escaped + '\\s*\\{([^}]*)\\}');
      const m = src.match(ruleRx);
      if (!m) { offenders.push(`${site.file}: rule ${site.cls} not found`); continue; }
      const body = m[1];
      if (/background:\s*(transparent|none)\s*;/.test(body)) {
        offenders.push(`${site.file} ${site.cls}: still declares background: transparent/none at rest`);
      }
      if (!new RegExp(`var\\(--close-btn-${site.family}-bg\\)`).test(body)) {
        offenders.push(`${site.file} ${site.cls}: rest state must use var(--close-btn-${site.family}-bg)`);
      }
    }
    expect(offenders, `Close-button rest-fill regression:\n${offenders.join('\n')}`).toEqual([]);
  });

  test('every close-button hover rule steps up to the matching -hover token', () => {
    // .ip-modal-x is a deliberate exception: its rest fill is the neutral
    // (slate) token, but its :hover deliberately KEEPS the pre-existing
    // literal red tint (rgba(248,113,113,0.12) + #fca5a5 text) — a
    // "close = danger on hover" affordance distinct from its neutral rest
    // identity. Operator instruction: fix the missing rest fill only,
    // leave this button's hover behaviour untouched.
    const HOVER_EXCEPTIONS = new Set(['.ip-modal-x']);
    const offenders = [];
    for (const site of CLOSE_BTN_SITES) {
      if (HOVER_EXCEPTIONS.has(site.cls)) continue;
      const abs = path.join(process.cwd(), site.file);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      if (!new RegExp(`var\\(--close-btn-${site.family}-bg-hover\\)`).test(src)) {
        offenders.push(`${site.file} ${site.cls}: :hover must reference var(--close-btn-${site.family}-bg-hover)`);
      }
    }
    expect(offenders, `Close-button hover-step regression:\n${offenders.join('\n')}`).toEqual([]);
  });
});

test.describe.serial('algo consistency — close-button rest-state fill (live)', () => {
  test.setTimeout(60_000);

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

  /** Parses `rgba(r, g, b, a)` (or opaque `rgb(...)`) and returns the alpha
   *  channel (1 when omitted). */
  function alphaOf(rgbaStr) {
    const m = /rgba?\(\s*[\d.]+\s*,\s*[\d.]+\s*,\s*[\d.]+\s*(?:,\s*([\d.]+))?\)/.exec(rgbaStr || '');
    if (!m) return null;
    return m[1] == null ? 1 : Number.parseFloat(m[1]);
  }

  test('BrokerHealthBadge close button: rest-state background has non-zero alpha, hover steps up', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    const chip = p.locator('.broker-chip');
    await chip.first().waitFor({ state: 'visible', timeout: 15_000 }).catch(() => {});
    if (!(await chip.count())) { test.skip(true, 'broker-chip not present on this session'); return; }
    await chip.first().click();
    const closeBtn = p.locator('.bh-close');
    await closeBtn.first().waitFor({ state: 'visible', timeout: 10_000 }).catch(() => {});
    if (!(await closeBtn.count())) { test.skip(true, '.bh-close not rendered'); return; }

    // Move the mouse off the button before sampling rest state.
    await p.mouse.move(0, 0);
    const restBg = await closeBtn.first().evaluate((el) => getComputedStyle(el).backgroundColor);
    const restAlpha = alphaOf(restBg);
    expect(restAlpha, `.bh-close rest background: ${restBg}`).not.toBeNull();
    expect(restAlpha, `.bh-close rest background alpha must be > 0 (filled, not transparent): ${restBg}`).toBeGreaterThan(0);

    await closeBtn.first().hover();
    await p.waitForTimeout(200); // past the 120ms background transition
    const hoverBg = await closeBtn.first().evaluate((el) => getComputedStyle(el).backgroundColor);
    const hoverAlpha = alphaOf(hoverBg);
    expect(hoverAlpha, `.bh-close hover background: ${hoverBg}`).not.toBeNull();
    expect(hoverAlpha, `.bh-close hover alpha (${hoverAlpha}) must exceed rest alpha (${restAlpha})`).toBeGreaterThan(restAlpha);
  });

  test('ShortcutCheatsheet close button ("?"): rest-state background has non-zero alpha, hover steps up', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    await p.locator('body').click({ position: { x: 5, y: 5 } }).catch(() => {});
    await p.keyboard.press('?');
    const closeBtn = p.locator('.sc-close');
    await closeBtn.first().waitFor({ state: 'visible', timeout: 10_000 }).catch(() => {});
    if (!(await closeBtn.count())) { test.skip(true, 'cheatsheet did not open'); return; }

    await p.mouse.move(0, 0);
    const restBg = await closeBtn.first().evaluate((el) => getComputedStyle(el).backgroundColor);
    const restAlpha = alphaOf(restBg);
    expect(restAlpha, `.sc-close rest background: ${restBg}`).not.toBeNull();
    expect(restAlpha, `.sc-close rest background alpha must be > 0 (filled, not transparent): ${restBg}`).toBeGreaterThan(0);

    await closeBtn.first().hover();
    await p.waitForTimeout(200);
    const hoverBg = await closeBtn.first().evaluate((el) => getComputedStyle(el).backgroundColor);
    const hoverAlpha = alphaOf(hoverBg);
    expect(hoverAlpha, `.sc-close hover alpha (${hoverAlpha}) must exceed rest alpha (${restAlpha})`).toBeGreaterThan(restAlpha);
  });
});

/* ── Build/Config-group font-size consistency audit (2026-09) ─────────
 * Operator-requested sweep of the Automation, Sandbox, Strategies pages
 * plus the "Build" (Activity/Console/Research/Tokens) and "Config"
 * (Brokers/Settings/Users/Statements/History/Audit/Metrics/Perf/Health)
 * nav groups — pages that hadn't been covered by the earlier Dashboard/
 * Pulse/Derivatives consistency passes. Found + fixed three literal
 * (non-`var(--fs-*)`) font-size drift sites:
 *   - admin/health: .kv-row/.broker-row/.ip-row hardcoded 0.72rem
 *     (exact duplicate of --fs-lg) -> var(--fs-lg).
 *   - admin/tokens: `.algo-table thead th` local override at 0.68rem,
 *     diverging from the canonical `.algo-table thead th` SSOT in
 *     app.css (var(--fs-sm)/0.6rem) — removed so it matches
 *     admin/settings, the only other `.algo-table` consumer.
 *   - admin/mcp: `.thr-sym` hardcoded 1rem (no matching token)
 *     -> var(--fs-xl), the "title cluster" tier.
 * Every other page in the sweep (Automation, Sandbox, Strategies,
 * Activity, Console, Tokens body, Brokers, Settings, Users,
 * Statements, History, Audit, Metrics, Perf) already used
 * var(--fs-*) exclusively — checked-and-clean, no fix needed.
 * Also fixed: /admin/execution page header + <title> said "Lab" while
 * its own nav entry reads "Sandbox" (URL kept at /admin/execution for
 * backward-compat — see (algo)/+layout.svelte _algoLinksAll comment).
 * ─────────────────────────────────────────────────────────────────── */

const FONT_AUDIT_PAGES = [
  'src/routes/(algo)/automation/+page.svelte',
  'src/routes/(algo)/admin/execution/+page.svelte',
  'src/routes/(algo)/strategies/+page.svelte',
  'src/routes/(algo)/activity/+page.svelte',
  'src/routes/(algo)/console/+page.svelte',
  'src/routes/(algo)/admin/mcp/+page.svelte',
  'src/routes/(algo)/admin/tokens/+page.svelte',
  'src/routes/(algo)/admin/brokers/+page.svelte',
  'src/routes/(algo)/admin/settings/+page.svelte',
  'src/routes/(algo)/admin/+page.svelte',
  'src/routes/(algo)/admin/statements/+page.svelte',
  'src/routes/(algo)/admin/history/+page.svelte',
  'src/routes/(algo)/admin/audit/+page.svelte',
  'src/routes/(algo)/admin/metrics/+page.svelte',
  'src/routes/(algo)/admin/perf/+page.svelte',
  'src/routes/(algo)/admin/health/+page.svelte',
];

test.describe('algo consistency — Build/Config font-size audit (source)', () => {
  test('no literal (non-token) font-size regressions across the audited page group', () => {
    // Same contract as the top-of-file stale-code guard, scoped to this
    // page set, with the two known/allowed non-role literals (both
    // close-button glyph sizes, covered by the Task-1 close-btn guard
    // above, not page-content text) excluded.
    const ALLOWED = new Set([
      'src/routes/(algo)/admin/+page.svelte::font-size: 1.1rem;',            // .ip-modal-x close glyph
      'src/routes/(algo)/admin/metrics/+page.svelte::font-size: 1.4rem;',    // .metrics-modal-close glyph
    ]);
    const offenders = [];
    for (const rel of FONT_AUDIT_PAGES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      const stripped = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '');
      const rx = /font-size\s*:\s*[0-9.]+(?:rem|em|px)\s*;/g;
      let m;
      while ((m = rx.exec(stripped))) {
        const key = `${rel}::${m[0]}`;
        if (!ALLOWED.has(key)) offenders.push(key);
      }
    }
    expect(offenders, `Literal font-size regression (must use var(--fs-*)):\n${offenders.join('\n')}`).toEqual([]);
  });

  test('admin/health kv/broker/ip rows use var(--fs-lg), not the old 0.72rem literal', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/admin/health/+page.svelte'), 'utf-8');
    for (const cls of ['.kv-row', '.broker-row', '.ip-row']) {
      const m = src.match(new RegExp(cls.replace('.', '\\.') + '\\s*\\{([^}]*)\\}'));
      expect(m, `${cls} rule not found`).not.toBeNull();
      expect(m[1], `${cls} must use var(--fs-lg)`).toMatch(/font-size:\s*var\(--fs-lg\)/);
    }
  });

  test('admin/tokens .algo-table thead th no longer overrides the canonical SSOT size', () => {
    const raw = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/admin/tokens/+page.svelte'), 'utf-8');
    // Strip comments — the migration-note comment documents the OLD
    // value in prose ("a bespoke 0.68rem override"), which must not
    // trip this live-code guard.
    const src = raw.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '');
    expect(src, 'the bespoke 0.68rem thead-th override must be removed').not.toContain('0.68rem');
  });

  test('admin/mcp .thr-sym uses var(--fs-xl), not the old 1rem literal', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/admin/mcp/+page.svelte'), 'utf-8');
    const m = src.match(/\.thr-sym\s*\{([^}]*)\}/);
    expect(m, '.thr-sym rule not found').not.toBeNull();
    expect(m[1], '.thr-sym must use var(--fs-xl)').toMatch(/font-size:\s*var\(--fs-xl\)/);
  });

  test('admin/execution header + title say "Sandbox", not the stale "Lab"', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/admin/execution/+page.svelte'), 'utf-8');
    expect(src).toContain('<h1 class="page-title-chip">Sandbox</h1>');
    expect(src).toContain('<title>Sandbox | RamboQuant Analytics</title>');
    // Internal-only references (comments, console.warn prefixes) are
    // out of scope — only the user-visible header + tab title changed.
  });
});

test.describe.serial('algo consistency — Build/Config font-size audit (live)', () => {
  test.setTimeout(60_000);

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

  test('/admin/execution renders "Sandbox" as its page title, matching the nav label', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/admin/execution', { waitUntil: 'domcontentloaded' });
    await expect(p.locator('h1.page-title-chip')).toHaveText('Sandbox', { timeout: 10_000 });
    await expect(p).toHaveTitle('Sandbox | RamboQuant Analytics');
  });

  test('admin/health KV row computed font-size matches --fs-lg (0.72rem)', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/admin/health', { waitUntil: 'domcontentloaded' });
    // Health data loads async (broker/system status fetch) — wait for the
    // first KV row rather than assuming it's present at domcontentloaded.
    await p.waitForSelector('.kv-row', { timeout: 15_000 }).catch(() => {});
    const row = p.locator('.kv-row').first();
    const count = await row.count();
    test.skip(count === 0, 'no .kv-row rendered on /admin/health');
    const fs_ = await row.evaluate((el) => getComputedStyle(el).fontSize);
    expect(fs_, `.kv-row computed font-size: ${fs_}`).toBe('11.52px'); // 0.72rem @ 16px root
  });
});

/* ── Monitor nav-group font-size consistency audit (2026-09) ──────────
 * Follow-up to the Build/Config sweep (f9270635) — this time auditing
 * the group that SERVED as that pass's baseline reference (Pulse,
 * Dashboard, Derivatives) plus the remaining Monitor-group pages
 * (Orders, Charts, Automation, Strategies), verified rather than
 * assumed clean. `/showcase` ("About") also carries `group: 'monitor'`
 * in (algo)/+layout.svelte's nav table but is a narrative "Tour" page
 * (bespoke rgba() literals throughout, not the --fs-* dark-terminal
 * system — see the A3-sweep comment marking it a documentation
 * exception) — confirmed out of scope, not silently dropped.
 *
 * Pulse and Charts have almost no CSS of their own (thin wrappers) —
 * auditing only the `+page.svelte` file would be vacuous, so the page
 * set below includes each page's exclusive rendering component:
 *   Pulse       -> MarketPulse.svelte, data/pulseColumns.js
 *   Orders      -> OrderBook.svelte
 *   Derivatives -> CandidateLegRow.svelte
 *   Charts      -> ChartWorkspace.svelte
 *   Automation  -> AutomationTabs.svelte (embedded via <AutomationTabs />)
 * Shared multi-surface lib components reachable from these pages
 * (NavBreakdown, NavCard, PnlAnalysis, CardHeader, etc.) are NOT
 * included — NavBreakdown/NavCard are mid-investigation for a separate
 * FIRM-NAV-load-latency report and must not be touched here; the
 * others are cross-cutting SSOT already covered by the Phase-1/2
 * palette guards above.
 *
 * Found and fixed nine literal (non-`var(--fs-*)`) font-size drift
 * sites:
 *   - data/pulseColumns.js: STALE@HH:MM badge inline style, 9px
 *     (nearest --fs-xs/0.55rem=8.8px) -> var(--fs-xs).
 *   - dashboard: `.fs-card-on .eq-stat-v` (fullscreen hero P&L value)
 *     hardcoded 1.4rem while its own mobile media-query sibling
 *     already used var(--fs-xl) -> var(--fs-2xl) (1.55rem, the
 *     documented headline tier — the nearest-magnitude token for a
 *     magnified fullscreen stat, +2.4px/~10%).
 *   - dashboard: `.dash-nav-err` hardcoded 0.72rem (exact --fs-lg
 *     duplicate) -> var(--fs-lg).
 *   - dashboard: `.dash-nav-retry` hardcoded 0.68rem (nearest --fs-md/
 *     0.65rem) -> var(--fs-md).
 *   - OrderBook.svelte `.ob-count` hardcoded 0.65rem (exact --fs-md
 *     duplicate) -> var(--fs-md).
 *   - derivatives `.leg-pair-btn` hardcoded 0.75rem (nearest --fs-lg/
 *     0.72rem) -> var(--fs-lg).
 *   - derivatives `.byund-table` hardcoded 0.72rem (exact --fs-lg
 *     duplicate, with a stale/misleading "match Pulse Positions
 *     ~0.625rem" comment removed) -> var(--fs-lg).
 *   - derivatives `.cand-headrow` hardcoded 0.65rem (exact --fs-md
 *     duplicate) -> var(--fs-md).
 *   - CandidateLegRow.svelte `.cand-state-cell` hardcoded 9px (nearest
 *     --fs-xs) -> var(--fs-xs); `.cand-row` hardcoded 0.72rem (exact
 *     --fs-lg duplicate) -> var(--fs-lg).
 *   - ChartWorkspace.svelte `.chart-partial-hint` hardcoded 11px
 *     (nearest --fs-lg/0.72rem=11.52px, +0.52px) -> var(--fs-lg).
 *
 * One literal was found, classified, and DELIBERATELY left unfixed:
 *   - OrderBook.svelte `.ob-sc-n` (chase-queue count number) at 1.1rem
 *     falls in the real gap between --fs-xl (0.85rem/13.6px, -23%)
 *     and --fs-2xl (1.55rem/24.8px, +41%) — no existing token lands
 *     within ~4px without a visible resize of a live trading-surface
 *     counter. Documented inline at the declaration site; guarded
 *     below so it can't silently drift further without this test
 *     noticing.
 *
 * Every other file/selector in the audited set (Pulse `+page.svelte`,
 * MarketPulse.svelte, dashboard's other font-size rules, Orders
 * `+page.svelte`, derivatives' remaining ~60 font-size sites, Charts
 * `+page.svelte`, Automation `+page.svelte` + AutomationTabs.svelte,
 * Strategies `+page.svelte`) already used var(--fs-*) exclusively —
 * checked-and-clean, no changes needed. Font-weight values across the
 * whole group remain on the standard 400/500/600/700/800 scale (no
 * off-scale literal found).
 *
 * Automation/Strategies cross-check: `git log f9270635..HEAD` touches
 * only one file relevant to this tree, `6368bcea` (public hero-title
 * mobile fix, unrelated route) — confirms the Build/Config audit's
 * "checked-and-clean" finding for these two pages still holds after
 * the close-icon + cell-divider fixes that landed in the interim.
 * ─────────────────────────────────────────────────────────────────── */

const MONITOR_FONT_AUDIT_PAGES = [
  'src/routes/(algo)/pulse/+page.svelte',
  'src/lib/MarketPulse.svelte',
  'src/lib/data/pulseColumns.js',
  'src/routes/(algo)/dashboard/+page.svelte',
  'src/routes/(algo)/orders/+page.svelte',
  'src/lib/OrderBook.svelte',
  'src/routes/(algo)/admin/derivatives/+page.svelte',
  'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte',
  'src/routes/(algo)/charts/+page.svelte',
  'src/lib/ChartWorkspace.svelte',
  'src/routes/(algo)/automation/+page.svelte',
  'src/lib/AutomationTabs.svelte',
  'src/routes/(algo)/strategies/+page.svelte',
];

test.describe('algo consistency — Monitor-group font-size audit (source)', () => {
  test('no literal (non-token) font-size regressions across the audited Monitor page/component set', () => {
    // Same contract as the Build/Config sweep's guard, scoped to this
    // page set. .ob-sc-n's 1.1rem is a documented, deliberate exception
    // (see comment at its declaration) — not a regression.
    const ALLOWED = new Set([
      'src/lib/OrderBook.svelte::font-size: 1.1rem;',   // .ob-sc-n — no token fits within ~4px, see inline comment
    ]);
    const offenders = [];
    for (const rel of MONITOR_FONT_AUDIT_PAGES) {
      const abs = path.join(process.cwd(), rel);
      let src;
      try { src = fs.readFileSync(abs, 'utf-8'); } catch { continue; }
      const stripped = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/<!--[\s\S]*?-->/g, '');
      const rx = /font-size\s*:\s*[0-9.]+(?:rem|em|px)\s*;/g;
      let m;
      while ((m = rx.exec(stripped))) {
        const key = `${rel}::${m[0]}`;
        if (!ALLOWED.has(key)) offenders.push(key);
      }
    }
    expect(offenders, `Literal font-size regression (must use var(--fs-*)):\n${offenders.join('\n')}`).toEqual([]);
  });

  test('data/pulseColumns.js STALE badge uses var(--fs-xs), not the old 9px literal', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib/data/pulseColumns.js'), 'utf-8');
    expect(src).toContain("font-size:var(--fs-xs)");
    expect(src).not.toContain('font-size:9px');
  });

  test('dashboard fullscreen hero P&L value uses var(--fs-2xl), not the old 1.4rem literal', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/dashboard/+page.svelte'), 'utf-8');
    const m = src.match(/\.fs-card-on \.eq-stat-v \{ font-size:\s*([^;]+);/);
    expect(m, '.fs-card-on .eq-stat-v rule not found').not.toBeNull();
    expect(m[1].trim()).toBe('var(--fs-2xl)');
  });

  test('dashboard .dash-nav-err / .dash-nav-retry use var(--fs-lg)/var(--fs-md), not the old 0.72rem/0.68rem literals', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/dashboard/+page.svelte'), 'utf-8');
    const errM = src.match(/\.dash-nav-err\s*\{([^}]*)\}/);
    const retryM = src.match(/\.dash-nav-retry\s*\{([^}]*)\}/);
    expect(errM, '.dash-nav-err rule not found').not.toBeNull();
    expect(retryM, '.dash-nav-retry rule not found').not.toBeNull();
    expect(errM[1]).toMatch(/font-size:\s*var\(--fs-lg\)/);
    expect(retryM[1]).toMatch(/font-size:\s*var\(--fs-md\)/);
  });

  test('OrderBook.svelte .ob-count uses var(--fs-md); .ob-sc-n keeps its documented 1.1rem exception', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib/OrderBook.svelte'), 'utf-8');
    const countM = src.match(/\.ob-count\s*\{([^}]*)\}/);
    expect(countM, '.ob-count rule not found').not.toBeNull();
    expect(countM[1]).toMatch(/font-size:\s*var\(--fs-md\)/);

    // The exception must stay documented — if the comment disappears,
    // a future edit may have silently "fixed" it without re-litigating
    // the token-gap tradeoff explained there.
    expect(src, '.ob-sc-n token-gap rationale comment must stay attached')
      .toMatch(/no existing token lands within[\s\S]{0,20}~4px[\s\S]{0,400}\.ob-sc-n \{[\s\S]{0,60}font-size:\s*1\.1rem;/);
  });

  test('derivatives .leg-pair-btn / .byund-table / .cand-headrow use var(--fs-*), not the old literals', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/admin/derivatives/+page.svelte'), 'utf-8');
    const legPairM = src.match(/\.leg-pair-btn\s*\{([^}]*)\}/);
    expect(legPairM, '.leg-pair-btn rule not found').not.toBeNull();
    expect(legPairM[1]).toMatch(/font-size:\s*var\(--fs-lg\)/);

    // .cand-headrow is declared TWICE (layout rule + typography rule —
    // see the "headrow is scoped here" comment) — the fixed literal
    // lives in the SECOND block, so scan every `.cand-headrow { … }`
    // occurrence rather than assuming .match() picks the right one.
    const candHeadBlocks = [...src.matchAll(/\.cand-headrow\s*\{([^}]*)\}/g)].map(m => m[1]);
    expect(candHeadBlocks.length, '.cand-headrow rule(s) not found').toBeGreaterThan(0);
    expect(candHeadBlocks.some(b => /font-size:\s*var\(--fs-md\)/.test(b)),
      `no .cand-headrow block uses var(--fs-md):\n${candHeadBlocks.join('\n---\n')}`).toBe(true);
    expect(candHeadBlocks.some(b => /font-size:\s*0\.65rem/.test(b)),
      'a .cand-headrow block still has the old 0.65rem literal').toBe(false);

    // .byund-table's selector is a multi-line grid-template-columns
    // block ending in the font-size decl — match on the literal
    // fragment directly plus the removed stale comment.
    expect(src).toContain('font-size: var(--fs-lg);\n  }\n  .byund-headrow');
    expect(src).not.toContain('match Pulse Positions ~0.625rem');
  });

  test('CandidateLegRow.svelte .cand-state-cell / .cand-row use var(--fs-*), not the old 9px/0.72rem literals', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte'), 'utf-8');
    const stateM = src.match(/\.cand-state-cell\s*\{([^}]*)\}/);
    const rowM = src.match(/\.cand-row\s*\{([^}]*)\}/);
    expect(stateM, '.cand-state-cell rule not found').not.toBeNull();
    expect(rowM, '.cand-row rule not found').not.toBeNull();
    expect(stateM[1]).toMatch(/font-size:\s*var\(--fs-xs\)/);
    expect(rowM[1]).toMatch(/font-size:\s*var\(--fs-lg\)/);
  });

  test('ChartWorkspace.svelte .chart-partial-hint uses var(--fs-lg), not the old 11px literal', () => {
    const src = fs.readFileSync(path.join(process.cwd(), 'src/lib/ChartWorkspace.svelte'), 'utf-8');
    const m = src.match(/\.chart-partial-hint\s*\{([^}]*)\}/);
    expect(m, '.chart-partial-hint rule not found').not.toBeNull();
    expect(m[1]).toMatch(/font-size:\s*var\(--fs-lg\)/);
  });

  test('Automation/Strategies remain clean post Build/Config audit (no font-size regression introduced since f9270635)', () => {
    const autoSrc = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/automation/+page.svelte'), 'utf-8');
    const tabsSrc = fs.readFileSync(path.join(process.cwd(), 'src/lib/AutomationTabs.svelte'), 'utf-8');
    const stratSrc = fs.readFileSync(path.join(process.cwd(), 'src/routes/(algo)/strategies/+page.svelte'), 'utf-8');
    const rx = /font-size\s*:\s*[0-9.]+(?:rem|em|px)\s*;/;
    for (const [name, src] of [['automation/+page.svelte', autoSrc], ['AutomationTabs.svelte', tabsSrc], ['strategies/+page.svelte', stratSrc]]) {
      expect(rx.test(src.replace(/\/\*[\s\S]*?\*\//g, '')), `${name} must have no literal font-size`).toBe(false);
    }
  });
});

test.describe.serial('algo consistency — Monitor-group font-size audit (live)', () => {
  test.setTimeout(90_000);

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

  /**
   * Proves a REAL rendered element's font-size derives from the named
   * --fs-* token (not a coincidentally-matching literal). Unlike the
   * `.cell-muted` derivation test above (which targets a `:global(...)`
   * rule), the selectors here are Svelte per-component SCOPED styles —
   * a synthetic element appended to <body> would lack the compiler's
   * `svelte-xxxxx` scoping attribute and never match the rule at all,
   * so this reads the token override off the already-rendered element
   * instead. CSS custom properties inherit through the cascade
   * regardless of Svelte's attribute-selector scoping, so overriding
   * at :root reaches every real element on the page.
   */
  async function tokenDerivationCheck(locator, tokenName) {
    return locator.evaluate((el, tokenName) => {
      document.documentElement.style.setProperty(tokenName, '37px');
      const resolved = getComputedStyle(el).fontSize;
      document.documentElement.style.removeProperty(tokenName);
      return resolved;
    }, tokenName);
  }

  test('dashboard .dash-nav-retry font-size tracks --fs-md (not a frozen literal)', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/dashboard', { waitUntil: 'domcontentloaded' });
    // .dash-nav-retry only renders when the firm-NAV poll is erroring —
    // not guaranteed on a healthy session. Skip rather than fail when
    // the strip isn't present.
    const el = p.locator('.dash-nav-retry').first();
    await el.waitFor({ state: 'attached', timeout: 5_000 }).catch(() => {});
    test.skip(await el.count() === 0, '.dash-nav-retry not rendered (no NAV fetch error this session)');
    const resolved = await tokenDerivationCheck(el, '--fs-md');
    expect(resolved, `.dash-nav-retry font-size under --fs-md override: ${resolved}`).toBe('37px');
  });

  test('derivatives .cand-headrow font-size tracks --fs-md (not a frozen literal)', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/admin/derivatives', { waitUntil: 'domcontentloaded' });
    const el = p.locator('.cand-headrow').first();
    await el.waitFor({ state: 'attached', timeout: 15_000 }).catch(() => {});
    test.skip(await el.count() === 0, '.cand-headrow not rendered (Legs/Expiry tab not populated this session)');
    const resolved = await tokenDerivationCheck(el, '--fs-md');
    expect(resolved, `.cand-headrow font-size under --fs-md override: ${resolved}`).toBe('37px');
  });

  test('Charts .chart-partial-hint font-size tracks --fs-lg (not a frozen literal)', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/charts', { waitUntil: 'domcontentloaded' });
    const el = p.locator('.chart-partial-hint').first();
    await el.waitFor({ state: 'attached', timeout: 10_000 }).catch(() => {});
    test.skip(await el.count() === 0, '.chart-partial-hint not rendered (no partial-data warning this session)');
    const resolved = await tokenDerivationCheck(el, '--fs-lg');
    expect(resolved, `.chart-partial-hint font-size under --fs-lg override: ${resolved}`).toBe('37px');
  });

  test('OrderBook .ob-sc-n renders at its documented 1.1rem (17.6px), the deliberate token-gap exception', async () => {
    test.skip(!sharedPage, authSkipReason);
    const p = /** @type {import('@playwright/test').Page} */ (sharedPage);
    await p.goto('/orders', { waitUntil: 'domcontentloaded' });
    await p.waitForSelector('.ob-sc-n', { timeout: 15_000 }).catch(() => {});
    const el = p.locator('.ob-sc-n').first();
    const count = await el.count();
    test.skip(count === 0, 'no .ob-sc-n rendered on /orders (no chase queue active this session)');
    const fs_ = await el.evaluate((node) => getComputedStyle(node).fontSize);
    expect(fs_, `.ob-sc-n computed font-size: ${fs_}`).toBe('17.6px'); // 1.1rem @ 16px root
  });
});

