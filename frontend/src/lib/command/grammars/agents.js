// Agent CLI grammar — Sprint 2 (core parser/compiler, no UI yet).
//
// Full design: .claude/PLAN.md / ~/.claude/plans/purrfect-marinating-pixel.md.
// This file is NOT a hand-written per-token grammar like orders.js — it's a
// generic, SCHEMA-DRIVEN builder that resolves every metric/scope/channel/
// action token against the live backend catalog (`fetchGrammarTokens`), plus
// a hand-rolled recursive-descent parser for the full WHEN/ALERT/DO boolean-
// tree grammar `engine.js` has no equivalent of, plus a compiler to the exact
// JSON shape the existing agent-create/update and ticket/basket endpoints
// already accept, plus an exhaustive semantic validator.
//
// ─────────────────────────────────────────────────────────────────────────
// EBNF (implemented exactly — see .claude/PLAN.md for the authoritative copy)
// ─────────────────────────────────────────────────────────────────────────
//   statement      := agent_stmt | order_stmt
//   agent_stmt     := "WHEN" condition "ALERT" alert_clause "DO" do_clause
//   alert_clause   := "nop" | call_list
//   do_clause      := "nop" | call_list
//   call_list      := call ("," call)*
//   order_stmt     := call ("," call)*            (* order/place_order only *)
//   condition      := "always" | or_expr
//   or_expr        := term (("&" | "|") term)*     (* equal precedence, L-to-R *)
//   term           := "~" primary | primary
//   primary        := "(" or_expr ")" | leaf
//   leaf           := metric_ref comparator value
//                    | value comparator metric_ref comparator value   (* between *)
//   metric_ref     := call "@" scope_ref
//   scope_ref      := call ("." call)*
//   comparator     := "<" | "<=" | ">" | ">=" | "==" | "!=" | "in" | "not in"
//   value          := NUMBER | STRING | NAME | list_literal
//   list_literal   := "[" value ("," value)* "]"
//   call           := NAME | NAME "(" arg_list ")"
//   arg_list       := kw_arg ("," kw_arg)*          (* EVERY arg is keyword —
//                                                       including the first;
//                                                       no positional form
//                                                       exists in this
//                                                       grammar at all *)
//   kw_arg         := NAME "=" value
//
// ─────────────────────────────────────────────────────────────────────────
// AST shape (internal, documented for Sprint 3's decompiler / Sprint 4's
// live-preview — NOT the compiled JSON, see compile*() below for that)
// ─────────────────────────────────────────────────────────────────────────
//   Statement  := { kind:'agent', condition, alertCalls, doCalls }
//               | { kind:'order', calls: CallNode[] }
//   Condition  := { type:'always' }
//               | { type:'and'|'or', left: Condition, right: Condition }
//               | { type:'not', term: Condition }
//               | { type:'leaf', metric: CallNode, scope: ScopeNode,
//                   op: string|null, value: ValueNode|null }
//               | { type:'between', value1: ValueNode, op1: string,
//                   metric: CallNode, scope: ScopeNode, op2: string,
//                   value2: ValueNode }
//   CallNode   := { type:'call', name: string, args: ArgEntry[],
//                   hasCall: boolean, start, end }
//   ArgEntry   := { key: string, value: ValueNode }   (* key is NEVER null —
//                   every argument is keyword-only, including the first *)
//   ScopeNode  := { type:'scope', segments: CallNode[] }
//   ValueNode  := { type:'number'|'string'|'name', value }
//               | { type:'list', value: ValueNode[] }
//
// ─────────────────────────────────────────────────────────────────────────
// KNOWN GAPS between this design and the LIVE backend catalog (documented
// here rather than silently "fixed" — implement the MECHANISM generically
// per the design doc; these are catalog-content facts, not grammar bugs):
//   - `always`'s sentinel scope is `funds.any_acct` (confirmed against the
//     real `market-open-nse` builtin agent in agent_engine.py), NOT
//     `funds.total` as one earlier draft guessed.
//   - The boolean-metric shorthand is keyed off `value_type === 'boolean'`
//     exactly as specified, but NO metric in the live catalog is actually
//     typed 'boolean' today — `is_itm`/`is_ntm` are `value_type: 'number'`
//     and `is_future` has no `value_type` at all. The mechanism is correct
//     and future-proof; it simply has nothing to fire against yet. Tests
//     exercise it via a fixture catalog that declares a real boolean metric.
//   - `ntfy` (used in the design doc's own canonical example 2) does not
//     exist in the live `notify`/`channel` catalog. Implemented generically;
//     tests use a fixture catalog that adds it.
//   - `side` (used in the design doc's own operator-table example,
//     `side in [BUY, SELL]`) is not a real condition/metric token in the
//     live catalog. `in`/`not_in` are implemented generically against ANY
//     metric; tests use a fixture metric for this.
//   - RESOLVED (was flagged in an earlier revision of this sprint, kept here
//     as a record of why the design changed): `params_schema` is stored as
//     Postgres JSONB (`backend/api/models.py: GrammarToken.params_schema`),
//     which does not guarantee object key order survives a round-trip
//     through the DB. The original design read `paramKeys[0]` as "the
//     positional-or-keyword slot," which would have silently depended on
//     that unguaranteed order in production even though every test (JS
//     object literals DO preserve order) would have passed regardless. The
//     operator's fix: remove positional arguments from the grammar
//     entirely — every argument is always keyword (`arg_list := kw_arg
//     ("," kw_arg)*` above), so `_mapCallArgsToKeys` only ever looks up a
//     key by NAME, never by position. `paramKeys`' array order is now used
//     only for cosmetic/display purposes (if at all), never for binding
//     correctness — no backend change was needed.

import { fetchGrammarTokens } from '$lib/api';
import { getInstrument } from '$lib/data/instruments';
import { tokenizeCallStyle } from '$lib/command/engine.js';

// ── Depth/length guards — mirrors backend/api/algo/expr_eval.py's
//    _MAX_EXPR_LEN / _MAX_DEPTH philosophy (cheap guards against a
//    pathological paste freezing the parser via runaway recursion), scaled
//    up from expr_eval's 200-char single-expression cap since a REALISTIC
//    multi-leg agent statement (several leaves, several DO calls) is
//    legitimately much longer than a single arithmetic sub-expression —
//    the nesting-DEPTH guard is the one that matters for recursion safety
//    and is kept at the SAME value expr_eval.py uses. ──────────────────────
const _MAX_STATEMENT_LEN = 4000;
const _MAX_DEPTH = 20;

// ── Reserved words — checked before generic NAME classification, never
//    treated as a callable token name. ──────────────────────────────────
const _RESERVED = new Set(['when', 'alert', 'do', 'nop', 'always', 'in', 'not', 'true', 'false']);
export const RESERVED_WORDS = _RESERVED;

// ── `always` sentinel leaf — the exact shape the real `market-open-nse` /
//    `market-preclose-mcx` builtin agents use (agent_engine.py ~1578-1590).
export const ALWAYS_LEAF = Object.freeze({
  metric: 'avail_margin', scope: 'funds.any_acct', op: '>=', value: -999999999,
});

// ── Shortened-keyword-name alias table (presentation-only; CLI typing
//    convenience, never written to the output JSON — the compiler always
//    maps back to the real schema key). `chase_level: 'chase'` reuses the
//    name ALREADY established in orders.yaml (role: chase, $ref:
//    chase_level) — not renamed here. ─────────────────────────────────────
export const KWARG_ALIASES = Object.freeze({
  account: 'acct',
  symbol: 'sym',
  exchange: 'exch',
  order_type: 'otype',
  price: 'px',
  product: 'prod',
  variety: 'var',
  chase_level: 'chase',
});
const _ALIAS_TO_REAL = Object.fromEntries(
  Object.entries(KWARG_ALIASES).map(([real, alias]) => [alias.toLowerCase(), real])
);

// ═══════════════════════════════════════════════════════════════════════
// (a) Catalog fetch + generic grammar builder
// ═══════════════════════════════════════════════════════════════════════

/** Build a lookup catalog from raw grammar_tokens rows (mixed grammar_kind).
 *  Pure — no network. `fetchAgentCatalog()` below is the thin I/O wrapper,
 *  kept separate so every parser/compiler test can run against a plain
 *  fixture array with zero network/auth mocking. */
export function buildCatalog(rows) {
  const metrics = new Map();
  const scopes = new Map();
  const channels = new Map();
  const actions = new Map();
  for (const row of rows || []) {
    if (!row || row.is_active === false) continue;
    const entry = _buildCatalogEntry(row);
    const key = String(row.token).toLowerCase();
    if (row.grammar_kind === 'condition' && row.token_kind === 'metric') metrics.set(key, entry);
    else if (row.grammar_kind === 'condition' && row.token_kind === 'scope') scopes.set(key, entry);
    else if (row.grammar_kind === 'notify' && row.token_kind === 'channel') channels.set(key, entry);
    else if (row.grammar_kind === 'action' && row.token_kind === 'action_type') actions.set(key, entry);
  }
  return { metrics, scopes, channels, actions };
}

function _buildCatalogEntry(row) {
  const schema = row.params_schema || {};
  return {
    token: row.token,              // canonical stored casing
    valueType: row.value_type || null,
    paramsSchema: schema,
    paramKeys: Object.keys(schema), // declared order — keys[0] is positional-or-keyword
  };
}

/** Fetch the live catalog (condition + notify + action kinds) and build it.
 *  The ONLY function in this file that touches the network — every other
 *  export is pure and testable with a fixture catalog from `buildCatalog`. */
export async function fetchAgentCatalog() {
  const [condRows, notifyRows, actionRows] = await Promise.all([
    fetchGrammarTokens('condition'),
    fetchGrammarTokens('notify'),
    fetchGrammarTokens('action'),
  ]);
  return buildCatalog([...(condRows || []), ...(notifyRows || []), ...(actionRows || [])]);
}

/** Default lot-size resolver — reuses the SAME instruments-cache lookup
 *  orders.js's own resolveInstrument()/getInstrument() mechanism is built
 *  on, rather than inventing a second lot-size source. Returns null (never
 *  1) on any miss — callers must treat null as "unresolvable", not "no
 *  lots". */
function _defaultLotSizeOf(symbol) {
  try {
    const inst = getInstrument(symbol);
    const ls = inst && inst.ls != null ? Number(inst.ls) : null;
    return ls && ls > 0 ? ls : null;
  } catch { return null; }
}

// ═══════════════════════════════════════════════════════════════════════
// Error helper
// ═══════════════════════════════════════════════════════════════════════

function _err(message, token) {
  return { message, position: token && typeof token.start === 'number' ? token.start : undefined };
}

class _ParseError extends Error {
  constructor(message, token) {
    super(message);
    this.position = token && typeof token.start === 'number' ? token.start : undefined;
  }
}

// ═══════════════════════════════════════════════════════════════════════
// (b) Lexer
// ═══════════════════════════════════════════════════════════════════════

const _PUNCT_SINGLE = {
  '(': 'LPAREN', ')': 'RPAREN', '[': 'LBRACKET', ']': 'RBRACKET',
  ',': 'COMMA', '@': 'AT', '.': 'DOT', '&': 'AMP', '|': 'PIPE', '~': 'TILDE',
};

function _isWordChar(ch) { return /[A-Za-z0-9_]/.test(ch); }
function _isWordStart(ch) { return /[A-Za-z_]/.test(ch); }
function _isDigit(ch) { return /[0-9]/.test(ch); }

/** Tokenize a value-ish fragment (from the inner-arg tokenizeCallStyle
 *  stream) into a ValueNode. */
function _valueNodeFromCallStyleToken(tok) {
  if (tok.quoted) return { type: 'string', value: tok.text };
  const text = tok.text;
  if (/^-?\d+(\.\d+)?$/.test(text)) return { type: 'number', value: Number(text) };
  return { type: 'name', value: text };
}

