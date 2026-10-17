/**
 * commandEngine.test.js — Vitest coverage for engine.js's
 * tokenizeCallStyle()/fuzzyFilter() exports (the two agents.js actually
 * imports — see `import { tokenizeCallStyle, fuzzyFilter } from
 * '$lib/command/engine.js'` in grammars/agents.js).
 *
 * The original (a) section characterized orders.js's buy/sell grammar
 * running through engine.js's generic tokenize()/parse()/suggestAt()/
 * applySuggestion() — those four exports lost their last real consumer
 * when orders.js, CommandBar.svelte and CommandLineTab.svelte were
 * deleted entirely (2026-10-17, operator-confirmed removal of the
 * legacy positional Console CLI fallback — see the 2026-10-17 entry in
 * docs/specs/AUTOMATION_SPEC.md's change log). That section
 * was removed rather than rewritten against a throwaway fixture grammar,
 * since it was characterizing a grammar that no longer exists in the
 * product, not engine.js's own contract.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT  — imports the real engine.js exports, no reimpl.
 *  2. Perf  — pure unit, no I/O.
 *  3. Stale — proves tokenizeCallStyle()/fuzzyFilter() are independent of
 *             tokenize()'s own code path — both are standalone exports
 *             agents.js relies on directly.
 *  4. Reuse — exercises the exact call-style tokenizer every WHEN/ALERT/DO/
 *             order(...)/bracket(...)/token(...) statement goes through.
 *  5. UX    — fuzzy ranking order assertions (not just membership).
 */

import { describe, it, expect } from 'vitest';

const {
  tokenize, tokenizeCallStyle,
  fuzzySubsequenceMatch, fuzzyFilter,
} = await import('$lib/command/engine.js');

describe('tokenizeCallStyle() — call-style tokenization mode (Sprint 2)', () => {
  it('does not change tokenize()\'s own export/signature', () => {
    // tokenize() itself must still treat ( ) , as plain non-whitespace chars
    // (bundled into the surrounding atom) — this proves the two modes are
    // genuinely independent, not the same function under a new name.
    const toks = tokenize('order(a,b)');
    expect(toks).toHaveLength(1);
    expect(toks[0].text).toBe('order(a,b)');
  });

  it('splits a verb + parenthesized arg list into discrete tokens', () => {
    const toks = tokenizeCallStyle('order(account, symbol="NIFTY25JULFUT")');
    const texts = toks.map(t => t.text);
    expect(texts[0]).toBe('order');
    expect(toks[1].punct).toBe('(');
    expect(texts).toContain('account');
    expect(toks.some(t => t.punct === ',')).toBe(true);
    expect(toks.some(t => t.punct === ')')).toBe(true);
  });

  it('kwarg with a quoted value containing a space stays one token', () => {
    const toks = tokenizeCallStyle('f(tag="stop loss hit")');
    const kwTok = toks.find(t => t.kwarg && t.kwarg.key === 'tag');
    expect(kwTok).toBeDefined();
    expect(kwTok.kwarg.value).toBe('stop loss hit');
    expect(kwTok.kwarg.quoted).toBe(true);
  });

  it('bare numeric and bare name args tokenize as plain atoms', () => {
    const toks = tokenizeCallStyle('mean_pnl(30)');
    expect(toks.map(t => t.text)).toEqual(['mean_pnl', '(', '30', ')']);
  });

  it('adjacency tracked via start/end — "(" immediately after NAME has no gap', () => {
    const toks = tokenizeCallStyle('order(account)');
    expect(toks[0].end).toBe(toks[1].start); // no whitespace between 'order' and '('
  });

  it('a space before "(" still tokenizes — adjacency decision is the PARSER\'s job, not the tokenizer\'s', () => {
    const toks = tokenizeCallStyle('order (account)');
    expect(toks[0].end).toBeLessThan(toks[1].start);
  });
});

describe('fuzzySubsequenceMatch() / fuzzyFilter() (Sprint 2, standalone exports)', () => {
  it('matches non-contiguous in-order characters, case-insensitive', () => {
    expect(fuzzySubsequenceMatch('mnpnl', 'mean_pnl')).toBe(true);
    expect(fuzzySubsequenceMatch('MNPNL', 'mean_pnl')).toBe(true);
  });

  it('rejects out-of-order characters', () => {
    expect(fuzzySubsequenceMatch('plnm', 'mean_pnl')).toBe(false);
  });

  it('empty prefix matches everything', () => {
    expect(fuzzySubsequenceMatch('', 'mean_pnl')).toBe(true);
  });

  it('fuzzyFilter ranks exact-prefix matches before pure-subsequence matches', () => {
    const candidates = ['stdev_pnl', 'mean_pnl', 'max_drawdown_pnl'];
    const ranked = fuzzyFilter('me', candidates);
    expect(ranked[0]).toBe('mean_pnl'); // startsWith('me')
  });

  it('fuzzyFilter drops non-matching candidates entirely', () => {
    const ranked = fuzzyFilter('zzz', ['mean_pnl', 'stdev_pnl']);
    expect(ranked).toEqual([]);
  });
});
