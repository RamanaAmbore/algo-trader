// @ts-nocheck — Chevrotain's `$.RULE('name', ...)` dynamically assigns a
// same-named method onto the parser instance; svelte-check's static JSDoc
// type inference (checkJs) has no way to see those methods on the
// `AgentsCstParser` class type (same class of issue as the `-nocheck`
// pragma already used elsewhere in this codebase for similarly dynamic
// JS — see e.g. `pulseUnified.test.js`). Behavior is unaffected; this
// silences false-positive "Property X does not exist" errors only.
// agentsParser.js — Chevrotain CST parser + CST→AST visitor for the agent
// CLI grammar (Phase 1 of the migration plan — see .claude/PLAN.md and the
// header comment in agentsLexer.js for the full design rationale, the
// tp_scales_json-unification decision, and the documented, known gaps
// relative to the hand-written parser in agents.js).
//
// NOT wired into production. `agents.js`'s `parseStatement` remains the
// live entry point everywhere (compile/decompile/suggest/describe). This
// module's only consumer today is the differential test harness,
// `__tests__/agentsChevrotainDiff.test.js`.
//
// Exports `parseStatementNew(text)`, matching `parseStatement`'s own
// `{ ast, errors }` return contract for every statement shape this file
// covers (see the COVERED/NOT-COVERED lists below).

import { CstParser, EOF, tokenMatcher } from 'chevrotain';
import {
  AgentsLexer, allTokens,
  NUMBER, STRING, NAME,
  LE, GE, EQEQ, NE, LT, GT, EQ,
  LPAREN, RPAREN, LBRACKET, RBRACKET, LBRACE, RBRACE,
  COMMA, SEMI, AT, DOT, AMP, PIPE, TILDE, COLON, STAR,
} from './agentsLexer.js';

// ── Reserved words — same vocabulary as agents.js's `_RESERVED` ──────────
const RESERVED = new Set(['when', 'do', 'nop', 'always', 'in', 'not', 'and', 'or', 'true', 'false']);
const LABEL_RESERVED = new Set(['debug', 'result', 'alert', 'given', 'help']);
const EVENT_META_KEYS = new Set(['log_tag', 'min_level', 'render', 'where', 'gate', 'match']);
const COMPARATOR_MAP = { LT: '<', LE: '<=', GT: '>', GE: '>=', EQEQ: '==', NE: '!=' };

function lc(tok) { return tok.image.toLowerCase(); }
function isWord(tok, word) { return tok.tokenType === NAME && lc(tok) === word; }

// ═══════════════════════════════════════════════════════════════════════
// Grammar
// ═══════════════════════════════════════════════════════════════════════

