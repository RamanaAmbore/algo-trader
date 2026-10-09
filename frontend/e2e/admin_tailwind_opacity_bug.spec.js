/**
 * admin_tailwind_opacity_bug.spec.js
 *
 * Covers the 2026-10 admin-surface P0 consistency audit fixes:
 *
 *   P0 #1 — Portaled modals lose the dark theme. admin/+page.svelte's
 *   Investor Portal modal (`ModalShell` → portals to `document.body`,
 *   outside `.algo-content`/`.card-theme-dark`) now carries
 *   `card-theme-dark` directly on the `.ip-modal` panel div itself.
 *   `.card-theme-dark .btn-primary` is a plain descendant selector, so
 *   it reaches the panel's `.btn-primary` buttons regardless of where
 *   in the DOM the panel physically lives.
 *
 *   P0 #2 — Destructive/semantic admin buttons silently rendered plain
 *   white. `:global(.algo-content .btn-secondary)` (+layout.svelte) is
 *   a 2-class selector that beats a single Tailwind color utility (e.g.
 *   `text-red-300 border-red-400/50`) applied directly to a
 *   `.btn-secondary` button. Fixed via a shared `!important` tone
 *   family in app.css (`.btn-tone-red/-amber/-sky/-cyan/-green/-violet`),
 *   consolidating what used to be three independent hand-rolled copies
 *   of the same escape hatch (admin/brokers' local `.destructive`,
 *   strategies' local `.btn-danger`, and admin's un-escaped utilities).
 *
 *   P0 #3 — `bg-[var(--x)]/NN`, `border-[var(--x)]/NN`, `text-[var(--x)]/NN`
 *   (an opacity modifier on an arbitrary `var()` value) emits ZERO CSS
 *   on this project's pinned Tailwind 3.4.17 — confirmed directly by
 *   compiling a minimal Tailwind build against the real project config
 *   and grepping the output (see PR notes). Every occurrence in
 *   admin/+page.svelte, admin/tokens/+page.svelte and
 *   admin/settings/+page.svelte is replaced with a pre-mixed app.css
 *   alpha token that already bakes the intended opacity in.
 *
 * Five quality dimensions:
 *   1. SSOT   — verifies the actual app.css cascade result via
 *               getComputedStyle, not just presence of a class name
 *   2. Perf   — synthetic-DOM checks need one page load each, no live
 *               data dependency, never skip
 *   3. Stale  — static source scan re-greps the exact bug-signature
 *               regex across all touched files, catching any site the
 *               audit's own list missed, and asserts the OLD local
 *               escape-hatch copies (`.destructive`, `.btn-danger`) are
 *               gone, not just duplicated
 *   4. Reuse  — loginAsAdmin fixture; mirrors the synthetic-DOM idiom
 *               already established by account_cell_specificity_fix.spec.js
 *   5. UX     — checks the actual rendered color/alpha, not just that
 *               *some* non-default color applies
 *
 * Run:
 *   cd frontend && npx playwright test e2e/admin_tailwind_opacity_bug.spec.js
 */
import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loginAsAdmin } from './fixtures/auth.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const TIMEOUT = 25_000;

const readFile = (relPath) => {
  const abs = path.resolve(__dirname, '..', relPath);
  return readFileSync(abs, 'utf-8');
};

// Signature of the P0 #3 bug: an opacity modifier applied directly to an
// arbitrary var() value inside a Tailwind bracket utility.
const OPACITY_ON_VAR_RE = /\[var\([^)]*\)\]\/\d+/;

const TARGET_FILES = [
  'src/routes/(algo)/admin/+page.svelte',
  'src/routes/(algo)/admin/tokens/+page.svelte',
  'src/routes/(algo)/admin/settings/+page.svelte',
];

test.describe('P0 #3 — opacity-suffix-on-var() Tailwind bug, static source scan', () => {
  for (const file of TARGET_FILES) {
    test(`${file} has zero [var(--x)]/NN occurrences`, () => {
      const content = readFile(file);
      const matches = content.match(new RegExp(OPACITY_ON_VAR_RE, 'g')) || [];
      expect(matches, `found: ${JSON.stringify(matches)}`).toEqual([]);
    });
  }

  test('admin/+page.svelte partner-role pill uses a dim-green text token, not a stripped opacity suffix', () => {
    const content = readFile('src/routes/(algo)/admin/+page.svelte');
    expect(content).toMatch(/text-\[var\(--algo-green-text-dim\)\]/);
  });

  test('admin/tokens/+page.svelte Edit button uses pre-mixed amber tokens', () => {
    const content = readFile('src/routes/(algo)/admin/tokens/+page.svelte');
    expect(content).toMatch(/border-\[var\(--algo-amber-border\)\]/);
    expect(content).toMatch(/hover:bg-\[var\(--c-action-14\)\]/);
  });

  test('admin/settings/+page.svelte global-switch pills use pre-mixed green/red bg tokens', () => {
    const content = readFile('src/routes/(algo)/admin/settings/+page.svelte');
    const greenMatches = content.match(/bg-\[var\(--algo-green-bg-mid\)\]/g) || [];
    const redMatches = content.match(/bg-\[var\(--algo-red-bg-strong\)\]/g) || [];
    expect(greenMatches.length).toBeGreaterThanOrEqual(3);
    expect(redMatches.length).toBeGreaterThanOrEqual(3);
  });
});

