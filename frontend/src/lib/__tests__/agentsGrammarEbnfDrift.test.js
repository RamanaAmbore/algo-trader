/**
 * agentsGrammarEbnfDrift.test.js — drift guard between the agent CLI parser's
 * reserved-word set and docs/specs/AGENTS_GRAMMAR_EBNF.md.
 *
 * `RESERVED_WORDS` (= agents.js `_RESERVED`) is the set checked before generic
 * NAME classification. The EBNF doc documents the same set in its
 * `reserved_word := "…" | "…" | …` production. This test fails if a word is
 * added to or removed from either side without the other.
 *
 * Deliberately NOT covered: `_EVENT_META_KEYS` (not exported, retired by the
 * event_stmt dissolve in Wave 2) and `_LABEL_RESERVED_WORDS` (not exported).
 */

import { describe, it, expect, vi } from 'vitest';
// @ts-ignore -- node:fs types are not installed for svelte-check; vitest resolves it at runtime.
import { readFileSync } from 'node:fs';
// @ts-ignore -- node:url types are not installed for svelte-check; vitest resolves it at runtime.
import { fileURLToPath } from 'node:url';

vi.mock('$lib/data/instruments', () => ({
  getInstrument: vi.fn(() => null),
  listExchangesForSymbol: vi.fn(() => []),
}));
vi.mock('$lib/api', () => ({ fetchGrammarTokens: vi.fn() }));

const { RESERVED_WORDS } = await import('$lib/command/grammars/agents.js');

const EBNF_PATH = fileURLToPath(
  new URL('../../../../docs/specs/AGENTS_GRAMMAR_EBNF.md', import.meta.url),
);

/** Extract the quoted words of the `reserved_word := …` production. Only the
 *  production's own text is read: slicing stops at the first "(*" comment
 *  that follows it, so quoted words mentioned in the explanatory comment
 *  ("given", "alert", …) are never mistaken for reserved words. */
function documentedReservedWords(markdown) {
  const start = markdown.indexOf('reserved_word');
  if (start < 0) throw new Error('reserved_word production not found in EBNF doc');
  const rest = markdown.slice(start);
  const commentAt = rest.indexOf('(*');
  const production = commentAt >= 0 ? rest.slice(0, commentAt) : rest;
  const words = [...production.matchAll(/"([^"]+)"/g)].map((m) => m[1].toLowerCase());
  return words;
}

describe('agents.js reserved words match AGENTS_GRAMMAR_EBNF.md', () => {
  const doc = readFileSync(EBNF_PATH, 'utf8');
  const documented = documentedReservedWords(doc);
  const code = Array.from(RESERVED_WORDS).map((w) => w.toLowerCase());

  it('the EBNF doc documents a non-empty reserved_word production', () => {
    expect(documented.length).toBeGreaterThan(0);
  });

  it('every reserved word in agents.js is documented in the EBNF doc', () => {
    const docSet = new Set(documented);
    const missing = code.filter((w) => !docSet.has(w));
    expect(missing, `reserved in agents.js but missing from the EBNF doc`).toEqual([]);
  });

  it('every reserved word documented in the EBNF doc exists in agents.js', () => {
    const codeSet = new Set(code);
    const extra = documented.filter((w) => !codeSet.has(w));
    expect(extra, `documented in the EBNF doc but not reserved in agents.js`).toEqual([]);
  });

  it('the two sets are exactly equal (sorted, no duplicates)', () => {
    expect([...new Set(documented)].sort()).toEqual([...new Set(code)].sort());
  });
});