class AgentsCstParser extends CstParser {
  constructor() {
    super(allTokens, { recoveryEnabled: false, maxLookahead: 2 });
    const $ = this;

    // ── value / list / object literals (unified — see agentsLexer.js's
    //    header comment on the tp_scales_json decision) ──────────────────
    $.RULE('value', () => {
      $.OR([
        { ALT: () => $.CONSUME(NUMBER) },
        { ALT: () => $.CONSUME(STRING) },
        { ALT: () => $.SUBRULE($.objectLiteral) },
        { ALT: () => $.SUBRULE($.listLiteral) },
        { ALT: () => $.CONSUME(NAME) },
      ]);
    });

    $.RULE('listLiteral', () => {
      $.CONSUME(LBRACKET);
      $.MANY_SEP({
        SEP: COMMA,
        DEF: () => $.SUBRULE($.listItem),
      });
      $.CONSUME(RBRACKET);
    });

    $.RULE('listItem', () => {
      $.OR([
        { ALT: () => $.SUBRULE($.objectLiteral) },
        { ALT: () => $.CONSUME(NUMBER) },
        { ALT: () => $.CONSUME(STRING) },
        { ALT: () => $.SUBRULE($.call) },
      ]);
    });

    $.RULE('objectLiteral', () => {
      $.CONSUME(LBRACE);
      $.MANY_SEP({
        SEP: COMMA,
        DEF: () => $.SUBRULE($.objField),
      });
      $.CONSUME(RBRACE);
    });

    $.RULE('objField', () => {
      $.CONSUME(NAME);
      $.CONSUME(EQ);
      $.SUBRULE($.objFieldValue);
    });

    $.RULE('objFieldValue', () => {
      $.OR([
        { ALT: () => $.CONSUME(NUMBER) },
        { ALT: () => $.CONSUME(STRING) },
        { ALT: () => $.SUBRULE($.objectLiteral) },
        { ALT: () => $.CONSUME(NAME) },
      ]);
    });

    // ── call / arg_list ───────────────────────────────────────────────
    $.RULE('call', () => {
      const nameTok = $.CONSUME(NAME);
      $.OPTION({
        GATE: () => tokenMatcher($.LA(1), LPAREN),
        DEF: () => {
          $.CONSUME(LPAREN);
          if (lc(nameTok) === 'alert') {
            $.SUBRULE($.itemList, { ARGS: [true] });
          } else if (lc(nameTok) === 'debug' || lc(nameTok) === 'help') {
            // `debug(x)`/`help(x)` — exactly one BARE argument (a label or
            // token-reference name, no "="), mirroring agents.js's own
            // `lexAgentStatement` special case (checked by name, before the
            // generic arg_list rule — same "contextual, not reserved"
            // carve-out). The synthetic key (`x`/`name`) matches
            // `_parseArgListFromInnerText`'s own hardcoded keys.
            $.CONSUME2(NAME);
          } else {
            $.OPTION2({
              GATE: () => !tokenMatcher($.LA(1), RPAREN),
              DEF: () => $.SUBRULE($.argList),
            });
          }
          $.CONSUME(RPAREN);
        },
      });
    });

    $.RULE('argList', () => {
      $.MANY_SEP({ SEP: COMMA, DEF: () => $.SUBRULE($.argEntry) });
    });

    $.RULE('argEntry', () => {
      $.CONSUME(NAME);
      $.CONSUME(EQ);
      $.SUBRULE($.value);
    });

    // ── scope_ref ──────────────────────────────────────────────────────
    $.RULE('scopeRef', () => {
      $.SUBRULE($.scopeSeg);
      $.MANY(() => { $.CONSUME(DOT); $.SUBRULE2($.scopeSeg); });
    });

    $.RULE('scopeSeg', () => {
      $.OR([
        {
          GATE: () => tokenMatcher($.LA(1), NAME) && tokenMatcher($.LA(2), COLON),
          ALT: () => $.SUBRULE($.qualifierSeg),
        },
        { ALT: () => $.SUBRULE($.call) },
      ]);
    });

    $.RULE('qualifierSeg', () => {
      $.CONSUME(NAME);
      $.CONSUME(COLON);
      $.OR([
        { ALT: () => $.CONSUME(STAR) },
        { ALT: () => $.CONSUME(STRING) },
        { ALT: () => $.CONSUME2(NAME) },
      ]);
    });

    // ── condition grammar ──────────────────────────────────────────────
    $.RULE('condition', () => {
      $.OR([
        {
          GATE: () => isWord($.LA(1), 'always'),
          ALT: () => $.CONSUME(NAME),
        },
        { ALT: () => $.SUBRULE($.orExpr) },
      ]);
    });

    $.RULE('orExpr', () => {
      $.SUBRULE($.andExpr);
      $.MANY({
        GATE: () => tokenMatcher($.LA(1), PIPE) || isWord($.LA(1), 'or'),
        DEF: () => {
          $.OR([{ ALT: () => $.CONSUME(PIPE) }, { ALT: () => $.CONSUME(NAME) }]);
          $.SUBRULE2($.andExpr);
        },
      });
    });

    $.RULE('andExpr', () => {
      $.SUBRULE($.term);
      $.MANY({
        GATE: () => tokenMatcher($.LA(1), AMP) || isWord($.LA(1), 'and'),
        DEF: () => {
          $.OR([{ ALT: () => $.CONSUME(AMP) }, { ALT: () => $.CONSUME(NAME) }]);
          $.SUBRULE2($.term);
        },
      });
    });

    $.RULE('term', () => {
      $.OPTION({
        GATE: () => tokenMatcher($.LA(1), TILDE) || isWord($.LA(1), 'not'),
        DEF: () => $.OR([{ ALT: () => $.CONSUME(TILDE) }, { ALT: () => $.CONSUME(NAME) }]),
      });
      $.SUBRULE($.primary);
    });

    $.RULE('primary', () => {
      $.OR([
        {
          ALT: () => { $.CONSUME(LPAREN); $.SUBRULE($.orExpr); $.CONSUME(RPAREN); },
        },
        { ALT: () => $.SUBRULE($.leaf) },
      ]);
    });

    $.RULE('comparator', () => {
      $.OR([
        { ALT: () => $.CONSUME(LE) }, { ALT: () => $.CONSUME(GE) },
        { ALT: () => $.CONSUME(LT) }, { ALT: () => $.CONSUME(GT) },
        { ALT: () => $.CONSUME(EQEQ) }, { ALT: () => $.CONSUME(NE) }, { ALT: () => $.CONSUME(EQ) },
        {
          GATE: () => isWord($.LA(1), 'not'),
          ALT: () => { $.CONSUME(NAME); $.CONSUME2(NAME); }, // "not" "in"
        },
        {
          GATE: () => isWord($.LA(1), 'in'),
          ALT: () => $.CONSUME3(NAME),
        },
      ]);
    });

    // `leaf := between | metric_ref/colref/bare-ref (comparator value)?`
    $.RULE('leaf', () => {
      $.OR([
        {
          GATE: () => tokenMatcher($.LA(1), NUMBER) || tokenMatcher($.LA(1), STRING) || tokenMatcher($.LA(1), LBRACKET),
          ALT: () => {
            $.SUBRULE($.value);
            $.SUBRULE2($.comparator);
            $.SUBRULE($.metricOrColRef);
            $.SUBRULE3($.comparator);
            $.SUBRULE2($.value);
          },
        },
        {
          ALT: () => {
            $.SUBRULE($.call);
            $.OR2([
              {
                GATE: () => tokenMatcher($.LA(1), DOT),
                ALT: () => {
                  $.MANY2(() => { $.CONSUME(DOT); $.SUBRULE($.scopeSeg); });
                  $.OPTION3({
                    GATE: () => this.isComparatorStart(),
                    DEF: () => { $.SUBRULE4($.comparator); $.SUBRULE3($.value); },
                  });
                },
              },
              {
                GATE: () => tokenMatcher($.LA(1), AT),
                ALT: () => {
                  $.CONSUME(AT);
                  $.SUBRULE2($.scopeRef);
                  $.OPTION4({
                    GATE: () => this.isComparatorStart(),
                    DEF: () => { $.SUBRULE5($.comparator); $.SUBRULE4($.value); },
                  });
                },
              },
              { ALT: () => {} }, // bare `ref` (Token reference / label)
            ]);
          },
        },
      ]);
    });

    // `metric_ref | col_ref` shared entry (between-chain's middle operand)
    $.RULE('metricOrColRef', () => {
      $.SUBRULE($.call);
      $.OR([
        {
          GATE: () => tokenMatcher($.LA(1), DOT),
          ALT: () => $.MANY(() => { $.CONSUME(DOT); $.SUBRULE($.scopeSeg); }),
        },
        {
          ALT: () => { $.CONSUME(AT); $.SUBRULE($.scopeRef); },
        },
      ]);
    });

    // ── clause_item list (GIVEN/ALERT/DO preamble + alert(...) body) ───
    // `allowMsgDefault` (Sprint 6's `msg="...", <call_list>` sugar) is
    // threaded as a real rule parameter, TRUE only for the FIRST item of
    // `alert(...)`'s own body (mirrors `parseItemListClause`'s own
    // `allowMsgDefault` — only ever true for alert's clause, and only for
    // the leading item; every later item in the SAME list parses with it
    // forced back to false — see `parseOneClauseItem`'s own docstring).
    $.RULE('itemList', (allowMsgDefault) => {
      $.OR([
        {
          GATE: () => isWord($.LA(1), 'nop') && !tokenMatcher($.LA(2), LPAREN) && !tokenMatcher($.LA(2), EQ),
          ALT: () => $.CONSUME(NAME),
        },
        {
          ALT: () => {
            $.SUBRULE($.clauseItem, { ARGS: [!!allowMsgDefault] });
            $.MANY(() => {
              $.OR2([{ ALT: () => $.CONSUME(COMMA) }, { ALT: () => $.CONSUME(SEMI) }]);
              $.SUBRULE2($.clauseItem, { ARGS: [false] });
            });
          },
        },
      ]);
    });

    $.RULE('clauseItem', (allowMsgDefault) => {
      $.OR([
        {
          GATE: () => tokenMatcher($.LA(1), NAME) && tokenMatcher($.LA(2), EQ),
          ALT: () => $.SUBRULE($.assignItem, { ARGS: [!!allowMsgDefault] }),
        },
        { ALT: () => $.SUBRULE($.callList) },
      ]);
    });

    $.RULE('assignItem', (allowMsgDefault) => {
      const labelTok = $.CONSUME(NAME);
      $.CONSUME(EQ);
      $.OR([
        {
          GATE: () => this.lookaheadIsValueThenTerminator(),
          ALT: () => {
            $.SUBRULE($.value);
            $.OPTION({
              GATE: () => allowMsgDefault && lc(labelTok) === 'msg' && tokenMatcher($.LA(1), COMMA),
              DEF: () => { $.CONSUME(COMMA); $.SUBRULE($.callList); },
            });
          },
        },
        { ALT: () => $.SUBRULE($.orExpr) },
      ]);
    });

    $.RULE('callList', () => {
      $.SUBRULE($.call);
      $.MANY({
        GATE: () => tokenMatcher($.LA(1), COMMA)
          && !(tokenMatcher($.LA(2), NAME) && tokenMatcher($.LA(3), EQ)),
        DEF: () => { $.CONSUME(COMMA); $.SUBRULE2($.call); },
      });
    });

    // ── statement forms ─────────────────────────────────────────────────
    // `preambleItemList`/`doItemList` are thin wrapper rules around the
    // SAME `itemList` production (grammatically identical) — needed ONLY
    // so each ends up under its OWN CST key (`ctx.preambleItemList` vs
    // `ctx.doItemList`); Chevrotain's numeric SUBRULE suffixes (SUBRULE/
    // SUBRULE2/...) disambiguate for LOOKAHEAD/static-analysis purposes
    // only — they do NOT create separate CST child-array slots. Both
    // `$.SUBRULE($.itemList)` and `$.SUBRULE2($.itemList)` would collect
    // into the SAME `ctx.itemList` array, in actual-parse order — which
    // breaks positional indexing the moment the OPTIONAL preamble is
    // absent (the one itemList that fires lands at index 0 either way).
    $.RULE('preambleItemList', () => { $.SUBRULE($.itemList, { ARGS: [false] }); });
    $.RULE('doItemList', () => { $.SUBRULE($.itemList, { ARGS: [false] }); });

    $.RULE('agentStmt', () => {
      $.OPTION({
        GATE: () => !isWord($.LA(1), 'when') && !isWord($.LA(1), 'do'),
        DEF: () => $.SUBRULE($.preambleItemList),
      });
      $.OPTION2({
        GATE: () => isWord($.LA(1), 'when'),
        DEF: () => { $.CONSUME(NAME); $.SUBRULE($.condition); },
      });
      $.OPTION3({
        GATE: () => isWord($.LA(1), 'do'),
        DEF: () => { $.CONSUME2(NAME); $.SUBRULE($.doItemList); },
      });
    });

    $.RULE('orderStmt', () => {
      $.SUBRULE($.callList);
    });

    $.RULE('bracketStmt', () => { $.SUBRULE($.call); });
    $.RULE('helpStmt', () => { $.SUBRULE($.call); });
    $.RULE('cancelAllOrdersStmt', () => { $.SUBRULE($.call); });

    $.RULE('tokenStmt', () => {
      $.SUBRULE($.call);
      $.OR([
        {
          GATE: () => this.tokenStmtBodyIsCondition(),
          ALT: () => $.SUBRULE($.condition),
        },
        { ALT: () => $.SUBRULE($.callList) },
      ]);
    });

    this.performSelfAnalysis();
  }