/** Parse a single comma-separated arg segment (array of tokenizeCallStyle
 *  tokens) into one ArgEntry, handling every spacing variant around `=`
 *  (fused `key=value`, fused `key="quoted value"`, spaced `key = value`,
 *  `key =value`, `key= value`). Throws _ParseError on malformed segments. */
function _parseArgSegment(segTokens, labelToken) {
  if (segTokens.length === 0) throw new _ParseError('empty argument (trailing/leading/double comma)', labelToken);
  const first = segTokens[0];
  if (first.kwarg) {
    if (first.kwarg.value === '' && segTokens.length > 1) {
      // `key= value` — value is the glued-nothing kwarg continued by the
      // next token.
      if (segTokens.length > 2) throw new _ParseError('unexpected token in argument', segTokens[2]);
      return { key: first.kwarg.key, value: _valueNodeFromCallStyleToken(segTokens[1]) };
    }
    if (segTokens.length > 1) throw new _ParseError('unexpected token in argument', segTokens[1]);
    return {
      key: first.kwarg.key,
      value: first.kwarg.quoted ? { type: 'string', value: first.kwarg.value } : _valueNodeFromCallStyleToken({ text: first.kwarg.value }),
    };
  }
  if (!first.punct && !first.quoted && segTokens.length > 1) {
    const second = segTokens[1];
    if (!second.punct && !second.quoted && !second.kwarg && second.text === '=') {
      // spaced `key = value`
      if (segTokens.length < 3) throw new _ParseError('expected a value after "="', second);
      if (segTokens.length > 3) throw new _ParseError('unexpected token in argument', segTokens[3]);
      return { key: first.text, value: _valueNodeFromCallStyleToken(segTokens[2]) };
    }
    if (!second.punct && !second.quoted && !second.kwarg && second.text.startsWith('=') && second.text.length > 1) {
      // `key =value` — second token is glued "=value"
      if (segTokens.length > 2) throw new _ParseError('unexpected token in argument', segTokens[2]);
      return { key: first.text, value: _valueNodeFromCallStyleToken({ text: second.text.slice(1) }) };
    }
  }
  if (first.punct) throw new _ParseError(`unexpected '${first.punct}' in argument list`, first);
  // Every argument is always keyword — including the first — per the
  // operator's explicit decision (this also fully removes the only case
  // where a schema's params_schema key ORDER mattered for correctness;
  // see the "Known gaps" note below on jsonb not guaranteeing key order).
  throw new _ParseError(
    `argument must be written as name=value (every argument is keyword-only, including the first)`,
    first,
  );
}

/** Parse the inner text of a call's `(...)` into ArgEntry[], via
 *  engine.js's tokenizeCallStyle() — the Sprint 2 reuse point: a `call`'s
 *  argument list has NO extra punctuation beyond what tokenizeCallStyle
 *  already understands (names, numbers, quoted strings, `key=value`,
 *  commas) — list literals as a CALL ARGUMENT value (as opposed to a leaf's
 *  RHS value, which the outer lexer below handles directly) are a known,
 *  documented scope limitation: tokenizeCallStyle has no `[`/`]` awareness,
 *  so a bracketed list typed as a call arg will not parse as a list — no
 *  example in the design doc's grammar needs this, so it is out of scope
 *  for Sprint 2. */
function _parseArgListFromInnerText(innerText, labelToken) {
  const raw = innerText.trim();
  if (raw === '') return [];
  const callTokens = tokenizeCallStyle(innerText);
  if (callTokens.length === 0) return [];
  // Split on top-level commas (no nesting possible inside — see docstring).
  const segments = [[]];
  for (const t of callTokens) {
    if (t.punct === ',') segments.push([]);
    else segments[segments.length - 1].push(t);
  }
  return segments.map(seg => _parseArgSegment(seg, labelToken));
}

/** Lex a full statement into a flat token stream. Returns
 *  `{ tokens, errors }` — `errors` is a structured list (never throws out
 *  of this function); `tokens` is `[]` on a lex error. */
export function lexAgentStatement(text) {
  if (typeof text !== 'string') return { tokens: [], errors: [_err('statement must be a string')] };
  if (text.length > _MAX_STATEMENT_LEN) {
    return { tokens: [], errors: [_err(`statement too long (${text.length} chars > ${_MAX_STATEMENT_LEN} cap)`)] };
  }
  // Cheap text-level paren-depth guard before any real scanning — mirrors
  // expr_eval.py's _check_paren_depth rationale (catch pathological nesting
  // before it ever drives recursion). Quote-aware (parens inside a quoted
  // string literal, e.g. emit_log(message="(((hi)))"), must never count —
  // otherwise a legitimate message containing parens could be falsely
  // rejected. Deliberately counts a metric/scope/action CALL's own
  // "(...)" toward this depth too, even though those are absorbed into one
  // token at lex time and never actually recurse through the parser's own
  // depth-tracked productions (parsePrimary's "(" / parseTerm's "~") — this
  // makes the pre-scan a conservative (slightly stricter) over-approximation
  // of the real recursion risk, same spirit as expr_eval.py's own guard:
  // cheap and approximate, not a precise parse.
  let depth = 0;
  let inQuote = null;
  for (const ch of text) {
    if (inQuote) { if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; continue; }
    if (ch === '(') { depth++; if (depth > _MAX_DEPTH) return { tokens: [], errors: [_err(`nesting too deep (> ${_MAX_DEPTH})`)] }; }
    else if (ch === ')') depth--;
  }

  const tokens = [];
  let i = 0;
  const n = text.length;
  try {
    while (i < n) {
      while (i < n && /\s/.test(text[i])) i++;
      if (i >= n) break;
      const start = i;
      const ch = text[i];

      if (ch === '"' || ch === "'") {
        const quote = ch;
        i++;
        while (i < n && text[i] !== quote) i++;
        if (i >= n) throw new _ParseError('unterminated string literal', { start });
        i++;
        tokens.push({ type: 'STRING', value: text.slice(start + 1, i - 1), start, end: i });
        continue;
      }

      if (ch === '<' || ch === '>' || ch === '=' || ch === '!') {
        const two = text.slice(i, i + 2);
        if (ch === '<') { if (two === '<=') { tokens.push({ type: 'LE', start, end: i + 2 }); i += 2; } else { tokens.push({ type: 'LT', start, end: i + 1 }); i += 1; } continue; }
        if (ch === '>') { if (two === '>=') { tokens.push({ type: 'GE', start, end: i + 2 }); i += 2; } else { tokens.push({ type: 'GT', start, end: i + 1 }); i += 1; } continue; }
        if (ch === '=') { if (two === '==') { tokens.push({ type: 'EQEQ', start, end: i + 2 }); i += 2; continue; } throw new _ParseError(`unexpected '='`, { start }); }
        if (ch === '!') { if (two === '!=') { tokens.push({ type: 'NE', start, end: i + 2 }); i += 2; continue; } throw new _ParseError(`unexpected '!'`, { start }); }
      }

      if (_PUNCT_SINGLE[ch]) {
        tokens.push({ type: _PUNCT_SINGLE[ch], start, end: i + 1 });
        i++;
        continue;
      }

      if (_isDigit(ch) || (ch === '-' && _isDigit(text[i + 1] || ''))) {
        let j = i + (ch === '-' ? 1 : 0);
        while (j < n && _isDigit(text[j])) j++;
        if (text[j] === '.' && _isDigit(text[j + 1] || '')) {
          j++;
          while (j < n && _isDigit(text[j])) j++;
        }
        tokens.push({ type: 'NUMBER', value: Number(text.slice(start, j)), start, end: j });
        i = j;
        continue;
      }

      if (_isWordStart(ch)) {
        let j = i;
        while (j < n && _isWordChar(text[j])) j++;
        const nameRaw = text.slice(start, j);
        i = j;
        const lower = nameRaw.toLowerCase();
        if (text[i] === '(') {
          // CALL — extract the balanced-paren substring (quote-aware).
          const innerStart = i + 1;
          let pd = 1;
          let k = innerStart;
          while (k < n && pd > 0) {
            const c = text[k];
            if (c === '"' || c === "'") {
              const q = c; k++;
              while (k < n && text[k] !== q) k++;
              if (k >= n) throw new _ParseError('unterminated string literal inside call arguments', { start: innerStart });
              k++;
              continue;
            }
            if (c === '(') pd++;
            else if (c === ')') pd--;
            k++;
          }
          if (pd !== 0) throw new _ParseError(`unbalanced parentheses after '${nameRaw}'`, { start });
          const innerEnd = k - 1;
          const innerText = text.slice(innerStart, innerEnd);
          // Note: innerText.trim() !== '' always yields args.length >= 1 or
          // a thrown _ParseError (tokenizeCallStyle never silently drops a
          // non-whitespace char) — no separate "non-empty but zero args"
          // case to guard here.
          const args = _parseArgListFromInnerText(innerText, { start });
          tokens.push({ type: 'NAME', value: nameRaw, reserved: _RESERVED.has(lower), hasCall: true, args, start, end: k });
          i = k;
        } else {
          tokens.push({ type: 'NAME', value: nameRaw, reserved: _RESERVED.has(lower), hasCall: false, args: null, start, end: i });
        }
        continue;
      }

      throw new _ParseError(`unexpected character '${ch}'`, { start });
    }
  } catch (e) {
    if (e instanceof _ParseError) return { tokens: [], errors: [{ message: e.message, position: e.position }] };
    throw e;
  }
  tokens.push({ type: 'EOF', start: n, end: n });
  return { tokens, errors: [] };
}

// ═══════════════════════════════════════════════════════════════════════
// (c) Recursive-descent parser
// ═══════════════════════════════════════════════════════════════════════

function _peek(state) { return state.tokens[state.pos]; }
function _advance(state) { return state.tokens[state.pos++]; }
function _wordIs(tok, word) { return tok.type === 'NAME' && tok.value.toLowerCase() === word; }

function _expectWord(state, word) {
  const tok = _peek(state);
  if (!_wordIs(tok, word)) throw new _ParseError(`expected "${word.toUpperCase()}"`, tok);
  return _advance(state);
}

function _checkDepth(depth, tok) {
  if (depth > _MAX_DEPTH) throw new _ParseError(`expression nesting too deep (> ${_MAX_DEPTH})`, tok);
}

/** `call := NAME | NAME "(" arg_list ")"` */
export function parseCall(state) {
  const tok = _peek(state);
  if (tok.type !== 'NAME') throw new _ParseError('expected a name', tok);
  if (tok.reserved) throw new _ParseError(`"${tok.value}" is a reserved word and cannot be used as a token name`, tok);
  if (tok.hasCall && (!tok.args || tok.args.length === 0)) {
    throw new _ParseError(`"${tok.value}()" is a parse error — a zero-argument token is always written bare`, tok);
  }
  _advance(state);
  return { type: 'call', name: tok.value, args: tok.args || [], hasCall: tok.hasCall, start: tok.start, end: tok.end };
}

/** `arg_list := first_arg ("," kw_arg)*` — args are resolved at LEX time
 *  (tokenizeCallStyle reuse point, see `_parseArgListFromInnerText`); this
 *  accessor exists for EBNF-production naming parity with the formal
 *  grammar and for Sprint 3/4 consumers that want to inspect a call's
 *  already-parsed argument list without re-deriving it. */
export function parseArgList(callNode) { return callNode.args || []; }

/** `scope_ref := call ("." call)*` */
export function parseScopeRef(state) {
  const segments = [parseCall(state)];
  while (_peek(state).type === 'DOT') {
    _advance(state);
    segments.push(parseCall(state));
  }
  return { type: 'scope', segments };
}

/** `metric_ref := call "@" scope_ref` */
export function parseMetricRef(state) {
  const metric = parseCall(state);
  const at = _peek(state);
  if (at.type !== 'AT') throw new _ParseError(`expected "@" after metric '${metric.name}'`, at);
  _advance(state);
  const scope = parseScopeRef(state);
  return { metric, scope };
}

