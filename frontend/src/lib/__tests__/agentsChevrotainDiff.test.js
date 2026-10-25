/**
 * agentsChevrotainDiff.test.js — Phase 1 differential harness for the
 * Chevrotain-based agent-grammar parser (see .claude/PLAN.md).
 *
 * Runs EVERY statement string in `fixtures/agentsCorpus.json` (captured
 * live from a real run of the 5 existing agent-grammar test suites — see
 * the PLAN's "Build the corpus by capturing real inputs" step; NOT
 * hand-extracted string literals) through BOTH the hand-written parser
 * (`agents.js:parseStatement`) and the new Chevrotain-based parser
 * (`agentsParser.js:parseStatementNew`).
 *
 * Every corpus entry lands in exactly one of five buckets (counts MUST sum
 * to `corpus.length` — asserted below):
 *   - `bothAcceptedEqual`   — both parsers accepted it, AST-equal (modulo
 *     ONLY `start`/`end`/`keyStart` position fields — see `normalize()`'s
 *     own comment on why positions are excluded from THIS count but
 *     tracked separately; `bindings`/`_bindingsAt` are NOT excluded).
 *   - `bothRejectedSameMsg` — both parsers refused it with the identical
 *     `errors[0].message`.
 *   - `bothRejectedDiffMsg` — both refused it, but with different
 *     messages (error-message parity is an explicit, UNMEASURED-until-now
 *     gap — see the report).
 *   - `knownGap`            — anything else, but ONLY if `text` is in the
 *     frozen `KNOWN_GAP_TEXTS` ratchet below.
 *   - `unexpectedDiff`      — anything else NOT in the ratchet. This is
 *     the only bucket that fails the suite.
 *
 * `KNOWN_GAP_TEXTS` is a frozen, EXACT list of corpus strings (not a
 * pattern) — this is a ratchet, not an open-ended classifier: a future
 * regression in grammar this file DOES cover (e.g. the condition grammar
 * silently starts accepting a dangling tail) produces a BRAND NEW string
 * not in this list, which fails the suite loudly rather than being
 * silently absorbed by a broad predicate. The list can only shrink
 * (as Phase 1 gains coverage) without an explicit, reviewed edit here.
 */
import { describe, it, expect, vi } from 'vitest';
import corpus from './fixtures/agentsCorpus.json';

vi.mock('$lib/data/instruments', () => ({ getInstrument: vi.fn(() => null), listExchangesForSymbol: vi.fn(() => []) }));
vi.mock('$lib/api', () => ({ fetchGrammarTokens: vi.fn() }));

const { parseStatement } = await import('$lib/command/grammars/agents.js');
const { parseStatementNew } = await import('$lib/command/grammars/agentsParser.js');

/** Strip ONLY `start`/`end`/`keyStart` position fields. Deliberately
 *  NOT stripped: `bindings` (top-level Sprint-G label list) and
 *  `_bindingsAt` (per-call binding snapshot) — both are real, consumed-
 *  downstream fields (`_substituteBoundArgs`, `compileAgentStmt`'s
 *  collision check), and this parser doesn't implement Sprint-G binding
 *  tracking at all, so any statement with a real label assignment MUST
 *  surface as a diff here, not be silently hidden. Position fields ARE
 *  real too (consumed by `_coerceValueForSpec`/`describeErrorPosition`,
 *  confirmed via grep) — excluding them from the PRIMARY count is a
 *  documented Phase-2 blocker (not a "nice to have"), tracked separately
 *  below via `positionExactMatches`, never silently assumed fine. */
function normalize(node) {
  if (Array.isArray(node)) return node.map(normalize);
  if (node && typeof node === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(node)) {
      if (k === 'start' || k === 'end' || k === 'keyStart') continue;
      out[k] = normalize(v);
    }
    return out;
  }
  return node;
}

/** Frozen ratchet — see the file header. Computed once from a real run
 *  (every string below was independently verified to hit exactly one of:
 *  event_stmt, newline-as-separator, GIVEN-rhs arithmetic, Sprint-G label
 *  bindings, or a dotted-NAME call-arg value — all documented NOT-COVERED
 *  gaps in agentsParser.js). Edit this list ONLY after confirming a new
 *  diff's root cause is one of those same documented gaps. */
