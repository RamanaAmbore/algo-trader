// Agent CLI grammar — Sprint 2 (core parser/compiler, no UI yet).
//
// This file is NOT a hand-written per-token grammar like orders.js — it's a
// generic, SCHEMA-DRIVEN builder that resolves every metric/scope/channel/
// action token against the live backend catalog (`fetchGrammarTokens`), plus
// a hand-rolled recursive-descent parser for the full statement/condition
// grammar `engine.js` has no equivalent of, plus a compiler to the exact
// JSON shape the existing agent-create/update and ticket/basket endpoints
// already accept, plus an exhaustive semantic validator.
//
// Full EBNF + AST shape + known catalog gaps: docs/specs/AGENTS_GRAMMAR_EBNF.md
// (extracted 2026-10-25 from this exact comment block — keep both in sync when
// the grammar changes; that file is now the persistent source of truth, not
// the ephemeral .claude/PLAN.md this used to point at). As of extraction the
// EBNF itself was already stale relative to this file's real, current grammar
// (predates the GIVEN-removal/preamble/ALERT-is-an-ordinary-call unification)
// — see that file's own "KNOWN STALE" banner before trusting it.
import { fetchGrammarTokens } from '$lib/api';
import { getInstrument, listExchangesForSymbol } from '$lib/data/instruments';
import { tokenizeCallStyle, fuzzyFilter } from '$lib/command/engine.js';

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
// Sprint 20 (item 3) — 'and'/'or' added alongside the pre-existing 'not'
// as reserved words, now that all three are accepted as word aliases
// for '&'/'|'/'~' (see `_isAndTok`/`_isOrTok`/`_isNotTok`) — reserving
// them prevents `and(...)`/`or(...)` from ever being (mis)parsed as a
// real metric/channel/action token name.
// Sprint [grammar-unify] — 'given' and 'alert' are BOTH removed from the
// reserved set (confirmed, final — see the plan's "GIVEN is removed from
// the grammar completely" / "no ALERT keyword left anywhere"). `GIVEN` had
// nothing left to mark once the preamble became self-delimiting by
// position (bounded by WHEN, or end-of-input — see `parseAgentStmt`).
// `alert` is now an ORDINARY call (`alert(...)`), callable anywhere the
// shared statement vocabulary is accepted (preamble or DO) — it cannot be
// a reserved word and also be a valid call name (`parseCall` rejects any
// reserved-word call name outright). A bare, non-called `alert` token
// (no parens) is NOT thereby made meaningful again — `_dispatchStatement`
// still recognizes exactly two such shapes and nothing else: (1) the
// pre-existing, UNCHANGED standalone `event_stmt` (`ALERT log_tag=...` —
// see that function's own dispatch comment), and (2) a clear, specific
// rejection for the old removed clause-keyword spelling (`ALERT telegram
// DO ...`). 'debug'/'help' are DELIBERATELY NOT reserved — same
// "contextual, not a reserved word" treatment Sprint 6's `msg=` already
// uses (see the lexer's NAME-scanning branch below, which special-cases
// `debug(...)`/`help(...)`'s single bare argument without ever marking
// the token itself reserved) — this lets `parseCall` keep working
// unchanged for any real catalog token that might coincidentally share
// one of these names.
const _RESERVED = new Set(['when', 'do', 'nop', 'always', 'in', 'not', 'and', 'or', 'true', 'false']);
export const RESERVED_WORDS = _RESERVED;

// ── event_stmt reserved kwarg names (Sprint 12, +`gate` Sprint 18) — NOT
//    in `_RESERVED` (same "contextual, not a reserved word" treatment
//    Sprint 6's `msg=` already uses): recognized only by lookahead (a
//    bare NAME spelled one of these five, immediately followed by a lone
//    `EQ`), in `parseEventStmt`'s own event_meta loop AND (for the
//    "combined with WHEN/DO" rejection) `parseAlertClauseBody`. A real
//    channel/action token could in principle be named one of these —
//    none are, in the live catalog, and the grammar's own
//    contextual-lookahead style already accepts that same small risk for
//    `msg=`. `gate=` is the kind='event' agent's RENDER-ACTION-level gate
//    (repeat-dedup via the shared RepeatGate — see `catalog.renderAction`'s
//    own params_schema, sourced from backend's `event_actions.render.
//    params_schema`) — unrelated to a CHANNEL's own `gate=` kwarg (e.g.
//    `telegram(gate=false)`), which is an ordinary per-channel param
//    resolved generically through that channel's own `paramsSchema`, not
//    through this reserved-kwarg set at all. `match=` (Sprint 18
//    follow-up) is a NEW sixth key — a list-of-objects alternative to
//    the `log_tag=`/`min_level=`/`where=` triple, mutually exclusive
//    with all three (never combined — see `compileEventStmt`'s own
//    guard). ─────────────────────────────────────────────────────────
const _EVENT_META_KEYS = new Set(['log_tag', 'min_level', 'render', 'where', 'gate', 'match']);

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
  trigger_price: 'trigpx',
  product: 'prod',
  variety: 'var',
  chase_level: 'chase',
});
const _ALIAS_TO_REAL = Object.fromEntries(
  Object.entries(KWARG_ALIASES).map(([real, alias]) => [alias.toLowerCase(), real])
);

// ── Short scope-family alias table (Sprint 19) — SAME exact pattern as
//    KWARG_ALIASES above: presentation-only, input-side CLI typing
//    convenience, applied ONLY to a scope chain's FIRST segment (the
//    family root — `positions`/`holdings`/`funds`/`watchlist`; deeper
//    segments like `expiring_today`/`mcx_unhedged` have no abbreviated
//    form). Never written to the output JSON — the compiler always
//    resolves to the real catalog scope token, and the decompiler always
//    renders the canonical long form, never the alias. ───────────────────
export const SCOPE_FAMILY_ALIASES = Object.freeze({
  positions: 'pos',
  holdings: 'hld',
  funds: 'fnd',
  watchlist: 'wl',
});
// Sprint G (Task 3) — `hold` as an ADDITIONAL accepted alias for
// `holdings`, alongside the already-shipped `hld` above (never removed —
// aliases are pure input-side sugar with zero migration cost either way).
// Kept as a SEPARATE table (not folded into `SCOPE_FAMILY_ALIASES` itself,
// which maps each real family to exactly ONE canonical short alias for
// display/documentation purposes) so `SCOPE_FAMILY_ALIASES.holdings`
// keeps returning `'hld'` unchanged for any existing reader — only the
// resolver's reverse lookup (`_SCOPE_ALIAS_TO_REAL`, below) gains the
// extra entry.
const _EXTRA_SCOPE_FAMILY_ALIASES = Object.freeze({ hold: 'holdings' });
// Sprint G (Task 3) — `funds.cash`/`funds.margin` dot-access aliases
// (bare column name -> real metric token), consulted by `_resolveDotAccess`
// only when the chain's own prefix is exactly `funds` (2 segments total,
// e.g. `funds.cash`) — see that function's own comment for why this needs
// a dedicated check rather than falling out of the generic scope-prefix
// resolution.
const _FUNDS_COLUMN_ALIASES = Object.freeze({ cash: 'cash', margin: 'avail_margin' });
const _SCOPE_ALIAS_TO_REAL = {
  ...Object.fromEntries(Object.entries(SCOPE_FAMILY_ALIASES).map(([real, alias]) => [alias.toLowerCase(), real])),
  ..._EXTRA_SCOPE_FAMILY_ALIASES,
};

/** Resolve a scope chain's FIRST segment name through `SCOPE_FAMILY_ALIASES`
 *  (case-insensitive) to its canonical family name — a no-op for a name
 *  that isn't a registered alias (including an already-canonical name). */
function _scopeFamilyAliasOf(name) {
  const lower = String(name).toLowerCase();
  return _SCOPE_ALIAS_TO_REAL[lower] || lower;
}

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
  // Sprint 15 — `token_kind: 'column'` rows, the per-scope exposed-column
  // registry projection (`backend.api.algo.grammar.EXPOSED_COLUMNS`, merged
  // into `list_tokens(grammar='condition')` by
  // `routes/grammar.py:_merge_exposed_column_rows`). Keyed by the row's own
  // COMPOUND token string, lowercased (e.g. `"positions.total.pnl"`) — same
  // real-casing-preserving-on-`.token`/lowercased-key convention every other
  // bucket here already follows.
  const columns = new Map();
  // Sprint 18 — the single synthetic `token_kind: 'render_action'` row
  // (backend's `routes/grammar.py:_render_action_catalog_rows`, gated to
  // `grammar == 'action'`, so it rides the SAME fetchGrammarTokens
  // ('action') call below with no new network call). Declares the
  // kind='event' agent render action's own optional sibling params
  // (currently just `gate`) — deliberately kept OUT of `actions` (the
  // DO-dispatchable `action_type` bucket): `token_kind` is
  // `'render_action'`, never `'action_type'`, so a cycle-agent's DO list
  // can never be tricked into accepting a bare `render` action it has no
  // handler for. A single entry (not a Map) since there is exactly one
  // row; `null` when the backend hasn't shipped it yet (older catalog,
  // or the YAML section is empty) — every consumer must treat that as
  // "no declared schema", matching the real pre-Sprint-18 behavior.
  let renderAction = null;
  for (const row of rows || []) {
    if (!row || row.is_active === false) continue;
    const entry = _buildCatalogEntry(row);
    const key = String(row.token).toLowerCase();
    if (row.grammar_kind === 'condition' && row.token_kind === 'metric') metrics.set(key, entry);
    else if (row.grammar_kind === 'condition' && row.token_kind === 'scope') scopes.set(key, entry);
    else if (row.grammar_kind === 'condition' && row.token_kind === 'column') columns.set(key, entry);
    else if (row.grammar_kind === 'notify' && row.token_kind === 'channel') channels.set(key, entry);
    else if (row.grammar_kind === 'action' && row.token_kind === 'action_type') actions.set(key, entry);
    else if (row.grammar_kind === 'action' && row.token_kind === 'render_action') renderAction = entry;
  }
  return { metrics, scopes, channels, actions, columns, renderAction };
}

function _buildCatalogEntry(row) {
  const schema = row.params_schema || {};
  return {
    token: row.token,              // canonical stored casing
    valueType: row.value_type || null,
    paramsSchema: schema,
    paramKeys: Object.keys(schema), // declared order — keys[0] is positional-or-keyword
    // Sprint 11 — the token's own catalog description (e.g. the `telegram`
    // channel row's "Telegram group defined by secrets.telegram_chat_id.").
    // Each PARAM's own description lives inside `schema[key].description`
    // already — untouched here, just surfaced downstream by
    // `suggestAgentCliAt`'s param-name branch.
    description: row.description || '',
    // Sprint 10 — true only for a merged-in kind='template' Agent
    // ("Token"/"Template") row (backend's GrammarTokenOut.is_fragment_ref)
    // — see the file-header
    // EBNF's `leaf`/`call` production notes for how this flag is consumed.
    isFragmentRef: row.is_fragment_ref === true,
    // Sprint 17 — token-level (sibling of params_schema, NOT nested inside
    // it — see backend/api/schemas.py:GrammarTokenOut) declared
    // mutual-exclusion groups, e.g. [["wing_premium_pct_override",
    // "wing_strike_offset_override"]]. `null` when the token declares
    // none (every condition/notify/non-mutex action row) — never `[]`,
    // so callers can use a plain truthy check.
    mutuallyExclusiveGroups: Array.isArray(row.mutually_exclusive_groups) ? row.mutually_exclusive_groups : null,
    // Sprint 15 — operator-facing SHORT display text (e.g. `"P&L (₹)"`),
    // populated today only for `token_kind:'column'` rows (backend's
    // `GrammarTokenOut.label`). `null` for every other token kind — this
    // is what autocomplete shows prominently for a column suggestion,
    // never the long compound `token` string (see `_suggestBareDottedRef`).
    label: row.label || null,
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

// Sprint 20 (item 2) — canonical F&O-vs-equity exchange split, per the
// backend's own documented convention (`backend/api/schemas.py`:
// "F&O exchanges (NFO/MCX/CDS/BFO/BCD/NCO): quantity is LOTS... Equity
// exchanges (NSE/BSE): quantity is raw shares.") — the SAME set drives
// both item 2's exchange/product defaulting AND item 1's qty-as-contracts
// conversion below, since both need the identical F&O/equity split.
const _FO_EXCHANGES = new Set(['NFO', 'MCX', 'CDS', 'BFO', 'BCD', 'NCO']);

/** Default exchange resolver — mirrors `_defaultLotSizeOf`'s pattern,
 *  reusing the SAME instruments-cache index (`listExchangesForSymbol`,
 *  the multi-listing-aware sibling of `getInstrument`) rather than a
 *  second lookup mechanism. Returns the raw array (never a single
 *  string) — callers decide what "exactly one" / "zero" / "2+" means. */
function _defaultExchangesOf(symbol) {
  try { return listExchangesForSymbol(symbol) || []; } catch { return []; }
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

/** Sprint 20 (item 5) — pure, UI-agnostic position-describer: given the
 *  full editor text and a character offset (`error.position`, as every
 *  `_err()`/`_ParseError` above now threads for an ever-growing share of
 *  this grammar's compile/parse errors), returns 1-based `line`/`column`
 *  plus a bounded ~`2*windowSize`-char `snippet` window around the
 *  offset with its own `caretOffset` (the offset's position WITHIN
 *  `snippet`, for a caller to render a `^` marker under it) —
 *  `AgentCliEditor.svelte` renders this; never throws, returns `null`
 *  for an out-of-range/missing position (never a garbled partial result). */
export function describeErrorPosition(text, position, windowSize = 30) {
  if (typeof text !== 'string' || typeof position !== 'number' || !Number.isFinite(position)) return null;
  if (position < 0 || position > text.length) return null;
  let line = 1;
  let lineStart = 0;
  for (let i = 0; i < position; i++) {
    if (text[i] === '\n') { line++; lineStart = i + 1; }
  }
  const column = position - lineStart + 1;
  const start = Math.max(0, position - windowSize);
  const end = Math.min(text.length, position + windowSize);
  return {
    line, column,
    snippet: text.slice(start, end),
    caretOffset: position - start,
    truncatedStart: start > 0,
    truncatedEnd: end < text.length,
  };
}

// ═══════════════════════════════════════════════════════════════════════
// (b) Lexer
// ═══════════════════════════════════════════════════════════════════════

const _PUNCT_SINGLE = {
  '(': 'LPAREN', ')': 'RPAREN', '[': 'LBRACKET', ']': 'RBRACKET',
  ',': 'COMMA', '@': 'AT', '.': 'DOT', '&': 'AMP', '|': 'PIPE', '~': 'TILDE',
  // Sprint 12 — `{`/`}` are lexed at the TOP level too (previously only
  // ever produced by the SEPARATE tp_scales_json mini-lexer, `_lexObjScalesTok`
  // below). Only `event_stmt`'s `where=` production actually consumes these
  // tokens — no pre-Sprint-12 production in this grammar expected a bare
  // `{`/`}` at the top level (it was a hard lex error before), so this is a
  // no-op for every existing statement shape.
  '{': 'LBRACE', '}': 'RBRACE',
  // Sprint 19 — `:` binds a qualifier key to its value inside a scope_ref
  // segment (`account:ZG0790`, `symbol:*`, `account:worst`); `*` is the
  // qualifier's own "any value" wildcard literal. Neither character was
  // used anywhere else in this grammar before Sprint 19, so this is a
  // no-op for every pre-Sprint-19 statement.
  ':': 'COLON', '*': 'STAR',
  // Sprint G — `;` separates items inside GIVEN/ALERT/DO's own item list
  // (`label = value; call, call; label = value; ...`) — SEPARATE from
  // `,`, which still only ever joins calls WITHIN one call_list run (see
  // `parseItemListClause`'s own header comment for the full grammar).
  ';': 'SEMI',
  // Sprint [GIVEN-expr] — `+`/`/`/`%` for GIVEN's new arithmetic/string-
  // concat `given_rhs` alternative (see `parseArithExpr`). No ambiguity
  // with any existing production — none of these three characters were
  // used anywhere else in this grammar before. `-` (binary MINUS) and `*`
  // doubled into `**` (STARSTAR) are each special-cased in the lexer body
  // below instead of listed here — see the comments at those branches for
  // why (ordering interactions with the existing negative-NUMBER literal
  // rule and the existing single-STAR qualifier-wildcard token).
  '+': 'PLUS', '/': 'SLASH', '%': 'PERCENT',
};

function _isWordChar(ch) { return /[A-Za-z0-9_]/.test(ch); }
function _isWordStart(ch) { return /[A-Za-z_]/.test(ch); }
function _isDigit(ch) { return /[0-9]/.test(ch); }

/** Tokenize a value-ish fragment (from the inner-arg tokenizeCallStyle
 *  stream) into a ValueNode, tagged with its own `start`/`end` position
 *  (Sprint 20, item 5 — threads a real compile-error position through;
 *  `start`/`end` default to the source token's own when omitted by the
 *  caller, so every existing call site keeps working unchanged). */
function _valueNodeFromCallStyleToken(tok, start, end) {
  const pos = { start: start != null ? start : tok.start, end: end != null ? end : tok.end };
  if (tok.quoted) return { type: 'string', value: tok.text, ...pos };
  const text = tok.text;
  if (/^-?\d+(\.\d+)?$/.test(text)) return { type: 'number', value: Number(text), ...pos };
  return { type: 'name', value: text, ...pos };
}

/** Parse a single comma-separated arg segment (array of tokenizeCallStyle
 *  tokens) into one ArgEntry, handling every spacing variant around `=`
 *  (fused `key=value`, fused `key="quoted value"`, spaced `key = value`,
 *  `key =value`, `key= value`). Throws _ParseError on malformed segments.
 *
 *  Sprint 20 (items 4/5) — every returned ArgEntry now also carries
 *  `keyStart` (the position of the TYPED key itself, for "unknown
 *  parameter"/"duplicate parameter" errors) and every `value` ValueNode
 *  carries its own `start`/`end` (for per-value coercion errors) — see
 *  `_mapCallArgsToKeys`/`_coerceValueForSpec` for the consumer side. */
function _parseArgSegment(segTokens, labelToken) {
  if (segTokens.length === 0) throw new _ParseError('empty argument (trailing/leading/double comma)', labelToken);
  const first = segTokens[0];
  if (first.kwarg) {
    if (first.kwarg.value === '' && segTokens.length > 1) {
      // `key= value` — value is the glued-nothing kwarg continued by the
      // next token.
      if (segTokens.length > 2) throw new _ParseError('unexpected token in argument', segTokens[2]);
      return { key: first.kwarg.key, keyStart: first.start, value: _valueNodeFromCallStyleToken(segTokens[1]) };
    }
    if (segTokens.length > 1) throw new _ParseError('unexpected token in argument', segTokens[1]);
    const value = first.kwarg.quoted
      ? { type: 'string', value: first.kwarg.value, start: first.start, end: first.end }
      : _valueNodeFromCallStyleToken({ text: first.kwarg.value }, first.start, first.end);
    return { key: first.kwarg.key, keyStart: first.start, value };
  }
  if (!first.punct && !first.quoted && segTokens.length > 1) {
    const second = segTokens[1];
    if (!second.punct && !second.quoted && !second.kwarg && second.text === '=') {
      // spaced `key = value`
      if (segTokens.length < 3) throw new _ParseError('expected a value after "="', second);
      if (segTokens.length > 3) throw new _ParseError('unexpected token in argument', segTokens[3]);
      return { key: first.text, keyStart: first.start, value: _valueNodeFromCallStyleToken(segTokens[2]) };
    }
    if (!second.punct && !second.quoted && !second.kwarg && second.text.startsWith('=') && second.text.length > 1) {
      // `key =value` — second token is glued "=value"
      if (segTokens.length > 2) throw new _ParseError('unexpected token in argument', segTokens[2]);
      const value = _valueNodeFromCallStyleToken({ text: second.text.slice(1) }, second.start + 1, second.end);
      return { key: first.text, keyStart: first.start, value };
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

// ── object_literal production (Sprint 8, GENERALIZED Sprint 12) ────────
// Sprint 8 introduced `object_literal := "{" obj_kwarg ("," obj_kwarg)* "}"`
// STRICTLY scoped to one call-argument value shape (`tp_scales_json=[...]`),
// via a dedicated, self-contained mini lexer+parser (`_lexObjScalesTok` +
// `parseObjectLiteral` below) operating on a RAW bracket-span substring
// extracted from inside a call's `(...)` text — NOT routed through
// `tokenizeCallStyle` (no `[`/`]`/`{`/`}` awareness at all) or the main
// `lexAgentStatement` token stream.
//
// Sprint 12 GENERALIZES `parseObjectLiteral` (renamed from the original
// `_parseObjectLiteral`, error wording genericized) into a reusable
// production any call-site can invoke against ANY token array sharing this
// file's `{type, value, start, end}` token shape — not just
// `_lexObjScalesTok`'s mini stream. `event_stmt`'s `where=` (Sprint 12) is
// the SECOND call-site: once `{`/`}` are lexed at the TOP level too (see
// `_PUNCT_SINGLE` above), the MAIN `lexAgentStatement` token stream already
// produces matching LBRACE/RBRACE/COMMA/EQ/NUMBER/STRING/NAME/EOF tokens,
// so `parseObjectLiteral(state)` runs directly against the main parser's
// `state` with ZERO changes — the exact same function, not a second
// copy-pasted parser. Still produces a `{type:'list', value:
// ObjectValueNode[]}` ValueNode for the `tp_scales_json_value` call-site
// (list of objects) or a single `{type:'object', value}` ValueNode for the
// `where=` call-site (one bare object, no enclosing list) — the SAME
// `parseListLiteral`/`parseValue` ValueNode vocabulary used elsewhere in
// this file either way, so every downstream consumer (coercion, decompile)
// only ever sees one list/object vocabulary.

function _lexObjScalesTok(text, baseStart) {
  const tokens = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    while (i < n && /\s/.test(text[i])) i++;
    if (i >= n) break;
    const start = i;
    const ch = text[i];
    if (ch === '"' || ch === "'") {
      const q = ch;
      i++;
      while (i < n && text[i] !== q) i++;
      if (i >= n) throw new _ParseError('unterminated string literal in tp_scales_json value', { start: baseStart + start });
      i++;
      tokens.push({ type: 'STRING', value: text.slice(start + 1, i - 1), start: baseStart + start, end: baseStart + i });
      continue;
    }
    if (ch === '{') { tokens.push({ type: 'LBRACE', start: baseStart + start, end: baseStart + i + 1 }); i++; continue; }
    if (ch === '}') { tokens.push({ type: 'RBRACE', start: baseStart + start, end: baseStart + i + 1 }); i++; continue; }
    if (ch === '[') { tokens.push({ type: 'LBRACKET', start: baseStart + start, end: baseStart + i + 1 }); i++; continue; }
    if (ch === ']') { tokens.push({ type: 'RBRACKET', start: baseStart + start, end: baseStart + i + 1 }); i++; continue; }
    if (ch === ',') { tokens.push({ type: 'COMMA', start: baseStart + start, end: baseStart + i + 1 }); i++; continue; }
    if (ch === '=') { tokens.push({ type: 'EQ', start: baseStart + start, end: baseStart + i + 1 }); i++; continue; }
    if (_isDigit(ch) || (ch === '-' && _isDigit(text[i + 1] || ''))) {
      let j = i + (ch === '-' ? 1 : 0);
      while (j < n && _isDigit(text[j])) j++;
      if (text[j] === '.' && _isDigit(text[j + 1] || '')) { j++; while (j < n && _isDigit(text[j])) j++; }
      tokens.push({ type: 'NUMBER', value: Number(text.slice(start, j)), start: baseStart + start, end: baseStart + j });
      i = j;
      continue;
    }
    if (_isWordStart(ch)) {
      let j = i;
      while (j < n && _isWordChar(text[j])) j++;
      tokens.push({ type: 'NAME', value: text.slice(start, j), start: baseStart + start, end: baseStart + j });
      i = j;
      continue;
    }
    throw new _ParseError(`unexpected character '${ch}' in tp_scales_json value`, { start: baseStart + start });
  }
  tokens.push({ type: 'EOF', start: baseStart + n, end: baseStart + n });
  return tokens;
}

/** `value` production, reused for an `obj_kwarg`'s RHS — NUMBER/STRING/
 *  NAME, or (Sprint 18 follow-up, `match=[...]`'s per-entry `where={...}`)
 *  a NESTED object_literal via a direct recursive call to
 *  `parseObjectLiteral` below. A nested list_literal remains intentionally
 *  NOT supported — out of scope, no realistic object_literal call-site
 *  needs it. GENERIC (Sprint 12) — no tp_scales_json-specific wording,
 *  since `where=` (event_stmt) shares this exact function unchanged; the
 *  LBRACE branch works against BOTH the mini `_lexObjScalesTok` stream
 *  (which already lexes `{`/`}`, see that function) and the main
 *  `lexAgentStatement` stream, same as every other branch here. */
function _objLiteralParseValue(state) {
  const tok = state.tokens[state.pos];
  if (tok.type === 'NUMBER') { state.pos++; return { type: 'number', value: tok.value }; }
  if (tok.type === 'STRING') { state.pos++; return { type: 'string', value: tok.value }; }
  if (tok.type === 'NAME') { state.pos++; return { type: 'name', value: tok.value }; }
  if (tok.type === 'LBRACE') return parseObjectLiteral(state);
  throw new _ParseError('expected a value (number, string, bare name, or {...}) inside {...}', tok);
}

/** `object_literal := "{" obj_kwarg ("," obj_kwarg)* "}"` — GENERALIZED
 *  (Sprint 12, see the section-header comment above) off its original
 *  Sprint 8 tp_scales_json-only scoping. Exported so a future call-site
 *  outside this file's own tp_scales_json/where= uses could reuse it too;
 *  operates on ANY `{tokens, pos}` state sharing this file's token shape —
 *  the mini `_lexObjScalesTok` stream (tp_scales_json) or the main
 *  `lexAgentStatement` stream (event_stmt's `where=`), interchangeably. */
export function parseObjectLiteral(state) {
  const open = state.tokens[state.pos];
  if (open.type !== 'LBRACE') throw new _ParseError('expected "{" (object literal entries are {key=value, ...})', open);
  state.pos++;
  const fields = {};
  if (state.tokens[state.pos].type !== 'RBRACE') {
    while (true) {
      const keyTok = state.tokens[state.pos];
      if (keyTok.type !== 'NAME') throw new _ParseError('expected a key name inside {...}', keyTok);
      state.pos++;
      const eq = state.tokens[state.pos];
      if (eq.type !== 'EQ') throw new _ParseError(`expected "=" after key '${keyTok.value}' (object literal fields use "=", not ":")`, eq);
      state.pos++;
      const value = _objLiteralParseValue(state);
      if (Object.prototype.hasOwnProperty.call(fields, keyTok.value)) {
        throw new _ParseError(`duplicate key '${keyTok.value}' in object literal`, keyTok);
      }
      fields[keyTok.value] = value;
      if (state.tokens[state.pos].type === 'COMMA') { state.pos++; continue; }
      break;
    }
  }
  const close = state.tokens[state.pos];
  if (close.type !== 'RBRACE') throw new _ParseError('expected "}"', close);
  state.pos++;
  return { type: 'object', value: fields };
}

/** `tp_scales_json_value := "[" object_literal ("," object_literal)* "]"` —
 *  top-level entry point. `rawBracketText` is the FULL `[...]` span
 *  (including both brackets); `baseStart` is its absolute offset (best-
 *  effort, same relative-to-innerText convention every other arg-position
 *  error in this file already uses) for error reporting. */
function _parseTpScalesJsonArg(rawBracketText, baseStart) {
  const tokens = _lexObjScalesTok(rawBracketText, baseStart);
  const state = { tokens, pos: 0 };
  const open = state.tokens[state.pos];
  if (open.type !== 'LBRACKET') {
    throw new _ParseError('tp_scales_json requires a list of {...} entries, e.g. [{at_pct=30, close_pct=50}]', open);
  }
  state.pos++;
  const items = [];
  if (state.tokens[state.pos].type !== 'RBRACKET') {
    items.push(parseObjectLiteral(state));
    while (state.tokens[state.pos].type === 'COMMA') {
      state.pos++;
      items.push(parseObjectLiteral(state));
    }
  }
  const close = state.tokens[state.pos];
  if (close.type !== 'RBRACKET') throw new _ParseError('expected "]"', close);
  state.pos++;
  const trailing = state.tokens[state.pos];
  if (trailing.type !== 'EOF') throw new _ParseError('unexpected trailing input after tp_scales_json value', trailing);
  return { type: 'list', value: items };
}

/** Recursively shift every numeric `start`/`end`/`keyStart` field found
 *  anywhere in a plain token/AST-node tree by `baseStart` — used by
 *  `_parseNotifyListArg` to re-anchor positions produced by a STANDALONE
 *  `lexAgentStatement(...)` call (which always starts counting from 0)
 *  onto the ORIGINAL statement's own absolute offsets, matching the
 *  convention every other raw-segment arg parser in this file follows
 *  (`_lexObjScalesTok`'s `baseStart` param, `_parseArgSegment`'s
 *  `base`-shifted tokens). Pure/non-mutating; safe on any JSON-shaped
 *  node (CallNode/ArgEntry/ValueNode all qualify — see the file-header
 *  AST-shape comment). */
function _shiftPositions(node, baseStart) {
  if (Array.isArray(node)) return node.map((n) => _shiftPositions(n, baseStart));
  if (node && typeof node === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(node)) {
      if ((k === 'start' || k === 'end' || k === 'keyStart') && typeof v === 'number') out[k] = v + baseStart;
      else out[k] = _shiftPositions(v, baseStart);
    }
    return out;
  }
  return node;
}

/** `notify_list_value := "[" call ("," call)* "]"` — a LIST OF CALLS
 *  (channel names, each optionally carrying its own `(...)` params, e.g.
 *  `[telegram, ntfy(priority="urgent")]`), structurally distinct from
 *  `tp_scales_json`'s list of `{...}` object literals
 *  (`_parseTpScalesJsonArg` above) — reuses the MAIN `lexAgentStatement`
 *  lexer directly (rather than a second hand-rolled mini-lexer like
 *  `_lexObjScalesTok`) since that lexer already fully tokenizes
 *  NAME/NAME(args)/LBRACKET/RBRACKET with correct recursive arg-list
 *  parsing for a nested call's own params — exactly what a channel call
 *  inside `notify=[...]` needs. `rawBracketText` is the FULL `[...]`
 *  span; `baseStart` is its absolute offset, re-applied via
 *  `_shiftPositions` since a fresh `lexAgentStatement` call always
 *  counts from 0. Returns `{ type: 'list', value: CallNode[] }` — NOT
 *  the ordinary ValueNode list shape (whose items are ValueNodes); the
 *  sole consumer (`_compileNotifyClause`) knows to expect CallNode[]
 *  here and never routes this through `_coerceValueForSpec`/
 *  `_unwrapValueNode`. */
function _parseNotifyListArg(rawBracketText, baseStart) {
  const { tokens, errors } = lexAgentStatement(rawBracketText);
  if (errors.length) {
    const e = errors[0];
    throw new _ParseError(e.message, { start: (typeof e.position === 'number' ? e.position : 0) + baseStart });
  }
  const state = { tokens: _shiftPositions(tokens, baseStart), pos: 0 };
  const open = _peek(state);
  if (open.type !== 'LBRACKET') {
    throw new _ParseError('notify requires a list of channel calls, e.g. notify=[telegram, ntfy(priority="urgent")]', open);
  }
  _advance(state);
  const calls = [];
  if (_peek(state).type !== 'RBRACKET') {
    calls.push(parseCall(state));
    while (_peek(state).type === 'COMMA') {
      _advance(state);
      calls.push(parseCall(state));
    }
  }
  const close = _peek(state);
  if (close.type !== 'RBRACKET') throw new _ParseError('expected "]"', close);
  _advance(state);
  const trailing = _peek(state);
  if (trailing.type !== 'EOF') throw new _ParseError('unexpected trailing input after notify value', trailing);
  return { type: 'list', value: calls };
}

/** Bracket/brace/quote-aware split of a call's RAW inner text on TOP-LEVEL
 *  commas only (Sprint 8) — commas nested inside a `tp_scales_json=[...]`
 *  value (list of `{...}` object literals) must never split the arg list.
 *  Operates on the raw text BEFORE any tokenizing happens — the old
 *  Sprint 2 mechanism (tokenize the whole innerText via tokenizeCallStyle,
 *  then split at its own comma-punct tokens) has no bracket/brace
 *  awareness at all, so it would incorrectly treat every comma inside a
 *  tp_scales_json value as a fresh top-level argument. No existing call
 *  argument (any catalog action, order/bracket, metric/scope) uses `[`/
 *  `{` characters outside of this one new value shape, so this change is
 *  a no-op for every pre-Sprint-8 statement. Returns `[{text, start}]` —
 *  `start` is the segment's offset into `innerText`. */
function _splitTopLevelArgCommas(innerText) {
  const segs = [];
  let segStart = 0;
  let depth = 0;
  let inQuote = null;
  for (let i = 0; i < innerText.length; i++) {
    const ch = innerText[i];
    if (inQuote) { if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; continue; }
    if (ch === '(' || ch === '[' || ch === '{') { depth++; continue; }
    if (ch === ')' || ch === ']' || ch === '}') { depth--; continue; }
    if (ch === ',' && depth === 0) {
      segs.push({ text: innerText.slice(segStart, i), start: segStart });
      segStart = i + 1;
    }
  }
  segs.push({ text: innerText.slice(segStart), start: segStart });
  return segs;
}

const _TP_SCALES_JSON_ARG_RE = /^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(\[[\s\S]*\])$/;

/** Parse one raw (pre-split) argument segment into an ArgEntry — dispatches
 *  to the `tp_scales_json` object-literal-list parser, or (same bracket-
 *  shaped-value trick) the `notify` channel-call-list parser
 *  (`_parseNotifyListArg`), when the segment's key matches (case-
 *  insensitive) and its value is bracket-shaped; otherwise tokenizes via
 *  the ORIGINAL Sprint 2 path (tokenizeCallStyle + `_parseArgSegment`),
 *  unchanged for every other argument. Both share the SAME `key=[...]`
 *  regex (`_TP_SCALES_JSON_ARG_RE` — generically named only for
 *  historical reasons, matches any bracket-shaped kwarg value) since the
 *  only difference between them is which bracket-content parser runs. */
function _parseOneArgFromRawSegment(seg, labelToken) {
  const leadWs = seg.text.length - seg.text.replace(/^\s+/, '').length;
  const trimmed = seg.text.trim();
  if (trimmed === '') throw new _ParseError('empty argument (trailing/leading/double comma)', labelToken);
  const base = seg.start + leadWs;

  const m = _TP_SCALES_JSON_ARG_RE.exec(trimmed);
  if (m && (m[1].toLowerCase() === 'tp_scales_json' || m[1].toLowerCase() === 'notify')) {
    const bracketLocalIdx = trimmed.indexOf('[', m[1].length);
    const bracketText = trimmed.slice(bracketLocalIdx);
    const value = m[1].toLowerCase() === 'tp_scales_json'
      ? _parseTpScalesJsonArg(bracketText, base + bracketLocalIdx)
      : _parseNotifyListArg(bracketText, base + bracketLocalIdx);
    return { key: m[1], value };
  }

  const segTokens = tokenizeCallStyle(trimmed).map(t => ({ ...t, start: t.start + base, end: t.end + base }));
  return _parseArgSegment(segTokens, labelToken);
}

/** Parse the inner text of a call's `(...)` into ArgEntry[]. Splits on
 *  top-level commas via `_splitTopLevelArgCommas` (bracket/brace/quote-
 *  aware, Sprint 8) then parses each raw segment via
 *  `_parseOneArgFromRawSegment` — the Sprint 2 tokenizeCallStyle() reuse
 *  point for every ORDINARY argument is unchanged; only `tp_scales_json`
 *  gets a dedicated parse path (see that function's own docstring). */
function _parseArgListFromInnerText(innerText, labelToken) {
  const raw = innerText.trim();
  if (raw === '') return [];
  const rawSegments = _splitTopLevelArgCommas(innerText);
  return rawSegments.map(seg => _parseOneArgFromRawSegment(seg, labelToken));
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

  // Sprint [newline-separator] — `any[]` (not inferred from the first
  // push site's narrower shape): every token object gains a `nlBefore`
  // boolean in the post-pass below, which no individual push site here
  // declares up front.
  /** @type {any[]} */
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

      if (ch === '#') {
        // meta_item := "#" NAME "=" meta_value ("," meta_value)*
        // meta_value := STRING | BARE_WORD
        // Scanned entirely in THIS branch (not via the generic word-char/
        // STRING scanners above) because BARE_WORD is lexically WIDER than
        // NAME — it allows any character except whitespace/","/"#"/"="/a
        // quote (e.g. a hyphen, so "hedge-book" lexes as one token), which
        // the ordinary NAME scanner (`_isWordChar`) does not permit. The
        // key (before "=") IS a plain NAME — reuses `_isWordStart`/
        // `_isWordChar` unchanged.
        tokens.push({ type: 'HASH', start, end: i + 1 });
        i++;
        while (i < n && /\s/.test(text[i])) i++;
        if (!_isWordStart(text[i] || '')) throw new _ParseError('expected a meta key name after "#" (e.g. #name=...)', { start: i });
        const keyStart = i;
        while (i < n && _isWordChar(text[i])) i++;
        tokens.push({ type: 'NAME', value: text.slice(keyStart, i), reserved: false, hasCall: false, args: null, start: keyStart, end: i });
        while (i < n && /\s/.test(text[i])) i++;
        if (text[i] !== '=') throw new _ParseError('expected "=" after meta key', { start: i });
        tokens.push({ type: 'EQ', start: i, end: i + 1 });
        i++;
        // meta_value ("," meta_value)*
        while (true) {
          while (i < n && /\s/.test(text[i])) i++;
          const vStart = i;
          if (text[i] === '"' || text[i] === "'") {
            const quote = text[i];
            i++;
            while (i < n && text[i] !== quote) i++;
            if (i >= n) throw new _ParseError('unterminated string literal in meta value', { start: vStart });
            i++;
            tokens.push({ type: 'STRING', value: text.slice(vStart + 1, i - 1), start: vStart, end: i });
          } else {
            while (i < n && !/[\s,#="']/.test(text[i])) i++;
            if (i === vStart) throw new _ParseError('expected a meta value after "="', { start: vStart });
            tokens.push({ type: 'BAREWORD', value: text.slice(vStart, i), start: vStart, end: i });
          }
          while (i < n && /\s/.test(text[i])) i++;
          if (text[i] === ',') {
            tokens.push({ type: 'COMMA', start: i, end: i + 1 });
            i++;
            continue;
          }
          break;
        }
        continue;
      }

      if (ch === '<' || ch === '>' || ch === '=' || ch === '!') {
        const two = text.slice(i, i + 2);
        if (ch === '<') { if (two === '<=') { tokens.push({ type: 'LE', start, end: i + 2 }); i += 2; } else { tokens.push({ type: 'LT', start, end: i + 1 }); i += 1; } continue; }
        if (ch === '>') { if (two === '>=') { tokens.push({ type: 'GE', start, end: i + 2 }); i += 2; } else { tokens.push({ type: 'GT', start, end: i + 1 }); i += 1; } continue; }
        if (ch === '=') {
          if (two === '==') { tokens.push({ type: 'EQEQ', start, end: i + 2 }); i += 2; continue; }
          // A single "=" at the top level (outside any call's own "(...)",
          // which is tokenized separately via tokenizeCallStyle) has
          // exactly ONE legitimate grammar production: the ALERT clause's
          // contextual `msg=` default (Sprint 6, see parseAlertClauseBody).
          // No other production in this grammar ever expects a bare EQ
          // token, so emitting one here (instead of throwing, as before)
          // is safe — any OTHER use of a stray "=" still fails, just at
          // the PARSER stage now ("expected a comparator" / "unexpected
          // trailing input") rather than at lex time.
          tokens.push({ type: 'EQ', start, end: i + 1 });
          i += 1;
          continue;
        }
        if (ch === '!') {
          if (two === '!=') { tokens.push({ type: 'NE', start, end: i + 2 }); i += 2; continue; }
          // Sprint 20 (item 3) — targeted hint: `!x` (negation) isn't
          // this grammar's spelling — `~`/`not` negate, `!=` compares.
          throw new _ParseError(`unexpected '!' — use '~' or 'not' to negate, '!=' for not-equal`, { start });
        }
      }

      // Sprint [GIVEN-expr] — `**` (exponentiation) must be checked BEFORE
      // the generic `_PUNCT_SINGLE[ch]` lookup below, since a single `*`
      // is ALREADY a real token (STAR — Sprint 19's qualifier wildcard,
      // `account:*`) and `_PUNCT_SINGLE` only ever matches one character
      // at a time. A doubled `**` is consumed here as ONE token; a lone
      // `*` falls through unchanged to the existing STAR handling.
      if (ch === '*' && text[i + 1] === '*') {
        tokens.push({ type: 'STARSTAR', start, end: i + 2 });
        i += 2;
        continue;
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

      // Sprint [GIVEN-expr] — binary MINUS for GIVEN's new arithmetic
      // `given_rhs` alternative. Deliberately NOT added to `_PUNCT_SINGLE`
      // (checked earlier in this branch order) — that would fire BEFORE
      // the digit branch above ever got a chance to absorb a negative
      // NUMBER literal (`value=-50000`), breaking every existing negative-
      // number statement. Placed here, AFTER the digit branch, so this is
      // only ever reached when `-` is NOT immediately followed by a digit
      // (the digit branch's own condition already claimed that case).
      // KNOWN, PRE-EXISTING LEXER TRAP (not introduced by this change,
      // just newly reachable): the Sprint 10 mid-word-hyphen NAME rule
      // below treats a digit as an ordinary word character, so `p-5`
      // (no spaces) lexes as ONE NAME token `"p-5"`, not NAME `p` MINUS
      // NUMBER `5` — an arithmetic subtraction against a literal needs a
      // space before the `-` (e.g. `p - 5` or `p -5`, not `p-5`) to lex as
      // three separate tokens.
      if (ch === '-') {
        tokens.push({ type: 'MINUS', start, end: i + 1 });
        i++;
        continue;
      }

      if (_isWordStart(ch)) {
        // Sprint 10 — this is the ONE place a bare CALL/TOKEN-REFERENCE NAME
        // is scanned (metric/scope/channel/action names, call names
        // generally — everywhere `call := NAME | NAME "(" arg_list ")"` is
        // used). Widened HERE ONLY (not the shared `_isWordChar` class
        // itself, which kwarg KEYS and every other NAME use everywhere else
        // in this file still rely on unchanged) to additionally accept a
        // MID-WORD hyphen, so a real seeded Token name like
        // `notify-critical-trio` lexes as ONE token. Never leading (a
        // leading "-" never reaches this branch at all — `_isWordStart`
        // doesn't match it, and the NUMBER branch above already claims
        // "-" immediately followed by a digit, e.g. `value=-50000`, before
        // this branch is ever reached) and never trailing (a "-" is only
        // consumed when the character immediately after it is itself a
        // word char — a trailing "-" before whitespace/EOF/punctuation is
        // left unconsumed, so `notify-critical-trio-` lexes as the NAME
        // `notify-critical-trio` followed by a bare `-`, not swallowed into
        // the name). Kwarg KEYS are scanned entirely by a SEPARATE tokenizer
        // (`tokenizeCallStyle` in engine.js, reached only from inside a
        // call's own "(...)" via `_parseArgListFromInnerText`) — untouched
        // by this widening, so a kwarg key still cannot contain a hyphen.
        let j = i;
        while (j < n) {
          if (_isWordChar(text[j])) { j++; continue; }
          if (text[j] === '-' && _isWordChar(text[j + 1] || '')) { j++; continue; }
          break;
        }
        const nameRaw = text.slice(start, j);
        i = j;
        const lower = nameRaw.toLowerCase();
        // Sprint [grammar-unify] — `alert` is excluded from the generic
        // "NAME followed by '(' is a balanced-paren call" branch below,
        // UNCONDITIONALLY (whether or not a '(' follows) — it always
        // lexes as a bare keyword-shaped NAME token (`hasCall: false`),
        // exactly as it always has. A literal '(' immediately after it is
        // left UNCONSUMED here, falling through to the ordinary top-level
        // punctuation branch on the lexer's NEXT loop iteration, which
        // already produces a real LPAREN token (see `_PUNCT_SINGLE`) —
        // the same token type arithmetic/condition grouping already use.
        // This is deliberate, not an oversight: `alert(...)`'s own body
        // is the UNCHANGED `clause_item` list grammar (assignments,
        // `msg=` sugar, a channel call_list — see `parseItemListClause`),
        // which is already fully expressible against the MAIN token
        // stream — reusing it via real LPAREN/RPAREN tokens means
        // `alert(...)` needs no dedicated sub-lexer at all, and
        // `compileAlertClause` (the channel/events compiler) is reused
        // completely unchanged. See `parseCall`'s own `alert`-specific
        // branch for where `(` ... `)` actually gets consumed.
        if (text[i] === '(' && lower !== 'alert') {
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
          // Sprint G (Task 2) — `debug(x)` takes exactly ONE BARE argument
          // (a label or token reference name, no "="), which `arg_list`'s
          // ordinary keyword-only rule (every argument is `name=value`)
          // cannot express at all — `_parseArgListFromInnerText` would
          // reject a bare `x` with "argument must be written as
          // name=value". Special-cased HERE, by name, before the generic
          // arg-list parse ever runs — mirrors this grammar's established
          // "contextual, not reserved" carve-outs (Sprint 6's `msg=`,
          // Sprint 12's event-meta keys) rather than widening `arg_list`
          // itself for every OTHER call. Produces a single synthetic
          // ArgEntry under the fixed key `'x'` so the rest of the pipeline
          // (parseCall/CallNode shape) is completely unchanged — only
          // `_compileDebugCall` (compiler) and the parser's label-binding
          // substitution ever look at this specific entry.
          //
          // Item 4 — `help(x)` takes the SAME single-bare-argument shape
          // (a token/function name, no "="), mirrored byte-for-byte from
          // `debug(x)` above, differing only in the synthetic arg's KEY:
          // `'name'`, matching the `help` action's own `params_schema`
          // (`{name: {type: string, required: true}}`) — so the generic
          // `_compileGenericActionParams` path resolves it with zero
          // special-casing on the compiler side, unlike `debug(x)` (which
          // has its own dedicated compiler, `_compileDebugCall`, because
          // its value resolves against bound LABELS, not a catalog
          // schema).
          let args;
          if (lower === 'debug' || lower === 'help') {
            const trimmed = innerText.trim();
            const leadWs = innerText.length - innerText.replace(/^\s+/, '').length;
            if (!/^[A-Za-z_][A-Za-z0-9_-]*$/.test(trimmed)) {
              throw new _ParseError(
                `${lower}(...) takes exactly one bare argument — a label or token reference name, e.g. ${lower}(x)`,
                { start: innerStart + leadWs },
              );
            }
            args = [{
              key: lower === 'debug' ? 'x' : 'name',
              keyStart: innerStart + leadWs,
              value: { type: 'name', value: trimmed, start: innerStart + leadWs, end: innerStart + leadWs + trimmed.length },
            }];
          } else {
            // Note: innerText.trim() !== '' always yields args.length >= 1 or
            // a thrown _ParseError (tokenizeCallStyle never silently drops a
            // non-whitespace char) — no separate "non-empty but zero args"
            // case to guard here.
            args = _parseArgListFromInnerText(innerText, { start });
          }
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
  // Sprint [newline-separator] — post-pass, not threaded through every
  // individual push site above: `tokens[k].nlBefore` is true iff a `\n`
  // occurred anywhere in the (whitespace-only) gap between the PREVIOUS
  // token's end and this token's start. Computed unconditionally for
  // every token stream this function ever produces (including the
  // notify=[...]/tp_scales_json=[...] mini-lexer calls) — cheap, and a
  // harmless no-op wherever nothing ever consults it; only the four
  // newline-as-separator production sites (parseItemListClause,
  // parseCallList's opt-in mode, parseListLiteral, parseMatchValue) read
  // this field. `tokens[0]` (and a lexer that produced zero tokens before
  // EOF) has no "previous" token — `nlBefore` stays `false`.
  for (let k = 1; k < tokens.length; k++) {
    tokens[k].nlBefore = text.slice(tokens[k - 1].end, tokens[k].start).includes('\n');
  }
  if (tokens.length) tokens[0].nlBefore = false;
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

/** `call := NAME | NAME "(" arg_list ")" | alert_call`
 *  `alert_call := "alert" "(" clause_body ")"` — Sprint [grammar-unify].
 *  `alert` is lexed as a BARE keyword-shaped NAME token unconditionally
 *  (see `lexAgentStatement`'s own `alert`-exclusion comment), so a literal
 *  `(` immediately after it reaches HERE as its own real LPAREN token,
 *  not as part of the NAME. Recognized by name + a following LPAREN
 *  (never ambiguous — `alert` with no `(` following, or event_stmt's
 *  bare `alert log_tag=...` form, both dispatch elsewhere entirely, see
 *  `_dispatchStatement`). The body between the parens is the EXACT SAME
 *  `clause_item` list grammar the old `ALERT <body>` clause always used
 *  (`parseItemListClause`, with the `msg=` default sugar enabled) — reused
 *  byte-for-byte, parsed directly against the MAIN token stream (no
 *  sub-lexer needed, since LPAREN/RPAREN are already real tokens here).
 *  Returned as an ordinary `{type:'call', name:'alert', ...}` node (so
 *  every generic "only X can be used standalone"/call-name check keeps
 *  working unchanged) with one extra field, `_alertBody`, carrying the
 *  parsed item list — see `_compileAlertCallAsOrdinaryCall`, the ONE
 *  place that ever reads it. */
export function parseCall(state) {
  const tok = _peek(state);
  if (tok.type !== 'NAME') throw new _ParseError('expected a name', tok);
  if (tok.reserved) throw new _ParseError(`"${tok.value}" is a reserved word and cannot be used as a token name`, tok);
  if (tok.hasCall && (!tok.args || tok.args.length === 0)) {
    throw new _ParseError(`"${tok.value}()" is a parse error — a zero-argument token is always written bare`, tok);
  }
  const isAlertCall = tok.type === 'NAME' && !tok.hasCall && tok.value.toLowerCase() === 'alert'
    && state.tokens[state.pos + 1] && state.tokens[state.pos + 1].type === 'LPAREN';
  _advance(state);
  // Sprint G — snapshot whatever labels are visible RIGHT HERE (a plain
  // copy of `state.bindings`, cheap — statements bind at most a handful
  // of labels) so compile-time arg substitution (`_substituteBoundArgs`)
  // can later resolve a bare-NAME arg value against exactly the bindings
  // in scope at THIS call's own position in the source text, enforcing
  // left-to-right visibility (no forward reference) even though every
  // call's args were already parsed into ArgEntry[] at LEX time, before
  // any binding existed. `undefined` (not an empty Map) for every
  // pre-Sprint-G call site with no GIVEN/assignment anywhere in the
  // statement — a true no-op, zero behavior change.
  const bindingsAt = state.bindings && state.bindings.size ? new Map(state.bindings) : undefined;
  if (isAlertCall) {
    _advance(state); // "("
    const { items } = parseItemListClause(state, { allowMsgDefault: true, allowNop: true });
    const close = _peek(state);
    if (close.type !== 'RPAREN') throw new _ParseError('expected ")" to close alert(...)', close);
    _advance(state);
    return {
      type: 'call', name: 'alert', args: [], hasCall: true,
      start: tok.start, end: close.end, _bindingsAt: bindingsAt, _alertBody: items,
    };
  }
  return { type: 'call', name: tok.value, args: tok.args || [], hasCall: tok.hasCall, start: tok.start, end: tok.end, _bindingsAt: bindingsAt };
}

/** `arg_list := first_arg ("," kw_arg)*` — args are resolved at LEX time
 *  (tokenizeCallStyle reuse point, see `_parseArgListFromInnerText`); this
 *  accessor exists for EBNF-production naming parity with the formal
 *  grammar and for Sprint 3/4 consumers that want to inspect a call's
 *  already-parsed argument list without re-deriving it. */
export function parseArgList(callNode) { return callNode.args || []; }

/** `scope_seg := call | qualifier_seg` (Sprint 19)
 *  `qualifier_seg := NAME ":" qualifier_value`
 *  `qualifier_value := "*" | NAME | STRING`
 *
 *  Disambiguated from an ordinary `call` segment by a bare NAME
 *  immediately followed by `:` — a real scope-family/scope-narrowing
 *  name is NEVER itself followed by `:` anywhere else in this grammar,
 *  so this lookahead is unambiguous. The qualifier KEY (`account`/
 *  `symbol`) is accepted permissively here (any bare NAME) — an unknown
 *  key is a compile-time error (`_resolveScopeRefFull`), same
 *  parse-permissively/validate-at-compile split this grammar always
 *  uses. `*` is the reserved "any value" wildcard; a bare NAME value of
 *  `worst` (case-insensitive) is the reserved "computed worst" sentinel
 *  — both resolved at compile time, never here (this function only
 *  shapes the AST). */
function parseScopeSeg(state) {
  const tok = _peek(state);
  if (tok.type === 'NAME' && !tok.hasCall) {
    const next = state.tokens[state.pos + 1];
    if (next && next.type === 'COLON') {
      _advance(state); // qualifier key
      _advance(state); // ":"
      const valueTok = _peek(state);
      let value;
      if (valueTok.type === 'STAR') value = { type: 'star' };
      else if (valueTok.type === 'NAME' && !valueTok.hasCall) value = { type: 'name', value: valueTok.value };
      else if (valueTok.type === 'STRING') value = { type: 'string', value: valueTok.value };
      else {
        throw new _ParseError(
          `expected a qualifier value after '${tok.value}:' (a bare account/symbol value, "*", or "worst")`,
          valueTok,
        );
      }
      _advance(state); // value
      return { type: 'qualifier', key: tok.value, value, start: tok.start, end: valueTok.end };
    }
  }
  return parseCall(state);
}

/** `scope_ref := scope_seg ("." scope_seg)*` */
export function parseScopeRef(state) {
  const segments = [parseScopeSeg(state)];
  while (_peek(state).type === 'DOT') {
    _advance(state);
    segments.push(parseScopeSeg(state));
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

/** `metric_ref | col_ref` — Sprint 15's shared entry point for BOTH
 *  `parseLeaf`'s between-chain branch and (via its own inline handling,
 *  not this function — see `parseLeaf` directly) the plain leaf branch.
 *  Parses the first `call`, then dispatches on what follows it: "." means
 *  a bare `col_ref` chain (collected the same way `scope_ref` collects its
 *  own "." call)* segments — NOT yet disambiguated into scope-vs-column
 *  here, that split is a compile-time decision, see `_resolveDotAccess`);
 *  "@" means an ordinary `metric_ref`; anything else is a parse error
 *  (the exact same "expected '@' after metric" wording `parseMetricRef`
 *  already throws, since a `between` chain's metric position has no bare-
 *  ref/Token-reference alternative the way a plain leaf does). Returns
 *  `{ kind: 'colref', segments: (CallNode|QualifierNode)[] }` or
 *  `{ kind: 'metric', metric: CallNode, scope: ScopeNode }`. Sprint 21 —
 *  every segment AFTER the first uses `parseScopeSeg` (not `parseCall`),
 *  so a qualifier segment (`symbol:NIFTY25JULFUT`) may now appear
 *  anywhere in the chain before the trailing column name — see
 *  `_resolveDotAccessWithQualifier`'s own header comment for the
 *  resolution-side half of this. The first segment stays `parseCall`
 *  (unchanged) since the colref/metric_ref dispatch below only knows to
 *  look for a following "." or "@" once it's been consumed. */
function parseMetricOrColRef(state) {
  const first = parseCall(state);
  if (_peek(state).type === 'DOT') {
    // Sprint 21 — typed `any[]` (not inferred from `first`'s narrower
    // CallNode shape) since a later segment may now be a QualifierNode.
    /** @type {any[]} */
    const segments = [first];
    while (_peek(state).type === 'DOT') {
      _advance(state);
      segments.push(parseScopeSeg(state));
    }
    return { kind: 'colref', segments };
  }
  const at = _peek(state);
  if (at.type !== 'AT') throw new _ParseError(`expected "@" after metric '${first.name}'`, at);
  _advance(state);
  const scope = parseScopeRef(state);
  return { kind: 'metric', metric: first, scope };
}

/** Sprint [newline-separator] — true iff `tok` could legally START a new
 *  GIVEN/ALERT/DO clause item or call-list call (a plain, non-reserved
 *  NAME, with or without its own call) — used ONLY to decide whether a
 *  bare newline (no literal separator token) should be treated as if the
 *  separator were present. Never true for EOF/HASH/a closing bracket/a
 *  reserved keyword (WHEN/ALERT/DO/GIVEN/NOP/ALWAYS/IN/NOT/AND/OR/TRUE/
 *  FALSE — `_RESERVED`), so a newline immediately before any of those is
 *  correctly left alone (falls through to whatever the existing
 *  "expected X" error already says). NAME-only — clause items and calls
 *  always start with a bare name; see `_looksLikeNewValueListItemStart`
 *  for the WIDER start-set a plain `value` list literal needs (NUMBER/
 *  STRING/LBRACKET are also valid list-item starts, neither a clause
 *  item nor a call can ever start with one). */
function _looksLikeNewListItemStart(tok) {
  return tok.type === 'NAME' && !tok.reserved;
}

/** Sprint [newline-separator] — the WIDER start-set `list_literal`'s own
 *  `value` production allows (`NUMBER | STRING | NAME | list_literal`,
 *  see `parseValue`) — a plain value list (`[1, 2, 3]`) may start a new
 *  item with a bare number/string/nested-list, not just a NAME. */
function _looksLikeNewValueListItemStart(tok) {
  return tok.type === 'NUMBER' || tok.type === 'STRING' || tok.type === 'LBRACKET' || _looksLikeNewListItemStart(tok);
}

/** `list_literal := "[" value ("," value)* "]"` — Sprint [newline-separator]:
 *  a bare newline (no literal `,`) is also accepted between items, same
 *  rule as every other list-of-X production in this file (see the
 *  section-header comment on `parseItemListClause`/`parseCallList` for
 *  the shared rationale) — a LITERAL `,` still always demands a following
 *  item (the existing trailing-comma check below is untouched). */
export function parseListLiteral(state) {
  const open = _peek(state);
  if (open.type !== 'LBRACKET') throw new _ParseError('expected "["', open);
  _advance(state);
  const items = [];
  if (_peek(state).type !== 'RBRACKET') {
    items.push(parseValue(state));
    while (true) {
      if (_peek(state).type === 'COMMA') {
        _advance(state);
        if (_peek(state).type === 'RBRACKET') throw new _ParseError('trailing comma in list literal', _peek(state));
        items.push(parseValue(state));
        continue;
      }
      if (_peek(state).nlBefore && _looksLikeNewValueListItemStart(_peek(state))) {
        items.push(parseValue(state));
        continue;
      }
      break;
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
  // Sprint 20 (item 3) — a lone EQ ("=") is ALSO recognized as "a
  // comparator is starting here" (not a real comparator — `parseComparator`
  // immediately rejects it with the targeted "==" hint below) so that
  // rejection actually happens, instead of `parseLeaf` silently treating
  // the metric as a boolean-shorthand leaf and leaving the "=" dangling
  // as unconsumed trailing input with a far less specific error.
  if (tok.type === 'EQ') return true;
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
  // Sprint 20 (item 3) — targeted hint: a lone "=" lexes fine (it's
  // `alert_clause`'s own `msg=` production elsewhere) but is never a
  // valid COMPARATOR — the trader almost always meant "==".
  if (tok.type === 'EQ') throw new _ParseError('expected a comparator — use "==" for equality, not "="', tok);
  throw new _ParseError('expected a comparator', tok);
}

function _isLiteralStart(tok) {
  return tok.type === 'NUMBER' || tok.type === 'STRING' || tok.type === 'LBRACKET';
}

/** `leaf := metric_ref comparator value | col_ref comparator value
 *         | value comparator (metric_ref | col_ref) comparator value | call`
 *  (Sprint 10's bare-call/Token-reference alternative, and Sprint 15's bare
 *  `col_ref` dot-access alternative — see the file-header EBNF's own
 *  comment on this production for the full rationale on both.) */
export function parseLeaf(state) {
  const startTok = _peek(state);
  if (_isLiteralStart(startTok)) {
    const value1 = parseValue(state);
    const op1 = parseComparator(state);
    const ref = parseMetricOrColRef(state);
    const op2 = parseComparator(state);
    const value2 = parseValue(state);
    if (ref.kind === 'colref') {
      return { type: 'between', value1, op1, colSegments: ref.segments, op2, value2 };
    }
    return { type: 'between', value1, op1, metric: ref.metric, scope: ref.scope, op2, value2 };
  }
  const firstCall = parseCall(state);
  if (_peek(state).type === 'DOT') {
    // Sprint 15 — a bare `col_ref` chain with NO "@" at all, e.g.
    // `positions.total.pnl <= -50000`. Collected exactly like
    // `scope_ref`'s own "." call)* segments — the scope-vs-column split
    // is resolved later, at COMPILE time (`_resolveDotAccess`), not here.
    // This branch is reached ONLY when a "." immediately follows the
    // first call with no "@" anywhere — the existing `scope_ref` grammar
    // (always preceded by "@") is completely untouched.
    // Sprint 21 — segments AFTER the first use `parseScopeSeg` (same as
    // `parseMetricOrColRef`'s identical loop above), so a qualifier
    // segment may appear mid-chain, e.g.
    // `quote.tracked.symbol:NIFTY25JULFUT.ltp <= 100`. Typed `any[]` for
    // the same reason as that sibling loop.
    /** @type {any[]} */
    const segments = [firstCall];
    while (_peek(state).type === 'DOT') {
      _advance(state);
      segments.push(parseScopeSeg(state));
    }
    if (_isComparatorStart(_peek(state))) {
      const op = parseComparator(state);
      const value = parseValue(state);
      return { type: 'colref', segments, op, value };
    }
    return { type: 'colref', segments, op: null, value: null };
  }
  if (_peek(state).type !== 'AT') {
    // Sprint G — a bare (non-call) NAME here that matches an already-
    // visible label (`state.bindings`, left-to-right source order) is a
    // CONDITION-kind label reference: splice in a CLONE of the bound
    // condition subtree directly, in place of what would otherwise be a
    // Sprint-10 Token ($ref) reference — this is the "label used as the
    // whole bare WHEN condition" case (`GIVEN x = (...) & (...); WHEN x`).
    // A PRIMITIVE-kind label used here is a clear parse-time error (zero
    // catalog dependency either way, so there's no reason to defer this
    // one to compile time the way an unrecognized name normally is).
    if (!firstCall.hasCall && state.bindings) {
      const bound = state.bindings.get(firstCall.name.toLowerCase());
      if (bound) {
        if (bound.rhsKind === 'condition') {
          // Sprint [GIVEN-expr] — a bound label whose condition subtree is
          // a BARE, operator-less `leaf`/`colref` (`op === null` — i.e. a
          // "pure alias" GIVEN binding, e.g. `p = pnl@positions.total`,
          // with no comparator of its own) immediately followed by a
          // comparator here is re-attached to a FRESH leaf built from the
          // bound metric/scope (or colref segments) plus the comparator +
          // value just parsed — e.g. `WHEN p <= -50000`. Without this, a
          // pure-alias label could ONLY ever be used as a whole bare WHEN
          // condition or inside debug(...) (the pre-existing "KNOWN
          // LIMITATION" documented below), making it structurally
          // impossible to write `WHEN p <= -50000` at all — the
          // motivating, explicitly-requested example this fix exists for.
          // Deliberately narrow: does NOT extend to a REAL multi-leaf
          // condition binding (one with its own comparator, or combined
          // via &/|/~/between) — those still only ever splice in whole,
          // unchanged, via the fallback `_cloneAstNode` below. Additive
          // and zero-regression by construction: this branch is only ever
          // reached on input that previously threw "unexpected trailing
          // input" (a comparator left over after the bound node spliced
          // in whole) — no previously-valid statement's parse changes.
          const node = bound.rhsNode;
          if (node && node.op === null && (node.type === 'leaf' || node.type === 'colref') && _isComparatorStart(_peek(state))) {
            const op = parseComparator(state);
            const value = parseValue(state);
            return node.type === 'leaf'
              ? { type: 'leaf', metric: _cloneAstNode(node.metric), scope: _cloneAstNode(node.scope), op, value }
              : { type: 'colref', segments: _cloneAstNode(node.segments), op, value };
          }
          return _cloneAstNode(node);
        }
        throw new _ParseError(
          `label '${firstCall.name}' is bound to a plain value — it cannot be used as a WHEN condition `
          + `(only a label bound to a condition, e.g. 'x = metric@scope <= value', can be)`,
          firstCall,
        );
      }
    }
    // No "@" and no "." follows, and not a bound label — a real metric
    // ALWAYS requires "@ scope_ref", so this can only be a bare Token
    // ($ref) reference. Deferred to COMPILE time (see `compileRef`) —
    // parse permissively, validate once the catalog is known, same split
    // this grammar already uses everywhere else.
    //
    // KNOWN LIMITATION, operator-accepted 2026-10-11 (Sprint G): a
    // PRIMITIVE-kind label (e.g. `GIVEN x = mean_pnl(30)@positions.total`)
    // can ONLY be read as a whole value (an action/channel argument) or
    // — via the CONDITION-kind branch above — as a whole bare WHEN
    // condition. It canNOT be re-attached to a NEW comparator to form a
    // fresh leaf (`WHEN x <= -50000` does NOT work when `x` is bound to a
    // metric_ref, even though `WHEN x` alone works when `x` is bound to a
    // full condition). This is structural, not an oversight: a leaf's
    // shape (`metric_ref comparator value`) is fixed at PARSE time by
    // this very function, and a bare label reaching this branch has
    // already committed to being parsed as a `ref`/condition-splice node,
    // not a `metric_ref` — there is no production here that re-opens a
    // label into "the left operand of a comparator I haven't seen yet."
    // Supporting it would need a real grammar change (a label's bound
    // metric_ref AST node substituting into leaf position), not a
    // compile-time workaround. If this is ever needed, write the full
    // expression inline instead: `WHEN mean_pnl(30)@positions.total <= -50000`.
    return { type: 'ref', call: firstCall };
  }
  _advance(state); // consume "@"
  const scope = parseScopeRef(state);
  if (_isComparatorStart(_peek(state))) {
    const op = parseComparator(state);
    const value = parseValue(state);
    return { type: 'leaf', metric: firstCall, scope, op, value };
  }
  // Boolean-metric shorthand — syntactically accepted here; the semantic
  // check ("was this metric actually boolean?") happens at compile time,
  // once the token is resolved against the catalog (see design doc).
  return { type: 'leaf', metric: firstCall, scope, op: null, value: null };
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

// Sprint 20 (item 3) — `and`/`or`/`not` are accepted as case-insensitive
// WORD aliases for `&`/`|`/`~` respectively, matching how a trader
// already thinks/speaks about boolean logic (the standing "intuitiveness
// over technical correctness alone" design principle). Recognized by
// PEEKING at a bare (non-call) NAME token — never by lexer-level token-
// type rewriting — so no other production in this grammar needs to
// change. `not` collides with NOTHING: the existing `not in` comparator
// (`parseComparator`) is only ever consumed AFTER a `metric@scope` has
// already been parsed, deep inside `parseLeaf` — `parseTerm` only ever
// peeks at a bare `not` at the START of a fresh term (before any
// `metric@scope` exists yet), so the two productions can never collide
// on the same token.
function _isAndTok(tok) { return tok.type === 'AMP' || (tok.type === 'NAME' && !tok.hasCall && _wordIs(tok, 'and')); }
function _isOrTok(tok) { return tok.type === 'PIPE' || (tok.type === 'NAME' && !tok.hasCall && _wordIs(tok, 'or')); }
function _isNotTok(tok) { return tok.type === 'TILDE' || (tok.type === 'NAME' && !tok.hasCall && _wordIs(tok, 'not')); }

/** `term := ("~" | "not") primary | primary` */
export function parseTerm(state, depth = 0) {
  const tok = _peek(state);
  if (_isNotTok(tok)) {
    _checkDepth(depth + 1, tok);
    _advance(state);
    const inner = parsePrimary(state, depth + 1);
    return { type: 'not', term: inner };
  }
  return parsePrimary(state, depth);
}

/** `and_expr := term (("&" | "and") term)*` — left-to-right fold,
 *  BINDS TIGHTER than `or_expr` below (Sprint 20, item 3 — see its own
 *  comment for why this is now a real precedence split rather than the
 *  pre-Sprint-20 "`&`/`|` share one equal-precedence level" design). */
export function parseAndExpr(state, depth = 0) {
  let node = parseTerm(state, depth);
  while (_isAndTok(_peek(state))) {
    _advance(state);
    node = { type: 'and', left: node, right: parseTerm(state, depth) };
  }
  return node;
}

/** `or_expr := and_expr (("|" | "or") and_expr)*` — left-to-right fold.
 *
 *  Sprint 20 (item 3) — PRECEDENCE CHANGE: `&`/`and` now binds TIGHTER
 *  than `|`/`or` (standard boolean precedence — matches Python's
 *  `and`/`or`, SQL, and how a trader already reads a mixed expression),
 *  replacing the pre-Sprint-20 "equal precedence, strictly left-to-right"
 *  design where `a | b & c` silently meant `(a | b) & c` with no warning
 *  — the exact audit-flagged defect this closes. An unparenthesized
 *  mixed `&`/`|` chain now means `a | (b & c)` instead.
 *
 *  Confirmed safe to change (no silent behavior flip on any existing
 *  agent): (1) neither `agentsGrammar.test.js` nor
 *  `agentsDecompiler.test.js` has ANY test exercising an unparenthesized
 *  mixed `&`/`|` chain — the only mixed-operator fixtures in either file
 *  are fully, explicitly parenthesized. (2) `decompileAllAny` (this
 *  file) ALWAYS wraps every nested child in its own `(...)` at the
 *  recursive call boundary — `decompileCondNode`'s own caller wraps
 *  `inner` in `(${inner})` regardless of what shape `inner` is — so a
 *  decompiled agent can NEVER emit an unparenthesized mixed chain in the
 *  first place; re-saving an existing agent through the CLI (decompile →
 *  recompile with no edits) is unaffected by this precedence change. */
export function parseOrExpr(state, depth = 0) {
  let node = parseAndExpr(state, depth);
  while (_isOrTok(_peek(state))) {
    _advance(state);
    node = { type: 'or', left: node, right: parseAndExpr(state, depth) };
  }
  return node;
}

/** `condition := "always" | or_expr` — "always" is standalone-only: a
 *  parse error, not a silently-tolerated redundancy, if followed by `&`
 *  or `|` (or their `and`/`or` word aliases) right here at the top
 *  level. `always` appearing as an OPERAND deeper in an expression (e.g.
 *  `x | always`) is caught differently — `parseCall` rejects any
 *  reserved word used as a token name, which `always` always is outside
 *  this one top-level sentinel spot — still a clear parse error, just a
 *  different message. */
export function parseCondition(state, depth = 0) {
  const tok = _peek(state);
  if (tok.type === 'NAME' && _wordIs(tok, 'always') && !tok.hasCall) {
    _advance(state);
    const next = _peek(state);
    if (_isAndTok(next) || _isOrTok(next)) {
      throw new _ParseError('"always" is standalone-only — it cannot be combined with & or |', next);
    }
    return { type: 'always' };
  }
  return parseOrExpr(state, depth);
}

/** Sprint [newline-separator] — stricter than `_looksLikeNewListItemStart`:
 *  used ONLY by `parseCallList`'s opt-in newline mode (see that function),
 *  where a bare NAME immediately followed by `=` is the START OF A NEW
 *  clause_item (a label assignment) — that belongs to the OUTER
 *  `parseItemListClause` loop, not to THIS call-list. Without this
 *  exclusion, `ALERT telegram\nmsg_txt = "x", email` would try to parse
 *  `msg_txt` as just another bare call name in `telegram`'s own
 *  call-list, leaving `= "x"` as confusing unconsumed trailing input. */
function _looksLikeNewCallStartAfterNewline(state, tok) {
  if (!_looksLikeNewListItemStart(tok)) return false;
  const next = state.tokens[state.pos + 1];
  return !(next && next.type === 'EQ');
}

/** `call_list := call ("," call)*` — Sprint [newline-separator]: in
 *  `allowNewline` mode ONLY (opt-in, see call sites), a bare newline is
 *  also accepted in place of the `,` — see `parseItemListClause`'s
 *  header comment for the shared rationale. `parseOrderStmt`'s bare
 *  multi-order statement and `parseEventStmt`'s channel call_list both
 *  call this with NO second argument (`allowNewline` defaults to
 *  `false`), so both stay comma-only, unchanged, per the operator's
 *  explicit instruction. */
function parseCallList(state, { allowNewline = false } = {}) {
  const calls = [parseCall(state)];
  while (true) {
    if (_peek(state).type === 'COMMA') {
      // Sprint [comma-separator] — a comma followed by `NAME "="` is NOT
      // another call in THIS list; it's a new assignment item, which
      // belongs to the OUTER `parseItemListClause` loop (now that it
      // also accepts a bare comma as an item separator — see that
      // function). Mirrors the newline path's existing
      // `_looksLikeNewCallStartAfterNewline` exclusion below, which
      // already special-cases this for the newline-separator case; the
      // comma path never had the equivalent guard, so e.g.
      // `ALERT telegram, a2=1` used to misparse `a2` as a second (bogus)
      // call and choke on the leftover "=". A call's own `(...)` args
      // are resolved entirely at LEX time (never re-tokenized as a
      // standalone EQ in this stream), so this can only ever match a
      // bare, parenless name immediately followed by a real top-level
      // "=" — i.e. exactly the "this is actually an assignment" case,
      // never a legitimate call's own syntax.
      const nameTok = state.tokens[state.pos + 1];
      const afterName = state.tokens[state.pos + 2];
      const looksLikeNewAssignment = nameTok && nameTok.type === 'NAME' && !nameTok.reserved
        && afterName && afterName.type === 'EQ';
      if (looksLikeNewAssignment) break; // leave the comma for the outer loop
      _advance(state);
      calls.push(parseCall(state));
      continue;
    }
    if (allowNewline && _peek(state).nlBefore && _looksLikeNewCallStartAfterNewline(state, _peek(state))) {
      calls.push(parseCall(state));
      continue;
    }
    break;
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

// ═══════════════════════════════════════════════════════════════════════
// Sprint G — GIVEN preamble + the "=" bind operator + debug(...)
// ═══════════════════════════════════════════════════════════════════════
//
// `given_part := "GIVEN" clause_item (";" clause_item)*`           (new)
// `alert_clause := "nop" | clause_item (";" clause_item)*`  (generalizes
//                                                             Sprint 6)
// `do_clause    := "nop" | clause_item (";" clause_item)*`  (generalizes
//                                                             the old
//                                                             plain
//                                                             call_list)
// `clause_item  := assignment | (msg_default? call_list)`
// `assignment   := NAME "=" given_rhs`
// `given_rhs    := value | or_expr`
// `msg_default  := "msg" "=" value ","`      (Sprint 6, ALERT-only,
//                                              PRESERVED byte-for-byte —
//                                              see `parseOneClauseItem`)
//
// The bind operator is plain "=" (operator-revised; an earlier `:=`
// design was superseded before any shipped code depended on it) — this
// sidesteps a `:=`-vs-`:`-qualifier lexer disambiguation problem entirely
// (no two-character lookahead needed: `=` and `:` never share a
// character), and reads as "the same `name=value` kwarg syntax, just at
// STATEMENT level instead of inside a call's parens." A label may be
// bound ANYWHERE in GIVEN/ALERT/DO (not just GIVEN) and read anywhere
// else in the SAME statement, including a clause that textually precedes
// it being bound LATER being a genuine, rejected forward-reference (see
// `_registerBinding`/`parseLeaf`'s bare-ref branch) — `state.bindings`
// is a single Map threaded through the WHOLE parse, mutated in strict
// left-to-right source order, so visibility naturally follows text order
// with zero extra bookkeeping. `WHEN` itself never WRITES a binding
// (read-only), only reads ones already visible by the time it's parsed.
//
// Binding kind (Sprint G's "dict vs primitive" type rule) is determined
// structurally by `given_rhs`, not declared: `value` (NUMBER/STRING/NAME/
// list_literal) is a PRIMITIVE binding; anything requiring the full
// `or_expr` condition grammar (a bare metric_ref/col_ref boolean-
// shorthand leaf, a between-chain, a Token ($ref) reference, or a real
// `&`/`|`/`~`-combined tree) is a CONDITION binding, which compiles to a
// JSON dict — see `compileCondition`. A condition-bound label may be used
// as a WHOLE bare WHEN condition (substituted in place of what would
// otherwise be a Sprint-10 bare Token ($ref) reference) or inside
// `debug(...)`; using it as an ordinary action/channel param value is a
// compile-time error (`_substituteBoundArgs`) — the hand-written
// equivalent would be invalid syntax there too.

/** Clone a plain AST/ValueNode/Condition node — JSON round-trip is safe
 *  here since every node in this grammar is plain, acyclic, JSON-
 *  serializable data (see the file-header AST-shape comment). Needed so
 *  a label referenced MULTIPLE times in one statement never shares one
 *  mutable AST subtree across occurrences. */
function _cloneAstNode(node) {
  return node == null ? node : JSON.parse(JSON.stringify(node));
}

/** True iff `tok` can legally end a `given_rhs := value` parse attempt —
 *  i.e. the point right after an `assign` item's RHS (a `;`, a trailing
 *  `#meta`, EOF, or the next WHEN/ALERT/DO keyword — WHEN only appears
 *  here because GIVEN itself is `when_part`'s own predecessor), OR a
 *  bare `,` — needed so `parseOneClauseItem` can tell "this WAS a
 *  complete `value`, now decide whether it's the old Sprint-6 `msg=...,`
 *  sugar or a plain comma-after-assignment typo" apart from "this value
 *  parse succeeded but the REAL expression continues past it" (e.g.
 *  `pnl@positions.total <= -1` — `parseValue` only ever consumes the bare
 *  NAME `pnl`, leaving `@` next, which is correctly NOT a terminator).
 *
 *  Sprint [newline-separator] — a token with `nlBefore` set is ALSO a
 *  terminator, unconditionally, checked FIRST: a bare newline always ends
 *  the current clause_item's RHS (same "newline is interchangeable with
 *  the expected separator" rule Part 1 applies everywhere else). Without
 *  this, `GIVEN x = 5\ny = 6` would fail to recognize `5` as a complete
 *  `value` (the next token `y` isn't otherwise a terminator), backtrack
 *  into the full condition grammar, and throw trying to consume `y` as a
 *  comparator. Also reused, unchanged, by the new `parseGivenRhs`
 *  arithmetic-expression attempt below (same "did the RHS end here"
 *  question, same answer).
 *
 *  Sprint [grammar-unify] — RPAREN is ALSO a terminator: `alert(...)`'s
 *  body reuses this SAME item-list grammar nested inside real parens
 *  (see `parseCall`'s `alert`-specific branch), so an assignment that
 *  happens to be the LAST item before the closing `)` (e.g. `alert(y =
 *  2)`, no trailing comma) must recognize `2` as a complete value right
 *  there, the same way EOF already does for a top-level, unparenthesized
 *  clause. RPAREN never legally followed a clause-item's RHS before this
 *  sprint (no clause was ever nested in parens), so this is additive —
 *  zero behavior change for every pre-existing statement shape. */
function _isClauseItemTerminator(tok) {
  if (tok.nlBefore) return true;
  if (tok.type === 'SEMI' || tok.type === 'EOF' || tok.type === 'HASH' || tok.type === 'COMMA' || tok.type === 'RPAREN') return true;
  return tok.type === 'NAME' && !tok.hasCall && (_wordIs(tok, 'when') || _wordIs(tok, 'alert') || _wordIs(tok, 'do'));
}

// ═══════════════════════════════════════════════════════════════════════
// Sprint [GIVEN-expr] — arithmetic/string-concat expressions, GIVEN's RHS
// ONLY (never reachable from WHEN's own condition grammar). Classifies a
// binding as an "operator expression" (vs. the pre-existing `value`/
// `condition` kinds) — see `parseGivenRhs`'s own header comment for the
// three-way backtracking order. This AST shape is internal scaffolding
// ONLY: it is never exposed in compiled JSON (the compiled `given[]`
// entry carries only the verbatim SOURCE TEXT, sliced by the caller,
// `parseOneClauseItem` — see `_registerBinding`'s `rhsText` param) — its
// only two jobs are (a) correctly delimiting how many tokens belong to
// this RHS, and (b) letting compile-time code (`_arithContainsDynamicRead`)
// detect/reject an inline `metric@scope`/col_ref read mixed into an
// operator expression (the backend's `expr_eval.py` is a flat-namespace
// evaluator with no dotted/`@` support — an inline dynamic read can never
// be evaluated there; it must be bound to its own label first).
//
// arith_expr  := arith_term (("+"|"-") arith_term)*
// arith_term  := arith_pow (("*"|"/"|"%") arith_pow)*
// arith_pow   := arith_unary ("**" arith_pow)?      (* right-assoc *)
// arith_unary := "-" arith_unary | arith_atom
// arith_atom  := NUMBER | STRING | "(" arith_expr ")"
//              | metric_ref_or_colref                (* reuses
//                  parseMetricOrColRef verbatim — rejected later at
//                  compile time if combined with any operator *)
//              | NAME                                (* a reference to
//                  an earlier GIVEN-bound label *)

/** `arith_atom` — see the section header above for the full production. */
function parseArithAtom(state, depth) {
  const tok = _peek(state);
  if (tok.type === 'NUMBER') { _advance(state); return { type: 'number', value: tok.value }; }
  if (tok.type === 'STRING') { _advance(state); return { type: 'string', value: tok.value }; }
  if (tok.type === 'LPAREN') {
    _checkDepth(depth + 1, tok);
    _advance(state);
    const inner = parseArithExpr(state, depth + 1);
    const close = _peek(state);
    if (close.type !== 'RPAREN') throw new _ParseError('expected ")"', close);
    _advance(state);
    return inner;
  }
  if (tok.type === 'NAME') {
    const next = state.tokens[state.pos + 1];
    const looksLikeRef = tok.hasCall || (next && (next.type === 'AT' || next.type === 'DOT'));
    if (looksLikeRef) {
      const ref = parseMetricOrColRef(state);
      return ref.kind === 'colref'
        ? { type: 'colref_atom', segments: ref.segments }
        : { type: 'metric_atom', metric: ref.metric, scope: ref.scope };
    }
    if (tok.reserved && tok.value.toLowerCase() !== 'true' && tok.value.toLowerCase() !== 'false') {
      throw new _ParseError(`"${tok.value}" is a reserved word and cannot be used as a value here`, tok);
    }
    _advance(state);
    return { type: 'name_atom', value: tok.value };
  }
  throw new _ParseError('expected a number, string, name, metric@scope reference, or "(...)"', tok);
}

/** `arith_unary := "-" arith_unary | arith_atom` */
function parseArithUnary(state, depth) {
  const tok = _peek(state);
  if (tok.type === 'MINUS') {
    _checkDepth(depth + 1, tok);
    _advance(state);
    return { type: 'neg', node: parseArithUnary(state, depth + 1) };
  }
  return parseArithAtom(state, depth);
}

/** `arith_pow := arith_unary ("**" arith_pow)?` — right-associative. */
function parseArithPow(state, depth) {
  const base = parseArithUnary(state, depth);
  if (_peek(state).type === 'STARSTAR') {
    _advance(state);
    const exp = parseArithPow(state, depth);
    return { type: 'pow', base, exp };
  }
  return base;
}

/** `arith_term := arith_pow (("*"|"/"|"%") arith_pow)*` — left-to-right fold. */
function parseArithTerm(state, depth) {
  let node = parseArithPow(state, depth);
  while (true) {
    const tok = _peek(state);
    if (tok.type !== 'STAR' && tok.type !== 'SLASH' && tok.type !== 'PERCENT') break;
    _advance(state);
    const op = tok.type === 'STAR' ? '*' : tok.type === 'SLASH' ? '/' : '%';
    node = { type: 'arith', op, left: node, right: parseArithPow(state, depth) };
  }
  return node;
}

/** `arith_expr := arith_term (("+"|"-") arith_term)*` — left-to-right fold.
 *  Top-level entry point for Sprint [GIVEN-expr]'s arithmetic production. */
function parseArithExpr(state, depth = 0) {
  let node = parseArithTerm(state, depth);
  while (true) {
    const tok = _peek(state);
    if (tok.type !== 'PLUS' && tok.type !== 'MINUS') break;
    _advance(state);
    const op = tok.type === 'PLUS' ? '+' : '-';
    node = { type: 'arith', op, left: node, right: parseArithTerm(state, depth) };
  }
  return node;
}

/** True iff the ROOT of a parsed arith tree is a composite (an operator
 *  was consumed SOMEWHERE at or above this node) — an atom that passed
 *  through every level unchanged (no operator token matched anywhere)
 *  keeps its own atom type, so checking just the root is sufficient; see
 *  `parseGivenRhs`'s classification use. */
function _arithHasOperator(node) {
  return !!node && (node.type === 'arith' || node.type === 'pow' || node.type === 'neg');
}

/** `given_rhs := value | arith_expr | or_expr` — tries the narrower
 *  `value` production FIRST (the PRIMITIVE binding kind, UNCHANGED from
 *  pre-Sprint-[GIVEN-expr]), then the NEW arithmetic/string-concat
 *  production (the "operator expression" kind), backtracking to the full
 *  `or_expr` condition grammar (the CONDITION/dict binding kind,
 *  UNCHANGED) when neither of the first two apply. Order matters and is
 *  deliberate:
 *    - A bare `metric@scope`/col_ref "pure alias" with NO operator (e.g.
 *      `p = pnl@positions.total`) fails step 1 (NAME then "@", not a
 *      terminator), and ALSO fails step 2 — `parseArithAtom` happily
 *      consumes the whole metric_ref as ONE atom, but with no operator
 *      token following it, `_arithHasOperator` is false, so step 2's own
 *      `if` never returns — falling through UNTOUCHED to step 3, the
 *      exact same `parseOrExpr` call pre-Sprint-[GIVEN-expr] always made.
 *      **Zero behavior change for this case**, by construction, not by
 *      special-casing it.
 *    - `x = pnl@positions.total <= -5` — step 1 fails; step 2's
 *      `parseArithAtom` consumes the metric_ref, finds no operator
 *      matches `<=` at any arith level, returns the bare atom unchanged
 *      (`_arithHasOperator` false) — falls through to step 3, which
 *      parses the full comparator leaf exactly as before.
 *    - A bare NAME referencing an EARLIER-bound label with NO operator
 *      (e.g. `q = p`) succeeds at step 1 as a plain `value` — reclassified
 *      inline to `'expr'` kind (see the dedicated check below): its value
 *      is a live re-export of another binding, not its own literal text,
 *      so it must resolve server-side like any other operator expression,
 *      not as a literal `NAME` value. */
function parseGivenRhs(state) {
  const save = state.pos;
  try {
    const v = parseValue(state);
    if (_isClauseItemTerminator(_peek(state))) {
      // Sprint [GIVEN-expr] — `node: v` here (the ORDINARY `{type:'name',
      // value}` ValueNode `parseValue` already produced), deliberately
      // NOT wrapped into the internal `name_atom` arith-AST shape: this
      // `rhs.node` becomes `ast.alertMsg`/a plain action-param arg value
      // in several places (the Sprint-6 `msg="...", <calls>` sugar,
      // `_resolveMessageValue`, `_resolveGivenExprRefOrNull`), every one
      // of which pattern-matches on `valueNode.type === 'name'` — a
      // `name_atom` shape would silently fail every one of those checks.
      // `_collectArithNameRefs`/`_arithContainsDynamicRead` are taught to
      // treat a plain `type: 'name'` node identically to `name_atom` for
      // this reason (see those functions' own comments).
      if (v.type === 'name' && state.bindings && state.bindings.has(String(v.value).toLowerCase())) {
        return { kind: 'expr', node: v };
      }
      return { kind: 'value', node: v };
    }
  } catch { /* fall through */ }
  state.pos = save;
  try {
    const node = parseArithExpr(state, 0);
    if (_arithHasOperator(node) && _isClauseItemTerminator(_peek(state))) {
      return { kind: 'expr', node };
    }
  } catch { /* fall through to the condition grammar below */ }
  state.pos = save;
  const cond = parseOrExpr(state, 0);
  return { kind: 'condition', node: cond };
}

/** Register a new label binding into `state.bindings` (read live by
 *  `parseLeaf`'s bare-ref branch, and snapshotted per-call by
 *  `parseCall`) — enforces the "no shadowing/rebinding" rule (ONE write
 *  per label per statement, never a silent override) and rejects a label
 *  spelled as a real grammar keyword. Collision against a real catalog
 *  token/scope-family-alias/exposed-column name is a COMPILE-time check
 *  (`_checkLabelCollisions`) — the catalog isn't available yet at parse
 *  time, same "parse permissively, validate at compile time" split this
 *  file always uses. */
// Sprint [grammar-unify] — 'alert'/'given'/'help' are removed from
// `_RESERVED` (so they can be used as ordinary call names), but a label
// spelled one of these three is STILL rejected here, same as `debug`/
// `result` already are — all five are "contextual, not reserved" words
// with a real meaning in this grammar that a label sharing the name
// would silently shadow.
const _LABEL_RESERVED_WORDS = new Set(['debug', 'result', 'alert', 'given', 'help']);

function _registerBinding(state, labelTok, rhsKind, rhsNode, rhsText) {
  const lower = labelTok.value.toLowerCase();
  if (_RESERVED.has(lower) || _LABEL_RESERVED_WORDS.has(lower)) {
    throw new _ParseError(`'${labelTok.value}' is a reserved word and cannot be used as a label`, labelTok);
  }
  if (!state.bindings) state.bindings = new Map();
  if (!state.allBindings) state.allBindings = [];
  if (state.bindings.has(lower)) {
    throw new _ParseError(`label '${labelTok.value}' is already bound in this statement — rebinding is not allowed (no shadowing)`, labelTok);
  }
  // Sprint [GIVEN-expr] — `rhsText` is the binding's RHS, sliced VERBATIM
  // from the original source text (captured by the one caller,
  // `parseOneClauseItem`, regardless of which `parseGivenRhs` branch won)
  // — needed for an `'expr'`-kind binding's own `agent.given[].expr`
  // entry, and for a referenced pure-alias's OWN injected entry (see
  // `_buildGivenEntries`). Harmless/unused for `'value'`/`'condition'`
  // kind bindings that never end up referenced by an operator expression.
  const binding = { label: labelTok.value, rhsKind, rhsNode, rhsText };
  state.bindings.set(lower, binding);
  state.allBindings.push(binding);
}

/** One item of a GIVEN/ALERT/DO item list — see the section header above
 *  for the full `clause_item`/`assignment`/`msg_default` grammar.
 *  Disambiguated from an ordinary new-style `assign` item purely by what
 *  follows the parsed RHS: a literal "msg"-spelled assignment whose RHS
 *  parsed as a plain `value` AND is immediately followed by `,` (not a
 *  terminator) is the OLD Sprint-6 sugar — consume the comma and keep
 *  parsing a call_list on the SAME item. Any OTHER label, or "msg" not
 *  immediately followed by `,`, is a plain `assign` item instead.
 *  `allowMsgDefault` is only ever `true` for ALERT's clause, and the old
 *  sugar is (as it always was) a leading-position-only concept — callers
 *  only pass `true` for the FIRST item in a clause. */
function parseOneClauseItem(state, { allowMsgDefault }) {
  const tok = _peek(state);
  // Sprint 12 — log_tag=/min_level=/render=/where=/gate=/match= are
  // event_stmt-ONLY reserved kwargs; hitting one here means the operator
  // combined them with WHEN/ALERT/DO (this function is only ever reached
  // from agent_stmt's own GIVEN/ALERT/DO, never from a bare event_stmt —
  // see `parseEventStmt`) — same clear, specific rejection the pre-
  // Sprint-G `parseAlertClauseBody` always gave, ported over unchanged.
  if (tok.type === 'NAME' && !tok.hasCall && _EVENT_META_KEYS.has(tok.value.toLowerCase())) {
    const evNext = state.tokens[state.pos + 1];
    if (evNext && evNext.type === 'EQ') {
      throw new _ParseError(
        `'${tok.value.toLowerCase()}=' is only valid on a bare ALERT-only statement, not combined with WHEN/DO`,
        tok,
      );
    }
  }
  if (tok.type === 'NAME' && !tok.hasCall) {
    const next = state.tokens[state.pos + 1];
    if (next && next.type === 'EQ') {
      const labelTok = tok;
      _advance(state); // label
      _advance(state); // "="
      // Sprint [GIVEN-expr] — capture the RHS's own verbatim source span,
      // regardless of which `parseGivenRhs` branch wins, for
      // `_registerBinding`'s `rhsText` param (see that function's own
      // comment). `rhsStartTok` is the FIRST token `parseGivenRhs` will
      // consume; `state.tokens[state.pos - 1]` right after it returns is
      // the LAST token it actually consumed.
      const rhsStartTok = _peek(state);
      const rhs = parseGivenRhs(state);
      const rhsEndTok = state.tokens[state.pos - 1];
      const rhsText = state.text ? state.text.slice(rhsStartTok.start, rhsEndTok.end) : null;
      const after = _peek(state);
      // Sprint [newline-separator] — the Sprint-6 `msg="...", <call_list>`
      // sugar's own `,` ALSO accepts a bare newline instead, same rule as
      // every other separator in this clause-item grammar (a literal ","
      // still always works, unchanged — this only widens what else is
      // ALSO accepted in its place).
      // Sprint [GIVEN-expr] — `rhs.kind === 'expr'` ALSO activates this
      // sugar now, not just `'value'` — `ALERT msg = msg_txt, telegram`
      // (where `msg_txt` is an operator-expression GIVEN binding) is
      // exactly the motivating, explicitly-requested use case; excluding
      // it here would silently fall through to the generic "a comma only
      // joins calls, never an assignment" rejection below instead.
      const msgDefaultSugarActive = allowMsgDefault && labelTok.value.toLowerCase() === 'msg'
        && (rhs.kind === 'value' || rhs.kind === 'expr')
        && (after.type === 'COMMA' || (after.nlBefore && _looksLikeNewListItemStart(after)));
      if (msgDefaultSugarActive) {
        if (after.type === 'COMMA') _advance(state); // consume "," — a bare newline has no token to consume
        const calls = parseCallList(state, { allowNewline: true });
        return { type: 'msgDefaultCalls', msgNode: rhs.node, calls };
      }
      // Sprint [comma-separator] — a trailing "," here (not the msg=
      // sugar above, which already consumed its own comma and returned)
      // is left UNCONSUMED, exactly like every other terminator (";",
      // newline, EOF, next keyword) — `_isClauseItemTerminator` already
      // treats COMMA as ending the RHS parse, so by construction a comma
      // reaching this point always sits at a genuine item boundary.
      // `parseItemListClause`'s own loop now accepts a bare COMMA as a
      // synonym for ";" (same unconditional-consume behavior), so this
      // used to be a hard parse error ("a comma only joins calls, never
      // an assignment") purely because that outer acceptance didn't
      // exist yet — comma is otherwise the separator for everything else
      // in this grammar (call args, enum/list literals, multi-channel
      // ALERT), and `GIVEN a1=1, a2=1` is now accepted the same way
      // `GIVEN a1=1; a2=1` always was.
      _registerBinding(state, labelTok, rhs.kind, rhs.node, rhsText);
      return { type: 'assign', label: labelTok.value };
    }
  }
  const calls = parseCallList(state, { allowNewline: true });
  return { type: 'calls', calls };
}

/** `clause_body := "nop" | clause_item (";" clause_item)*` — the unified
 *  item-list grammar for GIVEN/ALERT/DO. Subsumes the pre-Sprint-G
 *  `alert_clause`/plain call_list productions byte-for-byte when the
 *  clause contains no `;` and no label assignment at all (every pre-
 *  existing agent decompiles/recompiles through this function
 *  unchanged) — see `parseOneClauseItem`'s own header comment for the
 *  disambiguation rule. `allowNop` is `false` only for GIVEN (a bare
 *  "nop" there is meaningless — GIVEN is omitted entirely instead). A
 *  bare "nop" cannot be combined with any other item (same "nop excludes
 *  everything else" rule the pre-Sprint-G grammar always had). */
function parseItemListClause(state, { allowMsgDefault = false, allowNop = true } = {}) {
  const tok = _peek(state);
  if (allowNop && tok.type === 'NAME' && _wordIs(tok, 'nop')) {
    if (tok.hasCall) throw new _ParseError('"nop" takes no arguments — nop() is a parse error', tok);
    _advance(state);
    return { items: [{ type: 'nop' }] };
  }
  const items = [parseOneClauseItem(state, { allowMsgDefault })];
  // Sprint [newline-separator] — a bare newline is also accepted in place
  // of the literal `;` between items — see the shared rationale on
  // `_looksLikeNewListItemStart`. A literal `;` still always demands a
  // following item (unchanged below); only its ABSENCE, replaced by a
  // bare newline, is newly accepted.
  //
  // Sprint [comma-separator] — a literal `,` is ALSO accepted here, same
  // unconditional-consume treatment as `;` (no lookahead gate needed: by
  // the time a comma reaches this loop, `parseOneClauseItem`/the RHS
  // parser already guarantee it sits at a genuine item boundary, and if
  // nothing legal follows, the recursive `parseOneClauseItem` call below
  // fails with its own clear error, exactly as `;` already does today).
  // Comma is the separator for everything else in this grammar (call
  // args, enum/list literals, multi-channel ALERT) — this closes the one
  // place it wasn't.
  while (true) {
    if (_peek(state).type === 'SEMI' || _peek(state).type === 'COMMA') {
      _advance(state);
      items.push(parseOneClauseItem(state, { allowMsgDefault: false }));
      continue;
    }
    if (_peek(state).nlBefore && _looksLikeNewListItemStart(_peek(state))) {
      items.push(parseOneClauseItem(state, { allowMsgDefault: false }));
      continue;
    }
    break;
  }
  if (items.length > 1 && items.some((it) => it.type === 'nop')) {
    throw new _ParseError('"nop" cannot be combined with other items', tok);
  }
  return { items };
}

/** Flatten a parsed preamble/DO item list down to `{ msgDefault, calls }`
 *  so `compileAlertClause`/`compileDoClause`/`compilePreambleClause` never
 *  need to know about assignment items at all. Sprint [grammar-unify] —
 *  `debug(...)`/`help(...)`/`alert(...)` are NO LONGER hoisted out into a
 *  separate array: every call (including these) rides along in `calls`,
 *  in textual order, and is dispatched by name via `_compileSharedCall`,
 *  the ONE per-call compiler shared by the preamble and DO positions (see
 *  that function's own header comment). `calls` is `'nop'` only when the
 *  WHOLE item list was the bare `nop` sentinel, OR (pre-existing) the
 *  item list turned out to be made ENTIRELY of `assign` items with no
 *  real calls at all (e.g. `msg = "y"` with no channel list at all — this
 *  is semantically IDENTICAL to `nop`: zero events/actions). Normalizing
 *  both to the SAME `'nop'` sentinel means every downstream consumer
 *  treats them identically, with no separate "empty array" case to
 *  special-case anywhere else. `assign` items otherwise contribute
 *  nothing here — they already did their only job (populating
 *  `state.bindings`) as a side effect of parsing.
 *  @returns {{ msgDefault: any, calls: 'nop' | any[] }} */
function _flattenClauseItems(items) {
  if (items.length === 1 && items[0].type === 'nop') return { msgDefault: null, calls: 'nop' };
  let msgDefault = null;
  /** @type {any[]} */
  const calls = [];
  for (const it of items) {
    if (it.type === 'assign') continue;
    if (it.type === 'msgDefaultCalls') msgDefault = it.msgNode;
    for (const c of it.calls || []) calls.push(c);
  }
  return { msgDefault, calls: calls.length ? calls : 'nop' };
}

/** `meta_value := STRING | BARE_WORD` — both already resolved to one token
 *  each at lex time (`lexAgentStatement`'s dedicated "#" branch); this is a
 *  thin AST-shaping accessor, parallel to `parseValue` for the main
 *  grammar's `value` production. */
function parseMetaValue(state) {
  const tok = _peek(state);
  if (tok.type === 'STRING') { _advance(state); return { type: 'string', value: tok.value }; }
  if (tok.type === 'BAREWORD') { _advance(state); return { type: 'bareword', value: tok.value }; }
  throw new _ParseError('expected a meta value (a quoted string or a bare word)', tok);
}

/** `meta_item := "#" NAME "=" meta_value ("," meta_value)*` — GENERIC,
 *  reusable helper: parses ZERO OR MORE trailing meta_items starting at
 *  `state`'s current position, stopping the moment the next token isn't
 *  "#". Sprint 7. Trailing-only by construction — callers invoke this
 *  AFTER their own clause-parsing loop completes (`parseAgentStmt`'s tail,
 *  `parseOrderStmt`'s call_list), never interspersed partway through.
 *  Written with no agent_stmt-specific assumptions so a future
 *  bracket_stmt can call it unchanged. An absent meta clause (next token
 *  isn't "#") is NOT an error — it simply returns `[]`; semantic checks
 *  (unknown key, wrong arity, "not allowed on a bare order") all happen
 *  later, at compile time, via `compileMetaClause`. */
export function parseMetaClause(state) {
  const items = [];
  while (_peek(state).type === 'HASH') {
    const hashTok = _advance(state);
    const keyTok = _peek(state);
    if (keyTok.type !== 'NAME') throw new _ParseError('expected a meta key name after "#"', keyTok);
    _advance(state);
    const eq = _peek(state);
    if (eq.type !== 'EQ') throw new _ParseError(`expected "=" after meta key '${keyTok.value}'`, eq);
    _advance(state);
    const values = [parseMetaValue(state)];
    while (_peek(state).type === 'COMMA') {
      _advance(state);
      values.push(parseMetaValue(state));
    }
    items.push({ key: keyTok.value, values, start: hashTok.start });
  }
  return items;
}

/** `agent_stmt := preamble ["WHEN" condition "DO" calls]`
 *  `preamble := statement_call*`   -- the implicit, self-delimiting
 *                                     "given" section (Sprint
 *                                     [grammar-unify] — see `_RESERVED`'s
 *                                     own header comment for why `GIVEN`
 *                                     carries nothing to mark any more)
 *
 *  **Preamble** — bounded purely by POSITION: everything before `WHEN`
 *  (or, when `WHEN` never appears at all, the ENTIRE remaining input) is
 *  the preamble — never introduced by a keyword of its own. It accepts
 *  the FULL shared statement-call vocabulary (label assignment,
 *  `debug(...)`, `help(...)`, `alert(...)`, `order(...)`/`place_order(...)`,
 *  `bracket(...)`, or any other real action call) via the exact SAME
 *  item-list grammar `DO` uses (`parseItemListClause`) — `_compileSharedCall`
 *  is the ONE per-call compiler shared by both positions, so this parser
 *  never needs a narrower accept-list for the preamble than for `DO`. A
 *  bare `nop` preamble item is meaningless (there's nothing to search
 *  "nop" as a standalone unconditional statement for) — `allowNop:
 *  false`, same restriction the old `GIVEN` clause always had.
 *
 *  **`WHEN ... DO ...`** — stays a single, paired, optional conditional
 *  wrapper: `ALERT` is no longer a clause keyword at all (`alert(...)` is
 *  just an ordinary call inside `DO`'s own call list now, exactly like
 *  `order(...)`/`help(...)` — see `_compileSharedCall`), so the old
 *  "ALERT/DO, either order, each independently optional" tail loop
 *  collapses into a single, optional `DO` clause after `WHEN`. Validity
 *  (checked at COMPILE time, same "parse permissively, validate later"
 *  split this file always uses — see `compileAgentStmt`): `WHEN` present
 *  with no `DO` is now an error (nothing to gate), and `DO` present with
 *  no `WHEN` is ALSO an error (redundant with just writing the call in
 *  the preamble, which gets the correct once-per-activation execution
 *  semantics for a side-effecting call — see the plan's item 0). A
 *  preamble with NEITHER `WHEN` nor `DO` at all is a complete, valid,
 *  standalone statement on its own (required by this plan — never a
 *  fragment awaiting a conditional tail). `sawWhen`/`sawDo` are threaded
 *  through verbatim for that compile-time check. */
export function parseAgentStmt(state) {
  let preambleCalls = [];
  const firstTok = _peek(state);
  if (!_wordIs(firstTok, 'when') && !_wordIs(firstTok, 'do')) {
    const { items } = parseItemListClause(state, { allowMsgDefault: false, allowNop: false });
    const flat = _flattenClauseItems(items);
    preambleCalls = flat.calls === 'nop' ? [] : flat.calls;
  }
  // WHEN itself is optional — omitting it means the same thing as writing
  // it as `always` explicitly (both produce the identical `{type:
  // 'always'}` AST node). WHEN only ever READS labels bound in the
  // preamble above — it never writes one itself (see `parseLeaf`'s
  // bare-ref branch).
  let condition = { type: 'always' };
  let sawWhen = false;
  if (_wordIs(_peek(state), 'when')) {
    _advance(state);
    condition = parseCondition(state, 0);
    sawWhen = true;
  }
  /** @type {'nop' | any[]} */
  let doCalls = 'nop';
  let sawDo = false;
  if (_wordIs(_peek(state), 'do')) {
    _advance(state);
    const { items } = parseItemListClause(state, { allowMsgDefault: false });
    doCalls = _flattenClauseItems(items).calls;
    sawDo = true;
  }
  // meta_item* — trailing-only, parsed AFTER the tail above completes.
  // See `parseMetaClause`'s own docstring for why no pre-pass is needed.
  const meta = parseMetaClause(state);
  // `bindings` is the ORDERED list of every label bound anywhere in this
  // statement (preamble or DO), for `compileAgentStmt`'s collision check
  // against real catalog names.
  return { kind: 'agent', preambleCalls, condition, doCalls, sawWhen, sawDo, meta, bindings: state.allBindings || [] };
}

/** `match_value := "[" object_literal ("," object_literal)* "]"` — Sprint
 *  18 follow-up (`ALERT match=[{tag=orders, min_level=WARNING}, {...}]`).
 *  Parsed DIRECTLY against the main `state` token stream — UNLIKE
 *  `tp_scales_json_value` (`_parseTpScalesJsonArg`), which needs its own
 *  raw-substring mini-lexer because it sits INSIDE a call's `(...)`
 *  argument list, where top-level comma-splitting (`_splitTopLevelArgCommas`)
 *  would otherwise confuse a nested object's commas with argument-
 *  separating commas. `match=` sits in the event_meta KEYWORD position of
 *  the main statement grammar, not inside any call's parens, so the main
 *  lexer's already-lexed LBRACKET/LBRACE/COMMA/EQ/NAME/STRING/NUMBER
 *  tokens are sufficient with no special-casing — this reuses
 *  `parseObjectLiteral` (the SAME function `where=` already calls
 *  directly against `state`) per list entry, mirroring
 *  `_parseTpScalesJsonArg`'s own list-of-objects structure one level up,
 *  just without that function's dedicated mini-lexer. */
/** `match_value := "[" object_literal ("," object_literal)* "]"` —
 *  Sprint [newline-separator]: a bare newline (no literal `,`) is also
 *  accepted between `{...}` entries — each entry always starts with a
 *  literal `{` (LBRACE), the one start-token this list-of-X production
 *  needs (distinct from `parseListLiteral`'s own wider value start-set
 *  and `parseItemListClause`/`parseCallList`'s NAME-only start-set). */
function parseMatchValue(state) {
  const open = _peek(state);
  if (open.type !== 'LBRACKET') {
    throw new _ParseError('match= requires a list of {...} entries, e.g. [{tag=orders, min_level=WARNING}]', open);
  }
  _advance(state);
  const items = [];
  if (_peek(state).type !== 'RBRACKET') {
    items.push(parseObjectLiteral(state));
    while (true) {
      if (_peek(state).type === 'COMMA') {
        _advance(state);
        if (_peek(state).type === 'RBRACKET') throw new _ParseError('trailing comma in match= list', _peek(state));
        items.push(parseObjectLiteral(state));
        continue;
      }
      if (_peek(state).nlBefore && _peek(state).type === 'LBRACE') {
        items.push(parseObjectLiteral(state));
        continue;
      }
      break;
    }
  }
  const close = _peek(state);
  if (close.type !== 'RBRACKET') throw new _ParseError('expected "]"', close);
  _advance(state);
  return { type: 'list', value: items };
}

/** `event_stmt := "ALERT" event_meta call_list meta_item*` — Sprint 12,
 *  notification (`kind='event'`) agents. Dispatched from `parseStatement`'s
 *  own lookahead rule (see that function's dispatch comment) specifically
 *  when a bare leading `ALERT` is immediately followed by one of the six
 *  reserved event-meta kwargs (`log_tag=`/`min_level=`/`render=`/`where=`/
 *  `gate=`/`match=`, see `_EVENT_META_KEYS`) — never reached for an ordinary
 *  bare-ALERT agent_stmt (e.g. `ALERT telegram DO nop`), which keeps going
 *  through `parseAgentStmt`/`parseAlertClauseBody` exactly as before.
 *
 *  `event_meta := event_kw*` — each of the six keys at most once, ANY
 *  ORDER (a plain loop, not six fixed positions); a duplicate key is a
 *  PARSE-time error (not deferred to compile time, per the operator's
 *  explicit instruction — unlike almost everything else in this grammar).
 *  `where=` is the ONE key whose value is an `object_literal` (reusing
 *  `parseObjectLiteral` directly against THIS function's own `state` — see
 *  that function's own generalization comment); `match=` (Sprint 18
 *  follow-up) is a `match_value` (list of `object_literal`, see
 *  `parseMatchValue` above) — mutually exclusive with
 *  `log_tag=`/`min_level=`/`where=` (checked at COMPILE time,
 *  `compileEventStmt`, not here — this function parses permissively);
 *  the other three use the ordinary `value` production.
 *
 *  After `event_meta`, two guards run before the channel call_list:
 *    1. WHEN/DO immediately following `event_meta` (e.g.
 *       `ALERT log_tag=error, render=x DO nop`) is the OTHER half of the
 *       "combined with WHEN/DO" rejection (see `parseAlertClauseBody`'s own
 *       half, which covers `WHEN ... ALERT log_tag=...`) — a clear,
 *       specific message instead of `parseCall`'s generic reserved-word
 *       error (DO/WHEN are reserved words `parseCall` would reject anyway).
 *    2. A contextual leading `msg=` default, recognized EXACTLY the same
 *       way `parseAlertClauseBody` recognizes it (Sprint 6) — consumed
 *       HERE (not left for `parseCallList` to choke on) so
 *       `compileEventStmt` can raise a clear, specific error ("msg=
 *       doesn't apply to event agents") instead of a confusing parse
 *       failure. */
export function parseEventStmt(state) {
  const alertTok = _advance(state); // consume "ALERT"
  const eventMeta = {};
  const seenKeys = new Set();
  while (true) {
    const tok = _peek(state);
    if (tok.type !== 'NAME' || tok.hasCall) break;
    const lower = tok.value.toLowerCase();
    if (!_EVENT_META_KEYS.has(lower)) break;
    const next = state.tokens[state.pos + 1];
    if (!next || next.type !== 'EQ') break;
    if (seenKeys.has(lower)) throw new _ParseError(`duplicate event-meta key '${lower}='`, tok);
    seenKeys.add(lower);
    _advance(state); // key
    _advance(state); // "="
    eventMeta[lower] = lower === 'where' ? parseObjectLiteral(state)
      : lower === 'match' ? parseMatchValue(state)
      : parseValue(state);
    const comma = _peek(state);
    if (comma.type !== 'COMMA') throw new _ParseError(`expected "," after ${lower}=...`, comma);
    _advance(state);
  }
  const after = _peek(state);
  if (after.type === 'NAME' && !after.hasCall && (_wordIs(after, 'when') || _wordIs(after, 'do'))) {
    throw new _ParseError(
      'log_tag=/min_level=/render=/where=/gate=/match= are only valid on a bare ALERT-only statement, not combined with WHEN/DO',
      after,
    );
  }
  let msgNode = null;
  const msgTok = _peek(state);
  if (msgTok.type === 'NAME' && !msgTok.hasCall && msgTok.value.toLowerCase() === 'msg') {
    const msgNext = state.tokens[state.pos + 1];
    if (msgNext && msgNext.type === 'EQ') {
      _advance(state); // consume "msg"
      _advance(state); // consume "="
      msgNode = parseValue(state);
      const comma = _peek(state);
      if (comma.type !== 'COMMA') throw new _ParseError('expected "," after msg="..." default, followed by a channel list', comma);
      _advance(state);
    }
  }
  // Sprint 18 — `parseClauseBody` (not the bare `parseCallList`) so a
  // kind='event' agent's channel list can ALSO be written as bare `nop`
  // (an empty `events: []` — the real, confirmed-intentional shape of
  // the seeded `cycle-breach-alert` agent, which supplies channels
  // dynamically at render time). `calls` is therefore `'nop' | any[]`,
  // same union `parseAlertClauseBody`'s own ordinary-ALERT callers
  // already handle — see `compileEventStmt`'s matching `'nop'` guards.
  const calls = parseClauseBody(state);
  // Second half of the "combined with WHEN/DO" guard — `after` (above)
  // only catches DO/WHEN appearing IMMEDIATELY after event_meta, before
  // any channel; a trailing `DO ...` AFTER the channel call_list (e.g.
  // `ALERT log_tag=error, render=error, telegram DO nop`) must be caught
  // here instead, or it falls through as unconsumed trailing input and
  // surfaces parseStatement's generic "unexpected trailing input" error
  // rather than this clear, specific one.
  const afterCalls = _peek(state);
  if (afterCalls.type === 'NAME' && !afterCalls.hasCall && _wordIs(afterCalls, 'do')) {
    throw new _ParseError(
      'log_tag=/min_level=/render=/where=/gate=/match= are only valid on a bare ALERT-only statement, not combined with WHEN/DO',
      afterCalls,
    );
  }
  const meta = parseMetaClause(state);
  return { kind: 'event', eventMeta, msgNode, calls, meta, start: alertTok.start };
}

/** `order_stmt := call ("," call)* meta_item*` — restricted to
 *  order/place_order calls. A non-empty `meta` here is syntactically fine
 *  (parsed, not rejected) but is a COMPILE-time error — see
 *  `compileOrderStmt` — meta tags only apply to agents/brackets. */
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
  const meta = parseMetaClause(state);
  return { kind: 'order', calls, meta };
}

/** `bracket_stmt := call meta_item*` — Sprint 8. A SINGLE `bracket(...)`
 *  call (never a call_list — order_stmt's 2+-call basket form has no
 *  bracket equivalent), restricted to standalone use, EXACTLY mirroring
 *  `parseOrderStmt`'s own "only order(...) can be used standalone"
 *  rule/error-message style above, just naming "bracket" instead of
 *  "order". A trailing comma (an attempt at a bracket call_list) is
 *  rejected with its own, analogous message rather than silently being
 *  swallowed by `parseCallList`. */
/** `cancel_all_orders_stmt := call meta_item*` — Sprint [cancel-all-cli]
 *  (2026-10). A SINGLE `cancel_all_orders(...)` call, restricted to
 *  standalone use, EXACTLY mirroring `parseBracketStmt`'s own "only
 *  bracket(...) can be used standalone" rule/error-message style above,
 *  just naming "cancel_all_orders" instead of "bracket". `meta` is parsed
 *  (never rejected here) but is a compile-time error — see
 *  `compileCancelAllOrdersStmt` — a bare fleet-wide cancel has no
 *  agent/bracket to attach a name/description/tag/group to, same
 *  convention `parseOrderStmt`/`compileOrderStmt` already established for
 *  a bare `order(...)` statement. */
export function parseCancelAllOrdersStmt(state) {
  const call = parseCall(state);
  if (call.name.toLowerCase() !== 'cancel_all_orders') {
    throw new _ParseError(
      'only cancel_all_orders(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
      { start: call.start },
    );
  }
  if (_peek(state).type === 'COMMA') {
    throw new _ParseError(
      'only a single cancel_all_orders(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
      _peek(state),
    );
  }
  const meta = parseMetaClause(state);
  return { kind: 'cancel_all_orders', call, meta };
}

/** `help_stmt := call meta_item*` — item 4. A SINGLE `help(<name>)` call,
 *  restricted to standalone use, EXACTLY mirroring
 *  `parseCancelAllOrdersStmt`'s own "only X(...) can be used standalone"
 *  rule/error-message style above, naming "help" instead of
 *  "cancel_all_orders". Read-only catalog lookup — no live/paper mode
 *  gate, safe in any mode (unlike `cancel_all_orders`, which is
 *  live-only). `meta` is parsed (never rejected here) but is a
 *  compile-time error — see `compileHelpStmt` — a bare lookup has no
 *  agent/bracket to attach a name/description/tag/group to, same
 *  convention every other bare-statement compiler already established. */
export function parseHelpStmt(state) {
  const call = parseCall(state);
  if (call.name.toLowerCase() !== 'help') {
    throw new _ParseError(
      'only help(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
      { start: call.start },
    );
  }
  if (_peek(state).type === 'COMMA') {
    throw new _ParseError(
      'only a single help(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
      _peek(state),
    );
  }
  const meta = parseMetaClause(state);
  return { kind: 'help', call, meta };
}

export function parseBracketStmt(state) {
  const call = parseCall(state);
  if (call.name.toLowerCase() !== 'bracket') {
    throw new _ParseError(
      'only bracket(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
      { start: call.start },
    );
  }
  if (_peek(state).type === 'COMMA') {
    throw new _ParseError(
      'only a single bracket(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
      _peek(state),
    );
  }
  const meta = parseMetaClause(state);
  return { kind: 'bracket', call, meta };
}

/** `token_stmt := call token_body meta_item*` — Sprint 10, extended
 *  2026-10-12 (operator: "build [condition-kind Token support] now") —
 *  a standalone `token(kind=...)` HEAD call, restricted to standalone
 *  use, EXACTLY mirroring `parseBracketStmt`'s own "only bracket(...)
 *  can be used standalone" rule/error-message style above, naming
 *  "token" instead.
 *
 *  `token_body` depends on the HEAD's own `kind=` argument, peeked here
 *  (via the same `_coerceValueForSpec`/`_TOKEN_KIND_SPEC` helper
 *  `compileTokenStmt` uses to validate it properly) BEFORE deciding how
 *  to parse the rest of the statement:
 *    - `kind=notify` (or an unrecognized/omitted kind — unchanged
 *      default) → `call_list` (`telegram, email, log`, reusing
 *      `parseCallList` unchanged — the SAME production ALERT's own
 *      call_list uses). At least one body call is required
 *      (`parseCallList` never returns an empty array) — a notify Token
 *      with zero channels has no useful body.
 *    - `kind=condition` → a full boolean `condition` expression (reuses
 *      `parseCondition` UNCHANGED — the exact same production `WHEN`
 *      uses), e.g. `token(kind=condition) pnl@positions.total <= -50000
 *      #name=...`. A condition-kind Token's body IS a condition tree
 *      (the same shape a stored Agent row's own `conditions` column
 *      already holds for these rows — see `compileTokenStmt`), so
 *      reusing the WHEN-clause grammar verbatim is correct, not just
 *      convenient.
 *  An invalid `kind=` value (or a missing one) is NOT re-validated
 *  here — this peek is parse-only and defaults to the notify/call_list
 *  shape on any ambiguity; `compileTokenStmt` is the single source of
 *  truth for the real "kind must be notify|condition" error. */
export function parseTokenStmt(state) {
  const head = parseCall(state);
  if (head.name.toLowerCase() !== 'token') {
    throw new _ParseError(
      'only token(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
      { start: head.start },
    );
  }
  const kindArg = (head.args || []).find((a) => a.key && a.key.toLowerCase() === 'kind');
  const kindPeek = kindArg ? _coerceValueForSpec(kindArg.value, _TOKEN_KIND_SPEC, 'kind', 'token') : null;
  if (kindPeek && !kindPeek.error && kindPeek.value === 'condition') {
    const condition = parseCondition(state);
    const meta = parseMetaClause(state);
    return { kind: 'token', head, condition, meta };
  }
  const bodyCalls = parseCallList(state);
  const meta = parseMetaClause(state);
  return { kind: 'token', head, bodyCalls, meta };
}

/** `statement := agent_stmt | order_stmt | bracket_stmt | token_stmt` —
 *  the top-level DISPATCH rule only (which production to parse `state`
 *  as), factored out of `parseStatement` (Sprint H) so the suggestion
 *  engine's `_collectBindingsUpToCursor` can run the EXACT SAME dispatch
 *  against a deliberately-incomplete prefix and inspect `state.bindings`
 *  afterward — `_registerBinding` mutates `state.bindings` in place
 *  before any later parse error throws, so a partial parse still leaves
 *  every label bound BEFORE the throw point intact. Never call this
 *  directly for a real compile — `parseStatement` below is still the
 *  real entry point (adds the trailing-EOF check + error normalization). */
function _dispatchStatement(state) {
  const first = _peek(state);

  // event_stmt dispatch rule, PRECISE and UNCHANGED (Sprint 12): a bare
  // leading `ALERT` (no parens) routes to `parseEventStmt` specifically
  // when its FIRST kwarg — before any bare channel name — is one of the
  // reserved event-meta keys (`log_tag=`/`min_level=`/`render=`/`where=`/
  // `gate=`/`match=`, see `_EVENT_META_KEYS`); checked via a 2-token
  // lookahead (NAME matching one of those keys, immediately followed by
  // a lone `EQ`) PAST the `ALERT` token itself. This is the "standalone
  // top-level alert CLI statement" the plan explicitly keeps untouched.
  //
  // Sprint [grammar-unify] correction — `alert` is now ALWAYS lexed with
  // `hasCall: false` (see `lexAgentStatement`'s `alert`-exclusion
  // comment: a literal `(` right after it is deliberately left for the
  // NEXT lexer iteration to tokenize as a real, standalone LPAREN, so
  // `parseCall` can consume it against the real token stream — see that
  // function's own `alert`-specific branch). `first.hasCall` can
  // therefore NEVER distinguish `alert(...)` (the new ordinary call)
  // from bare `ALERT` (the old, removed clause keyword / the event_stmt
  // form) — both always report `hasCall: false` at this point. The
  // actual test is a 1-token lookahead PAST `alert` itself: an immediate
  // LPAREN means "ordinary call," full stop, and this whole bare-ALERT
  // branch (event_stmt lookahead AND the clause-keyword rejection below)
  // must not fire — it falls through, unhandled, all the way to the
  // generic `order`/`bracket`-trial-then-`parseAgentStmt` fallback at the
  // bottom of this function, where `parseCall`'s own `alert`-specific
  // branch (inside `parseItemListClause`'s per-call parsing) is what
  // actually consumes it.
  // A bare `alert` immediately followed by `=` is an attempted LABEL
  // assignment (`alert = 5`), not any flavor of the clause keyword — it
  // must fall through to the unified preamble parser so `_registerBinding`
  // gives its own specific "reserved word and cannot be used as a label"
  // rejection, not this function's generic clause-keyword message.
  const nextTok = state.tokens[state.pos + 1];
  const firstIsBareAlert = first.type === 'NAME' && !first.hasCall && _wordIs(first, 'alert')
    && !(nextTok && (nextTok.type === 'LPAREN' || nextTok.type === 'EQ'));
  const evSecond = firstIsBareAlert ? state.tokens[state.pos + 1] : null;
  const evThird = firstIsBareAlert ? state.tokens[state.pos + 2] : null;
  const isEventStmt = firstIsBareAlert && evSecond && evSecond.type === 'NAME' && !evSecond.hasCall
    && _EVENT_META_KEYS.has(evSecond.value.toLowerCase()) && evThird && evThird.type === 'EQ';
  if (isEventStmt) return parseEventStmt(state);

  // Sprint [grammar-unify] — GIVEN is removed from the grammar completely
  // (not kept as optional/backward-compatible syntax, per the plan's
  // explicit, final decision). A literal leading `given` is a clear,
  // specific rejection naming the removed keyword, rather than falling
  // through to a confusing generic "unexpected trailing input"/"unknown
  // action 'given'" error several tokens later.
  if (first.type === 'NAME' && !first.hasCall && _wordIs(first, 'given')) {
    throw new _ParseError(
      'GIVEN is no longer part of the grammar — write the statement directly, e.g. "a = 1 when (a==1) do close_position(...)"',
      first,
    );
  }
  // A bare, non-called leading `ALERT` that ISN'T the event_stmt form
  // above is the OLD agent_stmt clause-keyword spelling (`ALERT telegram
  // DO nop`) — also removed, also a clear, specific rejection. `alert`
  // written as an ordinary call (`alert(...)`, `hasCall: true`) is NOT
  // this case at all — it falls through below, to the unified statement
  // parser, exactly like `order(...)`/`debug(...)`/`help(...)`.
  if (firstIsBareAlert) {
    throw new _ParseError(
      'ALERT is no longer a reserved clause keyword — call it as an ordinary statement, e.g. alert(telegram)',
      first,
    );
  }

  // `token`/`cancel_all_orders` — leading call names that are never real
  // metric/scope/channel/action-as-preamble-item names, so they dispatch
  // straight to their own standalone-statement parser, unambiguously.
  // Sprint [grammar-unify] amendment: unlike `order`/`bracket` below,
  // these two are NOT folded into the unified preamble/DO vocabulary (no
  // existing reuse path — `token`/`cancel_all_orders` each still has
  // exactly one call-site-free meaning, "a standalone statement of this
  // exact kind," never "one call among several in a mixed preamble") —
  // they keep their pre-existing, EXCLUSIVE dispatch, unchanged.
  if (first.type === 'NAME' && first.hasCall && _wordIs(first, 'token')) return parseTokenStmt(state);
  if (first.type === 'NAME' && first.hasCall && _wordIs(first, 'cancel_all_orders')) return parseCancelAllOrdersStmt(state);

  // `when`/`do` as a bare leading keyword always goes straight to the
  // unified statement parser (no preamble at all — see `parseAgentStmt`).
  if (first.type === 'NAME' && !first.hasCall && (_wordIs(first, 'when') || _wordIs(first, 'do'))) {
    return parseAgentStmt(state);
  }

  // `order(...)`/`bracket(...)` — Sprint [grammar-unify]: these TWO ARE
  // folded into the unified preamble/DO vocabulary (the plan's own worked
  // example mixes `order(...)`/`bracket(...)` with `alert(...)`/
  // `debug(...)`/`help(...)`/label-assignment ahead of a `WHEN ... DO
  // ...` tail). A leading `order(...)`/`bracket(...)` is therefore
  // AMBIGUOUS between "the whole statement is just this one standalone
  // order/bracket" (today's pre-existing, unchanged `kind:'order'`/
  // `kind:'bracket'` standalone forms — `parseOrderStmt`/
  // `parseBracketStmt`, still reused completely unchanged — do NOT touch
  // those compilers) and "this is the first item of a longer mixed
  // preamble." Resolved by TRYING the exclusive standalone parser first
  // and accepting it ONLY if it consumes the entire remaining input
  // (reaches EOF); any throw, OR leftover trailing input, backtracks
  // `state.pos` and falls through to the unified parser instead — which
  // accepts the identical call vocabulary, so nothing is lost. Safe to
  // backtrack: neither `parseOrderStmt` nor `parseBracketStmt` ever
  // registers a binding (`_registerBinding`/`state.bindings` are untouched
  // by either), so resetting `state.pos` alone is sufficient — there is
  // no OTHER mutable parse state to undo. A non-`_ParseError` throw is
  // NEVER swallowed here — only a nested statement's OWN deliberate
  // `_ParseError` is a legitimate "try the other interpretation" signal;
  // anything else is a real bug and must propagate.
  // `help(...)` joins the SAME try-then-fallback treatment (item 4): a
  // bare `help(<name>)` alone is still the pre-existing, unchanged
  // `kind:'help'` standalone form (`parseHelpStmt`), but `help(...)` is
  // ALSO folded into the unified preamble/DO vocabulary (the plan's own
  // worked example uses it there) — so the same EOF-gated trial applies.
  const isLeadingOrder = first.type === 'NAME' && first.hasCall && (_wordIs(first, 'order') || _wordIs(first, 'place_order'));
  const isLeadingBracket = first.type === 'NAME' && first.hasCall && _wordIs(first, 'bracket');
  const isLeadingHelp = first.type === 'NAME' && first.hasCall && _wordIs(first, 'help');
  if (isLeadingOrder || isLeadingBracket || isLeadingHelp) {
    const save = state.pos;
    try {
      const ast = isLeadingOrder ? parseOrderStmt(state) : isLeadingBracket ? parseBracketStmt(state) : parseHelpStmt(state);
      if (_peek(state).type === 'EOF') return ast;
    } catch (e) {
      if (!(e instanceof _ParseError)) throw e;
    }
    state.pos = save;
  }

  return parseAgentStmt(state);
}

/** `statement := agent_stmt | order_stmt | bracket_stmt | token_stmt` —
 *  top-level entry point. Returns `{ ast, errors }` — `ast` is `null` on
 *  any parse failure. */
export function parseStatement(text) {
  const { tokens, errors: lexErrors } = lexAgentStatement(text);
  if (lexErrors.length) return { ast: null, errors: lexErrors };
  // Sprint [GIVEN-expr] — `state.text` is the raw source text, needed ONLY
  // so a GIVEN binding's RHS can be sliced back out VERBATIM (see
  // `parseOneClauseItem`'s `rhsText` capture) for the new `agent.given[]`
  // compiled-output shape and the decompiler's byte-for-byte round-trip.
  // Harmless when unused (every pre-existing statement shape).
  const state = { tokens, pos: 0, text };
  try {
    const ast = _dispatchStatement(state);
    const trailing = _peek(state);
    if (trailing.type !== 'EOF') throw new _ParseError('unexpected trailing input', trailing);
    return { ast, errors: [] };
  } catch (e) {
    if (e instanceof _ParseError) return { ast: null, errors: [{ message: e.message, position: e.position }] };
    throw e;
  }
}

/** Sprint H (item 8) — parse `text.slice(0, pos)` through the SAME
 *  dispatch rule `parseStatement` uses, and return whatever
 *  `state.bindings` holds afterward, REGARDLESS of whether the parse
 *  ultimately throws (expected — the text up to the cursor is, by
 *  definition, usually an in-progress, not-yet-complete statement).
 *  `_registerBinding` (see its own docstring) mutates `state.bindings` in
 *  place the moment a `label = ...` assignment is parsed, strictly before
 *  any LATER token could throw — so every label bound before the cursor
 *  is already visible even when parsing the rest fails or never finishes.
 *  Returns an empty `Map` (never throws) on a lex failure or any dispatch
 *  failure that happens before a single binding was registered — the
 *  normal, expected outcome for a cursor sitting before any GIVEN/ALERT/DO
 *  label assignment at all. This is the live-autocomplete analogue of
 *  `compileAgentStmt`'s/the real parser's own `state.bindings` — reading
 *  the SAME mechanism, not a separate token-scanning heuristic, so a
 *  label suggested here is guaranteed to be exactly what the real
 *  compiler would also resolve. */
function _collectBindingsUpToCursor(text, pos) {
  const sliced = text.slice(0, pos);
  const { tokens, errors: lexErrors } = lexAgentStatement(sliced);
  if (lexErrors.length) return new Map();
  const state = { tokens, pos: 0, text: sliced };
  try { _dispatchStatement(state); } catch { /* partial parse — bindings registered before the throw still stand */ }
  return state.bindings || new Map();
}

/** Sprint H (items 1/3) — try to parse `text.slice(0, pos)` as a
 *  COMPLETE, standalone statement (same dispatch + trailing-EOF check
 *  `parseStatement` always applies). Returns the `{ ast, errors }` result
 *  (errors empty on success) or `null` on any unexpected throw (never
 *  bubbles — this is a speculative "are we at a clause boundary" probe,
 *  not a real compile). Used to detect "the text typed so far is already
 *  a syntactically complete statement" — the signal this file uses for
 *  "offer a clause-head keyword (WHEN/ALERT/DO) or a boolean connective
 *  (&/|) next" instead of a token-pattern heuristic, so it can never
 *  drift from what the real parser actually accepts. */
function _tryParsePrefixStatement(text, pos) {
  try { return parseStatement(text.slice(0, pos)); } catch { return null; }
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
    const keyTok = { start: arg.keyStart };
    if (!realKey) { errors.push(_err(`unknown parameter '${arg.key}' for ${label}`, keyTok)); return; }
    if (provided.has(realKey)) { errors.push(_err(`${label}: duplicate parameter '${realKey}'`, keyTok)); return; }
    // Sprint 20 (item 4) — clone (never mutate the parsed AST node in
    // place) and tag with the operator's ORIGINAL typed spelling (which
    // may be an alias, e.g. "acct" for "account") so a downstream
    // coercion error can name what the trader actually typed, not the
    // canonical resolved field name — see `_coerceValueForSpec`.
    provided.set(realKey, { ...arg.value, _typedKey: arg.key });
  });
  return provided;
}

/** Coerce one ValueNode against its params_schema field spec. Returns
 *  `{ value }` or `{ error }` — never throws.
 *
 *  Sprint 20 (items 4/5) — every error message now names whatever the
 *  operator ACTUALLY TYPED for this field (`valueNode._typedKey`, set by
 *  `_mapCallArgsToKeys` when the typed spelling is an alias; falls back
 *  to the canonical `key` when absent, e.g. for a call site that never
 *  went through alias mapping at all) and carries a real text position
 *  (`valueNode` itself, when it has a `start`, since `_parseArgSegment`
 *  now tags every ValueNode with its own `start`/`end`). */
function _coerceValueForSpec(valueNode, spec, key, label) {
  const type = spec && spec.type;
  const displayKey = (valueNode && valueNode._typedKey) || key;
  const pos = valueNode && typeof valueNode.start === 'number' ? valueNode : undefined;
  if (type === 'enum') {
    if (valueNode.type !== 'string' && valueNode.type !== 'name') return { error: _err(`${label}.${displayKey}: expected an enum value`, pos) };
    const raw = valueNode.value;
    const match = (spec.enum || []).find(e => String(e).toLowerCase() === String(raw).toLowerCase());
    if (!match) return { error: _err(`${label}.${displayKey}: '${raw}' is not one of [${(spec.enum || []).join(', ')}]`, pos) };
    return { value: match };
  }
  if (type === 'number') {
    if (valueNode.type === 'number') return { value: valueNode.value };
    if (valueNode.type === 'string' && spec.token_ref_ok) return { value: valueNode.value }; // expr string, evaluated server-side
    return { error: _err(`${label}.${displayKey}: expected a number`, pos) };
  }
  if (type === 'boolean') {
    if (valueNode.type === 'name') {
      const low = String(valueNode.value).toLowerCase();
      if (low === 'true') return { value: true };
      if (low === 'false') return { value: false };
    }
    return { error: _err(`${label}.${displayKey}: expected true or false`, pos) };
  }
  if (type === 'string') {
    if (valueNode.type === 'string') return { value: valueNode.value };
    // Sprint 20 (item 4) — explains WHY, so the rule stops "looking
    // arbitrary from the error alone": a bare word here is reserved for
    // enum values and $row column references (see `_resolveRowRefOrNull`
    // for the latter), never literal free text — unlike `msg=`/`log_tag=`/
    // meta values below, which have no $row concept and accept either.
    return { error: _err(`${label}.${displayKey}: free-text value must be quoted (e.g. ${displayKey}="...") — bare words are reserved for enum/row-ref values`, pos) };
  }
  // Sprint 18 follow-up (`match=[...]`) — `freetext` mirrors
  // `_freeTextValue`'s own bare-NAME-or-quoted-STRING acceptance (e.g.
  // `log_tag=`/`render=`): preserved EXACTLY as typed, no case
  // normalization, unlike `type:'enum'` above or `type:'string'`'s
  // quote-required rule. Used for match entry `tag=` (`tag=orders`,
  // unquoted, same spelling convention as the simple form's `log_tag=`)
  // and (Sprint 20, item 4) `msg=`/`message=` (`_MESSAGE_SPEC`) — a bare
  // single word now works there too, same rule as log_tag/meta, closing
  // the "msg=hello doesn't [accept bare]" inconsistency the audit flagged.
  if (type === 'freetext') {
    if (valueNode.type === 'string' || valueNode.type === 'name') return { value: valueNode.value };
    return { error: _err(`${label}.${displayKey}: expected a bare word or quoted string`, pos) };
  }
  // Sprint 18 follow-up — a nested object_literal value (match entry's
  // own `where={...}`), unwrapped via `_unwrapValueNode` same as the
  // top-level `where=`'s passthrough schema (`OBJECT_LITERAL_SCHEMAS.where
  // === null`) already does.
  if (type === 'object') {
    if (valueNode.type !== 'object') return { error: _err(`${label}.${displayKey}: expected an object literal {...}`, pos) };
    return { value: _unwrapValueNode(valueNode) };
  }
  // Unknown/absent spec type — best-effort passthrough.
  if (valueNode.type === 'list') return { value: valueNode.value.map(v => v.value) };
  return { value: valueNode.value };
}

// ═══════════════════════════════════════════════════════════════════════
// Sprint 17 — three generic parameter-type mechanisms, each reading a
// catalog-declared schema key instead of a hand-rolled, field-name-specific
// check: per-key object-literal typing, conditional (`required_if`)
// requirements, and declared (`mutuallyExclusiveGroups`) mutual-exclusion.
// ═══════════════════════════════════════════════════════════════════════

// `min_level`'s enum spec + default — shared by event_stmt's simple
// single-filter form (`min_level=`) AND (Sprint 18 follow-up) each
// `match=[...]` list entry's own `min_level` field. Moved up here (ahead
// of its original Sprint 12 call site, further down this file) so
// `OBJECT_LITERAL_SCHEMAS.match` below can reference `_MIN_LEVEL_SPEC`
// directly — see the "event_stmt compiler" section comment further down
// for the full history/rationale.
const _MIN_LEVEL_SPEC = Object.freeze({ type: 'enum', enum: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] });
const _MIN_LEVEL_DEFAULT = 'INFO'; // confirmed against event_agents.py:validate_seed_spec's own `str(leaf.get("min_level", "INFO"))` default

/** Per-key type schema for object-literal-valued call args, keyed by the
 *  FIELD NAME the object-literal is assigned to. `tp_scales_json=[{...}]`'s
 *  per-entry shape has a real fixed schema (both keys are plain numbers on
 *  the backend's `[{at_pct, close_pct}]` JSON convention — see
 *  `backend/api/schemas.py`'s `tp_scales_json` field comment). `where`
 *  deliberately maps to `null` — it has no fixed schema at all (arbitrary
 *  key/value pairs matching whatever real `log` `extra` fields exist),
 *  so `_coerceObjectLiteralEntry` below treats a `null` schema as
 *  "passthrough every key unwrapped, untyped" — not "reject everything".
 *
 *  `match` (Sprint 18 follow-up, `ALERT match=[{...}, {...}]`) is the
 *  per-entry shape of `match=`'s list-of-objects — each entry has the
 *  SAME three sub-keys the simple single-filter event_stmt form has
 *  (`tag`/`min_level`/`where`, spelled `tag=` rather than `log_tag=`
 *  since there is no top-level `log_tag=` reserved-word collision risk
 *  inside an object literal): `tag` is free-text (same `_freeTextValue`-
 *  equivalent semantics as the simple form's `log_tag=` — bare or
 *  quoted, never case-normalized), `min_level` is the SAME enum spec as
 *  the simple form (defaults to `_MIN_LEVEL_DEFAULT` when omitted,
 *  applied in `_compileMatchList` below), `where` is a nested object
 *  literal (untyped passthrough, same as the simple form's own `where=`). */
const OBJECT_LITERAL_SCHEMAS = Object.freeze({
  tp_scales_json: Object.freeze({ at_pct: { type: 'number' }, close_pct: { type: 'number' } }),
  where: null,
  match: Object.freeze({ tag: { type: 'freetext' }, min_level: _MIN_LEVEL_SPEC, where: { type: 'object' } }),
});

/** Coerce ONE `{key=value}` object-literal entry node (the `{type:'object',
 *  value: {...}}` ValueNode shape) against a per-key type `schema` —
 *  reuses `_coerceValueForSpec` per key (same enum/number/boolean/string
 *  branches every other param already goes through) rather than
 *  reimplementing coercion rules. `schema == null` means "no fixed
 *  schema" — every key passes through `_unwrapValueNode` untyped, exactly
 *  matching this file's pre-Sprint-17 behavior for `where=`, so this is a
 *  pure additive generalization for any field that DOES declare a schema.
 *  An entry key absent from a non-null schema is a compile error (unknown
 *  key), matching this file's existing unknown-kwarg-rejection convention
 *  (`_mapCallArgsToKeys`). Never throws; returns `{ value, errors }`. */
function _coerceObjectLiteralEntry(entryNode, schema, label) {
  const errors = [];
  const out = {};
  for (const [key, valueNode] of Object.entries((entryNode && entryNode.value) || {})) {
    if (!schema) { out[key] = _unwrapValueNode(valueNode); continue; }
    const spec = schema[key];
    if (!spec) { errors.push(_err(`${label}: unknown key '${key}' in object literal`)); continue; }
    const coerced = _coerceValueForSpec(valueNode, spec, key, label);
    if (coerced.error) errors.push(coerced.error); else out[key] = coerced.value;
  }
  return { value: out, errors };
}

/** Generic mutual-exclusion checker — given a token's catalog-declared
 *  `mutuallyExclusiveGroups` (list of list[str] field-name groups) and a
 *  Set of the field names actually PRESENT on this call (lowercased, in
 *  whatever naming convention the caller's own group list already uses —
 *  a caller bridging a shared short-kwarg vocabulary onto a differently-
 *  suffixed catalog field-name convention normalizes BOTH sides to the
 *  same convention before calling this), raises one error per group with
 *  2+ members present, naming the first two offending fields — mirrors
 *  this file's pre-existing "cannot specify both X and Y" wording (the
 *  hand-rolled wing_premium_pct/wing_strike_offset check this generalizes).
 *  Never throws. */
function _checkMutexGroups(presentKeys, groups, label) {
  const errors = [];
  for (const group of groups || []) {
    const present = (group || []).filter((f) => presentKeys.has(String(f).toLowerCase()));
    if (present.length > 1) {
      errors.push(_err(`${label}: cannot specify both ${present[0]} and ${present[1]}`));
    }
  }
  return errors;
}

/** Generic conditional-requirement checker — for each key in `paramKeys`
 *  whose own spec declares `required_if: {field, in}`, checks whether
 *  `field`'s own RESOLVED value (via `resolvedGet(name)`, which must
 *  return `undefined` for anything not explicitly resolved on this call —
 *  never silently substitute a default inside this function) is a member
 *  of `in` (case-insensitive); if so AND the dependent key itself has no
 *  resolved value, pushes one "required when" error naming both the
 *  dependent field and the triggering value. MUST be called only after
 *  every other arg on the call has already been resolved — checking
 *  `price`'s conditional requirement needs `order_type`'s own
 *  already-resolved value, which can't exist mid per-key resolution loop.
 *  Never throws. */
function _checkRequiredIf(paramKeys, paramsSchema, resolvedGet, label) {
  const errors = [];
  for (const key of paramKeys) {
    const spec = paramsSchema[key];
    const reqIf = spec && spec.required_if;
    if (!reqIf || resolvedGet(key) !== undefined) continue;
    const refVal = resolvedGet(reqIf.field);
    if (refVal === undefined) continue;
    const inList = (reqIf.in || []).some((v) => String(v).toLowerCase() === String(refVal).toLowerCase());
    if (inList) errors.push(_err(`${label}: ${key} is required when ${reqIf.field}=${refVal}`));
  }
  return errors;
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

/** Join plain `call`-type scope segments into a dotted lookup key, applying
 *  the Sprint 19 short scope-family alias to segment 0 only (deeper
 *  segments like `expiring_today`/`mcx_unhedged` have no abbreviated
 *  form). Returns the resolved catalog TOKEN (canonical casing) or `null`
 *  when the joined chain doesn't resolve as a known scope — `null` is NOT
 *  itself an error here (a caller may legitimately want to try a fallback
 *  resolution, see `_resolveScopeRefFull`'s implicit-base-scope branch);
 *  an `.args` violation on a segment IS always a hard error, pushed
 *  directly onto `errors`. */
function _resolveScopeStringParts(segs, catalog, errors) {
  const parts = [];
  for (let i = 0; i < segs.length; i++) {
    const seg = segs[i];
    if (seg.args && seg.args.length) { errors.push(_err(`scope segment '${seg.name}' takes no arguments`)); return null; }
    const lower = seg.name.toLowerCase();
    parts.push(i === 0 ? _scopeFamilyAliasOf(lower) : lower);
  }
  const entry = catalog.scopes.get(parts.join('.'));
  return entry ? entry.token : null;
}

function _resolveScopeString(scopeNode, catalog, errors) {
  const localErrors = [];
  const resolved = _resolveScopeStringParts(scopeNode.segments, catalog, localErrors);
  if (localErrors.length) { errors.push(...localErrors); return null; }
  if (!resolved) {
    errors.push(_err(`unknown scope '${scopeNode.segments.map(s => s.name.toLowerCase()).join('.')}'`));
    return null;
  }
  return resolved;
}

// ── Scope-filter qualifiers (Sprint 19) — `account:value`/`symbol:value`
// (plus their `*`/`worst` reserved-value forms), appended to a scope
// chain via the existing "." dotting convention, e.g.
// `positions.account:ZG0790`, `positions.expiring_today.symbol:
// NIFTY25JULFUT`. Targets the real, already-shipped backend shape (commit
// 1a904a12): a leaf's compiled JSON gains an OPTIONAL `scope_filter` key,
// a SIBLING of `metric`/`scope`/`op`/`value` (NEVER folded into the
// `scope` string itself) — e.g.
// `{"metric":"pnl","scope":"positions.expiring_today","op":"<=",
//   "value":-5000,"scope_filter":{"symbol":"NIFTY25JULFUT"}}`.
// Qualifier keys compile to EXPOSED-COLUMN names (`account_id`/`symbol`),
// reusing Sprint 15's `catalog.columns` per-scope registry as the
// validity whitelist — mirrors the backend's own
// `exposed_columns_for_scope()`/`scope_filter_row_predicate()` check
// byte-for-byte (same error class: an unsupported qualifier on a scope
// with no matching exposed column is a compile error, exactly like the
// backend's own save-time `validate()` rejects it). An empty/all-wildcard
// filter is never emitted as `scope_filter: {}` — omitted entirely,
// matching the backend's explicit "omit the key for no filter" rule.
//
// `*` and `worst` are RESERVED qualifier values carrying NO `scope_filter`
// at all — pure aliases onto an EXISTING scope token, zero new mechanism:
// `account:*` always resolves the implicit `<family>.any_acct` scope
// with no filter (identical to omitting the qualifier entirely);
// `account:worst`/`symbol:worst` resolve the EXISTING
// `<family>.worst_acct`/`<family>.worst_symbol` tokens directly. `worst`
// is valid ONLY as the chain's sole qualifier, directly off the bare
// scope-family root (no deeper scope name before it) — there is no
// existing "worst within THIS narrower scope" token to alias onto, and
// inventing one is explicitly out of scope for this sprint (deliberately
// surfaces as a clear compile error for e.g. `funds.account:worst`, which
// has no `funds.worst_acct` token today — a real, intentionally-deferred
// gap, not silently-wrong behavior).
// `exchange:` (added for `quote.tracked` — Sprint 21's non-NSE quote-fire
// bug fix) follows the SAME generic mechanism as `account:`/`symbol:`: no
// per-scope allowlist here, the `catalog.columns.has(scope.exchange)`
// check just below already restricts it to whichever scopes genuinely
// expose a real `exchange` column (`EXPOSED_COLUMNS` in the backend's
// `grammar.py` — currently `quote.tracked`, `watchlist.*`,
// `orders.any_acct`, and the `positions.expiring_today` row family; NOT
// `positions.total`/`funds.*`/summary scopes, which have no single
// exchange to filter on).
// Sprint G (Task 4) — `kind:` (the new `symbols.*` family — watchlist/
// gainers/losers/pinned) and `underlying:` (`positions.*` — free-text
// symbol name) extend this SAME scope_filter mechanism (Sprint 19,
// commit 1a904a12) — a small, mechanical whitelist addition, not a new
// qualifier-parsing path. Both compile to a leaf-level `scope_filter`
// dict exactly like `account:`/`symbol:`/`exchange:` already do; neither
// gets its own uppercase-normalization rule (that's `exchange:`-specific,
// per the real backend's uppercase-only `exchange` column convention).
const _QUALIFIER_COLUMN_NAME = Object.freeze({
  account: 'account_id', symbol: 'symbol', exchange: 'exchange', kind: 'kind', underlying: 'underlying',
});
const _QUALIFIER_COLUMN_NAME_REVERSE = Object.freeze({
  account_id: 'account', symbol: 'symbol', exchange: 'exchange', kind: 'kind', underlying: 'underlying',
});

// family -> qualifier key -> existing scope token "worst" aliases onto.
// Deliberately sparse — e.g. `funds` has no `worst_acct` token today (see
// the comment above) — a missing entry surfaces as a clear compile
// error, never a silent wrong scope.
const _QUALIFIER_WORST_ALIAS = Object.freeze({
  positions: Object.freeze({ account: 'positions.worst_acct', symbol: 'positions.worst_symbol' }),
  holdings: Object.freeze({ account: 'holdings.worst_acct', symbol: 'holdings.worst_symbol' }),
});

function _qualifierValueText(q) {
  if (q.value.type === 'star') return '*';
  return String(q.value.value);
}

/** `scope_ref` resolver (Sprint 15's `_resolveScopeString`, extended for
 *  Sprint 19's qualifier segments). Returns `{ scopeStr, scopeFilter }` —
 *  `scopeFilter` is `undefined` when no literal-valued qualifier was
 *  present (NEVER an empty `{}`). Returns `null` (after pushing onto
 *  `errors`) on any failure. */
function _resolveScopeRefFull(scopeNode, catalog, errors) {
  const segs = scopeNode.segments;
  const qualIdx = segs.findIndex((s) => s.type === 'qualifier');
  if (qualIdx === -1) {
    const scopeStr = _resolveScopeString(scopeNode, catalog, errors);
    return scopeStr ? { scopeStr, scopeFilter: undefined } : null;
  }
  // Trailing-only — a 'call' segment after the first qualifier segment is
  // a clear, specific compile error (same "trailing-only" convention
  // `meta_item*` already follows elsewhere in this grammar).
  for (let i = qualIdx + 1; i < segs.length; i++) {
    if (segs[i].type !== 'qualifier') {
      errors.push(_err(`scope qualifier segments ('${segs[qualIdx].key}:...') must be trailing — '${segs[i].name}' cannot follow one`));
      return null;
    }
  }
  const callSegs = segs.slice(0, qualIdx);
  const qualSegs = segs.slice(qualIdx);
  if (callSegs.length === 0) {
    errors.push(_err('expected a scope before a qualifier segment'));
    return null;
  }
  // A real args-violation on a call segment is always a hard error — even
  // though the "does the prefix resolve explicitly" check just below is
  // speculative (a failed explicit resolution legitimately falls back to
  // the implicit any_acct base), a malformed segment like `positions(5)`
  // must never be silently swallowed by that fallback.
  for (const seg of callSegs) {
    if (seg.args && seg.args.length) { errors.push(_err(`scope segment '${seg.name}' takes no arguments`)); return null; }
  }

  // Resolve the base scope the qualifiers apply to. If the call-segment
  // prefix ALREADY is a real, existing scope token (e.g.
  // `positions.expiring_today`), use it unchanged. Otherwise — the
  // common case, a BARE scope-family root with no scope token of its own
  // (e.g. bare `positions`) — fall back to that family's own `.any_acct`
  // token, the implicit "every account, per-row" base every qualifier
  // example assumes (`positions.account:ZG0790` reads as "within
  // positions.any_acct's own rows, keep only this account").
  const family = _scopeFamilyAliasOf(callSegs[0].name);
  const explicit = _resolveScopeStringParts(callSegs, catalog, []);
  let effectiveBaseScope;
  if (explicit) {
    effectiveBaseScope = explicit;
  } else if (callSegs.length === 1 && catalog.scopes.has(`${family}.any_acct`)) {
    effectiveBaseScope = catalog.scopes.get(`${family}.any_acct`).token;
  } else {
    errors.push(_err(`unknown scope '${callSegs.map(s => s.name.toLowerCase()).join('.')}'`));
    return null;
  }

  // "worst" — pure alias onto an existing scope token, mutually exclusive
  // with every other qualifier form (no backend representation exists
  // for combining it with a literal filter or a second qualifier). Valid
  // ONLY directly off the bare scope-family root (`callSegs` resolving
  // via the IMPLICIT any_acct fallback, never an explicit deeper/narrower
  // scope like `expiring_today`) — there is no "worst within THIS
  // narrower scope" token to alias onto, and silently discarding the
  // narrowing (e.g. collapsing `positions.expiring_today.symbol:worst`
  // onto plain `positions.worst_symbol`) would change the condition's
  // meaning without any error.
  const worstQual = qualSegs.find((q) => q.value.type === 'name' && String(q.value.value).toLowerCase() === 'worst');
  if (worstQual) {
    if (qualSegs.length > 1) {
      errors.push(_err(`'${worstQual.key}:worst' must be the only qualifier on this scope — it already selects one specific row`));
      return null;
    }
    if (explicit) {
      errors.push(_err(
        `'${worstQual.key}:worst' is only valid directly off the bare '${family}' scope family — `
        + `there is no "worst within '${effectiveBaseScope}'" token to alias onto`
      ));
      return null;
    }
    const qualKey = worstQual.key.toLowerCase();
    const target = (_QUALIFIER_WORST_ALIAS[family] || {})[qualKey];
    if (!target || !catalog.scopes.has(target)) {
      errors.push(_err(`'${family}.${qualKey}:worst' has no existing scope token to alias onto`));
      return null;
    }
    return { scopeStr: catalog.scopes.get(target).token, scopeFilter: undefined };
  }

  const scopeFilter = {};
  const seenColumns = new Set();
  for (const q of qualSegs) {
    const qualKey = q.key.toLowerCase();
    const column = _QUALIFIER_COLUMN_NAME[qualKey];
    if (!column) {
      errors.push(_err(`unknown scope qualifier '${q.key}:' — only 'account:'/'symbol:'/'exchange:'/'kind:'/'underlying:' are supported`));
      return null;
    }
    const colKey = `${effectiveBaseScope}.${column}`.toLowerCase();
    if (!catalog.columns || !catalog.columns.has(colKey)) {
      errors.push(_err(`'${q.key}' is not a valid qualifier for scope '${effectiveBaseScope}' (no exposed '${column}' column)`));
      return null;
    }
    if (seenColumns.has(column)) {
      errors.push(_err(`scope_filter: duplicate qualifier '${q.key}:'`));
      return null;
    }
    seenColumns.add(column);
    // "*" is validated identically to a literal value above (an unknown
    // key or an unexposed column is still a compile error for a wildcard
    // qualifier) but contributes NO scope_filter entry — explicit
    // wildcard is the same as omitting the qualifier entirely.
    if (q.value.type === 'star') continue;
    let qualText = _qualifierValueText(q);
    // `exchange:` values are always stored/compared uppercase — every
    // real row's `exchange` column is uppercase ("MCX"/"NFO"/"CDS"/...,
    // see `background._perf_build_quote_rows`'s `exch_u`), and the
    // backend's `scope_filter_row_predicate` does an exact case-sensitive
    // `str(...) == str(...)` match (grammar.py), so `exchange:mcx` would
    // otherwise compile cleanly but never match any row — a silent
    // never-fires bug of the same class this fix closes. Uppercase here
    // at compile time rather than relaxing the backend match to
    // case-insensitive, since every other qualifier (`account:`/
    // `symbol:`) intentionally stays case-sensitive passthrough.
    if (column === 'exchange') qualText = qualText.toUpperCase();
    scopeFilter[column] = qualText;
  }
  const hasFilter = Object.keys(scopeFilter).length > 0;
  return { scopeStr: effectiveBaseScope, scopeFilter: hasFilter ? scopeFilter : undefined };
}

/** Decompile-direction mirror of `_resolveScopeRefFull`'s `scope_filter`
 *  handling — renders a stored `scope_filter` dict back to its trailing
 *  `.qualifier:value` CLI suffix (e.g. `.account:ZG0790`), appended
 *  verbatim to whatever the stored `scope` string already decompiled to.
 *  Fails loud (never guesses) on an unrecognized scope_filter key or an
 *  unsafe-to-render value, per this file's "never silently drop a field"
 *  convention. */
function _decompileScopeFilter(scopeFilter, errors) {
  if (!scopeFilter || typeof scopeFilter !== 'object' || Array.isArray(scopeFilter)) {
    errors.push(_err(`scope_filter must be an object, got ${JSON.stringify(scopeFilter)}`));
    return null;
  }
  const keys = Object.keys(scopeFilter);
  if (keys.length === 0) {
    errors.push(_err('scope_filter must not be empty (omit the key entirely for "no filter")'));
    return null;
  }
  const parts = [];
  for (const column of keys) {
    const qualKey = _QUALIFIER_COLUMN_NAME_REVERSE[column];
    if (!qualKey) { errors.push(_err(`scope_filter key '${column}' has no CLI qualifier representation yet`)); return null; }
    const raw = scopeFilter[column];
    if (typeof raw !== 'string' || !raw) { errors.push(_err(`scope_filter.${column} must be a non-empty string`)); return null; }
    if (raw === '*') { parts.push(`${qualKey}:*`); continue; }
    const r = _isBareable(raw) ? { text: raw } : _quoteString(raw);
    if (r.error) { errors.push(_err(`scope_filter.${column}: ${r.message}`)); return null; }
    parts.push(`${qualKey}:${r.text}`);
  }
  return `.${parts.join('.')}`;
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

function _compileListValue(listNode, valueType, errors) {
  const isNumeric = valueType === 'number' || !valueType;
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

/** Shared comparator/value compile logic for a resolved metric-OR-column
 *  reference (Sprint 15) — the exact checks `compileLeaf` always had
 *  ("boolean metric only supports ==/!=", "numeric comparator requires a
 *  numeric value", the bare/boolean-shorthand case, `in`/`not_in`'s list
 *  requirement), extracted so a resolved COLUMN entry (which has no
 *  concept of "metric-kind" at all — it's a different catalog bucket)
 *  gets the IDENTICAL behavior for free. Gated purely on `valueType`
 *  (`'boolean'` / `'number'` / `'string'` / falsy), never on which
 *  catalog bucket the caller resolved from — this is the "widen to
 *  valueType, not metric-kind" the sprint's design asked for. Takes the
 *  already-compiled `metricStr`/`scopeStr` (a column's `metricStr` is its
 *  bare exposed name, e.g. `"pnl"` — see `_resolveDotAccess`) so the
 *  output shape is byte-identical to what `compileLeaf` always produced
 *  for a real metric. */
function _compileComparatorValue(metricStr, scopeStr, valueType, op, value, errors) {
  if (op === null) {
    if (valueType === 'boolean') {
      return { metric: metricStr, scope: scopeStr, op: '==', value: true };
    }
    errors.push(_err(`expected a comparator after a numeric metric '${metricStr}'`));
    return null;
  }

  if (op === 'in' || op === 'not_in') {
    if (value.type !== 'list') {
      errors.push(_err(`'${op === 'in' ? 'in' : 'not in'}' requires a list literal on the right`));
      return null;
    }
    const list = _compileListValue(value, valueType, errors);
    if (errors.length) return null;
    return { metric: metricStr, scope: scopeStr, op, value: list };
  }

  if (valueType === 'boolean') {
    if (op !== '==' && op !== '!=') {
      errors.push(_err(`boolean metric '${metricStr}' only supports == / !=`));
      return null;
    }
    if (value.type !== 'name' || !['true', 'false'].includes(String(value.value).toLowerCase())) {
      errors.push(_err(`${metricStr}: expected true or false`));
      return null;
    }
    return { metric: metricStr, scope: scopeStr, op, value: String(value.value).toLowerCase() === 'true' };
  }

  // Sprint 21 — a string-typed metric/column (e.g. `orders.any_acct.status`)
  // only supports scalar equality, never an ordered comparator — same
  // "==/!= only" shape the boolean branch above already uses. Both a bare
  // NAME and a quoted STRING are accepted for the right-hand value (same
  // "either spelling accepted" precedent `in`/`not_in` already established
  // for a non-numeric metric's list items, via `_compileListValue`) — the
  // DO-clause's separate "free-text value must be quoted" rule is a
  // different code path (`_coerceValueForSpec`, action params only) and is
  // untouched by this.
  if (valueType === 'string') {
    if (op !== '==' && op !== '!=') {
      errors.push(_err(`string metric '${metricStr}' only supports == / !=`));
      return null;
    }
    if (value.type !== 'string' && value.type !== 'name') {
      errors.push(_err(`${metricStr}: expected a string value`));
      return null;
    }
    return { metric: metricStr, scope: scopeStr, op, value: String(value.value) };
  }

  if (value.type !== 'number') {
    errors.push(_err('comparator requires both sides numeric'));
    return null;
  }
  return { metric: metricStr, scope: scopeStr, op, value: value.value };
}

/** Resolve a bare dotted chain (Sprint 15's `col_ref`, e.g.
 *  `positions.total.pnl` or `positions.expiring_today.mcx_unhedged`) at
 *  COMPILE time into `{ metricStr, scopeStr, valueType }` — the same
 *  trio `compileLeaf` already derives for a `metric@scope` leaf — or
 *  pushes an error and returns `null`.
 *
 *  Disambiguation order (per the sprint's design, "longest known-scope-
 *  chain match first"):
 *    1. The WHOLE chain is itself a known SCOPE token (e.g.
 *       `positions.expiring_today.mcx_unhedged` used bare, no trailing
 *       column) — a clear, specific error: there's nothing to compare, a
 *       scope alone is not a condition. This is what keeps a genuine
 *       multi-segment scope name from ever being misparsed as a shorter
 *       scope + a column read.
 *    2. Chain-minus-last-segment is a known scope AND the last segment is
 *       a registered exposed COLUMN on that scope — the common case
 *       (`positions.total.pnl`).
 *    3. Chain-minus-last-segment is a known scope AND the last segment is
 *       an EXISTING bare (zero-arg) metric token (not a fragment/Token
 *       ref) — e.g. a fixed-window metric like `mean_pnl_30m` read via
 *       dot-access instead of `mean_pnl_30m@scope`. A metric that NEEDS
 *       arguments has no default-dot shortcut (dot-access can't pass
 *       arguments) — a clear error pointing at the explicit call form.
 *    4. Anything else — unknown scope or unknown column/metric on it. */
/** Sprint 21 — the qualifier-aware branch of `_resolveDotAccess`, reached
 *  only when at least one segment of the bare dotted chain is a Sprint-19
 *  QualifierNode (e.g. `quote.tracked.symbol:NIFTY25JULFUT.ltp`). Without
 *  this, a scope requiring a qualifier (`quote.tracked`) would be
 *  completely unreachable for any exposed column that has no dedicated
 *  "@"-form metric token (`bid`/`ask`/`volume` have none) — only `ltp`
 *  (reused as a real metric) would have a usable spelling at all.
 *
 *  The grammar rule is simple and matches `scope_ref`'s own convention:
 *  the LAST segment is always the trailing column/metric name (a plain
 *  call, never a qualifier); EVERY segment before it is the scope chain,
 *  qualifiers included — handed off verbatim to Sprint 19's own
 *  `_resolveScopeRefFull`, so qualifier validation (unknown key, "*"/
 *  "worst" handling, trailing-only enforcement, exposed-column
 *  whitelisting) is defined in exactly ONE place for both the "@" form
 *  and this bare dot-access form. */
function _resolveDotAccessWithQualifier(segments, catalog, errors) {
  const last = segments[segments.length - 1];
  if (last.type !== 'call') {
    errors.push(_err(`expected a trailing '.column' name after '${last.key}:' — a qualifier segment cannot be the last segment of a dot-access chain`));
    return null;
  }
  if (last.args && last.args.length) {
    errors.push(_err(`'${last.name}' takes no arguments`));
    return null;
  }
  const scopeSegs = segments.slice(0, -1);
  const scopeResolved = _resolveScopeRefFull({ type: 'scope', segments: scopeSegs }, catalog, errors);
  if (!scopeResolved) return null; // _resolveScopeRefFull already pushed the specific error

  const lastName = last.name.toLowerCase();
  const { scopeStr, scopeFilter } = scopeResolved;
  const colKey = `${scopeStr}.${lastName}`.toLowerCase();
  const colEntry = catalog.columns && catalog.columns.get(colKey);
  if (colEntry) {
    const exposedName = colEntry.token.slice(scopeStr.length + 1);
    return { metricStr: exposedName, scopeStr, valueType: colEntry.valueType, scopeFilter };
  }
  const metricEntry = catalog.metrics.get(lastName);
  if (metricEntry && !metricEntry.isFragmentRef) {
    if (metricEntry.paramKeys && metricEntry.paramKeys.length) {
      errors.push(_err(
        `'${lastName}' requires argument(s) — use '${lastName}(...)@${scopeStr}', `
        + `dot-access has no way to pass arguments`
      ));
      return null;
    }
    return { metricStr: metricEntry.token, scopeStr, valueType: metricEntry.valueType, scopeFilter };
  }
  errors.push(_err(`unknown column '${lastName}' on scope '${scopeStr}'`));
  return null;
}

function _resolveDotAccess(segments, catalog, errors) {
  // Sprint 21 — a qualifier segment anywhere in the chain delegates
  // entirely to the qualifier-aware branch above; the no-qualifier path
  // below is completely unchanged (byte-identical behavior for every
  // pre-Sprint-21 chain).
  if (segments.some((s) => s.type === 'qualifier')) {
    return _resolveDotAccessWithQualifier(segments, catalog, errors);
  }
  const parts = [];
  for (let i = 0; i < segments.length; i++) {
    const seg = segments[i];
    if (seg.args && seg.args.length) {
      errors.push(_err(`'${seg.name}' takes no arguments`));
      return null;
    }
    // Sprint 19 — short scope-family alias (`pos`/`hld`/`fnd`/`wl`),
    // applied to segment 0 only, same as `_resolveScopeStringParts`.
    const lower = seg.name.toLowerCase();
    parts.push(i === 0 ? _scopeFamilyAliasOf(lower) : lower);
  }
  const full = parts.join('.');
  if (catalog.scopes.has(full)) {
    errors.push(_err(
      `'${full}' is a scope, not a condition — append '.column' to read a value off it `
      + `(e.g. '${full}.pnl'), or use "metric@${full}" syntax`
    ));
    return null;
  }
  // Sprint G (Task 3) — `funds.cash`/`funds.margin` are column-alias
  // shortcuts for the real `cash`/`avail_margin` metrics, bound to
  // `funds.total` (the aggregate, operator-facing default) — NOT a new
  // scope root (operator's explicit "do NOT split funds into separate
  // cash/margin scope roots" instruction; reuses Sprint 15's exposed-
  // column registry MECHANISM, not a literal new column row). Checked
  // here, before the generic scope-prefix resolution below, since the
  // literal 2-segment spelling `funds.cash` has no scope token of its
  // own at all (only `funds.total`/`funds.any_acct` do) — the generic
  // path below would otherwise fail with "unknown scope 'funds'" before
  // ever reaching a column/metric lookup.
  if (parts.length === 2 && parts[0] === 'funds' && _FUNDS_COLUMN_ALIASES[parts[1]]) {
    const base = catalog.scopes.get('funds.total');
    const aliasedMetric = base && catalog.metrics.get(_FUNDS_COLUMN_ALIASES[parts[1]]);
    if (base && aliasedMetric && !aliasedMetric.isFragmentRef) {
      return { metricStr: aliasedMetric.token, scopeStr: base.token, valueType: aliasedMetric.valueType };
    }
  }
  if (parts.length < 2) {
    const other = _describeKind(parts[0], catalog);
    errors.push(_err(other
      ? `'${parts[0]}' is ${other} — expected a 'scope.column' dot-access read`
      : `unknown scope or column '${parts[0]}'`));
    return null;
  }
  const scopePart = parts.slice(0, -1).join('.');
  const lastName = parts[parts.length - 1];
  const scopeEntry = catalog.scopes.get(scopePart);
  if (!scopeEntry) {
    errors.push(_err(`unknown scope '${scopePart}'`));
    return null;
  }
  const columns = catalog.columns;
  const colKey = `${scopeEntry.token}.${lastName}`.toLowerCase();
  const colEntry = columns && columns.get(colKey);
  if (colEntry) {
    const exposedName = colEntry.token.slice(scopeEntry.token.length + 1);
    return { metricStr: exposedName, scopeStr: scopeEntry.token, valueType: colEntry.valueType };
  }
  const metricEntry = catalog.metrics.get(lastName);
  if (metricEntry && !metricEntry.isFragmentRef) {
    if (metricEntry.paramKeys && metricEntry.paramKeys.length) {
      errors.push(_err(
        `'${lastName}' requires argument(s) — use '${lastName}(...)@${scopeEntry.token}', `
        + `dot-access has no way to pass arguments`
      ));
      return null;
    }
    return { metricStr: metricEntry.token, scopeStr: scopeEntry.token, valueType: metricEntry.valueType };
  }
  errors.push(_err(`unknown column '${lastName}' on scope '${scopeEntry.token}'`));
  return null;
}

function compileLeaf(node, catalog) {
  const errors = [];
  const metricEntry = _resolveMetric(node.metric, catalog, errors);
  const scopeResolved = metricEntry ? _resolveScopeRefFull(node.scope, catalog, errors) : null;
  if (errors.length) return { conditions: null, errors };
  _checkQuoteScopeRequiresSymbol(scopeResolved.scopeStr, scopeResolved.scopeFilter, errors);
  if (errors.length) return { conditions: null, errors };
  const metricStr = _compileMetricCallString(node.metric, metricEntry, errors);
  if (errors.length) return { conditions: null, errors };
  const conditions = _compileComparatorValue(metricStr, scopeResolved.scopeStr, metricEntry.valueType, node.op, node.value, errors);
  if (errors.length) return { conditions: null, errors };
  // Sprint 19 — a resolved qualifier filter is a SIBLING key of the
  // condition object the comparator just built, never folded into
  // `scope`/`metric` — mirrors the real backend leaf shape exactly.
  if (scopeResolved.scopeFilter) conditions.scope_filter = scopeResolved.scopeFilter;
  return { conditions, errors: [] };
}

/** Compile a bare `col_ref` leaf (Sprint 15), e.g.
 *  `positions.total.pnl <= -50000` — the mirror of `compileLeaf`, just
 *  resolved via `_resolveDotAccess` instead of `_resolveMetric` +
 *  `_resolveScopeString`. Shares `_compileComparatorValue` with
 *  `compileLeaf` so the two produce byte-identical JSON for the "same
 *  concept, different spelling" case (`positions.total.pnl <= -50000`
 *  vs. `pnl@positions.total <= -50000`). */
/** Sprint 21 — `quote.*` is instrument-rooted (market data belongs to no
 *  account) with no meaningful "every tracked symbol" aggregate the way
 *  `*.any_acct` works for accounts, so a `quote.*` leaf with no (or
 *  wildcard) `symbol:` qualifier has nothing to disambiguate WHICH
 *  tracked row it means. Mirrors the backend's own `agent_evaluator.
 *  validate()` guard byte-for-byte (same condition: `scope_filter` missing
 *  or its `symbol` key absent) — the exact symmetric-opposite direction of
 *  Sprint 19's "qualifier not valid for this scope" check above (a
 *  REQUIRED qualifier missing, instead of an unsupported one supplied).
 *  Called once scope + scope_filter are BOTH already resolved, from every
 *  leaf-producing compile path (`compileLeaf`, `compileColRef`, and both
 *  of `compileBetween`'s output shapes) so a `quote.*` leaf can never
 *  reach the compiled JSON without a concrete symbol, regardless of which
 *  CLI spelling produced it. A no-op for every other scope family. */
function _checkQuoteScopeRequiresSymbol(scopeStr, scopeFilter, errors) {
  if (typeof scopeStr !== 'string' || !scopeStr.toLowerCase().startsWith('quote.')) return;
  if (scopeFilter && scopeFilter.symbol) return;
  errors.push(_err(
    `${scopeStr} requires a specific symbol: qualifier, e.g. `
    + `${scopeStr}.symbol:NIFTY25JULFUT — no 'any symbol' aggregate exists for market data`
  ));
}

function compileColRef(node, catalog) {
  const errors = [];
  const resolved = _resolveDotAccess(node.segments, catalog, errors);
  if (errors.length) return { conditions: null, errors };
  _checkQuoteScopeRequiresSymbol(resolved.scopeStr, resolved.scopeFilter, errors);
  if (errors.length) return { conditions: null, errors };
  const conditions = _compileComparatorValue(resolved.metricStr, resolved.scopeStr, resolved.valueType, node.op, node.value, errors);
  if (errors.length) return { conditions: null, errors };
  if (resolved.scopeFilter) conditions.scope_filter = resolved.scopeFilter;
  return { conditions, errors: [] };
}

const _BETWEEN_FLIP = { '<': '>', '<=': '>=', '>': '<', '>=': '<=' };

function compileBetween(node, catalog) {
  const errors = [];
  let metricStr, scopeStr, valueType, scopeFilter;
  if (node.colSegments) {
    const resolved = _resolveDotAccess(node.colSegments, catalog, errors);
    if (errors.length) return { conditions: null, errors };
    ({ metricStr, scopeStr, valueType, scopeFilter } = resolved);
  } else {
    const metricEntry = _resolveMetric(node.metric, catalog, errors);
    const scopeResolved = metricEntry ? _resolveScopeRefFull(node.scope, catalog, errors) : null;
    if (errors.length) return { conditions: null, errors };
    metricStr = _compileMetricCallString(node.metric, metricEntry, errors);
    if (errors.length) return { conditions: null, errors };
    valueType = metricEntry.valueType;
    scopeStr = scopeResolved.scopeStr;
    scopeFilter = scopeResolved.scopeFilter;
  }
  _checkQuoteScopeRequiresSymbol(scopeStr, scopeFilter, errors);
  if (errors.length) return { conditions: null, errors };

  if (node.value1.type !== 'number' || node.value2.type !== 'number') {
    errors.push(_err('chained ("between") comparison literals must be numeric'));
    return { conditions: null, errors };
  }
  if (valueType && valueType !== 'number') {
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
    const conditions = { metric: metricStr, scope: scopeStr, op: 'between', value: [low, high] };
    if (scopeFilter) conditions.scope_filter = scopeFilter;
    return { conditions, errors: [] };
  }
  // Strict / mixed-strictness chain — the backend's `between` op is ALWAYS
  // inclusive (`lambda a,b: b[0] <= a <= b[1]`), so compiling a strict
  // chain to it would silently loosen the condition. Compile to an
  // explicit `all` of the two original (direction-correct) comparisons
  // instead — see .claude/PLAN.md Step 5's "between" design note.
  const leaf1 = { metric: metricStr, scope: scopeStr, op: _BETWEEN_FLIP[node.op1], value: node.value1.value };
  const leaf2 = { metric: metricStr, scope: scopeStr, op: node.op2, value: node.value2.value };
  if (scopeFilter) { leaf1.scope_filter = scopeFilter; leaf2.scope_filter = scopeFilter; }
  return { conditions: { all: [leaf1, leaf2] }, errors: [] };
}

/** Compile a bare-call `{type:'ref', call}` leaf (Sprint 10) — the mirror of
 *  `_decompileRef` below. A name resolving to a catalog METRIC entry with
 *  `isFragmentRef` compiles to `{"$ref": <token>}`; a name resolving to a
 *  REAL metric (missing its "@") gets the ORIGINAL `parseMetricRef` error
 *  message, just surfaced at compile time instead of parse time (the "@" is
 *  genuinely required for a real metric, so this is not a regression — see
 *  the file-header EBNF's `leaf` production comment); anything else falls
 *  through to the same "unknown metric" / kind-mismatch wording
 *  `_resolveMetric` already uses. */
function compileRef(node, catalog) {
  const errors = [];
  const name = node.call.name;
  const metricEntry = catalog.metrics.get(name.toLowerCase());
  if (metricEntry && metricEntry.isFragmentRef) {
    if (node.call.args && node.call.args.length) {
      errors.push(_err(`'${name}' is a Token reference and takes no arguments`));
      return { conditions: null, errors };
    }
    return { conditions: { '$ref': metricEntry.token }, errors: [] };
  }
  if (metricEntry) {
    errors.push(_err(`expected "@" after metric '${metricEntry.token}'`));
    return { conditions: null, errors };
  }
  const other = _describeKind(name, catalog);
  errors.push(_err(other
    ? `'${name}' is ${other} — only metrics/Tokens are allowed in a WHEN condition`
    : `unknown metric '${name}'`));
  return { conditions: null, errors };
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
  if (node.type === 'colref') return compileColRef(node, catalog);
  if (node.type === 'ref') return compileRef(node, catalog);
  return { conditions: null, errors: [_err('internal: unknown condition node type')] };
}

function compileCondition(node, catalog) {
  if (node.type === 'always') return { conditions: { ...ALWAYS_LEAF }, errors: [] };
  return compileCondNode(node, catalog);
}

function _compileGenericActionParams(call, entry, rowRefCtx) {
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
    // Sprint 16 — a bare NAME matching a known exposed-row column (e.g.
    // `account=account_id`) is tried FIRST, before the ordinary
    // enum/number/string coercion — see `_resolveRowRefOrNull`'s own
    // header comment for why this never collides with an existing
    // literal/enum value.
    const rowRef = _resolveRowRefOrNull(v, rowRefCtx, `${entry.token}.${k}`);
    if (rowRef) {
      if (rowRef.error) errors.push(rowRef.error); else params[k] = rowRef.value;
      continue;
    }
    // Sprint [GIVEN-expr] — same priority slot as the `$row` check above,
    // for an `'expr'`-kind GIVEN binding referenced by bare NAME — see
    // `_resolveGivenExprRefOrNull`'s own header comment.
    const givenRef = _resolveGivenExprRefOrNull(v, call._bindingsAt, entry.paramsSchema[k], `${entry.token}.${k}`);
    if (givenRef) {
      if (givenRef.error) errors.push(givenRef.error); else params[k] = givenRef.value;
      continue;
    }
    const coerced = _coerceValueForSpec(v, entry.paramsSchema[k], k, entry.token);
    if (coerced.error) errors.push(coerced.error); else params[k] = coerced.value;
  }
  if (errors.length) return { params: null, errors };
  // Sprint 17 — generic conditional-requirement + mutual-exclusion checks,
  // driven by the catalog's own declared `required_if`/`mutuallyExclusiveGroups`
  // (no action in today's catalog declares either through this generic
  // path — place_order is handled by `_compileOrderActionParams` below —
  // but any future action that does gets both checks for free).
  errors.push(..._checkRequiredIf(entry.paramKeys, entry.paramsSchema, (k) => (k in params ? params[k] : undefined), entry.token));
  if (entry.mutuallyExclusiveGroups) {
    const presentKeys = new Set(Object.keys(params).map((k) => k.toLowerCase()));
    errors.push(..._checkMutexGroups(presentKeys, entry.mutuallyExclusiveGroups, entry.token));
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

// ═══════════════════════════════════════════════════════════════════════
// Shared TP/SL/Wing kwarg vocabulary (Sprint 8) — ONE vocabulary for both
// `order(...)` (ad-hoc, per-order override) and `bracket(...)` (saved,
// reusable preset). Field-name mapping CONFIRMED directly against the
// real backend structs (`backend/api/schemas.py`):
//   TicketOrderRequest:  tp_pct_override, sl_pct_override,
//                        wing_premium_pct_override, wing_strike_offset_override,
//                        wing_max_spread_pct_override, sl_trail_pct_override,
//                        tp_scales_json_override (str — JSON-encoded)
//   OrderTemplateCreate: tp_pct, sl_pct, wing_premium_pct, wing_strike_offset,
//                        wing_max_spread_pct, tp_order_type, sl_trail_pct,
//                        tp_scales_json (str — JSON-encoded)
// Every one of the above is CONFIRMED present on both structs, with the
// SAME plain-name/`_override`-suffix convention, EXCEPT `tp_order_type`:
// `TicketOrderRequest` has NO `tp_order_type` / `tp_order_type_override`
// field at all today (only `OrderTemplateCreate`/`OrderTemplatePatch`
// declare it). `tp_order_type_override` below is this sprint's
// extrapolation of the same convention every OTHER sibling field already
// confirms — FLAGGED here and in this sprint's handback report for the
// backend agent to verify/add; order(...)'s compiled ticket output will
// carry a field the real schema doesn't (yet) have until that's resolved. ─

const _TPSLWING_KWARGS = Object.freeze([
  'tp_pct', 'sl_pct', 'wing_premium_pct', 'wing_strike_offset',
  'wing_max_spread_pct', 'tp_order_type', 'sl_trail_pct', 'tp_scales_json',
]);

/** Recursively unwrap a ValueNode (including the Sprint 8 'object'/'list'
 *  shapes) to its plain-JS equivalent — used only for `tp_scales_json`,
 *  the one value shape that can contain an 'object' node; every other
 *  shared kwarg is a flat number/enum handled directly via
 *  `_coerceValueForSpec`. */
function _unwrapValueNode(v) {
  if (!v || typeof v !== 'object') return v;
  if (v.type === 'list') return v.value.map(_unwrapValueNode);
  if (v.type === 'object') {
    const out = {};
    for (const [k, vv] of Object.entries(v.value)) out[k] = _unwrapValueNode(vv);
    return out;
  }
  return v.value;
}

/** Strip the shared TP/SL/Wing kwargs (Sprint 8) out of a call's arg
 *  list — CLI-only shared vocabulary, never a literal key in either
 *  destination's own catalog-declared params_schema — same "strip sugar
 *  before generic key-mapping" pattern `_splitLotsArg` already
 *  established. Returns `{ tpSlWingArgs, call }`. */
function _splitTpSlWingArgs(call) {
  const picked = [];
  const rest = [];
  for (const a of (call.args || [])) {
    if (a.key && _TPSLWING_KWARGS.includes(a.key.toLowerCase())) picked.push(a);
    else rest.push(a);
  }
  return { tpSlWingArgs: picked, call: { ...call, args: rest } };
}

const _TP_ORDER_TYPE_SPEC = Object.freeze({ type: 'enum', enum: ['LIMIT', 'MARKET'] });
const _TPSLWING_NUMBER_SPEC = Object.freeze({ type: 'number' });

/** Compile the shared TP/SL/Wing kwargs to either destination's field
 *  names — the ONE mapping function both `order(...)` (`destKind:
 *  'ticket'` → `${kwarg}_override`) and `bracket(...)` (`destKind:
 *  'template'` → plain `${kwarg}`) call into, per the module-header note
 *  above. `wing_premium_pct` and `wing_strike_offset` are mutually
 *  exclusive — Sprint 17 generalizes this from a hand-rolled pair check to
 *  a read of the catalog's own declared `mutexGroups` (the `place_order`
 *  token's `mutuallyExclusiveGroups`, e.g.
 *  `[["wing_premium_pct_override","wing_strike_offset_override"]]`):
 *  since the shared short-kwarg vocabulary here (`wing_premium_pct`, no
 *  suffix) is EXACTLY the catalog's `_override`-suffixed field name minus
 *  that suffix (the same convention `outKey` below encodes the other
 *  direction), each group's field names are normalized by stripping a
 *  trailing `_override` before comparing against `byKey` — this keeps
 *  `_checkMutexGroups` itself naming-convention-agnostic while still
 *  working for BOTH `destKind`s (bracket's plain template names have no
 *  catalog-side mutex declaration of their own, so this reuses
 *  place_order's ticket-side group as the one shared business rule).
 *  `mutexGroups` is optional — omitted/`null` means "no groups to check"
 *  (never throws either way).
 *  `tp_scales_json` is a `str` (JSON-encoded) field on BOTH destinations —
 *  each `{...}` entry is now per-key typed via `OBJECT_LITERAL_SCHEMAS`/
 *  `_coerceObjectLiteralEntry` (Sprint 17) instead of a raw untyped
 *  unwrap, so a typo'd value (e.g. a string where a number is required)
 *  is a clear compile error naming the entry index and key. Returns
 *  `{ fields, errors }` — `fields` is a plain object meant to be spread
 *  into the caller's compiled leg/template object; never throws. */
function _compileTpSlWingFields(tpSlWingArgs, destKind, label, mutexGroups) {
  const errors = [];
  const byKey = new Map();
  for (const a of tpSlWingArgs || []) {
    const k = a.key.toLowerCase();
    if (byKey.has(k)) { errors.push(_err(`${label}: duplicate parameter '${k}'`)); continue; }
    byKey.set(k, a.value);
  }
  if (mutexGroups) {
    const normalizedGroups = mutexGroups.map((g) => (g || []).map((f) => String(f).toLowerCase().replace(/_override$/, '')));
    errors.push(..._checkMutexGroups(new Set(byKey.keys()), normalizedGroups, label));
  }
  if (errors.length) return { fields: null, errors };

  const fields = {};
  for (const kwarg of _TPSLWING_KWARGS) {
    if (!byKey.has(kwarg)) continue;
    const valueNode = byKey.get(kwarg);
    const outKey = destKind === 'ticket' ? `${kwarg}_override` : kwarg;

    if (kwarg === 'tp_scales_json') {
      if (valueNode.type !== 'list' || !valueNode.value.every((v) => v && v.type === 'object')) {
        errors.push(_err(`${label}: tp_scales_json must be a list of {...} entries, e.g. [{at_pct=30, close_pct=50}]`));
        continue;
      }
      const schema = OBJECT_LITERAL_SCHEMAS.tp_scales_json;
      const entries = [];
      let hadEntryError = false;
      valueNode.value.forEach((entryNode, i) => {
        const r = _coerceObjectLiteralEntry(entryNode, schema, `${label}.tp_scales_json[${i}]`);
        if (r.errors.length) { errors.push(...r.errors); hadEntryError = true; } else entries.push(r.value);
      });
      if (hadEntryError) continue;
      fields[outKey] = JSON.stringify(entries);
      continue;
    }
    const spec = kwarg === 'tp_order_type' ? _TP_ORDER_TYPE_SPEC : _TPSLWING_NUMBER_SPEC;
    const coerced = _coerceValueForSpec(valueNode, spec, kwarg, label);
    if (coerced.error) { errors.push(coerced.error); continue; }
    fields[outKey] = coerced.value;
  }
  return { fields: errors.length ? null : fields, errors };
}

/** Compile a `DO order(...)` / `DO place_order(...)` call to an agent
 *  action's `{"type":"place_order","params":{...}}` entry. `lots=N`
 *  converts to the REAL schema's `qty` param as CONTRACTS
 *  (`qty = lots * lot_size`) — this is place_order's own documented
 *  convention (order_fields.yaml: "qty: Number of lots × lot size"),
 *  DISTINCT from the ticket/basket convention below (where `quantity` is
 *  lots directly, no multiplication) — see `_compileOrderCallToLeg`. */
function _compileOrderActionParams(call, entry, opts, rowRefCtx) {
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
    // Sprint 16 — see `_compileGenericActionParams`'s identical guard;
    // place_order/order(...) gets the same bare-NAME ($row) recognition
    // for every non-qty param (qty keeps its own lots/qty-specific path
    // below, untouched).
    const rowRef = _resolveRowRefOrNull(v, rowRefCtx, `${entry.token}.${k}`);
    if (rowRef) {
      if (rowRef.error) errors.push(rowRef.error); else params[k] = rowRef.value;
      continue;
    }
    // Sprint [GIVEN-expr] — see `_compileGenericActionParams`'s identical
    // guard; same priority slot as the `$row` check above.
    const givenRef = _resolveGivenExprRefOrNull(v, call._bindingsAt, entry.paramsSchema[k], `${entry.token}.${k}`);
    if (givenRef) {
      if (givenRef.error) errors.push(givenRef.error); else params[k] = givenRef.value;
      continue;
    }
    const coerced = _coerceValueForSpec(v, entry.paramsSchema[k], k, entry.token);
    if (coerced.error) errors.push(coerced.error); else params[k] = coerced.value;
  }
  if (errors.length) return { params: null, errors };

  // Sprint 17 — generic conditional-requirement + mutual-exclusion checks
  // against place_order's own real catalog schema (`price`/`trigger_price`'s
  // `required_if`, the token-level `mutuallyExclusiveGroups`), driven
  // directly by `entry.paramsSchema`/`entry.mutuallyExclusiveGroups` — no
  // hand-checked field names. `DO place_order(...)`/`DO order(...)` has no
  // short-kwarg TP/SL/Wing sugar (unlike bare `order(...)` below), so the
  // operator types the real `_override`-suffixed field names directly;
  // `params` already holds exactly those, unnormalized.
  errors.push(..._checkRequiredIf(entry.paramKeys, entry.paramsSchema, (k) => (k in params ? params[k] : undefined), entry.token));
  if (entry.mutuallyExclusiveGroups) {
    const presentKeys = new Set(Object.keys(params).map((k) => k.toLowerCase()));
    errors.push(..._checkMutexGroups(presentKeys, entry.mutuallyExclusiveGroups, entry.token));
  }
  if (errors.length) return { params: null, errors };

  if (lotsArgs.length === 1) {
    const lotsVal = lotsArgs[0].value;
    // Sprint 20 (item 1) — reject a fractional lot count rather than
    // silently rounding it into `Math.round(lots * lot_size)` below;
    // same unit-safety class as the qty-vs-contracts fix elsewhere in
    // this item (a trader typing `lots=1.5` almost certainly meant
    // something else, and a silent round is exactly the kind of
    // unannounced qty mutation this sprint exists to close).
    if (lotsVal.type !== 'number' || !(lotsVal.value > 0) || !Number.isInteger(lotsVal.value)) {
      errors.push(_err(`${entry.token}: lots must be a positive whole number`));
      return { params: null, errors };
    }
    const symbol = params.symbol;
    // Sprint 16 — `symbol` may now be a `$row` marker object (not a
    // plain string) when $row-bound — `lots=` needs a LITERAL symbol to
    // look up a lot size at COMPILE time, which a row-bound value can
    // never provide (the real symbol is only known at dispatch time,
    // per matched row). Fails loud with a clear redirect to `qty=`
    // instead of silently stringifying the marker object into
    // `lotSizeOf('[object Object]')`.
    if (!symbol || typeof symbol !== 'string') {
      errors.push(_err(
        `${entry.token}: lots= requires a literal (quoted) symbol to resolve lot size at compile `
        + `time — a $row-bound or missing symbol can't be resolved here; use qty= instead`
      ));
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
    // Sprint 16 — `qty=qty` ($row-bound, e.g. `close_position`'s matched
    // row carries its own `qty` column — CONTRACTS, same unit
    // `place_order.qty` already uses) is tried before the ordinary
    // numeric coercion, same as every other param above.
    const qtyRowRef = _resolveRowRefOrNull(qtyVal, rowRefCtx, `${entry.token}.qty`);
    if (qtyRowRef) {
      if (qtyRowRef.error) { errors.push(qtyRowRef.error); return { params: null, errors }; }
      params.qty = qtyRowRef.value;
    } else {
      // Sprint [GIVEN-expr] — same priority slot as the two generic-param
      // loops above; `qty` is numeric, so this always surfaces the
      // numeric/enum compile-time rejection for an operator-expression
      // label here (there is no string-typed qty to legitimately accept).
      const qtyGivenRef = _resolveGivenExprRefOrNull(qtyVal, call._bindingsAt, entry.paramsSchema.qty, `${entry.token}.qty`);
      if (qtyGivenRef) {
        if (qtyGivenRef.error) { errors.push(qtyGivenRef.error); return { params: null, errors }; }
        params.qty = qtyGivenRef.value;
      } else {
        const coerced = _coerceValueForSpec(qtyVal, entry.paramsSchema.qty, 'qty', entry.token);
        if (coerced.error) { errors.push(coerced.error); return { params: null, errors }; }
        params.qty = coerced.value;
      }
    }
  }
  return { params, errors: [] };
}

// ── ALERT `msg=` default (Sprint 6) ─────────────────────────────────────
// `msg`/`message` is never part of a real channel's own `params_schema`
// (confirmed against the live catalog's telegram/email/websocket/log/inapp
// rows — none declare any params at all today), so it is stripped out of
// a call's own args HERE, inside compileAlertClause, BEFORE the call ever
// reaches `_compileGenericActionParams` — never inside that shared helper
// itself, which DO's `emit_log(message=...)` also uses and which DOES
// declare a real `message` param that must keep working unmodified.
const _MESSAGE_KEYS = new Set(['msg', 'message']);
// Sprint 20 (item 4) — 'freetext' (bare NAME or quoted STRING), not the
// stricter 'string' (quote-required) type: `msg=`/`message=` is pure
// notification text with no $row-binding concept at all (unlike
// account/symbol/qty action params, where a bare word is ambiguous with
// a $row column reference and MUST stay quote-required), so there is
// no reason to force quotes on a single bare word — same rule
// `log_tag=`/`render=`/meta values (`#name=`/`#desc=`/`#tag=`) already
// follow. Closes the "msg=hello doesn't [accept bare]" inconsistency.
const _MESSAGE_SPEC = Object.freeze({ type: 'freetext' });

/** Resolve ONE call from a channel-call-list — the shared half of
 *  `alert_clause`'s own call_list production AND the new action-level
 *  `notify=[...]` (Sprint — see this file's CLAUDE.md entry for the
 *  full design) — against `catalog.channels`. This is the ONE place a
 *  channel name is looked up and its own declared `params_schema`
 *  resolved (via `_compileGenericActionParams`, generically, with zero
 *  channel-specific code), so a brand-new channel added only to the
 *  catalog works in EITHER caller with no frontend code changes at all.
 *
 *  `stripKeys` (optional `Set<lowercased string>`) names args to pull
 *  out of the call BEFORE the generic params_schema compile runs —
 *  ALERT's own `msg=`/`message=` sugar (`_MESSAGE_KEYS`) is never a
 *  real channel param (see that const's own header comment), so ALERT
 *  passes `_MESSAGE_KEYS` here; `notify=` has no such sugar and omits
 *  this entirely. `contextLabel` only affects the "is ${other} — only
 *  channels are allowed in ${contextLabel}" wording, so each caller's
 *  error message still names its own clause.
 *
 *  Returns `{ errors }` (non-empty on failure), or one of:
 *    `{ errors: [], isFragmentRef: true, token }` — a bare Token
 *      reference (Sprint 10) — no params/message machinery applies at
 *      all; it expands into OTHER channels server-side
 *      (`template_registry.py:resolve_events`).
 *    `{ errors: [], isFragmentRef: false, entry, params, strippedArgs }`
 *      — a real channel call; `params` is the generically-resolved
 *      param object (possibly `{}`); `strippedArgs` is whatever
 *      `stripKeys` pulled out (always `[]` when `stripKeys` is
 *      omitted) — left for the caller to interpret (ALERT's own `msg`
 *      precedence logic; `notify=` never receives a non-empty array). */
function _resolveChannelCall(call, catalog, stripKeys, contextLabel = 'here') {
  const entry = catalog.channels.get(call.name.toLowerCase());
  if (!entry) {
    const other = _describeKind(call.name, catalog);
    return { errors: [_err(other
      ? `'${call.name}' is ${other} — only channels are allowed in ${contextLabel}`
      : `unknown channel '${call.name}'`)] };
  }

  // Sprint 10 — a bare Token reference (`isFragmentRef`) resolves to
  // `{"$ref": <token>}` directly, bypassing the own-message/params
  // machinery below entirely — a Token reference expands into OTHER
  // channels server-side (`template_registry.py:resolve_events`), so it
  // can never carry its own params or its own `msg=`.
  if (entry.isFragmentRef) {
    if (call.args && call.args.length) {
      return { errors: [_err(`'${call.name}' is a Token reference and takes no arguments`)] };
    }
    return { errors: [], isFragmentRef: true, token: entry.token };
  }

  const strippedArgs = stripKeys
    ? (call.args || []).filter((a) => a.key && stripKeys.has(a.key.toLowerCase()))
    : [];
  const strippedCall = stripKeys
    ? { ...call, args: (call.args || []).filter((a) => !(a.key && stripKeys.has(a.key.toLowerCase()))) }
    : call;
  const r = _compileGenericActionParams(strippedCall, entry);
  if (r.errors.length) return { errors: r.errors };
  return { errors: [], isFragmentRef: false, entry, params: r.params, strippedArgs };
}

/** Sprint [GIVEN-expr] — mirror of `_resolveGivenExprRefOrNull`, scoped to
 *  `msg=`/`message=` value positions (both the clause-level `ALERT msg=`
 *  default and a channel's own `msg=`/`message=` kwarg), which resolve
 *  through `_MESSAGE_SPEC` (`type: 'freetext'`) rather than a generic
 *  action-param schema and so never reach `_resolveGivenExprRefOrNull` at
 *  all. A bare NAME referencing an `'expr'`-kind GIVEN binding resolves to
 *  the same `{"$given": name}` marker (message text is always string-
 *  shaped, so there is no numeric/enum rejection case here — `msg=` isn't
 *  gated by `_coerceValueForSpec`'s type system the way action params
 *  are). Anything else (no match, or a binding that isn't `'expr'`-kind)
 *  falls through to the EXISTING `_coerceValueForSpec(valueNode,
 *  _MESSAGE_SPEC, ...)` path, unchanged — a pure-alias or literal/bareword
 *  `msg=` value compiles exactly as before. */
function _resolveMessageValue(valueNode, bindingsMap, key, label) {
  if (valueNode && valueNode.type === 'name' && bindingsMap) {
    const bound = bindingsMap.get(String(valueNode.value).toLowerCase());
    if (bound && bound.rhsKind === 'expr') return { value: { '$given': String(valueNode.value).toLowerCase() } };
  }
  return _coerceValueForSpec(valueNode, _MESSAGE_SPEC, key, label);
}

/** `alert_clause`'s per-channel message resolution (Sprint 6):
 *    1. a channel's OWN `msg=`/`message=` kwarg (either spelling) always
 *       wins over the clause-level default;
 *    2. otherwise the clause-level `msg=` default (`alertMsg`), if any;
 *    3. otherwise — but ONLY if `msg` was used SOMEWHERE in this clause at
 *       all (own or default) — a compile error naming the channel;
 *    4. if `msg` was used NOWHERE in the clause, every channel compiles
 *       with NO `message` key at all — byte-for-byte unchanged from the
 *       pre-Sprint-6 output, since the backend treats an absent `message`
 *       key as "use the auto-composed default" for the vast majority of
 *       real, existing agents.
 *
 *  `bindingsMap` (Sprint [GIVEN-expr], new 4th param, optional — every
 *  pre-existing call site that doesn't pass it keeps working unchanged,
 *  `_resolveMessageValue` degrades to the old `_coerceValueForSpec`-only
 *  path when it's absent) is the full statement's bound-label map
 *  (`label.toLowerCase() -> binding`) — safe to use in full here (not a
 *  per-call `_bindingsAt` snapshot) because GIVEN's own preamble always
 *  textually precedes ALERT/DO, so every binding is already fully
 *  resolved by the time either clause's message is compiled. */
function compileAlertClause(alertCalls, alertMsg, catalog, bindingsMap) {
  if (alertCalls === 'nop') return { events: [], errors: [] };
  const errors = [];

  let defaultMessage = null;
  if (alertMsg != null) {
    const r = _resolveMessageValue(alertMsg, bindingsMap, 'msg', 'ALERT');
    if (r.error) errors.push(r.error);
    else defaultMessage = r.value;
  }

  const ownMsgArgsByCall = alertCalls.map((call) =>
    (call.args || []).filter((a) => a.key && _MESSAGE_KEYS.has(a.key.toLowerCase())));
  const anyMsgUsed = defaultMessage != null
    || ownMsgArgsByCall.some((args) => args.length > 0);

  const events = [];
  for (let i = 0; i < alertCalls.length; i++) {
    const call = alertCalls[i];
    const resolved = _resolveChannelCall(call, catalog, _MESSAGE_KEYS, 'ALERT');
    if (resolved.errors.length) { errors.push(...resolved.errors); continue; }

    if (resolved.isFragmentRef) {
      events.push({ '$ref': resolved.token });
      continue;
    }

    const { entry, params, strippedArgs } = resolved;
    const msgArgs = strippedArgs;
    if (msgArgs.length > 1) {
      errors.push(_err(`${entry.token}: duplicate parameter 'msg'/'message'`));
      continue;
    }
    let ownMessage = null;
    if (msgArgs.length === 1) {
      const r = _resolveMessageValue(msgArgs[0].value, bindingsMap, msgArgs[0].key.toLowerCase(), entry.token);
      if (r.error) { errors.push(r.error); continue; }
      ownMessage = r.value;
    }

    let resolvedMessage = null;
    if (ownMessage != null) {
      resolvedMessage = ownMessage;
    } else if (defaultMessage != null) {
      resolvedMessage = defaultMessage;
    } else if (anyMsgUsed) {
      errors.push(_err(`channel '${entry.token}' has no message — set msg= on ALERT or on this channel directly`));
      continue;
    }

    const event = { channel: entry.token, enabled: true, ...params };
    if (resolvedMessage != null) event.message = resolvedMessage;
    events.push(event);
  }
  return { events: errors.length ? null : events, errors };
}

// ═══════════════════════════════════════════════════════════════════════
// Sprint 16 — matched-row ($row) binding for DO-clause action params.
// ═══════════════════════════════════════════════════════════════════════
//
// Mirrors the backend marker shape EXACTLY (backend/api/algo/actions.py:
// resolve_row_ref_params / _is_row_ref_marker): a bare, unquoted NAME
// written as a DO-clause action argument value — e.g.
// `close_position(account=account_id, symbol=symbol)` — compiles to the
// single-key marker `{"$row": "<exposed_name>"}`, never a plain string.
// `result.<name>` is accepted as an alias for the IDENTICAL marker (pure
// spelling choice, same resolution either way).
//
// A bare NAME is recognised as a $row CANDIDATE only when it matches one
// of Sprint 15's exposed-column names for SOME scope anywhere in the
// catalog (`_allExposedColumnNames`) — this keeps every pre-existing
// enum/free-text bare-NAME error path (`_coerceValueForSpec`) completely
// unchanged for every name that doesn't collide (the backend deliberately
// keeps `account` itself out of the exposed-name vocabulary for exactly
// this reason — see grammar.py's own comment on `_COL_ACCOUNT_ID`; the
// real column is only ever exposed as `account_id`). Once recognised as
// a candidate, resolution ALWAYS returns success or a specific compile
// error — never silently falls through to a different, confusing error.
//
// Three compile-time guards (mirror the runtime backstop in
// `actions.py:resolve_row_ref_params` plus the operator's explicit scope
// rules):
//   1. AGGREGATE scope (any leaf scoped `*.total`) — no real per-row
//      identity exists; `$row` is rejected outright, regardless of which
//      field is referenced (even `account_id`, which IS technically
//      present in an aggregate scope's own exposed-column schema — the
//      backend's own `_ae_dispatch_actions` never binds a row for an
//      aggregate match, so compiling to `$row` here would be a silent
//      runtime skip, not a working reference).
//   2. Per-LEAF-scope resolution — a condition tree can combine leaves
//      across DIFFERENT scopes (e.g. an `any[]` of a `positions.any_acct`
//      leaf and a `funds.any_acct` leaf); the referenced name must be
//      exposed on EVERY leaf's own scope in the tree, not just one, since
//      there is no single well-defined "the agent's scope" in general.
//   3. WATCHLIST scope (any leaf scoped `watchlist.*`) — a watchlist row
//      carries no account/qty identity suitable for an order-placing
//      action, even for a name (`symbol`) that IS present in its own
//      exposed-column schema.

/** Union of every exposed-column PLAIN name (the part after the scope
 *  prefix) registered anywhere in the catalog, lowercased — e.g. `pnl`,
 *  `account_id`, `symbol`. Exposed names never contain a `.` themselves
 *  (enforced on the backend — see grammar.py's own naming-rule comment),
 *  so the plain name is always the token's LAST dot-segment, regardless
 *  of how many dot-segments the owning scope's own token has. */
function _allExposedColumnNames(catalog) {
  const names = new Set();
  for (const entry of catalog.columns.values()) {
    const parts = String(entry.token).split('.');
    names.add(parts[parts.length - 1].toLowerCase());
  }
  return names;
}

function _isAggregateScope(scope) {
  return typeof scope === 'string' && scope.toLowerCase().endsWith('.total');
}

function _isWatchlistScope(scope) {
  return typeof scope === 'string' && scope.toLowerCase().startsWith('watchlist.');
}

/** Recursively collect every real leaf's `scope` string out of a
 *  COMPILED condition tree (`{all:[...]}` / `{any:[...]}` / `{not:...}` /
 *  a flat `{metric,scope,op,value}` leaf) into `out`, and flag two
 *  structural cases ANYWHERE in the tree (not just at the root) via
 *  `state` — both make a leaf's real per-row identity unknowable at
 *  compile time, and (per the backend) unknowable at RUNTIME too for
 *  `not`, so a `$row` reference must fail closed whenever either is
 *  present, even mixed into a larger tree alongside other, perfectly
 *  resolvable per-row leaves:
 *    - `state.hasNegation` — ANY `not` composite anywhere. Per the
 *      backend's own `_ae_fanout_matches` docstring, a `not` leaf
 *      produces a synthetic match with `account=None` — `_ae_dispatch_
 *      actions` treats that exactly like an aggregate match and never
 *      binds a row (`_matched_row=None`) — so a `$row` reference under
 *      a negated leaf would compile clean but ALWAYS skip at runtime.
 *      This is the exact "silent pass-through to a runtime skip" this
 *      sprint's guards exist to prevent — treated with the same
 *      "reject unconditionally, regardless of which field" severity as
 *      the aggregate-scope guard.
 *    - `state.hasTokenRef` — ANY `{"$ref": token}` Token reference
 *      anywhere. Its own scope is resolved server-side at RUNTIME
 *      against whatever condition the referenced Token itself declares
 *      — genuinely unknowable here, so the "name must be exposed on
 *      EVERY leaf's scope" check (guard 2) cannot be soundly performed
 *      when a leaf's scope isn't even visible, whether the Token is the
 *      ENTIRE condition or just one leaf mixed into a larger tree. */
function _collectLeafScopes(node, out, state) {
  if (!node || typeof node !== 'object') return;
  if (Array.isArray(node.all)) { for (const n of node.all) _collectLeafScopes(n, out, state); return; }
  if (Array.isArray(node.any)) { for (const n of node.any) _collectLeafScopes(n, out, state); return; }
  if (node.not) { state.hasNegation = true; _collectLeafScopes(node.not, out, state); return; }
  if ('$ref' in node) { state.hasTokenRef = true; return; }
  if (typeof node.scope === 'string') out.add(node.scope);
}

function _exposedColumnForScope(catalog, scope, lowerName) {
  return catalog.columns.get(`${scope}.${lowerName}`.toLowerCase()) || null;
}

/** Try to resolve `valueNode` as a Sprint-16 matched-row reference.
 *  Returns:
 *    - `null` — not a row-ref candidate at all (not a bare NAME, or the
 *      name matches no known exposed column anywhere, or no rowRefCtx
 *      was threaded in at all — e.g. ALERT-channel params/standalone
 *      `order(...)` statements never get this feature) — caller falls
 *      through to the ordinary `_coerceValueForSpec` path, byte-identical
 *      to pre-Sprint-16 behavior.
 *    - `{ value: {"$row": name} }` — resolved successfully.
 *    - `{ error }` — a definite row-ref-shaped value that FAILS one of
 *      the three guards — never falls through to a different error. */
function _resolveRowRefOrNull(valueNode, rowRefCtx, label) {
  if (!rowRefCtx || !valueNode || valueNode.type !== 'name') return null;
  const raw = valueNode.value;
  const aliasMatch = /^result\.(.+)$/i.exec(raw);
  const requested = aliasMatch ? aliasMatch[1] : raw;
  const lower = requested.toLowerCase();
  if (!rowRefCtx.allExposedNames.has(lower)) return null;

  const { leafScopes, catalog } = rowRefCtx;
  if (rowRefCtx.hasNegation) {
    return { error: _err(
      `${label}: '${requested}' cannot be bound from a matched row — WHEN condition contains a `
      + `negated (~) leaf (no real per-row identity at runtime — see agent_engine.py's _ae_fanout_matches)`
    ) };
  }
  if (rowRefCtx.hasTokenRef) {
    return { error: _err(
      `${label}: '${requested}' cannot be bound from a matched row — WHEN condition includes a `
      + `Token ($ref) reference whose own per-row scope isn't visible at compile time`
    ) };
  }
  if (leafScopes.size === 0) {
    return { error: _err(`${label}: '${requested}' needs a per-row WHEN condition to bind against`) };
  }
  for (const scope of leafScopes) {
    if (_isAggregateScope(scope)) {
      return { error: _err(
        `${label}: '${requested}' cannot be bound from a matched row — WHEN condition is scoped `
        + `to the aggregate '${scope}' (no real per-row identity)`
      ) };
    }
    if (_isWatchlistScope(scope)) {
      return { error: _err(
        `${label}: '${requested}' cannot be bound from a matched row — a watchlist row `
        + `('${scope}') has no account/qty identity suitable for an order-placing action`
      ) };
    }
  }
  let canonicalName = null;
  for (const scope of leafScopes) {
    const colEntry = _exposedColumnForScope(catalog, scope, lower);
    if (!colEntry) {
      return { error: _err(`${label}: '${requested}' is not an exposed column for scope '${scope}'`) };
    }
    if (canonicalName === null) canonicalName = String(colEntry.token).split('.').pop();
  }
  return { value: { '$row': canonicalName } };
}

/** Sprint [GIVEN-expr] — mirror of `_resolveRowRefOrNull` above, but for
 *  an `'expr'`-kind (operator-expression) GIVEN binding referenced by
 *  bare NAME as an action-param value, e.g. `DO order(..., symbol=sym,
 *  ...)` where `sym` was computed via `GIVEN sym = ...`. Returns:
 *    - `null` — not a GIVEN-expr-ref candidate at all (not a bare NAME,
 *      no `bindingsAt` map threaded in, or the name matches no binding /
 *      matches a binding that isn't `'expr'`-kind) — caller falls through
 *      to the ordinary `_coerceValueForSpec` path, unaffected.
 *    - `{ value: {"$given": name} }` — resolved successfully (only when
 *      the target param's own declared schema `type` is `'string'` —
 *      the one compile-time guard this sprint's design mandates: a
 *      computed value can never feed a numeric/enum field).
 *    - `{ error }` — a definite GIVEN-expr-ref whose target param is
 *      NOT `string`-typed — never falls through to a different error. */
function _resolveGivenExprRefOrNull(valueNode, bindingsAt, spec, label) {
  if (!bindingsAt || !valueNode || valueNode.type !== 'name') return null;
  const lower = String(valueNode.value).toLowerCase();
  const bound = bindingsAt.get(lower);
  if (!bound || bound.rhsKind !== 'expr') return null;
  if (spec && spec.type === 'string') return { value: { '$given': lower } };
  return { error: _err(`${label}: GIVEN label '${valueNode.value}' is a computed value; it cannot be used for a numeric/enum field.`) };
}

// ── `notify=[...]` — universal action-level kwarg (Part B, 2026-10) ────
// Available on EVERY action-kind token (`order`/`place_order`,
// `close_position`, `cancel_order`, `modify_order`, `cancel_all_orders`,
// `chase_close_positions`, `expiry_auto_close`, `monitor_order`,
// `deactivate_agent`, `set_flag`, `emit_log` — every row the live
// catalog's `grammar_kind=action` seeds today, per
// `backend/config/grammars/agent_grammar.yaml`) with ZERO per-action
// code: `notify=` is stripped out of the call's own args HERE, in
// `compileDoClause`, BEFORE any per-type dispatch (place_order/emit_log/
// generic) ever sees the call — same "strip sugar before generic
// key-mapping" pattern `_splitLotsArg`/`_splitTpSlWingArgs`/
// `_MESSAGE_KEYS` already established — so no action's own
// `params_schema` needs to declare it. Compiles to a SIBLING key on the
// action's own compiled dict (`{"type":...,"params":{...},"notify":[...]}`
// ) — NEVER written inside `params` — using the SAME
// `_resolveChannelCall` helper (and underlying `catalog.channels`
// resolution) ALERT's own channel-call-list already uses, so a
// brand-new channel added only to the backend catalog works here with
// zero frontend code changes.
const _NOTIFY_KEY = 'notify';

/** Strip any `notify=[...]` arg out of a DO-clause action call — see the
 *  section header above. Returns `{ notifyArgs, call }`; `notifyArgs` is
 *  the (0 or more) matching ArgEntry(s) — more than one is a
 *  duplicate-parameter compile error, surfaced by the caller. */
function _splitNotifyArg(call) {
  const notifyArgs = (call.args || []).filter((a) => a.key && a.key.toLowerCase() === _NOTIFY_KEY);
  const rest = (call.args || []).filter((a) => !(a.key && a.key.toLowerCase() === _NOTIFY_KEY));
  return { notifyArgs, call: { ...call, args: rest } };
}

/** Compile a `notify=[...]` arg's already-parsed CallNode[] value (see
 *  `_parseNotifyListArg`) against `catalog.channels` — reuses
 *  `_resolveChannelCall` with no `stripKeys` (notify has no `msg=`-style
 *  sugar of its own). Returns `{ notify, errors }` — `notify` is the
 *  compiled sibling-key array (`null` on any error): each entry is
 *  either `{"$ref": token}` (a bare Token reference) or `{"channel":
 *  token}` / `{"channel": token, "params": {...}}` (params present only
 *  when the call declared at least one — never an empty `{}`). */
function _compileNotifyClause(valueNode, catalog, label) {
  if (!valueNode || valueNode.type !== 'list' || !Array.isArray(valueNode.value)) {
    return { notify: null, errors: [_err(`${label}: notify must be a list of channel calls, e.g. notify=[telegram, ntfy(priority="urgent")]`)] };
  }
  const errors = [];
  const notify = [];
  for (const call of valueNode.value) {
    const r = _resolveChannelCall(call, catalog, null, 'notify=[...]');
    if (r.errors.length) { errors.push(...r.errors); continue; }
    if (r.isFragmentRef) { notify.push({ '$ref': r.token }); continue; }
    const out = { channel: r.entry.token };
    if (r.params && Object.keys(r.params).length) out.params = r.params;
    notify.push(out);
  }
  return { notify: errors.length ? null : notify, errors };
}

/** Compile ONE call against the real action catalog (`order`/`place_order`,
 *  `log` (the renamed `emit_log`), or any other generic action) — the
 *  pre-[grammar-unify] body of `compileDoClause`'s per-call loop,
 *  extracted unchanged so `_compileSharedCall` (below) can reuse it from
 *  BOTH the preamble and DO positions. Returns `{action, errors}` —
 *  `action` is `null` on any error. Does NOT handle `alert`/`bracket`/
 *  `debug` — those are intercepted one level up, in `_compileSharedCall`,
 *  before this function is ever called, since none of the three is a
 *  real catalog-action lookup. */
function _compileCatalogActionCall(rawCall, catalog, opts, rowRefCtx) {
  const { notifyArgs, call } = _splitNotifyArg(rawCall);
  if (notifyArgs.length > 1) {
    return { action: null, errors: [_err(`${call.name}: duplicate parameter 'notify'`)] };
  }

  const errors = [];
  const rawName = call.name.toLowerCase();
  // Sprint [order-unify] — BOTH typed spellings (`order`/`place_order`)
  // resolve to the exact same schema-lookup key for compiling params;
  // `isOrderAction` (not `lookupName === 'place_order'`) now drives the
  // branch dispatch below, since the EMITTED `type` is always the new
  // canonical `'order'` regardless of which spelling was typed or which
  // catalog row actually matched (tolerant lookup — tries a literal
  // `order` catalog row first, in case the backend has seeded one,
  // falling back to the guaranteed-present `place_order` row otherwise).
  const isOrderAction = rawName === 'order' || rawName === 'place_order';
  const lookupName = isOrderAction ? 'order' : rawName;
  const entry = isOrderAction
    ? (catalog.actions.get('order') || catalog.actions.get('place_order'))
    : catalog.actions.get(lookupName);
  if (!entry) {
    const other = _describeKind(call.name, catalog);
    errors.push(_err(other
      ? `'${call.name}' is ${other} — only actions are allowed here`
      : `unknown action '${call.name}'`));
    return { action: null, errors };
  }

  let action = null;
  if (isOrderAction) {
    const r = _compileOrderActionParams(call, entry, opts, rowRefCtx);
    errors.push(...r.errors);
    // Sprint [order-unify] — `order` is the sole name the compiler ever
    // EMITS going forward, regardless of which spelling was typed
    // (`order(...)` or `place_order(...)` both compile identically) —
    // never `'place_order'`. The backend's Phase-A dispatch tables
    // accept both strings identically.
    if (!r.errors.length) action = { type: 'order', params: r.params };
  } else if (lookupName === 'log') {
    // item 7 (backend) — the catalog/dispatch token renamed
    // `emit_log` -> `log`; the Python resolver function itself (and
    // this helper's own name) stays `emit_log`/`_compileEmitLogActionParams`
    // (dotted resolver paths don't need to match the DSL token name —
    // same precedent as `help`/`help_lookup`), only the DSL-visible
    // token/type string changes.
    const r = _compileEmitLogActionParams(call, entry, rowRefCtx);
    errors.push(...r.errors);
    if (!r.errors.length) action = { type: 'log', params: r.params };
  } else {
    const r = _compileGenericActionParams(call, entry, rowRefCtx);
    errors.push(...r.errors);
    if (!r.errors.length) action = { type: entry.token, params: r.params };
  }
  if (!action) return { action: null, errors };

  if (notifyArgs.length === 1) {
    const nr = _compileNotifyClause(notifyArgs[0].value, catalog, entry.token);
    errors.push(...nr.errors);
    if (nr.errors.length) return { action: null, errors };
    action = { ...action, notify: nr.notify };
  }
  return { action, errors };
}

/** Compile `alert(...)` written as an ORDINARY call (Sprint
 *  [grammar-unify]) back into the EXACT SAME `{events, errors}` shape
 *  `compileAlertClause` always produced for the old `ALERT <body>`
 *  clause — reusing that function completely unchanged. `call._alertBody`
 *  (set by `parseCall`'s own `alert`-specific branch) is the parsed item
 *  list from INSIDE the parens — the identical `clause_item` list shape
 *  `_flattenClauseItems` already knows how to reduce to `{msgDefault,
 *  calls}`, so there is nothing new to parse or validate here, only to
 *  re-plumb. `call._bindingsAt` (not the full statement's binding map) is
 *  used for `msg=`'s label resolution — same left-to-right "no forward
 *  reference" visibility rule every other label read in this grammar
 *  enforces (see `_compileDebugCall`'s identical choice). */
function _compileAlertCallAsOrdinaryCall(call, catalog) {
  // A bare `alert` (no parens) reaching here at all means it was used as
  // an ORDINARY mid-list call with no body (e.g. a second preamble item,
  // where the dispatch-level bare-ALERT rejection — which only ever
  // inspects the statement's FIRST token — never had a chance to catch
  // it). `alert` with no body is never meaningful (there's no clause to
  // compile), so this is a clear, specific compile error rather than
  // silently degrading to zero events.
  if (!call._alertBody) {
    return { events: null, errors: [_err("alert must be written as a call with a body, e.g. alert(telegram) — bare 'alert' has no meaning here")] };
  }
  const flat = _flattenClauseItems(call._alertBody);
  const errors = [];
  // Sprint G parity — a bound label referenced as a plain channel-arg
  // value inside `alert(...)`'s own body (e.g. `x = 5 do alert(ntfy(priority=x))`)
  // gets the SAME substitution pass every other call list in this
  // grammar already runs — see `_substituteBoundArgsInCalls`'s own
  // header comment. Errors from substitution are merged with
  // `compileAlertClause`'s own below.
  const substCalls = flat.calls === 'nop' ? 'nop' : _substituteBoundArgsInCalls(flat.calls, errors);
  const r = compileAlertClause(substCalls, flat.msgDefault, catalog, call._bindingsAt || new Map());
  return { events: errors.length || r.errors.length ? null : r.events, errors: [...errors, ...r.errors] };
}

/** The ONE per-call compiler shared by BOTH the preamble and the DO
 *  position (Sprint [grammar-unify], item 2: "share one parse-a-
 *  statement-list implementation ... rather than maintaining two
 *  separate accept-lists" — this is that sharing's compile-time half).
 *  Dispatches by call name to whichever REUSED compiler already knows
 *  how to handle it:
 *    - `alert(...)`  -> `_compileAlertCallAsOrdinaryCall` (produces
 *      `events`, the SAME field a DO-position `alert(...)` has always
 *      produced — see `compileAgentStmt`'s own `events` wiring).
 *    - `bracket(...)` -> `_compileBracketCallFields`, wrapped as an
 *      ordinary `{type:'bracket', params}` action entry. NOTE: there is
 *      currently NO `bracket` action registered in the real backend
 *      action-dispatch tables (`_NOOP_ACTION_HANDLERS`/`_LIVE_ACTION_
 *      HANDLERS` — see CLAUDE.md's "Agent action-dispatch SSOT" entry) —
 *      this compiles correctly and gives the engine agent (plan item 0)
 *      something concrete to dispatch against, but the entry is INERT
 *      server-side until that wiring lands. Reported, not silently
 *      worked around.
 *    - `debug(...)`  -> `_compileDebugCall` (unchanged — already reads
 *      `call._bindingsAt`, so no position-specific adjustment needed).
 *    - everything else (`order`/`place_order`/`help`/any other real
 *      catalog action) -> `_compileCatalogActionCall`.
 *  Returns exactly one of `{isEvent:true, events}`, `{action}`, or
 *  `{errors}` (non-empty only on failure — never combined with a
 *  populated `action`/`events`). */
function _compileSharedCall(rawCall, catalog, opts, rowRefCtx) {
  const rawName = rawCall.name.toLowerCase();
  if (rawName === 'alert') {
    const r = _compileAlertCallAsOrdinaryCall(rawCall, catalog);
    return r.errors.length ? { errors: r.errors } : { isEvent: true, events: r.events };
  }
  if (rawName === 'bracket') {
    const r = _compileBracketCallFields(rawCall, catalog);
    return r.errors.length ? { errors: r.errors } : { action: { type: 'bracket', params: r.fields } };
  }
  if (rawName === 'debug') {
    const r = _compileDebugCall(rawCall, rawCall._bindingsAt, catalog);
    return r.errors.length ? { errors: r.errors } : { action: r.action };
  }
  if (rawName === 'help') {
    const entry = catalog && catalog.actions ? catalog.actions.get('help') : null;
    const r = _compileHelpCall(rawCall, entry);
    return r.errors.length ? { errors: r.errors } : { action: r.action };
  }
  const r = _compileCatalogActionCall(rawCall, catalog, opts, rowRefCtx);
  return r.errors.length ? { errors: r.errors } : { action: r.action };
}

/** Compile ONE `help(x)` call to `{type:'help', params:{name:'x'}}` —
 *  item 4. `help(...)`'s sole argument is ALWAYS the lexer's own bare-arg
 *  sugar (see `lexAgentStatement`'s `debug`/`help`-shared special case —
 *  there is no "hand-typed `help(name=...)`" alternate spelling at all,
 *  unlike `emit_log`/`debug(x)`), so `call.args` is GUARANTEED to be
 *  exactly `[{key:'name', value:{type:'name', value:<bareword>}}]` for
 *  every call that reaches here — this deliberately bypasses the generic
 *  `_coerceValueForSpec` schema path (which would reject a bare word for
 *  a `type:'string'` param, per this grammar's "enum values are bare,
 *  free-text values are quoted" rule) and takes the bare name's text
 *  LITERALLY, same reasoning as `_compileDebugCall`'s own dedicated
 *  compiler for its own bare-arg sugar. */
function _compileHelpCall(call, entry) {
  if (!entry) return { action: null, errors: [_err('help: action not found in catalog')] };
  const arg = (call.args || [])[0];
  if (!call.args || call.args.length !== 1 || !arg || arg.value.type !== 'name') {
    return { action: null, errors: [_err('help(...) takes exactly one argument — a token/function name, e.g. help(pnl)')] };
  }
  return { action: { type: 'help', params: { name: arg.value.value } }, errors: [] };
}

/** `do_clause := "nop" | call*` — compiles every call via
 *  `_compileSharedCall`, splitting the results into `actions` (every
 *  real action/bracket/debug call) and `events` (every `alert(...)`
 *  call, in textual order — collapses the old separate ALERT clause's
 *  own `compileAlertClause` call into this single pass, per item 2:
 *  "a call whose action is alert still routes through the existing
 *  compileAlertClause -> events field under the hood"). */
function compileDoClause(doCalls, catalog, opts, rowRefCtx) {
  if (doCalls === 'nop') return { actions: [], events: [], errors: [] };
  const errors = [];
  const actions = [];
  const events = [];
  for (const rawCall of doCalls) {
    const r = _compileSharedCall(rawCall, catalog, opts, rowRefCtx);
    if (r.errors) { errors.push(...r.errors); continue; }
    if (r.isEvent) events.push(...r.events);
    else actions.push(r.action);
  }
  return { actions: errors.length ? null : actions, events: errors.length ? null : events, errors };
}

/** `preamble := call*` — Sprint [grammar-unify], item 2's "the preamble
 *  is NOT a restricted subset — it is the full shared grammar." Compiles
 *  every preamble call via the SAME `_compileSharedCall` DO uses, but
 *  into a SINGLE flat `preamble` array (not split into `actions`/
 *  `events`) — a preamble call is UNCONDITIONAL (runs regardless of
 *  `WHEN`), a semantically different bucket from DO's gated `actions`/
 *  `events`, so it gets its own compiled field entirely (see
 *  `compileAgentStmt`). Each entry is tagged with its own `type` (`'alert'`
 *  carries `events` instead of `params` — every other entry is the
 *  ordinary `{type, params}` shape `_compileSharedCall`'s action branches
 *  already produce) so a consumer can tell them apart without
 *  re-deriving it from the call name. */
function compilePreambleClause(preambleCalls, catalog, opts, rowRefCtx) {
  if (!preambleCalls || !preambleCalls.length) return { preamble: [], errors: [] };
  const errors = [];
  const preamble = [];
  for (const rawCall of preambleCalls) {
    const r = _compileSharedCall(rawCall, catalog, opts, rowRefCtx);
    if (r.errors) { errors.push(...r.errors); continue; }
    if (r.isEvent) preamble.push({ type: 'alert', events: r.events });
    else preamble.push(r.action);
  }
  return { preamble: errors.length ? null : preamble, errors };
}

/** `emit_log(...)` — hand-typed only (the `debug(x)` sugar bypasses this
 *  entirely, see `_compileDebugCall`'s own header comment for why). The
 *  live catalog's declared `level` enum (`agent_grammar.yaml`) is
 *  `[info, warning, error]` — it doesn't list `debug`, even though the
 *  Python handler accepts it unconditionally (`getattr(logger, level,
 *  logger.info)`). Strip `level=debug` out before the ordinary generic
 *  enum-validated path runs, then reattach it afterwards — this is the
 *  ONE field/value this sprint special-cases, so a hand-typed
 *  `emit_log(level=debug, message="...")` (not written via the
 *  `debug(x)` sugar) also compiles, and `decompileActions`' generic
 *  rendering of a `debug(x)`-compiled action that falls back to the
 *  plain `emit_log(...)` form (see that function's own comment) recompiles
 *  cleanly too. Every OTHER field/value still goes through the real
 *  catalog schema unchanged. */
function _compileEmitLogActionParams(call, entry, rowRefCtx) {
  const levelArg = (call.args || []).find((a) => a.key && a.key.toLowerCase() === 'level');
  const isDebugLevel = levelArg && levelArg.value
    && (levelArg.value.type === 'name' || levelArg.value.type === 'string')
    && String(levelArg.value.value).toLowerCase() === 'debug';
  if (!isDebugLevel) return _compileGenericActionParams(call, entry, rowRefCtx);
  const strippedCall = { ...call, args: (call.args || []).filter((a) => a !== levelArg) };
  const r = _compileGenericActionParams(strippedCall, entry, rowRefCtx);
  if (r.errors.length) return r;
  return { params: { ...r.params, level: 'debug' }, errors: [] };
}

// ── Meta clause (Sprint 7) — `#name=`/`#desc=`/`#tag=`/`#group=`. GENERIC,
// reusable across agent_stmt (this sprint) and a future bracket_stmt — no
// agent-specific assumptions baked in. `group` is a genuinely NEW concept,
// deliberately distinct from (and never aliased to) this grammar's
// existing `Agent.topic` field. ─────────────────────────────────────────
const _META_KEY_MAP = Object.freeze({ name: 'name', desc: 'description', tag: 'tags', group: 'group' });

/** Compile a parsed `meta_item[]` (see `parseMetaClause`) to
 *  `{ name?, description?, tags?, group? }` — only keys actually PROVIDED
 *  appear on the returned object (never a null/undefined placeholder for
 *  an omitted key), so callers can safely `{ ...other, ...compileMetaClause(...) }`.
 *  Errors are pushed onto the caller-supplied `errors` array (never
 *  thrown) — same "never throw from a compile* function" convention every
 *  other compiler helper in this file follows. `#tag=` is the only key
 *  that accepts 2+ comma-separated meta_values (becomes a real JS array);
 *  `#name=`/`#desc=`/`#group=` each require exactly one value. */
export function compileMetaClause(metaItems, errors) {
  const out = {};
  if (!metaItems || metaItems.length === 0) return out;
  const seen = new Set();
  for (const item of metaItems) {
    const lower = item.key.toLowerCase();
    const realKey = _META_KEY_MAP[lower];
    if (!realKey) {
      errors.push(_err(`unknown meta tag '#${item.key}' — expected one of #name, #desc, #tag, #group`));
      continue;
    }
    if (seen.has(realKey)) {
      errors.push(_err(`duplicate meta tag '#${lower}'`));
      continue;
    }
    seen.add(realKey);
    if (realKey === 'tags') {
      out.tags = item.values.map((v) => v.value);
    } else if (item.values.length !== 1) {
      errors.push(_err(`#${lower} takes exactly one value (only #tag= accepts comma-separated multiple values)`));
    } else {
      out[realKey] = item.values[0].value;
    }
  }
  return out;
}

// ═══════════════════════════════════════════════════════════════════════
// Sprint G — compile-time label-binding support (substitution, collision
// checks, and `debug(...)` → `emit_log` compilation).
// ═══════════════════════════════════════════════════════════════════════

// Words a label may never be bound to, beyond `_RESERVED`/'debug'/'result'
// (already rejected at PARSE time by `_registerBinding`) — these need the
// live `catalog` to check, so they're COMPILE-time-only.
const _LABEL_BLOCKED_WORDS = Object.freeze(new Set(['given']));

/** Every word a scope-chain's FIRST segment could mean — every real
 *  family name (`positions`/`holdings`/`funds`/`watchlist`) AND every
 *  short alias for one (`pos`/`hld`/`hold`/`fnd`/`wl`) — used only for
 *  the label-collision check below; `_scopeFamilyAliasOf` itself (parse-
 *  side resolution) is untouched. */
function _allScopeFamilyWords() {
  const words = new Set(Object.keys(SCOPE_FAMILY_ALIASES));
  for (const alias of Object.keys(_SCOPE_ALIAS_TO_REAL)) words.add(alias);
  return words;
}

/** Reject any bound label that collides with a real, live catalog name —
 *  a metric/scope/channel/action token, an exposed column's bare name
 *  (the SAME vocabulary `$row` binding already reserves — see
 *  `_allExposedColumnNames`), or a scope-family name/alias. Parse-time
 *  already rejected reserved words / rebinding (`_registerBinding`); this
 *  is the half that NEEDS the live catalog, so it only ever runs here, at
 *  compile time. */
function _checkLabelCollisions(bindings, catalog) {
  const errors = [];
  if (!bindings || !bindings.length) return errors;
  const exposedCols = _allExposedColumnNames(catalog);
  const scopeWords = _allScopeFamilyWords();
  for (const b of bindings) {
    const lower = b.label.toLowerCase();
    if (_LABEL_BLOCKED_WORDS.has(lower)) {
      errors.push(_err(`label '${b.label}' is a reserved word in this grammar and cannot be used as a label`));
    } else if (catalog.metrics.has(lower) || catalog.scopes.has(lower) || catalog.channels.has(lower) || catalog.actions.has(lower)) {
      errors.push(_err(`label '${b.label}' collides with an existing token name — choose a different label`));
    } else if (exposedCols.has(lower)) {
      errors.push(_err(`label '${b.label}' collides with an exposed column name — choose a different label`));
    } else if (scopeWords.has(lower)) {
      errors.push(_err(`label '${b.label}' collides with a scope-family name/alias — choose a different label`));
    }
  }
  return errors;
}

/** Compile-time half of the label-binding substitution for ArgEntry
 *  VALUE positions (an action/channel call's own `key=value` args) — the
 *  mirror of `parseLeaf`'s PARSE-time splice for CONDITION positions.
 *  Can't run at parse time: every call's `args` are already parsed into
 *  ArgEntry[] at LEX time, before any binding exists — so instead, each
 *  CallNode carries its own `_bindingsAt` snapshot (set by `parseCall`,
 *  the bindings visible AT THAT CALL'S OWN POSITION in the source,
 *  enforcing left-to-right visibility / no forward reference) which this
 *  function consults. `debug(...)`'s own sole argument is EXPLICITLY
 *  skipped here — it's resolved separately by `_compileDebugCall`, which
 *  needs the full `catalog` to format a condition-kind binding, not just
 *  a value swap. A bare NAME matching no binding at all is untouched,
 *  falling through to the EXISTING `_resolveRowRefOrNull`/
 *  `_coerceValueForSpec` path unchanged — true no-op for every pre-
 *  Sprint-G statement (no GIVEN/assignment anywhere → `_bindingsAt` is
 *  always `undefined`). */
function _substituteBoundArgs(call, errors) {
  const bindings = call._bindingsAt;
  if (!bindings || !bindings.size || !call.args || !call.args.length) return call;
  if (call.name.toLowerCase() === 'debug') return call;
  let changed = false;
  const newArgs = call.args.map((arg) => {
    if (!arg.value || arg.value.type !== 'name') return arg;
    const bound = bindings.get(String(arg.value.value).toLowerCase());
    if (!bound) return arg;
    if (bound.rhsKind === 'condition') {
      errors.push(_err(`label '${arg.value.value}' is bound to a condition — only usable in WHEN or debug(...)`, arg.value));
      return arg;
    }
    // Sprint [GIVEN-expr] — an `'expr'`-kind (operator-expression) binding
    // is left UNTOUCHED here, deliberately — it must NOT be textually
    // substituted (the bound `rhsNode` is this file's own internal
    // arithmetic-AST scaffolding, not a valid `ValueNode` shape, and would
    // corrupt the arg if spliced in directly). It is resolved later,
    // schema-type-aware, by `_resolveGivenExprRefOrNull` inside
    // `_compileGenericActionParams`/`_compileOrderActionParams` — only a
    // `string`-typed param may reference it; a numeric/enum param is a
    // compile error there, not here (this function has no visibility
    // into the target param's own schema type).
    if (bound.rhsKind === 'expr') return arg;
    changed = true;
    return { ...arg, value: _cloneAstNode(bound.rhsNode) };
  });
  return changed ? { ...call, args: newArgs } : call;
}

function _substituteBoundArgsInCalls(calls, errors) {
  if (!Array.isArray(calls)) return calls;
  return calls.map((c) => _substituteBoundArgs(c, errors));
}

/** `debug(x)`'s own auto-format rule: a PRIMITIVE binding (or literal)
 *  renders as its plain value — no JSON-stringify ceremony for a single
 *  number/string/bare-word. A `list` ValueNode (the one other `value`
 *  shape) renders as its JSON array — there's no "plain" text form for a
 *  list. */
function _debugFormatValueNode(node) {
  if (!node) return '';
  if (node.type === 'number') return String(node.value);
  if (node.type === 'string' || node.type === 'name') return String(node.value);
  if (node.type === 'list') return JSON.stringify(node.value.map((v) => v.value));
  return JSON.stringify(node);
}

/** Compile one `debug(x)` call (found in GIVEN, ALERT, or DO — all
 *  hoisted into `ast.debugCalls` by the parser) to a real `emit_log`
 *  action — `{"type":"emit_log","params":{"level":"debug","message":...}}`.
 *  Built DIRECTLY (bypassing `catalog.actions.get('emit_log')`'s own
 *  declared `params_schema` entirely) rather than through the generic
 *  `_compileGenericActionParams` path, for two confirmed reasons:
 *    1. The live catalog's `emit_log.level` enum (`agent_grammar.yaml`)
 *       is `[info, warning, error]` — it does not list `debug` (the
 *       Python HANDLER already accepts it unconditionally via
 *       `getattr(logger, level, logger.info)`, but the DECLARED enum
 *       does not; flagged for the backend agent to add 'debug' to that
 *       enum for documentation/consistency — see this sprint's handback
 *       report). Going through the generic enum-coercion path would
 *       reject `level: "debug"` outright.
 *    2. `message`'s declared type is `string` with `token_ref_ok: true`,
 *       but `backend/api/algo/actions.py:_is_expr_eligible_param`
 *       deliberately restricts expression-string resolution to `type in
 *       (number, boolean)` ONLY — `token_ref_ok` on a `string` field is a
 *       no-op server-side (by design, to keep fields like account/symbol
 *       safe even if one of them ever carried a stale `token_ref_ok`
 *       flag). There is therefore NO existing mechanism that resolves an
 *       arbitrary metric@scope/condition expression into live message
 *       TEXT at action-dispatch time — only `resolve_row_ref_params`'s
 *       `{"$row": "<name>"}` marker (Sprint 16, ANY param type) genuinely
 *       resolves to a LIVE value at execution time. So:
 *       - a GIVEN-bound PRIMITIVE label (or a literal) formats to a
 *         plain STATIC string at COMPILE time (`_debugFormatValueNode`)
 *         — fully known already, no runtime resolution needed.
 *       - a GIVEN-bound CONDITION label formats to the JSON-stringified
 *         COMPILED condition dict (`compileCondition`) — a static
 *         DESCRIPTION of what's being checked, not its live truth value
 *         (there is no backend mechanism to re-evaluate an arbitrary sub-
 *         condition at action-dispatch time and splice its boolean result
 *         into a log message) — consistent with `emit_log`'s own catalog
 *         description ("quiet action for testing agent wiring").
 *       - a bare NAME matching neither falls back to the EXISTING,
 *         genuinely-live `{"$row": name}` marker when it's a real
 *         exposed-row-column name (Sprint 16) — the ONE case this
 *         compiles to an honestly-live runtime value, via a mechanism
 *         that already exists server-side today.
 *       - anything else (an unbound, non-exposed-column bare word) is a
 *         compile-time error.
 *       KNOWN LIMITATION, operator-accepted 2026-10-11 (Sprint G): only
 *       the `{"$row": name}` case above shows a genuinely live value.
 *       `debug()` on a GIVEN-bound metric/condition label is closer to
 *       "here's what's being checked" than "here's what it actually is
 *       right now" — accepted as-is rather than widening the backend's
 *       deliberate `token_ref_ok` type restriction (a real safety
 *       boundary, not an oversight) to chase live-value inspection for
 *       every label shape. */
function _compileDebugCall(call, bindings, catalog) {
  const errors = [];
  const arg = (call.args || [])[0];
  if (!call.args || call.args.length !== 1 || !arg || arg.value.type !== 'name') {
    errors.push(_err('debug(...) takes exactly one argument — a label or token reference name', { start: call.start }));
    return { action: null, errors };
  }
  const refName = arg.value.value;
  const lower = String(refName).toLowerCase();
  const bound = bindings && bindings.get(lower);
  if (bound) {
    // Sprint [GIVEN-expr] — an operator-expression ('expr'-kind) binding
    // is the ONE case that now has a genuinely LIVE value server-side
    // (the backend's new per-tick `given` evaluation step) — compiles to
    // the new `{"$given": name}` marker, parallel to the pre-existing
    // `{"$row": name}` case below. A PURE-ALIAS or real multi-leaf
    // CONDITION-kind binding is UNCHANGED — still the static
    // `_debugFormatValueNode`/`JSON.stringify(compileCondition(...))`
    // format this function has always used for those, per the "KNOWN
    // LIMITATION" comment above (nothing about THAT case changed).
    if (bound.rhsKind === 'expr') {
      return { action: { type: 'log', params: { level: 'debug', message: { '$given': lower } } }, errors };
    }
    let message;
    if (bound.rhsKind === 'value') {
      message = _debugFormatValueNode(bound.rhsNode);
    } else {
      const compiled = compileCondition(bound.rhsNode, catalog);
      if (compiled.errors.length) {
        errors.push(...compiled.errors);
        return { action: null, errors };
      }
      message = JSON.stringify(compiled.conditions);
    }
    return { action: { type: 'log', params: { level: 'debug', message } }, errors };
  }
  if (_allExposedColumnNames(catalog).has(lower)) {
    return { action: { type: 'log', params: { level: 'debug', message: { '$row': lower } } }, errors };
  }
  errors.push(_err(`debug(${refName}): '${refName}' is not a bound label (GIVEN/ALERT/DO) or a matched-row column name`, { start: call.start }));
  return { action: null, errors };
}

/** Sprint [GIVEN-expr] — true iff `b` is a "pure alias" GIVEN binding: a
 *  bare `metric@scope`/col_ref reference with NO comparator of its own
 *  (`op === null`) — i.e. the pre-existing, unchanged `rhsKind:
 *  'condition'` shape a bare `p = pnl@positions.total` assignment has
 *  always produced (Sprint G). Used only to decide which ALIAS bindings
 *  must ALSO get their own `agent.given[]` entry when an operator
 *  expression references them by name (see `_buildGivenEntries`) — a
 *  REAL multi-leaf condition binding (its own comparator, or a &/|/~/
 *  between combination) is never a "pure alias" and is excluded. */
function _isPureAliasBinding(b) {
  return !!b && b.rhsKind === 'condition' && b.rhsNode && b.rhsNode.op === null
    && (b.rhsNode.type === 'leaf' || b.rhsNode.type === 'colref');
}

/** Sprint [GIVEN-expr] — true iff ANY node in a parsed arithmetic tree
 *  (see `parseArithExpr`) is a `metric_atom`/`colref_atom` — i.e. an
 *  INLINE `metric@scope`/col_ref read mixed directly into an operator
 *  expression, e.g. `GIVEN y = "x" + pnl@positions.total`. This must be
 *  a compile-time error: the backend's `expr_eval.py` (confirmed
 *  read-only) is a flat-namespace Python-`ast` evaluator with no `@`/`.`
 *  dotted-access support at all — it can never evaluate such a string.
 *  The required fix is to bind the dynamic read to its own label FIRST
 *  (`GIVEN p = pnl@positions.total`) and reference `p` by bare name
 *  instead — exactly the pure-alias-referenced-by-an-operator-expression
 *  case `_buildGivenEntries` already handles. */
function _arithContainsDynamicRead(node) {
  if (!node || typeof node !== 'object') return false;
  if (node.type === 'metric_atom' || node.type === 'colref_atom') return true;
  if (node.type === 'arith') return _arithContainsDynamicRead(node.left) || _arithContainsDynamicRead(node.right);
  if (node.type === 'pow') return _arithContainsDynamicRead(node.base) || _arithContainsDynamicRead(node.exp);
  if (node.type === 'neg') return _arithContainsDynamicRead(node.node);
  return false;
}

/** Sprint [GIVEN-expr] — collect every name reference (lowercased)
 *  anywhere in a parsed arithmetic tree into `out` — used to find which
 *  OTHER bound labels an `'expr'`-kind binding's own expression touches,
 *  so `_buildGivenEntries` can decide which pure-alias bindings need
 *  their own injected `given[]` entry (see that function). Recognizes
 *  BOTH `name_atom` (produced by `parseArithAtom`, for a genuine `+`/`-`/
 *  etc.-combined expression) AND the plain ValueNode `type: 'name'`
 *  shape (produced by `parseGivenRhs`'s "bare NAME referencing an
 *  earlier binding, no operator at all" reclassification, e.g. `q = p`
 *  — deliberately left as an ordinary ValueNode there, not wrapped, so
 *  it also satisfies every OTHER consumer that pattern-matches on
 *  `type === 'name'`, see that function's own comment). */
function _collectArithNameRefs(node, out) {
  if (!node || typeof node !== 'object') return;
  if (node.type === 'name_atom' || node.type === 'name') { out.add(String(node.value).toLowerCase()); return; }
  if (node.type === 'arith') { _collectArithNameRefs(node.left, out); _collectArithNameRefs(node.right, out); return; }
  if (node.type === 'pow') { _collectArithNameRefs(node.base, out); _collectArithNameRefs(node.exp, out); return; }
  if (node.type === 'neg') { _collectArithNameRefs(node.node, out); return; }
}

/** Sprint [GIVEN-parts] — true iff ANY node in a parsed arithmetic tree is
 *  a STRING literal atom. This is the static, usage-independent property
 *  that decides `given[]`'s compiled SHAPE for an `'expr'`-kind binding:
 *  operator council decision (2026-10) — `backend/api/algo/expr_eval.py`
 *  is a deliberately strict trust boundary (no implicit str/number
 *  coercion — a string operand is only ever valid as a literal, a `Name`
 *  lookup, or a `+` operand where BOTH sides are already strings) and
 *  loosening it was explicitly rejected, since it sits directly upstream
 *  of real order placement. A pure-arithmetic expression (numbers,
 *  metric/colref atoms, earlier-bound names, `+ - * / % **`) still
 *  compiles to the existing verbatim `expr` string, evaluated by
 *  `expr_eval.py` exactly as before — zero change to that path. Anything
 *  containing a string literal anywhere compiles to the NEW `parts[]`
 *  shape instead (see `_flattenPlusChainToParts`), built and resolved
 *  entirely server-side without ever going through `expr_eval.py`'s
 *  string-coercion rules at all. */
function _arithContainsStringLiteral(node) {
  if (!node || typeof node !== 'object') return false;
  if (node.type === 'string') return true;
  if (node.type === 'arith') return _arithContainsStringLiteral(node.left) || _arithContainsStringLiteral(node.right);
  if (node.type === 'pow') return _arithContainsStringLiteral(node.base) || _arithContainsStringLiteral(node.exp);
  if (node.type === 'neg') return _arithContainsStringLiteral(node.node);
  return false;
}

/** Sprint [GIVEN-parts] — flatten a string-containing arithmetic tree
 *  into a left-to-right `parts[]` array for the new compiled shape
 *  (`{"name": label, "parts": [...]}` ), REJECTING anything that isn't a
 *  flat `+`-chain (a `-`/`*`/`/`/`%`/`**`/unary-minus anywhere in a tree
 *  that ALSO contains a string literal has no sensible meaning — string
 *  concatenation is the only operation `parts[]` exists to express, per
 *  the explicit operator decision). Each leaf becomes exactly one part:
 *  a STRING atom's own value (a plain string), or a NUMBER atom
 *  stringified (a plain string — judgment call: not explicitly in the
 *  operator's two-shape enumeration, but rejecting a literal number
 *  mixed into a message string outright seemed needlessly restrictive;
 *  flagged in the handback), or a `name_atom`/plain `name`-typed
 *  reference to an earlier binding (`{"$given": "<name>"}`, the SAME
 *  marker shape used everywhere else in this file). A `metric_atom`/
 *  `colref_atom` leaf can never actually reach this function — the
 *  caller (`_buildGivenEntries`) only calls this AFTER
 *  `_arithContainsDynamicRead` has already confirmed (compile-time) that
 *  no inline dynamic read exists anywhere in the tree; the defensive
 *  branch below is unreachable in practice, kept only so a future caller
 *  that skips that guard fails loud instead of producing a malformed
 *  part. Returns the flattened array, or `null` after pushing a compile
 *  error onto `errors` (caller checks `errors.length`, same convention
 *  as every other compile-time check in this file). */
function _flattenPlusChainToParts(node, errors, label) {
  if (!node || typeof node !== 'object') return null;
  if (node.type === 'arith') {
    if (node.op !== '+') {
      errors.push(_err(`GIVEN label '${label}': only string concatenation with + is supported here`));
      return null;
    }
    const left = _flattenPlusChainToParts(node.left, errors, label);
    const right = _flattenPlusChainToParts(node.right, errors, label);
    if (!left || !right) return null;
    return [...left, ...right];
  }
  if (node.type === 'pow' || node.type === 'neg') {
    errors.push(_err(`GIVEN label '${label}': only string concatenation with + is supported here`));
    return null;
  }
  if (node.type === 'string') return [node.value];
  if (node.type === 'number') return [String(node.value)];
  if (node.type === 'name_atom' || node.type === 'name') return [{ '$given': String(node.value).toLowerCase() }];
  // Unreachable in practice — see docstring.
  errors.push(_err(`GIVEN label '${label}': unsupported value inside a string-building expression`));
  return null;
}

/** Sprint [GIVEN-expr] — build the agent's new top-level `given[]` array
 *  from its full, ordered binding list (`ast.bindings`, i.e.
 *  `state.allBindings` — already strict left-to-right source order, no
 *  forward references possible per the parser's own "no shadowing/no
 *  forward reference" rule). Two kinds of entry, BOTH rendered from the
 *  binding's own verbatim `rhsText` (never re-derived — see
 *  `_registerBinding`'s own comment, and the decompiler's matching
 *  "read `expr` back verbatim" rule):
 *    1. every `'expr'`-kind (operator-expression) binding, unconditionally,
 *       in declaration order;
 *    2. a pure-alias binding, but ONLY when it's actually referenced
 *       (directly) by SOME `'expr'`-kind binding's own expression — the
 *       backend's per-tick namespace needs that alias's own value to
 *       resolve the name, since an `'expr'` binding's `expr` text only
 *       ever carries the bare referenced name, never the alias's own
 *       expanded `metric@scope` text (that would defeat the "resolve
 *       server-side, not via text splicing" design). An alias referenced
 *       only inside WHEN/debug(...) (the pre-existing, unchanged client-
 *       side-substitution paths) needs no entry here at all — this is
 *       solely for the NEW server-resolved paths.
 *  Statements with no `'expr'`-kind binding at all (every pre-existing
 *  GIVEN usage) produce `given: null` here — the caller only splices this
 *  key into the compiled agent when non-null, so a statement untouched by
 *  this sprint compiles to byte-identical JSON. Returns `{ given, errors }`
 *  — `given` is `null` on error (the inline-dynamic-read compile error
 *  above). */
function _buildGivenEntries(bindings) {
  const errors = [];
  const byLower = new Map((bindings || []).map((b) => [b.label.toLowerCase(), b]));
  const exprBindings = (bindings || []).filter((b) => b.rhsKind === 'expr');
  for (const b of exprBindings) {
    if (_arithContainsDynamicRead(b.rhsNode)) {
      errors.push(_err(
        `GIVEN label '${b.label}': a computed expression cannot reference a metric@scope value inline `
        + `— bind it first (e.g. GIVEN p = pnl@positions.total) and reference 'p' here instead`
      ));
    }
  }
  if (errors.length) return { given: null, errors };

  // Sprint [GIVEN-parts] — classify each expr-kind binding's COMPILED
  // SHAPE (not its `rhsKind`, which stays `'expr'` either way — every
  // OTHER consumer, `_resolveGivenExprRefOrNull`/`_resolveMessageValue`/
  // `_substituteBoundArgs`/`_compileDebugCall`, keeps checking
  // `rhsKind === 'expr'` uniformly and keeps emitting the SAME
  // `{"$given": name}` marker regardless of parts-vs-expr — the backend
  // resolves a NAME from its per-tick dict the same way either way, so
  // none of those call sites need to know or care which shape produced
  // the value). See `_arithContainsStringLiteral`'s own docstring for
  // the "why" and the operator decision this implements.
  const partsByLower = new Map();
  for (const b of exprBindings) {
    if (_arithContainsStringLiteral(b.rhsNode)) {
      const parts = _flattenPlusChainToParts(b.rhsNode, errors, b.label);
      if (parts) partsByLower.set(b.label.toLowerCase(), parts);
    }
  }
  if (errors.length) return { given: null, errors };

  const referencedAliases = new Set();
  for (const b of exprBindings) {
    const refs = new Set();
    _collectArithNameRefs(b.rhsNode, refs);
    for (const r of refs) {
      const target = byLower.get(r);
      if (target && _isPureAliasBinding(target)) referencedAliases.add(r);
    }
  }
  const entries = [];
  for (const b of bindings || []) {
    const lower = b.label.toLowerCase();
    if (b.rhsKind === 'expr') {
      entries.push(partsByLower.has(lower)
        ? { name: b.label, parts: partsByLower.get(lower) }
        : { name: b.label, expr: b.rhsText });
    } else if (referencedAliases.has(lower)) {
      // A referenced ALIAS's own binding is always a bare metric@scope/
      // col_ref leaf (see `_isPureAliasBinding`) — never a string
      // literal — so it is always `expr`-shaped, never `parts`-shaped.
      entries.push({ name: b.label, expr: b.rhsText });
    }
  }
  return { given: entries.length ? entries : null, errors: [] };
}

/** Compile an `agent_stmt` AST (see `parseAgentStmt`) — Sprint
 *  [grammar-unify] rewrite. Two independent call-lists now exist instead
 *  of the old three (`preambleCalls`, always compiled; `doCalls`, only
 *  gated by `condResult`) — there is no more separate `alertCalls`/
 *  `alertMsg` pair, `alert(...)` is just one more call name
 *  `_compileSharedCall` recognizes in EITHER list (item 2's "a call
 *  whose action is alert still routes through the existing
 *  compileAlertClause -> events field under the hood"). */
function compileAgentStmt(ast, catalog, opts) {
  const errors = [];
  // Sprint G — bound-label collisions against real catalog names, checked
  // once up front (pure catalog lookups, no interaction with anything
  // else compiled below).
  errors.push(..._checkLabelCollisions(ast.bindings, catalog));
  // Sprint [GIVEN-expr] — build the new `agent.given[]` array (and run
  // its own compile-time inline-dynamic-read guard) up front too, same
  // "pure catalog/binding-shape checks, no interaction with anything else
  // compiled below" spirit as the collision check above.
  const givenResult = _buildGivenEntries(ast.bindings);
  errors.push(...givenResult.errors);
  const condResult = compileCondition(ast.condition, catalog);
  errors.push(...condResult.errors);

  // Sprint [grammar-unify] — the conditional wrapper is paired as a
  // UNIT: `WHEN` with no `DO` has nothing to gate (an error, not a
  // silently-accepted no-op); `DO` with no `WHEN` is now REDUNDANT with
  // simply writing the call in the preamble, which additionally gets the
  // correct once-per-activation execution semantics for a side-effecting
  // call (plan item 0) instead of DO's cooldown-gated-but-repeating
  // shape — so it's rejected too, steering the operator to the form that
  // actually does what they want. A preamble-only statement (neither
  // keyword at all) is NEVER subject to either check.
  if (ast.sawWhen && !ast.sawDo) {
    errors.push(_err('WHEN requires a DO clause — write WHEN <condition> DO <calls>'));
  }
  if (ast.sawDo && !ast.sawWhen) {
    errors.push(_err('DO requires a WHEN condition — for an unconditional action, write it directly (no WHEN/DO needed)'));
  }

  // Sprint G — substitute any VALUE-kind bound label referenced as a
  // plain action/channel arg value BEFORE either call-list compiler ever
  // sees these calls (a CONDITION-kind label used this way is a compile
  // error raised by `_substituteBoundArgs` itself). True no-op for every
  // pre-Sprint-G statement (`_bindingsAt` is `undefined` everywhere).
  const preambleCallsSubst = _substituteBoundArgsInCalls(ast.preambleCalls, errors);
  const doCallsSubst = _substituteBoundArgsInCalls(ast.doCalls, errors);
  // Sprint 16 — matched-row ($row) binding context, built from THIS
  // statement's own compiled condition tree (every real leaf's `scope`,
  // see `_collectLeafScopes`) so `compileDoClause` can recognise/validate
  // bare-NAME action-param values against it. See the "Sprint 16" header
  // comment above `_resolveRowRefOrNull` for the full design. A preamble
  // call runs UNCONDITIONALLY, outside any WHEN match — there is no real
  // matched row to bind it from at all, so `compilePreambleClause` is
  // given `rowRefCtx: null` (same convention `compileCancelAllOrdersStmt`
  // already uses for its own no-WHEN bare statement), NOT this
  // condition-derived context — passing the condition's context here
  // would let a bare NAME that happens to match an exposed column
  // compile to a `$row` marker that silently resolves to nothing at
  // runtime (the preamble's own execution path has no row to read it
  // from), rather than the clear compile-time error `_resolveRowRefOrNull`
  // gives for every OTHER no-row-available case.
  const leafScopes = new Set();
  const scopeState = { hasNegation: false, hasTokenRef: false };
  _collectLeafScopes(condResult.conditions, leafScopes, scopeState);
  const rowRefCtx = {
    catalog, leafScopes, allExposedNames: _allExposedColumnNames(catalog),
    hasNegation: scopeState.hasNegation, hasTokenRef: scopeState.hasTokenRef,
  };
  const preambleResult = compilePreambleClause(preambleCallsSubst, catalog, opts, null);
  errors.push(...preambleResult.errors);
  const doResult = compileDoClause(doCallsSubst, catalog, opts, rowRefCtx);
  errors.push(...doResult.errors);

  const metaFields = compileMetaClause(ast.meta, errors);
  // A `WHEN ... DO ...` pair with nothing real inside DO (e.g. `DO nop`)
  // is the same degenerate case the old "at least one of ALERT or DO"
  // check caught — simplified now that ALERT is gone: DO itself must
  // produce at least one real action or alert. A preamble-only statement
  // is NEVER subject to this (see the plan's explicit "label-assignment
  // alone is a complete, valid statement" requirement).
  if (ast.sawWhen && ast.sawDo && !errors.length
    && (doResult.actions || []).length === 0 && (doResult.events || []).length === 0) {
    errors.push(_err('DO must have at least one real action or alert — DO nop by itself does nothing'));
  }
  if (errors.length) return { kind: 'agent', errors, agent: null };
  return {
    kind: 'agent', errors: [],
    agent: {
      conditions: condResult.conditions,
      events: doResult.events || [],
      actions: doResult.actions || [],
      // Sprint [grammar-unify] — only spliced in when non-empty, so a
      // statement with no preamble calls at all (every pre-existing
      // agent, which never had anything but GIVEN's own label
      // assignments/debug) compiles to byte-identical JSON — no stray
      // `preamble: []` key.
      ...(preambleResult.preamble && preambleResult.preamble.length ? { preamble: preambleResult.preamble } : {}),
      // Sprint [GIVEN-expr] — only spliced in when non-null, same reason.
      ...(givenResult.given ? { given: givenResult.given } : {}),
      // item 0.C — lets the backend engine distinguish a real DO-level
      // `alert(...)` call from the old seed/form-UI default `events` list
      // (every `loss-*` builtin carries a non-empty `events` from
      // `_LOSS_AGENT_DEFAULTS`, unrelated to any DO alert() call — both
      // shapes are byte-identical in the `events` column, so the backend
      // can't tell them apart from `events` alone). `true` only when THIS
      // compile produced a real alert from DO; absent (never `false`)
      // otherwise, same "only spliced in when meaningful" convention as
      // `preamble`/`given` above, so every pre-existing agent with no DO
      // alert() call compiles byte-identical.
      ...(doResult.events && doResult.events.length > 0 ? { has_explicit_alert: true } : {}),
      ...metaFields,
    },
  };
}

// ── event_stmt compiler (Sprint 12) — notification (kind='event') agents.
// `log_tag`/`render` are free-text, preserved EXACTLY as typed (no enum/
// case normalization — see `_EVENT_META_KEYS`'s own file-header note and
// the PLAN's explicit "asymmetric casing" rule); `min_level` is the ONE
// field that DOES get case-normalized, via the SAME `_coerceValueForSpec`
// enum path every other enum field in this grammar already uses.
// `_MIN_LEVEL_SPEC`/`_MIN_LEVEL_DEFAULT` are defined earlier in this file
// (right before `OBJECT_LITERAL_SCHEMAS`) — moved up in Sprint 18's
// follow-up (`match=[...]`) so `OBJECT_LITERAL_SCHEMAS.match` can
// reference `_MIN_LEVEL_SPEC` directly for its per-entry `min_level`
// field, without a module-load-order (TDZ) hazard. ─────────────────────

/** Extract a free-text value (bare NAME or quoted STRING) PRESERVED
 *  EXACTLY as typed — no case normalization, unlike an enum field. Used
 *  for `log_tag=`/`render=` (never `min_level=`, which IS enum-normalized
 *  via `_MIN_LEVEL_SPEC` above). */
function _freeTextValue(valueNode, label) {
  if (valueNode.type === 'string' || valueNode.type === 'name') return { value: valueNode.value };
  return { error: _err(`${label}: expected a bare word or quoted string`) };
}

/** Compile an `event_stmt` AST (see `parseEventStmt`) to a `kind:'event'`
 *  agent-create payload. The channel call_list is compiled via the EXACT
 *  SAME `compileAlertClause` path ALERT's own call_list already uses (so
 *  `$ref` Token references, per-channel params, and the `enabled: true`
 *  stamp all come free) — but with NO clause-level `msg=` default
 *  (`null`), since an event-agent channel carries no message at all (the
 *  `render=` renderer owns the message body server-side). `msg=`/`message=`
 *  appearing ANYWHERE in the clause (the clause-level default captured by
 *  `parseEventStmt`, OR a per-channel own `msg=`/`message=` kwarg) is a
 *  compile-time error naming the offending key, per the operator's
 *  explicit instruction that `render=` — not `msg=` — owns the message for
 *  an event agent. */
/** Compile `match=[...]`'s list of object-literal entries (Sprint 18
 *  follow-up) into the backend's `{"any": [{"log": {...}}, {"log":
 *  {...}}]}` leaf shape — each entry resolved via
 *  `OBJECT_LITERAL_SCHEMAS.match` (the SAME generic
 *  `_coerceObjectLiteralEntry` mechanism tp_scales_json/where= already
 *  use). `min_level` defaults to `_MIN_LEVEL_DEFAULT` per-entry, same
 *  default the simple single-filter form already applies. Returns the
 *  `any` array (list of `{log: {...}}` objects) or `null` on any error
 *  (pushed onto the caller-supplied `errors`). */
function _compileMatchList(matchNode, errors) {
  if (!matchNode || matchNode.type !== 'list') {
    errors.push(_err('match= requires a list of {...} entries, e.g. [{tag=orders, min_level=WARNING}]'));
    return null;
  }
  if (matchNode.value.length === 0) {
    errors.push(_err('match= requires at least one {...} entry'));
    return null;
  }
  const logs = [];
  matchNode.value.forEach((entryNode, i) => {
    const coerced = _coerceObjectLiteralEntry(entryNode, OBJECT_LITERAL_SCHEMAS.match, `match[${i}]`);
    if (coerced.errors.length) { errors.push(...coerced.errors); return; }
    const fields = coerced.value;
    if (fields.tag === undefined) { errors.push(_err(`match[${i}]: missing required 'tag='`)); return; }
    const log = { tag: fields.tag, min_level: fields.min_level !== undefined ? fields.min_level : _MIN_LEVEL_DEFAULT };
    if (fields.where !== undefined) log.where = fields.where;
    logs.push({ log });
  });
  return errors.length ? null : logs;
}

function compileEventStmt(ast, catalog) {
  const errors = [];

  const logTagNode = ast.eventMeta.log_tag;
  const renderNode = ast.eventMeta.render;
  const minLevelNode = ast.eventMeta.min_level;
  const whereNode = ast.eventMeta.where;
  const gateNode = ast.eventMeta.gate;
  const matchNode = ast.eventMeta.match; // Sprint 18 follow-up

  // `msg=`/`message=` rejection, naming EVERY other reserved key present
  // alongside it (not just a generic "doesn't apply" wording) — per the
  // operator's explicit instruction that "log_tag= and msg= both present"
  // must name both keys, not just complain about msg= in isolation.
  if (ast.msgNode != null) {
    const present = ['log_tag', 'min_level', 'render', 'where', 'gate', 'match'].filter((k) => ast.eventMeta[k]);
    const alongside = present.length ? ` (combined with ${present.map((k) => `${k}=`).join(', ')})` : '';
    errors.push(_err(`msg= doesn't apply to event agents${alongside} — set render= instead`));
  }

  // Sprint 18 follow-up — `match=` is mutually exclusive with the simple
  // single-filter `log_tag=`/`min_level=`/`where=` triple (an event_stmt
  // has either form, never both).
  if (matchNode) {
    const present = ['log_tag', 'min_level', 'where'].filter((k) => ast.eventMeta[k]);
    if (present.length) {
      errors.push(_err(`match= is mutually exclusive with ${present.map((k) => `${k}=`).join(', ')} — use one form or the other`));
    }
  }
  // Sprint 18 — `ast.calls` is `'nop' | CallNode[]` (see `parseEventStmt`'s
  // own comment on switching to `parseClauseBody`); `'nop'` means an empty
  // channel list, so there is nothing to check here.
  const rawCalls = ast.calls === 'nop' ? [] : ast.calls;
  for (const call of rawCalls) {
    const hasMsg = (call.args || []).some((a) => a.key && _MESSAGE_KEYS.has(a.key.toLowerCase()));
    if (hasMsg) {
      errors.push(_err(`msg=/message= doesn't apply to event agents (channel '${call.name}') — set render= instead`));
    }
  }

  // Sprint 18 follow-up — the simple single-filter triple (`log_tag=`/
  // `min_level=`/`where=`) is only resolved/required when `match=` is
  // ABSENT; `matchList` is only resolved when it's PRESENT. The mutual-
  // exclusion check above already errored if both forms were typed
  // together, so this branch never silently prefers one over the other.
  let logTag = null, minLevel = _MIN_LEVEL_DEFAULT, where, matchList = null;
  if (!matchNode) {
    if (!logTagNode) errors.push(_err("event agent: missing required 'log_tag=' (or match=[...])"));
    else {
      const r = _freeTextValue(logTagNode, 'log_tag');
      if (r.error) errors.push(r.error); else logTag = r.value;
    }
    if (minLevelNode) {
      const r = _coerceValueForSpec(minLevelNode, _MIN_LEVEL_SPEC, 'min_level', 'event agent');
      if (r.error) errors.push(r.error); else minLevel = r.value;
    }
    if (whereNode) where = _unwrapValueNode(whereNode);
  } else {
    matchList = _compileMatchList(matchNode, errors);
  }

  let renderName = null;
  if (!renderNode) errors.push(_err("event agent: missing required 'render='"));
  else {
    const r = _freeTextValue(renderNode, 'render');
    if (r.error) errors.push(r.error); else renderName = r.value;
  }

  // Sprint 18 — `gate=` is the RENDER ACTION's own optional sibling key
  // (repeat-dedup via the shared RepeatGate), validated against
  // `catalog.renderAction`'s own params_schema (backend's
  // `event_actions.render.params_schema`, via `GET /tokens?grammar=action`'s
  // new `render_action` row) rather than a hardcoded boolean check — a
  // stale/older catalog that hasn't shipped this row yet fails loud
  // instead of silently accepting an undeclared key. `undefined` (never
  // typed) is the overwhelmingly common case and must NOT be defaulted to
  // `false` — the compiled `actions[0]` omits `gate` entirely then,
  // matching every real seeded agent except `error-alert`.
  let gate; // undefined = not typed
  if (gateNode) {
    const spec = catalog.renderAction && catalog.renderAction.paramsSchema && catalog.renderAction.paramsSchema.gate;
    if (!spec) {
      errors.push(_err("event agent: 'gate=' is not declared in the current catalog (render action schema missing)"));
    } else {
      const r = _coerceValueForSpec(gateNode, spec, 'gate', 'event agent');
      if (r.error) errors.push(r.error); else gate = r.value;
    }
  }

  // Strip any (already-rejected-above) msg=/message= kwarg before handing
  // the channel list to compileAlertClause — avoids a confusing SECOND,
  // unrelated "channel has no message" error cascading on top of our own
  // clear one above. `'nop'` passes straight through (compileAlertClause's
  // own `'nop'` guard returns `{events: [], errors: []}`).
  const sanitizedCalls = ast.calls === 'nop' ? 'nop' : ast.calls.map((call) => ({
    ...call,
    args: (call.args || []).filter((a) => !(a.key && _MESSAGE_KEYS.has(a.key.toLowerCase()))),
  }));
  const eventsResult = compileAlertClause(sanitizedCalls, null, catalog);
  errors.push(...eventsResult.errors);

  const metaFields = compileMetaClause(ast.meta, errors);

  if (errors.length) return { kind: 'event', errors, agent: null };

  let conditions;
  if (matchNode) {
    conditions = { any: matchList };
  } else {
    const log = { tag: logTag, min_level: minLevel };
    if (where !== undefined) log.where = where;
    conditions = { log };
  }

  const action = { type: 'render', render: renderName };
  if (gate !== undefined) action.gate = gate;

  return {
    kind: 'event', errors: [],
    agent: {
      kind: 'event',
      conditions,
      events: eventsResult.events,
      actions: [action],
      ...metaFields,
    },
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
  // Sprint [order-unify] — tolerant lookup: schema only (this compiled
  // leg object carries no `type` field at all — ticket/basket JSON, not
  // an agent-action dict — so there is nothing to "flip" here, only the
  // lookup itself needs to tolerate a future literal `order` catalog row).
  const entry = catalog.actions.get('order') || catalog.actions.get('place_order');
  if (!entry) { errors.push(_err('place_order action not found in catalog')); return { leg: null, errors }; }

  const { lotsArgs, call: afterLots } = _splitLotsArg(call);
  // Shared TP/SL/Wing kwargs (Sprint 8) — stripped here, same "sugar
  // before generic key-mapping" pattern `lots=` already uses, since none
  // of tp_pct/sl_pct/wing_premium_pct/wing_strike_offset/
  // wing_max_spread_pct/tp_order_type/sl_trail_pct/tp_scales_json are
  // literal keys in this entry's own catalog params_schema.
  const { tpSlWingArgs, call: afterTpSlWing } = _splitTpSlWingArgs(afterLots);
  // `bracket=` (Sprint 8) — references an existing saved OrderTemplate by
  // name; see `_compileTpSlWingFields`'s sibling note above for the
  // analogous tp_order_type caveat. FIELD-NAME CHOICE (not independently
  // confirmed — flagged for the backend agent): `TicketOrderRequest` has
  // no raw name/slug reference field at all (only numeric `template_id`);
  // the one existing precedent anywhere in the codebase for "reference a
  // saved template by NAME, resolved server-side" is `backend/api/routes/
  // lab.py`'s own request struct's `template_slug` field (resolved via
  // `_res_resolve_template_slug` BEFORE a real TicketOrderRequest is
  // built) — reused here for consistency. This compiler does no network
  // I/O, so it can only emit the raw slug, never a resolved id.
  const bracketArgs = (afterTpSlWing.args || []).filter(a => a.key && a.key.toLowerCase() === 'bracket');
  const strippedCall = { ...afterTpSlWing, args: (afterTpSlWing.args || []).filter(a => !(a.key && a.key.toLowerCase() === 'bracket')) };
  if (bracketArgs.length > 1) errors.push(_err("order: duplicate parameter 'bracket'"));
  let template_slug = null;
  if (bracketArgs.length === 1) {
    const r = _coerceValueForSpec(bracketArgs[0].value, { type: 'string' }, 'bracket', 'order');
    if (r.error) errors.push(r.error); else template_slug = r.value;
  }
  const tpSlWingResult = _compileTpSlWingFields(tpSlWingArgs, 'ticket', 'order', entry.mutuallyExclusiveGroups);
  errors.push(...tpSlWingResult.errors);

  const provided = _mapCallArgsToKeys(strippedCall, entry.paramKeys, _ALIAS_TO_REAL, errors, 'order');
  if (lotsArgs.length > 1) errors.push(_err("order: duplicate parameter 'lots'"));
  if (lotsArgs.length === 1 && provided.has('qty')) errors.push(_err('order: cannot specify both qty and lots'));
  if (!provided.has('account')) errors.push(_err("order: missing required param 'account'"));
  if (!provided.has('symbol')) errors.push(_err("order: missing required param 'symbol'"));
  if (!provided.has('side')) errors.push(_err("order: missing required param 'side'"));
  if (!provided.has('qty') && lotsArgs.length === 0) errors.push(_err("order: missing required param 'qty' (or lots=N for F&O)"));
  // Sprint 17 — the shared TP/SL/Wing sugar above already catches a
  // wing-pair conflict typed via the short names (wing_premium_pct=/
  // wing_strike_offset=); this second check catches the MIXED case (one
  // typed via the short sugar, the other typed directly as its real
  // catalog `_override`-suffixed name) and the fully-direct case (both
  // typed as `_override` names) — neither of which the short-name-only
  // check above can see, since those bypass `_splitTpSlWingArgs` entirely
  // and land in `provided` as ordinary place_order params instead.
  if (entry.mutuallyExclusiveGroups) {
    const presentKeys = new Set([...provided.keys()].map((k) => k.toLowerCase()));
    for (const k of Object.keys(tpSlWingResult.fields || {})) presentKeys.add(k.toLowerCase());
    errors.push(..._checkMutexGroups(presentKeys, entry.mutuallyExclusiveGroups, 'order'));
  }
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
  if (errors.length) return { leg: null, errors };

  // Sprint 20 (item 2) — `exchange=` omitted: derive it from the real
  // instruments cache (`listExchangesForSymbol`, the SAME multi-listing
  // index `getInstrument`'s own lot-size lookup is built on) instead of
  // the old hardcoded 'NFO' default, which silently mis-tagged every
  // equity/MCX/BSE order as NFO. A symbol listed on exactly one exchange
  // resolves unambiguously; 0 (cache miss) or 2+ (a dual-listed equity,
  // e.g. RELIANCE on NSE+BSE) listings REQUIRE an explicit `exchange=` —
  // never a silent guess either way.
  let exchange;
  if (provided.has('exchange')) {
    exchange = coerce('exchange', null);
  } else if (typeof symbol === 'string' && symbol) {
    const exchangesOf = (opts && opts.exchangesOf) || _defaultExchangesOf;
    const listed = exchangesOf(symbol) || [];
    if (listed.length === 1) {
      exchange = listed[0];
    } else if (listed.length === 0) {
      errors.push(_err(`order: could not resolve exchange for '${symbol}' — specify exchange= explicitly`));
    } else {
      errors.push(_err(`order: '${symbol}' lists on multiple exchanges (${listed.join(', ')}) — specify exchange= explicitly`));
    }
  } else {
    errors.push(_err("order: exchange= is required when symbol isn't a literal value"));
  }
  if (errors.length) return { leg: null, errors };

  // Sprint 20 (item 2) — `product=` default now follows the SAME
  // F&O-vs-equity split as the qty-as-contracts conversion below
  // ('CNC' for NSE/BSE equity, 'NRML' for F&O) instead of a flat 'NRML'
  // that was invalid at the broker for an equity order.
  const product = coerce('product', _FO_EXCHANGES.has(exchange) ? 'NRML' : 'CNC');
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

  // Sprint 17 — generic conditional-requirement check (place_order's real
  // catalog `price`/`trigger_price` `required_if` specs) against this
  // call's EXPLICITLY-TYPED values only.
  //
  // Sprint 20 (item 2) fix — this now checks `order_type` against the
  // SAME local default (`'LIMIT'`) the compiled leg ACTUALLY ships with,
  // not the catalog's own declared default ('MARKET'). Pre-Sprint-20,
  // checking against 'MARKET' meant a bare `order(...)` with no
  // `order_type=`/`price=` at all compiled cleanly (MARKET never
  // satisfies LIMIT/SL's `required_if.in`) while actually emitting a
  // LIMIT order with `price: null` — a compile-time-invisible defect
  // that only surfaced as a confusing broker-side rejection. `order_type`
  // itself already equals either the provided value or the local
  // 'LIMIT' default (set above), so this is now a direct passthrough.
  const resolvedForReqIf = (k) => {
    if (k === 'order_type') return order_type;
    if (k === 'price') return provided.has('price') ? price : undefined;
    if (k === 'trigger_price') return provided.has('trigger_price') ? trigger_price : undefined;
    return undefined;
  };
  errors.push(..._checkRequiredIf(['price', 'trigger_price'], entry.paramsSchema, resolvedForReqIf, 'order'));
  if (errors.length) return { leg: null, errors };

  // Sprint 20 (item 1, URGENT) — `qty=` now means CONTRACTS everywhere
  // (bare `order(...)` AND `DO place_order(...)`/`DO order(...)` alike),
  // matching the agent-action convention that is already baked into the
  // decompiler, `$row`-bound `qty=qty` (a matched row's own `qty` column
  // is contracts), and every already-stored `Agent.cli_source` text —
  // all of which are far more expensive to change than this ephemeral,
  // never-persisted bare-order path. For the ticket/basket destination
  // (which the real `/api/orders/ticket`/`/api/orders/basket` endpoints
  // still expect in LOTS for F&O, per `_ticket_validate_input`), a
  // literal F&O exchange's contracts are converted to lots HERE, at
  // compile time, using the SAME resolved `exchange`/lot-size machinery
  // `lots=` already uses — never passed through unconverted (that was
  // the exact "qty=75 silently became 75 LOTS instead of 1" incident
  // class this item exists to close). Equity (NSE/BSE) has no lots
  // concept — `qty=` there is raw shares, passed through unchanged, same
  // as before.
  let quantity;
  let lot_size_hint = null;
  const isFo = _FO_EXCHANGES.has(exchange);
  if (lotsArgs.length === 1) {
    const lotsVal = lotsArgs[0].value;
    if (lotsVal.type !== 'number' || !(lotsVal.value > 0) || !Number.isInteger(lotsVal.value)) {
      errors.push(_err('order: lots must be a positive whole number'));
      return { leg: null, errors };
    }
    const lotSizeOf = (opts && opts.lotSizeOf) || _defaultLotSizeOf;
    const lotSize = lotSizeOf(symbol);
    if (!lotSize || !(lotSize > 0)) {
      errors.push(_err(`order: could not resolve lot size for '${symbol}'`));
      return { leg: null, errors };
    }
    quantity = lotsVal.value;
    lot_size_hint = Math.round(lotSize);
  } else {
    const c = _coerceValueForSpec(provided.get('qty'), entry.paramsSchema.qty, 'qty', 'order');
    if (c.error) { errors.push(c.error); return { leg: null, errors }; }
    const qtyContracts = c.value;
    if (!isFo) {
      quantity = qtyContracts;
    } else {
      const lotSizeOf = (opts && opts.lotSizeOf) || _defaultLotSizeOf;
      const lotSize = lotSizeOf(symbol);
      if (!lotSize || !(lotSize > 0)) {
        errors.push(_err(
          `order: could not resolve lot size for '${symbol}' — required to interpret `
          + `qty= as contracts; use lots=N instead`
        ));
        return { leg: null, errors };
      }
      if (qtyContracts % lotSize !== 0) {
        errors.push(_err(
          `order: qty= is contracts; '${symbol}' lot size is ${lotSize} — use lots=N `
          + `or a qty that's a multiple of ${lotSize}`
        ));
        return { leg: null, errors };
      }
      quantity = qtyContracts / lotSize;
      lot_size_hint = Math.round(lotSize);
    }
  }

  const leg = {
    account, side, tradingsymbol: symbol, quantity,
    exchange, product, order_type, variety, price, trigger_price,
    chase, chase_aggressiveness,
    template_id, tp_pct_override, sl_pct_override,
    wing_premium_pct_override, wing_strike_offset_override,
  };
  // Shared TP/SL/Wing plain-name kwargs (Sprint 8) — merged AFTER the old
  // catalog-schema-driven `*_override` fields above (via `Object.assign`,
  // not an object-literal spread — a spread here widens svelte-check's
  // inferred type for `leg` just enough to break the plain property
  // assignments right below, which this exact file already relies on for
  // `lot_size_hint`) so the new spelling wins if an operator (unusually)
  // supplies both; only keys actually TYPED appear here (see
  // `_compileTpSlWingFields`), so every key the operator didn't use keeps
  // its `null` default from above.
  Object.assign(leg, tpSlWingResult.fields || {});
  if (lot_size_hint != null) leg.lot_size_hint = lot_size_hint;
  // `bracket=` (Sprint 8) — conditionally added, same "only present when
  // actually typed" convention `lot_size_hint` above already uses, so
  // every pre-Sprint-8 `order(...)` compile's exact output shape
  // (asserted via `.toEqual` in existing tests) is byte-for-byte
  // unaffected when `bracket=` isn't used.
  if (template_slug != null) leg.template_slug = template_slug;
  return { leg, errors: [] };
}

/** Compile a bare `order_stmt` to a SINGLE TicketOrderRequest-shaped
 *  object (one call) or a BasketOrderRequest-shaped object (2+ calls,
 *  grouped by account) — the SAME shapes the existing Ticket UI / basket
 *  endpoint already accept (`frontend/src/lib/order/orderTicketSubmit.js`,
 *  `backend/api/schemas.py:TicketOrderRequest` / `BasketOrderRequest`). */
function compileOrderStmt(ast, catalog, opts = {}) {
  const errors = [];
  // A bare order(...) statement has no agent/bracket to attach a
  // name/description/tag/group to — parsed fine (see `parseOrderStmt`),
  // rejected here at compile time, per the operator's explicit scope.
  if (ast.meta && ast.meta.length) {
    errors.push(_err('meta tags only apply to agents/brackets, not a bare order'));
  }
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

// Sprint 14 — added 'sell_any' to match the real backend
// `_APPLIES_TO_CHOICES` set. Sprint 14 finding #4 (documented, never
// actioned until 2026-10-12) also flagged a SECOND, separate gap:
// 'buy_option' was still missing even though the real seeded
// `default-long-option` bracket uses it — confirmed again directly by
// a live decompile failure on a new custom bracket replicating that
// same applies_to value. `_APPLIES_TO_CHOICES`
// (`backend/api/routes/templates.py:50`) is the full, real set:
// {"buy_any", "sell_option", "sell_any", "both", "buy_option"}.
const _BRACKET_APPLIES_TO_SPEC = Object.freeze({ type: 'enum', enum: ['buy_any', 'sell_option', 'sell_any', 'both', 'buy_option'] });

/** Compile a standalone `bracket(...)` call to an `OrderTemplateCreate`-
 *  shaped object (`backend/api/schemas.py`) — Sprint 8. `applies_to` is
 *  bracket-ONLY (not part of the shared TP/SL/Wing vocabulary, never valid
 *  on `order(...)`), CONFIRMED against the real struct's own declared
 *  default/comment (`applies_to: str = "both" # 'buy_any' / 'sell_option'
 *  / 'both'`). `#name=` is effectively required — `OrderTemplateCreate.name`
 *  is a required (non-Optional) `str` field — a missing `#name=` is a
 *  compile-time error, matching this file's existing required-field error
 *  style (`'<label>: missing required param '<key>''`). `catalog` (Sprint
 *  17) is threaded through purely so the shared wing-pair mutex check
 *  below can read `place_order`'s declared `mutuallyExclusiveGroups` —
 *  `OrderTemplateCreate` has no catalog token of its own; bracket's plain
 *  template-side field names reuse place_order's ticket-side group as the
 *  one shared business rule (see `_compileTpSlWingFields`'s own doc). */
/** Compile a `bracket(...)` call's OWN arguments (tp/sl/wing fields +
 *  `applies_to=`) to `{fields, errors}` — extracted out of
 *  `compileBracketStmt` (Sprint [grammar-unify]) so the exact same body
 *  logic is reusable from `_compileSharedCall` when `bracket(...)`
 *  appears as one call among several in a preamble/DO list, not just as
 *  the WHOLE standalone statement. Carries NO meta/`#name=` handling at
 *  all — that stays `compileBracketStmt`-only (a bracket embedded in a
 *  preamble/DO list has no meta clause of its own to read; see
 *  `_compileSharedCall`'s own bracket branch, which reports — rather than
 *  invents — the fact that no backend `bracket` action exists yet to
 *  dispatch this entry, a gap for the engine agent to close, not this
 *  compiler). */
function _compileBracketCallFields(call, catalog) {
  const errors = [];
  const { tpSlWingArgs, call: afterTpSlWing } = _splitTpSlWingArgs(call);
  const appliesToArgs = (afterTpSlWing.args || []).filter(a => a.key && a.key.toLowerCase() === 'applies_to');
  const otherArgs = (afterTpSlWing.args || []).filter(a => !(a.key && a.key.toLowerCase() === 'applies_to'));
  if (appliesToArgs.length > 1) errors.push(_err("bracket: duplicate parameter 'applies_to'"));
  for (const a of otherArgs) errors.push(_err(`bracket: unknown parameter '${a.key}'`));

  // Sprint [order-unify] — tolerant lookup (schema-only; see
  // `_compileOrderCallToLeg`'s identical comment).
  const placeOrderEntry = catalog && catalog.actions
    ? (catalog.actions.get('order') || catalog.actions.get('place_order'))
    : null;
  const tpSlWingResult = _compileTpSlWingFields(tpSlWingArgs, 'template', 'bracket', placeOrderEntry && placeOrderEntry.mutuallyExclusiveGroups);
  errors.push(...tpSlWingResult.errors);

  let applies_to;
  if (appliesToArgs.length === 1) {
    const r = _coerceValueForSpec(appliesToArgs[0].value, _BRACKET_APPLIES_TO_SPEC, 'applies_to', 'bracket');
    if (r.error) errors.push(r.error); else applies_to = r.value;
  }

  if (errors.length) return { fields: null, errors };
  const fields = {
    ...(applies_to !== undefined ? { applies_to } : {}),
    ...tpSlWingResult.fields,
  };
  return { fields, errors: [] };
}

function compileBracketStmt(ast, catalog, opts = {}) {
  const errors = [];
  const call = ast.call;
  if (!call || call.name.toLowerCase() !== 'bracket') {
    errors.push(_err('only bracket(...) can be used standalone'));
    return { errors, template: null };
  }

  const bodyResult = _compileBracketCallFields(call, catalog);
  errors.push(...bodyResult.errors);

  const metaFields = compileMetaClause(ast.meta, errors);
  if (!metaFields.name) {
    errors.push(_err("bracket: missing required meta tag '#name' — a saved bracket must have a name"));
  }

  if (errors.length) return { errors, template: null };

  const template = { ...metaFields, ...bodyResult.fields };
  return { errors: [], template };
}

const _TOKEN_KIND_SPEC = Object.freeze({ type: 'enum', enum: ['notify', 'condition'] });

/** Compile a standalone `token(kind=...)` statement to a
 *  `{kind, name, body?, conditions?, description?}` object (Sprint 10,
 *  extended 2026-10-12 for condition-kind — operator: "build it now")
 *  — the caller (`submitConsoleToken`/`updateConsoleToken` in
 *  consoleDispatch.js) maps this onto the unified `/api/agents/`
 *  create/update request shape for an `Agent(kind='template')` row
 *  (Sprint D2, Agent/Bracket/Template unification phase — the
 *  dedicated `AgentTemplate` model/table this used to target directly
 *  is retired).
 *
 *  `kind=notify`: the body call_list is compiled via the EXACT SAME
 *  `compileAlertClause` path ALERT's own call_list already uses (no
 *  clause-level `msg=` default — `null` — since a Token body is a plain
 *  channel list, not an ALERT clause). Result carries `fragment.body`
 *  (an events array) — `fragment.conditions` is absent.
 *
 *  `kind=condition`: `ast.condition` (parsed by `parseTokenStmt` via the
 *  SAME `parseCondition` production `WHEN` uses) is compiled via the
 *  SAME `compileCondition` function `WHEN`'s own clause uses. Result
 *  carries `fragment.conditions` (the JSON condition tree) — `fragment.
 *  body` is absent. No backend change needed for this — condition-kind
 *  Template rows already exist in production with real `conditions`
 *  trees (authored via direct JSON before this grammar supported them);
 *  `createAgent`/`updateAgent` already accept this shape for
 *  `kind='template'` rows.
 *
 *  `#name=` is required in both cases (it doubles as the row's `slug`,
 *  the real `$ref` key — see `submitConsoleToken`'s own doc); `#tag=`/
 *  `#group=` are rejected outright — not yet wired up for Template rows
 *  by this grammar, even though the underlying `Agent` model has
 *  `tags`/`group_name` columns a future sprint could expose. */
function compileTokenStmt(ast, catalog, opts = {}) {
  const errors = [];
  const head = ast.head;
  if (!head || head.name.toLowerCase() !== 'token') {
    errors.push(_err('only token(...) can be used standalone'));
    return { errors, fragment: null };
  }

  const kindArgs = (head.args || []).filter(a => a.key && a.key.toLowerCase() === 'kind');
  const otherArgs = (head.args || []).filter(a => !(a.key && a.key.toLowerCase() === 'kind'));
  for (const a of otherArgs) errors.push(_err(`token: unknown parameter '${a.key}'`));
  if (kindArgs.length > 1) errors.push(_err("token: duplicate parameter 'kind'"));
  if (kindArgs.length === 0) errors.push(_err("token: missing required param 'kind'"));

  let kind = null;
  if (kindArgs.length === 1) {
    const r = _coerceValueForSpec(kindArgs[0].value, _TOKEN_KIND_SPEC, 'kind', 'token');
    if (r.error) errors.push(r.error); else kind = r.value;
  }

  let body, conditions;
  if (kind === 'condition') {
    if (!ast.condition) {
      errors.push(_err('token: kind=condition requires a condition expression body'));
    } else {
      const condResult = compileCondition(ast.condition, catalog);
      errors.push(...condResult.errors);
      conditions = condResult.conditions;
    }
  } else if (kind === 'notify') {
    const bodyResult = compileAlertClause(ast.bodyCalls, null, catalog);
    errors.push(...bodyResult.errors);
    body = bodyResult.events;
  }
  // kind invalid/unresolved (e.g. missing/duplicate/bad enum) — the
  // errors above already explain why; skip body/condition compilation
  // rather than guessing which one was intended.

  const metaFields = compileMetaClause(ast.meta, errors);
  if (metaFields.tags) errors.push(_err('token: #tag is not supported for a Token — Token has no tags field'));
  if (metaFields.group) errors.push(_err('token: #group is not supported for a Token — Token has no group field'));
  if (!metaFields.name) errors.push(_err("token: missing required meta tag '#name' — a saved Token must have a name"));

  if (errors.length) return { errors, fragment: null };

  const fragment = { kind, name: metaFields.name };
  if (kind === 'condition') fragment.conditions = conditions;
  else fragment.body = body;
  if (metaFields.description) fragment.description = metaFields.description;
  return { errors: [], fragment };
}

/** Compile a standalone `cancel_all_orders(...)` call to a
 *  `{mode:'live', params:{scope, account?, symbol?, side?}}` object — the
 *  SAME shape `POST /api/orders/cancel-all`
 *  (`backend/api/schemas.py:CancelAllOrdersRequest`) accepts — Sprint
 *  [cancel-all-cli] (2026-10). Reuses `cancel_all_orders`'s EXISTING
 *  `grammar_kind=action` params_schema (`agent_grammar.yaml`) via
 *  `_compileGenericActionParams`, the SAME path a `DO cancel_all_orders(...)`
 *  action already compiles through — no second schema to drift out of
 *  sync. `rowRefCtx` is always `null` here: a bare statement has no WHEN
 *  condition to bind a matched row from, so `$row`-style references are
 *  never meaningful on this path (mirrors `compileBracketStmt`'s own
 *  always-null rowRefCtx usage).
 *
 *  Live-only safety gate: `POST /api/orders/cancel-all` has no `mode`
 *  parameter and never routes through `PaperTradeEngine` — it only ever
 *  cancels real broker orders (confirmed directly against
 *  `backend/api/routes/orders.py` + `actions_live.cancel_all_orders_scoped`).
 *  A paper-mode (or missing-mode) compile is refused outright with a clear
 *  message — this file's established convention for scope restrictions
 *  (see `compileStatement`'s own doc: "the UI never hides a button to
 *  fake this restriction, it relies on the compiler refusing the
 *  statement outright") — mirrors `compileOrderStmt`'s own `!opts.mode`
 *  refusal for the analogous bare-order case.
 *
 *  `scope="account"` without `account` is refused here too — a
 *  client-side mirror of `cancel_all_orders_scoped`'s own server-side
 *  `ValueError` (`actions_live.py`) — surfaced immediately in the UI
 *  rather than only after a round trip to the backend. `scope` defaults
 *  to `'total'` (matching the catalog's own declared default) when
 *  omitted, since this compiler applies no params_schema `default`s
 *  generically (`_compileGenericActionParams` only maps/coerces values
 *  actually TYPED) — explicit here so the JSON preview always shows the
 *  real scope that will be submitted, never a silently-absent key. */
function compileCancelAllOrdersStmt(ast, catalog, opts = {}) {
  const errors = [];
  const call = ast.call;
  if (!call || call.name.toLowerCase() !== 'cancel_all_orders') {
    errors.push(_err('only cancel_all_orders(...) can be used standalone'));
    return { errors, cancelAll: null };
  }
  if (ast.meta && ast.meta.length) {
    errors.push(_err('meta tags only apply to agents/brackets, not a bare cancel_all_orders statement'));
  }
  if (!opts.mode) {
    errors.push(_err('mode ("paper"|"live") is required to compile a bare cancel_all_orders statement'));
  } else if (opts.mode !== 'live') {
    errors.push(_err('cancel_all_orders is a live-only action — not available in paper mode'));
  }
  if (errors.length) return { errors, cancelAll: null };

  const entry = catalog && catalog.actions ? catalog.actions.get('cancel_all_orders') : null;
  if (!entry) {
    errors.push(_err('cancel_all_orders: action not found in catalog'));
    return { errors, cancelAll: null };
  }
  const r = _compileGenericActionParams(call, entry, null);
  errors.push(...r.errors);
  if (errors.length) return { errors, cancelAll: null };

  const params = { scope: 'total', ...r.params };
  if (String(params.scope).toLowerCase() === 'account' && !params.account) {
    errors.push(_err('cancel_all_orders: account is required when scope=account'));
    return { errors, cancelAll: null };
  }
  return { errors: [], cancelAll: { mode: 'live', params } };
}

/** Compile a standalone `help(<name>)` call to a `{params: {name}}`
 *  object — item 4, mirrors `compileCancelAllOrdersStmt`'s structure
 *  exactly, minus the live/paper mode gate (`help` is read-only — a
 *  catalog lookup, not a broker action — safe in any mode, so no `opts.mode`
 *  check at all). Reuses `_compileHelpCall`, the SAME dedicated bare-arg
 *  compiler a `DO help(...)`/preamble `help(...)` call already compiles
 *  through (see `_compileSharedCall`) — no second schema to drift out of
 *  sync, and no bare-word-vs-quoted-string rejection either (see that
 *  function's own header comment for why the generic path doesn't fit
 *  here). */
function compileHelpStmt(ast, catalog, opts = {}) {
  const errors = [];
  const call = ast.call;
  if (!call || call.name.toLowerCase() !== 'help') {
    errors.push(_err('only help(...) can be used standalone'));
    return { errors, help: null };
  }
  if (ast.meta && ast.meta.length) {
    errors.push(_err('meta tags only apply to agents/brackets, not a bare help statement'));
  }
  if (errors.length) return { errors, help: null };

  const entry = catalog && catalog.actions ? catalog.actions.get('help') : null;
  const r = _compileHelpCall(call, entry);
  errors.push(...r.errors);
  if (errors.length) return { errors, help: null };

  return { errors: [], help: { params: r.action.params } };
}

/** Compile an already-parsed AST (from `parseStatement`) to its JSON
 *  request shape. Returns `{ kind, errors, agent, ticket, basket,
 *  template, fragment }` — non-empty `errors` means every payload field is
 *  `null`.
 *
 *  `opts.allowedKinds` (Sprint B, 2026-10, Phase 2 "Console relocation") —
 *  an optional array restricting which of the five statement kinds
 *  (`'order'|'bracket'|'agent'|'event'|'token'`) this compile call accepts.
 *  Checked FIRST, before any kind-specific compiler runs, so a scope
 *  violation always surfaces as this one clear message — never a
 *  downstream error from a compiler that was never supposed to run (e.g.
 *  a `bracket(...)` missing `#name=` under an order-only scope must show
 *  THIS error, not "missing required meta tag '#name'"). This is the
 *  SOLE enforcement point for the order-scoped CLI mounts added this
 *  phase (`/orders`' CLI tab, the SymbolPanel order-ticket modal's CLI
 *  tab) — the UI never hides a button to fake this restriction, it relies
 *  on the compiler refusing the statement outright. `undefined`/omitted
 *  means no restriction (every existing caller — the full-scope Console
 *  tab on /automation — passes no `allowedKinds` at all). */
export function compileStatement(ast, catalog, opts = {}) {
  if (opts.allowedKinds && !opts.allowedKinds.includes(ast.kind)) {
    return {
      kind: ast.kind,
      errors: [_err('author agents/brackets on Automation → Console')],
      agent: null, ticket: null, basket: null, template: null, fragment: null, cancelAll: null, help: null,
    };
  }
  if (ast.kind === 'agent') {
    const r = compileAgentStmt(ast, catalog, opts);
    return { kind: 'agent', errors: r.errors, agent: r.agent, ticket: null, basket: null, template: null, fragment: null, cancelAll: null, help: null };
  }
  if (ast.kind === 'event') {
    const r = compileEventStmt(ast, catalog);
    return { kind: 'event', errors: r.errors, agent: r.agent, ticket: null, basket: null, template: null, fragment: null, cancelAll: null, help: null };
  }
  if (ast.kind === 'bracket') {
    const r = compileBracketStmt(ast, catalog, opts);
    return { kind: 'bracket', errors: r.errors, agent: null, ticket: null, basket: null, template: r.template, fragment: null, cancelAll: null, help: null };
  }
  if (ast.kind === 'token') {
    const r = compileTokenStmt(ast, catalog, opts);
    return { kind: 'token', errors: r.errors, agent: null, ticket: null, basket: null, template: null, fragment: r.fragment, cancelAll: null, help: null };
  }
  if (ast.kind === 'cancel_all_orders') {
    const r = compileCancelAllOrdersStmt(ast, catalog, opts);
    return { kind: 'cancel_all_orders', errors: r.errors, agent: null, ticket: null, basket: null, template: null, fragment: null, cancelAll: r.cancelAll, help: null };
  }
  // item 4 — `help(<name>)`, read-only, no mode/scope gate beyond the
  // generic `allowedKinds` check above (see `compileHelpStmt`'s own doc).
  if (ast.kind === 'help') {
    const r = compileHelpStmt(ast, catalog, opts);
    return { kind: 'help', errors: r.errors, agent: null, ticket: null, basket: null, template: null, fragment: null, cancelAll: null, help: r.help };
  }
  const r = compileOrderStmt(ast, catalog, opts);
  return { ...r, template: null, fragment: null, cancelAll: null, help: null };
}

/** One-shot parse + compile — the main entry point for Sprint 4's UI and
 *  for this sprint's tests. Never throws; always returns a structured
 *  result. `opts.mode` ("paper"|"live") is required for a single bare
 *  order_stmt; `opts.lotSizeOf(symbol) -> number|null` overrides the
 *  default instruments-cache lot-size lookup (used by tests).
 *  `opts.allowedKinds` (Sprint B, 2026-10) — forwarded verbatim to
 *  `compileStatement`, see that function's docstring for the full
 *  contract; this wrapper does no extra work for it. */
export function compileAgentCliStatement(text, catalog, opts = {}) {
  const { ast, errors: parseErrors } = parseStatement(text);
  if (parseErrors.length) {
    return { ok: false, errors: parseErrors, kind: null, agent: null, ticket: null, basket: null, template: null, fragment: null, cancelAll: null, help: null };
  }
  const result = compileStatement(ast, catalog, opts);
  if (result.errors && result.errors.length) {
    return { ok: false, errors: result.errors, kind: result.kind, agent: null, ticket: null, basket: null, template: null, fragment: null, cancelAll: null, help: null };
  }
  return {
    ok: true, errors: [], kind: result.kind,
    agent: result.agent || null, ticket: result.ticket || null, basket: result.basket || null,
    template: result.template || null, fragment: result.fragment || null,
    cancelAll: result.cancelAll || null, help: result.help || null,
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

/** True iff `s` can be written as an unquoted BARE_WORD meta value — any
 *  non-empty run of characters containing NONE of whitespace, ",", "#",
 *  "=", or a quote character (the grammar's own BARE_WORD definition —
 *  Sprint 7). DELIBERATELY WIDER than `_isBareable`'s NAME-shaped check
 *  (e.g. a hyphen like "hedge-book" is fine here, unlike `_isBareable`). */
function _isMetaBareWord(s) {
  return typeof s === 'string' && s.length > 0 && !/[\s,#="']/.test(s);
}

/** Render one meta value — bare when it's a safe BARE_WORD, quoted
 *  otherwise. The decompile-direction mirror of the meta lexer's own
 *  STRING-vs-BARE_WORD dispatch. */
function _renderMetaValue(s) {
  return _isMetaBareWord(s) ? { text: s } : _quoteString(s);
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

/** True iff `value` is EXACTLY the Sprint 16 single-key matched-row
 *  marker `{"$row": "<name>"}` — mirrors the backend's own
 *  `actions.py:_is_row_ref_marker` single-key check, so a legitimate
 *  multi-key object-literal param value is never misread as one. */
function _isRowRefMarker(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === 1
    && typeof value['$row'] === 'string';
}

/** Sprint [GIVEN-expr] — mirror of `_isRowRefMarker` above, for the new
 *  single-key `{"$given": "<name>"}` marker (an `'expr'`-kind GIVEN
 *  binding referenced from an action param / `msg=` / `debug(x)`). */
function _isGivenRefMarker(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length === 1
    && typeof value['$given'] === 'string';
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
  // Sprint 16 — a $row marker renders as the bare, unquoted exposed
  // column name (the exact CLI spelling that compiles back to this same
  // marker — see `_resolveRowRefOrNull`), REGARDLESS of the param's own
  // declared schema type (string/number/enum) — a $row-bound value is
  // never a literal of that type, so none of the type-specific render
  // branches below apply to it.
  if (_isRowRefMarker(value)) return { text: value['$row'] };
  // Sprint [GIVEN-expr] — same "render bare, regardless of declared
  // schema type" treatment as the $row marker above, for the new
  // `{"$given": name}` marker.
  if (_isGivenRefMarker(value)) return { text: value['$given'] };
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
  // Sprint 19 — both sides must carry the SAME scope_filter (including
  // "neither has one") before collapsing; a mismatched filter means these
  // aren't really the same qualified scope and must fall back to generic
  // per-leaf rendering.
  if (JSON.stringify(a.scope_filter || null) !== JSON.stringify(b.scope_filter || null)) return null;
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
  let scopeText = s.text;
  if (a.scope_filter !== undefined) {
    const qualSuffix = _decompileScopeFilter(a.scope_filter, []);
    if (qualSuffix == null) return null;
    scopeText = `${scopeText}${qualSuffix}`;
  }
  const v1 = _renderNumber(a.value);
  const v2 = _renderNumber(b.value);
  if (v1 == null || v2 == null) return null;
  return `${v1} ${op1} ${m.text}@${scopeText} ${op2} ${v2}`;
}

/** Decompile a single leaf `{metric, scope, op, value}` — the mirror of
 *  `compileLeaf`. Handles exactly the shapes the compiler can emit
 *  (native `between`, `in`/`not_in`, boolean-shorthand, boolean
 *  explicit, plain numeric comparator) and fails loud on anything else.
 *
 *  Sprint 15 fallback: `node.metric` may be a bare exposed-column name
 *  with NO registered metric token at all (e.g. `qty`, `symbol`,
 *  `realised` — real row columns, never hand-written metric resolvers).
 *  Resolve the scope FIRST (needed either way), then try the normal
 *  metric-call resolution; only on THAT failure, check whether
 *  `<scope>.<metric>` is a registered column — if so, render the whole
 *  leaf using the dotted `scope.column` form (`refText`) in place of
 *  every `metric@scope` spot below, since a pure column has no "@" form
 *  at all. A leaf whose metric genuinely resolves as a real metric token
 *  (e.g. `pnl`) is COMPLETELY UNAFFECTED by this fallback — it keeps
 *  decompiling to `pnl@positions.total` exactly as before; this is
 *  expected normalization (both spellings compile to the same JSON). */
function decompileLeaf(node, catalog, errors) {
  const s = _decompileResolveScopeString(node.scope, catalog);
  if (s.error) { errors.push(_err(s.error)); return null; }

  // Sprint 19 — a stored `scope_filter` renders as a trailing
  // `.qualifier:value` suffix on the scope text, applied BEFORE the
  // metric/column resolution below so both the `@`-metric-form and the
  // `between`/`in`/boolean ops downstream automatically pick it up via
  // the shared `refText`.
  let scopeText = s.text;
  if (node.scope_filter !== undefined) {
    const qualSuffix = _decompileScopeFilter(node.scope_filter, errors);
    if (qualSuffix == null) return null;
    scopeText = `${scopeText}${qualSuffix}`;
  }

  const m = _resolveMetricCall(node.metric, catalog);
  let refText, entry;
  if (!m.error) {
    refText = `${m.text}@${scopeText}`;
    entry = m.entry;
  } else {
    const colKey = `${s.text}.${node.metric}`.toLowerCase();
    const colEntry = catalog.columns && catalog.columns.get(colKey);
    if (!colEntry) { errors.push(_err(m.error)); return null; }
    // Sprint 21 — a bare-column metric has no "@" form, but it CAN carry
    // a scope_filter via the dot-access-with-qualifier mechanism
    // (`_resolveDotAccessWithQualifier`'s compile-side counterpart):
    // render `<scope><.qualifier:value><.column>`, e.g.
    // `quote.tracked.symbol:NIFTY25JULFUT.bid` — `scopeText` already
    // carries the qualifier suffix from above. Without a scope_filter,
    // this is byte-identical to the pre-Sprint-21 plain `colEntry.token`.
    refText = node.scope_filter !== undefined ? `${scopeText}.${node.metric}` : colEntry.token;
    entry = colEntry;
  }

  if (node.op === 'between') {
    if (!Array.isArray(node.value) || node.value.length !== 2
        || typeof node.value[0] !== 'number' || typeof node.value[1] !== 'number') {
      errors.push(_err(`leaf '${node.metric}@${node.scope}': 'between' op requires a 2-number value array`));
      return null;
    }
    const low = _renderNumber(node.value[0]), high = _renderNumber(node.value[1]);
    if (low == null || high == null) { errors.push(_err(`leaf '${node.metric}@${node.scope}': between bound cannot be rendered`)); return null; }
    return `${low} <= ${refText} <= ${high}`;
  }

  if (node.op === 'in' || node.op === 'not_in') {
    if (!Array.isArray(node.value)) {
      errors.push(_err(`leaf '${node.metric}@${node.scope}': '${node.op}' requires a list value`));
      return null;
    }
    const r = _renderListLiteral(node.value);
    if (r.error) { errors.push(_err(`leaf '${node.metric}@${node.scope}': ${r.message}`)); return null; }
    return `${refText} ${node.op === 'not_in' ? 'not in' : 'in'} ${r.text}`;
  }

  const valueType = entry ? entry.valueType : null;
  if (valueType === 'boolean') {
    if (node.op === '==' && node.value === true) return refText; // boolean shorthand
    if ((node.op === '==' || node.op === '!=') && typeof node.value === 'boolean') {
      return `${refText} ${node.op} ${node.value}`;
    }
    errors.push(_err(`leaf '${node.metric}@${node.scope}': unrecognized boolean leaf shape (op=${node.op}, value=${JSON.stringify(node.value)})`));
    return null;
  }

  if (['<', '<=', '>', '>=', '==', '!='].includes(node.op) && typeof node.value === 'number') {
    const numText = _renderNumber(node.value);
    if (numText == null) { errors.push(_err(`leaf '${node.metric}@${node.scope}': value ${node.value} cannot be rendered as a CLI numeric literal`)); return null; }
    return `${refText} ${node.op} ${numText}`;
  }

  // Sprint 21 — the decompile-direction mirror of `_compileComparatorValue`'s
  // new string-equality branch (e.g. `orders.any_acct.status == "OPEN"`).
  // Reuses `_renderListItem`'s existing bare/quoted string rendering (the
  // SAME rule `in`/`not_in` list items already use) rather than duplicating it.
  if ((node.op === '==' || node.op === '!=') && typeof node.value === 'string') {
    const r = _renderListItem(node.value);
    if (r.error) { errors.push(_err(`leaf '${node.metric}@${node.scope}': ${r.message}`)); return null; }
    return `${refText} ${node.op} ${r.text}`;
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

/** Decompile a `{"$ref": "<name>"}` condition-fragment node (Sprint 10) —
 *  the mirror of `compileRef`. Renders as the bare Token name, resolved
 *  against `catalog.metrics` (where a condition-kind Token is merged in —
 *  see `_buildCatalogEntry`'s `isFragmentRef` flag) so a stale/unknown
 *  reference fails loud rather than rendering an unresolvable name. */
function _decompileRef(node, catalog, errors) {
  const name = node['$ref'];
  if (typeof name !== 'string' || !name) {
    errors.push(_err(`'$ref' must be a non-empty string, got ${JSON.stringify(name)}`));
    return null;
  }
  const entry = catalog.metrics.get(name.toLowerCase());
  if (!entry || !entry.isFragmentRef) {
    errors.push(_err(`unknown Token reference '${name}' referenced in stored conditions`));
    return null;
  }
  return entry.token;
}

/** Recursive dispatcher — the mirror of `compileCondNode`. Dispatches on
 *  JSON SHAPE (not an AST node type — this operates directly on the
 *  stored `conditions` JSON): a `$ref` fragment (Sprint 10 — see
 *  `_decompileRef`), `all`/`any` composite, `not`, or a plain leaf (has
 *  `metric`+`scope` strings). Anything else fails loud, never guesses.
 *  Does NOT special-case `ALWAYS_LEAF` here (that check lives ONLY in
 *  `decompileCondition`, the top-level entry point, below) — advisor-
 *  flagged: `always` is standalone-only in the grammar (a parse error if
 *  combined with `&`/`|`), so a NESTED occurrence of the exact sentinel
 *  shape (e.g. inside an `all`/`any` alongside another leaf) must render
 *  as an ordinary leaf, never as the bare word `always`. */
function decompileCondNode(node, catalog, errors) {
  if (node == null || typeof node !== 'object' || Array.isArray(node)) {
    errors.push(_err(`unrecognized condition shape: ${JSON.stringify(node)}`));
    return null;
  }
  if ('$ref' in node) return _decompileRef(node, catalog, errors);
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

/** Decompile a stored `conditions` JSON tree back to `WHEN`-clause CLI
 *  text. Public entry point — checks the ONE condition that can only be
 *  evaluated at the whole-tree root (the `always` sentinel AT THE TOP,
 *  never nested — see `_isAlwaysLeafExact`'s own comment) before
 *  delegating to the recursive dispatcher, which handles a top-level
 *  `$ref` (Sprint 10 — see `_decompileRef`) the same as a nested one. */
export function decompileCondition(conditions, catalog) {
  // Operator-reported (2026-10-11, live-data smoke test against all real
  // prod agents) — a `null`/`undefined` `conditions` field is not a
  // malformed shape to flag with the generic message below; it's the
  // real, on-disk state of a handful of system bookkeeping agents (e.g.
  // the `manual` agent, `scope='manual'`, `is_system=True` — writes an
  // audit-trail event for every manually-placed order) that predate this
  // DSL entirely and were never meant to express a condition at all —
  // `run_cycle()` never auto-evaluates a `scope='manual'` agent, so there
  // was never a reason for one to have a real condition tree. Refuse
  // loudly with a SPECIFIC, dignified message instead of the generic
  // "unrecognized condition shape: null", mirroring `compileTokenStmt`'s
  // own refusal style for a condition-kind Token ("...isn't supported
  // from the CLI yet — edit via PUT /api/agents/{slug} directly, or ask
  // an admin"). This is a refusal, not a bug to work around — these rows
  // genuinely should not become CLI-editable.
  if (conditions === null || conditions === undefined) {
    return { text: null, errors: [_err(
      'this agent has no expressible condition (system/manual-trigger agent) — not editable via CLI'
    )] };
  }
  if (typeof conditions !== 'object') {
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
 *  `_mapCallArgsToKeys`'s symmetric "unknown parameter" rejection).
 *
 *  `message` (Sprint 6's `msg=` feature) is handled OUTSIDE the generic
 *  extraKeys loop above, since it is never declared in a real channel's
 *  own `params_schema` — `compileAlertClause` strips/re-attaches it the
 *  same way, never through `_mapCallArgsToKeys`. Per that compiler's own
 *  resolution invariant, a successfully-compiled `events` array is always
 *  in exactly one of two states: EVERY enabled entry carries a `message`
 *  key, or NONE do. A stored agent with SOME entries carrying `message`
 *  and others not (only reachable via hand-edited JSON, never the
 *  compiler) has NO CLI representation — under the compile-time
 *  resolution rule, a channel missing both its own message and a clause
 *  default when `msg` is used elsewhere is a hard compile error, so
 *  decompiling that exact mix could never recompile back to the same
 *  shape — and fails loud here rather than surfacing as a confusing
 *  "internal: decompiled text failed to recompile" from `decompileAgent`'s
 *  safety net. When every entry's `message` is the IDENTICAL string, it is
 *  hoisted to one leading clause-level `msg=` default and omitted from
 *  each channel (which then renders bare when it has no other args);
 *  otherwise each channel renders its own `msg=` individually, no shared
 *  default. Channels with no `message` key at all (today's normal,
 *  overwhelmingly common case) decompile exactly as before Sprint 6 — no
 *  `msg=` anywhere in the output. */
/** Resolve + render ONE channel-call-list entry back to CLI text pieces —
 *  the decompile-side mirror of `_resolveChannelCall`, shared by
 *  `decompileEvents` (ALERT's own `{channel, enabled, ...params}`
 *  flat-spread shape) and the new `notify[]` decompiler (`{channel,
 *  params}` nested shape, see `_compileNotifyClause`) — so a channel's
 *  own declared `params_schema` renders identically in both places, with
 *  zero channel-specific code in either caller.
 *
 *  `excludeKeys` names sibling keys on `ev` that are NEVER a real
 *  channel param when reading the FLAT-SPREAD shape (ALERT's own
 *  `enabled`/`message`, both validated separately by that caller, see
 *  below); `paramsKey`, when set, means `ev`'s own params live NESTED
 *  under that key instead (notify's shape) — mutually exclusive in
 *  practice (ALERT never sets `paramsKey`, notify never sets
 *  `excludeKeys`).
 *
 *  Returns `{ error }`, or `{ ref: true, text }` for a Token reference,
 *  or `{ entry, argParts }` for a real channel — `argParts` is the
 *  channel's own rendered `key=value` strings (possibly `[]`). */
function _resolveChannelEntryForDecompile(ev, catalog, { excludeKeys = [], paramsKey = null } = {}) {
  // Sprint 10 — a `{"$ref": "<name>"}` entry (a Token reference expanded
  // by `template_registry.py:resolve_events`) renders as the bare Token
  // name, same as `_decompileRef`'s condition-side mirror — it can never
  // carry params or its own `msg=` (see `_resolveChannelCall`'s
  // `isFragmentRef` branch).
  if (ev && typeof ev === 'object' && '$ref' in ev) {
    const name = ev['$ref'];
    if (typeof name !== 'string' || !name) return { error: `'$ref' must be a non-empty string, got ${JSON.stringify(name)}` };
    const refEntry = catalog.channels.get(name.toLowerCase());
    if (!refEntry || !refEntry.isFragmentRef) return { error: `unknown Token reference '${name}' referenced in stored events` };
    return { ref: true, text: refEntry.token };
  }
  if (!ev || typeof ev !== 'object' || typeof ev.channel !== 'string') {
    return { error: `unrecognized channel entry: ${JSON.stringify(ev)}` };
  }
  const entry = catalog.channels.get(ev.channel.toLowerCase());
  if (!entry) return { error: `unknown channel '${ev.channel}' referenced in stored events` };
  const rawParams = paramsKey ? (ev[paramsKey] || {}) : ev;
  const keys = paramsKey
    ? Object.keys(rawParams)
    : Object.keys(ev).filter((k) => k !== 'channel' && !excludeKeys.includes(k));
  const argParts = [];
  for (const k of keys) {
    const spec = entry.paramsSchema[k];
    if (!spec) return { error: `event '${entry.token}': unknown parameter '${k}' (not declared in its catalog schema)` };
    const r = _renderParamValue(rawParams[k], spec);
    if (r.error) return { error: `event '${entry.token}'.${k}: ${r.message}` };
    argParts.push(`${k}=${r.text}`);
  }
  return { entry, argParts };
}

export function decompileEvents(events, catalog) {
  if (!Array.isArray(events)) return { text: null, errors: [_err(`'events' must be an array, got ${JSON.stringify(events)}`)] };
  if (events.length === 0) return { text: 'nop', errors: [] };
  const errors = [];
  const rendered = [];
  for (const ev of events) {
    if (ev && typeof ev === 'object' && '$ref' in ev) {
      const r = _resolveChannelEntryForDecompile(ev, catalog);
      if (r.error) { errors.push(_err(r.error)); continue; }
      rendered.push({ ref: true, text: r.text });
      continue;
    }
    if (!ev || typeof ev !== 'object' || typeof ev.channel !== 'string') {
      errors.push(_err(`unrecognized event entry: ${JSON.stringify(ev)}`));
      continue;
    }
    if (!('enabled' in ev) || ev.enabled !== true) {
      errors.push(_err(`event '${ev.channel}': a disabled or missing-'enabled' channel entry has no CLI representation`));
      continue;
    }
    const r = _resolveChannelEntryForDecompile(ev, catalog, { excludeKeys: ['enabled', 'message'] });
    if (r.error) { errors.push(_err(r.error)); continue; }
    const { entry, argParts } = r;
    let message; // undefined = no 'message' key at all (the normal case)
    let bad = false;
    if ('message' in ev) {
      // Sprint [GIVEN-expr] — a `message` value is either a plain string
      // (the pre-existing shape) OR the new `{"$given": name}` marker (an
      // ALERT `msg=`/channel-own-`msg=` that resolved to an `'expr'`-kind
      // GIVEN binding) — anything else is still malformed.
      if (typeof ev.message === 'string' || _isGivenRefMarker(ev.message)) {
        message = ev.message;
      } else {
        errors.push(_err(`event '${entry.token}'.message: must be a string, got ${JSON.stringify(ev.message)}`));
        bad = true;
      }
    }
    if (bad) continue;
    rendered.push({ entry, argParts, message });
  }
  if (errors.length) return { text: null, errors };

  // Sprint [GIVEN-expr] — render a message value (string OR `$given`
  // marker) to its `msg=...` CLI text; a marker renders bare/unquoted
  // (the exact label name, same as `_renderParamValue`'s own treatment).
  const _renderMsg = (m) => (_isGivenRefMarker(m) ? { text: m['$given'] } : _quoteString(m));
  // Sprint [GIVEN-expr] — a comparison KEY for "are these two messages
  // the same", since two distinct `{"$given": name}` OBJECTS (one per
  // channel entry, even when they name the SAME binding) are never `===`
  // to each other the way two identical strings are.
  const _msgKey = (m) => (_isGivenRefMarker(m) ? `$given:${m['$given']}` : m);

  // Message hoisting (Sprint 6) only ever considers PLAIN channel entries —
  // a `ref: true` entry (Sprint 10) is excluded from both the numerator and
  // denominator below, since it can never carry a `message` at all.
  const plain = rendered.filter(r => !r.ref);
  const withMsg = plain.filter(r => typeof r.message === 'string' || _isGivenRefMarker(r.message));
  if (withMsg.length > 0 && withMsg.length < plain.length) {
    const missing = plain.find(r => !(typeof r.message === 'string' || _isGivenRefMarker(r.message)));
    return {
      text: null,
      errors: [_err(`channel '${missing.entry.token}' has no message while other channels in this ALERT do — this shape has no CLI representation`)],
    };
  }

  const uniform = withMsg.length === plain.length && plain.length > 0
    && plain.every(r => _msgKey(r.message) === _msgKey(plain[0].message));

  let defaultPrefix = '';
  if (uniform) {
    const q = _renderMsg(plain[0].message);
    if (q.error) return { text: null, errors: [_err(q.message)] };
    defaultPrefix = `msg=${q.text}, `;
  }

  const parts = [];
  for (const r of rendered) {
    if (r.ref) { parts.push(r.text); continue; }
    const argParts = [...r.argParts];
    if (!uniform && (typeof r.message === 'string' || _isGivenRefMarker(r.message))) {
      const q = _renderMsg(r.message);
      if (q.error) return { text: null, errors: [_err(q.message)] };
      argParts.push(`msg=${q.text}`);
    }
    parts.push(argParts.length === 0 ? r.entry.token : `${r.entry.token}(${argParts.join(', ')})`);
  }
  return { text: `${defaultPrefix}${parts.join(', ')}`, errors: [] };
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
  // Sprint 16 — a $row-bound qty (e.g. `{"$row": "qty"}`) has no
  // compile-time numeric value to attempt a lots= conversion against,
  // and is not itself a number/string — it is excluded from the
  // malformed-shape guard below (which predates this marker shape) and
  // renders via the exact same `_renderParamValue` path every other
  // param uses, just always as plain `qty=`, never `lots=`.
  const qtyIsRowRef = _isRowRefMarker(qty);
  if (!qtyIsRowRef && typeof qty !== 'string' && (typeof qty !== 'number' || !Number.isFinite(qty))) {
    return { error: `order: qty is neither a number nor an expression string — malformed place_order action` };
  }

  // Try the nicer lots= form first — only when it converts cleanly.
  let qtyArgText = null;
  if (!qtyIsRowRef && typeof qty === 'number') {
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
  // expression string automatically, renders a $row marker bare).
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
/** Render an action's `notify[]` sibling array (see `_compileNotifyClause`)
 *  back to `notify=[...]`'s bracket-content text (WITHOUT the leading
 *  `notify=` — the caller splices that into the action call's own
 *  argument list, see `_appendNotifyArgText`) — shares
 *  `_resolveChannelEntryForDecompile` with `decompileEvents`, so a
 *  channel's own params render identically in both places. `notify` is
 *  required non-empty here (the compiler never emits an empty array —
 *  see `_compileNotifyClause`); the caller checks presence first. */
function _renderNotifyListText(notify, catalog) {
  if (!Array.isArray(notify) || notify.length === 0) {
    return { text: null, errors: [_err(`'notify' must be a non-empty array when present, got ${JSON.stringify(notify)}`)] };
  }
  const errors = [];
  const parts = [];
  for (const ev of notify) {
    const r = _resolveChannelEntryForDecompile(ev, catalog, { paramsKey: 'params' });
    if (r.error) { errors.push(_err(r.error)); continue; }
    if (r.ref) { parts.push(r.text); continue; }
    parts.push(r.argParts.length === 0 ? r.entry.token : `${r.entry.token}(${r.argParts.join(', ')})`);
  }
  if (errors.length) return { text: null, errors };
  return { text: `[${parts.join(', ')}]`, errors: [] };
}

/** Splice a rendered `notify=[...]` arg into an already-rendered action
 *  call's text — `text` is either a bare token (zero other params, e.g.
 *  `deactivate_agent`) or a `token(...)` call; either shape gets
 *  `notify=` appended as one more argument, no other change. */
function _appendNotifyArgText(text, notifyArgText) {
  if (notifyArgText == null) return text;
  const arg = `notify=${notifyArgText}`;
  return text.endsWith(')') ? `${text.slice(0, -1)}, ${arg})` : `${text}(${arg})`;
}

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
    // Sprint [grammar-unify] — `bracket` is NEVER a real catalog action
    // (`catalog.actions.get('bracket')` is always `undefined` — see
    // `_compileSharedCall`'s own bracket branch) — checked BEFORE the
    // catalog lookup below, which would otherwise fail this entry as
    // "unknown action" before ever reaching a bracket-specific render
    // path. No `notify=` support (bracket never compiles one).
    if (lookupName === 'bracket') {
      const r = _decompileBracketCallFields(act.params, catalog);
      if (r.errors.length) { errors.push(...r.errors); continue; }
      parts.push(r.text);
      continue;
    }
    // Sprint [order-unify] — tolerant catalog lookup: `order` is the sole
    // name the compiler ever EMITS going forward (see `compileDoClause`),
    // but stored JSON may still carry the legacy `place_order` (any
    // pre-backfill row, or a hand-authored shape) — both must decompile
    // identically. Falls back to the real `place_order` catalog row when
    // looked up as `order` and the backend hasn't (yet) seeded a literal
    // `order` row of its own.
    const entry = catalog.actions.get(lookupName)
      || (lookupName === 'order' ? catalog.actions.get('place_order') : null);
    if (!entry) { errors.push(_err(`unknown action '${act.type}' referenced in stored actions`)); continue; }

    // Part B (2026-10) — `notify=[...]` is a SIBLING key on the action
    // dict (never inside `params`, see `_compileNotifyClause`'s own
    // header comment); rendered once here (regardless of which branch
    // below renders the action's own call text) and spliced in via
    // `_appendNotifyArgText`. Absent/`undefined` is the overwhelmingly
    // common case — zero behavior change for every action that doesn't
    // use this.
    let notifyArgText = null;
    if (act.notify !== undefined) {
      const nr = _renderNotifyListText(act.notify, catalog);
      if (nr.errors.length) { errors.push(...nr.errors); continue; }
      notifyArgText = nr.text;
    }

    // Sprint [order-unify] — a stored action's `type` may be EITHER the
    // new canonical `'order'` or the legacy `'place_order'` (see the
    // tolerant lookup above) — both decompile via the exact same
    // `_decompileOrderAction` path, rendering `order(...)` either way.
    if (lookupName === 'order' || lookupName === 'place_order') {
      const r = _decompileOrderAction(act.params, entry, opts);
      if (r.error) { errors.push(_err(r.error)); continue; }
      parts.push(_appendNotifyArgText(r.text, notifyArgText));
      continue;
    }
    // Sprint [GIVEN-expr] — render the `debug(x)` sugar form when the
    // message is the new, genuinely-live `{"$given": name}` marker (an
    // operator-expression GIVEN binding) — byte-for-byte round-trip,
    // same as the pre-existing `$row` case directly below.
    if (lookupName === 'log' && act.params && act.params.level === 'debug' && _isGivenRefMarker(act.params.message) && notifyArgText == null) {
      parts.push(`debug(${act.params.message['$given']})`);
      continue;
    }
    // Sprint G — render the `debug(x)` sugar form ONLY when the ORIGINAL
    // label/column name is actually recoverable: a `$row` marker carries
    // its own exposed-column name verbatim, so `debug(x)` round-trips
    // byte-for-byte. A GIVEN-bound label's value/condition is NOT
    // recoverable (the compiled JSON only ever holds the EXPANDED literal/
    // JSON-stringified form, by the macro's own documented design — see
    // the GIVEN section header comment) — those fall through to the
    // generic `emit_log(level=debug, message=...)` rendering below, same
    // as any OTHER hand-authored `emit_log(level=debug, ...)` the operator
    // might have typed directly without ever using the `debug(...)`
    // sugar — there is no way to tell the two apart from stored JSON
    // alone, and there doesn't need to be: both are equally valid, and
    // re-saving either one is a no-op round trip. Skipped entirely when
    // `notify` is present — `debug(x)`'s own grammar takes exactly one
    // bare argument, with no room for a second `notify=` kwarg — falling
    // through to the generic rendering below instead, which supports it.
    if (lookupName === 'log' && act.params && act.params.level === 'debug' && _isRowRefMarker(act.params.message) && notifyArgText == null) {
      parts.push(`debug(${act.params.message['$row']})`);
      continue;
    }
    // item 4 — `help(x)`'s sole argument is ALWAYS the lexer's bare-arg
    // sugar (see `lexAgentStatement`'s own comment) — there is no
    // "hand-typed help(name=...)" alternate spelling at all. The generic
    // `_decompileGenericAction` path below would render it as the
    // schema-typed STRING form, `help(name="pnl")`, which the lexer then
    // rejects on recompile (its bare-arg sugar requires an unquoted bare
    // word) — the exact same mismatch `debug(x)`'s own dedicated branches
    // above exist to avoid. Mirrors `_compileHelpCall`'s shape exactly.
    if (lookupName === 'help' && act.params && typeof act.params.name === 'string' && notifyArgText == null) {
      parts.push(`help(${act.params.name})`);
      continue;
    }
    const r = _decompileGenericAction(act.params, entry);
    if (r.error) { errors.push(_err(r.error)); continue; }
    parts.push(_appendNotifyArgText(r.text, notifyArgText));
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
/** Decompile `{ name?, description?, tags?, group? }` back to a trailing
 *  `#...` meta clause — the inverse of `compileMetaClause`. GENERIC,
 *  reusable helper (also usable by a future bracket decompiler). Omits
 *  any individual key whose value is empty/unset; omits the WHOLE clause
 *  (returns `''`) when all four are empty/unset — an agent that predates
 *  Sprint 7 (e.g. has only a real `name` from the Structured tab) still
 *  decompiles correctly, rendering just `#name=...`. Returns
 *  `{ text, errors }` — `text` is `null` only on a genuine rendering
 *  failure (e.g. a tag string containing both quote characters), never a
 *  silent guess. */
export function decompileMetaClause(agent) {
  const errors = [];
  const parts = [];
  const a = agent || {};

  if (typeof a.name === 'string' && a.name !== '') {
    const r = _renderMetaValue(a.name);
    if (r.error) errors.push(_err(`#name: ${r.message}`)); else parts.push(`#name=${r.text}`);
  }
  if (typeof a.description === 'string' && a.description !== '') {
    const r = _renderMetaValue(a.description);
    if (r.error) errors.push(_err(`#desc: ${r.message}`)); else parts.push(`#desc=${r.text}`);
  }
  if (Array.isArray(a.tags) && a.tags.length > 0) {
    const rendered = [];
    let bad = false;
    for (const t of a.tags) {
      if (typeof t !== 'string' || t === '') { errors.push(_err('#tag: every tag must be a non-empty string')); bad = true; continue; }
      const r = _renderMetaValue(t);
      if (r.error) { errors.push(_err(`#tag: ${r.message}`)); bad = true; continue; }
      rendered.push(r.text);
    }
    if (!bad) parts.push(`#tag=${rendered.join(',')}`);
  }
  if (typeof a.group === 'string' && a.group !== '') {
    const r = _renderMetaValue(a.group);
    if (r.error) errors.push(_err(`#group: ${r.message}`)); else parts.push(`#group=${r.text}`);
  }

  if (errors.length) return { text: null, errors };
  return { text: parts.join(' '), errors: [] };
}

/** Sprint [GIVEN-parts] — render a `parts[]` array (see
 *  `_flattenPlusChainToParts`) back to its `+`-chain CLI source text —
 *  the decompile-direction mirror. NOT "read back verbatim" the way
 *  `expr` is (there is no stored source string for a `parts`-kind
 *  binding at all) — this is a FRESH render straight from the parts
 *  array, which is lossless for this flat-chain shape (every part is
 *  either a literal string or a `{"$given": name}` reference, and both
 *  render back unambiguously). A plain string part renders as a quoted
 *  string literal (via `_quoteString`, same quoting rules as every other
 *  string value in this file); a `{"$given": name}` part renders as the
 *  bare name. Returns `{ text }` or `{ error }`. */
function _decompileGivenPartsToExprText(parts) {
  if (!Array.isArray(parts) || parts.length === 0) {
    return { error: `'parts' must be a non-empty array, got ${JSON.stringify(parts)}` };
  }
  const pieces = [];
  for (const p of parts) {
    if (typeof p === 'string') {
      const q = _quoteString(p);
      if (q.error) return { error: q.message };
      pieces.push(q.text);
      continue;
    }
    if (_isGivenRefMarker(p)) { pieces.push(p['$given']); continue; }
    return { error: `unrecognized 'parts' entry ${JSON.stringify(p)}` };
  }
  return { text: pieces.join(' + ') };
}

/** Sprint [GIVEN-expr]/[GIVEN-parts] — render `agent.given[]` back to a
 *  leading `GIVEN name1 = expr1; name2 = expr2; ...` line. An `expr`-kind
 *  entry reads its `expr` string back VERBATIM — never re-derived/
 *  reformatted from any AST — the explicit architect-council watch-point
 *  for this sprint: a round-trip mismatch here would be a silent bug. A
 *  `parts`-kind entry (string-building — see `_arithContainsStringLiteral`'s
 *  own docstring for why this is a separate compiled shape) has no stored
 *  source text at all, so it is instead FRESHLY rendered from its own
 *  `parts[]` array via `_decompileGivenPartsToExprText` — lossless for
 *  this flat-chain shape, so the round-trip contract still holds even
 *  though the mechanism differs. This is the ONLY place `given` round-
 *  trips at all: a pure-alias/primitive GIVEN binding has always
 *  decompiled via its own already-inlined expansion inside `conditions`/
 *  `actions` (zero backend footprint, Sprint G's original design) — an
 *  `'expr'`/`'parts'`-kind binding is new and has no other home. Returns
 *  `{ text: '', errors: [] }` when `given` is absent/empty (every pre-
 *  existing agent) — the overwhelming common case, zero output change. */
function _decompileGivenPreamble(given) {
  if (!Array.isArray(given) || given.length === 0) return { text: '', errors: [] };
  const errors = [];
  const parts = [];
  for (const g of given) {
    if (!g || typeof g.name !== 'string' || !g.name) {
      errors.push(_err(`a 'given' entry is missing a valid 'name'`));
      continue;
    }
    if (Array.isArray(g.parts)) {
      const r = _decompileGivenPartsToExprText(g.parts);
      if (r.error) { errors.push(_err(`given.${g.name}: ${r.error}`)); continue; }
      parts.push(`${g.name} = ${r.text}`);
    } else if (typeof g.expr === 'string' && g.expr !== '') {
      parts.push(`${g.name} = ${g.expr}`);
    } else {
      errors.push(_err(`given.${g.name}: must carry either a non-empty 'expr' string or a 'parts' array`));
    }
  }
  if (errors.length) return { text: null, errors };
  // Sprint [grammar-unify] — NO leading `GIVEN ` keyword any more; the
  // preamble is self-delimiting by position (bounded by `WHEN`, or
  // end-of-input — see `parseAgentStmt`'s own header comment), so there
  // is nothing left for the keyword to mark.
  return { text: `${parts.join('; ')}\n`, errors: [] };
}

/** Decompile a preamble/DO-position `bracket(...)` call's OWN fields
 *  (`{applies_to?, tp_order_type?, ...}` — the SAME shape
 *  `_compileBracketCallFields` produces) back to its `bracket(...)` call
 *  text — the mirror of THAT function, not of the full `decompileBracket`
 *  (which additionally requires `#name=`/meta that a preamble/DO
 *  bracket call never carries — see `_compileSharedCall`'s own bracket
 *  branch). A zero-argument result renders bare (`bracket`, no parens),
 *  matching `parseCall`'s own "a zero-argument token is always written
 *  bare" rule. */
function _decompileBracketCallFields(fields, catalog) {
  const t = fields || {};
  const errors = [];
  const argParts = [];
  if (t.applies_to != null) {
    if (typeof t.applies_to !== 'string') {
      errors.push(_err('bracket.applies_to: expected an enum value'));
    } else {
      argParts.push(`applies_to=${_isBareable(t.applies_to) ? t.applies_to : _quoteString(t.applies_to).text}`);
    }
  }
  const tpSlWingResult = _decompileTpSlWingFields(t, 'template', 'bracket');
  errors.push(...tpSlWingResult.errors);
  argParts.push(...tpSlWingResult.argParts);
  if (errors.length) return { text: null, errors };
  return { text: argParts.length ? `bracket(${argParts.join(', ')})` : 'bracket', errors: [] };
}

/** Decompile `agent.preamble[]` (Sprint [grammar-unify]) back to one CLI
 *  line PER entry, in textual order — the mirror of `compilePreambleClause`.
 *  `{type:'alert', events}` reuses `decompileEvents` UNCHANGED (the exact
 *  same body grammar `alert(...)`'s parens now wrap, see `parseCall`'s
 *  own `alert`-specific branch) and wraps it as `alert(...)`.
 *  `{type:'bracket', params}` goes through the dedicated
 *  `_decompileBracketCallFields` above. Every other entry is the
 *  ordinary `{type, params}` action shape `decompileActions` already
 *  renders ONE of — reused by wrapping it in a one-element array (never
 *  the `'nop'`/empty-array shortcut, since this is always called with a
 *  REAL single entry). */
function _decompilePreambleEntries(preamble, catalog, opts) {
  if (!Array.isArray(preamble) || preamble.length === 0) return { text: '', errors: [] };
  const errors = [];
  const lines = [];
  for (const entry of preamble) {
    if (!entry || typeof entry !== 'object' || typeof entry.type !== 'string') {
      errors.push(_err(`unrecognized preamble entry: ${JSON.stringify(entry)}`));
      continue;
    }
    if (entry.type === 'alert') {
      const r = decompileEvents(entry.events || [], catalog);
      if (r.errors.length) { errors.push(...r.errors); continue; }
      lines.push(`alert(${r.text})`);
      continue;
    }
    if (entry.type === 'bracket') {
      const r = _decompileBracketCallFields(entry.params, catalog);
      if (r.errors.length) { errors.push(...r.errors); continue; }
      lines.push(r.text);
      continue;
    }
    const r = decompileActions([entry], catalog, opts);
    if (r.errors.length) { errors.push(...r.errors); continue; }
    lines.push(r.text);
  }
  if (errors.length) return { text: null, errors };
  return { text: lines.join('\n'), errors: [] };
}

/** Decompile a stored `kind='cycle'` agent back to its unified CLI text
 *  (Sprint [grammar-unify] rewrite of the old `GIVEN ... WHEN ... ALERT
 *  ... DO ...` four-clause renderer) — the reverse of `compileAgentStmt`.
 *  Shape: every preamble line first (label-assignment bindings via
 *  `_decompileGivenPreamble`, then every `agent.preamble[]` call via
 *  `_decompilePreambleEntries`, each on its own line, textual order
 *  preserved within each source but NOT interleaved between the two —
 *  `given`/`preamble` are compiled as two separate fields, so their
 *  relative order from the ORIGINAL source text is not preserved
 *  byte-for-byte; this is a KNOWN, accepted decompile-fidelity gap, not a
 *  correctness bug — re-saving the decompiled text still compiles to the
 *  identical `conditions`/`events`/`actions`/`given`/`preamble` JSON,
 *  which is the actual round-trip contract this function's own recompile
 *  safety net below verifies), THEN — only when there is any real
 *  conditional content at all (`events`/`actions` not both empty) — a
 *  single `WHEN <condition> DO <calls>` line, with a DO-position
 *  `alert(...)` call synthesized from `events` (collapsing the old
 *  separate ALERT clause back into one DO call list, mirroring item 2's
 *  compile-side collapse). A preamble-only agent (empty `events/actions`,
 *  non-empty `preamble`/`given`) renders with NO `WHEN`/`DO` line at
 *  all — the plan's own "a preamble with no WHEN...DO... at all is a
 *  complete, valid, standalone statement" requirement, honored on the
 *  decompile side too. */
export function decompileAgent({ conditions, events, actions, given = undefined, preamble = undefined, name = undefined, description = undefined, tags = undefined, group = undefined }, catalog, opts = {}) {
  const condResult = decompileCondition(conditions, catalog);
  const eventsResult = decompileEvents(events, catalog);
  const actionsResult = decompileActions(actions, catalog, opts);
  const metaResult = decompileMetaClause({ name, description, tags, group });
  const givenResult = _decompileGivenPreamble(given);
  const preambleResult = _decompilePreambleEntries(preamble, catalog, opts);
  const errors = [
    ...condResult.errors, ...eventsResult.errors, ...actionsResult.errors,
    ...metaResult.errors, ...givenResult.errors, ...preambleResult.errors,
  ];
  const evLen = Array.isArray(events) ? events.length : null;
  const acLen = Array.isArray(actions) ? actions.length : null;
  const hasConditional = !(evLen === 0 && acLen === 0);
  const hasPreambleContent = !!(givenResult.text || preambleResult.text);
  if (!hasConditional && !hasPreambleContent) {
    errors.push(_err('an agent must have at least one preamble call/binding or a WHEN ... DO ... clause'));
  }
  if (errors.length) return { text: null, errors };

  const metaSuffix = metaResult.text ? ` ${metaResult.text}` : '';
  const preambleLines = [];
  if (givenResult.text) preambleLines.push(givenResult.text.replace(/\n$/, ''));
  if (preambleResult.text) preambleLines.push(preambleResult.text);
  const preambleBlock = preambleLines.length ? `${preambleLines.join('\n')}\n` : '';
  let whenBlock = '';
  if (hasConditional) {
    const doParts = [];
    if (eventsResult.text !== 'nop') doParts.push(`alert(${eventsResult.text})`);
    if (actionsResult.text !== 'nop') doParts.push(actionsResult.text);
    const doText = doParts.length ? doParts.join(', ') : 'nop';
    whenBlock = `WHEN ${condResult.text} DO ${doText}`;
  }
  const text = `${preambleBlock}${whenBlock}${metaSuffix}`.replace(/^\n+/, '');
  const roundTrip = compileAgentCliStatement(text, catalog, opts);
  if (!roundTrip.ok) {
    const detail = (roundTrip.errors && roundTrip.errors[0] && roundTrip.errors[0].message) || 'unknown error';
    return { text: null, errors: [_err(`internal: decompiled text failed to recompile (${detail})`)] };
  }
  return { text, errors: [] };
}

/** Decompile ONE `conditions.any[i].log` entry (Sprint 18 follow-up) back
 *  to its `{tag=..., min_level=..., where={...}}` object-literal CLI
 *  text — the mirror of `_compileMatchList`'s per-entry handling inside
 *  `compileEventStmt`. `tag` is rendered bare-or-quoted (same
 *  `_freeTextValue`-equivalent convention as the simple form's own
 *  `log_tag=`); `min_level` always renders explicitly (never omitted,
 *  even when it was defaulted from an absent key — matches the simple
 *  form's own always-explicit `min_level=` rendering). Returns
 *  `{ text, errors }` — `text` is `null` when `errors` is non-empty. */
function _decompileMatchEntry(log, idx) {
  const errors = [];
  if (!log || typeof log !== 'object' || Array.isArray(log)) {
    errors.push(_err(`match[${idx}]: expected a {"log": {...}} entry`));
    return { text: null, errors };
  }
  if (typeof log.tag !== 'string' || !log.tag) {
    errors.push(_err(`match[${idx}].tag must be a non-empty string`));
    return { text: null, errors };
  }
  const tagR = _isBareable(log.tag) ? { text: log.tag } : _quoteString(log.tag);
  if (tagR.error) { errors.push(_err(`match[${idx}].tag: ${tagR.message}`)); return { text: null, errors }; }
  const rawLevel = 'min_level' in log ? log.min_level : _MIN_LEVEL_DEFAULT;
  if (typeof rawLevel !== 'string' || !_MIN_LEVEL_SPEC.enum.includes(String(rawLevel).toUpperCase())) {
    errors.push(_err(`match[${idx}].min_level ${JSON.stringify(log.min_level)} is not a recognized level`));
    return { text: null, errors };
  }
  const minLevel = String(rawLevel).toUpperCase();
  const parts = [`tag=${tagR.text}`, `min_level=${minLevel}`];
  if ('where' in log) {
    if (!log.where || typeof log.where !== 'object' || Array.isArray(log.where)) {
      errors.push(_err(`match[${idx}].where must be an object`));
      return { text: null, errors };
    }
    const whereR = _renderObjectLiteral(log.where);
    if (whereR.error) { errors.push(_err(`match[${idx}].where: ${whereR.message}`)); return { text: null, errors }; }
    parts.push(`where=${whereR.text}`);
  }
  return { text: `{${parts.join(', ')}}`, errors: [] };
}

/** Decompile a stored `kind='event'` agent's `{conditions, events,
 *  actions}` (plus the shared meta fields) back to its equivalent `ALERT
 *  log_tag=... render=... ...` CLI text (Sprint 12) — the reverse of
 *  `compileEventStmt`. Distinct from `decompileAgent` (which only ever
 *  handles the ordinary `WHEN/ALERT/DO` shape) since a `kind='event'`
 *  row's `conditions`/`actions` are a STRUCTURALLY DIFFERENT shape
 *  (`{"log": {...}}`, not a WHEN condition tree; a single
 *  `[{"type":"render","render":...}]` entry, not a generic DO action
 *  list) — reuses `decompileEvents` UNCHANGED for the `events` array (the
 *  ONE piece that genuinely is the same shape ALERT's own channel list
 *  uses) plus the shared `decompileMetaClause` helper every other
 *  decompiler in this file already uses.
 *
 *  Fails loud (never guesses) on anything this syntax can't express yet —
 *  e.g. a `conditions` tree wrapped in `any`/`all`/`not` (multiple log
 *  leaves ORed/ANDed together — two real seeded agents,
 *  `template-attach-urgent`/`template-attach-high`, use this shape) has
 *  no CLI representation yet: `event_meta` carries exactly one
 *  `log_tag=`/`min_level=`/`where=` triple, with no syntax today for a
 *  second, independently-thresholded leaf. Same "fail loud, don't
 *  silently drop/collapse a field that would change behavior on resave"
 *  convention `decompileEvents`/`decompileActions` already follow — this
 *  is a genuine grammar-extension gap, not a shortcut taken here.
 *
 *  `actions[0]`'s extra keys (beyond `type`/`render`, e.g. the real
 *  seeded `error-alert` agent's repeat-dedup `"gate"` key) are resolved
 *  against `catalog.renderAction`'s own params_schema (Sprint 18 — see
 *  `buildCatalog`'s own comment) the same generic, catalog-driven way
 *  `decompileEvents` already resolves a channel's own extra keys — an
 *  undeclared key (or a stale catalog missing the `render_action` row
 *  entirely) still fails loud, never silently dropped.
 *
 *  Sprint 18 follow-up — `conditions` wrapped in `{"any": [{"log":...},
 *  {"log":...}]}` (the real seeded `template-attach-urgent`/
 *  `template-attach-high` shape the comment above used to flag as a gap)
 *  now decompiles to `match=[{...}, {...}]` via `_decompileMatchEntry`
 *  below — the ONE remaining unsupported `conditions` shape is anything
 *  other than a bare `{"log":...}` or a flat `{"any": [{"log":...}, ...]}`
 *  (e.g. `all`/`not`/a nested `any`), which still fails loud exactly as
 *  before. */
export function decompileEventAgent({ conditions, events, actions, name = undefined, description = undefined, tags = undefined, group = undefined }, catalog, opts = {}) {
  const errors = [];

  const isPlainObj = conditions && typeof conditions === 'object' && !Array.isArray(conditions);
  const log = isPlainObj ? conditions.log : null;
  const anyList = isPlainObj && Array.isArray(conditions.any) ? conditions.any : null;
  if (!log && !anyList) {
    errors.push(_err(`event agent: 'conditions' must be a {"log": {...}} or {"any": [{"log":...}, ...]} shape, got ${JSON.stringify(conditions)}`));
  }

  let logTag = null, minLevel = null, whereObj = null, matchEntryTexts = null;
  if (log) {
    if (typeof log.tag !== 'string' || !log.tag) {
      errors.push(_err('event agent: conditions.log.tag must be a non-empty string'));
    } else {
      logTag = log.tag;
    }
    const rawLevel = 'min_level' in log ? log.min_level : _MIN_LEVEL_DEFAULT;
    if (typeof rawLevel !== 'string' || !_MIN_LEVEL_SPEC.enum.includes(String(rawLevel).toUpperCase())) {
      errors.push(_err(`event agent: conditions.log.min_level ${JSON.stringify(log.min_level)} is not a recognized level`));
    } else {
      minLevel = String(rawLevel).toUpperCase();
    }
    if ('where' in log) {
      if (!log.where || typeof log.where !== 'object' || Array.isArray(log.where)) {
        errors.push(_err('event agent: conditions.log.where must be an object'));
      } else {
        whereObj = log.where;
      }
    }
  } else if (anyList) {
    if (anyList.length === 0) {
      errors.push(_err('event agent: conditions.any must contain at least one {"log": {...}} entry'));
    } else {
      matchEntryTexts = [];
      anyList.forEach((item, i) => {
        const entryLog = item && typeof item === 'object' && !Array.isArray(item) ? item.log : null;
        const r = _decompileMatchEntry(entryLog, i);
        if (r.errors.length) errors.push(...r.errors); else matchEntryTexts.push(r.text);
      });
    }
  }

  const eventsResult = decompileEvents(events, catalog);
  errors.push(...eventsResult.errors);
  // Sprint 18 — an empty channel list (`events: []`, `eventsResult.text
  // === 'nop'`) IS now expressible (`parseEventStmt` accepts bare `nop`
  // for the channel list too — see its own comment) — the real,
  // confirmed-intentional shape of the seeded `cycle-breach-alert` agent,
  // which supplies channels dynamically at render time. No longer
  // rejected here.

  let renderName = null;
  const extraEventKwParts = [];
  if (!Array.isArray(actions) || actions.length !== 1 || !actions[0] || typeof actions[0] !== 'object') {
    errors.push(_err(`event agent: 'actions' must be a single-entry [{"type":"render","render":"<name>"}] array, got ${JSON.stringify(actions)}`));
  } else {
    const act = actions[0];
    const extraKeys = Object.keys(act).filter((k) => k !== 'type' && k !== 'render');
    if (act.type !== 'render' || typeof act.render !== 'string' || !act.render) {
      errors.push(_err(`event agent: actions[0] must be {"type":"render","render":"<name>"}, got ${JSON.stringify(act)}`));
    } else {
      renderName = act.render;
      // Sprint 18 — resolved generically against `catalog.renderAction`'s
      // own params_schema (backend's `event_actions.render.params_schema`,
      // via `GET /tokens?grammar=action`'s synthetic `render_action` row)
      // instead of a hardcoded `{type, render}` set. `catalog.renderAction`
      // is `null` for a stale/older catalog that hasn't shipped this row
      // yet — every extra key then fails loud, same as an undeclared key
      // against a real schema.
      const schema = (catalog.renderAction && catalog.renderAction.paramsSchema) || {};
      for (const k of extraKeys) {
        const spec = schema[k];
        if (!spec) { errors.push(_err(`event agent: actions[0] has extra key '${k}' not expressible by this syntax yet`)); continue; }
        const r = _renderParamValue(act[k], spec);
        if (r.error) { errors.push(_err(`event agent: actions[0].${k}: ${r.message}`)); continue; }
        extraEventKwParts.push(`${k}=${r.text}`);
      }
    }
  }

  const metaResult = decompileMetaClause({ name, description, tags, group });
  errors.push(...metaResult.errors);

  if (errors.length) return { text: null, errors };

  const renderR = _isBareable(renderName) ? { text: renderName } : _quoteString(renderName);
  if (renderR.error) return { text: null, errors: [_err(`render: ${renderR.message}`)] };

  let parts;
  if (matchEntryTexts) {
    // Sprint 18 follow-up — `match=[...]` replaces the whole
    // log_tag=/min_level=/where= triple.
    parts = [`match=[${matchEntryTexts.join(', ')}]`, `render=${renderR.text}`];
  } else {
    const logTagR = _isBareable(logTag) ? { text: logTag } : _quoteString(logTag);
    if (logTagR.error) return { text: null, errors: [_err(`log_tag: ${logTagR.message}`)] };
    parts = [`log_tag=${logTagR.text}`, `min_level=${minLevel}`, `render=${renderR.text}`];
    if (whereObj) {
      const whereR = _renderObjectLiteral(whereObj);
      if (whereR.error) return { text: null, errors: [_err(`where: ${whereR.message}`)] };
      parts.push(`where=${whereR.text}`);
    }
  }
  // Sprint 18 — render action's own extra keys (e.g. `gate=true`),
  // rendered above against `catalog.renderAction`'s schema.
  parts.push(...extraEventKwParts);

  const metaSuffix = metaResult.text ? ` ${metaResult.text}` : '';
  const text = `ALERT ${parts.join(', ')}, ${eventsResult.text}${metaSuffix}`;
  const roundTrip = compileAgentCliStatement(text, catalog, opts);
  if (!roundTrip.ok) {
    const detail = (roundTrip.errors && roundTrip.errors[0] && roundTrip.errors[0].message) || 'unknown error';
    return { text: null, errors: [_err(`internal: decompiled text failed to recompile (${detail})`)] };
  }
  return { text, errors: [] };
}

// ── Shared TP/SL/Wing decompiler (Sprint 8) — the decompile-direction
// mirror of `_compileTpSlWingFields`, reusing the same bare/quote
// rendering rules `_renderParamValue`'s "unknown/absent schema type"
// fallback branch already uses for every other generic action param. ───

/** Render one `tp_scales_json`/`where`/match-entry object-literal entry (a
 *  plain JS object, after `JSON.parse`/direct property read) back to
 *  `{key=value, ...}` CLI text. No fixed params_schema exists for an
 *  object literal's inner fields — always best-effort passthrough, same
 *  as `_renderParamValue`'s own fallback branch (reused directly, with
 *  `spec: null`) — EXCEPT a nested plain object value (Sprint 18
 *  follow-up, a match entry's own `where={...}`), which recurses into
 *  THIS function again (object_literal values can nest exactly one level
 *  deep in practice — the parser itself has no depth limit on this
 *  recursion, since `parseObjectLiteral`'s own LBRACE branch is
 *  general). */
function _renderObjectLiteral(obj) {
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    return { error: true, message: 'object literal entry must be an object' };
  }
  const parts = [];
  for (const [k, v] of Object.entries(obj)) {
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      const nested = _renderObjectLiteral(v);
      if (nested.error) return nested;
      parts.push(`${k}=${nested.text}`);
      continue;
    }
    const r = _renderParamValue(v, null);
    if (r.error) return r;
    parts.push(`${k}=${r.text}`);
  }
  return { text: `{${parts.join(', ')}}` };
}

/** Render a stored `tp_scales_json`/`tp_scales_json_override` STRING
 *  field (JSON-encoded array of objects — see the shared-vocabulary note
 *  on `_compileTpSlWingFields`) back to `[{...}, {...}]` CLI text. */
function _renderTpScalesJsonValue(rawStr) {
  let parsed;
  try { parsed = JSON.parse(rawStr); } catch { return { error: true, message: 'tp_scales_json is not valid JSON' }; }
  if (!Array.isArray(parsed)) return { error: true, message: 'tp_scales_json must decode to a JSON array of objects' };
  const parts = [];
  for (const item of parsed) {
    const r = _renderObjectLiteral(item);
    if (r.error) return r;
    parts.push(r.text);
  }
  return { text: `[${parts.join(', ')}]` };
}

/** Decompile the shared TP/SL/Wing fields off a stored leg/template
 *  object back to `key=value` CLI arg-text fragments — the ONE mapping
 *  function both `order(...)` and `bracket(...)` decompilers would call
 *  (mirrors `_compileTpSlWingFields`'s own destKind convention: `'ticket'`
 *  reads `${kwarg}_override` keys, `'template'` reads plain `${kwarg}`
 *  keys). Returns `{ argParts, errors }` — never throws. */
function _decompileTpSlWingFields(obj, destKind, label) {
  const errors = [];
  const argParts = [];
  for (const kwarg of _TPSLWING_KWARGS) {
    const srcKey = destKind === 'ticket' ? `${kwarg}_override` : kwarg;
    if (obj[srcKey] == null) continue;
    const value = obj[srcKey];

    if (kwarg === 'tp_scales_json') {
      if (typeof value !== 'string') { errors.push(_err(`${label}.${srcKey}: expected a JSON string`)); continue; }
      const r = _renderTpScalesJsonValue(value);
      if (r.error) { errors.push(_err(`${label}.${srcKey}: ${r.message}`)); continue; }
      argParts.push(`tp_scales_json=${r.text}`);
      continue;
    }
    if (kwarg === 'tp_order_type') {
      const r = _renderParamValue(value, _TP_ORDER_TYPE_SPEC);
      if (r.error) { errors.push(_err(`${label}.${srcKey}: ${r.message}`)); continue; }
      argParts.push(`tp_order_type=${r.text}`);
      continue;
    }
    const r = _renderParamValue(value, _TPSLWING_NUMBER_SPEC);
    if (r.error) { errors.push(_err(`${label}.${srcKey}: ${r.message}`)); continue; }
    argParts.push(`${kwarg}=${r.text}`);
  }
  return { argParts, errors };
}

/** The 4 real `OrderTemplate` fields (`backend/api/models.py`) that
 *  `bracket(...)` CLI syntax has NO way to express (Sprint 18 audit fix)
 *  — `compileBracketStmt` only ever recognizes `applies_to` + the shared
 *  TP/SL/Wing kwargs + the generic `#`-meta clause; any other kwarg is a
 *  hard "unknown parameter" compile error. Checking truthiness alone
 *  would be wrong — `is_active` is `true` and `is_default`/`is_system`
 *  are `false` on the overwhelming majority of real rows (every
 *  operator-created custom bracket), so this only flags a NON-DEFAULT
 *  value that this syntax would actually lose on resave:
 *    - `slug` (non-null) — system-seed-only; `OrderTemplateCreate` (the
 *      ONLY write path `/console`'s create-only submit can reach, see
 *      `consoleDispatch.js:submitConsoleBracket`) has no `slug` field at
 *      all, so there is structurally no way to round-trip it even if a
 *      `#slug=` meta key were invented.
 *    - `is_system: true` — `create_template` (`backend/api/routes/
 *      templates.py`) hardcodes `is_system=False` on every create; a
 *      decompiled-then-resaved system bracket can never come back as
 *      system, no matter what CLI syntax existed.
 *    - `is_default: true` — `OrderTemplateCreate.is_default` IS a real,
 *      honored create-time field, but `create_template`'s own
 *      `_demote_existing_default()` call means resubmitting this would
 *      ACTIVELY STEAL the default slot away from the original row in
 *      that `applies_to` scope — strictly worse than just losing
 *      information, so this is refused rather than "safely dropped".
 *    - `is_active: false` — dropping this silently resurrects an
 *      operator-deactivated row as active on resave.
 *  Refusing loud here (never silently emitting CLI text that would
 *  resave as a DIFFERENT row) is the same "fail loud, don't silently
 *  drop a field that would change behavior on resave" convention
 *  `decompileEventAgent`'s own docstring documents for its `gate` key.
 *  Returns the offending field names (empty = safe to decompile). */
function _bracketUnexpressibleFields(t) {
  const bad = [];
  if (t.slug != null) bad.push('slug');
  if (t.is_default === true) bad.push('is_default');
  if (t.is_active === false) bad.push('is_active');
  if (t.is_system === true) bad.push('is_system');
  return bad;
}

/** Decompile a stored `OrderTemplateCreate`-shaped bracket back to its
 *  `bracket(...) #name=...` CLI text — the reverse of `compileBracketStmt`
 *  (Sprint 8). Reuses `decompileMetaClause` UNCHANGED (same generic helper
 *  `decompileAgent` uses) plus the shared `_decompileTpSlWingFields`
 *  renderer above. Same recompile safety net `decompileAgent` already
 *  uses — builds the text, then actually recompiles it and fails loud if
 *  that recompile itself errors.
 *
 *  Refuses outright (Sprint 18) when the row carries `slug`/`is_default`/
 *  `is_active`/`is_system` in a way `bracket(...)` syntax can't express —
 *  see `_bracketUnexpressibleFields` above for the full reasoning. */
export function decompileBracket(template, catalog, opts = {}) {
  const t = template || {};
  const errors = [];
  const argParts = [];

  const unexpressible = _bracketUnexpressibleFields(t);
  if (unexpressible.length) {
    errors.push(_err(
      `bracket: cannot safely decompile — [${unexpressible.join(', ')}] ` +
      `${unexpressible.length === 1 ? 'has' : 'have'} no bracket(...) CLI ` +
      `syntax yet; resaving this text would silently change this row's ` +
      `${unexpressible.join('/')} instead of preserving it. This bracket ` +
      `can't be edited via the CLI yet — view/delete it from its row ` +
      `card on Automation → Agents → Brackets instead (Sprint C, 2026-10:` +
      ` the old standalone Brackets structured-edit form was retired).`
    ));
    return { text: null, errors };
  }

  if (t.applies_to != null) {
    if (typeof t.applies_to !== 'string') {
      errors.push(_err('bracket.applies_to: expected an enum value'));
    } else {
      argParts.push(`applies_to=${_isBareable(t.applies_to) ? t.applies_to : _quoteString(t.applies_to).text}`);
    }
  }

  const tpSlWingResult = _decompileTpSlWingFields(t, 'template', 'bracket');
  errors.push(...tpSlWingResult.errors);
  argParts.push(...tpSlWingResult.argParts);

  const metaResult = decompileMetaClause({ name: t.name, description: t.description, tags: t.tags, group: t.group });
  errors.push(...metaResult.errors);
  if (typeof t.name !== 'string' || t.name === '') {
    errors.push(_err("bracket: missing required 'name' — cannot decompile a nameless bracket"));
  }

  if (errors.length) return { text: null, errors };

  const metaSuffix = metaResult.text ? ` ${metaResult.text}` : '';
  const text = `bracket(${argParts.join(', ')})${metaSuffix}`;
  const roundTrip = compileAgentCliStatement(text, catalog, opts);
  if (!roundTrip.ok) {
    const detail = (roundTrip.errors && roundTrip.errors[0] && roundTrip.errors[0].message) || 'unknown error';
    return { text: null, errors: [_err(`internal: decompiled text failed to recompile (${detail})`)] };
  }
  return { text, errors: [] };
}

/** Decompile a stored `kind='template'` Agent row back to its CLI text
 *  (Sprint C, 2026-10, Phase 2 "Automation redesign"; condition-kind
 *  support added 2026-10-12, operator: "build it now"). The reverse of
 *  `compileTokenStmt`. Dispatches on which field is actually populated:
 *
 *  NOTIFY-kind — non-empty `events`, `conditions` empty/`{}` — decompiles
 *  to `token(kind=notify) <channels> #name=...`. Reuses `decompileEvents`
 *  UNCHANGED (a Token's notify body is the exact same `{channel,
 *  enabled}` list shape ALERT's own clause already uses).
 *
 *  CONDITION-kind — non-empty `conditions` — decompiles to
 *  `token(kind=condition) <expr> #name=...`. Reuses `decompileCondition`
 *  UNCHANGED — the SAME public entry point `decompileAgent`'s own WHEN
 *  clause uses — so any condition tree a cycle agent could decompile
 *  (metric refs, scopes, between-chains, `$ref`s, etc.) a condition-kind
 *  Template can too; no separate rendering logic duplicated here.
 *
 *  Both branches share `decompileMetaClause` — Tokens only ever carry
 *  `#name=`/`#desc=` (no `#tag=`/`#group=`, matching `compileTokenStmt`'s
 *  own explicit rejection of those two meta keys for a token statement). */
export function decompileToken({ conditions, events, name, description }, catalog, opts = {}) {
  const errors = [];
  const hasConditions = conditions && typeof conditions === 'object' && !Array.isArray(conditions) && Object.keys(conditions).length > 0;
  const hasEvents = Array.isArray(events) && events.length > 0;

  if (!hasConditions && !hasEvents) {
    errors.push(_err('token: this Token has an empty body — nothing to decompile'));
    return { text: null, errors };
  }

  let headAndBodyText;
  if (hasConditions) {
    const condResult = decompileCondition(conditions, catalog);
    errors.push(...condResult.errors);
    if (condResult.text == null) return { text: null, errors };
    headAndBodyText = `token(kind=condition) ${condResult.text}`;
  } else {
    const eventsResult = decompileEvents(events, catalog);
    errors.push(...eventsResult.errors);
    if (eventsResult.text == null) return { text: null, errors };
    headAndBodyText = `token(kind=notify) ${eventsResult.text}`;
  }

  const metaResult = decompileMetaClause({ name, description });
  errors.push(...metaResult.errors);
  if (typeof name !== 'string' || name === '') {
    errors.push(_err("token: missing required 'name' — cannot decompile a nameless Token"));
  }

  if (errors.length) return { text: null, errors };

  const metaSuffix = metaResult.text ? ` ${metaResult.text}` : '';
  const text = `${headAndBodyText}${metaSuffix}`;
  const roundTrip = compileAgentCliStatement(text, catalog, opts);
  if (!roundTrip.ok) {
    const detail = (roundTrip.errors && roundTrip.errors[0] && roundTrip.errors[0].message) || 'unknown error';
    return { text: null, errors: [_err(`internal: decompiled text failed to recompile (${detail})`)] };
  }
  return { text, errors: [] };
}

// ═══════════════════════════════════════════════════════════════════════
// (g) Live autocomplete (Sprint 4) — below-the-textarea suggestion row,
//     NOT caret-anchored (deliberate scope decision, see PLAN.md). Reuses
//     `lexAgentStatement` for the common (closed-syntax) case and
//     `fuzzyFilter` from engine.js for ranking; never throws — a bad/
//     mid-typing input degrades to an empty suggestion list.
//
//     Design note: `lexAgentStatement` throws internally (caught, surfaced
//     as `errors`) whenever a call's "(" has no matching ")" yet, or an
//     arg segment is empty (a bare trailing comma) — BOTH of which are the
//     NORMAL, expected state of the text while an operator is actively
//     typing a call's argument list (e.g. `order(` or `order(account=X,`).
//     So the primary mechanism here is a small, quote-aware "find the
//     innermost still-open call paren ending at/before the cursor" scan
//     (same spirit as the depth-guard already in `lexAgentStatement`,
//     just without requiring the close to exist) — this covers the
//     unclosed-call case AND the "cursor sits inside an already-closed
//     call" case AND the trailing-comma case uniformly, with no need to
//     special-case any of them. Only once we know we are NOT inside an
//     open call's parens do we fall back to `lexAgentStatement` on the
//     TEXT UP TO THE CURSOR (never the full text — a half-typed later
//     clause must never blank out suggestions for the clause the operator
//     is actually editing) for the bare metric/channel/action-name case.
// ═══════════════════════════════════════════════════════════════════════

/** Quote-aware scan for the innermost call whose "(" is open (no matching
 *  ")" yet) somewhere in `text.slice(0, cursor)`. Returns
 *  `{ nameStart, nameEnd, parenPos }` or `null` when the cursor isn't
 *  inside a call's argument list at all (e.g. a bare grouping "(" from
 *  `"(" or_expr ")"`, or no open paren at all). */
function _findOpenCallAt(text, cursor) {
  const prefix = text.slice(0, cursor);
  const stack = [];
  let inQuote = null;
  for (let i = 0; i < prefix.length; i++) {
    const ch = prefix[i];
    if (inQuote) { if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; continue; }
    if (ch === '(') stack.push(i);
    else if (ch === ')') stack.pop();
  }
  if (!stack.length) return null;
  const parenPos = stack[stack.length - 1];
  // A call paren is immediately preceded by a word char (no space allowed
  // between a call's name and its own "(" — see lexAgentStatement). A
  // bare grouping paren (boolean sub-expression) is not a call context.
  const before = parenPos > 0 ? prefix[parenPos - 1] : '';
  if (!_isWordChar(before)) return null;
  let nameStart = parenPos;
  while (nameStart > 0 && _isWordChar(prefix[nameStart - 1])) nameStart--;
  return { nameStart, nameEnd: parenPos, parenPos };
}

/** Public pass-through to `_findOpenCallAt` — exported for
 *  `AgentCliEditor.svelte`'s auto-advance logic (Sprint [CLI dropdown],
 *  2026-10). Disambiguates `kind:'token'` suggestions, which
 *  `suggestAgentCliAt` returns from BOTH a top-level bare-name position
 *  (metric/channel/action/label/debug-arg candidates — may warrant an
 *  auto-inserted `(`, see `lookupAgentCliEntry`) AND an open call's
 *  free-text value-fill position (a bound label filling an
 *  already-named param — warrants an auto-inserted `, ` instead, same
 *  as a real `kind:'enum'` value). Returns `true` iff `cursor` sits
 *  inside a call's still-open argument list in `text`. */
export function isInsideOpenCall(text, cursor) {
  return !!_findOpenCallAt(text, cursor);
}

/** Nearest non-whitespace char before `pos` in `text` — used to detect
 *  "immediately after @ or ." (scope-ref position — see `_suggestScopeRef`
 *  below, Sprint 5) and to bias catalog lookup toward `catalog.scopes` for
 *  a call sitting right after "@"/".". */
function _nearestNonSpaceBefore(text, pos) {
  let i = pos - 1;
  while (i >= 0 && /\s/.test(text[i])) i--;
  return i >= 0 ? text[i] : '';
}

/** Determine which POSITION `tokens` (a token stream from lexing some
 *  PREFIX of the statement) currently sits in — the last WHEN/DO
 *  reserved-word marker seen wins; neither seen at all means the
 *  implicit, self-delimiting PREAMBLE (Sprint [grammar-unify] — the
 *  default position, replacing the old GIVEN/bare-order_stmt split:
 *  there is now exactly ONE "nothing has happened yet" state, and it
 *  accepts the full shared statement vocabulary, same as DO — see
 *  `_bareNameCandidates`'s own `'preamble'` branch).
 *
 *  A FOURTH value, `'alert'`, exists in this same conceptual slot but is
 *  NEVER returned by this function — `alert(...)`'s own body now lives
 *  inside real parens (a nested call), not a bare top-level clause, so
 *  it's detected by position (the nearest unclosed paren's own call name
 *  — see `_suggestAgentCliAtImpl`'s/`_describeTokenAtImpl`'s own
 *  "inside an open alert(...) call" branches) and passed explicitly to
 *  `_lookupAnyCatalogEntry`/`_bareNameCandidates`/`_candidateTokensForClause`/
 *  `_descMapForClause`, which all still recognize it exactly as before —
 *  only the WAY it's produced changed, not what it means once you have
 *  it. */
function _clauseKindFromTokenPrefix(tokens) {
  let kind = null;
  for (const t of tokens) {
    if (t.type !== 'NAME' || !t.reserved) continue;
    const w = String(t.value).toLowerCase();
    if (w === 'when') kind = 'when';
    else if (w === 'do') kind = 'do';
  }
  return kind || 'preamble';
}

// Sprint H (item 4) — the SAME name-alias resolution `compileDoClause`
// (`rawName === 'order' ? 'place_order' : rawName`) and
// `_compileOrderCallToLeg` already apply when COMPILING a `DO order(...)`/
// bare `order(...)` call, mirrored here for the SUGGESTION path. Confirmed
// root cause of the Sprint 20 audit's "order(/bracket(/token( get ZERO
// parameter suggestions" finding: the compiler resolves this alias before
// its own catalog lookup, but `_lookupAnyCatalogEntry` below never did —
// so `catalog.actions.get('order')` always missed (only `'place_order'`
// is a real catalog key) and the whole call silently degraded to no
// suggestions. `bracket`/`token` have NO catalog entry at all (hand-coded
// compilers — `compileBracketStmt`/`compileTokenStmt` never go through
// `entry.paramKeys`/`params_schema` the way `order`/`place_order` does),
// so aliasing alone can't fix them — see `_syntheticEntryFor` below.
// Sprint [order-unify] — relabeled, NOT functionally inverted (the
// `_ACTION_NAME_ALIAS[key] || key` fallback at every call site already
// resolved a directly-typed `place_order` to itself before this change,
// via that `|| key` default — there was no functional gap to invert).
// `order` is the sole name the compiler ever EMITS going forward (see
// `compileDoClause`); `place_order` is the legacy spelling — still
// accepted as typed CLI input, and still the guaranteed-present real
// catalog row through Phase A — but never produced. Both map to the
// real catalog key this table (and every caller below) uses for schema
// lookup.
const _ACTION_NAME_ALIAS = Object.freeze({ order: 'place_order', place_order: 'place_order' });

// Sprint H (item 4) — synthetic, SUGGESTION-ONLY catalog-entry shapes for
// `bracket(...)`/`token(...)`, built from the exact same constants their
// REAL hand-coded compilers (`compileBracketStmt`/`compileTokenStmt`,
// `_compileTpSlWingFields`, `_splitTpSlWingArgs`) already use — so a
// change to the real accepted-field list there can never silently drift
// out of sync with what's suggested here (no second hand-maintained list).
// `order(...)`'s own real catalog entry (`place_order`) is ALSO missing
// three categories of sugar its compiler (`_compileOrderCallToLeg`)
// accepts — `lots`/`bracket` and the shared TP/SL/Wing kwargs — augmented
// the same way below (`_syntheticEntryFor('order', ...)`).
function _syntheticEntryFor(name, catalog) {
  const lower = String(name).toLowerCase();
  if (lower === 'bracket') {
    // Sprint [order-unify] — tolerant lookup (suggestion-only; see
    // `_compileOrderCallToLeg`'s identical comment).
    const placeOrderEntry = catalog && catalog.actions
      ? (catalog.actions.get('order') || catalog.actions.get('place_order'))
      : null;
    const schema = { applies_to: _BRACKET_APPLIES_TO_SPEC };
    for (const k of _TPSLWING_KWARGS) schema[k] = k === 'tp_order_type' ? _TP_ORDER_TYPE_SPEC : _TPSLWING_NUMBER_SPEC;
    return {
      token: 'bracket',
      paramsSchema: schema,
      paramKeys: Object.keys(schema),
      description: 'Saved, reusable TP/SL/Wing preset — requires #name=.',
      mutuallyExclusiveGroups: placeOrderEntry ? placeOrderEntry.mutuallyExclusiveGroups : null,
    };
  }
  if (lower === 'token') {
    // `kind=condition` parses but is a guaranteed compile error today
    // (compileTokenStmt) — restrict the SUGGESTED enum to the one value
    // that actually compiles, rather than suggesting a value that's
    // certain to fail once picked (per the advisor review for this
    // sprint). `kind=` itself is still a free-typed enum field as far as
    // the grammar/lexer is concerned — only the autocomplete candidate
    // list is narrowed.
    const schema = { kind: { type: 'enum', enum: ['notify'] } };
    return { token: 'token', paramsSchema: schema, paramKeys: Object.keys(schema), description: 'Saved, reusable notify channel-list preset — requires #name=.', mutuallyExclusiveGroups: null };
  }
  if (lower === 'order' || lower === 'place_order') {
    // Sprint [order-unify] — tolerant lookup (suggestion-only; see
    // `_compileOrderCallToLeg`'s identical comment).
    const base = catalog && catalog.actions
      ? (catalog.actions.get('order') || catalog.actions.get('place_order'))
      : null;
    if (!base) return null;
    const schema = { ...base.paramsSchema, lots: { type: 'number', description: 'Order quantity in LOTS for F&O (converted to qty via lot size) — mutually exclusive with qty=.' }, bracket: { type: 'string', description: 'Reference an existing saved bracket(...) preset by its #name=.' } };
    for (const k of _TPSLWING_KWARGS) if (!(k in schema)) schema[k] = k === 'tp_order_type' ? _TP_ORDER_TYPE_SPEC : _TPSLWING_NUMBER_SPEC;
    return { token: base.token, paramsSchema: schema, paramKeys: Object.keys(schema), description: base.description, mutuallyExclusiveGroups: base.mutuallyExclusiveGroups };
  }
  return null;
}

/** Look up a call's catalog entry by name, trying the catalog section(s)
 *  implied by `clauseKind`/`scopeHint` first (disambiguation only — the
 *  grammar's token namespaces don't collide in practice). Sprint H (item
 *  4) — tries the real catalog FIRST (unchanged lookup order/behavior for
 *  every already-working token), then falls back to the compiler's own
 *  name-alias table (`order`→`place_order`) and finally to a synthetic,
 *  suggestion-only entry (`bracket`/`token`, and an augmented `order`)
 *  when nothing in the live catalog matches by either name. */
function _lookupAnyCatalogEntry(name, catalog, clauseKind, scopeHint) {
  if (!catalog) return null;
  const key = String(name).toLowerCase();
  // `order`/`place_order`/`bracket`/`token` ALWAYS resolve to the
  // (possibly-augmented) synthetic shape FIRST — ahead of the plain
  // catalog loop below — so `place_order(` and `order(` suggest the
  // identical, fully-augmented param list (both compile through the exact
  // same `_compileOrderActionParams`/`_compileOrderCallToLeg` sugar), not
  // just whichever spelling happens to be typed.
  const aliased = _ACTION_NAME_ALIAS[key] || key;
  const synthetic = _syntheticEntryFor(aliased, catalog);
  if (synthetic) return synthetic;
  let order;
  if (scopeHint) order = ['scopes', 'metrics', 'channels', 'actions'];
  else if (clauseKind === 'alert') order = ['channels', 'metrics', 'scopes', 'actions'];
  else if (clauseKind === 'do' || clauseKind === 'preamble') order = ['actions', 'metrics', 'scopes', 'channels'];
  else order = ['metrics', 'scopes', 'channels', 'actions']; // 'when' / unknown
  for (const section of order) {
    const m = catalog[section];
    if (m && m.has(key)) return m.get(key);
  }
  return null;
}

/** Public pass-through to `_lookupAnyCatalogEntry` — exported for
 *  `AgentCliEditor.svelte`'s auto-advance logic (Sprint [CLI dropdown],
 *  2026-10), which needs to answer "does the token the operator just
 *  accepted from the dropdown take parameters" (to decide whether to
 *  auto-insert `(`) using the EXACT SAME resolution order/aliasing/
 *  synthetic-entry rules `suggestAgentCliAt` itself already applies —
 *  no second lookup table to drift out of sync. `clauseKind`/`scopeHint`
 *  are optional disambiguation hints (see `_lookupAnyCatalogEntry`'s own
 *  doc comment above) — omit both for a plain by-name lookup, safe
 *  because the grammar's token namespaces don't collide in practice. */
export function lookupAgentCliEntry(name, catalog, clauseKind = null, scopeHint = false) {
  return _lookupAnyCatalogEntry(name, catalog, clauseKind, scopeHint);
}

// Bare top-level statement tokens that map to a specific `allowedKinds`
// entry directly (checked by exact name, case-insensitive) — everything
// else that's a real `catalog.actions` entry, plus every clause-head
// keyword/connective, is only reachable inside a full WHEN/ALERT/DO
// agent statement, so it maps to 'agent'.
const _ACTION_TOKEN_KIND = { order: 'order', place_order: 'order', cancel_all_orders: 'cancel_all_orders', bracket: 'bracket' };
// Sprint [grammar-unify] — GIVEN and ALERT are removed: GIVEN has nothing
// left to mark (the preamble is self-delimiting by position), and ALERT
// is an ordinary call (`alert(...)`) now, never a clause-head keyword.
const _CLAUSE_HEAD_TOKENS = new Set(['WHEN', 'DO', '&', '|', 'always']);

/** Filters a bare-top-level suggestion list down to tokens the host
 *  editor's `allowedKinds` restriction (Sprint B's order-scoped CLI
 *  mounts) can actually reach — 2026-10-15 fix (operator: "does the
 *  dropdown for orders cli, non order related tokens functions. check
 *  it"): confirmed live the Orders CLI tab's bare-start dropdown
 *  suggested the FULL catalog.actions list (modify_order, monitor_order,
 *  close_position, chase_close_positions, expiry_auto_close, set_flag)
 *  plus GIVEN/WHEN/ALERT/DO, every one of which the compiler was
 *  ALREADY correctly rejecting at submit time via `allowedKinds` — the
 *  suggestions themselves just never reflected that restriction. `null`/
 *  undefined `allowedKinds` (the unrestricted Automation Console mount)
 *  passes every token through unchanged. Metric/channel/scope-root/
 *  bound-label names (everything NOT a clause-head keyword and NOT a
 *  real `catalog.actions` entry) are never touched here — those only
 *  ever appear in a DIFFERENT clauseKind branch (when/given/alert) that
 *  is itself only reachable after typing WHEN/GIVEN/ALERT, which this
 *  same filter already gates upstream. */
function _filterTokensByAllowedKinds(tokens, allowedKinds, catalog) {
  if (!allowedKinds) return tokens;
  return tokens.filter((t) => {
    if (_CLAUSE_HEAD_TOKENS.has(t)) return allowedKinds.includes('agent');
    const mapped = _ACTION_TOKEN_KIND[String(t).toLowerCase()];
    if (mapped) return allowedKinds.includes(mapped);
    if (catalog && catalog.actions && catalog.actions.has(String(t).toLowerCase())) return allowedKinds.includes('agent');
    return true;
  });
}

// Sprint [grammar-unify] — both helpers below are now UNREACHABLE fallbacks
// in practice: `_bareNameCandidates` handles every real `clauseKind` value
// ('when'/'alert'/'do'/'preamble') directly, with its own catalog-section
// choice, 'debug'/'nop' additions, and bound-label/scope-root extras — these
// two only ever run for a genuinely unrecognized clauseKind (which can't
// happen via `_clauseKindFromTokenPrefix`'s own `|| 'preamble'` fallback).
// Kept in sync anyway, in case a future caller passes a clauseKind straight
// through without going via `_bareNameCandidates`.
function _candidateTokensForClause(clauseKind, catalog) {
  if (!catalog) return null;
  if (clauseKind === 'when') return [...catalog.metrics.values()].map((e) => e.token);
  if (clauseKind === 'alert') return [...catalog.channels.values()].map((e) => e.token);
  if (clauseKind === 'do' || clauseKind === 'preamble') return [...catalog.actions.values()].map((e) => e.token);
  return null;
}

/** Token → catalog description lookup for whichever catalog section
 *  `clauseKind` draws its candidates from (same section `_candidateTokensForClause`
 *  reads) — used to build the `descriptions` array parallel to `suggestions`
 *  in the bare-name branch of `_suggestAgentCliAtImpl`. `null` when
 *  `clauseKind` doesn't map to a real section (mirrors `_candidateTokensForClause`). */
function _descMapForClause(clauseKind, catalog) {
  if (!catalog) return null;
  let section;
  if (clauseKind === 'when') section = catalog.metrics;
  else if (clauseKind === 'alert') section = catalog.channels;
  else if (clauseKind === 'do' || clauseKind === 'preamble') section = catalog.actions;
  else return null;
  const out = new Map();
  for (const e of section.values()) out.set(e.token, e.description || '');
  return out;
}

/** Walk backward through `real` (tokens lexed from the text UP TO THE
 *  CURSOR) to confirm the cursor sits inside an in-progress `scope_ref`
 *  (`call ("." call)*`) immediately preceded by a metric's "@" — exactly
 *  the position `metric_ref := call "@" scope_ref` describes. Returns
 *  `{ scopeRefStart: number }` — the text offset right after the "@",
 *  i.e. the start of the WHOLE scope_ref typed so far (not just the last
 *  segment) — or `null` when `real` doesn't actually end in a real
 *  scope_ref position. `null` is the normal, expected outcome for a '.'
 *  that isn't part of a scope_ref at all (e.g. an incomplete decimal
 *  literal like `3.` lexes as NUMBER(3), DOT — see the regression test
 *  for this exact shape); callers degrade to an empty suggestion list. */
function _scopeRefContextFromTokens(real, pos) {
  let idx = real.length - 1;
  if (idx >= 0 && real[idx].type === 'NAME' && !real[idx].hasCall && real[idx].end === pos) {
    idx--; // skip the still-being-typed last segment — only completed segments matter below
  }
  while (idx >= 0 && real[idx].type === 'DOT') {
    idx--;
    if (idx < 0 || real[idx].type !== 'NAME' || real[idx].hasCall) return null;
    idx--;
  }
  if (idx < 0 || real[idx].type !== 'AT') return null;
  return { scopeRefStart: real[idx].end };
}

/** Suggest scope-name completions for an in-progress `scope_ref` sitting
 *  right after "@" or "." (metric_ref's `@ scope_ref` position). Scope
 *  tokens are stored/typed as a single dotted string with no per-segment
 *  catalog entries (see `_resolveScopeString`/`_decompileResolveScopeString`
 *  elsewhere in this file), so the candidate list is simply every full
 *  `catalog.scopes` token (e.g. `"positions.expiring_today.mcx_unhedged"`),
 *  and `replaceRange` spans the WHOLE scope_ref typed so far — from right
 *  after "@" to the cursor, not just the last segment — so picking a
 *  suggestion replaces everything typed for the scope_ref in one shot.
 *  Fuzzy-ranked via the SAME `fuzzyFilter` every other branch of
 *  `suggestAgentCliAt` uses, against the raw typed-so-far text (dots
 *  included) — a real prefix match (e.g. "positions." against
 *  "positions.total") always ranks above a looser subsequence match. */
function _suggestScopeRef(real, text, pos, catalog) {
  if (!catalog) return _emptySuggest(pos);
  const ctx = _scopeRefContextFromTokens(real, pos);
  if (!ctx) return _emptySuggest(pos);
  const typedSoFar = text.slice(ctx.scopeRefStart, pos);
  const candidates = [...catalog.scopes.values()].map((e) => e.token);
  const descByToken = new Map([...catalog.scopes.values()].map((e) => [e.token, e.description || '']));
  const ranked = fuzzyFilter(typedSoFar, candidates);
  return {
    suggestions: ranked,
    descriptions: ranked.map((t) => descByToken.get(t) || ''),
    labels: ranked.map(() => null), // scope tokens have no distinct short label — pill shows the token itself
    replaceRange: [ctx.scopeRefStart, pos],
    kind: 'scope',
  };
}

/** Like `_scopeRefContextFromTokens` but for a BARE dotted chain with NO
 *  preceding "@" at all (Sprint 15's `col_ref` leaf form, e.g. typing
 *  `positions.` or `positions.total.` directly as a WHEN leaf — no
 *  `metric@` prefix). Walks backward the same way, but requires the
 *  token immediately before the whole chain to be a legal FRESH-NAME
 *  position (`_isFreshNamePosition` — the same check the bare-name
 *  branch below uses), not an "@". Returns `{ chainStart }` or `null`
 *  when `real` doesn't end in a real bare dotted chain (e.g. no "." at
 *  all, or the chain sits right after a comparator/value where a fresh
 *  leaf can't start). */
function _bareDottedChainContextFromTokens(real, pos) {
  let idx = real.length - 1;
  if (idx >= 0 && real[idx].type === 'NAME' && !real[idx].hasCall && real[idx].end === pos) {
    idx--; // skip the still-being-typed last segment
  }
  let sawDot = false;
  // Tracks the START offset of the EARLIEST (leftmost) segment seen so
  // far — walking backward means each successful match below is further
  // LEFT than the last, so the final value after the loop is exactly the
  // start of the whole chain's first segment. Deliberately NOT the end
  // of whatever token precedes the chain (unlike `_scopeRefContextFromTokens`,
  // which can rely on "@" sitting with no whitespace before the scope) —
  // there is ALWAYS a space between a clause keyword/connective and a
  // fresh leaf, so using the preceding token's `.end` here would wrongly
  // fold that leading whitespace into `typedSoFar`.
  let chainStart = null;
  while (idx >= 0 && real[idx].type === 'DOT') {
    sawDot = true;
    idx--;
    if (idx < 0 || real[idx].type !== 'NAME' || real[idx].hasCall) return null;
    chainStart = real[idx].start;
    idx--;
  }
  if (!sawDot) return null;
  const before = idx >= 0 ? real[idx] : undefined;
  if (!_isFreshNamePosition(before)) return null;
  return { chainStart };
}

/** Suggest completions for a bare `col_ref` chain typed with NO "@" at
 *  all (Sprint 15) — the sibling of `_suggestScopeRef`, for the NEW leaf
 *  form `positions.total.pnl <= -50000`. Deliberately uses EXACT (not
 *  fuzzy) matching on the scope part typed so far — a column/deeper-
 *  scope suggestion only ever appears once the operator has typed a
 *  COMPLETE, real scope name followed by ".", never a fuzzy guess that
 *  could show the SAME short label (e.g. "P&L (₹)") under several
 *  unrelated candidate scopes at once. Candidates are the union of (a)
 *  this scope's own exposed columns (`catalog.columns`) and (b) any
 *  DEEPER known scope that extends this exact prefix (e.g. typing
 *  `positions.expiring_today.` still offers `...nfo` / `...mcx_unhedged`
 *  as scope-chain continuations, same as the existing `@`-anchored
 *  scope_ref completion). `suggestions[i]` is always the FULL text to
 *  insert (replacing the WHOLE chain typed so far, same convention
 *  `_suggestScopeRef` uses) — a column candidate inserts its full
 *  compound token (e.g. `"positions.total.pnl"`), which the compiler
 *  resolves exactly like hand-typing it; `labels[i]` carries the SHORT
 *  operator-facing text (Sprint 15's `label` field) for a column
 *  candidate, so the pill can show "P&L (₹)" instead of the long
 *  compound token, without changing what text actually gets inserted. */
function _suggestBareDottedRef(real, text, pos, catalog) {
  if (!catalog) return _emptySuggest(pos);
  const ctx = _bareDottedChainContextFromTokens(real, pos);
  if (!ctx) return _emptySuggest(pos);
  const typedSoFar = text.slice(ctx.chainStart, pos);
  const lastDot = typedSoFar.lastIndexOf('.');
  if (lastDot < 0) return _emptySuggest(pos); // defensive — ctx guarantees at least one dot

  // Sprint H (items 6/11) — resolve the chain's FIRST segment through the
  // SAME short-family-alias table (`hld`/`hold`/`pos`/`fnd`/`wl`) the
  // compiler's own `_resolveDotAccess`/`_resolveScopeRefFull` already
  // apply, so typing `hold.` suggests exactly what `holdings.` would —
  // this was the root cause of `hold.`/`pos.` returning nothing (the
  // EXACT-match guard below compared the typed alias directly against
  // catalog.scopes' CANONICAL keys, which never contain the short form).
  // Only segment 0 is resolved — deeper segments have no abbreviated form.
  const rawScopeSegs = typedSoFar.slice(0, lastDot).split('.');
  rawScopeSegs[0] = _scopeFamilyAliasOf(rawScopeSegs[0]);
  const scopePart = rawScopeSegs.join('.');
  const scopePartLower = scopePart.toLowerCase();
  const partial = typedSoFar.slice(lastDot + 1);
  const prefix = `${scopePartLower}.`;

  const items = [];
  if (catalog.columns) {
    for (const e of catalog.columns.values()) {
      const tokenLower = e.token.toLowerCase();
      // A column's OWNING scope must be EXACTLY `scopePart` — `startsWith`
      // alone is too loose: e.g. "positions.expiring_today.mcx_unhedged.pnl"
      // also starts with "positions.expiring_today." but its real owning
      // scope is the DEEPER "positions.expiring_today.mcx_unhedged", not
      // "positions.expiring_today". Compare the column's own scope part
      // (everything before its LAST dot) for exact equality instead.
      const lastDotInToken = tokenLower.lastIndexOf('.');
      if (lastDotInToken < 0 || tokenLower.slice(0, lastDotInToken) !== scopePartLower) continue;
      const suffix = e.token.slice(scopePart.length + 1);
      items.push({ insert: e.token, matchText: suffix, label: e.label || null, description: e.description || '' });
    }
  }
  for (const e of catalog.scopes.values()) {
    const tokenLower = e.token.toLowerCase();
    if (tokenLower === scopePartLower || !tokenLower.startsWith(prefix)) continue;
    const suffix = e.token.slice(scopePart.length + 1);
    items.push({ insert: e.token, matchText: suffix, label: null, description: e.description || '' });
  }
  // `familyKnown` — true once `scopePart`'s (resolved) FIRST segment is a
  // real scope family actually present in the live catalog — gates BOTH
  // additions below so a genuinely unknown prefix (e.g. "zzz.") still
  // offers nothing, same as before this sprint.
  const firstSeg = scopePart.split('.')[0];
  const familyKnown = catalog.scopes.has(scopePartLower)
    || [...catalog.scopes.keys()].some((t) => t.split('.')[0] === firstSeg);
  if (familyKnown) {
    // Sprint H (item 11) — `funds.cash`/`funds.margin` are compiler-level
    // column-ALIAS shortcuts (`_FUNDS_COLUMN_ALIASES`) with no catalog
    // `columns` row of their own at all — `_resolveDotAccess` special-
    // cases them before the generic column/metric lookup (see that
    // function's own comment) — injected here so they're autocompletable
    // like any other exposed column instead of being invisible.
    if (scopePart === 'funds') {
      for (const key of Object.keys(_FUNDS_COLUMN_ALIASES)) {
        items.push({ insert: `funds.${key}`, matchText: key, label: null, description: '' });
      }
    }
    // Sprint H (item 10) — scope qualifier keys (`account:`/`symbol:`/
    // `exchange:`/`kind:`/`underlying:`, Sprint 19/21/G), offered as a
    // NEXT-segment candidate the same way a deeper scope/column name
    // already is. Whether a SPECIFIC qualifier is actually valid for THIS
    // scope (an exposed-column check) is left to compile time
    // (`_resolveScopeRefFull`), matching this file's existing "parse
    // permissively, validate at compile" convention everywhere else.
    for (const qualKey of Object.keys(_QUALIFIER_COLUMN_NAME)) {
      items.push({ insert: `${scopePart}.${qualKey}:`, matchText: `${qualKey}:`, label: null, description: '' });
    }
  }
  // EXACT scope matching — only offer anything once the typed prefix is
  // itself a real, complete scope token (or already has real deeper
  // candidates under it); never fuzzy-guess across unrelated scopes.
  if (!catalog.scopes.has(scopePartLower) && items.length === 0) return _emptySuggest(pos);

  const byMatch = new Map(items.map((it) => [it.matchText, it]));
  const ranked = fuzzyFilter(partial, items.map((it) => it.matchText));
  return {
    suggestions: ranked.map((mt) => byMatch.get(mt)?.insert ?? mt),
    descriptions: ranked.map((mt) => byMatch.get(mt)?.description ?? ''),
    labels: ranked.map((mt) => byMatch.get(mt)?.label ?? null),
    replaceRange: [ctx.chainStart, pos],
    kind: 'column',
  };
}

/** Quote-aware split of a call's inner-text-so-far on top-level commas,
 *  keeping each segment's own start offset (relative to the inner text)
 *  so callers can compute absolute `replaceRange` positions. No nesting
 *  is possible inside a call's arg list (same documented limitation
 *  `_parseArgListFromInnerText` already carries). */
function _splitTopLevelCommasWithOffsets(str) {
  const segs = [];
  let segStart = 0;
  let inQuote = null;
  for (let i = 0; i < str.length; i++) {
    const ch = str[i];
    if (inQuote) { if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; continue; }
    if (ch === ',') { segs.push({ text: str.slice(segStart, i), start: segStart }); segStart = i + 1; }
  }
  segs.push({ text: str.slice(segStart), start: segStart });
  return segs;
}

/** First top-level (unquoted) "=" offset in `str`, or -1. */
function _findTopLevelEquals(str) {
  let inQuote = null;
  for (let i = 0; i < str.length; i++) {
    const ch = str[i];
    if (inQuote) { if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; continue; }
    if (ch === '=') return i;
  }
  return -1;
}

function _emptySuggest(cursor) {
  return { suggestions: [], descriptions: [], labels: [], replaceRange: [cursor, cursor], kind: null, hint: null };
}

/** Sprint 15 (doc-rendering half) — generate a plain-English line for a
 *  param's `required_if` (Sprint 17) and/or token-level
 *  `mutuallyExclusiveGroups` (Sprint 17) membership, to append to that
 *  param's own `description`. Flows through the SAME hover/focus
 *  mechanism `AgentCliEditor.svelte` already renders `descriptions`
 *  through (Sprint 11) — no new UI mechanism needed, just more text in
 *  the string that mechanism already displays. ALWAYS generated from the
 *  structured `required_if.field`/`required_if.in`/group-membership
 *  data, never hand-written per field, so it can never drift from the
 *  actual enforced rule. `''` when `spec` carries neither. */
function _paramConstraintText(key, spec, mutexGroups) {
  const lines = [];
  const reqIf = spec && spec.required_if;
  if (reqIf && reqIf.field && Array.isArray(reqIf.in) && reqIf.in.length) {
    lines.push(`${key} — required when ${reqIf.field} is ${reqIf.in.join('/')}`);
  }
  if (Array.isArray(mutexGroups)) {
    const lk = String(key).toLowerCase();
    for (const group of mutexGroups) {
      if (!Array.isArray(group) || !group.some((g) => String(g).toLowerCase() === lk)) continue;
      const others = group.filter((g) => String(g).toLowerCase() !== lk);
      if (others.length) lines.push(`${key} is mutually exclusive with ${others.join(', ')}`);
    }
  }
  return lines.join('\n');
}

/** Start offset of the call-name NAME immediately before its own "(" at
 *  `parenPos` — the one point BEFORE this (possibly still-open, unbalanced)
 *  call where `lexAgentStatement` is still guaranteed to succeed on
 *  `text.slice(0, callNameStart)` (everything before it is, by
 *  construction, already-complete/balanced). Used by both
 *  `_paramOrEnumSuggestion` and `_suggestDebugArg` to bound
 *  `_collectBindingsUpToCursor` — calling it with the raw `cursor`/
 *  `parenPos` instead would make `lexAgentStatement` try to re-lex the
 *  CURRENT unterminated call and throw "unbalanced parentheses", losing
 *  every earlier GIVEN/label binding in the process. */
function _callNameStartBefore(text, parenPos) {
  let nameStart = parenPos;
  while (nameStart > 0 && _isWordChar(text[nameStart - 1])) nameStart--;
  return nameStart;
}

/** Suggest param-name or enum-value completions for the call whose "("
 *  sits at `parenPos`, given everything typed so far up to `cursor`. */
function _paramOrEnumSuggestion(text, parenPos, cursor, entry, opts = {}) {
  const innerSoFar = text.slice(parenPos + 1, cursor);
  const segments = _splitTopLevelCommasWithOffsets(innerSoFar);
  const usedKeys = new Set();
  for (let i = 0; i < segments.length - 1; i++) {
    const eq = _findTopLevelEquals(segments[i].text);
    if (eq < 0) continue;
    const k = segments[i].text.slice(0, eq).trim().toLowerCase();
    usedKeys.add(_ALIAS_TO_REAL[k] || k);
  }
  const last = segments[segments.length - 1];
  const lastAbsStart = parenPos + 1 + last.start;
  const eqIdx = _findTopLevelEquals(last.text);

  if (eqIdx >= 0) {
    // Right after (or mid-typing) a value for an already-named param.
    const rawKey = last.text.slice(0, eqIdx).trim().toLowerCase();
    const realKey = _ALIAS_TO_REAL[rawKey] || rawKey;
    const spec = entry.paramsSchema && entry.paramsSchema[realKey];
    const valueAbsStart = lastAbsStart + eqIdx + 1;
    const valuePartialRaw = text.slice(valueAbsStart, cursor);
    const valuePartial = valuePartialRaw.replace(/^['"]/, '');
    // `account` is declared `type: string` in the grammar YAML (its real
    // values are the operator's own broker accounts — runtime data, not
    // something a static grammar schema can enumerate) — 2026-10-15,
    // operator: "when account= is shown since the accounts are a list of
    // possible values, it should show the dropdown of the accounts."
    // `opts.accountIds` (threaded from the UI layer, which DOES have live
    // account data via `getAccountsSync()`) stands in for a real schema
    // enum here, same shape/behavior as one once candidates exist.
    // `symbol` is the same "runtime data, not a static enum" case as
    // `account` above, but the candidate universe is ~90k tradingsymbols
    // (not a few operator accounts) — too large to eagerly thread as an
    // array on every suggestion recompute. `opts.suggestSymbols(prefix)`
    // is a lazy resolver instead (backed by instruments.js's sorted
    // prefix-scan index), called ONLY once 3+ characters are typed
    // (2026-10-15, operator: "when 3 keys are typed, it should show the
    // symbols starting with that characters") — below that, no symbol
    // candidates are suggested at all (not a smaller/noisier list).
    const _symbolPrefix = valuePartial.trim();
    const enumValues = (spec && spec.type === 'enum' && Array.isArray(spec.enum)) ? spec.enum
      : (realKey === 'account' && Array.isArray(opts.accountIds) && opts.accountIds.length) ? opts.accountIds
      : (realKey === 'symbol' && typeof opts.suggestSymbols === 'function' && _symbolPrefix.length >= 3) ? opts.suggestSymbols(_symbolPrefix)
      : [];
    // Sprint H (item 8) — a bound label (GIVEN, or a label assignment
    // inside ALERT/DO's own item list) is a valid value ANYWHERE an
    // ordinary call argument's value goes — `_substituteBoundArgs`
    // splices a PRIMITIVE-kind label's bound value straight in at
    // compile time, uniformly, regardless of whether that param happens
    // to be enum-typed or not. Only PRIMITIVE-kind labels belong here — a
    // CONDITION-kind label used as an ordinary param value is a compile-
    // time error (`_substituteBoundArgs`'s own rejection), so suggesting
    // one here would hand the operator a guaranteed-to-fail completion.
    const bindings = _collectBindingsUpToCursor(text, _callNameStartBefore(text, parenPos));
    const primitiveLabels = [...bindings.values()].filter((b) => b.rhsKind === 'value').map((b) => b.label);
    // Sprint [GIVEN-expr] — an `'expr'`-kind (operator-expression) label
    // is ALSO a valid value here, but ONLY for a `string`-typed param
    // (the same compile-time restriction `_resolveGivenExprRefOrNull`
    // enforces) — suggesting it for a numeric/enum param would hand the
    // operator a guaranteed-to-fail completion, same rationale as the
    // existing CONDITION-kind exclusion above.
    const exprLabels = spec && spec.type === 'string'
      ? [...bindings.values()].filter((b) => b.rhsKind === 'expr').map((b) => b.label)
      : [];
    const candidates = [...enumValues, ...primitiveLabels, ...exprLabels];
    if (!candidates.length) return _emptySuggest(cursor);
    const ranked = fuzzyFilter(valuePartial.trim(), candidates);
    // No per-VALUE description exists anywhere in the backend schema
    // (agent_grammar.yaml / order_fields.yaml enum lists carry no
    // per-entry text, only the param itself does) — degrade to empty
    // strings rather than inventing text. A bound label gets a short,
    // generated description instead of an empty one (there's no catalog
    // text to surface, but "why did this show up" is worth one line).
    const descriptions = ranked.map((v) => {
      if (primitiveLabels.includes(v)) return 'Label bound earlier in this statement.';
      if (exprLabels.includes(v)) return 'Computed label (GIVEN) bound earlier in this statement.';
      return '';
    });
    const kind = enumValues.length ? 'enum' : 'token';
    return { suggestions: ranked, descriptions, labels: ranked.map(() => null), replaceRange: [valueAbsStart, cursor], kind };
  }

  // No "=" yet in the segment being typed — suggest/complete a param NAME.
  const raw = last.text;
  const leadWs = raw.length - raw.replace(/^\s+/, '').length;
  const partialKey = raw.trim();
  const available = (entry.paramKeys || []).filter((k) => !usedKeys.has(k.toLowerCase()));
  const ranked = fuzzyFilter(partialKey, available);
  const replaceStart = lastAbsStart + leadWs;
  const descriptions = ranked.map((k) => {
    const spec = entry.paramsSchema && entry.paramsSchema[k];
    const base = (spec && spec.description) || '';
    // Sprint 17 docs-rendering gap closed here (Sprint 15's catalog-
    // documentation ask): append a GENERATED plain-English line for
    // `required_if` and/or `mutuallyExclusiveGroups` membership, never
    // hand-written per field.
    const extra = _paramConstraintText(k, spec, entry.mutuallyExclusiveGroups);
    return extra ? (base ? `${base}\n${extra}` : extra) : base;
  });
  return { suggestions: ranked, descriptions, labels: ranked.map(() => null), replaceRange: [replaceStart, cursor], kind: 'param' };
}

// ═══════════════════════════════════════════════════════════════════════
// Sprint H — autocomplete/dropdown completeness follow-up (2026-10). Fixes
// a Sprint 20 audit's six confirmed-never-fixed gaps (statement-head
// keywords; `in`/`not in`; nothing after a complete leaf/comparator;
// `order(`/`bracket(`/`token(` zero param suggestions; `#` meta keys; WHEN
// not suggesting dot-access scope roots) and adds coverage for Sprint G's
// GIVEN/`=`/labels/`debug(...)`/`kind:`/`underlying:`/`hold`/
// `funds.cash`/`funds.margin`. See `.claude/PLAN.md`'s "Sprint H" section
// for the full brief this implements.
// ═══════════════════════════════════════════════════════════════════════

/** Every word a bare scope-chain's FIRST segment could be typed as — the
 *  canonical family names actually present in the live catalog (derived,
 *  never hand-listed, so a future new family needs no edit here) PLUS
 *  every short alias for one of those families (Sprint H items 6/11 —
 *  `pos`/`hld`/`hold`/`fnd`/`wl`). Used to offer `positions`/`holdings`/
 *  `hold`/etc. as bare-name candidates at a WHEN/GIVEN leaf-start position
 *  (the newer Sprint 15 dot-access leaf form, `positions.total.pnl <= 0`,
 *  has no `metric@scope` prefix to anchor off of, so it needs its OWN
 *  entry point into autocomplete, distinct from `_suggestScopeRef`'s
 *  "@"-anchored one). */
function _allScopeFamilyRootNames(catalog) {
  if (!catalog || !catalog.scopes) return [];
  const canonical = new Set();
  for (const token of catalog.scopes.keys()) canonical.add(token.split('.')[0]);
  const names = new Set(canonical);
  for (const [real, alias] of Object.entries(SCOPE_FAMILY_ALIASES)) if (canonical.has(real)) names.add(alias);
  for (const [alias, real] of Object.entries(_EXTRA_SCOPE_FAMILY_ALIASES)) if (canonical.has(real)) names.add(alias);
  return [...names];
}

// Sprint H (items 2/3) — the full comparator vocabulary, including the
// two WORD-shaped comparators (`in`/`not in`) the Sprint 20 audit found
// missing entirely from autocomplete. Punctuation comparators are offered
// only as complete, zero-partial suggestions (typing `<` character-by-
// character has nothing to "complete" — there's no shorter prefix); the
// WORD ones also participate in the glued-partial-typing branch below via
// `_COMPARATOR_WORDS`.
const _COMPARATOR_SUGGESTIONS = Object.freeze(['<', '<=', '>', '>=', '==', '!=', 'in', 'not in']);
const _COMPARATOR_WORDS = Object.freeze(['in', 'not in']);

/** True iff `effectiveReal` (tokens lexed up to, but NOT including, any
 *  still-being-typed partial) ends in a COMPLETE `metric_ref`
 *  (`call "@" scope_ref`) or bare `col_ref` (`call ("." call)*`, Sprint
 *  15, no "@" at all) chain, with no comparator consumed yet — exactly
 *  the position `leaf := metric_ref comparator value | col_ref comparator
 *  value` expects one next (Sprint 20 item 2 / Sprint H item 3). Walks
 *  backward the same way `_scopeRefContextFromTokens`/
 *  `_bareDottedChainContextFromTokens` already do, but for the COMPLETE
 *  case (nothing left pending) rather than the mid-typing case those two
 *  handle. */
function _isAfterCompleteScopeChain(effectiveReal) {
  let idx = effectiveReal.length - 1;
  if (idx < 0 || effectiveReal[idx].type !== 'NAME') return false;
  idx--;
  let sawDot = false;
  while (idx >= 0 && effectiveReal[idx].type === 'DOT') {
    sawDot = true;
    idx--;
    if (idx < 0 || effectiveReal[idx].type !== 'NAME') return false;
    idx--;
  }
  if (idx >= 0 && effectiveReal[idx].type === 'AT') return true; // metric_ref, complete
  return sawDot; // bare col_ref chain, complete (no "@" required)
}

/** True iff `tok` is a comparator token that's already COMPLETE on its
 *  own (so the position right after it expects a VALUE next) — the
 *  punctuation comparators, or the word `in` (both halves of `not in` are
 *  individually reserved words, but `not` alone is ambiguous with term-
 *  level negation and is never reached here — see the call site's own
 *  comment for why). */
function _isComparatorToken(tok) {
  if (!tok) return false;
  if (tok.type === 'LT' || tok.type === 'LE' || tok.type === 'GT' || tok.type === 'GE' || tok.type === 'EQEQ' || tok.type === 'NE') return true;
  return tok.type === 'NAME' && !tok.hasCall && _wordIs(tok, 'in');
}

/** Which of GIVEN/WHEN/ALERT/DO have already appeared as a real reserved-
 *  word marker anywhere in `real` — used to compute which clause-head
 *  keyword(s) legally remain (Sprint H item 1).
 *
 *  KNOWN GAP (Sprint [grammar-unify], not yet closed — see CLAUDE.md/the
 *  handback report for this sprint): `given`/`alert` are no longer
 *  reserved words at the LEXER level (see `_RESERVED`'s own comment), so
 *  `t.reserved` is never true for either any more and this function can
 *  no longer detect them this way. Left UNCHANGED (rather than patched
 *  here in isolation) because an isolated fix made this file's own test
 *  suite WORSE, not better — `_clauseKindAt`-style position
 *  classification elsewhere in this suggestion engine still assumes a
 *  real GIVEN/ALERT clause exists (bare-name "channel position"
 *  classification keyed off a seen ALERT marker, GIVEN-item-head-is-
 *  debug-only restriction, etc.) and needs the SAME rework in lockstep,
 *  not a partial one. This is real, intentional, documented tech debt,
 *  not an oversight: the suggestion/autocomplete engine still assumes
 *  the pre-[grammar-unify] 4-clause shape and needs its own dedicated
 *  pass. */
/** Which of WHEN/DO have already appeared as a real reserved-word marker
 *  anywhere in `real` — used to compute which clause-head keyword(s)
 *  legally remain (Sprint H item 1; narrowed by Sprint [grammar-unify] —
 *  GIVEN/ALERT are no longer clause-head keywords at all: GIVEN has
 *  nothing left to mark (the preamble is self-delimiting by position,
 *  accepting the full statement vocabulary with no introducer), and
 *  `alert(...)` is an ordinary call, detected by POSITION (the nearest
 *  unclosed paren's own name — see `_suggestAgentCliAtImpl`'s "inside an
 *  open alert(...) call" branch), never by a reserved-word marker here). */
function _seenClauseHeads(real) {
  const seen = new Set();
  for (const t of real) {
    if (t.type !== 'NAME' || !t.reserved) continue;
    const w = String(t.value).toLowerCase();
    if (w === 'when' || w === 'do') seen.add(w);
  }
  return seen;
}

/** Which clause-head keywords are still legal to type next, given `seen`
 *  (see `_seenClauseHeads`) — mirrors `agent_stmt`'s own formal grammar
 *  (`preamble [WHEN condition DO calls]`) directly. `WHEN` is offered
 *  until either keyword has been seen (it's the ONLY entry point into
 *  the conditional wrapper, and once `DO` exists `WHEN` can never be
 *  added after it — the pair is already sealed either way); `DO` is
 *  offered ONLY once `WHEN` has been seen and `DO` hasn't yet — `DO`
 *  with no preceding `WHEN` is now a compile-time error
 *  (`compileAgentStmt`'s pairing rule), so autocomplete never steers an
 *  operator into typing it bare. */
function _clauseHeadKeywords(seen) {
  const out = [];
  if (!seen.has('when') && !seen.has('do')) out.push('WHEN');
  if (seen.has('when') && !seen.has('do')) out.push('DO');
  return out;
}

/** Sprint H (items 1/3) — the clause-head-keyword / boolean-connective
 *  candidates legal at the boundary ending at `boundaryPos` (the start of
 *  whatever's about to be freshly typed — a pure gap, or the start of an
 *  in-progress partial word). Empty `effectiveReal` (absolute statement
 *  start) offers `WHEN` directly, with no parse needed (an empty prefix
 *  doesn't parse as anything, but `WHEN` is always legal to START a
 *  statement with — the preamble ahead of it is optional). Otherwise,
 *  real parses `text` up to `boundaryPos` through the EXACT same
 *  dispatcher the real compiler uses (`_tryParsePrefixStatement`) — only
 *  when that prefix is ALREADY a complete, valid `agent_stmt` on its own
 *  (zero errors) is this a real "a clause/preamble item just finished"
 *  boundary; `&`/`|` are offered ADDITIONALLY only while still inside an
 *  in-progress WHEN condition (seen WHEN, not yet DO — extending the
 *  condition is still legal; once DO has started, the condition is
 *  sealed). */
function _clauseBoundaryKeywordCandidates(text, boundaryPos, effectiveReal) {
  if (effectiveReal.length === 0) return ['WHEN'];
  const parsed = _tryParsePrefixStatement(text, boundaryPos);
  if (!parsed || parsed.errors.length || !parsed.ast || parsed.ast.kind !== 'agent') return [];
  const seen = _seenClauseHeads(effectiveReal);
  const out = _clauseHeadKeywords(seen);
  if (seen.has('when') && !seen.has('do')) out.push('&', '|');
  return out;
}

/** True iff `effectiveReal` ends in a META clause's own `"#" NAME "="`
 *  (e.g. `#name=`), NOT an ordinary GIVEN/ALERT/DO label assignment — both
 *  lex to the identical `NAME EQ` tail (the meta lexer branch emits a
 *  plain `EQ` token, same type, for `#key=`'s own "=" — see
 *  `lexAgentStatement`'s "#" branch), so the ONLY way to tell them apart
 *  is to check two tokens further back for the `HASH` that only a meta
 *  item ever has. Guards the `given_rhs` suggestion branch below so a
 *  `#name=` meta value position doesn't get offered metric/scope/label
 *  candidates that make no sense there. */
function _isMetaKeyEq(effectiveReal) {
  const n = effectiveReal.length;
  if (n < 3) return false;
  if (effectiveReal[n - 1].type !== 'EQ' || effectiveReal[n - 2].type !== 'NAME') return false;
  return effectiveReal[n - 3].type === 'HASH';
}

/** Sprint H (items 6/8) — candidates for a `given_rhs := value | or_expr`
 *  start position, i.e. right after any label-bind's own "=" — GIVEN's
 *  `label = ...`, or an ORDINARY label assignment inside ALERT/DO's own
 *  item list (`parseOneClauseItem`'s `assign` production is identical in
 *  all three clauses). NOT the enclosing clause's own channel/action
 *  candidates — a metric/scope-root/label reference is what's
 *  syntactically valid here, regardless of whether the assignment happens
 *  to sit inside GIVEN, ALERT, or DO. Reads `state.bindings` via
 *  `_collectBindingsUpToCursor` — the SAME mechanism the real compiler
 *  uses — rather than a separate tracking scheme, so a label suggested
 *  here is guaranteed resolvable. */
function _givenRhsCandidates(catalog, text, boundaryPos) {
  const metricEntries = [...catalog.metrics.values()];
  const tokens = metricEntries.map((e) => e.token);
  const descByToken = new Map(metricEntries.map((e) => [e.token, e.description || '']));
  for (const root of _allScopeFamilyRootNames(catalog)) {
    if (!descByToken.has(root)) { tokens.push(root); descByToken.set(root, ''); }
  }
  const bindings = _collectBindingsUpToCursor(text, boundaryPos);
  for (const b of bindings.values()) {
    tokens.push(b.label);
    descByToken.set(b.label, `Label bound earlier in this statement (${b.rhsKind}).`);
  }
  return { tokens, descByToken };
}

/** Sprint H (items 1/6/8/9), reworked for Sprint [grammar-unify] — the
 *  full bare-name candidate set for a genuinely fresh metric/channel/
 *  action slot, keyed by `clauseKind` (`'preamble'` | `'when'` | `'do'` |
 *  `'alert'` — see `_clauseKindFromTokenPrefix`'s own doc for what each
 *  means and how `'alert'` is reached) and `contextTok` (the token
 *  immediately preceding the fresh slot — used to detect clause-specific
 *  "first item of this clause" positions that unlock `nop`/`always`).
 *  Replaces the old direct `_candidateTokensForClause`/`_descMapForClause`
 *  pair at every bare-name call site so every addition below lives in
 *  ONE place. */
function _bareNameCandidates(clauseKind, catalog, contextTok, text, boundaryPos) {
  const lastWord = (contextTok && contextTok.type === 'NAME' && contextTok.reserved)
    ? String(contextTok.value).toLowerCase() : null;

  if (clauseKind === 'when') {
    const base = [...catalog.metrics.values()];
    const tokens = base.map((e) => e.token);
    const descByToken = new Map(base.map((e) => [e.token, e.description || '']));
    // Sprint H item 6 — the Sprint 15 dot-access leaf form has no
    // "metric@scope" prefix to anchor autocomplete off of, so a bare
    // scope-family root (and its short aliases, item 11) is offered as
    // its OWN candidate here, alongside ordinary metric names.
    for (const root of _allScopeFamilyRootNames(catalog)) {
      if (!descByToken.has(root)) { tokens.push(root); descByToken.set(root, ''); }
    }
    // Sprint H item 8 — a CONDITION-kind bound label may stand in for a
    // WHOLE bare WHEN leaf (`parseLeaf`'s bare-ref branch); a PRIMITIVE-
    // kind one may not (parse-time error) — only condition-kind labels
    // belong in this specific candidate set.
    const bindings = _collectBindingsUpToCursor(text, boundaryPos);
    for (const b of bindings.values()) {
      if (b.rhsKind !== 'condition') continue;
      tokens.push(b.label);
      descByToken.set(b.label, 'Label bound earlier in this statement (condition).');
    }
    // "always" is standalone-only (never combined with &/|/~) and is only
    // a legal completion directly after the WHEN keyword itself — never
    // after "and"/"or"/"not"/"(" (those predecessors also pass
    // `_isFreshNamePosition`, but "always" there would be a guaranteed
    // parse error per the grammar's own rule).
    // 2026-10-15 fix (operator: "after when in console it is not always
    // in the dropdown") — `always` is almost certainly the operator's
    // most common pick directly after WHEN, but `push` put it dead last
    // among ~59 metric/scope-root candidates (confirmed live: index 58
    // of 59, far below the dropdown's own scrollable viewport). `unshift`
    // surfaces it first instead, without needing to scroll at all.
    if (lastWord === 'when') { tokens.unshift('always'); descByToken.set('always', 'Sentinel condition — always true (schedule-only agents).'); }
    return { tokens, descByToken };
  }

  if (clauseKind === 'alert' || clauseKind === 'do') {
    const section = clauseKind === 'alert' ? catalog.channels : catalog.actions;
    const base = [...section.values()];
    const tokens = base.map((e) => e.token);
    const descByToken = new Map(base.map((e) => [e.token, e.description || '']));
    // Sprint H item 9 — debug(...) is a valid call anywhere an
    // action/channel call is expected inside DO or alert(...)'s own body
    // (it's compiled to a real `log` action regardless of which position
    // it was written in — see `_compileSharedCall`).
    tokens.push('debug');
    descByToken.set('debug', 'Emit a debug log line for a bound label or matched-row column — debug(x).');
    // "nop" means "this WHOLE clause/call body does nothing" — only
    // legal as the FIRST (and only) item. For `do`, that's right after
    // the literal DO keyword itself (`lastWord === 'do'`). For `alert`,
    // there is no leading keyword at all any more (`alert(...)`'s body
    // lives inside real parens, reached by position — see
    // `_clauseKindFromTokenPrefix`'s own doc) — a FRESH, otherwise-empty
    // body is instead detected by `contextTok` being entirely absent
    // (nothing lexed yet inside this specific open call's own span; see
    // `_suggestAgentCliAtImpl`'s alert-body branch, which lexes ONLY the
    // text inside alert's own parens, so an empty/fresh body naturally
    // produces no tokens at all there).
    const isClauseStart = lastWord === clauseKind || (clauseKind === 'alert' && !contextTok);
    if (isClauseStart) { tokens.push('nop'); descByToken.set('nop', 'Do nothing — this clause has no channels/actions.'); }
    return { tokens, descByToken };
  }

  if (clauseKind === 'preamble') {
    // Sprint [grammar-unify] — the preamble is self-delimiting by
    // position, not introduced by any keyword (GIVEN is gone), and
    // accepts the FULL shared statement vocabulary — the SAME candidate
    // set DO already offers (every real catalog action, plus debug(...)),
    // per the plan's own explicit "no narrower than DO" requirement.
    // Deliberately NO `nop` here — a bare top-level `nop` is meaningless
    // for an unconditional preamble item (there's no clause to declare
    // empty), and the parser's own preamble item-list never accepts it
    // (`allowNop: false`, see `parseAgentStmt`).
    const base = [...catalog.actions.values()];
    const tokens = base.map((e) => e.token);
    const descByToken = new Map(base.map((e) => [e.token, e.description || '']));
    tokens.push('debug');
    descByToken.set('debug', 'Emit a debug log line for a bound label or matched-row column — debug(x).');
    return { tokens, descByToken };
  }

  const base = _candidateTokensForClause(clauseKind, catalog) || [];
  const descMap = _descMapForClause(clauseKind, catalog) || new Map();
  return { tokens: base, descByToken: descMap };
}

// The four Sprint 7 meta keys (`#name=`/`#desc=`/`#tag=`/`#group=`) — kept
// as a small local list rather than importing `_META_KEY_MAP`'s own keys
// so this suggestion-only helper has no dependency on that map's shape.
const _META_SUGGEST_KEYS = Object.freeze(['name', 'desc', 'tag', 'group']);

/** Sprint H (item 5) — suggest the meta-tag key names (`name=`/`desc=`/
 *  `tag=`/`group=`) right after a trailing "#", or while typing a partial
 *  key. `lexAgentStatement` throws on a bare/incomplete trailing "#...="
 *  (there's no value yet to lex), which is exactly why this position was
 *  getting ZERO suggestions before this sprint — the whole-prefix lex in
 *  `_suggestAgentCliAtImpl` bails the instant ANY lex error appears
 *  anywhere in the text up to the cursor, including one caused by the
 *  very "#" the operator just typed. Worked around the same way the "@"/
 *  "."-anchored scope_ref branches already handle their own special
 *  trailing punctuation: lex/parse only the text BEFORE the last "#"
 *  (which must itself be a complete, valid statement — meta is always
 *  trailing-only, so anything before the LAST "#" is either the main
 *  clauses or an earlier, already-complete `#key=value` meta item) and
 *  handle the in-progress `#key` text by hand. Returns `null` (not
 *  `_emptySuggest`) when there's no "#" at all, or the position doesn't
 *  look like an in-progress meta key, so the caller can fall through to
 *  its own (also likely-empty) handling without this helper masking a
 *  genuinely different degenerate case. */
function _suggestMetaKey(text, pos) {
  const prefix = text.slice(0, pos);
  const lastHash = prefix.lastIndexOf('#');
  if (lastHash < 0) return null;
  const keyPart = prefix.slice(lastHash + 1);
  if (keyPart !== '' && !/^[A-Za-z_][A-Za-z0-9_]*$/.test(keyPart)) return null;
  const beforeHash = prefix.slice(0, lastHash);
  const parsed = _tryParsePrefixStatement(beforeHash, beforeHash.length);
  if (!parsed || parsed.errors.length || !parsed.ast) return null;
  const usedKeys = new Set((parsed.ast.meta || []).map((m) => String(m.key).toLowerCase()));
  const available = _META_SUGGEST_KEYS.filter((k) => !usedKeys.has(k));
  const ranked = fuzzyFilter(keyPart, available);
  return {
    suggestions: ranked.map((k) => `${k}=`),
    descriptions: ranked.map(() => ''),
    labels: ranked.map(() => null),
    replaceRange: [lastHash + 1, pos],
    kind: 'meta',
    hint: null,
  };
}

// Sprint H (item 8/9) — `debug(...)`'s one bare argument accepts a bound
// label (either kind) or a matched-row exposed-column name (see
// `_compileDebugCall`) — fuzzy-filtered against whatever's typed so far
// between its own parens. Built as a plain fuzzy-filter over the union of
// those two name sets, mirroring every OTHER bare-name branch's shape.
function _suggestDebugArg(text, parenPos, pos, catalog) {
  const partial = text.slice(parenPos + 1, pos).trim();
  const bindings = _collectBindingsUpToCursor(text, _callNameStartBefore(text, parenPos));
  const labelNames = [...bindings.values()].map((b) => b.label);
  const columnNames = [..._allExposedColumnNames(catalog)];
  const candidates = [...labelNames, ...columnNames];
  const ranked = fuzzyFilter(partial, candidates);
  return {
    suggestions: ranked,
    descriptions: ranked.map((t) => (labelNames.includes(t) ? 'Label bound earlier in this statement.' : 'Matched-row column.')),
    labels: ranked.map(() => null),
    replaceRange: [parenPos + 1, pos],
    kind: 'token',
    hint: null,
  };
}

/** Shift a `suggestAgentCliAt`-shaped result's `replaceRange` by `offset`
 *  — used when a result was computed against a NESTED span (e.g.
 *  `alert(...)`'s own body, sliced out and treated as its own
 *  self-contained "text") so the caller's returned offsets are absolute
 *  in the REAL, outer text again. `kind: null`/`_emptySuggest`-shaped
 *  results have a `[pos, pos]` range too, so this is safe to apply
 *  unconditionally — never special-cased per `kind`. */
function _shiftSuggestResult(result, offset) {
  if (!result || !Array.isArray(result.replaceRange)) return result;
  return { ...result, replaceRange: [result.replaceRange[0] + offset, result.replaceRange[1] + offset] };
}

/** Step 2 of `_suggestAgentCliAtImpl` ("not inside an ordinary call —
 *  bare metric/channel/action NAME being typed"), factored out (Sprint
 *  [grammar-unify]) so the IDENTICAL logic can also run scoped to a
 *  NESTED span — specifically `alert(...)`'s own body, which is a
 *  nested statement-list (channel calls, `debug(...)`, `nop`) living
 *  inside real parens now, rather than a bare top-level clause — with a
 *  FORCED clauseKind instead of deriving one from `_clauseKindFromTokenPrefix`.
 *
 *  `text`/`pos` are in ONE coordinate space throughout (the caller slices/
 *  shifts for a nested span — this function never does so itself).
 *  `forcedClauseKind`, when non-null (always `'alert'` in practice),
 *  skips `_clauseKindFromTokenPrefix` entirely AND suppresses the
 *  scope-ref/comparator/given_rhs/clause-boundary-keyword branches —
 *  none of those make sense inside a nested call's own parens (there's
 *  no WHEN/DO to start, no comparator to complete, etc.) — falling
 *  straight through to the clause's own bare-name candidate set. */
function _suggestBareNamePosition(text, pos, catalog, opts, forcedClauseKind) {
  const { tokens, errors } = lexAgentStatement(text);
  if (errors.length) {
    // Sprint H (item 5) — the ONE lex failure this file still tries to
    // recover a suggestion from: a trailing, not-yet-complete "#key"
    // meta-tag position (`lexAgentStatement` throws there by necessity —
    // there's no value yet to lex). Never reachable for a forced
    // (nested) span — a `#` meta tag only ever appears trailing the
    // WHOLE statement, never inside a call's own parens.
    if (forcedClauseKind) return _emptySuggest(pos);
    return _suggestMetaKey(text, pos) || _emptySuggest(pos);
  }
  if (!tokens.length) return _emptySuggest(pos);

  // Drop the synthetic EOF token lexAgentStatement always appends.
  const real = tokens[tokens.length - 1].type === 'EOF' ? tokens.slice(0, -1) : tokens;
  const lastTok = real[real.length - 1];

  if (!forcedClauseKind) {
    // "Immediately after @ or ." (scope-ref position) — suggest scope
    // names (see `_suggestScopeRef`); degrades to empty when the char
    // isn't actually part of a real scope_ref (e.g. an incomplete decimal
    // literal's trailing '.').
    const wordStart = (lastTok && lastTok.type === 'NAME' && !lastTok.hasCall && lastTok.end === pos)
      ? lastTok.start : pos;
    if (_nearestNonSpaceBefore(text, wordStart) === '@' || _nearestNonSpaceBefore(text, wordStart) === '.') {
      // "@"-anchored scope_ref (metric_ref's own `@ scope_ref`) takes
      // priority — completely unaffected by Sprint 15. Only when there's
      // genuinely no "@" anywhere before this chain (a BARE dotted
      // `col_ref` leaf, e.g. typing `positions.` directly) does the new
      // column/deeper-scope completion (`_suggestBareDottedRef`) apply.
      if (_scopeRefContextFromTokens(real, pos)) return _suggestScopeRef(real, text, pos, catalog);
      return _suggestBareDottedRef(real, text, pos, catalog);
    }
  }

  // Sprint H — `contextTok` is the token immediately preceding whatever
  // "fresh" text the cursor is about to extend: the LAST real token when
  // the cursor sits in a pure gap (nothing glued), or the token BEFORE a
  // still-being-typed glued partial NAME. Every position-specific branch
  // below (comparator, given_rhs "=", clause-boundary keyword, ordinary
  // metric/channel/action candidates) classifies off this ONE token, so
  // the glued-partial and pure-gap cases share one classification path
  // instead of two parallel copies.
  // Bug fix (2026-10) — a glued partial whose typed text ALREADY equals a
  // complete reserved word (e.g. the operator has just finished typing
  // "do"/"Do"/"DO") used to bail to `_emptySuggest` here unconditionally,
  // so the dropdown visibly collapsed to nothing the instant the last
  // letter of a legal clause-head keyword was typed — reported by the
  // operator as "DO" not showing as selected (misdiagnosed as a case-
  // sensitivity issue; `fuzzyFilter` already lowercases both sides, so
  // "Do"/"do"/"DO" were always equally affected — the real cause is this
  // early return, independent of case). A completed reserved word is
  // classified exactly like any other still-being-typed partial keyword
  // (e.g. "WH" for "WHEN", already handled below via `effectiveReal`/
  // `contextTok`) instead of being special-cased to empty, so it keeps
  // appearing as the single matched/selectable suggestion until the
  // operator types past it (a space, etc.) or it's genuinely illegal here.
  const isGluedPartial = !!(lastTok && lastTok.type === 'NAME' && !lastTok.hasCall && lastTok.end === pos);
  const partial = isGluedPartial ? lastTok.value : '';
  const replaceStart = isGluedPartial ? lastTok.start : pos;
  const effectiveReal = isGluedPartial ? real.slice(0, -1) : real;
  const contextTok = effectiveReal[effectiveReal.length - 1];

  if (!forcedClauseKind) {
    // (a) Comparator position — right after a COMPLETE metric_ref/col_ref
    // with no comparator yet (Sprint 20 item 2 / Sprint H item 3).
    if (_isAfterCompleteScopeChain(effectiveReal)) {
      const ranked = fuzzyFilter(partial, isGluedPartial ? _COMPARATOR_WORDS : _COMPARATOR_SUGGESTIONS);
      return { suggestions: ranked, descriptions: ranked.map(() => ''), labels: ranked.map(() => null), replaceRange: [replaceStart, pos], kind: 'comparator', hint: null };
    }
    // (b) Right after an already-complete comparator token — a value is
    // expected next; there's no catalog-driven completion for an arbitrary
    // number/string/enum/list, so surface a plain hint instead of silently
    // offering nothing (Sprint H item 3).
    if (!isGluedPartial && _isComparatorToken(contextTok)) {
      return { suggestions: [], descriptions: [], labels: [], replaceRange: [pos, pos], kind: 'value-hint', hint: 'Enter a number, string, enum value, or [list] (for in / not in).' };
    }
    // (c) Right after a label-bind's own "=" — a `given_rhs` value
    // position (Sprint H items 6/8), distinct from meta's `#key=` (same
    // token shape, different grammar entirely — see `_isMetaKeyEq`).
    if (contextTok && contextTok.type === 'EQ' && !_isMetaKeyEq(effectiveReal)) {
      const { tokens: rhsTokens, descByToken } = _givenRhsCandidates(catalog, text, replaceStart);
      const ranked = fuzzyFilter(partial, rhsTokens);
      const descriptions = ranked.map((t) => descByToken.get(t) || '');
      return { suggestions: ranked, descriptions, labels: ranked.map(() => null), replaceRange: [replaceStart, pos], kind: 'token', hint: null };
    }
  }

  const clauseKind = forcedClauseKind || _clauseKindFromTokenPrefix(effectiveReal);

  if (!forcedClauseKind && !isGluedPartial && !_isFreshNamePosition(contextTok)) {
    // (d) Not a legal fresh metric/channel/action slot — the only thing
    // that can still apply here is a clause-head keyword or boolean
    // connective (Sprint H items 1/3), via a real speculative parse
    // rather than a token-pattern heuristic. Never reached for a forced
    // (nested alert-body) span — WHEN/DO/&/| can never follow anything
    // inside a call's own parens.
    const kws = _filterTokensByAllowedKinds(_clauseBoundaryKeywordCandidates(text, pos, effectiveReal), opts.allowedKinds, catalog);
    if (!kws.length) return _emptySuggest(pos);
    return { suggestions: kws, descriptions: kws.map(() => ''), labels: kws.map(() => null), replaceRange: [pos, pos], kind: 'keyword', hint: null };
  }

  // (e) A genuinely fresh metric/channel/action slot — the clause's own
  // ordinary candidates (now via `_bareNameCandidates`, item 1/6/8/9's
  // extras folded in), PLUS whatever clause-head keyword/connective is
  // ALSO legal right here (item 1/7 — covers both the absolute statement
  // start and a still-being-typed partial keyword, e.g. "WH" for "WHEN"
  // after a complete leaf) — omitted entirely for a forced clauseKind.
  const { tokens: baseCandidates, descByToken } = _bareNameCandidates(clauseKind, catalog, contextTok, text, replaceStart);
  const headKeywords = forcedClauseKind ? [] : _clauseBoundaryKeywordCandidates(text, replaceStart, effectiveReal);
  // 2026-10-15 fix (operator: "does the dropdown for orders cli, non
  // order related tokens functions. check it") — confirmed live: the
  // Orders CLI tab's bare-start dropdown suggested the FULL
  // catalog.actions list (modify_order, monitor_order, close_position,
  // chase_close_positions, expiry_auto_close, set_flag, debug, nop) PLUS
  // every clause-head keyword, even though this editor's `allowedKinds`
  // ONLY permits `order`/`cancel_all_orders` bare statements — every one
  // of those suggestions was guaranteed to fail at compile time with the
  // scope-rejection error (already correctly enforced there, just never
  // reflected back into the SUGGESTIONS themselves). Filtered at the
  // single combination point both the clause-boundary keywords (d, above)
  // and the ordinary bare-name candidates (here) pass through, so neither
  // list offers something the compiler is certain to reject on this mount.
  const allTokens = [...baseCandidates, ..._filterTokensByAllowedKinds(headKeywords, opts.allowedKinds, catalog)];
  const filteredTokens = _filterTokensByAllowedKinds(allTokens, opts.allowedKinds, catalog);
  if (!filteredTokens.length) return _emptySuggest(pos);
  const ranked = fuzzyFilter(partial, filteredTokens);
  const descriptions = ranked.map((t) => descByToken.get(t) || '');
  return { suggestions: ranked, descriptions, labels: ranked.map(() => null), replaceRange: [replaceStart, pos], kind: 'token', hint: null };
}

function _suggestAgentCliAtImpl(text, cursor, catalog, opts = {}) {
  if (typeof text !== 'string') return _emptySuggest(0);
  const n = text.length;
  const pos = Math.max(0, Math.min(Number(cursor) || 0, n));

  // 1. Inside a call's (possibly still-open) argument list?
  const openCall = _findOpenCallAt(text, pos);
  if (openCall) {
    const name = text.slice(openCall.nameStart, openCall.nameEnd);
    // Sprint H (item 9) — `debug(...)` has no catalog entry at all (its
    // one bare argument is a label/column reference, never a schema-
    // declared kwarg) — handled entirely separately, before the ordinary
    // catalog-entry lookup below would otherwise just miss and degrade
    // to nothing.
    if (name.toLowerCase() === 'debug') return _suggestDebugArg(text, openCall.parenPos, pos, catalog);
    // Sprint [grammar-unify] — `alert(...)`'s own body is NOT an
    // ordinary call's key=value arg list; it's a nested statement-list
    // (channel calls, `debug(...)`, `nop`) — the SAME shape the old bare
    // `ALERT` clause always had, just living inside real parens now (see
    // `lexAgentStatement`'s `alert`-exclusion comment and `parseCall`'s
    // own `alert`-specific branch). `alert` itself has no catalog entry
    // at all, so the ordinary "look up this call's own params_schema"
    // path below would otherwise just degrade to empty — recurse into
    // `_suggestBareNamePosition` instead, scoped to the inner span, with
    // clauseKind FORCED to `'alert'` (`_bareNameCandidates`'s own
    // channel+debug+nop set), then shift the result back to absolute
    // offsets in the real, outer `text`.
    if (name.toLowerCase() === 'alert') {
      const innerStart = openCall.parenPos + 1;
      const result = _suggestBareNamePosition(text.slice(innerStart, pos), pos - innerStart, catalog, opts, 'alert');
      return _shiftSuggestResult(result, innerStart);
    }
    const scopeHint = _nearestNonSpaceBefore(text, openCall.nameStart) === '@'
      || _nearestNonSpaceBefore(text, openCall.nameStart) === '.';
    const prefixLex = lexAgentStatement(text.slice(0, openCall.nameStart));
    const clauseKind = prefixLex.errors.length ? null : _clauseKindFromTokenPrefix(prefixLex.tokens);
    const entry = _lookupAnyCatalogEntry(name, catalog, clauseKind, scopeHint);
    if (!entry) return _emptySuggest(pos);
    return _paramOrEnumSuggestion(text, openCall.parenPos, pos, entry, opts);
  }

  // 2. Not inside a call — bare metric/channel/action NAME being typed.
  //    Lex only the text UP TO THE CURSOR so a half-typed later clause
  //    never blanks out suggestions for the clause actually being edited.
  return _suggestBareNamePosition(text.slice(0, pos), pos, catalog, opts, null);
}

/** True when `lastTok` (the last REAL token lexed before the cursor, or
 *  `undefined` at the very start of the text) legally precedes a fresh
 *  metric/channel/action NAME per the grammar — see call site above. */
function _isFreshNamePosition(lastTok) {
  if (!lastTok) return true; // start of text — nothing typed yet
  if (lastTok.type === 'NAME' && lastTok.reserved) {
    const w = String(lastTok.value).toLowerCase();
    // Sprint 20 (item 3) — 'and'/'or'/'not' legally precede a fresh
    // metric/scope name too, same as 'when'/'do' already did, now that
    // they're accepted word aliases for '&'/'|'/'~'. Sprint [grammar-unify]
    // — 'given'/'alert' are REMOVED here: neither is reserved any more
    // (see `_RESERVED`'s own comment), so `lastTok.reserved` is never
    // true for either — this branch is simply unreachable for them,
    // regardless of what word list it checks.
    return w === 'when' || w === 'do' || w === 'and' || w === 'or' || w === 'not';
  }
  // Sprint H — SEMI joins the structural set: `;` separates items within
  // the preamble/DO's own item list (Sprint G), and a fresh item legally
  // follows it the same way one follows a call-list ",".
  return lastTok.type === 'AMP' || lastTok.type === 'PIPE' || lastTok.type === 'TILDE'
    || lastTok.type === 'LPAREN' || lastTok.type === 'COMMA' || lastTok.type === 'SEMI';
}

/** Live autocomplete for the agent CLI textarea. Never throws — returns
 *  `{ suggestions: string[], descriptions: string[], labels: (string|null)[],
 *  replaceRange: [start, end], kind: 'token'|'param'|'enum'|'scope'|'column'|null }`.
 *  `descriptions` and `labels` are always the SAME length/order as
 *  `suggestions` — Sprint 11 surfaces each catalog token's own
 *  `description` (bare token-name / scope-ref suggestions) or each
 *  param's own `paramsSchema[key].description` (param-name suggestions
 *  inside an open call, now with a Sprint 17 `required_if`/
 *  `mutuallyExclusiveGroups`-derived line appended — see
 *  `_paramConstraintText`); enum-VALUE suggestions have no per-value
 *  description anywhere in the backend schema, so that branch always
 *  returns empty strings. `suggestions[i]` is ALWAYS the literal text to
 *  insert (what `AgentCliEditor.svelte`'s `applySuggestion` writes into
 *  the textarea) — `labels[i]` (Sprint 15) is `null` for every kind
 *  except `'column'`, where it carries the exposed column's SHORT
 *  operator-facing display text (e.g. `"P&L (₹)"`) to show INSTEAD of
 *  the long compound token string `suggestions[i]` actually holds (e.g.
 *  `"positions.total.pnl"`) — the UI shows `labels[i] || suggestions[i]`.
 *  See the section header above for the design rationale. */
export function suggestAgentCliAt(text, cursor, catalog, opts = {}) {
  try {
    return _suggestAgentCliAtImpl(text, cursor, catalog, opts);
  } catch {
    return _emptySuggest(Number(cursor) || 0);
  }
}

// ═══════════════════════════════════════════════════════════════════════
// (f) Click-to-describe an ALREADY-TYPED token (distinct from the live
//     autocomplete dropdown above, which only ever describes a candidate
//     while it's still being typed). See `describeTokenAt`'s own doc
//     comment below for the full design — this section is purely
//     additive and reuses `_findOpenCallAt`/`_lookupAnyCatalogEntry`/
//     `_clauseKindFromTokenPrefix`/`_collectBindingsUpToCursor` exactly
//     as they already stand (no internal change to any of the four).
// ═══════════════════════════════════════════════════════════════════════

/** Forward scan from a click position `pos` to a safe slice end for
 *  `describeTokenAt`'s tier-2 lex fallback — extends just far enough to
 *  close out whatever call-paren nesting is already open AT `pos` (so the
 *  slice up to the returned offset is balanced and lexes cleanly), then
 *  stops at the next top-level whitespace gap once paren depth returns to
 *  (or starts at) zero. Quote-aware so a comma/paren INSIDE a string
 *  value never miscounts. Mirrors `_suggestAgentCliAtImpl`'s own "lex a
 *  self-contained slice, not the whole possibly-broken text" strategy —
 *  just extended forward from an already-typed click position instead of
 *  backward from a live typing cursor (which never needs to look ahead of
 *  itself). */
function _probeSliceEndAt(text, pos) {
  const n = text.length;
  let i = Math.max(0, Math.min(pos, n));
  let depth = 0;
  let inQuote = null;
  while (i < n) {
    const ch = text[i];
    if (inQuote) { i++; if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; i++; continue; }
    if (ch === '(') { depth++; i++; continue; }
    if (ch === ')') { i++; depth--; if (depth <= 0) break; continue; }
    if (depth <= 0 && /\s/.test(ch)) break;
    i++;
  }
  return i;
}

/** Quote-aware scan for the TEXT content of the call whose "(" sits at
 *  `parenPos`, stopping at its matching ")" when one exists, or at
 *  end-of-text for a genuinely still-open call. Used only by
 *  `_describeOpenCallParam`'s tier-3 fallback — distinct from
 *  `_findOpenCallAt` (which only locates the open paren, quote-aware but
 *  cursor-bounded) and from the lexer's own per-call paren-matching
 *  (which throws rather than degrading when a call never closes). */
function _scanCallArgsText(text, parenPos) {
  const n = text.length;
  let i = parenPos + 1;
  let depth = 1;
  let inQuote = null;
  while (i < n && depth > 0) {
    const ch = text[i];
    if (inQuote) { if (ch === inQuote) inQuote = null; i++; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; i++; continue; }
    else if (ch === '(') depth++;
    else if (ch === ')') depth--;
    i++;
  }
  const end = depth > 0 ? n : i - 1;
  return text.slice(parenPos + 1, end);
}

/** Render a preamble/DO label binding (as collected by
 *  `_collectBindingsUpToCursor`) into a `describeTokenAt` result — the
 *  "preamble-bound label used elsewhere" case called out as a confirmed
 *  coverage gap in the original design (a plain catalog lookup never
 *  checks bindings at all). Sprint [grammar-unify]: label assignment is
 *  no longer gated behind a `GIVEN` keyword — it's an ordinary preamble
 *  item — so the wording no longer names a specific reserved word. */
function _describeBinding(binding) {
  const rhs = String(binding.rhsText || '').trim();
  const description = rhs ? `Bound label — ${binding.label} = ${rhs}` : `Bound label — ${binding.label}`;
  return { name: binding.label, description, token: binding.label, kind: 'binding' };
}

/** Resolve a param-key description for a position `pos` (absolute offset
 *  in `text`) known to sit INSIDE a CLOSED call's `(...)` — `coveringTok`
 *  is the NAME token (`hasCall: true`) found by `describeTokenAt`'s own
 *  covering-token scan. Scans `coveringTok.args` directly (each ArgEntry
 *  already carries its own `keyStart`/`value.start`/`value.end`) rather
 *  than re-tokenizing — NOTE these positions are relative to the call's
 *  OWN inner text (right after "("), not absolute in `text`, so every
 *  comparison below shifts `pos` into that same relative frame via
 *  `innerStart` first. Returns `null` when the call has no catalog entry
 *  (e.g. `debug(...)`, which takes a bare label/column reference, not a
 *  schema kwarg) or when no arg actually covers `pos` (a comma/space gap
 *  between args). */
function _describeClosedCallArg(coveringTok, text, pos, catalog, clauseKind) {
  const scopeHint = _nearestNonSpaceBefore(text, coveringTok.start) === '@'
    || _nearestNonSpaceBefore(text, coveringTok.start) === '.';
  const entry = _lookupAnyCatalogEntry(coveringTok.value, catalog, clauseKind, scopeHint);
  if (!entry || !Array.isArray(coveringTok.args)) return null;
  const innerStart = coveringTok.start + String(coveringTok.value).length + 1;
  const relPos = pos - innerStart;
  for (const arg of coveringTok.args) {
    const keyLen = String(arg.key || '').length;
    const lo = Math.min(arg.keyStart, arg.value.start);
    const hi = Math.max(arg.value.end, arg.keyStart + keyLen);
    if (relPos < lo || relPos >= hi) continue;
    const rawKey = String(arg.key || '').toLowerCase();
    const realKey = _ALIAS_TO_REAL[rawKey] || rawKey;
    const spec = entry.paramsSchema && entry.paramsSchema[realKey];
    if (!spec) return null;
    return { name: realKey, description: spec.description || '', token: coveringTok.value, kind: 'param' };
  }
  return null;
}

/** `true` iff `pos` sits inside a quoted-string literal that is still
 *  open at that point, scanning `text` from the very start (unlike
 *  `_probeSliceEndAt`'s forward-only scan, which has no way to know a
 *  quote was already open before the position it starts from). Used only
 *  to gate the tier-3 open-call fallback — see its own comment. */
function _isInsideOpenQuoteAt(text, pos) {
  let inQuote = null;
  const n = Math.min(pos, text.length);
  for (let i = 0; i < n; i++) {
    const ch = text[i];
    if (inQuote) { if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") inQuote = ch;
  }
  return inQuote !== null;
}

/** Tier-3 fallback for `describeTokenAt` — reached only when BOTH lex
 *  tiers (full text, then the tier-2 probe slice) threw, meaning the call
 *  enclosing `pos` can't be lexed as a clean, closed token at all (it's
 *  genuinely still open — the realistic "operator clicked an already-typed
 *  param name while still mid-edit on this very call" case). Reuses
 *  `_findOpenCallAt` + the same raw-text key=value scan
 *  `_paramOrEnumSuggestion` already does for live autocomplete, adapted to
 *  resolve "which already-typed key covers this position" instead of
 *  "what completes at the cursor". */
function _describeOpenCallParam(text, pos, catalog) {
  // Guard against the "unterminated string spanning the click position"
  // failure mode called out in `describeTokenAt`'s own doc comment: once
  // a quote never closes, every quote-aware scan below (comma-splitting,
  // top-level "=" detection) treats everything AFTER it as still inside
  // that one string, so a later, unrelated "=" earlier in the text (e.g.
  // an EARLIER param's own key) can wrongly appear to be the "last
  // top-level one" and get matched instead of returning no match. Bail
  // out before ever reaching that ambiguity, rather than resolve to a
  // description that may not actually belong to the clicked token.
  if (_isInsideOpenQuoteAt(text, pos)) return null;
  const openCall = _findOpenCallAt(text, pos);
  if (!openCall) return null;
  const name = text.slice(openCall.nameStart, openCall.nameEnd);
  if (name.toLowerCase() === 'debug') return null; // no catalog entry — a bare label/column ref, not a kwarg
  const scopeHint = _nearestNonSpaceBefore(text, openCall.nameStart) === '@'
    || _nearestNonSpaceBefore(text, openCall.nameStart) === '.';
  const prefixLex = lexAgentStatement(text.slice(0, openCall.nameStart));
  const clauseKind = prefixLex.errors.length ? null : _clauseKindFromTokenPrefix(prefixLex.tokens);
  const entry = _lookupAnyCatalogEntry(name, catalog, clauseKind, scopeHint);
  if (!entry) return null;
  const innerText = _scanCallArgsText(text, openCall.parenPos);
  const segments = _splitTopLevelCommasWithOffsets(innerText);
  for (const seg of segments) {
    const segAbsStart = openCall.parenPos + 1 + seg.start;
    const segAbsEnd = segAbsStart + seg.text.length;
    if (pos < segAbsStart || pos > segAbsEnd) continue;
    const eq = _findTopLevelEquals(seg.text);
    if (eq < 0) return null;
    const rawKey = seg.text.slice(0, eq).trim().toLowerCase();
    if (!rawKey) return null;
    const realKey = _ALIAS_TO_REAL[rawKey] || rawKey;
    const spec = entry.paramsSchema && entry.paramsSchema[realKey];
    if (!spec) return null;
    return { name: realKey, description: spec.description || '', token: name, kind: 'param' };
  }
  return null;
}

function _describeTokenAtImpl(text, pos, catalog) {
  if (typeof text !== 'string') return null;
  const n = text.length;
  const p = Math.max(0, Math.min(Number(pos) || 0, n));

  let tokens = null;
  const tier1 = lexAgentStatement(text);
  if (!tier1.errors.length) {
    tokens = tier1.tokens;
  } else {
    const probeEnd = _probeSliceEndAt(text, p);
    const tier2 = lexAgentStatement(text.slice(0, probeEnd));
    if (!tier2.errors.length) tokens = tier2.tokens;
  }

  if (tokens) {
    const real = tokens.length && tokens[tokens.length - 1].type === 'EOF' ? tokens.slice(0, -1) : tokens;
    let idx = -1;
    for (let i = 0; i < real.length; i++) {
      const t = real[i];
      if (p >= t.start && p < t.end) { idx = i; break; }
    }
    if (idx < 0 && real.length && p === n) {
      const last = real[real.length - 1];
      if (last.end === n) idx = real.length - 1;
    }
    if (idx >= 0) {
      const covering = real[idx];
      if (covering.type !== 'NAME') return null; // literal/punctuation — no tooltip
      let clauseKind = _clauseKindFromTokenPrefix(real.slice(0, idx + 1));
      const nameEnd = covering.start + String(covering.value).length;
      if (covering.hasCall && p > nameEnd) {
        // Inside this (closed) call's own parens — resolve a param key,
        // never a bound-label match on the call's own NAME (that check
        // only applies to a bare reference, handled below).
        return _describeClosedCallArg(covering, text, p, catalog, clauseKind);
      }
      // Sprint [grammar-unify] — `covering` is a BARE name (not itself a
      // call — `alert` is lexed to never set `hasCall`, see
      // `lexAgentStatement`'s own `alert`-exclusion comment, so a
      // channel call like `telegram`/`sig` sitting directly inside
      // `alert(...)`'s real parens reaches HERE, not the branch above).
      // `_clauseKindFromTokenPrefix` only ever sees WHEN/DO markers, so
      // it has no way to know we're nested inside alert's own body —
      // `_findOpenCallAt` (quote-aware, prefix-only — correct regardless
      // of whether this particular call has since closed) answers that
      // directly: if the nearest enclosing unclosed-as-of-`p` paren is
      // immediately preceded by the literal word `alert`, force
      // clauseKind to `'alert'` so `_lookupAnyCatalogEntry` prioritizes
      // the channels bucket, same as it always has for a real ALERT
      // clause.
      const enclosing = _findOpenCallAt(text, p);
      if (enclosing && text.slice(enclosing.nameStart, enclosing.nameEnd).toLowerCase() === 'alert') {
        clauseKind = 'alert';
      }
      const bindings = _collectBindingsUpToCursor(text, p);
      const lower = String(covering.value).toLowerCase();
      if (bindings.has(lower)) return _describeBinding(bindings.get(lower));
      const scopeHint = _nearestNonSpaceBefore(text, covering.start) === '@'
        || _nearestNonSpaceBefore(text, covering.start) === '.';
      const entry = _lookupAnyCatalogEntry(covering.value, catalog, clauseKind, scopeHint);
      if (!entry) return null;
      return { name: entry.token || covering.value, description: entry.description || '', token: covering.value, kind: 'token' };
    }
    // `pos` sits in a whitespace/comma gap the successful lex still
    // produced tokens for — fall through to the tier-3 open-call probe
    // below (safe: it degrades to `null` for a genuine gap/punctuation
    // click that isn't inside any call's parens at all).
  }

  return _describeOpenCallParam(text, p, catalog);
}

/** Click-to-describe an already-typed CLI token — resolves whatever sits
 *  at `pos` (an absolute offset into `text`, e.g. the textarea's
 *  `selectionStart` at the click point) to a short description, WITHOUT
 *  touching `text`/selection/compile output in any way (pure read-only
 *  lookup — never throws, mirrors `suggestAgentCliAt`'s own try/catch
 *  shape). Distinct from `suggestAgentCliAt`: that function describes a
 *  CANDIDATE while it's still being typed (keyed off the live cursor and
 *  the dropdown's own suggestion list); this one describes whatever is
 *  ALREADY sitting at an arbitrary position, including a token the
 *  operator typed long ago and is now just clicking on to recall what it
 *  means.
 *
 *  Resolution order: (1) lex `text` in full; if that throws (e.g. a
 *  LATER, unrelated unclosed call elsewhere in the statement — the
 *  realistic "in-progress agent" case, since a single unclosed paren
 *  ANYWHERE fails `lexAgentStatement`'s whole-text lex), fall back to
 *  lexing just a forward-extended slice around `pos` (`_probeSliceEndAt`)
 *  — a self-contained prefix that excludes whatever's broken later. If
 *  BOTH throw (e.g. an unterminated string spanning `pos`, or the call
 *  enclosing `pos` is itself genuinely still open), fall back once more to
 *  `_findOpenCallAt` + a raw-text key=value scan (same mechanism
 *  `suggestAgentCliAt` already uses for live typing) before finally
 *  giving up and returning `null`. (2) Find the token whose `[start,end)`
 *  covers `pos` — a NAME token only; a literal/punctuation token (and any
 *  position landing in a pure whitespace gap outside a call) returns
 *  `null`. (3) Derive clause context via the existing
 *  `_clauseKindFromTokenPrefix`. (4) Check `_collectBindingsUpToCursor`
 *  FIRST — a GIVEN/ALERT/DO-bound label referenced elsewhere describes as
 *  the binding itself, not a catalog lookup (`_lookupAnyCatalogEntry`
 *  never consults bindings). (5) For a position inside a CLOSED call's own
 *  `(...)`, scan its pre-parsed `.args` for the one covering `pos` and
 *  resolve via that call's `paramsSchema`; for a position inside a
 *  still-OPEN call, the same resolution via the tier-3 fallback above.
 *  (6) Otherwise resolve the covering token's own name via the existing
 *  `lookupAgentCliEntry`/`_lookupAnyCatalogEntry`, same as the dropdown's
 *  hover-description path.
 *
 *  Returns `{ name, description, token, kind }` — `kind` is
 *  `'token'` (plain metric/scope/channel/action name), `'param'` (a call's
 *  kwarg key), or `'binding'` (a GIVEN/ALERT/DO-bound label reference) —
 *  or `null` when nothing describable sits at `pos`. */
export function describeTokenAt(text, pos, catalog) {
  try {
    return _describeTokenAtImpl(text, pos, catalog);
  } catch {
    return null;
  }
}