/** `list_literal := "[" value ("," value)* "]"` */
export function parseListLiteral(state) {
  const open = _peek(state);
  if (open.type !== 'LBRACKET') throw new _ParseError('expected "["', open);
  _advance(state);
  const items = [];
  if (_peek(state).type !== 'RBRACKET') {
    items.push(parseValue(state));
    while (_peek(state).type === 'COMMA') {
      _advance(state);
      if (_peek(state).type === 'RBRACKET') throw new _ParseError('trailing comma in list literal', _peek(state));
      items.push(parseValue(state));
    }
  }
  const close = _peek(state);
  if (close.type !== 'RBRACKET') throw new _ParseError('expected "]"', close);
  _advance(state);
  return { type: 'list', value: items };
}

/** `value := NUMBER | STRING | NAME | list_literal` */
export function parseValue(state) {
  const tok = _peek(state);
  if (tok.type === 'NUMBER') { _advance(state); return { type: 'number', value: tok.value }; }
  if (tok.type === 'STRING') { _advance(state); return { type: 'string', value: tok.value }; }
  if (tok.type === 'LBRACKET') return parseListLiteral(state);
  if (tok.type === 'NAME' && !tok.hasCall) {
    if (tok.reserved && tok.value.toLowerCase() !== 'true' && tok.value.toLowerCase() !== 'false') {
      throw new _ParseError(`"${tok.value}" is a reserved word and cannot be used as a value here`, tok);
    }
    _advance(state);
    return { type: 'name', value: tok.value };
  }
  throw new _ParseError('expected a value (number, string, or bare name)', tok);
}

const _COMPARATOR_MAP = { LT: '<', LE: '<=', GT: '>', GE: '>=', EQEQ: '==', NE: '!=' };

function _isComparatorStart(tok) {
  if (_COMPARATOR_MAP[tok.type]) return true;
  return tok.type === 'NAME' && (_wordIs(tok, 'in') || _wordIs(tok, 'not'));
}

function parseComparator(state) {
  const tok = _peek(state);
  if (_COMPARATOR_MAP[tok.type]) { _advance(state); return _COMPARATOR_MAP[tok.type]; }
  if (tok.type === 'NAME' && _wordIs(tok, 'not')) {
    _advance(state);
    const t2 = _peek(state);
    if (!(t2.type === 'NAME' && _wordIs(t2, 'in'))) throw new _ParseError('expected "in" after "not"', t2);
    _advance(state);
    return 'not_in';
  }
  if (tok.type === 'NAME' && _wordIs(tok, 'in')) { _advance(state); return 'in'; }
  throw new _ParseError('expected a comparator', tok);
}

function _isLiteralStart(tok) {
  return tok.type === 'NUMBER' || tok.type === 'STRING' || tok.type === 'LBRACKET';
}

/** `leaf := metric_ref comparator value | value comparator metric_ref comparator value` */
export function parseLeaf(state) {
  const startTok = _peek(state);
  if (_isLiteralStart(startTok)) {
    const value1 = parseValue(state);
    const op1 = parseComparator(state);
    const { metric, scope } = parseMetricRef(state);
    const op2 = parseComparator(state);
    const value2 = parseValue(state);
    return { type: 'between', value1, op1, metric, scope, op2, value2 };
  }
  const { metric, scope } = parseMetricRef(state);
  if (_isComparatorStart(_peek(state))) {
    const op = parseComparator(state);
    const value = parseValue(state);
    return { type: 'leaf', metric, scope, op, value };
  }
  // Boolean-metric shorthand — syntactically accepted here; the semantic
  // check ("was this metric actually boolean?") happens at compile time,
  // once the token is resolved against the catalog (see design doc).
  return { type: 'leaf', metric, scope, op: null, value: null };
}

/** `primary := "(" or_expr ")" | leaf` — `depth` increments EXACTLY ONCE
 *  per actual `(` nesting level here (and once per `~` in `parseTerm`
 *  below) — these are the only two productions that recurse into a NEW
 *  nesting level; `parseOrExpr`/`parseTerm` passing `depth` straight
 *  through (not +1) keeps the guard's ">20" semantics matching the
 *  operator-visible nesting depth 1:1, not inflated by grammar-structure
 *  hops that aren't real nesting. */
export function parsePrimary(state, depth = 0) {
  const tok = _peek(state);
  if (tok.type === 'LPAREN') {
    _checkDepth(depth + 1, tok);
    _advance(state);
    const inner = parseOrExpr(state, depth + 1);
    const close = _peek(state);
    if (close.type !== 'RPAREN') throw new _ParseError('expected ")"', close);
    _advance(state);
    return inner;
  }
  return parseLeaf(state);
}

/** `term := "~" primary | primary` */
export function parseTerm(state, depth = 0) {
  const tok = _peek(state);
  if (tok.type === 'TILDE') {
    _checkDepth(depth + 1, tok);
    _advance(state);
    const inner = parsePrimary(state, depth + 1);
    return { type: 'not', term: inner };
  }
  return parsePrimary(state, depth);
}

/** `or_expr := term (("&" | "|") term)*` — equal precedence, strictly
 *  left-to-right (folded as a left-leaning binary chain, NOT an n-ary node —
 *  this correctly represents e.g. `a & b | c` as `(a & b) | c` without
 *  pretending `&`/`|` share one node type). */
export function parseOrExpr(state, depth = 0) {
  let node = parseTerm(state, depth);
  while (true) {
    const tok = _peek(state);
    if (tok.type === 'AMP') { _advance(state); node = { type: 'and', left: node, right: parseTerm(state, depth) }; continue; }
    if (tok.type === 'PIPE') { _advance(state); node = { type: 'or', left: node, right: parseTerm(state, depth) }; continue; }
    break;
  }
  return node;
}

/** `condition := "always" | or_expr` — "always" is standalone-only: a
 *  parse error, not a silently-tolerated redundancy, if followed by `&`
 *  or `|` right here at the top level. `always` appearing as an OPERAND
 *  deeper in an expression (e.g. `x | always`) is caught differently —
 *  `parseCall` rejects any reserved word used as a token name, which
 *  `always` always is outside this one top-level sentinel spot — still a
 *  clear parse error, just a different message. */
export function parseCondition(state, depth = 0) {
  const tok = _peek(state);
  if (tok.type === 'NAME' && _wordIs(tok, 'always') && !tok.hasCall) {
    _advance(state);
    const next = _peek(state);
    if (next.type === 'AMP' || next.type === 'PIPE') {
      throw new _ParseError('"always" is standalone-only — it cannot be combined with & or |', next);
    }
    return { type: 'always' };
  }
  return parseOrExpr(state, depth);
}

/** `call_list := call ("," call)*` */
function parseCallList(state) {
  const calls = [parseCall(state)];
  while (_peek(state).type === 'COMMA') {
    _advance(state);
    calls.push(parseCall(state));
  }
  return calls;
}

function parseClauseBody(state) {
  const tok = _peek(state);
  if (tok.type === 'NAME' && _wordIs(tok, 'nop')) {
    if (tok.hasCall) throw new _ParseError('"nop" takes no arguments — nop() is a parse error', tok);
    _advance(state);
    return 'nop';
  }
  return parseCallList(state);
}

/** `agent_stmt := "WHEN" condition "ALERT" alert_clause "DO" do_clause` */
export function parseAgentStmt(state) {
  _expectWord(state, 'when');
  const condition = parseCondition(state, 0);
  _expectWord(state, 'alert');
  const alertCalls = parseClauseBody(state);
  _expectWord(state, 'do');
  const doCalls = parseClauseBody(state);
  return { kind: 'agent', condition, alertCalls, doCalls };
}

/** `order_stmt := call ("," call)*` — restricted to order/place_order calls. */
export function parseOrderStmt(state) {
  const calls = parseCallList(state);
  for (const c of calls) {
    const lname = c.name.toLowerCase();
    if (lname !== 'order' && lname !== 'place_order') {
      throw new _ParseError(
        'only order(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
        { start: c.start }
      );
    }
  }
  return { kind: 'order', calls };
}

/** `statement := agent_stmt | order_stmt` — top-level entry point.
 *  Returns `{ ast, errors }` — `ast` is `null` on any parse failure. */
export function parseStatement(text) {
  const { tokens, errors: lexErrors } = lexAgentStatement(text);
  if (lexErrors.length) return { ast: null, errors: lexErrors };
  const state = { tokens, pos: 0 };
  try {
    const first = _peek(state);
    const ast = (first.type === 'NAME' && _wordIs(first, 'when') && !first.hasCall)
      ? parseAgentStmt(state)
      : parseOrderStmt(state);
    const trailing = _peek(state);
    if (trailing.type !== 'EOF') throw new _ParseError('unexpected trailing input', trailing);
    return { ast, errors: [] };
  } catch (e) {
    if (e instanceof _ParseError) return { ast: null, errors: [{ message: e.message, position: e.position }] };
    throw e;
  }
}

// ═══════════════════════════════════════════════════════════════════════
// (d) Compiler — AST → request JSON
// ═══════════════════════════════════════════════════════════════════════

/** Map a call's args onto a schema's declared keys — every argument is
 *  ALWAYS keyword (no positional form exists in this grammar at all, per
 *  the operator's explicit decision), so this never depends on `keys`'
 *  ORDER, only on key NAMES — important because `params_schema` is stored
 *  as Postgres jsonb, which does not guarantee key order is preserved on
 *  round-trip (see the file-header "Known gaps" note). Alias table applied
 *  when `aliasToReal` is provided. Returns a `Map<realKey, ValueNode>`;
 *  errors are pushed onto the caller-supplied `errors` array (never
 *  thrown). `callNode.args` is guaranteed to have every entry's `key` set
 *  (never `null`) by the lexer — `_parseArgSegment` throws a parse error
 *  before a positional-shaped arg ever reaches here. */
function _mapCallArgsToKeys(callNode, keys, aliasToReal, errors, label) {
  const provided = new Map();
  (callNode.args || []).forEach((arg) => {
    const typedLower = arg.key.toLowerCase();
    let realKey = keys.find(k => k.toLowerCase() === typedLower) || null;
    if (!realKey && aliasToReal) {
      const aliased = aliasToReal[typedLower];
      if (aliased && keys.includes(aliased)) realKey = aliased;
    }
    if (!realKey) { errors.push(_err(`unknown parameter '${arg.key}' for ${label}`)); return; }
    if (provided.has(realKey)) { errors.push(_err(`${label}: duplicate parameter '${realKey}'`)); return; }
    provided.set(realKey, arg.value);
  });
  return provided;
}

/** Coerce one ValueNode against its params_schema field spec. Returns
 *  `{ value }` or `{ error }` — never throws. */
function _coerceValueForSpec(valueNode, spec, key, label) {
  const type = spec && spec.type;
  if (type === 'enum') {
    if (valueNode.type !== 'string' && valueNode.type !== 'name') return { error: _err(`${label}.${key}: expected an enum value`) };
    const raw = valueNode.value;
    const match = (spec.enum || []).find(e => String(e).toLowerCase() === String(raw).toLowerCase());
    if (!match) return { error: _err(`${label}.${key}: '${raw}' is not one of [${(spec.enum || []).join(', ')}]`) };
    return { value: match };
  }
  if (type === 'number') {
    if (valueNode.type === 'number') return { value: valueNode.value };
    if (valueNode.type === 'string' && spec.token_ref_ok) return { value: valueNode.value }; // expr string, evaluated server-side
    return { error: _err(`${label}.${key}: expected a number`) };
  }
  if (type === 'boolean') {
    if (valueNode.type === 'name') {
      const low = String(valueNode.value).toLowerCase();
      if (low === 'true') return { value: true };
      if (low === 'false') return { value: false };
    }
    return { error: _err(`${label}.${key}: expected true or false`) };
  }
  if (type === 'string') {
    if (valueNode.type === 'string') return { value: valueNode.value };
    return { error: _err(`${label}.${key}: free-text value must be quoted (e.g. ${key}="...")`) };
  }
  // Unknown/absent spec type — best-effort passthrough.
  if (valueNode.type === 'list') return { value: valueNode.value.map(v => v.value) };
  return { value: valueNode.value };
}

