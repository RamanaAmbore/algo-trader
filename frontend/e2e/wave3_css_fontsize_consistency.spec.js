/**
 * wave3_css_fontsize_consistency.spec.js
 *
 * Static source-pattern checks for the 2026-10-18 audit batch (CLAUDE.md
 * items 1, 4-7, 12-17) — CSS token consistency + log-panel text-size
 * consistency. Same convention as wave2b_css_token_consistency.spec.js:
 * pure CSS-property-value swaps with identical/intentional rendered
 * output, verified via readFileSync + regex (no browser needed).
 *
 * Run:
 *   cd frontend && npx playwright test e2e/wave3_css_fontsize_consistency.spec.js
 */

import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const readFile = (relPath) => readFileSync(path.resolve(__dirname, '..', relPath), 'utf-8');

/** Extract the FIRST `.selector { ... }` rule body, or null if absent. */
function ruleBody(css, selector) {
  const re = new RegExp(
    selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\s*\\{([^}]*)\\}'
  );
  const m = css.match(re);
  return m ? m[1] : null;
}

// ── Item 1 — .cand-anchor-label / .cand-anchor-chg-sep divider alpha ────

test.describe('derivatives +page.svelte — anchor-row dividers match the 0.40 family, not 0.55', () => {
  const src = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');

  test('.cand-anchor-label box-shadow uses rgba(126,151,184,0.40)', () => {
    const body = ruleBody(src, '.cand-anchor-label');
    expect(body).not.toBeNull();
    expect(body).toMatch(/box-shadow:\s*inset -1px 0 0 0 rgba\(126,\s*151,\s*184,\s*0\.40\)/);
  });

  test('.cand-anchor-chg-sep box-shadow uses rgba(126,151,184,0.40), matching CandidateLegRow .cand-chg-sep', () => {
    const body = ruleBody(src, '.cand-anchor-chg-sep');
    expect(body).not.toBeNull();
    expect(body).toMatch(/box-shadow:\s*inset -1px 0 0 0 rgba\(126,\s*151,\s*184,\s*0\.40\)/);
  });

  test('neither rule uses --algo-muted-border (which resolves to 0.55, the confirmed bug)', () => {
    expect(src).not.toMatch(/\.cand-anchor-label[^}]*--algo-muted-border/);
    expect(src).not.toMatch(/\.cand-anchor-chg-sep[^}]*--algo-muted-border/);
  });
});

// ── Item 4 — 125,145,184 typo → 126,151,184 (14 occurrences across 4 files) ──

test.describe('Muted-gray RGB typo fixed: 125,145,184 -> 126,151,184', () => {
  const files = [
    'src/lib/OptionsPayoff.svelte',
    'src/lib/ChartWorkspace.svelte',
    'src/lib/order/OptionChainTab.svelte',
    'src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte',
  ];

  for (const file of files) {
    test(`${file} has no remaining 125, 145, 184 literal`, () => {
      const src = readFile(file);
      expect(src).not.toMatch(/125,\s*145,\s*184/);
    });
  }

  test('OptionsPayoff.svelte / ChartWorkspace.svelte / OptionChainTab.svelte use the exact-alpha tokens', () => {
    for (const file of ['src/lib/OptionsPayoff.svelte', 'src/lib/ChartWorkspace.svelte', 'src/lib/order/OptionChainTab.svelte']) {
      const src = readFile(file);
      expect(src).toMatch(/background:\s*var\(--algo-muted-bg-soft\)/);
      expect(src).toMatch(/border:\s*1px solid var\(--algo-muted-bg-strong\)/);
    }
  });

  test('CandidateLegRow.svelte 0.55-alpha dividers use var(--algo-muted-border); other alphas keep the (fixed) literal', () => {
    const src = readFile('src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte');
    const matches = src.match(/box-shadow:\s*inset 2px 0 0 var\(--algo-muted-border\)/g) ?? [];
    expect(matches.length).toBe(2);
    // The non-exact-alpha sites (0.18/0.42/0.30/0.45/0.15/0.3) keep the
    // corrected RGB as a plain literal — confirm the correct RGB triple
    // appears for each, not a token (no exact-alpha token exists for these).
    for (const alpha of ['0.18', '0.42', '0.30', '0.45', '0.15', '0.3']) {
      expect(src).toContain(`126, 151, 184, ${alpha}`);
    }
  });
});

// ── Item 5 — OrderBook.svelte --algo-navy (undefined token) ─────────────