  // ── Custom (non-grammar-table) lookahead helpers — plain LA() peeking,
  //    same spirit as the hand-written parser's own ad-hoc 1–3 token
  //    lookahead (e.g. `_isComparatorStart`, `_isClauseItemTerminator`).
  isComparatorStart() {
    const t = this.LA(1);
    if (COMPARATOR_MAP[t.tokenType?.name]) return true;
    if (tokenMatcher(t, EQ)) return true;
    return tokenMatcher(t, NAME) && (lc(t) === 'in' || lc(t) === 'not');
  }

  /** Mirrors `_isClauseItemTerminator` (minus `nlBefore` — newline-as-
   *  separator is a documented, NOT-implemented gap, see agentsLexer.js). */
  isTerminatorTok(t) {
    if (tokenMatcher(t, EOF) || tokenMatcher(t, SEMI) || tokenMatcher(t, COMMA) || tokenMatcher(t, RPAREN)) return true;
    return tokenMatcher(t, NAME) && (lc(t) === 'when' || lc(t) === 'alert' || lc(t) === 'do');
  }

  /** Mirrors `parseGivenRhs`'s first branch: does a bare `value` parse
   *  starting at LA(1) look like it's immediately followed by a clause-item
   *  terminator? Plain token-array lookahead, no backtracking machinery
   *  needed — `value`'s own shapes (NUMBER/STRING/bare-NAME/list_literal)
   *  are trivially boundable by direct bracket-depth counting. */
  lookaheadIsValueThenTerminator() {
    const t1 = this.LA(1);
    let endK;
    if (tokenMatcher(t1, NUMBER) || tokenMatcher(t1, STRING) || tokenMatcher(t1, NAME)) {
      endK = 1;
    } else if (tokenMatcher(t1, LBRACKET)) {
      let depth = 0; let j = 1;
      for (;;) {
        const tj = this.LA(j);
        if (tokenMatcher(tj, EOF)) return false;
        if (tokenMatcher(tj, LBRACKET)) depth++;
        if (tokenMatcher(tj, RBRACKET)) { depth--; if (depth === 0) { endK = j; break; } }
        j++;
        if (j > 5000) return false;
      }
    } else {
      return false;
    }
    return this.isTerminatorTok(this.LA(endK + 1));
  }