/** Describe which OTHER bucket a name resolves to in the catalog, for a
 *  clearer kind-mismatch error (e.g. "'emit_log' is an action — only
 *  metrics are allowed in a WHEN condition") instead of a bare "unknown
 *  X" when the token is actually real, just in the wrong clause. */
function _describeKind(name, catalog) {
  const key = String(name).toLowerCase();
  if (catalog.metrics.has(key)) return 'a metric';
  if (catalog.scopes.has(key)) return 'a scope';
  if (catalog.channels.has(key)) return 'a channel';
  if (catalog.actions.has(key)) return 'an action';
  return null;
}

function _resolveMetric(callNode, catalog, errors) {
  const entry = catalog.metrics.get(callNode.name.toLowerCase());
  if (!entry) {
    const other = _describeKind(callNode.name, catalog);
    errors.push(_err(other
      ? `'${callNode.name}' is ${other} — only metrics are allowed in a WHEN condition`
      : `unknown metric '${callNode.name}'`));
    return null;
  }
  return entry;
}

function _resolveScopeString(scopeNode, catalog, errors) {
  const parts = [];
  for (const seg of scopeNode.segments) {
    if (seg.args && seg.args.length) { errors.push(_err(`scope segment '${seg.name}' takes no arguments`)); return null; }
    parts.push(seg.name.toLowerCase());
  }
  const joined = parts.join('.');
  const entry = catalog.scopes.get(joined);
  if (!entry) { errors.push(_err(`unknown scope '${parts.join('.')}'`)); return null; }
  return entry.token;
}

/** Compiles a metric/scope `call` to its POSITIONAL-ONLY catalog string
 *  (e.g. `"mean_pnl(30)"`), regardless of whether the operator typed
 *  positional or keyword args — mirrors the live backend's
 *  `grammar_registry.py:_parse_call_token`, which ONLY accepts a single
 *  positional literal per declared param, EXACT arity (no optional/default
 *  params for metric/scope calls — unlike action/channel calls, which use
 *  per-key `required`). */
function _compileMetricCallString(callNode, entry, errors) {
  const keys = entry.paramKeys;
  if (keys.length === 0) {
    if (callNode.args && callNode.args.length) { errors.push(_err(`${entry.token} takes no arguments`)); return null; }
    return entry.token;
  }
  const localErrors = [];
  const provided = _mapCallArgsToKeys(callNode, keys, null, localErrors, entry.token);
  if (localErrors.length) { errors.push(...localErrors); return null; }
  const missing = keys.filter(k => !provided.has(k));
  if (missing.length) { errors.push(_err(`${entry.token} requires argument(s): ${missing.join(', ')}`)); return null; }
  const parts = [];
  for (const k of keys) {
    const v = provided.get(k);
    if (v.type !== 'number' || !(v.value > 0)) {
      errors.push(_err(`${entry.token}: argument '${k}' must be a positive numeric literal`));
      return null;
    }
    parts.push(String(v.value));
  }
  return `${entry.token}(${parts.join(',')})`;
}

function _compileListValue(listNode, metricEntry, errors) {
  const isNumeric = metricEntry.valueType === 'number' || !metricEntry.valueType;
  const out = [];
  for (const item of listNode.value) {
    if (isNumeric) {
      if (item.type !== 'number') { errors.push(_err(`list literal: expected a number`)); return null; }
      out.push(item.value);
    } else {
      out.push(item.value);
    }
  }
  return out;
}

function compileLeaf(node, catalog) {
  const errors = [];
  const metricEntry = _resolveMetric(node.metric, catalog, errors);
  const scopeStr = metricEntry ? _resolveScopeString(node.scope, catalog, errors) : null;
  if (errors.length) return { conditions: null, errors };
  const metricStr = _compileMetricCallString(node.metric, metricEntry, errors);
  if (errors.length) return { conditions: null, errors };

  if (node.op === null) {
    if (metricEntry.valueType === 'boolean') {
      return { conditions: { metric: metricStr, scope: scopeStr, op: '==', value: true }, errors: [] };
    }
    errors.push(_err(`expected a comparator after a numeric metric '${metricEntry.token}'`));
    return { conditions: null, errors };
  }

  if (node.op === 'in' || node.op === 'not_in') {
    if (node.value.type !== 'list') {
      errors.push(_err(`'${node.op === 'in' ? 'in' : 'not in'}' requires a list literal on the right`));
      return { conditions: null, errors };
    }
    const list = _compileListValue(node.value, metricEntry, errors);
    if (errors.length) return { conditions: null, errors };
    return { conditions: { metric: metricStr, scope: scopeStr, op: node.op, value: list }, errors: [] };
  }

  if (metricEntry.valueType === 'boolean') {
    if (node.op !== '==' && node.op !== '!=') {
      errors.push(_err(`boolean metric '${metricEntry.token}' only supports == / !=`));
      return { conditions: null, errors };
    }
    if (node.value.type !== 'name' || !['true', 'false'].includes(String(node.value.value).toLowerCase())) {
      errors.push(_err(`${metricEntry.token}: expected true or false`));
      return { conditions: null, errors };
    }
    return { conditions: { metric: metricStr, scope: scopeStr, op: node.op, value: String(node.value.value).toLowerCase() === 'true' }, errors: [] };
  }

  if (node.value.type !== 'number') {
    errors.push(_err('comparator requires both sides numeric'));
    return { conditions: null, errors };
  }
  return { conditions: { metric: metricStr, scope: scopeStr, op: node.op, value: node.value.value }, errors: [] };
}

const _BETWEEN_FLIP = { '<': '>', '<=': '>=', '>': '<', '>=': '<=' };

function compileBetween(node, catalog) {
  const errors = [];
  const metricEntry = _resolveMetric(node.metric, catalog, errors);
  const scopeStr = metricEntry ? _resolveScopeString(node.scope, catalog, errors) : null;
  if (errors.length) return { conditions: null, errors };
  const metricStr = _compileMetricCallString(node.metric, metricEntry, errors);
  if (errors.length) return { conditions: null, errors };

  if (node.value1.type !== 'number' || node.value2.type !== 'number') {
    errors.push(_err('chained ("between") comparison literals must be numeric'));
    return { conditions: null, errors };
  }
  if (metricEntry.valueType && metricEntry.valueType !== 'number') {
    errors.push(_err('chained ("between") comparison requires a numeric metric'));
    return { conditions: null, errors };
  }

  const lowSet = new Set(['<', '<=']);
  const highSet = new Set(['>', '>=']);
  const dirLow = lowSet.has(node.op1) && lowSet.has(node.op2);
  const dirHigh = highSet.has(node.op1) && highSet.has(node.op2);
  if (!dirLow && !dirHigh) {
    errors.push(_err('mixed-direction chained comparison — both sides must point the same direction'));
    return { conditions: null, errors };
  }

  const inclusive = (node.op1 === '<=' && node.op2 === '<=') || (node.op1 === '>=' && node.op2 === '>=');
  if (inclusive) {
    const [low, high] = dirLow ? [node.value1.value, node.value2.value] : [node.value2.value, node.value1.value];
    return { conditions: { metric: metricStr, scope: scopeStr, op: 'between', value: [low, high] }, errors: [] };
  }
  // Strict / mixed-strictness chain — the backend's `between` op is ALWAYS
  // inclusive (`lambda a,b: b[0] <= a <= b[1]`), so compiling a strict
  // chain to it would silently loosen the condition. Compile to an
  // explicit `all` of the two original (direction-correct) comparisons
  // instead — see .claude/PLAN.md Step 5's "between" design note.
  const leaf1 = { metric: metricStr, scope: scopeStr, op: _BETWEEN_FLIP[node.op1], value: node.value1.value };
  const leaf2 = { metric: metricStr, scope: scopeStr, op: node.op2, value: node.value2.value };
  return { conditions: { all: [leaf1, leaf2] }, errors: [] };
}

function compileCondNode(node, catalog) {
  if (node.type === 'and' || node.type === 'or') {
    const l = compileCondNode(node.left, catalog);
    const r = compileCondNode(node.right, catalog);
    const errors = [...l.errors, ...r.errors];
    if (errors.length) return { conditions: null, errors };
    return { conditions: { [node.type === 'and' ? 'all' : 'any']: [l.conditions, r.conditions] }, errors: [] };
  }
  if (node.type === 'not') {
    const inner = compileCondNode(node.term, catalog);
    if (inner.errors.length) return { conditions: null, errors: inner.errors };
    return { conditions: { not: inner.conditions }, errors: [] };
  }
  if (node.type === 'between') return compileBetween(node, catalog);
  if (node.type === 'leaf') return compileLeaf(node, catalog);
  return { conditions: null, errors: [_err('internal: unknown condition node type')] };
}

function compileCondition(node, catalog) {
  if (node.type === 'always') return { conditions: { ...ALWAYS_LEAF }, errors: [] };
  return compileCondNode(node, catalog);
}

function _compileGenericActionParams(call, entry) {
  const errors = [];
  const provided = _mapCallArgsToKeys(call, entry.paramKeys, _ALIAS_TO_REAL, errors, entry.token);
  if (errors.length) return { params: null, errors };
  for (const k of entry.paramKeys) {
    const spec = entry.paramsSchema[k];
    if (spec && spec.required && !provided.has(k)) errors.push(_err(`${entry.token}: missing required param '${k}'`));
  }
  if (errors.length) return { params: null, errors };
  const params = {};
  for (const [k, v] of provided) {
    const coerced = _coerceValueForSpec(v, entry.paramsSchema[k], k, entry.token);
    if (coerced.error) errors.push(coerced.error); else params[k] = coerced.value;
  }
  if (errors.length) return { params: null, errors };
  return { params, errors: [] };
}

/** Strip any `lots=` arg out of a call's arg list (CLI-only sugar — not a
 *  real schema key on `place_order`), returning `{ lotsArg, call }`. */
function _splitLotsArg(call) {
  const lotsArgs = (call.args || []).filter(a => a.key && a.key.toLowerCase() === 'lots');
  const otherArgs = (call.args || []).filter(a => !(a.key && a.key.toLowerCase() === 'lots'));
  return { lotsArgs, call: { ...call, args: otherArgs } };
}

/** Compile a `DO order(...)` / `DO place_order(...)` call to an agent
 *  action's `{"type":"place_order","params":{...}}` entry. `lots=N`
 *  converts to the REAL schema's `qty` param as CONTRACTS
 *  (`qty = lots * lot_size`) — this is place_order's own documented
 *  convention (order_fields.yaml: "qty: Number of lots × lot size"),
 *  DISTINCT from the ticket/basket convention below (where `quantity` is
 *  lots directly, no multiplication) — see `_compileOrderCallToLeg`. */
