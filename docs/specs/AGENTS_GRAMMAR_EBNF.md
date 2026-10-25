# Agents CLI Grammar — EBNF

Source of truth for the agent CLI grammar implemented in
[`frontend/src/lib/command/grammars/agents.js`](../../frontend/src/lib/command/grammars/agents.js).
When the grammar changes, update the implementation and this file in the same commit.
The prose companion is [AGENTS_SPEC.md §13](AGENTS_SPEC.md) (preamble execution, compiled
`preamble` JSON, operator semantics). A drift-guard test checks the reserved-word production
below against the code: `frontend/src/lib/__tests__/agentsGrammarEbnfDrift.test.js`.

## ⚠️ STATUS — read first

This document was rewritten 2026-10-24 to describe two layers at once.

- **Current (shipped).** Everything in this file is shipped **except** the event-agent shape
  marked `[target]`. The shipped grammar has no `GIVEN` keyword, no `ALERT` clause keyword, and
  one conditional wrapper, `WHEN … DO …`. Everything before `WHEN` (or the whole statement, if
  there is no `WHEN`) is an unconditional preamble. `alert`, `log`, `help`, `debug`, `order`,
  `bracket`, and label assignment are all ordinary items anywhere in the preamble or `DO`.
- **Target (NOT YET SHIPPED — Wave 2).** An event (`kind='event'`) agent is written as an ordinary
  `WHEN log(tag=…, min_level=…, where={…}) DO render(…)` statement. Its condition is a
  catalog-resolved bare call, and `|` replaces the old `match=[…]` list. The standalone
  `ALERT log_tag=… render=…` statement (`event_stmt`) is still live in `agents.js`
  (`_EVENT_META_KEYS` at line 84, `parseEventStmt`, `compileEventStmt`). Its productions and every
  semantic rule they carry are kept in
  [Appendix A](#appendix-a--retiring-event_stmt-live-until-wave-2),
  so Wave 2's byte-identical compile gate has a written reference. Delete Appendix A when Wave 2
  ships.

Wave 2 is the plan in `.claude/PLAN.md` (dissolve `event_stmt` into ordinary `WHEN … DO …`).
`[target]` tags mark every rule that depends on it.

---

## Grammar

```ebnf
(* ── Top level ───────────────────────────────────────────────────────────── *)
statement      := order_stmt | bracket_stmt | help_stmt | cancel_all_orders_stmt
                 | token_stmt | agent_stmt
                                                (* Dispatch order (`_dispatchStatement`): a leading
                                                   order/place_order, bracket, or help call is
                                                   tried FIRST as its exclusive standalone
                                                   statement and accepted only if it consumes the
                                                   whole input.
                                                   Otherwise the parse backtracks and falls through
                                                   to agent_stmt, which accepts the same call
                                                   vocabulary. token and cancel_all_orders have no
                                                   such fallback — they are standalone-only.
                                                   Event agents are agent_stmt too [target]. *)

(* ── Agent statement (shipped, except the event form noted below) ─────────── *)
agent_stmt     := preamble? ("WHEN" condition "DO" do_clause)? meta_item*
                                                (* WHEN and DO are one paired, optional wrapper.
                                                   WHEN without DO is a compile-time error.
                                                   DO without WHEN is a compile-time error.
                                                   A preamble with neither is a complete standalone
                                                   statement. DO nop alone is rejected. At least
                                                   one real call is required somewhere in the
                                                   statement. meta_item* is trailing-only. *)
preamble       := item_list                    (* Bounded by POSITION: everything before WHEN, or
                                                   the entire input if WHEN never appears. No
                                                   introducer keyword. A bare "nop" is rejected
                                                   here (allowNop=false). *)
do_clause      := "nop" | item_list           (* "nop" excludes every other item. *)
item_list      := item (("," | ";" | NEWLINE) item)*
                                                (* Separators: literal "," or ";", or a bare
                                                   newline before a token that starts a new
                                                   item. All three are equivalent. *)
item           := NAME "=" rhs | call_list     (* NAME "=" is a label assignment. It populates
                                                   state.bindings and emits no compiled entry. See
                                                   LABEL RULES below. *)
rhs            := value | arith_expr           (* arith_expr is the operator-expression RHS
                                                   (parseGivenRhs). Its grammar is not restated
                                                   here. See Known gaps. *)
call_list      := call ("," call)*

(* ── Standalone statements (each accepted ONLY as the whole statement) ─────── *)
order_stmt     := order_call ("," order_call)* meta_item*
                                                (* order / place_order only. Trailing meta_item is
                                                   parsed but is a COMPILE-time error. *)
bracket_stmt   := bracket_call meta_item*      (* Exactly ONE bracket(...) call. No basket form.
                                                   #name= is effectively required. *)
help_stmt      := help_call meta_item*         (* Exactly ONE help(<name>) call. Read-only.
                                                   Meta is a COMPILE-time error. *)
cancel_all_orders_stmt := cancel_all_orders_call meta_item*
                                                (* Exactly ONE cancel_all_orders(...) call. No
                                                   comma. Meta is a COMPILE-time error. *)
token_stmt     := token_head (condition_body | call_list) meta_item*
                                                (* token(...) only. head := token(kind=condition)
                                                   names a condition fragment, and its body is one
                                                   condition. head := token(kind=notify) is
                                                   followed by a channel call_list with NO comma
                                                   between head and body. kind=notify is the only
                                                   action kind supported on the notify path.
                                                   #name= is required. #tag= and #group= are
                                                   rejected. *)
token_head     := call                         (* call.name must be "token". *)
condition_body := condition

(* ── Built-in call shapes inside items (not new productions, shape notes) ─── *)
alert_call     := "alert" "(" ("nop" | item_list) ")"
                                                (* Ordinary call. Compiles through the same
                                                   channel-list machinery the old ALERT clause
                                                   used.
                                                   Allowed anywhere an item is allowed. Inside
                                                   alert(...) ONLY, a leading msg= sugar is
                                                   accepted: `msg=value, channel, …`. *)
order_call     := call                         (* name is order or place_order. *)
bracket_call   := call                         (* name is bracket. *)
help_call      := NAME "(" bare_word ")"       (* help(<name>). <name> is one bare word, same
                                                   lexer sugar debug(x) uses. Never quoted. *)
debug_call     := NAME "(" bare_word ")"       (* debug(x). Compiles to a log call with level
                                                   debug and message x. Same single-bare-word
                                                   sugar as help. *)
cancel_all_orders_call := call                 (* name is cancel_all_orders. *)

(* ── Metadata (trailing only) ────────────────────────────────────────────── *)
meta_item      := "#" NAME "=" meta_value ("," meta_value)*
                                                (* Trailing only. NAME is one of
                                                   name/desc/tag/group. An unknown key is a
                                                   COMPILE-time error, not a
                                                   parse error. *)
meta_value     := STRING | BARE_WORD
BARE_WORD      := (* an unquoted run of 1+ characters containing NONE of: whitespace, ",", "#",
                     "=", or a quote character. DELIBERATELY WIDER than NAME's word-char class:
                     "hedge-book" lexes as one BARE_WORD. Scanned by a dedicated "#" branch in
                     lexAgentStatement. *)

(* ── Condition tree (shipped) ────────────────────────────────────────────── *)
condition      := "always" | or_expr
or_expr        := term (("&" | "|") term)*     (* equal precedence, left-to-right.
                                                   a & b | c groups as (a & b) | c. *)
term           := "~" primary | primary
primary        := "(" or_expr ")" | leaf
leaf           := metric_ref comparator value
                 | col_ref comparator value
                 | value comparator (metric_ref | col_ref) comparator value   (* between *)
                 | call                        (* A BARE call with no "@" following. Parsed
                                                   permissively here and resolved at COMPILE
                                                   time against the catalog. If the name resolves
                                                   to an isFragmentRef entry, it compiles to
                                                   {"$ref": name}. A real metric missing its "@"
                                                   gets "expected '@' after metric" at compile
                                                   time. [target] A log-condition is ALSO a bare
                                                   call — see prose below. *)
metric_ref     := call "@" scope_ref
scope_ref      := scope_seg ("." scope_seg)*   (* scope_seg := call | qualifier_seg. A
                                                   qualifier_seg is trailing only. Short scope
                                                   aliases (pos/hld/fnd/wl) apply to the FIRST
                                                   call segment only. *)
scope_seg      := call | qualifier_seg
qualifier_seg  := NAME ":" qualifier_value    (* account:value / symbol:value. Compiles to a
                                                   leaf-level scope_filter dict, never folded into
                                                   the scope string. "*" and "worst" are reserved
                                                   values that alias onto the existing
                                                   <family>.any_acct / .worst_acct / .worst_symbol
                                                   scope tokens. *)
qualifier_value := "*" | NAME | STRING
col_ref        := call ("." call)+             (* A bare dotted chain with NO "@", e.g.
                                                   positions.total.pnl. Syntactically the same as
                                                   scope_ref, but semantically distinct. Which
                                                   trailing segment is the scope and which is the
                                                   column is decided at COMPILE time
                                                   (_resolveDotAccess): try the whole chain as a
                                                   known scope first, then fall back to "last
                                                   segment is a column on the scope so far". *)
comparator     := "<" | "<=" | ">" | ">=" | "==" | "!=" | "in" | "not in"

(* ── Values and calls ────────────────────────────────────────────────────── *)
value          := NUMBER | STRING | NAME | list_literal
list_literal   := "[" value ("," value)* "]"
call           := NAME | NAME "(" arg_list ")"
                                                (* A NAME in call position may contain internal
                                                   hyphens, mid-word only, so a seeded Token name
                                                   like notify-critical-trio lexes as ONE token.
                                                   This does NOT widen NAME elsewhere. Kwarg keys
                                                   are scanned by a separate tokenizer
                                                   (tokenizeCallStyle, engine.js). *)
arg_list       := kw_arg ("," kw_arg)*         (* EVERY argument is keyword, including the
                                                   first. No positional form exists. *)
kw_arg         := NAME "=" value
                                                (* [target] Wave 2 needs `where={…}` as a
                                                   log-condition argument. That requires widening
                                                   this production's value to include
                                                   object_literal, scoped to condition calls.
                                                   Not yet written. See Known gaps. *)

(* ── Object literals and the tp_scales_json exception ────────────────────── *)
object_literal := "{" obj_kwarg ("," obj_kwarg)* "}"
obj_kwarg      := NAME "=" value | NAME "=" object_literal
                                                (* NOT a general value. Reached only from
                                                   tp_scales_json (below), and from the retiring
                                                   event where=/match= (Appendix A). Reuses "=" as
                                                   the key/value separator. *)
tp_scales_json_value := "[" object_literal ("," object_literal)* "]"
                                                (* The ONLY legal value for a call argument named
                                                   tp_scales_json (case-insensitive), on order(...)
                                                   or bracket(...). Parsed by a dedicated
                                                   mini-lexer (_parseTpScalesJsonArg), not
                                                   tokenizeCallStyle. *)

(* ── Reserved words ──────────────────────────────────────────────────────── *)
(* Drift-guarded: frontend/src/lib/__tests__/agentsGrammarEbnfDrift.test.js asserts this set is
   exactly agents.js's RESERVED_WORDS (_RESERVED). Add a word here in the same commit that adds
   it to _RESERVED, and vice versa. Quoted words only, one per alternative. *)
reserved_word  := "when" | "do" | "nop" | "always" | "in" | "not" | "and" | "or"
                 | "true" | "false"
                                                (* NOT reserved: "given" (removed; a leading
                                                   given is a specific error), "alert" (an
                                                   ordinary call), "debug", "help", and "msg"
                                                   (contextual only, as NAME "=" inside
                                                   alert(...)). *)

(* ── Label rules (a separate set from reserved_word) ──────────────────────── *)
label_reserved := "debug" | "result" | "alert" | "given" | "help"
                                                (* _LABEL_RESERVED_WORDS. A label may NOT be
                                                   bound to any reserved_word OR label_reserved
                                                   word. Not drift-guarded: the set is not
                                                   exported. *)
```

### Event agents — target shape `[target]`

An event (`kind='event'`) agent is an ordinary `agent_stmt` whose condition is a log-condition
call and whose action is a `render(...)` call:

```
WHEN log(tag=error, min_level=WARNING) DO render(render=error, gate=true)
WHEN log(tag=orders, min_level=ERROR, where={channel="telegram"}) | log(tag=risk) DO render(...)
```

Prose rules for the target shape:

- **A log-condition is a catalog-resolved bare call.** `log(…)` in condition position has NO
  new production. It is the existing `leaf := … | call` alternative. Compile-time resolution
  finds the condition-kind `log` catalog row, the same way any other bare-call token reference
  resolves. The `log` name is resolved by POSITION: in a condition it means the log-stream
  filter, in an action (preamble or `DO`) it means the app-log action (see §Actions). Both rows
  share the name, the same way `help` reuses a name across token kinds.
- **`|` replaces `match=[…]`.** `log(…) | log(…)` is an ordinary `or_expr`. The OR-of-filters the
  retiring `match=` list encoded is the grammar's own `|` operator. No new production.
- **Mixing a log-condition with a metric or scope leaf in one `WHEN` is a COMPILE-time error.**
  The two use different dispatch loops and cannot coexist in one agent.
- **`&` between log-condition leaves is UNDEFINED.** Wave 2 must decide. Open question, not
  documented as behavior.
- **`min_level` is uppercased; defaults to `INFO`.** `tag`, `render`, and every value inside
  `where={…}` are byte-exact. They are never case-normalized, because `where` matches log
  `extra` fields by exact string equality server-side. See Appendix A for the full rule set.
- **`msg=` is rejected** on a log-condition or `render(...)`. `render(...)` owns the message.
- **`kind='event'` is set** when the condition resolves to a log-condition. The compiled
  `conditions` and `actions` JSON must be byte-identical to the retiring `event_stmt`'s compiled
  output for the same agent. `compute_seed_spec_hash()` hashes these fields.
- **`render(…)` is an `event_actions` entry, not a grammar token, today.** The render key and its
  `gate` sibling are declared under `event_actions.render` in
  [agent_grammar.yaml](../../backend/config/grammars/agent_grammar.yaml) (`gate` boolean, default
  false). Wave 2 must add a catalog row, or expose the `event_actions` entry through the generic
  call path. The argument spelling (`render(render=…, gate=…)`) is an assumption until then.

### Actions (ordinary call, shipped)

- **`log(level=…, message=…, priority=…, name=…, tags=…)`** — the app-log action, valid in the
  preamble or `DO`. Params per
  [agent_grammar.yaml](../../backend/config/grammars/agent_grammar.yaml) (`log` token,
  `action_type` kind):
  - `level` — enum, default `info`. The YAML enum lists `info`, `warning`, `error`, `critical`.
    `debug(x)` compiles to level `debug`, which the YAML enum does not list. See Known gaps.
  - `message` — required string.
  - `priority` — optional enum `low|default|high|urgent`. Routing hint only: a matching event
    agent's ntfy priority override.
  - `name` — optional, threaded to the log record as `extra["route_name"]`. NOT `extra["name"]`,
    which is a reserved `LogRecord` attribute.
  - `tags` — optional list, threaded to `extra["route_tags"]`. NOT `extra["tags"]`, which is the
    structural condition-match key (`log_store.tags_for()`).
- **`help(name)`** — read-only catalog lookup. Logged at DEBUG, never alert-eligible. Available on
  the Console tab only. Rejected on order-scoped CLI mounts (`allowedKinds`). Resolved by
  `help_lookup` in [actions.py](../../backend/api/algo/actions.py).
- **`alert(…)`** — see the call-shape note above. Compiles to `{type:'alert', events:[…]}`.

---

## Prose — how the pieces combine

- **Preamble vs `DO`.** Both use the same `item_list` and the same per-call compiler
  (`_compileSharedCall`). The position alone decides whether a call is gated by a condition.
  Execution semantics (every-cycle, once-per-activation, cooldown-windowed) are in
  [AGENTS_SPEC.md §13](AGENTS_SPEC.md) and in CLAUDE.md, "Saved cycle-agent preamble".
- **Label assignment** binds a NAME for later reads in the same statement. A label is never a
  compiled entry. Bindings are visible only to calls and conditions written AFTER the binding
  (`parseCall` snapshots `state.bindings` per call). A label may not be a reserved word or a
  `label_reserved` word.
- **Standalone statements do not mix.** `order`/`bracket`/`help` are standalone ONLY when they
  are the entire input. `token` and `cancel_all_orders` are standalone-only, with no preamble
  fallback. Anything else containing those names in a mixed list goes through `agent_stmt`.
- **`alert` with no parentheses** is no longer a clause keyword. A bare `ALERT` followed by an
  event-meta key (`ALERT log_tag=…`) still parses as the legacy event statement (retiring,
  Appendix A). A bare `ALERT` followed by anything else (`ALERT telegram DO …`) is a specific
  parse error naming the removed clause keyword. A bare `alert = …` is a label-reservation error.
- **Compiled JSON `preamble`.** The preamble compiles to a top-level `preamble` array on
  `kind:'agent'` rows (omitted when empty). Shape and execution are in AGENTS_SPEC §13.

---

## AST shape

Internal, for the decompiler and live preview. NOT the compiled JSON. See the `compile*()`
functions in `agents.js` for that.

```
Statement := AgentAst
           | OrderAst | BracketAst | HelpAst | CancelAllOrdersAst | TokenAst
           | EventAst                        (* legacy, retiring — Appendix A *)

AgentAst  := { kind:'agent',
               preambleCalls: CallNode[],     (* assign items are NOT here — they populate
                                                 `bindings`. Empty when there is no preamble. *)
               condition: Condition,          (* { type:'always' } when no WHEN *)
               doCalls: 'nop' | CallNode[],
               sawWhen: boolean, sawDo: boolean,   (* for the compile-time pairing check *)
               meta: MetaItem[],
               bindings: Binding[] }          (* every label bound anywhere in the statement *)
               (* [target] For event agents, condition is a log-condition leaf, and the compiler
                  sets kind='event' on the compiled agent. The AST shape itself does not change. *)

OrderAst  := { kind:'order', calls: CallNode[], meta: MetaItem[] }
BracketAst:= { kind:'bracket', call: CallNode, meta: MetaItem[] }
HelpAst   := { kind:'help', call: CallNode, meta: MetaItem[] }
CancelAllOrdersAst := { kind:'cancel_all_orders', call: CallNode, meta: MetaItem[] }
TokenAst  := { kind:'token', head: CallNode, bodyCalls: CallNode[], meta }
           | { kind:'token', head: CallNode, condition: Condition, meta }
               (* second form is head kind=condition *)

Binding   := { label: string, rhsKind: 'value' | 'expr', rhsNode: ValueNode | ArithNode,
               rhsText: string }              (* rhsText is the verbatim RHS source slice *)

EventAst  := { kind:'event', eventMeta, msgNode, calls: CallNode[] | 'nop', meta, start }
               (* LEGACY — retiring with event_stmt. eventMeta is { log_tag?, min_level?,
                  render?, where?, gate?, match? }. msgNode is captured only so
                  compileEventStmt can reject a stray msg=. Appendix A. *)

MetaItem  := { key: string, values: MetaValueNode[], start }
MetaValueNode := { type:'string' | 'bareword', value: string }

Condition := { type:'always' }
           | { type:'and' | 'or', left: Condition, right: Condition }
           | { type:'not', term: Condition }
           | { type:'leaf', metric: CallNode, scope: ScopeNode, op: string | null,
               value: ValueNode | null }
           | { type:'between', value1, op1, metric, scope, op2, value2 }
           | { type:'ref', call: CallNode }  (* bare call, resolved at compile time *)
           | { type:'colref', segments: CallNode[], op, value }
           (* [target] a log-condition is a { type:'ref' } whose call resolves to the
              condition-kind `log` row. Wave 2 may add a dedicated node type. Not decided. *)

CallNode  := { type:'call', name: string, args: ArgEntry[], hasCall: boolean, start, end,
               _bindingsAt?: Map }            (* snapshot of labels visible at this call *)
ArgEntry  := { key: string, value: ValueNode } (* key is never null *)
ScopeNode := { type:'scope', segments: (CallNode | QualifierNode)[] }
QualifierNode := { type:'qualifier', key: string,
                   value: { type:'star' } | { type:'name' | 'string', value }, start, end }
ValueNode := { type:'number' | 'string' | 'name', value }
           | { type:'list', value: ValueNode[] }
           | { type:'object', value: { [key: string]: ValueNode } }
               (* object ONLY inside a list that is a tp_scales_json value. Appendix A
                  also uses it for where=/match=. Never a general value. *)
```

---

## Known gaps between this design and the live backend catalog

Documented here rather than silently "fixed". Implement the mechanism generically; these are
catalog-content facts, not grammar bugs unless marked.

- `always`'s sentinel scope is `funds.any_acct` (confirmed against the real `market-open-nse`
  builtin agent in `agent_engine.py`), not `funds.total`.