  /** `token(kind=...)`'s body shape depends on its own `kind=` arg.
   *  Default `false` (notify/call_list body) — `_runTokenStmt` below
   *  overrides this per-call with a real raw-token peek of the head
   *  call's own `kind=` arg before invoking the rule (parse-only; real
   *  `kind=` validation stays entirely in `compileTokenStmt`, untouched). */
  tokenStmtBodyIsCondition() {
    return false;
  }
}

const parserInstance = new AgentsCstParser();

// ═══════════════════════════════════════════════════════════════════════
// CST → AST visitor
// ═══════════════════════════════════════════════════════════════════════

const BaseVisitor = parserInstance.getBaseCstVisitorConstructor();

function pos(tok) { return { start: tok.startOffset, end: tok.endOffset + 1 }; }

class AgentsVisitor extends BaseVisitor {
  constructor() { super(); this.validateVisitor(); }

  // `opts.asCalls` — threaded down from `argEntry` ONLY for the `notify=`
  // key (see that method). A bare NAME list ITEM is, by default, a plain
  // ValueNode (`{type:'name', value}` — matches old `parseValue`'s own
  // list-item shape, used for e.g. `side in [BUY, SELL]`). `notify=[...]`
  // is the one old-grammar exception whose items are CallNodes (channel
  // calls, each optionally carrying its own `(...)` params) — see
  // `_parseNotifyListArg` in agents.js. A call WITH args/parens (not a
  // valid old plain-list item at all — old's `parseValue` has no
  // call-with-parens branch) is left as a CallNode either way; that shape
  // is an old-invalid input, so it isn't a differential-parity concern —
  // see agentsLexer.js's header comment on the deliberate call-arg
  // widening.
  value(ctx, opts) {
    if (ctx.NUMBER) return { type: 'number', value: Number(ctx.NUMBER[0].image) };
    if (ctx.STRING) return { type: 'string', value: _unquote(ctx.STRING[0].image) };
    if (ctx.objectLiteral) return this.visit(ctx.objectLiteral[0]);
    if (ctx.listLiteral) return this.visit(ctx.listLiteral[0], opts);
    if (ctx.NAME) return { type: 'name', value: ctx.NAME[0].image };
    throw new Error('unreachable: value');
  }

  listLiteral(ctx, opts) {
    return { type: 'list', value: (ctx.listItem || []).map((n) => this.visit(n, opts)) };
  }

  listItem(ctx, opts) {
    if (ctx.objectLiteral) return this.visit(ctx.objectLiteral[0]);
    if (ctx.NUMBER) return { type: 'number', value: Number(ctx.NUMBER[0].image) };
    if (ctx.STRING) return { type: 'string', value: _unquote(ctx.STRING[0].image) };
    if (ctx.call) {
      const callNode = this.visit(ctx.call[0]);
      if (opts && opts.asCalls) return callNode;
      return callNode.hasCall ? callNode : { type: 'name', value: callNode.name };
    }
    throw new Error('unreachable: listItem');
  }

  objectLiteral(ctx) {
    const fields = {};
    (ctx.objField || []).forEach((n) => {
      const { key, value } = this.visit(n);
      fields[key] = value;
    });
    return { type: 'object', value: fields };
  }

