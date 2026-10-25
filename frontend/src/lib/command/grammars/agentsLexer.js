// agentsLexer.js — Chevrotain token vocabulary for the agent CLI grammar
// (Phase 1 of the Chevrotain migration plan, see .claude/PLAN.md).
//
// NOT wired into production — `agents.js`'s hand-written `lexAgentStatement`/
// `parseStatement` remain the live parser. This file + `agentsParser.js` are
// a parallel, differentially-verified-only artifact (see
// `__tests__/agentsChevrotainDiff.test.js`).
//
// Design notes (tp_scales_json wrinkle — see PLAN.md "Resolve the
// tp_scales_json wrinkle first"): the OLD hand-written lexer tokenizes a
// call's `(...)` inner text via a SEPARATE pass (`tokenizeCallStyle`,
// engine.js) that treats argument values as raw, bracket-unaware
// substrings — which is why `tp_scales_json=[...]`/`notify=[...]` need their
// OWN dedicated mini-lexers (`_lexObjScalesTok`, or a standalone
// `lexAgentStatement` re-entry for `notify`) to see `[`/`]`/`{`/`}` at all
// inside a call argument.
//
// DECISION: this lexer is fully UNIFIED — one token stream, start to finish,
// including inside a call's `(...)`. `[`, `]`, `{`, `}` are ordinary tokens
// everywhere (not just at the top level, where the old lexer's Sprint 12
// already unified `where=`'s `{...}` against the main stream). The
// Chevrotain PARSER's own recursive `value`/`objectLiteral`/`listLiteral`
// rules (agentsParser.js) handle nested brackets/braces inside a call
// argument directly, via ordinary recursive descent — no second lexer
// pass, no raw-substring re-tokenization, for ANY argument key (not just
// `tp_scales_json`/`notify`). This is a (narrow, deliberate) WIDENING
// relative to the old grammar: a generic call argument can now syntactically
// accept a list/object value where the old grammar only special-cased those
// two keys — but every statement the OLD grammar accepts still parses
// identically (nothing that used to succeed changes shape), which is what
// Phase 1's differential harness actually checks. See PLAN.md §2.
//
// Meta clauses (`#name=value, ...`) are deliberately OUT OF BAND — same as
// the old lexer, which hand-scans `#` specially inside `lexAgentStatement`
// rather than through its own generic NAME/value token productions. This
// lexer does not tokenize `#` at all; `agentsParser.js`'s `parseStatementNew`
// finds the top-level (quote/paren/bracket/brace-aware) meta boundary BEFORE
// invoking this lexer, and scans the trailing `#...` region with its own
// small hand-written scanner — mirroring the old architecture's own split,
// not a shortcut around it.
//
// Known, documented gaps (see PLAN.md / the differential harness report):
//   - Newline-as-statement-separator (`Sprint [newline-separator]`) is NOT
//     implemented — only `,`/`;` are accepted as item/call-list separators.
//   - GIVEN-rhs arithmetic/string-concat expressions (`+`/`/`/`%`/`**`) are
//     NOT implemented — a GIVEN/assign RHS is `value | condition` only.
//   - `event_stmt` (bare `ALERT log_tag=...`/`where=`/`match=`) is NOT
//     implemented.
//   - Hyphenated token names (`notify-critical-trio`) ARE supported (NAME's
//     pattern below matches the old lexer's mid-word-hyphen widening).

import { createToken, Lexer } from 'chevrotain';

// ── Skipped ──────────────────────────────────────────────────────────────
export const WhiteSpace = createToken({ name: 'WhiteSpace', pattern: /\s+/, group: Lexer.SKIPPED });

// ── Literals ─────────────────────────────────────────────────────────────
// NUMBER: optional leading "-" immediately followed by a digit (same rule
// the old lexer uses to disambiguate a negative-literal from a later
// standalone MINUS token — see agents.js's own comment on this ordering).
export const NUMBER = createToken({ name: 'NUMBER', pattern: /-?[0-9]+(?:\.[0-9]+)?/ });
export const STRING = createToken({
  name: 'STRING',
  // single- or double-quoted; old lexer has no escape handling either
  // (scans to the next literal matching quote char) — matched here.
  pattern: /"[^"]*"|'[^']*'/,
});