function _compileOrderActionParams(call, entry, opts) {
  const errors = [];
  const { lotsArgs, call: strippedCall } = _splitLotsArg(call);
  const provided = _mapCallArgsToKeys(strippedCall, entry.paramKeys, _ALIAS_TO_REAL, errors, entry.token);
  if (lotsArgs.length > 1) errors.push(_err(`${entry.token}: duplicate parameter 'lots'`));
  if (lotsArgs.length === 1 && provided.has('qty')) errors.push(_err(`${entry.token}: cannot specify both qty and lots`));
  if (errors.length) return { params: null, errors };

  for (const k of entry.paramKeys) {
    if (k === 'qty') continue;
    const spec = entry.paramsSchema[k];
    if (spec && spec.required && !provided.has(k)) errors.push(_err(`${entry.token}: missing required param '${k}'`));
  }
  if (!provided.has('qty') && lotsArgs.length === 0) {
    errors.push(_err(`${entry.token}: missing required param 'qty' (or lots=N for F&O)`));
  }
  if (errors.length) return { params: null, errors };

  const params = {};
  for (const [k, v] of provided) {
    if (k === 'qty') continue;
    const coerced = _coerceValueForSpec(v, entry.paramsSchema[k], k, entry.token);
    if (coerced.error) errors.push(coerced.error); else params[k] = coerced.value;
  }
  if (errors.length) return { params: null, errors };

  if (lotsArgs.length === 1) {
    const lotsVal = lotsArgs[0].value;
    if (lotsVal.type !== 'number' || !(lotsVal.value > 0)) {
      errors.push(_err(`${entry.token}: lots must be a positive number`));
      return { params: null, errors };
    }
    const symbol = params.symbol;
    if (!symbol) {
      errors.push(_err(`${entry.token}: lots requires symbol to be resolvable`));
      return { params: null, errors };
    }
    const lotSizeOf = (opts && opts.lotSizeOf) || _defaultLotSizeOf;
    const lotSize = lotSizeOf(symbol);
    if (!lotSize || !(lotSize > 0)) {
      errors.push(_err(`${entry.token}: could not resolve lot size for '${symbol}' — required to convert lots to qty`));
      return { params: null, errors };
    }
    params.qty = Math.round(lotsVal.value * lotSize);
  } else {
    const qtyVal = provided.get('qty');
    const coerced = _coerceValueForSpec(qtyVal, entry.paramsSchema.qty, 'qty', entry.token);
    if (coerced.error) { errors.push(coerced.error); return { params: null, errors }; }
    params.qty = coerced.value;
  }
  return { params, errors: [] };
}

function compileAlertClause(alertCalls, catalog) {
  if (alertCalls === 'nop') return { events: [], errors: [] };
  const errors = [];
  const events = [];
  for (const call of alertCalls) {
    const entry = catalog.channels.get(call.name.toLowerCase());
    if (!entry) {
      const other = _describeKind(call.name, catalog);
      errors.push(_err(other
        ? `'${call.name}' is ${other} — only channels are allowed in ALERT`
        : `unknown channel '${call.name}'`));
      continue;
    }
    const r = _compileGenericActionParams(call, entry);
    errors.push(...r.errors);
    if (!r.errors.length) events.push({ channel: entry.token, enabled: true, ...r.params });
  }
  return { events: errors.length ? null : events, errors };
}

function compileDoClause(doCalls, catalog, opts) {
  if (doCalls === 'nop') return { actions: [], errors: [] };
  const errors = [];
  const actions = [];
  for (const call of doCalls) {
    const rawName = call.name.toLowerCase();
    const lookupName = rawName === 'order' ? 'place_order' : rawName;
    const entry = catalog.actions.get(lookupName);
    if (!entry) {
      const other = _describeKind(call.name, catalog);
      errors.push(_err(other
        ? `'${call.name}' is ${other} — only actions are allowed in DO`
        : `unknown action '${call.name}'`));
      continue;
    }
    if (lookupName === 'place_order') {
      const r = _compileOrderActionParams(call, entry, opts);
      errors.push(...r.errors);
      if (!r.errors.length) actions.push({ type: 'place_order', params: r.params });
      continue;
    }
    const r = _compileGenericActionParams(call, entry);
    errors.push(...r.errors);
    if (!r.errors.length) actions.push({ type: entry.token, params: r.params });
  }
  return { actions: errors.length ? null : actions, errors };
}

function compileAgentStmt(ast, catalog, opts) {
  const errors = [];
  const condResult = compileCondition(ast.condition, catalog);
  errors.push(...condResult.errors);
  const eventsResult = compileAlertClause(ast.alertCalls, catalog);
  errors.push(...eventsResult.errors);
  const actionsResult = compileDoClause(ast.doCalls, catalog, opts);
  errors.push(...actionsResult.errors);
  if (ast.alertCalls === 'nop' && ast.doCalls === 'nop') {
    errors.push(_err('an agent must have at least one of ALERT or DO'));
  }
  if (errors.length) return { kind: 'agent', errors, agent: null };
  return {
    kind: 'agent', errors: [],
    agent: { conditions: condResult.conditions, events: eventsResult.events, actions: actionsResult.actions },
  };
}

/** Compile one bare `order(...)` call to a leg object using FIELD NAMES
 *  shared by both the ticket (`side`/`account`) and basket
 *  (`transaction_type`, no `account` — grouped at the BasketGroup level)
 *  destinations; `compileOrderStmt` adapts field names per destination.
 *  Ticket/basket convention: `quantity` IS LOTS for F&O (never multiplied
 *  by lot_size here) — `lot_size_hint` carries the resolved lot size for
 *  the backend's own cache. This is the OPPOSITE of the agent-action
 *  `qty` convention above (contracts) — see TicketOrderRequest's own
 *  docstring (schemas.py) for the "lots, not contracts" rule this exists
 *  to honor; multiplying here would reproduce the CRUDEOIL 100× oversize
 *  incident class. */
function _compileOrderCallToLeg(call, catalog, opts) {
  const errors = [];
  const lname = call.name.toLowerCase();
  if (lname !== 'order' && lname !== 'place_order') {
    errors.push(_err('only order(...) can be used standalone'));
    return { leg: null, errors };
  }
  const entry = catalog.actions.get('place_order');
  if (!entry) { errors.push(_err('place_order action not found in catalog')); return { leg: null, errors }; }

  const { lotsArgs, call: strippedCall } = _splitLotsArg(call);
  const provided = _mapCallArgsToKeys(strippedCall, entry.paramKeys, _ALIAS_TO_REAL, errors, 'order');
  if (lotsArgs.length > 1) errors.push(_err("order: duplicate parameter 'lots'"));
  if (lotsArgs.length === 1 && provided.has('qty')) errors.push(_err('order: cannot specify both qty and lots'));
  if (!provided.has('account')) errors.push(_err("order: missing required param 'account'"));
  if (!provided.has('symbol')) errors.push(_err("order: missing required param 'symbol'"));
  if (!provided.has('side')) errors.push(_err("order: missing required param 'side'"));
  if (!provided.has('qty') && lotsArgs.length === 0) errors.push(_err("order: missing required param 'qty' (or lots=N for F&O)"));
  if (errors.length) return { leg: null, errors };

  const coerce = (key, dflt) => {
    if (!provided.has(key)) return dflt;
    const c = _coerceValueForSpec(provided.get(key), entry.paramsSchema[key], key, 'order');
    if (c.error) { errors.push(c.error); return dflt; }
    return c.value;
  };
  const account = coerce('account', null);
  const symbol = coerce('symbol', null);
  const side = coerce('side', null);
  const exchange = coerce('exchange', 'NFO');
  const product = coerce('product', 'NRML');
  const order_type = coerce('order_type', 'LIMIT');
  const variety = coerce('variety', 'regular');
  const price = coerce('price', null);
  const trigger_price = coerce('trigger_price', null);
  const template_id = coerce('template_id', null);
  const tp_pct_override = coerce('tp_pct_override', null);
  const sl_pct_override = coerce('sl_pct_override', null);
  const wing_premium_pct_override = coerce('wing_premium_pct_override', null);
  const wing_strike_offset_override = coerce('wing_strike_offset_override', null);
  let chase = false;
  let chase_aggressiveness = 'low';
  if (provided.has('chase_level')) {
    const lvl = coerce('chase_level', null);
    if (lvl) { chase = true; chase_aggressiveness = String(lvl).toLowerCase(); }
  }
  if (errors.length) return { leg: null, errors };

  let quantity;
  let lot_size_hint = null;
  if (lotsArgs.length === 1) {
    const lotsVal = lotsArgs[0].value;
    if (lotsVal.type !== 'number' || !(lotsVal.value > 0)) {
      errors.push(_err('order: lots must be a positive number'));
      return { leg: null, errors };
    }
    const lotSizeOf = (opts && opts.lotSizeOf) || _defaultLotSizeOf;
    const lotSize = lotSizeOf(symbol);
    if (!lotSize || !(lotSize > 0)) {
      errors.push(_err(`order: could not resolve lot size for '${symbol}'`));
      return { leg: null, errors };
    }
    quantity = Math.round(lotsVal.value);
    lot_size_hint = Math.round(lotSize);
  } else {
    const c = _coerceValueForSpec(provided.get('qty'), entry.paramsSchema.qty, 'qty', 'order');
    if (c.error) { errors.push(c.error); return { leg: null, errors }; }
    quantity = c.value;
  }

  const leg = {
    account, side, tradingsymbol: symbol, quantity,
    exchange, product, order_type, variety, price, trigger_price,
    chase, chase_aggressiveness,
    template_id, tp_pct_override, sl_pct_override,
    wing_premium_pct_override, wing_strike_offset_override,
  };
  if (lot_size_hint != null) leg.lot_size_hint = lot_size_hint;
  return { leg, errors: [] };
}

/** Compile a bare `order_stmt` to a SINGLE TicketOrderRequest-shaped
 *  object (one call) or a BasketOrderRequest-shaped object (2+ calls,
 *  grouped by account) — the SAME shapes the existing Ticket UI / basket
 *  endpoint already accept (`frontend/src/lib/order/orderTicketSubmit.js`,
 *  `backend/api/schemas.py:TicketOrderRequest` / `BasketOrderRequest`). */
function compileOrderStmt(ast, catalog, opts = {}) {
  const errors = [];
  const legs = [];
  for (const call of ast.calls) {
    const r = _compileOrderCallToLeg(call, catalog, opts);
    errors.push(...r.errors);
    if (r.leg) legs.push(r.leg);
  }
  if (errors.length) return { kind: 'order', errors, ticket: null, basket: null };

  if (legs.length === 1) {
    if (!opts.mode) {
      return { kind: 'order', errors: [_err('mode ("paper"|"live") is required to compile a bare order statement')], ticket: null, basket: null };
    }
    return { kind: 'order', errors: [], ticket: { mode: opts.mode, ...legs[0] }, basket: null };
  }

  const order = [];
  const byAccount = new Map();
  for (const leg of legs) {
    if (!byAccount.has(leg.account)) { byAccount.set(leg.account, []); order.push(leg.account); }
    const { account, side, ...rest } = leg; // eslint-disable-line no-unused-vars
    byAccount.get(leg.account).push({ transaction_type: side, ...rest });
  }
  const groups = order.map(account => ({ account, legs: byAccount.get(account) }));
  return { kind: 'order', errors: [], ticket: null, basket: { groups } };
}

/** Compile an already-parsed AST (from `parseStatement`) to its JSON
 *  request shape. Returns `{ kind, errors, agent, ticket, basket }` —
 *  non-empty `errors` means every payload field is `null`. */
export function compileStatement(ast, catalog, opts = {}) {
  if (ast.kind === 'agent') {
    const r = compileAgentStmt(ast, catalog, opts);
    return { kind: 'agent', errors: r.errors, agent: r.agent, ticket: null, basket: null };
  }
  return compileOrderStmt(ast, catalog, opts);
}

/** One-shot parse + compile — the main entry point for Sprint 4's UI and
 *  for this sprint's tests. Never throws; always returns a structured
 *  result. `opts.mode` ("paper"|"live") is required for a single bare
 *  order_stmt; `opts.lotSizeOf(symbol) -> number|null` overrides the
 *  default instruments-cache lot-size lookup (used by tests). */
export function compileAgentCliStatement(text, catalog, opts = {}) {
  const { ast, errors: parseErrors } = parseStatement(text);
  if (parseErrors.length) {
    return { ok: false, errors: parseErrors, kind: null, agent: null, ticket: null, basket: null };
  }
  const result = compileStatement(ast, catalog, opts);
  if (result.errors && result.errors.length) {
    return { ok: false, errors: result.errors, kind: result.kind, agent: null, ticket: null, basket: null };
  }
  return {
    ok: true, errors: [], kind: result.kind,
    agent: result.agent || null, ticket: result.ticket || null, basket: result.basket || null,
  };
}

// ═══════════════════════════════════════════════════════════════════════
// (e) Semantic validator — thin wrapper for Sprint 4's live-preview, reuses
//     the EXACT SAME parse+compile path (no duplicated validation logic).
// ═══════════════════════════════════════════════════════════════════════

