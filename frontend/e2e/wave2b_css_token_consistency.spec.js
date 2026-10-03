/**
 * wave2b_css_token_consistency.spec.js
 *
 * Verifies the Wave 2b CSS-variable consistency pass (2026-09-30): literal
 * rgba(126,151,184,0.10) slate-divider values repointed to the `--sep-color`
 * token (established in app.css by the prior Wave 2a/1 fix, commit
 * e9cbdf53), literal rgba(251,191,36,0.30) amber-border values repointed to
 * `--algo-amber-border-soft`, and a handful of one-off hex text colors
 * converged onto their exact-matching named tokens. (DayPnlBreakup's own
 * font-size convergence tests were removed along with the component —
 * see docs/specs/NAVSTRIP_SPEC.md "Slot 1 Day P&L Breakup modal (REMOVED)".)
 *
 * Static source-pattern checks only — no browser needed, these are pure
 * CSS-property-value swaps with identical rendered output (token resolves
 * to the same literal value app.css already defines).
 *
 * Run:
 *   cd frontend && npx playwright test e2e/wave2b_css_token_consistency.spec.js
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const readFile = (relPath) => {
  const abs = path.resolve(__dirname, '..', relPath);
  return readFileSync(abs, 'utf-8');
};

/** Extract the FIRST `.selector { ... }` rule body, or null if absent. */
function ruleBody(css, selector) {
  const re = new RegExp(
    selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'
  );
  const m = css.match(re);
  return m ? m[1] : null;
}

const SEP_LITERAL_RE = /rgba\(\s*126,\s*151,\s*184,\s*0\.10?\s*\)/;
const AMBER_BORDER_LITERAL_RE = /rgba\(\s*251,\s*191,\s*36,\s*0\.30?\s*\)/;

test.describe('--sep-color token swap (slate divider, exact 0.10 literal)', () => {
  const cases = [
    ['src/lib/execution/SimulatorPanel.svelte', '.sim-leg-chart-row', /border:\s*1px solid var\(--sep-color\)/],
    ['src/lib/execution/SimulatorPanel.svelte', '.sim-summary-grid td', /border-bottom:\s*1px solid var\(--sep-color\)/],
    ['src/lib/SymbolSearchInput.svelte', '.ssi-section', /border-bottom:\s*1px solid var\(--sep-color\)/],
    ['src/lib/SymbolSearchInput.svelte', '.ssi-hint', /border-top:\s*1px solid var\(--sep-color\)/],
    ['src/lib/order/ChaseCard.svelte', '.cc-row', /border-bottom:\s*1px solid var\(--sep-color\)/],
    ['src/lib/ShortcutCheatsheet.svelte', '.sc-section-h', /border-bottom:\s*1px solid var\(--sep-color\)/],
    ['src/lib/ShortcutCheatsheet.svelte', '.sc-foot', /border-top:\s*1px solid var\(--sep-color\)/],
    ['src/lib/LogPanel.svelte', '.lp-conn-row', /border-bottom:\s*1px solid var\(--sep-color\)/],
  ];

  for (const [file, selector, expected] of cases) {
    test(`${file} ${selector} uses var(--sep-color)`, () => {
      const content = readFile(file);
      const body = ruleBody(content, selector);
      expect(body, `${selector} rule must exist in ${file}`).not.toBeNull();
      expect(body).toMatch(expected);
      expect(body).not.toMatch(SEP_LITERAL_RE);
    });
  }

  test('admin/health/+page.svelte — kv-row/broker-row/ip-row/ticker-stale-list/kv-section all use var(--sep-color)', () => {
    const content = readFile('src/routes/(algo)/admin/health/+page.svelte');
    for (const selector of ['.kv-row', '.broker-row', '.ip-row', '.ticker-stale-list', '.kv-section']) {
      const body = ruleBody(content, selector);
      expect(body, `${selector} rule must exist`).not.toBeNull();
      expect(body).toMatch(/var\(--sep-color\)/);
      expect(body).not.toMatch(SEP_LITERAL_RE);
    }
  });

  test('admin/settings/+page.svelte — inline border-top-color style uses var(--sep-color), .settings-row uses the token', () => {
    const content = readFile('src/routes/(algo)/admin/settings/+page.svelte');
    expect(content).not.toMatch(/border-top-color:\s*rgba\(126,151,184,0\.10\)/);
    expect(content.match(/border-top-color:\s*var\(--sep-color\)/g)?.length ?? 0).toBeGreaterThanOrEqual(3);
    const body = ruleBody(content, '.settings-row');
    expect(body, '.settings-row rule must exist').not.toBeNull();
    expect(body).toMatch(/border-bottom:\s*1px solid var\(--sep-color\)/);
  });
});