// ── Punctuation (checked before NAME/NUMBER so Chevrotain's longest-match
//    still resolves correctly; two-char tokens are listed before their
//    one-char prefixes so ties-by-definition-order never matter — longest
//    match always wins regardless, this ordering is just for clarity). ──
export const LE = createToken({ name: 'LE', pattern: /<=/ });
export const GE = createToken({ name: 'GE', pattern: />=/ });
export const EQEQ = createToken({ name: 'EQEQ', pattern: /==/ });
export const NE = createToken({ name: 'NE', pattern: /!=/ });
export const LT = createToken({ name: 'LT', pattern: /</ });
export const GT = createToken({ name: 'GT', pattern: />/ });
export const EQ = createToken({ name: 'EQ', pattern: /=/ });

export const LPAREN = createToken({ name: 'LPAREN', pattern: /\(/ });
export const RPAREN = createToken({ name: 'RPAREN', pattern: /\)/ });
export const LBRACKET = createToken({ name: 'LBRACKET', pattern: /\[/ });
export const RBRACKET = createToken({ name: 'RBRACKET', pattern: /\]/ });
export const LBRACE = createToken({ name: 'LBRACE', pattern: /\{/ });
export const RBRACE = createToken({ name: 'RBRACE', pattern: /\}/ });
export const COMMA = createToken({ name: 'COMMA', pattern: /,/ });
export const SEMI = createToken({ name: 'SEMI', pattern: /;/ });
export const AT = createToken({ name: 'AT', pattern: /@/ });
export const DOT = createToken({ name: 'DOT', pattern: /\./ });
export const AMP = createToken({ name: 'AMP', pattern: /&/ });
export const PIPE = createToken({ name: 'PIPE', pattern: /\|/ });
export const TILDE = createToken({ name: 'TILDE', pattern: /~/ });
export const COLON = createToken({ name: 'COLON', pattern: /:/ });
export const STAR = createToken({ name: 'STAR', pattern: /\*/ });
export const MINUS = createToken({ name: 'MINUS', pattern: /-/ });

// NAME — mirrors the old lexer's Sprint-10 mid-word-hyphen widening: a `-`
// is only ever consumed when immediately followed by another word char,
// never leading, never trailing.
export const NAME = createToken({ name: 'NAME', pattern: /[A-Za-z_][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+)*/ });

export const allTokens = [
  WhiteSpace,
  NUMBER, STRING,
  LE, GE, EQEQ, NE, LT, GT, EQ,
  LPAREN, RPAREN, LBRACKET, RBRACKET, LBRACE, RBRACE,
  COMMA, SEMI, AT, DOT, AMP, PIPE, TILDE, COLON, STAR, MINUS,
  NAME,
];

export const AgentsLexer = new Lexer(allTokens, { ensureOptimizations: false });

/** Lex `text` (already stripped of any trailing top-level meta region —
 *  see `findTopLevelMetaStart` in agentsParser.js) into a Chevrotain token
 *  vector + a normalized `{message, position}` error list, mirroring the
 *  old `lexAgentStatement`'s `{tokens, errors}` return shape closely enough
 *  for the differential harness to compare. */
export function lexAgentStatementNew(text) {
  if (typeof text !== 'string') return { tokens: [], errors: [{ message: 'statement must be a string', position: null }] };
  const result = AgentsLexer.tokenize(text);
  if (result.errors.length) {
    const e = result.errors[0];
    const ch = text[e.offset];
    // Mirrors agents.js's own lexer special-case (Sprint 20 item 3) and
    // agentsParser.js's `_lexCore` — see that function's comment.
    if (ch === '!') {
      return { tokens: [], errors: [{ message: `unexpected '!' — use '~' or 'not' to negate, '!=' for not-equal`, position: e.offset }] };
    }
    return { tokens: [], errors: [{ message: `unexpected character '${ch}'`, position: e.offset }] };
  }
  return { tokens: result.tokens, errors: [] };
}
