/**
 * automationConditionsInfoHint.test.js
 *
 * Sprint 3 (2026-10) — the agent-edit form's "Conditions (JSON)" field was
 * the one field on the whole form with no InfoHint/placeholder, unlike every
 * sibling field (Tags, Blackout windows, Long name, etc.). This fix adds an
 * InfoHint mentioning the new parameterized call-syntax metric tokens
 * (mean_pnl(45) etc., added in Sprint 2) alongside the existing fixed
 * tokens and leaf shape. Source-level guard (no live browser/backend
 * needed) — mirrors strategyDetailMetricsInfoHint.test.js's pattern.
 */

import { describe, it, expect } from 'vitest';
import SRC from '../../routes/(algo)/automation/+page.svelte?raw';

const scriptBlock = SRC.slice(SRC.indexOf('<script>'), SRC.indexOf('</script>'));
const markupBlock = SRC.slice(SRC.indexOf('</script>'), SRC.lastIndexOf('<style>'));

describe('automation/+page.svelte — Conditions (JSON) field wires InfoHint', () => {
  it('imports InfoHint', () => {
    expect(scriptBlock).toMatch(/import InfoHint from '\$lib\/InfoHint\.svelte';/);
  });

  it('the Conditions (JSON) field-label has an InfoHint popup panel', () => {
    const idx = markupBlock.indexOf('Conditions (JSON)');
    expect(idx, '"Conditions (JSON)" label not found').toBeGreaterThan(-1);
    const windowText = markupBlock.slice(idx, idx + 600);
    expect(windowText).toMatch(/<InfoHint popup panel title="Conditions \(JSON\)"/);
  });

  it('the hint text mentions both the fixed window tokens and the new call syntax', () => {
    const idx = markupBlock.indexOf('Conditions (JSON)');
    const windowText = markupBlock.slice(idx, idx + 1200);
    expect(windowText).toContain('mean_pnl_30m');
    expect(windowText).toContain('mean_pnl(45)');
  });

  it('the hint text shows an example leaf shape', () => {
    const idx = markupBlock.indexOf('Conditions (JSON)');
    const windowText = markupBlock.slice(idx, idx + 1200);
    expect(windowText).toContain('&quot;metric&quot;');
    expect(windowText).toContain('&quot;scope&quot;');
  });
});