  objField(ctx) {
    return { key: ctx.NAME[0].image, value: this.visit(ctx.objFieldValue[0]) };
  }

  objFieldValue(ctx) {
    if (ctx.NUMBER) return { type: 'number', value: Number(ctx.NUMBER[0].image) };
    if (ctx.STRING) return { type: 'string', value: _unquote(ctx.STRING[0].image) };
    if (ctx.objectLiteral) return this.visit(ctx.objectLiteral[0]);
    if (ctx.NAME) return { type: 'name', value: ctx.NAME[0].image };
    throw new Error('unreachable: objFieldValue');
  }

  call(ctx) {
    const nameTok = ctx.NAME[0];
    const hasCall = !!ctx.LPAREN;
    let args = [];
    let _alertBody = undefined;
    const lname = lc(nameTok);
    if (hasCall) {
      if (lname === 'alert') {
        _alertBody = this.visit(ctx.itemList[0]).items;
      } else if (lname === 'debug' || lname === 'help') {
        const argTok = ctx.NAME[1];
        args = [{
          key: lname === 'debug' ? 'x' : 'name',
          keyStart: argTok.startOffset,
          value: { type: 'name', value: argTok.image, start: argTok.startOffset, end: argTok.endOffset + 1 },
        }];
      } else if (ctx.argList) {
        args = this.visit(ctx.argList[0]);
      }
      if (!_alertBody && args.length === 0 && !ctx.argList) {
        throw new _NewParseError(`"${nameTok.image}()" is a parse error — a zero-argument token is always written bare`, pos(nameTok));
      }
    }
    if (RESERVED.has(lc(nameTok))) {
      throw new _NewParseError(`"${nameTok.image}" is a reserved word and cannot be used as a token name`, pos(nameTok));
    }
    const endTok = ctx.RPAREN ? ctx.RPAREN[0] : nameTok;
    const node = { type: 'call', name: nameTok.image, args, hasCall, start: nameTok.startOffset, end: endTok.endOffset + 1 };
    if (_alertBody !== undefined) node._alertBody = _alertBody;
    return node;
  }

  argList(ctx) {
    return (ctx.argEntry || []).map((n) => this.visit(n));
  }

  argEntry(ctx) {
    const key = ctx.NAME[0].image;
    const opts = key.toLowerCase() === 'notify' ? { asCalls: true } : undefined;
    return { key, keyStart: ctx.NAME[0].startOffset, value: this.visit(ctx.value[0], opts) };
  }

  scopeRef(ctx) {
    return { type: 'scope', segments: (ctx.scopeSeg || []).map((n) => this.visit(n)) };
  }

  scopeSeg(ctx) {
    if (ctx.qualifierSeg) return this.visit(ctx.qualifierSeg[0]);
    return this.visit(ctx.call[0]);
  }

  qualifierSeg(ctx) {
    const keyTok = ctx.NAME[0];
    let value;
    if (ctx.STAR) value = { type: 'star' };
    else if (ctx.STRING) value = { type: 'string', value: _unquote(ctx.STRING[0].image) };
    else value = { type: 'name', value: ctx.NAME[1].image };
    const endTok = ctx.STAR ? ctx.STAR[0] : (ctx.STRING ? ctx.STRING[0] : ctx.NAME[1]);
    return { type: 'qualifier', key: keyTok.image, value, start: keyTok.startOffset, end: endTok.endOffset + 1 };
  }

  condition(ctx) {
    if (ctx.NAME) return { type: 'always' };
    return this.visit(ctx.orExpr[0]);
  }

  orExpr(ctx) {
    const parts = (ctx.andExpr || []).map((n) => this.visit(n));
    return parts.reduce((left, right) => (left == null ? right : { type: 'or', left, right }), null);
  }

  andExpr(ctx) {
    const parts = (ctx.term || []).map((n) => this.visit(n));
    return parts.reduce((left, right) => (left == null ? right : { type: 'and', left, right }), null);
  }

  term(ctx) {
    const inner = this.visit(ctx.primary[0]);
    const negated = !!ctx.TILDE || !!ctx.NAME;
    return negated ? { type: 'not', term: inner } : inner;
  }

  primary(ctx) {
    if (ctx.orExpr) return this.visit(ctx.orExpr[0]);
    return this.visit(ctx.leaf[0]);
  }

  comparator(ctx) {
    for (const key of Object.keys(COMPARATOR_MAP)) {
      if (ctx[key]) return COMPARATOR_MAP[key];
    }
    if (ctx.EQ) return '__EQ__'; // caller must reject — mirrors old parseComparator's EQ rejection
    if (ctx.NAME && ctx.NAME.length === 2) return 'not_in';
    if (ctx.NAME && ctx.NAME.length === 1) return 'in';
    throw new Error('unreachable: comparator');
  }

  metricOrColRef(ctx) {
    const metric = this.visit(ctx.call[0]);
    if (ctx.scopeSeg) {
      return { kind: 'colref', segments: [metric, ...ctx.scopeSeg.map((n) => this.visit(n))] };
    }
    return { kind: 'metric', metric, scope: this.visit(ctx.scopeRef[0]) };
  }