- The boolean-metric shorthand is keyed off `value_type === 'boolean'`, but no live metric is
  typed `'boolean'`. `is_itm`/`is_ntm` are `'number'`, and `is_future` has no `value_type`. Tests
  use a fixture catalog that declares a real boolean metric.
- `ntfy` (used in the design doc's example) does not exist in the live `notify`/`channel`
  catalog. Implemented generically. Tests use a fixture.
- `side` (used in an operator-table example, `side in [BUY, SELL]`) is not a real condition or
  metric token. `in`/`not in` are generic. Tests use a fixture metric.
- **Resolved** (record of why the design changed): `params_schema` is stored as Postgres JSONB,
  which does not guarantee key order. Every argument is therefore keyword (`arg_list` above), so
  binding is by name only. `paramKeys` order is cosmetic.
- **[target] `render(…)` is not a grammar catalog row.** It is `event_actions.render` in
  `agent_grammar.yaml`. Wave 2 must expose it through the generic call path.
- **[target] `where={…}` is not a `value`.** `kw_arg := NAME "=" value` does not accept
  `object_literal`. Wave 2 must widen it for condition calls only. Do not widen `value` globally.
- **[target] `&` between log-condition leaves is undefined.** `|` is the documented OR. Wave 2
  decides whether `&` is rejected or defined.
- **[open] The `log` action `level` enum omits `debug`.** `agent_grammar.yaml`'s enum lists
  `info`, `warning`, `error`, `critical`. `debug(x)` compiles to level `debug`. Verify whether the
  enum is validated at compile time, and reconcile the YAML or the sugar.
- **[open] `rhs := arith_expr`** (the operator-expression form) is not restated in this file. Its
  grammar lives in `parseGivenRhs`'s own comments in `agents.js`.

---

## Appendix A — Retiring `event_stmt` (live until Wave 2)

**Delete this appendix when Wave 2 ships.** These productions are still the real parser for the
standalone `ALERT log_tag=… render=…` statement. Wave 2's byte-identical compile gate compares
against this behavior, so the rules below are the reference.

```ebnf
event_stmt     := "ALERT" event_meta call_list meta_item*
                                                (* Dispatched when a bare leading ALERT (no parens)
                                                   is immediately followed by one of the six
                                                   event-meta keys, then "=". Checked by a 2-token
                                                   lookahead past ALERT. Never for an ordinary
                                                   bare-ALERT agent, e.g. ALERT telegram DO nop. *)
event_meta     := event_kw*                    (* Keys in ANY order, each at most once. A duplicate
                                                   is a PARSE-time error. *)
event_kw       := ("log_tag" "=" value ",")
                 | ("min_level" "=" value ",")
                 | ("render" "=" value ",")
                 | ("where" "=" object_literal ",")
                 | ("gate" "=" value ",")
                 | ("match" "=" match_value ",")
match_value    := "[" object_literal ("," object_literal)* "]"
```

Semantic rules these productions carry (all must survive into Wave 2):

- **Required.** `log_tag` and `render` are required. A missing one is a COMPILE-time error.
  `min_level` is optional and defaults to `"INFO"`, confirmed against
  `event_agents.validate_seed_spec`'s default.
- **Case.** `min_level` is the ONLY value uppercased (to match the DEBUG/INFO/WARNING/ERROR/
  CRITICAL enum). `log_tag`, `render`, and every value inside `where={…}` are preserved exactly as
  typed. Re-casing `where` would silently break server-side exact-string matching.
- **`where`.** Optional. Keys are real log-record `extra` fields. The object literal is parsed
  directly against the main token stream by `parseObjectLiteral`. `tp_scales_json` cannot share
  that path, because its values sit inside a call's parentheses and need their own mini-lexer.
- **`gate`.** Boolean. Enables repeat-dedup (RepeatGate) on the single render action. Compiles to
  a sibling key of `type`/`render` on `actions[0]`, e.g. `{"type":"render","render":"error",
  "gate":true}`. OMITTED entirely (never written as `false`) when not typed. Validated against
  the backend's `event_actions.render.params_schema`. A channel's own `gate=` kwarg is a separate,
  unrelated per-channel param.
- **`match`.** A list of object literals, mutually exclusive with `log_tag`/`min_level`/`where`.
  Compiles to `{"any": [{"log": {…}}, {"log": {…}}]}`. Each entry uses `tag=` (not `log_tag=`)
  plus the same `min_level`/`where` sub-keys.
- **`msg=`** is rejected anywhere in an event statement, as a clause-level default or as a
  per-channel kwarg. `render=` owns the message for an event agent.
- **Combining with WHEN/DO** is a parse-time error in both positions: before the channel list,
  and after it. The error message names all six keys.
- **Channel list.** After the event meta, the channel list accepts bare `nop` (empty `events`),
  the same as ordinary `DO`. The compiled `events` is then `[]`.
- **Compiled shape.** Compiles to a `kind:'event'` agent. Channels compile through the same
  `compileAlertClause` path ALERT's call_list uses.

Retired reserved-kwarg set: `_EVENT_META_KEYS = {log_tag, min_level, render, where, gate, match}`
at [agents.js:84](../../frontend/src/lib/command/grammars/agents.js#L84). It is NOT in
`_RESERVED`, so the drift guard does not check it. Wave 2 removes it entirely.