/** Validate a CLI statement WITHOUT needing the caller to consume the
 *  compiled payload — returns only `{ ok, errors }`. Structured errors
 *  (never throws), per the design doc's requirement that Sprint 4's UI
 *  consume a stable error shape rather than catching exceptions. */
export function validateAgentCliStatement(text, catalog, opts = {}) {
  const result = compileAgentCliStatement(text, catalog, opts);
  return { ok: result.ok, errors: result.errors };
}

// ═══════════════════════════════════════════════════════════════════════
// (f) Decompiler — reverse of (d) above. Given an agent's CURRENT
//     conditions/events/actions JSON (never the audit-only cli_source
//     column — see .claude/PLAN.md), regenerate equivalent CLI text.
//     Round-trip contract: compile(decompile(J)) must be a semantically
//     valid agent whose conditions/events/actions are reconstructible to
//     J for every shape the COMPILER itself can produce; a handful of
//     shapes that only a non-CLI author (JSON textarea / AI-draft / direct
//     admin edit) could produce have NO exact CLI form at all (documented
//     at each call site below) — those either normalize to an equivalent-
//     but-differently-shaped JSON on resave, or fail loud with a
//     structured error, per this sprint's explicit "never guess" mandate.
//
//     KEY-NAME CHOICE (documented per .claude/PLAN.md's explicit ask):
//     decompiled output always uses the REAL schema key names (account,
//     symbol, chase_level, ...), never the KWARG_ALIASES short forms
//     (acct, sym, chase, ...). Simpler and safer — the compiler accepts
//     real names everywhere aliases are accepted, so there is no
//     round-trip reason to prefer aliases, and using real names avoids a
//     second alias-resolution pass on the decompile side for no benefit.
// ═══════════════════════════════════════════════════════════════════════

const _CMP_LOW_OPS = new Set(['<', '<=']);
const _CMP_HIGH_OPS = new Set(['>', '>=']);
const _BARE_IDENT_RE = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** True iff `s` can be written bare (unquoted) per the grammar's own
 *  "enum values are bare" convention — a plain identifier, and not a
 *  reserved word (which would be a parse error as a bare value/name). */
function _isBareable(s) {
  return typeof s === 'string' && _BARE_IDENT_RE.test(s) && !_RESERVED.has(s.toLowerCase());
}

/** Render `n` as a CLI numeric literal. Returns `null` (never a lossy
 *  guess) when `n` isn't finite or would stringify with exponent
 *  notation (`1e-7`) — the lexer's NUMBER rule has no exponent support,
 *  so emitting one would produce unparseable text (advisor-flagged). */
function _renderNumber(n) {
  if (typeof n !== 'number' || !Number.isFinite(n)) return null;
  const s = String(n);
  if (/e/i.test(s)) return null;
  return s;
}

/** Quote a free-text string for CLI output. The lexer has no escape
 *  sequences, so a string containing BOTH quote characters has no safe
 *  single-token representation — fail loud rather than mangle it. */
function _quoteString(s) {
  if (s.includes('"') && s.includes("'")) {
    return { error: true, message: `cannot represent a value containing both " and ' as a CLI string literal` };
  }
  const q = s.includes('"') ? "'" : '"';
  return { text: `${q}${s}${q}` };
}

/** Render one list-literal item (leaf `in`/`not_in` RHS, or a generic
 *  param's array passthrough) — bare if it's a safe identifier/number,
 *  quoted otherwise. Mirrors `_compileListValue`'s acceptance of either
 *  spelling for a non-numeric metric's list items. */
function _renderListItem(v) {
  if (typeof v === 'number') {
    const t = _renderNumber(v);
    return t == null ? { error: true, message: 'list item cannot be rendered (exponent notation)' } : { text: t };
  }
  if (typeof v === 'boolean') return { text: v ? 'true' : 'false' };
  if (typeof v === 'string') return _isBareable(v) ? { text: v } : _quoteString(v);
  return { error: true, message: `list item of type ${typeof v} has no CLI representation` };
}

function _renderListLiteral(items) {
  const parts = [];
  for (const v of items) {
    const r = _renderListItem(v);
    if (r.error) return r;
    parts.push(r.text);
  }
  return { text: `[${parts.join(', ')}]` };
}

/** Render a single param value against its `params_schema` field spec —
 *  the decompile-direction mirror of `_coerceValueForSpec`. Enum values
 *  always render bare when possible (grammar's own documented
 *  convention) and fall back to quoted when the value isn't a safe bare
 *  identifier (e.g. `order_type=SL-M`) — `_coerceValueForSpec` accepts
 *  either spelling for `type:'enum'`, so quoting is always safe there.
 *  `type:'number'` fields that are `token_ref_ok` may legitimately hold
 *  an EXPRESSION STRING (e.g. `qty=base_lots * 2`, `price=ltp * 1.01`)
 *  instead of a literal — rendered quoted, exactly as it would have been
 *  typed, never coerced to a bare (unparseable) arithmetic expression. */
function _renderParamValue(value, spec) {
  const type = spec && spec.type;
  if (type === 'number') {
    if (typeof value === 'string') return _quoteString(value); // token_ref_ok expression
    if (typeof value === 'number') {
      const t = _renderNumber(value);
      return t == null ? { error: true, message: 'exponent-notation number cannot be rendered' } : { text: t };
    }
    return { error: true, message: `expected a number, got ${JSON.stringify(value)}` };
  }
  if (type === 'boolean') {
    if (typeof value === 'boolean') return { text: value ? 'true' : 'false' };
    return { error: true, message: 'expected true/false' };
  }
  if (type === 'enum') {
    if (typeof value !== 'string') return { error: true, message: 'expected an enum value' };
    return _isBareable(value) ? { text: value } : _quoteString(value);
  }
  if (type === 'string') {
    if (typeof value !== 'string') return { error: true, message: 'expected a free-text string' };
    return _quoteString(value);
  }
  // Unknown/absent schema type — best-effort passthrough, mirrors
  // `_coerceValueForSpec`'s own fallback branch.
  if (typeof value === 'number') {
    const t = _renderNumber(value);
    return t == null ? { error: true, message: 'exponent-notation number cannot be rendered' } : { text: t };
  }
  if (typeof value === 'boolean') return { text: value ? 'true' : 'false' };
  if (typeof value === 'string') return _isBareable(value) ? { text: value } : _quoteString(value);
  if (Array.isArray(value)) return _renderListLiteral(value);
  return { error: true, message: 'value has no CLI representation' };
}

/** Resolve + reverse a stored metric-call string (e.g. `"mean_pnl(30)"`,
 *  or a bare fixed-window token like `"mean_pnl_30m"`) back to its
 *  KEYWORD call-syntax CLI spelling, per `.claude/PLAN.md`'s explicit
 *  instruction — parses the string's positional value(s) and zips them
 *  against the resolved catalog entry's `paramKeys`, in the SAME order
 *  `_compileMetricCallString` used to build the string. A bare token
 *  (no parens) decompiles unchanged. Returns `{ entry, text }` or
 *  `{ error }` — never throws, never guesses on an arity/catalog
 *  mismatch (fail loud, per the sprint's "never guess" mandate). */
function _resolveMetricCall(metricStr, catalog) {
  if (typeof metricStr !== 'string' || !metricStr) {
    return { error: `invalid metric reference ${JSON.stringify(metricStr)}` };
  }
  const m = /^([A-Za-z_][A-Za-z0-9_]*)\(([^()]*)\)$/.exec(metricStr);
  const name = m ? m[1] : metricStr;
  const entry = catalog.metrics.get(name.toLowerCase());
  if (!entry) return { error: `unknown metric '${name}' referenced in stored condition` };
  if (!m) {
    if (entry.paramKeys.length !== 0) {
      return { error: `metric '${entry.token}' requires argument(s) (${entry.paramKeys.join(', ')}) but the stored call has none` };
    }
    return { entry, text: entry.token };
  }
  const rawArgs = m[2].trim() === '' ? [] : m[2].split(',').map(s => s.trim());
  if (rawArgs.length !== entry.paramKeys.length) {
    return { error: `metric '${entry.token}': expected ${entry.paramKeys.length} argument(s), stored call has ${rawArgs.length}` };
  }
  const pairs = [];
  for (let i = 0; i < rawArgs.length; i++) {
    const n = Number(rawArgs[i]);
    if (!Number.isFinite(n)) return { error: `metric '${entry.token}': stored argument '${rawArgs[i]}' is not numeric` };
    const t = _renderNumber(n);
    if (t == null) return { error: `metric '${entry.token}': stored argument '${rawArgs[i]}' cannot be rendered as a CLI numeric literal` };
    pairs.push(`${entry.paramKeys[i]}=${t}`);
  }
  return { entry, text: `${entry.token}(${pairs.join(', ')})` };
}

/** Resolve a stored scope string (e.g. `"positions.expiring_today.nfo"`)
 *  against the catalog. Scope tokens are always stored/typed as a single
 *  dotted string with no call arguments in the live catalog today, so
 *  this is a direct lookup, not a reconstruction. Named distinctly from
 *  the compiler's own `_resolveScopeString(scopeNode, ...)` above (which
 *  takes a parsed AST `ScopeNode`, not a stored string) to avoid a
 *  same-name collision in this shared module. */
function _decompileResolveScopeString(scopeStr, catalog) {
  if (typeof scopeStr !== 'string' || !scopeStr) return { error: `invalid scope reference ${JSON.stringify(scopeStr)}` };
  const entry = catalog.scopes.get(scopeStr.toLowerCase());
  if (!entry) return { error: `unknown scope '${scopeStr}' referenced in stored condition` };
  return { text: entry.token };
}

/** True iff `x` is a plain (non-composite, non-$ref) leaf shape — has
 *  `metric`/`scope`/`op` as strings and none of `all`/`any`/`not`/`$ref`.
 *  Used by the between-chain-reversal heuristic below to confirm BOTH
 *  `all` children are genuine leaves before attempting the collapse. */
function _isPlainLeaf(x) {
  return !!x && typeof x === 'object'
    && typeof x.metric === 'string' && typeof x.scope === 'string' && typeof x.op === 'string'
    && !('all' in x) && !('any' in x) && !('not' in x) && !('$ref' in x);
}

/** Attempt to reverse a STRICT/mixed-strictness `all:[leaf1, leaf2]` pair
 *  (produced by `compileBetween`'s non-inclusive branch — see that
 *  function's own comment) back to pretty chained-comparison syntax
 *  (`low <op1> metric@scope <op2> high`) — a NICE-TO-HAVE per
 *  `.claude/PLAN.md`, falls back to `null` (generic `&` rendering) on any
 *  ambiguity. Never throws, never mutates the caller's shared error list
 *  — a `null` return just means "render this generically instead", and
 *  the generic path re-resolves (and correctly reports) any real error.
 *
 *  Order-preserving reconstruction: `compileBetween` ALWAYS builds
 *  `all:[flip(op1)-leaf, op2-leaf]` in that exact order, so treating
 *  `all[0]` as "leaf1" (reverse its op via `_BETWEEN_FLIP`, which is its
 *  own inverse) and `all[1]` as "leaf2" (used as-is) exactly reconstructs
 *  `value1 op1 metric@scope op2 value2` for ANY valid opposite-bucket
 *  pairing — regardless of which direction (low-to-high or
 *  high-to-low) the original chain was typed in.
 *
 *  Advisor-flagged correctness guard: an INCLUSIVE same-op pair
 *  (`op1===op2==='<='` or both `'>='`) must NOT collapse here — the
 *  compiler represents that shape as the native `{op:'between',...}`
 *  leaf, never as `all:[...]`, so a hand-constructed `all` pair with that
 *  exact shape would, if collapsed, recompile to a DIFFERENT JSON
 *  structure (`between` instead of `all`) than what was stored. */