test.describe('P0 #2 — destructive/semantic button tone, static source scan', () => {
  test('admin/+page.svelte: the five+ semantic buttons use btn-tone-* classes, not bare color utilities', () => {
    const content = readFile('src/routes/(algo)/admin/+page.svelte');
    for (const tone of ['btn-tone-amber', 'btn-tone-sky', 'btn-tone-green', 'btn-tone-red', 'btn-tone-violet', 'btn-tone-cyan']) {
      expect(content, `missing ${tone}`).toContain(tone);
    }
    // The raw, cascade-losing utility combinations must be gone from the
    // action-button row (not just duplicated alongside the fix).
    expect(content).not.toMatch(/btn-secondary text-\[0\.65rem\] py-1 px-2 border-(red|amber|sky|violet|cyan|emerald)-\d00\/50/);
  });

  test('strategies/+page.svelte: delete button uses shared btn-tone-red, local .btn-danger rule removed', () => {
    const content = readFile('src/routes/(algo)/strategies/+page.svelte');
    expect(content).toContain('btn-tone-red');
    expect(content).not.toContain('btn-danger');
  });

  test('admin/brokers/+page.svelte: delete button uses shared btn-tone-red, local .destructive rule removed', () => {
    const content = readFile('src/routes/(algo)/admin/brokers/+page.svelte');
    expect(content).toContain('btn-tone-red');
    expect(content).not.toMatch(/:global\(\.brokers-table \.destructive\)/);
  });

  test('app.css defines the shared btn-tone-* family with !important escape hatches', () => {
    const content = readFile('src/app.css');
    for (const tone of ['btn-tone-red', 'btn-tone-amber', 'btn-tone-sky', 'btn-tone-cyan', 'btn-tone-green', 'btn-tone-violet']) {
      const re = new RegExp(`\\.${tone}\\s*\\{[^}]*!important[^}]*!important`);
      expect(content, `.${tone} must set both color and border-color with !important`).toMatch(re);
    }
  });
});

test.describe('P0 #1 — portaled modal keeps dark theme', () => {
  test('admin/+page.svelte: .ip-modal panel carries card-theme-dark', () => {
    const content = readFile('src/routes/(algo)/admin/+page.svelte');
    expect(content).toMatch(/class="ip-modal card-theme-dark"/);
  });
});

test.describe('Rendered-CSS proof (synthetic DOM, no live data dependency)', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsAdmin(page);
  });

  test('.card-theme-dark .btn-primary renders amber even when NOT a descendant of .algo-viewport (portal simulation)', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
    await page.waitForSelector('.algo-viewport', { timeout: TIMEOUT });

    const result = await page.evaluate(() => {
      // Simulate ModalShell's portal: attach directly to <body>, OUTSIDE
      // .algo-viewport/.card-theme-dark, exactly like the real bug.
      const panel = document.createElement('div');
      panel.className = 'ip-modal card-theme-dark';
      panel.style.position = 'fixed';
      panel.style.top = '-9999px';
      document.body.appendChild(panel);

      const btn = document.createElement('button');
      btn.className = 'btn-primary';
      btn.textContent = 'Mint';
      panel.appendChild(btn);

      const out = getComputedStyle(btn).color;
      panel.remove();
      return out;
    });

    // #fbbf24 (dark-theme amber) — NOT #0c1830 (light-theme navy text).
    expect(result).toBe('rgb(251, 191, 36)');
  });

  test('a portaled .btn-primary WITHOUT card-theme-dark renders the light-theme fallback (negative control)', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
    await page.waitForSelector('.algo-viewport', { timeout: TIMEOUT });

    const result = await page.evaluate(() => {
      const panel = document.createElement('div');
      panel.className = 'ip-modal'; // no card-theme-dark — the pre-fix shape
      panel.style.position = 'fixed';
      panel.style.top = '-9999px';
      document.body.appendChild(panel);

      const btn = document.createElement('button');
      btn.className = 'btn-primary';
      panel.appendChild(btn);

      const out = getComputedStyle(btn).color;
      panel.remove();
      return out;
    });

    // #0c1830 — confirms the bug was real before the fix (and would
    // reappear if card-theme-dark were ever removed from the panel).
    expect(result).toBe('rgb(12, 24, 48)');
  });

  test('.algo-content .btn-secondary.btn-tone-red renders red, surviving the layout override', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
    await page.waitForSelector('.algo-content', { timeout: TIMEOUT });

    const result = await page.evaluate(() => {
      const content = document.querySelector('.algo-content');
      const btn = document.createElement('button');
      btn.className = 'btn-secondary btn-tone-red';
      btn.textContent = 'Terminate';
      btn.style.position = 'fixed';
      btn.style.top = '-9999px';
      content.appendChild(btn);

      const cs = getComputedStyle(btn);
      const out = { color: cs.color, borderColor: cs.borderColor };
      btn.remove();
      return out;
    });

    // --c-short / --algo-red == #f87171 == rgb(248, 113, 113). Must NOT
    // be the layout's plain slate fallback (--algo-slate).
    expect(result.color).toBe('rgb(248, 113, 113)');
    expect(result.borderColor).toMatch(/rgba?\(248,\s*113,\s*113/);
  });

  test('a .algo-content .btn-secondary WITHOUT a tone class renders the plain slate fallback (negative control)', async ({ page }) => {
    await page.goto('/dashboard', { waitUntil: 'domcontentloaded', timeout: TIMEOUT });
    await page.waitForSelector('.algo-content', { timeout: TIMEOUT });

    const color = await page.evaluate(() => {
      const content = document.querySelector('.algo-content');
      const btn = document.createElement('button');
      btn.className = 'btn-secondary';
      btn.style.position = 'fixed';
      btn.style.top = '-9999px';
      content.appendChild(btn);
      const out = getComputedStyle(btn).color;
      btn.remove();
      return out;
    });

    // Confirms the layout rule really does win in the absence of the
    // fix's tone class — i.e. the bug this PR fixes is real.
    expect(color).not.toBe('rgb(248, 113, 113)');
  });
});
