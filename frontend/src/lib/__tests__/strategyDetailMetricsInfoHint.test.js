/**
 * strategyDetailMetricsInfoHint.test.js
 *
 * /strategies/[id]'s "Risk-adjusted metrics" grid (8 metrics: Sharpe,
 * Sortino, Max DD, Max DD %, Win rate, Daily avg, Daily vol,
 * Cumulative) had plain `title=` attributes and no InfoHint import at
 * all. Fix adds InfoHint chips carrying the same wording. This is the
 * durable, always-green vitest-level guard — the companion Playwright
 * spec (`e2e/strategy_detail_metrics_infohint.spec.js`) exercises the
 * live popover-open/close contract but gracefully skips on any
 * environment with zero seeded strategies (e.g. a fresh dev DB), so
 * it alone cannot be the only coverage for this fix.
 */

import { describe, it, expect } from 'vitest';
import SRC from '../../routes/(algo)/strategies/[id]/+page.svelte?raw';

const scriptBlock = SRC.slice(SRC.indexOf('<script>'), SRC.indexOf('</script>'));
const markupBlock = SRC.slice(SRC.indexOf('</script>'), SRC.lastIndexOf('<style>'));

const METRICS = [
  { label: 'Sharpe',     needle: 'Annualised Sharpe ratio' },
  { label: 'Sortino',    needle: 'Sortino ratio' },
  { label: 'Max DD',     needle: 'Max drawdown — largest peak-to-trough' },
  { label: 'Max DD %',   needle: 'Max drawdown as % of the running peak' },
  { label: 'Win rate',   needle: 'Fraction of days with positive' },
  { label: 'Daily avg',  needle: 'Mean P&amp;L change per day' },
  { label: 'Daily vol',  needle: 'Standard deviation of daily P&amp;L change' },
  { label: 'Cumulative', needle: 'Cumulative P&amp;L (realised + unrealised)' },
];

describe('/strategies/[id] — Risk-adjusted metrics labels wire InfoHint', () => {
  it('imports InfoHint', () => {
    expect(scriptBlock).toMatch(/import InfoHint from '\$lib\/InfoHint\.svelte';/);
  });

  it('no .metric-lbl carries a bare title= attribute any more', () => {
    expect(markupBlock).not.toMatch(/class="metric-lbl" title=/);
  });

  it('each of the 8 metric labels has an InfoHint popup with matching wording, and a .metric-lbl-txt span for the visible text', () => {
    for (const { label, needle } of METRICS) {
      // Find the metric block: <div class="metric-lbl">...label...InfoHint...</div>
      const idx = markupBlock.indexOf(`>${label}</span>`);
      expect(idx, `label "${label}" not found`).toBeGreaterThan(-1);
      const windowText = markupBlock.slice(Math.max(0, idx - 200), idx + 400);
      expect(windowText).toContain('metric-lbl-txt');
      expect(windowText).toMatch(/<InfoHint popup text="/);
      expect(windowText).toContain(needle);
    }
  });

  it('CSS: .metric-lbl is now a flex row (icon + text), with uppercase/letter-spacing moved to .metric-lbl-txt', () => {
    const styleBlock = SRC.slice(SRC.lastIndexOf('<style>'));
    expect(styleBlock).toMatch(/\.metric-lbl\s*{\s*display:\s*flex;/);
    expect(styleBlock).toMatch(/\.metric-lbl-txt\s*{[^}]*text-transform:\s*uppercase;/);
    // cursor:help on the bare label is retired — InfoHint's own chip
    // carries the hover affordance now.
    expect(styleBlock).not.toMatch(/\.metric-lbl\s*{[^}]*cursor:\s*help;/);
  });
});