function _tryDecompileBetween(a, b, catalog) {
  if (!_isPlainLeaf(a) || !_isPlainLeaf(b)) return null;
  if (a.metric !== b.metric || a.scope !== b.scope) return null;
  if (typeof a.value !== 'number' || typeof b.value !== 'number') return null;
  const aLow = _CMP_LOW_OPS.has(a.op), aHigh = _CMP_HIGH_OPS.has(a.op);
  const bLow = _CMP_LOW_OPS.has(b.op), bHigh = _CMP_HIGH_OPS.has(b.op);
  const oppositeBuckets = (aLow && bHigh) || (aHigh && bLow);
  if (!oppositeBuckets) return null;
  const op1 = _BETWEEN_FLIP[a.op];
  const op2 = b.op;
  if ((op1 === '<=' && op2 === '<=') || (op1 === '>=' && op2 === '>=')) return null; // inclusive — would be native `between`, not `all`
  const m = _resolveMetricCall(a.metric, catalog);
  if (m.error) return null;
  const s = _decompileResolveScopeString(a.scope, catalog);
  if (s.error) return null;
  const v1 = _renderNumber(a.value);
  const v2 = _renderNumber(b.value);
  if (v1 == null || v2 == null) return null;
  return `${v1} ${op1} ${m.text}@${s.text} ${op2} ${v2}`;
}

/** Decompile a single leaf `{metric, scope, op, value}` — the mirror of
 *  `compileLeaf`. Handles exactly the shapes the compiler can emit
 *  (native `between`, `in`/`not_in`, boolean-shorthand, boolean
 *  explicit, plain numeric comparator) and fails loud on anything else. */
function decompileLeaf(node, catalog, errors) {
  const m = _resolveMetricCall(node.metric, catalog);
  if (m.error) { errors.push(_err(m.error)); return null; }
  const s = _decompileResolveScopeString(node.scope, catalog);
  if (s.error) { errors.push(_err(s.error)); return null; }
  const metricText = m.text, scopeText = s.text, entry = m.entry;

  if (node.op === 'between') {
    if (!Array.isArray(node.value) || node.value.length !== 2
        || typeof node.value[0] !== 'number' || typeof node.value[1] !== 'number') {
      errors.push(_err(`leaf '${node.metric}@${node.scope}': 'between' op requires a 2-number value array`));
      return null;
    }
    const low = _renderNumber(node.value[0]), high = _renderNumber(node.value[1]);
    if (low == null || high == null) { errors.push(_err(`leaf '${node.metric}@${node.scope}': between bound cannot be rendered`)); return null; }
    return `${low} <= ${metricText}@${scopeText} <= ${high}`;
  }

  if (node.op === 'in' || node.op === 'not_in') {
    if (!Array.isArray(node.value)) {
      errors.push(_err(`leaf '${node.metric}@${node.scope}': '${node.op}' requires a list value`));
      return null;
    }
    const r = _renderListLiteral(node.value);
    if (r.error) { errors.push(_err(`leaf '${node.metric}@${node.scope}': ${r.message}`)); return null; }
    return `${metricText}@${scopeText} ${node.op === 'not_in' ? 'not in' : 'in'} ${r.text}`;
  }

  const valueType = entry ? entry.valueType : null;
  if (valueType === 'boolean') {
    if (node.op === '==' && node.value === true) return `${metricText}@${scopeText}`; // boolean shorthand
    if ((node.op === '==' || node.op === '!=') && typeof node.value === 'boolean') {
      return `${metricText}@${scopeText} ${node.op} ${node.value}`;
    }
    errors.push(_err(`leaf '${node.metric}@${node.scope}': unrecognized boolean leaf shape (op=${node.op}, value=${JSON.stringify(node.value)})`));
    return null;
  }

  if (['<', '<=', '>', '>=', '==', '!='].includes(node.op) && typeof node.value === 'number') {
    const numText = _renderNumber(node.value);
    if (numText == null) { errors.push(_err(`leaf '${node.metric}@${node.scope}': value ${node.value} cannot be rendered as a CLI numeric literal`)); return null; }
    return `${metricText}@${scopeText} ${node.op} ${numText}`;
  }

  errors.push(_err(`leaf '${node.metric}@${node.scope}': unrecognized op/value combination (op=${node.op}, value=${JSON.stringify(node.value)})`));
  return null;
}

/** Recursively expand any child of an `all`/`any` array that is ITSELF
 *  the SAME op (e.g. a nested `all:[{all:[a,b]}, c]}`, which is exactly
 *  what `compileCondNode` builds for any chain of 3+ ANDed/ORed leaves —
 *  see its own left-leaning-binary-chain comment) into one flat sibling
 *  list. Recompiling a flattened `(a) & (b) & (c)` text reconstructs the
 *  SAME nested-pair tree via the parser's own left-to-right chain
 *  (`parseOrExpr`), so this flatten-then-rejoin is lossless for anything
 *  the compiler itself produces — see `decompileAllAny`'s own comment for
 *  the genuinely-flat-N-ary case (never compiler-produced) this does NOT
 *  make lossless. */
function _flattenCompositeChildren(opKey, arr) {
  const out = [];
  for (const child of arr) {
    if (child && typeof child === 'object' && Array.isArray(child[opKey]) && !('$ref' in child)) {
      out.push(..._flattenCompositeChildren(opKey, child[opKey]));
    } else {
      out.push(child);
    }
  }
  return out;
}

/** Decompile an `{all:[...]}` / `{any:[...]}` node. Flattens same-op
 *  nesting first (see `_flattenCompositeChildren`), tries the pretty
 *  between-chain reversal for an exactly-2-leaf `all`, then falls back to
 *  a flat `(t1) & (t2) & ... & (tN)` / `| ` join — each term wrapped
 *  individually (never a growing concatenated string re-wrapped at each
 *  recursion level), so paren nesting depth never exceeds the TRUE
 *  nesting depth of the stored tree, regardless of chain length
 *  (advisor-flagged: naive `(decompile(left)) & (decompile(right))`
 *  recursion compounds one extra paren pair per chain element and can
 *  blow the `_MAX_DEPTH` lexer guard on an ordinary long chain).
 *
 *  DOCUMENTED NORMALIZATION (advisor item 5 — a deliberate, consistent
 *  policy, not a bug): a flat array whose length is NOT exactly 2 (a
 *  single-element `all`/`any` — semantically just that one element, e.g.
 *  the REAL `expiry-day-equity-itm-auto-close` builtin agent's
 *  `{"all": [<one leaf>]}` — or a genuinely flat 3+-element array, which
 *  the compiler itself never produces but a hand/AI-authored JSON might)
 *  has NO distinct CLI form from the grammar's own left-associative
 *  chain — resaving normalizes it to whatever nested-pair (or bare,
 *  for N=1) shape the grammar's chain-builder naturally produces.
 *  Semantically identical, structurally re-shaped. */
function decompileAllAny(node, opKey, catalog, errors) {
  const rawArr = node[opKey];
  if (!Array.isArray(rawArr) || rawArr.length === 0) {
    errors.push(_err(`'${opKey}' must be a non-empty array`));
    return null;
  }
  const flat = _flattenCompositeChildren(opKey, rawArr);
  if (flat.length === 1) return decompileCondNode(flat[0], catalog, errors);
  if (flat.length === 2 && opKey === 'all') {
    const between = _tryDecompileBetween(flat[0], flat[1], catalog);
    if (between) return between;
  }
  const sym = opKey === 'all' ? '&' : '|';
  const parts = [];
  let bad = false;
  for (const c of flat) {
    const inner = decompileCondNode(c, catalog, errors);
    if (inner == null) { bad = true; continue; }
    parts.push(`(${inner})`);
  }
  if (bad) return null;
  return parts.join(` ${sym} `);
}

/** Recursive dispatcher — the mirror of `compileCondNode`. Dispatches on
 *  JSON SHAPE (not an AST node type — this operates directly on the
 *  stored `conditions` JSON): `all`/`any` composite, `not`, or a plain
 *  leaf (has `metric`+`scope` strings). Anything else — including a
 *  `$ref` fragment (checked here too, defense-in-depth, though
 *  `decompileCondition`'s own upfront whole-tree scan is the primary
 *  catch) — fails loud, never guesses. Does NOT special-case
 *  `ALWAYS_LEAF` here (that check lives ONLY in `decompileCondition`,
 *  the top-level entry point, below) — advisor-flagged: `always` is
 *  standalone-only in the grammar (a parse error if combined with
 *  `&`/`|`), so a NESTED occurrence of the exact sentinel shape (e.g.
 *  inside an `all`/`any` alongside another leaf) must render as an
 *  ordinary leaf, never as the bare word `always`. */
function decompileCondNode(node, catalog, errors) {
  if (node == null || typeof node !== 'object' || Array.isArray(node)) {
    errors.push(_err(`unrecognized condition shape: ${JSON.stringify(node)}`));
    return null;
  }
  if ('$ref' in node) {
    errors.push(_err('cannot decompile — this agent uses a $ref fragment; edit it via the JSON textarea instead'));
    return null;
  }
  if (Array.isArray(node.all)) return decompileAllAny(node, 'all', catalog, errors);
  if (Array.isArray(node.any)) return decompileAllAny(node, 'any', catalog, errors);
  if ('not' in node) {
    const inner = decompileCondNode(node.not, catalog, errors);
    if (inner == null) return null;
    return `~(${inner})`;
  }
  if (typeof node.metric === 'string' && typeof node.scope === 'string') {
    return decompileLeaf(node, catalog, errors);
  }
  errors.push(_err(`unrecognized condition shape: ${JSON.stringify(node)}`));
  return null;
}

/** True iff `node` is EXACTLY the frozen `ALWAYS_LEAF` sentinel —
 *  compared field-by-field (never `JSON.stringify` equality), since a
 *  real stored row can have its leaf's keys in a different order than
 *  `ALWAYS_LEAF`'s own declared order (confirmed against the real
 *  `market-open-nse`/`market-preclose-mcx` builtin agents in
 *  `agent_engine.py`, which write `{"op":..., "scope":..., "metric":...,
 *  "value":...}` — op-first, not `ALWAYS_LEAF`'s metric-first order).
 *  Deliberately does NOT match a leaf that merely shares the sentinel's
 *  numeric VALUE (-999999999) on a different metric/scope — e.g. the
 *  real `expiry-mcx-risk-alert` builtin uses that same magic number on
 *  `pnl@positions.expiring_today.mcx_unhedged`, which must decompile as
 *  an ordinary leaf, not as `always`. */
function _isAlwaysLeafExact(node) {
  return !!node && typeof node === 'object'
    && node.metric === ALWAYS_LEAF.metric
    && node.scope === ALWAYS_LEAF.scope
    && node.op === ALWAYS_LEAF.op
    && node.value === ALWAYS_LEAF.value
    && !('all' in node) && !('any' in node) && !('not' in node) && !('$ref' in node);
}

/** True iff a `$ref` fragment reference appears ANYWHERE in the
 *  conditions tree — the grammar has no CLI syntax for a template
 *  fragment reference at all (explicit non-goal per `.claude/PLAN.md`). */
function _containsRef(node) {
  if (node == null || typeof node !== 'object') return false;
  if ('$ref' in node) return true;
  if (Array.isArray(node.all)) return node.all.some(_containsRef);
  if (Array.isArray(node.any)) return node.any.some(_containsRef);
  if ('not' in node) return _containsRef(node.not);
  return false;
}

/** Decompile a stored `conditions` JSON tree back to `WHEN`-clause CLI
 *  text. Public entry point — checks the two conditions that can ONLY be
 *  evaluated at the whole-tree root (a `$ref` anywhere, or the `always`
 *  sentinel AT THE TOP, never nested — see `_isAlwaysLeafExact`'s own
 *  comment) before delegating to the recursive dispatcher. */