test.describe('--algo-amber-border-soft token swap (exact 0.30 amber literal, header borders)', () => {
  test('admin/history/+page.svelte .hist-table th uses var(--algo-amber-border-soft)', () => {
    const content = readFile('src/routes/(algo)/admin/history/+page.svelte');
    const body = ruleBody(content, '.hist-table th');
    expect(body, '.hist-table th rule must exist').not.toBeNull();
    expect(body).toMatch(/border-bottom:\s*1px solid var\(--algo-amber-border-soft\)/);
    expect(body).not.toMatch(AMBER_BORDER_LITERAL_RE);
  });

  test('strategies/[id]/+page.svelte .strat-table th uses var(--algo-amber-border-soft)', () => {
    const content = readFile('src/routes/(algo)/strategies/[id]/+page.svelte');
    const body = ruleBody(content, '.strat-table th');
    expect(body, '.strat-table th rule must exist').not.toBeNull();
    expect(body).toMatch(/border-bottom:\s*1px solid var\(--algo-amber-border-soft\)/);
    expect(body).not.toMatch(AMBER_BORDER_LITERAL_RE);
  });
});

test.describe('One-off hex text colors converged onto exact-matching named tokens', () => {
  test('PnlAnalysis.svelte muted-text selectors converge #4e6080 onto var(--algo-muted)', () => {
    const content = readFile('src/lib/PnlAnalysis.svelte');
    for (const selector of ['.muted', '.chart-placeholder', '.pnl-tbl .muted', '.empty-hint', '.drop-prompt']) {
      const body = ruleBody(content, selector);
      expect(body, `${selector} rule must exist`).not.toBeNull();
      expect(body).toMatch(/var\(--algo-muted\)/);
      expect(body).not.toMatch(/#4e6080/);
    }
  });

  test('PnlAnalysis.svelte .seg-pill converges #7dd3fc onto var(--algo-sky) (exact literal match)', () => {
    const content = readFile('src/lib/PnlAnalysis.svelte');
    const body = ruleBody(content, '.seg-pill');
    expect(body, '.seg-pill rule must exist').not.toBeNull();
    expect(body).toMatch(/color:\s*var\(--algo-sky\)/);
    expect(body).not.toMatch(/color:\s*#7dd3fc/);
  });

  test('admin/history/+page.svelte converges #94a3b8 onto var(--algo-dim) and #67e8f9 onto var(--algo-cyan-text)', () => {
    const content = readFile('src/routes/(algo)/admin/history/+page.svelte');
    for (const selector of ['.hist-flbl', '.hist-empty-row']) {
      const body = ruleBody(content, selector);
      expect(body, `${selector} rule must exist`).not.toBeNull();
      expect(body).toMatch(/var\(--algo-dim\)/);
      expect(body).not.toMatch(/#94a3b8/);
    }
    for (const selector of ['.hist-pill.st-pending', '.hist-pill-info', '.hist-audit-link']) {
      const body = ruleBody(content, selector);
      expect(body, `${selector} rule must exist`).not.toBeNull();
      expect(body).toMatch(/var\(--algo-cyan-text\)/);
      expect(body).not.toMatch(/#67e8f9/);
    }
    // Deliberately-preserved exceptions — documented in-file as a different
    // role (pill text color) distinct from the cell-text #4e6080 cluster;
    // left untouched by this wave (see handback report).
    expect(ruleBody(content, '.hist-side-sell')).toMatch(/#fca5a5/);
    expect(ruleBody(content, '.hist-pill.st-err')).toMatch(/#fca5a5/);
  });

  test('SimulatorPanel.svelte .sim-summary-total converges #fde68a onto var(--algo-amber-text)', () => {
    const content = readFile('src/lib/execution/SimulatorPanel.svelte');
    const body = ruleBody(content, '.sim-summary-total td');
    expect(body, '.sim-summary-total td rule must exist').not.toBeNull();
    expect(body).toMatch(/color:\s*var\(--algo-amber-text\)/);
  });
});
