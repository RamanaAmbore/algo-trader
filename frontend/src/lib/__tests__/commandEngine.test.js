/**
 * commandEngine.test.js — Vitest coverage for engine.js, both:
 *  (a) characterization tests proving orders.js's EXISTING parse()/suggestAt()
 *      behavior is unaffected by the Sprint 2 engine.js edits (run once BEFORE
 *      the edits landed, kept afterward as the regression guard — per
 *      .claude/PLAN.md Step 2's explicit instruction to verify this empirically,
 *      not assume it), and
 *  (b) new coverage for the 3 Sprint 2 additions: first-positional-as-keyword,
 *      tokenizeCallStyle(), and the fuzzy-subsequence suggestion ranking.
 *
 * Five quality dimensions per feedback_test_dimensions.md:
 *  1. SSOT  — imports the real orderGrammar + engine.js functions, no reimpl.
 *  2. Perf  — pure unit, no I/O; instruments/accounts mocked.
 *  3. Stale — (b) tests prove the new exports are additive, never touching
 *             tokenize()'s/suggestAt()'s own code paths for orders.js.
 *  4. Reuse — (a) exercises the exact buy/sell grammar every order-entry
 *             command goes through today.
 *  5. UX    — fuzzy ranking order assertions (not just membership).
 */

import { describe, it, expect, vi } from 'vitest';

vi.mock('$lib/data/instruments', () => ({
  getInstrument: vi.fn(),
  listOptions: vi.fn(),
  listFutures: vi.fn(),
  nearestExpiry: vi.fn(),
  listStrikes: vi.fn(),
  findOption: vi.fn(),
  findNearestFuture: vi.fn(),
  findEquity: vi.fn(),
  listUnderlyingsByType: vi.fn(),
  listExpiries: vi.fn(),
}));
vi.mock('$lib/data/accounts', () => ({
  suggestAccounts: vi.fn(),
}));

const { orderGrammar } = await import('$lib/command/grammars/orders.js');
const {
  tokenize, tokenizeCallStyle, tokenAtCursor, parse, suggestAt, applySuggestion,
  fuzzySubsequenceMatch, fuzzyFilter,
} = await import('$lib/command/engine.js');

// ── (a) orders.js characterization — unchanged behavior ────────────────────

describe('orders.js through engine.js — unchanged behavior (Sprint 2 regression guard)', () => {
  it('tokenize(): whitespace-delimited, quote-aware, kwarg-splitting — unchanged', () => {
    const toks = tokenize('buy ZG0790 EQ INFY 1500 MARKET product=NRML');
    expect(toks.map(t => t.text)).toEqual(
      ['buy', 'ZG0790', 'EQ', 'INFY', '1500', 'MARKET', 'product=NRML']
    );
    expect(toks[6].kwarg).toEqual({ key: 'product', value: 'NRML' });
  });

  it('tokenize(): quoted token kept intact, text stripped of quotes', () => {
    const toks = tokenize('buy "hello world" done');
    expect(toks[1].quoted).toBe(true);
    expect(toks[1].text).toBe('hello world');
  });

  it('parse(): full buy command resolves all positional roles correctly', () => {
    const line = 'buy ZG0790 EQ INFY 1500 MARKET';
    const result = parse(line, orderGrammar);
    expect(result.errors).toEqual([]);
    expect(result.verb).toBe('buy');
    expect(result.args.account).toBe('ZG0790');
    expect(result.args.instType).toBe('EQ');
    expect(result.args.symbol).toBe('INFY');
    expect(result.args.qty).toBe(1500);
    expect(result.args.orderType).toBe('MARKET');
  });

  it('parse(): missing required positional (qty) still errors exactly as before', () => {
    const result = parse('buy ZG0790 EQ INFY', orderGrammar);
    expect(result.errors).toContain('missing qty');
  });

  it('parse(): kwarg product= still resolves into kwargs, not args', () => {
    const result = parse('buy ZG0790 EQ INFY 1500 MARKET product=NRML', orderGrammar);
    expect(result.errors).toEqual([]);
    expect(result.kwargs.product).toBe('NRML');
    expect(result.args.product).toBeUndefined();
  });

  it('parse(): unknown verb — unchanged error shape', () => {
    const result = parse('frobnicate foo', orderGrammar);
    expect(result.verb).toBeNull();
    expect(result.errors).toEqual(['unknown verb: frobnicate']);
  });

  it('suggestAt(): verb-position suggestions unchanged (prefix match only)', () => {
    const { suggestions, role } = suggestAt('b', 1, orderGrammar);
    expect(role).toBe('verb');
    expect(suggestions).toContain('buy');
    expect(suggestions).not.toContain('sell');
  });

  it('suggestAt(): past-last-positional suggests kwarg names unchanged', () => {
    const line = 'buy ZG0790 EQ INFY 1500 MARKET 100 ';
    const { suggestions, role } = suggestAt(line, line.length, orderGrammar);
    expect(role).toBe('kwarg-key');
    expect(suggestions).toContain('product=');
  });

  it('applySuggestion(): unchanged replacement-at-cursor behavior', () => {
    const line = 'b';
    const { line: newLine, cursor } = applySuggestion(line, 1, 'buy');
    expect(newLine).toBe('buy ');
    expect(cursor).toBe(4);
  });
});

// ── (b) Sprint 2 engine.js additions ────────────────────────────────────────
//
// NOTE: an earlier revision of this sprint also added a "first-positional-
// as-keyword-too" branch to parse()'s required-check. It's been reverted —
// agents.js (the only consumer this was built for) only ever imports
// `tokenizeCallStyle` from this module, never `parse()`, and the operator's
// later "every parameter is always keyword, including the first" decision
// removed the entire concept of a positional argument from the agent CLI
// grammar, so the use case this was speculatively built for no longer
// exists anywhere. `parse()` is back to its original, unmodified behavior.

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

describe('fuzzySubsequenceMatch() / fuzzyFilter() (Sprint 2, opt-in, orders.js unaffected)', () => {
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

  it('does not alter suggestAt()\'s own default .startsWith() filtering for orders.js', () => {
    // suggestAt() itself was not modified — re-run the same characterization
    // assertion here as a belt-and-suspenders co-location check.
    const { suggestions } = suggestAt('S', 1, orderGrammar);
    // 'S' doesn't prefix-match 'sell'... wait it does (case-insensitive compare
    // inside suggestAt). Assert the real, unchanged contract instead:
    expect(suggestions).toContain('sell');
  });
});