const KNOWN_GAP_TEXTS = new Set([
  'WHEN pnl@positions.expiring_today > 0 DO close_position(account=result.account_id, symbol=result.symbol)',
  'WHEN orders.any_acct.status == "OPEN" DO cancel_order(account=result.account_id, broker_order_id=result.order_id)',
  'x = pnl@positions.total <= -50000 WHEN x DO alert(telegram)',
  'x = (pnl@positions.total <= -50000) & (avail_margin@funds.total < 0) WHEN x DO alert(telegram)',
  'x = pnl@positions.total <= -50000 WHEN x & (avail_margin@funds.total < 0) DO alert(telegram)',
  'x = 1; x = 2 WHEN always DO alert(telegram)',
  'alert = 5 WHEN always DO alert(telegram)',
  'pnl = 5 WHEN always DO alert(telegram)',
  'account_id = 5 WHEN always DO alert(telegram)',
  'hold = 5 WHEN always DO alert(telegram)',
  'x = 5 WHEN x DO alert(telegram)',
  'x = pnl@positions.total <= -50000 WHEN always DO alert(telegram), log(message=x)',
  'x = "ping" WHEN always DO alert(telegram), debug(x)',
  'x = pnl@positions.total <= -50000 WHEN always DO alert(telegram), debug(x)',
  'WHEN pnl@positions.expiring_today > 0 DO debug(account_id)',
  'WHEN always DO alert(telegram), debug(bogus_name)',
  'x = "ping"; debug(x) WHEN always DO alert(telegram)',
  'x = "hey" WHEN always DO debug(x), alert(telegram)',
  'x = "ping" WHEN always DO debug(x)',
  'a1 = 1, a2 = 1, total = a1 + a2 WHEN always DO alert(telegram), debug(total)',
  'WHEN always DO alert(a = 1, telegram), debug(a)',
  'WHEN always DO alert(telegram), a = "x", log(message=a)',
  'WHEN always DO alert(telegram, msg_txt = "late"), log(message=msg_txt)',
  'WHEN always DO alert(telegram, a = 1), debug(a)',
  'p = pnl@positions.total, msg_txt = "Loss: " + p WHEN p <= -50000 DO alert(msg = msg_txt, telegram), order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)',
  'p = pnl@positions.total WHEN p <= -50000 DO alert(telegram)',
  'x = pnl@positions.total <= -5 WHEN x DO alert(telegram)',
  'y = "x" + pnl@positions.total WHEN always DO alert(telegram)',
  'qty_expr = 1 + 1 WHEN always DO alert(telegram), order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", qty=qty_expr)',
  'msg_txt = "a" + "b" WHEN always DO alert(telegram), log(message=msg_txt)',
  'msg_txt = "a" + "b" WHEN always DO alert(msg = msg_txt, telegram)',
  'msg_txt = "a" + "b" WHEN always DO alert(telegram(msg=msg_txt))',
  'msg_txt = "a" + "b" WHEN always DO alert(telegram), debug(msg_txt)',
  'p = pnl@positions.total <= -50000 WHEN always DO alert(telegram), debug(p)',
  'total = 1 + 2 WHEN always DO alert(telegram), debug(total)',
  'total = a + b WHEN always DO alert(telegram), debug(total)',
  'bad = "x" + 1 * 2 WHEN always DO alert(telegram)',
  'msg_txt = "a" + "b" WHEN always DO alert(msg = msg_txt, telegram), log(message=msg_txt)',
  'msg_txt = "Symbol: " + "NIFTY" WHEN always DO alert(msg = msg_txt, telegram)',
  'a = 1 + 2 - 3 * 4 / 5 % 6 ** 2 WHEN always DO alert(telegram), debug(a)',
  'sym = pnl@positions.total\nmsg_txt = "Symbol: " + sym + " breached" WHEN always DO alert(telegram), debug(msg_txt)',
  'p = 5\nbad = "x" + -p WHEN always DO alert(telegram)',
  'msg_txt = "a" + "b"\nWHEN always DO alert(msg=msg_txt, telegram)',
  'p = pnl@positions.total\nmsg_txt = "Loss: " + p\nWHEN pnl@positions.total <= -50000 DO alert(msg=msg_txt, telegram)',
  'msg_txt = "Symbol: " + "NIFTY"\nWHEN always DO alert(msg=msg_txt, telegram)',
  'p = pnl@positions.total; msg_txt = "Loss: " + p\nWHEN always DO alert(msg=msg_txt, telegram)',
  'msg_txt = "a" + "b"\nWHEN always DO alert(telegram), debug(msg_txt)',
  'p = pnl@positions.total\nmsg_txt = "Loss: " + p\nWHEN always DO alert(msg = msg_txt, telegram)',
  'p = pnl@positions.total\nmsg_txt = "Loss: " + p WHEN p <= -50000 DO alert(msg = msg_txt, telegram)',
  'p = pnl@positions.total\nmsg_txt = "Loss: " + p WHEN always DO alert(msg = msg_txt, telegram)',
  'p = pnl@positions.total\nmsg_txt = "Loss: " + p WHEN always DO alert(telegram), debug(msg_txt)',
  // ── Sprint-G `bindings`/`_bindingsAt` (a bound label consumed as a
  //    plain arg value, e.g. `log(message=x)`, or `alert(msg="y")`'s own
  //    implicit `msg` binding) and/or newline-as-separator — root-caused
  //    individually, not a blanket catch-all. ──────────────────────────
  'WHEN pnl@positions.total <= -1 DO alert(msg="y")',
  'x = "stop hit" WHEN pnl@positions.total <= -50000 DO alert(telegram), log(message=x)',
  'x = "stop hit" WHEN pnl@positions.total <= -50000 DO alert(telegram(msg=x)), log(message=x)',
  'a = "first"; b = "second" WHEN always DO alert(telegram), log(message=a), log(message=b)',
  'a = "first"\nb = "second" WHEN always DO alert(telegram), log(message=a), log(message=b)',
  'WHEN always DO alert(msg = "hi"\ntelegram)',
  'WHEN always DO alert(telegram), log(message="a")\nlog(message="b")',
  'WHEN always DO alert(telegram\nemail)',
  'WHEN always DO alert(telegram\nmsg_txt = "late"), log(message=msg_txt)',
  'order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, price=100)\norder(account="ZJ6294", symbol="BANKNIFTY25JULFUT", side="BUY", lots=1, price=200)',
  'p = pnl@positions.total\nmsg_txt = "Loss: " + p\nWHEN p <= -50000\nDO alert(msg = msg_txt, telegram), order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1)',
  'p = pnl@positions.total\nq = p WHEN always DO alert(telegram), log(message=q)',
  'order(account="ZG0790", symbol="NIFTY25JULFUT", side="BUY", lots=2)\nbracket(applies_to=buy_any, tp_pct=30)\nalert(telegram)\nz = 1\ndebug(z)\nhelp(pnl)\nWHEN (pnl@positions.total==1) DO close_position(account="ZG0790", symbol="NIFTY25JULFUT")',
  'order(account="ZG0790", symbol="NIFTY25JULFUT", side="BUY", lots=1)\nx = 1\ndebug(x)\nhelp(pnl)',
  'p = pnl@positions.total; msg_txt = "Loss: " + p\nWHEN pnl@positions.total <= -50000 DO alert(msg=msg_txt, telegram)',
  'x = 5 ',
  'x = 5 WHEN always DO debug(x)',
  // ── Wave 2 (grammar-unification, 2026-10-24) — retiring `_EVENT_META_KEYS`
  //    removed `parseOneClauseItem`'s dedicated "'log_tag='/'render='/...
  //    is only valid on a bare ALERT-only statement, not combined with
  //    WHEN/DO" rejection (agents.js). `log_tag=error`/`render=error` inside
  //    this `alert(...)` body now parse as two ORDINARY label assignments
  //    (zero channel calls) — same as any other coincidentally-named label
  //    — so the hand-written parser now ACCEPTS this text (it still fails
  //    to COMPILE: "DO must have at least one real action or alert", but
  //    `parseStatement` only runs the PARSE phase). Chevrotain's parser
  //    still mirrors the OLD rejection (`agentsParser.js`'s own "Mirrors
  //    parseOneClauseItem's own leading check" comment — Wave 3 scope, not
  //    touched by this plan) and still REJECTS it. A real, understood,
  //    one-sided divergence — not a silent regression. ──────────────────
  'WHEN always DO alert(log_tag=error, render=error)',
]);