  leaf(ctx) {
    if (ctx.comparator && ctx.comparator.length === 2 && ctx.value && ctx.value.length === 2) {
      // between-chain
      const value1 = this.visit(ctx.value[0]);
      const op1 = this.visit(ctx.comparator[0]);
      const ref = this.visit(ctx.metricOrColRef[0]);
      const op2 = this.visit(ctx.comparator[1]);
      const value2 = this.visit(ctx.value[1]);
      if (op1 === '__EQ__' || op2 === '__EQ__') throw new _NewParseError('expected a comparator — use "==" for equality, not "="', null);
      if (ref.kind === 'colref') return { type: 'between', value1, op1, colSegments: ref.segments, op2, value2 };
      return { type: 'between', value1, op1, metric: ref.metric, scope: ref.scope, op2, value2 };
    }
    const firstCall = this.visit(ctx.call[0]);
    if (ctx.scopeSeg) {
      const segments = [firstCall, ...ctx.scopeSeg.map((n) => this.visit(n))];
      if (ctx.comparator) {
        const op = this.visit(ctx.comparator[0]);
        const value = this.visit(ctx.value[0]);
        if (op === '__EQ__') throw new _NewParseError('expected a comparator — use "==" for equality, not "="', null);
        return { type: 'colref', segments, op, value };
      }
      return { type: 'colref', segments, op: null, value: null };
    }
    if (ctx.scopeRef) {
      const scope = this.visit(ctx.scopeRef[0]);
      if (ctx.comparator) {
        const op = this.visit(ctx.comparator[0]);
        const value = this.visit(ctx.value[0]);
        if (op === '__EQ__') throw new _NewParseError('expected a comparator — use "==" for equality, not "="', null);
        return { type: 'leaf', metric: firstCall, scope, op, value };
      }
      return { type: 'leaf', metric: firstCall, scope, op: null, value: null };
    }
    return { type: 'ref', call: firstCall };
  }

  itemList(ctx) {
    if (ctx.NAME && !ctx.clauseItem) return { items: [{ type: 'nop' }] };
    return { items: (ctx.clauseItem || []).map((n) => this.visit(n)) };
  }

  clauseItem(ctx) {
    if (ctx.assignItem) return this.visit(ctx.assignItem[0]);
    return { type: 'calls', calls: this.visit(ctx.callList[0]) };
  }

  assignItem(ctx) {
    // Matches old `parseOneClauseItem`'s two possible return shapes:
    //   - the ordinary assign-branch (`{ type: 'assign', label }` — the
    //     RHS itself is consumed only for the side effect of registering a
    //     label binding, never carried in the returned node; see
    //     `_flattenClauseItems`, which drops every 'assign' item outright.
    //     Label-substitution semantics (Sprint G) are a documented Phase-1
    //     gap — not implemented here — but the shape must still match for
    //     every statement whose `alert(...)` body, the one place a raw,
    //     unflattened item list is exposed in the compiled AST, contains
    //     a plain assignment).
    //   - the Sprint-6 `msg="...", <call_list>` default-sugar branch
    //     (`{ type: 'msgDefaultCalls', msgNode, calls }`), active only when
    //     `allowMsgDefault` (threaded down from `itemList`) gated the
    //     optional trailing `, <call_list>` in the grammar rule above.
    const labelTok = ctx.NAME[0];
    // Mirrors `parseOneClauseItem`'s own leading check: an event_stmt-only
    // reserved kwarg combined with WHEN/ALERT/DO (i.e. reached via THIS
    // clause-item grammar, not the standalone bare-ALERT event_stmt) is a
    // clear, specific rejection — checked in the visitor (not the grammar
    // RULE body) so it never runs during Chevrotain's self-analysis pass.
    if (EVENT_META_KEYS.has(lc(labelTok))) {
      throw new _NewParseError(
        `'${lc(labelTok)}=' is only valid on a bare ALERT-only statement, not combined with WHEN/DO`,
        pos(labelTok),
      );
    }
    const msgNode = ctx.value ? this.visit(ctx.value[0]) : this.visit(ctx.orExpr[0]);
    if (ctx.callList) return { type: 'msgDefaultCalls', msgNode, calls: this.visit(ctx.callList[0]) };
    return { type: 'assign', label: labelTok.image };
  }

  callList(ctx) {
    return (ctx.call || []).map((n) => this.visit(n));
  }

  preambleItemList(ctx) { return this.visit(ctx.itemList[0]); }
  doItemList(ctx) { return this.visit(ctx.itemList[0]); }

  agentStmt(ctx) {
    const flat = (items) => {
      const calls = [];
      for (const it of items) {
        if (it.type === 'nop') return 'nop';
        if (it.type === 'calls') calls.push(...it.calls);
      }
      return calls.length ? calls : 'nop';
    };
    let preambleCalls = [];
    if (ctx.preambleItemList) {
      const f = flat(this.visit(ctx.preambleItemList[0]).items);
      preambleCalls = f === 'nop' ? [] : f;
    }
    const sawWhen = !!ctx.condition;
    const condition = sawWhen ? this.visit(ctx.condition[0]) : { type: 'always' };
    const sawDo = !!ctx.doItemList;
    const doCalls = sawDo ? flat(this.visit(ctx.doItemList[0]).items) : 'nop';
    // `bindings` is always `[]` here — Sprint-G label-binding tracking
    // (`state.bindings`/`state.allBindings`) is a documented, NOT-
    // implemented gap in this parser (see the COVERED/NOT-COVERED list
    // above). Emitting a real (always-empty) field, rather than omitting
    // the key, is deliberate: the differential harness's `normalize()`
    // does NOT strip `bindings`/`_bindingsAt`, so any statement containing
    // a real label assignment correctly surfaces as an AST diff instead of
    // silently passing — see the harness's own header comment.
    return { kind: 'agent', preambleCalls, condition, doCalls, sawWhen, sawDo, bindings: [] };
  }