export function decompileCondition(conditions, catalog) {
  if (_containsRef(conditions)) {
    return { text: null, errors: [_err('cannot decompile — this agent uses a $ref fragment; edit it via the JSON textarea instead')] };
  }
  if (conditions == null || typeof conditions !== 'object') {
    return { text: null, errors: [_err(`unrecognized condition shape: ${JSON.stringify(conditions)}`)] };
  }
  if (_isAlwaysLeafExact(conditions)) return { text: 'always', errors: [] };
  const errors = [];
  const text = decompileCondNode(conditions, catalog, errors);
  if (errors.length || text == null) {
    return { text: null, errors: errors.length ? errors : [_err('could not decompile condition')] };
  }
  return { text, errors: [] };
}

/** Decompile a stored `events` array back to `ALERT`-clause CLI text.
 *  `[]` → `nop`. Every entry must resolve to a real catalog channel and
 *  carry `enabled: true` — a disabled (`enabled: false`) or structurally
 *  missing-`enabled` entry has NO CLI representation at all (the grammar
 *  has no syntax for "this channel exists but is turned off") and fails
 *  loud rather than silently dropping it from the regenerated text (which
 *  would silently change the agent's behavior on resave). An extra key
 *  not declared in that channel's `params_schema` also fails loud — it
 *  could never have been produced by the compiler (see
 *  `_mapCallArgsToKeys`'s symmetric "unknown parameter" rejection). */
export function decompileEvents(events, catalog) {
  if (!Array.isArray(events)) return { text: null, errors: [_err(`'events' must be an array, got ${JSON.stringify(events)}`)] };
  if (events.length === 0) return { text: 'nop', errors: [] };
  const errors = [];
  const parts = [];
  for (const ev of events) {
    if (!ev || typeof ev !== 'object' || typeof ev.channel !== 'string') {
      errors.push(_err(`unrecognized event entry: ${JSON.stringify(ev)}`));
      continue;
    }
    if (!('enabled' in ev) || ev.enabled !== true) {
      errors.push(_err(`event '${ev.channel}': a disabled or missing-'enabled' channel entry has no CLI representation`));
      continue;
    }
    const entry = catalog.channels.get(ev.channel.toLowerCase());
    if (!entry) { errors.push(_err(`unknown channel '${ev.channel}' referenced in stored events`)); continue; }
    const extraKeys = Object.keys(ev).filter(k => k !== 'channel' && k !== 'enabled');
    if (extraKeys.length === 0) { parts.push(entry.token); continue; }
    const argParts = [];
    let bad = false;
    for (const k of extraKeys) {
      const spec = entry.paramsSchema[k];
      if (!spec) { errors.push(_err(`event '${entry.token}': unknown parameter '${k}' (not declared in its catalog schema)`)); bad = true; continue; }
      const r = _renderParamValue(ev[k], spec);
      if (r.error) { errors.push(_err(`event '${entry.token}'.${k}: ${r.message}`)); bad = true; continue; }
      argParts.push(`${k}=${r.text}`);
    }
    if (!bad) parts.push(`${entry.token}(${argParts.join(', ')})`);
  }
  if (errors.length) return { text: null, errors };
  return { text: parts.join(', '), errors: [] };
}

/** Decompile one `{"type":"place_order","params":{...}}` action to
 *  `order(...)`. `params.qty` is CONTRACTS (the agent-action convention,
 *  distinct from the ticket/basket "lots directly" convention) —
 *  converted back to `lots=` for display WHEN that conversion is clean,
 *  reusing the SAME `lotSizeOf` resolver the compiler's own
 *  `_compileOrderActionParams` uses (`opts.lotSizeOf` or
 *  `_defaultLotSizeOf`).
 *
 *  PLAN-VS-CODE DISCREPANCY, RESOLVED (not just documented — see the
 *  Sprint 3 handback report for the original finding): `.claude/PLAN.md`
 *  said "lots= is the ONLY valid CLI spelling for order quantity," but
 *  `_compileOrderActionParams` (the Sprint 2 compiler, same file) also
 *  accepts a plain `qty=` for `DO order(...)`, coerced through the exact
 *  same `_coerceValueForSpec` path every other param uses — so a plain
 *  `qty=<value>` is ALWAYS renderable with zero possibility of failure
 *  (no lot-size lookup, no exact-multiple requirement — it's just the
 *  stored value, verbatim, same as any other param). Given that, failing
 *  loud whenever lots-conversion isn't clean was stricter than necessary:
 *  a real, compiler-accepted alternate spelling already existed for
 *  exactly that case. Resolution: try the nicer `lots=` form first (when
 *  it converts cleanly); fall back to plain `qty=` — via the SAME
 *  `_renderParamValue` every other param already uses, so a `token_ref_ok`
 *  expression string renders quoted automatically, no special-casing
 *  needed — for every other case. This only fails loud for genuinely
 *  malformed data (`qty` key absent, or neither a string nor a number),
 *  never for "can't cleanly convert to lots," since that's no longer a
 *  real failure — there's always a valid spelling. */
function _decompileOrderAction(params, entry, opts) {
  const keys = Object.keys(params || {});
  for (const k of keys) {
    if (k !== 'qty' && !(k in entry.paramsSchema)) {
      return { error: `order: unknown parameter '${k}' (not declared in catalog schema)` };
    }
  }
  if (!('qty' in (params || {}))) return { error: `order: missing 'qty' in stored params — malformed place_order action` };
  const qty = params.qty;
  if (typeof qty !== 'string' && (typeof qty !== 'number' || !Number.isFinite(qty))) {
    return { error: `order: qty is neither a number nor an expression string — malformed place_order action` };
  }

  // Try the nicer lots= form first — only when it converts cleanly.
  let qtyArgText = null;
  if (typeof qty === 'number') {
    const symbol = params.symbol;
    const lotSizeOf = (opts && opts.lotSizeOf) || _defaultLotSizeOf;
    const lotSize = typeof symbol === 'string' && symbol ? lotSizeOf(symbol) : null;
    if (lotSize && lotSize > 0 && qty % lotSize === 0) {
      const lotsText = _renderNumber(qty / lotSize);
      if (lotsText != null) qtyArgText = `lots=${lotsText}`;
    }
  }
  // Fall back to plain qty= — always possible, reuses the exact same
  // rendering every other param goes through (quotes a token_ref_ok
  // expression string automatically).
  if (qtyArgText == null) {
    const r = _renderParamValue(qty, entry.paramsSchema.qty);
    if (r.error) return { error: `order.qty: ${r.message}` };
    qtyArgText = `qty=${r.text}`;
  }

  const argParts = [];
  for (const k of entry.paramKeys) {
    if (k === 'qty') { argParts.push(qtyArgText); continue; }
    if (!(k in params)) continue;
    const r = _renderParamValue(params[k], entry.paramsSchema[k]);
    if (r.error) return { error: `order.${k}: ${r.message}` };
    argParts.push(`${k}=${r.text}`);
  }
  return { text: `order(${argParts.join(', ')})` };
}

/** Decompile one non-`place_order` action generically — the mirror of
 *  `_compileGenericActionParams`. Any OTHER action type renders as
 *  `action_type_name(key=value, ...)`, reusing the same quoting rules
 *  uniformly; zero-param actions (or an action with zero PROVIDED params)
 *  render bare. A param key not declared in the action's catalog schema
 *  fails loud — it could never have been compiler-produced. */
function _decompileGenericAction(params, entry) {
  const provided = params || {};
  const keys = Object.keys(provided);
  for (const k of keys) {
    if (!(k in entry.paramsSchema)) return { error: `action '${entry.token}': unknown parameter '${k}' (not declared in catalog schema)` };
  }
  if (keys.length === 0) return { text: entry.token };
  const argParts = [];
  for (const k of entry.paramKeys) {
    if (!(k in provided)) continue;
    const r = _renderParamValue(provided[k], entry.paramsSchema[k]);
    if (r.error) return { error: `action '${entry.token}'.${k}: ${r.message}` };
    argParts.push(`${k}=${r.text}`);
  }
  return { text: `${entry.token}(${argParts.join(', ')})` };
}

/** Decompile a stored `actions` array back to `DO`-clause CLI text.
 *  `[]` → `nop`. Every entry must carry a real `type` resolving against
 *  the catalog AND a `params` object — an entry missing `params`
 *  entirely (or `params: null`) has no CLI representation and fails
 *  loud, same rationale as the disabled-channel case in
 *  `decompileEvents`. `place_order` renders via the `order` CLI alias
 *  (never the literal `place_order` name — the opposite-direction
 *  mapping from how the compiler resolves `order`→`place_order`). */
export function decompileActions(actions, catalog, opts = {}) {
  if (!Array.isArray(actions)) return { text: null, errors: [_err(`'actions' must be an array, got ${JSON.stringify(actions)}`)] };
  if (actions.length === 0) return { text: 'nop', errors: [] };
  const errors = [];
  const parts = [];
  for (const act of actions) {
    if (!act || typeof act !== 'object' || typeof act.type !== 'string') {
      errors.push(_err(`unrecognized action entry: ${JSON.stringify(act)}`));
      continue;
    }
    if (!('params' in act) || act.params === null || typeof act.params !== 'object') {
      errors.push(_err(`action '${act.type}': missing or invalid 'params' object — has no CLI representation`));
      continue;
    }
    const lookupName = act.type.toLowerCase();
    const entry = catalog.actions.get(lookupName);
    if (!entry) { errors.push(_err(`unknown action '${act.type}' referenced in stored actions`)); continue; }
    if (lookupName === 'place_order') {
      const r = _decompileOrderAction(act.params, entry, opts);
      if (r.error) { errors.push(_err(r.error)); continue; }
      parts.push(r.text);
      continue;
    }
    const r = _decompileGenericAction(act.params, entry);
    if (r.error) { errors.push(_err(r.error)); continue; }
    parts.push(r.text);
  }
  if (errors.length) return { text: null, errors };
  return { text: parts.join(', '), errors: [] };
}

/** Decompile a full agent `{conditions, events, actions}` to its
 *  equivalent `WHEN ... ALERT ... DO ...` CLI text — the reverse of
 *  `compileAgentStmt`. This is what Sprint 4's Edit flow calls on the
 *  agent's LIVE JSON (never on `cli_source`, which is audit/history
 *  only — see `.claude/PLAN.md`). Aggregates errors from all three
 *  clauses (mirrors `compileAgentStmt`'s own aggregation) rather than
 *  stopping at the first failure, so an operator fixing a broken agent
 *  via the JSON textarea sees every problem at once.
 *
 *  Safety net (advisor item 6, optional-but-cheap): after building the
 *  text, actually recompile it and fail loud if that recompile itself
 *  errors — catches any shape this decompiler mishandles that wasn't
 *  explicitly enumerated above. Deliberately does NOT deep-compare the
 *  recompiled JSON against the ORIGINAL input — several shapes documented
 *  above (a single-element `all`/`any`, a flat 3+-element `all`/`any`)
 *  are INTENTIONAL normalizations that legitimately differ in structure
 *  from the original while remaining semantically equivalent; comparing
 *  against the original would incorrectly reject those. */
export function decompileAgent({ conditions, events, actions }, catalog, opts = {}) {
  const condResult = decompileCondition(conditions, catalog);
  const eventsResult = decompileEvents(events, catalog);
  const actionsResult = decompileActions(actions, catalog, opts);
  const errors = [...condResult.errors, ...eventsResult.errors, ...actionsResult.errors];
  const evLen = Array.isArray(events) ? events.length : null;
  const acLen = Array.isArray(actions) ? actions.length : null;
  if (evLen === 0 && acLen === 0) errors.push(_err('an agent must have at least one of ALERT or DO'));
  if (errors.length) return { text: null, errors };

  const text = `WHEN ${condResult.text} ALERT ${eventsResult.text} DO ${actionsResult.text}`;
  const roundTrip = compileAgentCliStatement(text, catalog, opts);
  if (!roundTrip.ok) {
    const detail = (roundTrip.errors && roundTrip.errors[0] && roundTrip.errors[0].message) || 'unknown error';
    return { text: null, errors: [_err(`internal: decompiled text failed to recompile (${detail})`)] };
  }
  return { text, errors: [] };
}