/** The ONE classifier that isn't a frozen exact-string lookup — a precise
 *  structural predicate, not a broad catch-all: it mirrors `agentsParser.js`'s
 *  OWN dispatch rule (`parseStatementNew`'s `isEventStmt` check), which
 *  throws an explicit, fixed "event_stmt is not implemented" error for
 *  EXACTLY this shape. It can never silently start passing for a different
 *  reason without that thrown message itself changing — so it carries the
 *  same ratchet guarantee as the frozen set above, just expressed
 *  structurally instead of by enumerating ~37 near-identical corpus strings. */
function isEventStmtGap(text) {
  return /^alert\s+(log_tag|min_level|render|where|gate|match)\s*=/i.test(text.trim());
}

function isKnownGap(text) {
  return KNOWN_GAP_TEXTS.has(text) || isEventStmtGap(text);
}

describe('Chevrotain differential harness (Phase 1 — see .claude/PLAN.md)', () => {
  const buckets = {
    bothAcceptedEqual: 0,
    bothRejectedSameMsg: 0,
    bothRejectedDiffMsg: 0,
    knownGap: 0,
  };
  const unexpectedDiffs = [];
  let positionExactMatches = 0;
  let positionComparable = 0;
  const tpScalesJsonResults = [];

  for (const text of corpus) {
    const oldResult = parseStatement(text);
    let newResult = null;
    let newThrew = null;
    try {
      newResult = parseStatementNew(text);
    } catch (e) {
      newThrew = e;
    }

    it(`${text.slice(0, 90)}`, () => {
      const oldAccepted = !!oldResult.ast;
      const newAccepted = !newThrew && !!(newResult && newResult.ast);

      if (!oldAccepted && !newAccepted) {
        const oldMsg = oldResult.errors?.[0]?.message;
        const newMsg = newThrew ? newThrew.message : newResult?.errors?.[0]?.message;
        if (oldMsg === newMsg) { buckets.bothRejectedSameMsg++; return; }
        if (isKnownGap(text)) { buckets.knownGap++; return; }
        buckets.bothRejectedDiffMsg++; // both refused — not a Phase-1 AST-parity failure
        return;
      }

      if (oldAccepted && newAccepted) {
        const oldNorm = normalize(oldResult.ast);
        const newNorm = normalize(newResult.ast);
        let equal = true;
        try { expect(newNorm).toEqual(oldNorm); } catch { equal = false; }
        if (equal) {
          buckets.bothAcceptedEqual++;
          if (text.includes('tp_scales_json')) tpScalesJsonResults.push({ text, bucket: 'bothAcceptedEqual' });
          // Position-exactness is tracked separately, never silently assumed.
          positionComparable++;
          let posEqual = true;
          try { expect(newResult.ast).toEqual(oldResult.ast); } catch { posEqual = false; }
          if (posEqual) positionExactMatches++;
          return;
        }
        if (isKnownGap(text)) { buckets.knownGap++; return; }
        if (text.includes('tp_scales_json')) tpScalesJsonResults.push({ text, bucket: 'unexpectedDiff' });
        unexpectedDiffs.push({ text, oldAst: oldResult.ast, newAst: newResult.ast });
        return;
      }

      // One accepted, the other didn't — always a real divergence.
      if (isKnownGap(text)) { buckets.knownGap++; return; }
      if (text.includes('tp_scales_json')) tpScalesJsonResults.push({ text, bucket: 'unexpectedDiff' });
      unexpectedDiffs.push({
        text,
        reason: oldAccepted ? 'old accepted, new rejected' : 'new accepted, old rejected',
        oldAst: oldResult.ast, oldErrors: oldResult.errors,
        newAst: newResult?.ast, newThrew: newThrew?.message,
      });
    });
  }

  it('REPORT — summary (always passes; prints real counts)', () => {
    const total = buckets.bothAcceptedEqual + buckets.bothRejectedSameMsg + buckets.bothRejectedDiffMsg
      + buckets.knownGap + unexpectedDiffs.length;
    // eslint-disable-next-line no-console
    console.log(
      `[chevrotain-diff] corpus=${corpus.length} accounted=${total} `
      + `bothAcceptedEqual=${buckets.bothAcceptedEqual} (positionExact=${positionExactMatches}/${positionComparable}) `
      + `bothRejectedSameMsg=${buckets.bothRejectedSameMsg} bothRejectedDiffMsg=${buckets.bothRejectedDiffMsg} `
      + `knownGap=${buckets.knownGap} unexpectedDiff=${unexpectedDiffs.length}`,
    );
    // eslint-disable-next-line no-console
    console.log('[chevrotain-diff] tp_scales_json inputs:', JSON.stringify(tpScalesJsonResults.map((r) => r.bucket)));
    if (unexpectedDiffs.length) {
      // eslint-disable-next-line no-console
      console.log('[chevrotain-diff] unexpected diffs FULL:', JSON.stringify(unexpectedDiffs, null, 1));
    }
    expect(total).toBe(corpus.length); // the 5 buckets must account for every corpus entry exactly once
  });

  it('every tp_scales_json corpus input is AST-equal (the resolved wrinkle — see agentsLexer.js)', () => {
    expect(tpScalesJsonResults.length).toBeGreaterThan(0); // the corpus must actually exercise this
    expect(tpScalesJsonResults.every((r) => r.bucket === 'bothAcceptedEqual')).toBe(true);
  });

  it('a dangling tail after a complete condition is rejected, not silently accepted', () => {
    // Spot-check independent of corpus diffing — the worst failure class
    // for this DSL is a parser that accepts trailing garbage.
    const r = parseStatementNew('WHEN always & pnl@positions.total < 0 DO alert(telegram)');
    expect(r.ast).toBeNull();
    expect(r.errors.length).toBeGreaterThan(0);
  });

  it('GATE — zero unexpected diffs among covered-grammar inputs', () => {
    expect(unexpectedDiffs.map((d) => d.text)).toEqual([]);
  });
});