  orderStmt(ctx) { return { kind: 'order', calls: this.visit(ctx.callList[0]) }; }
  bracketStmt(ctx) { return { kind: 'bracket', call: this.visit(ctx.call[0]) }; }
  helpStmt(ctx) { return { kind: 'help', call: this.visit(ctx.call[0]) }; }
  cancelAllOrdersStmt(ctx) { return { kind: 'cancel_all_orders', call: this.visit(ctx.call[0]) }; }

  tokenStmt(ctx) {
    const head = this.visit(ctx.call[0]);
    if (ctx.condition) return { kind: 'token', head, condition: this.visit(ctx.condition[0]) };
    return { kind: 'token', head, bodyCalls: this.visit(ctx.callList[0]) };
  }
}

function _unquote(image) { return image.slice(1, -1); }

class _NewParseError extends Error {
  constructor(message, position) { super(message); this.position = position; }
}

const visitor = new AgentsVisitor();

// ═══════════════════════════════════════════════════════════════════════
// Top-level dispatch — mirrors `_dispatchStatement`'s structure (see
// agents.js) for the subset of statement forms this file covers.
//
// COVERED: agent_stmt (preamble + WHEN/DO, label assignment, nested
//   alert(...) call-list body), order_stmt, bracket_stmt, help_stmt,
//   cancel_all_orders_stmt, token_stmt (notify-body and condition-body),
//   the full condition grammar (leaf/between/colref/and/or/not/parens/
//   always), scope_ref qualifiers, unified list/object call-arg values,
//   meta clause (#name=..., scanned out-of-band — see below).
//
// NOT COVERED (documented gaps — see agentsLexer.js's header comment and
//   the differential-harness report for the exact corpus impact):
//   - event_stmt (bare `ALERT log_tag=...`/`where=`/`match=`)
//   - GIVEN-rhs arithmetic/string-concat expressions
//   - newline-as-separator
//   - the Sprint-6 `msg=...,` ALERT-clause default sugar
//   - Sprint G's bound-label re-attachment inside `parseLeaf`
// ═══════════════════════════════════════════════════════════════════════

const _MAX_STATEMENT_LEN = 4000;
const _MAX_DEPTH = 20;

/** Quote/paren/bracket/brace-aware scan for the first TOP-LEVEL `#` — the
 *  meta clause boundary. Mirrors `_splitTopLevelArgCommas`'s depth-tracking
 *  style. Returns `text.length` (no meta) if none found. */
function findTopLevelMetaStart(text) {
  let depth = 0; let inQuote = null;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inQuote) { if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; continue; }
    if (ch === '(' || ch === '[' || ch === '{') { depth++; continue; }
    if (ch === ')' || ch === ']' || ch === '}') { depth--; continue; }
    if (ch === '#' && depth === 0) return i;
  }
  return text.length;
}

function isWordChar(ch) { return /[A-Za-z0-9_]/.test(ch); }
function isWordStart(ch) { return /[A-Za-z_]/.test(ch); }

/** Hand-written meta-clause scanner, deliberately out-of-band from the
 *  Chevrotain token stream — mirrors the OLD lexer's own `#`-specific
 *  branch inside `lexAgentStatement` (also hand-scanned, not a generic
 *  NAME/value grammar production — see agentsLexer.js's header comment). */