test.describe('OrderBook.svelte .ob-root.fs-card-on — no reference to an undefined --algo-navy token', () => {
  const src = readFile('src/lib/OrderBook.svelte');
  const appCss = readFile('src/app.css');

  test('no longer references var(--algo-navy, ...)', () => {
    expect(src).not.toMatch(/var\(--algo-navy/);
  });

  test('uses the plain #0f1c36 literal directly', () => {
    const body = ruleBody(src, '.ob-root.fs-card-on');
    expect(body).not.toBeNull();
    expect(body).toMatch(/background:\s*#0f1c36/);
  });

  test('--algo-navy is still not defined in app.css (confirms the fallback hex was the only real value, nothing silently broke)', () => {
    expect(appCss).not.toMatch(/--algo-navy\s*:/);
  });
});

// ── Item 6 — derivatives .order-toast-mode-paper mismatched blue ────────

test.describe('derivatives +page.svelte .order-toast-mode-paper — sky tokens, not the mismatched #38bdf8 blue', () => {
  const src = readFile('src/routes/(algo)/admin/derivatives/+page.svelte');

  test('uses var(--algo-sky-bg) / var(--algo-sky-border), matching LogPanel .mode-pill-paper\'s own fix', () => {
    const body = ruleBody(src, '.order-toast-mode-paper');
    expect(body).not.toBeNull();
    expect(body).toMatch(/background:\s*var\(--algo-sky-bg\)/);
    expect(body).toMatch(/border-color:\s*var\(--algo-sky-border\)/);
    expect(body).toMatch(/color:\s*var\(--algo-sky\)/);
  });

  test('no longer uses the raw rgba(56,189,248,...) literal', () => {
    const body = ruleBody(src, '.order-toast-mode-paper');
    expect(body).not.toMatch(/rgba\(56,\s*189,\s*248/);
  });
});

// ── Item 7 — OrderCard.svelte dead 'ramboq-agent' tag branch ────────────

test.describe('OrderCard.svelte _tagClass — dead ramboq-agent branch removed', () => {
  const src = readFile('src/lib/order/OrderCard.svelte');

  test('_tagClass no longer checks for the never-produced "ramboq-agent" broker tag', () => {
    const fnStart = src.indexOf('function _tagClass(tag)');
    expect(fnStart).toBeGreaterThan(-1);
    const fnEnd = src.indexOf('\n  }', fnStart);
    const fnBody = src.slice(fnStart, fnEnd);
    // Only a documentation comment may still mention the string
    // 'ramboq-agent' (explaining why it was removed) — no executable
    // comparison against it may remain.
    expect(fnBody).not.toMatch(/startsWith\('ramboq-agent'\)/);
    expect(fnBody).not.toMatch(/===\s*'ramboq-agent'/);
    expect(fnBody).not.toContain("return 'tag-agent'");
    expect(fnBody).toContain('ramboq-ticket');
  });

  test('the now-unused .tag-agent CSS rule was removed too (no orphaned selector)', () => {
    expect(src).not.toMatch(/:global\(\.tag-agent\)/);
  });
});

// ── Item 12 — Conn tab event_type humanization ──────────────────────────

test.describe('LogPanel.svelte — Conn tab event_type rendered humanized, not raw snake_case', () => {
  const src = readFile('src/lib/LogPanel.svelte');

  test('defines a _humanizeEventType helper', () => {
    expect(src).toMatch(/function _humanizeEventType\(/);
  });

  test('the Conn tab template calls _humanizeEventType(ev.event_type), not a bare interpolation', () => {
    expect(src).toMatch(/<span class="lp-conn-type">\{_humanizeEventType\(ev\.event_type\)\}<\/span>/);
    expect(src).not.toMatch(/<span class="lp-conn-type">\{ev\.event_type\}<\/span>/);
  });

  test('the Agents tab tag also uses the same shared helper (no second inline .replace)', () => {
    const tagLine = src.match(/const tag\s*=\s*.*/)?.[0] ?? '';
    expect(tagLine).toContain('_humanizeEventType(e.event_type)');
  });
});

// ── Items 13-17 — text-size consistency ─────────────────────────────────

test.describe('Text-size consistency fixes (source)', () => {
  test('item 13: LogPanel .lp-conn-det gets font-size: var(--fs-lg), matching .log-row-msg', () => {
    const src = readFile('src/lib/LogPanel.svelte');
    const body = ruleBody(src, '.lp-conn-det');
    expect(body).not.toBeNull();
    expect(body).toMatch(/font-size:\s*var\(--fs-lg\)/);
  });

  test('item 14: app.css bare .log-ts (no nested ist/edt spans) uses var(--fs-sm), not the raw 0.55rem', () => {
    const src = readFile('src/app.css');
    const idx = src.indexOf('.log-ts:not(:has(.log-ts-ist, .log-ts-edt))');
    expect(idx).toBeGreaterThan(-1);
    const body = src.slice(idx, src.indexOf('}', idx));
    expect(body).toMatch(/font-size:\s*var\(--fs-sm\)/);
    expect(body).not.toMatch(/font-size:\s*0\.55rem/);
  });

  test('item 15: app.css .log-chip-key uses var(--fs-xs), not the orphan 0.52rem', () => {
    const src = readFile('src/app.css');
    const idx = src.lastIndexOf('.log-chip-key');
    expect(idx).toBeGreaterThan(-1);
    const body = src.slice(idx, src.indexOf('}', idx));
    expect(body).toMatch(/font-size:\s*var\(--fs-xs\)/);
    expect(body).not.toMatch(/font-size:\s*0\.52rem/);
  });

  test('item 16: LogPanel .lp-close-btn uses var(--fs-xl), matching .lp-fs-btn, not the raw 1rem', () => {
    const src = readFile('src/lib/LogPanel.svelte');
    const body = ruleBody(src, '.lp-close-btn');
    expect(body).not.toBeNull();
    expect(body).toMatch(/font-size:\s*var\(--fs-xl\)/);
    expect(body).not.toMatch(/font-size:\s*1rem/);
  });

  test('item 17: OrderCard.svelte header span uses var(--fs-lg) via inline style, not Tailwind text-xs', () => {
    const src = readFile('src/lib/order/OrderCard.svelte');
    const headerLineIdx = src.indexOf('font-semibold');
    expect(headerLineIdx).toBeGreaterThan(-1);
    const line = src.slice(headerLineIdx - 40, headerLineIdx + 120);
    expect(line).not.toMatch(/text-xs/);
    expect(line).toMatch(/font-size:\s*var\(--fs-lg\)/);
  });

  test('item 18: the .newslist-time comment no longer hardcodes a drift-prone rem value', () => {
    const src = readFile('src/lib/LogPanel.svelte');
    expect(src).not.toMatch(/Matches \.newslist-time \(0\.62rem\)/);
  });
});

// ── Items 9-11 — LogPanel wiring (the shared-helper unit tests live in
//    src/lib/__tests__/logChips.test.js; these confirm the COMPONENT
//    actually calls them in the right places, not just that the shared
//    helpers exist in isolation). ────────────────────────────────────────

test.describe('LogPanel.svelte — Agents/Alerts tab wiring (source)', () => {
  const src = readFile('src/lib/LogPanel.svelte');

  test('the dead Kind filter state/UI-restore machinery is fully removed (no executable declaration remains)', () => {
    // Only explanatory comments may still mention these retired names
    // (documenting WHY they were removed) — no `let`/`const` declaration
    // or function definition may remain.
    expect(src).not.toMatch(/(let|const)\s+_agentKindFilter/);
    expect(src).not.toMatch(/(const)\s+_AGENT_KIND_TABS/);
    expect(src).not.toMatch(/(const)\s+_AGENT_KIND_LS_KEY/);
    expect(src).not.toMatch(/function _agentKindBucket/);
  });

  test('_agentLevel reads level from the shared parseJsonObject(e.detail), not e.detail.level directly', () => {
    const fnStart = src.indexOf('function _agentLevel(');
    expect(fnStart).toBeGreaterThan(-1);
    const fnEnd = src.indexOf('\n  }', fnStart);
    const fnBody = src.slice(fnStart, fnEnd);
    expect(fnBody).toMatch(/parseJsonObject\(e\?\.detail\)/);
    expect(fnBody).not.toMatch(/e\?\.detail\?\.level/);
  });

  test('the account filter inside _agentRows reads account from parseJsonObject(e.detail), not the nonexistent e.account', () => {
    const idx = src.indexOf('const _agentRows = $derived.by');
    expect(idx).toBeGreaterThan(-1);
    const section = src.slice(idx, idx + 2000);
    expect(section).toMatch(/parseJsonObject\(e\.detail\)\.account/);
    expect(section).not.toMatch(/orderAccountFilter\.length > 0 && e\.account/);
  });

  test('the Agents tab row content renders the detail chips via the shared chipsFromJson/_DETAIL_CHIP_OPTS', () => {
    const idx = src.indexOf('const _agentRows = $derived.by');
    const section = src.slice(idx, idx + 3500);
    expect(section).toMatch(/chipsFromJson\(e\.detail, _DETAIL_CHIP_OPTS\)/);
  });

  test('the Alerts tab detail rendering uses the same shared chipsFromJson/_DETAIL_CHIP_OPTS call, not a raw unparsed string', () => {
    const fnStart = src.indexOf('function _alertContentHtml(');
    expect(fnStart).toBeGreaterThan(-1);
    const fnEnd = src.indexOf('\n  }', fnStart);
    const fnBody = src.slice(fnStart, fnEnd);
    expect(fnBody).toMatch(/chipsFromJson\(e\.detail, _DETAIL_CHIP_OPTS\)/);
    expect(fnBody).not.toMatch(/e\.detail \? _escAttr\(e\.detail\) : ''/);
  });

  test('imports parseJsonObject and chipsFromJson from the shared $lib/logChips module', () => {
    expect(src).toMatch(/import\s*\{\s*chipsFromJson,\s*parseJsonObject\s*\}\s*from\s*'\$lib\/logChips'/);
  });
});