function scanMetaClause(text) {
  const items = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    while (i < n && /\s/.test(text[i])) i++;
    if (i >= n) break;
    if (text[i] !== '#') throw new _NewParseError(`unexpected character '${text[i]}'`, { start: i });
    const hashStart = i;
    i++;
    while (i < n && /\s/.test(text[i])) i++;
    if (!isWordStart(text[i] || '')) throw new _NewParseError('expected a meta key name after "#" (e.g. #name=...)', { start: i });
    const keyStart = i;
    while (i < n && isWordChar(text[i])) i++;
    const key = text.slice(keyStart, i);
    while (i < n && /\s/.test(text[i])) i++;
    if (text[i] !== '=') throw new _NewParseError('expected "=" after meta key', { start: i });
    i++;
    const values = [];
    for (;;) {
      while (i < n && /\s/.test(text[i])) i++;
      const vStart = i;
      if (text[i] === '"' || text[i] === "'") {
        const q = text[i]; i++;
        while (i < n && text[i] !== q) i++;
        if (i >= n) throw new _NewParseError('unterminated string literal in meta value', { start: vStart });
        i++;
        values.push({ type: 'string', value: text.slice(vStart + 1, i - 1) });
      } else {
        while (i < n && !/[\s,#="']/.test(text[i])) i++;
        if (i === vStart) throw new _NewParseError('expected a meta value after "="', { start: vStart });
        values.push({ type: 'bareword', value: text.slice(vStart, i) });
      }
      while (i < n && /\s/.test(text[i])) i++;
      if (text[i] === ',') { i++; continue; }
      break;
    }
    items.push({ key, values, start: hashStart });
  }
  return items;
}

function _lexCore(text) {
  const result = AgentsLexer.tokenize(text);
  if (result.errors.length) {
    const e = result.errors[0];
    throw new _NewParseError(`unexpected character '${text[e.offset]}'`, { start: e.offset });
  }
  return result.tokens;
}

function _checkGuards(text) {
  if (typeof text !== 'string') throw new _NewParseError('statement must be a string', null);
  if (text.length > _MAX_STATEMENT_LEN) throw new _NewParseError(`statement too long (${text.length} chars > ${_MAX_STATEMENT_LEN} cap)`, null);
  let depth = 0; let inQuote = null;
  for (const ch of text) {
    if (inQuote) { if (ch === inQuote) inQuote = null; continue; }
    if (ch === '"' || ch === "'") { inQuote = ch; continue; }
    if (ch === '(') { depth++; if (depth > _MAX_DEPTH) throw new _NewParseError(`nesting too deep (> ${_MAX_DEPTH})`, null); }
    else if (ch === ')') depth--;
  }
}

function _firstWord(tokens, i = 0) {
  const t = tokens[i];
  return t && t.tokenType === NAME ? t.image.toLowerCase() : null;
}

/** New Chevrotain-backed entry point. `{ ast, errors }` — same contract as
 *  `parseStatement`. See the COVERED/NOT-COVERED lists above. */
export function parseStatementNew(text) {
  try {
    _checkGuards(text);
    const metaStart = findTopLevelMetaStart(text);
    const coreText = text.slice(0, metaStart);
    const metaText = text.slice(metaStart);
    const tokens = _lexCore(coreText);

    const first = _firstWord(tokens, 0);
    const second = tokens[1];

    if (first === 'given') {
      throw new _NewParseError(
        'GIVEN is no longer part of the grammar — write the statement directly, e.g. "a = 1 when (a==1) do close_position(...)"',
        pos(tokens[0]),
      );
    }
    const firstIsBareAlert = first === 'alert' && !(second && (tokenMatcher(second, LPAREN) || tokenMatcher(second, EQ)));
    if (firstIsBareAlert) {
      const evSecond = tokens[1];
      const evThird = tokens[2];
      const isEventStmt = evSecond && evSecond.tokenType === NAME && EVENT_META_KEYS.has(evSecond.image.toLowerCase())
        && evThird && tokenMatcher(evThird, EQ);
      if (isEventStmt) {
        throw new _NewParseError('event_stmt is not implemented in the Chevrotain parser (Phase 1 known gap)', pos(tokens[0]));
      }
      throw new _NewParseError(
        'ALERT is no longer a reserved clause keyword — call it as an ordinary statement, e.g. alert(telegram)',
        pos(tokens[0]),
      );
    }

    if (first === 'token' && second && tokenMatcher(second, LPAREN)) {
      return _finish(() => _runTokenStmt(tokens), metaText);
    }
    if (first === 'cancel_all_orders' && second && tokenMatcher(second, LPAREN)) {
      return _finish(() => _run('cancelAllOrdersStmt', tokens), metaText);
    }
    if (first === 'when' || first === 'do') {
      return _finish(() => _run('agentStmt', tokens), metaText);
    }

    const isLeadingOrder = (first === 'order' || first === 'place_order') && second && tokenMatcher(second, LPAREN);
    const isLeadingBracket = first === 'bracket' && second && tokenMatcher(second, LPAREN);
    const isLeadingHelp = first === 'help' && second && tokenMatcher(second, LPAREN);
    if (isLeadingOrder || isLeadingBracket || isLeadingHelp) {
      try {
        const ruleName = isLeadingOrder ? 'orderStmt' : isLeadingBracket ? 'bracketStmt' : 'helpStmt';
        const { ast, leftover } = _tryRun(ruleName, tokens);
        if (leftover === 0) return _finish(() => ({ ast }), metaText);
      } catch (e) {
        if (!(e instanceof _NewParseError)) throw e;
      }
    }

    return _finish(() => _run('agentStmt', tokens), metaText);
  } catch (e) {
    if (e instanceof _NewParseError) return { ast: null, errors: [{ message: e.message, position: e.position }] };
    throw e;
  }
}

function _run(ruleName, tokens) {
  parserInstance.input = tokens;
  const cst = parserInstance[ruleName]();
  if (parserInstance.errors.length) {
    const err = parserInstance.errors[0];
    throw new _NewParseError(err.message, err.token ? pos(err.token) : null);
  }
  const leftover = parserInstance.input.length - parserInstance.currIdx - 1;
  const ast = visitor.visit(cst);
  return { ast, leftover };
}

function _tryRun(ruleName, tokens) {
  return _run(ruleName, tokens);
}

function _runTokenStmt(tokens) {
  // Peek the head call's args for `kind=condition` (string literal only —
  // mirrors the old parser's own permissive peek; full `_coerceValueForSpec`
  // catalog validation stays entirely in compileTokenStmt, untouched).
  parserInstance.input = tokens;
  // naive peek: scan head call's raw arg tokens for `kind` "=" STRING(condition)
  let kindIsCondition = false;
  let depth = 0;
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (tokenMatcher(t, LPAREN)) { depth++; continue; }
    if (tokenMatcher(t, RPAREN)) { depth--; if (depth === 0) break; continue; }
    if (depth === 1 && tokenMatcher(t, NAME) && t.image.toLowerCase() === 'kind') {
      const eq = tokens[i + 1];
      const val = tokens[i + 2];
      if (eq && tokenMatcher(eq, EQ) && val && tokenMatcher(val, NAME) && val.image.toLowerCase() === 'condition') {
        kindIsCondition = true;
      }
    }
  }
  parserInstance.tokenStmtBodyIsCondition = () => kindIsCondition;
  const { ast } = _run('tokenStmt', tokens);
  return { ast };
}

function _finish(runFn, metaText) {
  const { ast } = runFn();
  const meta = metaText ? scanMetaClause(metaText) : [];
  return { ast: { ...ast, meta }, errors: [] };
}
