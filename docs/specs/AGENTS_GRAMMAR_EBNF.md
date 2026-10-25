# Agents CLI Grammar — EBNF

Source of truth for the agent CLI grammar implemented in
[`frontend/src/lib/command/grammars/agents.js`](../../frontend/src/lib/command/grammars/agents.js).
When the grammar changes, update the implementation and this file in the same commit.
The prose companion is [AGENTS_SPEC.md §13](AGENTS_SPEC.md) (preamble execution, compiled
`preamble` JSON, operator semantics). A drift-guard test checks the reserved-word production
below against the code: `frontend/src/lib/__tests__/agentsGrammarEbnfDrift.test.js`.

## ⚠️ STATUS — read first

This document describes the grammar as shipped (Wave 2, 2026-10-24 — the plan at
`.claude/PLAN.md`). The grammar has no `GIVEN` keyword, no `ALERT` clause keyword, and one
conditional wrapper, `WHEN … DO …`. Everything before `WHEN` (or the whole statement, if there
is no `WHEN`) is an unconditional preamble. `alert`, `log`, `help`, `debug`, `render`, `order`,
`bracket`, and label assignment are all ordinary items anywhere in the preamble or `DO`.

An event (`kind='event'`) agent is an ORDINARY `agent_stmt` whose condition is a
`log(tag=…, min_level=…, where={…})` call and whose `DO` carries exactly one
`render(render=…, gate=…)` call — see [Event agents](#event-agents) below. The old standalone
`ALERT log_tag=… render=…` statement (`event_stmt`, `_EVENT_META_KEYS`, `parseEventStmt`,
`compileEventStmt`, `decompileEventAgent`) is RETIRED — deleted from `agents.js`, not merely
superseded. There is no corpus of operator-authored `kind='event'` agents to migrate (confirmed
zero rows, same precedent as the `emit_log`→`log` rename); the 23 real `SEEDED_AGENTS`
(`backend/api/algo/event_agents.py`) are Python dicts written directly to the DB by the backend
seeder — never compiled through this CLI grammar — and this file's own byte-identical compile
gate (`agentsGrammar.test.js`) confirms the new syntax reproduces their exact `conditions`/
`events`/`actions` shapes for Edit/Save round-tripping.

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
                                                   Event agents are agent_stmt too. *)

(* ── Agent statement (shipped) ───────────────────────────────────────────── *)
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
                                                   time. A log-condition (`log(tag=…, ...)`) is
                                                   ALSO a bare call — see prose below. *)
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
kw_arg         := NAME "=" value | NAME "=" object_literal
                                                (* Widened (Wave 2) to accept object_literal as a
                                                   CALL ARGUMENT value — GENERICALLY, for any call,
                                                   not scoped to `log(...)`'s own `where=` alone
                                                   (`_OBJECT_LITERAL_ARG_RE` in agents.js). An
                                                   object-literal RHS (`key={...}`) is lexically
                                                   unambiguous — no other value production starts
                                                   with "{" — so the wider fix carries no new
                                                   ambiguity. `_coerceValueForSpec` still rejects an
                                                   object value for any param whose schema does NOT
                                                   declare `type: 'object'`, so only `log(...)`'s
                                                   `where=` (and tp_scales_json's own per-entry
                                                   fields, parsed via a separate mini-lexer, below)
                                                   actually accept one today. *)

(* ── Object literals and the tp_scales_json exception ────────────────────── *)
object_literal := "{" obj_kwarg ("," obj_kwarg)* "}"
obj_kwarg      := NAME "=" value | NAME "=" object_literal
                                                (* Reached from tp_scales_json (below, via its own
                                                   mini-lexer) and from kw_arg's object_literal
                                                   alternative above (via the main token stream).
                                                   Reuses "=" as the key/value separator. *)
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
                                                   given is a specific error), "alert"/"log"/
                                                   "render" (ordinary calls, "log"/"render"
                                                   position-resolved — condition vs. action), "debug",
                                                   "help", and "msg" (contextual only, as NAME "="
                                                   inside alert(...)). *)

(* ── Label rules (a separate set from reserved_word) ──────────────────────── *)
label_reserved := "debug" | "result" | "alert" | "given" | "help"
                                                (* _LABEL_RESERVED_WORDS. A label may NOT be
                                                   bound to any reserved_word OR label_reserved
                                                   word. Not drift-guarded: the set is not
                                                   exported. *)
```

### Event agents

An event (`kind='event'`) agent is an ordinary `agent_stmt` whose condition is a log-condition
call and whose `DO` carries exactly one `render(...)` call (optionally alongside `alert(...)`
channels — see below):

```
WHEN log(tag=error, min_level=WARNING) DO render(render=error, gate=true)
WHEN log(tag=orders, min_level=ERROR, where={channel="telegram"}) | log(tag=risk) DO render(render=x)
WHEN log(tag=agent, min_level=WARNING, where={alert_event="unrouted_alert"})
  DO alert(telegram(gate=false), ntfy(priority=high, gate=false)), render(render=generic_notice)
```

Prose rules (all shipped, Wave 2):

- **A log-condition is a catalog-resolved bare call.** `log(…)` in condition position has NO
  new production. It is the existing `leaf := … | call` alternative, dispatched in `compileRef`.
  Compile-time resolution finds the condition-kind `(condition, log_filter, log)` catalog row —
  a REAL, YAML-sourced row (`agent_grammar.yaml`, right after `between`), loaded through the
  exact same `SYSTEM_TOKENS`/`seed_grammar_tokens()`/`GET /tokens?grammar=condition` pipeline
  every metric/scope/operator row already uses — resolved by NAME, before the ordinary metric
  lookup, via `catalog.logConditions` (a dedicated bucket in `buildCatalog()`, never
  `catalog.metrics`). The `log` name is resolved by POSITION: in a condition it means this
  log-stream filter (`_compileLogConditionCall`), in an action (preamble or `DO`) it means the
  app-log action (see §Actions, `_compileCatalogActionCall`'s `lookupName === 'log'` branch).
  Both rows share the name, the same way `help` reuses a name across `token_kind`s. No
  `GrammarRegistry` loader exists for `(condition, log_filter)` — `_eval_leaf` dispatches
  `"log" in leaf` straight to `agent_evaluator._eval_log`, bypassing the registry entirely.
- **`log(...)`'s three kwargs** (`tag=`, required; `min_level=`, optional; `where=`, optional)
  are validated via `_mapCallArgsToKeys` (unknown-key/duplicate rejection — this is also how
  `render=`/`gate=`/`match=`/`msg=`/`log_tag=` are ALL rejected as "unknown parameter" with zero
  extra code) plus `OBJECT_LITERAL_SCHEMAS.match` (Sprint 18's pre-existing 3-key schema,
  unrenamed but repurposed — a `match=` entry and a bare `log(...)` condition always meant the
  same thing).
- **`|` replaces `match=[…]`.** `log(…) | log(…)` is an ordinary `or_expr`, compiling to
  `{"any": [{"log":...}, {"log":...}]}` via the existing generic `and`/`or` combinator — no new
  compile logic. `a | b | c` compiles to a NESTED binary `any`, **left-associative**:
  `{"any": [{"any": [a, b]}, c]}` — `parseOrExpr` ([agents.js:1635](<../../frontend/src/lib/command/grammars/agents.js#L1635>))
  is a plain left-to-right fold (`node = a`, then `node = {left: node, right: next}` per
  iteration), so the FIRST leaf nests deepest, not outermost. (Corrected 2026-10-25 — this entry
  previously said `{"any":[a, {"any":[b,c]}]}`, the opposite nesting; confirmed wrong against the
  real parser code during the alert-channel-collapse work, which needed exact byte-for-byte
  parity for a 16+-leaf `any` built in Python.) Not the OLD
  `match=[...]`'s flat N-ary list — every real seeded agent before 2026-10-25 used at most 2
  entries, so this never diverged from a real stored shape; a hand-authored 3+-filter agent would get a nested shape
  instead of a flat one (cosmetically different, functionally equivalent — `_eval_log`'s `any`/
  `all` walk recurses either way).
- **Mixing a log-condition with a metric or scope leaf in one `WHEN` is a COMPILE-time error**
  (`_conditionsHasLogLeaf`/`_conditionsHasOrdinaryLeaf`, checked in `compileAgentStmt`). The two
  use different dispatch loops (`_eval_log` vs. `_eval_leaf`'s metric/scope branch) and cannot
  coexist in one agent.
- **`&` and `~` with a log-condition are REJECTED.** Decided (Wave 2, resolving the prior open
  question): two different tag filters can never both match the SAME log record, so `&` has no
  sensible runtime meaning; negating a log-match leaf is undefined the same way. Both are a
  compile-time error (`_isPureLogConditionTree`) — `|` is the only documented combinator for
  multiple log filters.
- **A preamble or a bound label is REJECTED** when the condition is a log-condition. Every call
  belongs in `DO` for an event agent.
- **`DO` for an event agent must contain exactly one `render(...)` call**, optionally alongside
  `alert(...)` channels — no other action type. `alert(...)`'s channels compile to `events`
  (unchanged `compileAlertClause` path) for the rare already-stored row that still carries a
  real, non-empty channel list (most seeded rows carry `events: []` since the 2026-10
  routing-tree plan moved channel selection to `alert_routing.py`).
- **`min_level` is uppercased; defaults to `INFO`.** `tag` and every value inside `where={…}`
  are byte-exact. They are never case-normalized.
- **A `where={…}` field value is either a literal (exact-match) or a single-key operator dict
  (`in`/`not_in`).** No new production — this is purely a runtime semantic rule inside
  `agent_evaluator._eval_log`'s `where` matching (`_where_value_matches`), layered on the
  already-generic `object_literal`-as-value syntax (`kw_arg`'s widening, see "Known gaps"
  below) — a nested `object_literal` as an `obj_kwarg`'s own value was already syntactically
  legal before this rule existed. `where={alert_event="filled"}` is unchanged exact-equality.
  `where={alert_event={in=["filled","cancelled"]}}` / `{not_in=[...]}` apply that operator to
  the named `extra` field instead — `actual in (expected or [])` / `actual not in (expected or
  [])`, the same semantics as the unrelated leaf-level `comparator`'s `in`/`not in` (above), but
  a SEPARATE mechanism: this one matches one `where=` field inside a `log(...)` condition against
  a list, not a metric's resolved value against a scope row. A dict value with any other shape
  (not exactly one recognized operator key) falls back to exact equality against the whole dict —
  never raises. Every pre-existing `where=` value is a plain scalar, so this is additive with zero
  behavior change for any agent saved before this rule shipped.
- **`msg=`/`message=` is rejected** on a log-condition kwarg, a `render(...)` kwarg (both via the
  same generic unknown-key rejection above), AND on an `alert(...)` clause used inside an event
  agent's `DO` (checked by scanning the ALREADY-compiled `events` array for a `message` key,
  rather than re-walking the raw AST — `message` can only appear there when `msg=` was used
  somewhere in the clause). `render(...)` owns the message for an event agent.
- **`kind='event'` is set** when the condition resolves to a log-condition
  (`compileAgentStmt`'s `isEventKind`) — on BOTH the outer `{kind, errors, agent}` wrapper
  `compileStatement` returns (read by `AgentCliEditor.svelte`'s decompiler-selection and
  `allowedKinds` gating) AND the inner `agent.kind` field (read by
  `consoleDispatch.js:submitConsoleAgent`, which decides the real DB row's `kind` column). The
  compiled `conditions`/`events`/`actions` JSON is byte-identical to what the retired
  `event_stmt` compiler produced for the same shape, for every one of the 23 real
  `SEEDED_AGENTS` — `compute_seed_spec_hash()` hashes these three fields.
  `has_explicit_alert` is NEVER set for an event agent (that flag's rich-alert-skip semantics
  belong to `kind='agent'`/cycle dispatch only, which event agents bypass entirely).
- **`render(…)` is a dedicated-compiler call** (`_compileRenderCall`, dispatched by name in
  `_compileSharedCall` alongside `alert`/`bracket`/`debug`/`help` — NOT the generic
  `_compileCatalogActionCall`, whose `{type, params}` output shape doesn't fit: `render`'s own
  value is a TOP-LEVEL SIBLING key of `type`, never nested under `params`). Both `render=`
  (required, free text) and `gate=` (optional boolean) are validated against ONE schema,
  `catalog.renderAction.paramsSchema` — Sprint 18's synthetic `render_action` row
  (`routes/grammar.py:_render_action_catalog_rows`, sourced from `event_actions.render.
  params_schema` in `agent_grammar.yaml`), amended to declare `render` itself as a required
  `freetext` param, not just `gate`. Still deliberately NOT a real `action_type` catalog row —
  merging it in would make `is_known_action_type("render")` true and let an ordinary
  (non-event) cycle agent's `DO` accept a `render` action it has no handler for.

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
- **`alert` with no parentheses** is no longer a clause keyword. Any bare, non-called leading
  `ALERT` (e.g. `ALERT telegram DO …`, or the retired `ALERT log_tag=…` spelling) is a specific
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
               (* EventAst (the old event_stmt AST node) is retired — an event agent is an
                  AgentAst whose condition resolves to a log-condition leaf. There is no
                  separate parsed shape for it. *)

AgentAst  := { kind:'agent',
               preambleCalls: CallNode[],     (* assign items are NOT here — they populate
                                                 `bindings`. Empty when there is no preamble. *)
               condition: Condition,          (* { type:'always' } when no WHEN *)
               doCalls: 'nop' | CallNode[],
               sawWhen: boolean, sawDo: boolean,   (* for the compile-time pairing check *)
               meta: MetaItem[],
               bindings: Binding[] }          (* every label bound anywhere in the statement *)
               (* For an event agent, condition is a log-condition leaf (`{type:'ref'}`
                  resolving to the condition-kind `log` row), and `compileAgentStmt` sets
                  `kind:'event'` on BOTH the outer compile-result wrapper and the inner
                  compiled agent object — see "Event agents" above. The AST shape itself does
                  not change; no dedicated node type was added. *)

OrderAst  := { kind:'order', calls: CallNode[], meta: MetaItem[] }
BracketAst:= { kind:'bracket', call: CallNode, meta: MetaItem[] }
HelpAst   := { kind:'help', call: CallNode, meta: MetaItem[] }
CancelAllOrdersAst := { kind:'cancel_all_orders', call: CallNode, meta: MetaItem[] }
TokenAst  := { kind:'token', head: CallNode, bodyCalls: CallNode[], meta }
           | { kind:'token', head: CallNode, condition: Condition, meta }
               (* second form is head kind=condition *)

Binding   := { label: string, rhsKind: 'value' | 'expr', rhsNode: ValueNode | ArithNode,
               rhsText: string }              (* rhsText is the verbatim RHS source slice *)

MetaItem  := { key: string, values: MetaValueNode[], start }
MetaValueNode := { type:'string' | 'bareword', value: string }

Condition := { type:'always' }
           | { type:'and' | 'or', left: Condition, right: Condition }
           | { type:'not', term: Condition }
           | { type:'leaf', metric: CallNode, scope: ScopeNode, op: string | null,
               value: ValueNode | null }
           | { type:'between', value1, op1, metric, scope, op2, value2 }
           | { type:'ref', call: CallNode }  (* bare call, resolved at compile time — a
                                                 log-condition is this shape, resolving to the
                                                 condition-kind `log` row; no dedicated node
                                                 type was added for it. *)
           | { type:'colref', segments: CallNode[], op, value }

CallNode  := { type:'call', name: string, args: ArgEntry[], hasCall: boolean, start, end,
               _bindingsAt?: Map }            (* snapshot of labels visible at this call *)
ArgEntry  := { key: string, value: ValueNode } (* key is never null *)
ScopeNode := { type:'scope', segments: (CallNode | QualifierNode)[] }
QualifierNode := { type:'qualifier', key: string,
                   value: { type:'star' } | { type:'name' | 'string', value }, start, end }
ValueNode := { type:'number' | 'string' | 'name', value }
           | { type:'list', value: ValueNode[] }
           | { type:'object', value: { [key: string]: ValueNode } }
               (* Inside a list that is a tp_scales_json value, OR (Wave 2) as an ordinary
                  call-argument value via kw_arg's object_literal alternative — e.g. a
                  log(...) condition's `where=`. Never elsewhere. *)
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
- **Resolved (Wave 2) — `render(…)` is now exposed through the generic call path.** It is
  dispatched by name in `_compileSharedCall` (`_compileRenderCall`), reading
  `catalog.renderAction.paramsSchema` (amended to declare `render` itself, not just `gate`) —
  see "Event agents" above. It is still deliberately NOT a real `action_type` catalog row.
- **Resolved (Wave 2) — `where={…}` is now a `value`.** `kw_arg`'s value production was widened
  GENERICALLY (any call argument, any key) to accept `object_literal` — not scoped to `log(...)`'s
  own `where=` alone. See `kw_arg`'s own production comment above for why the generic fix was
  chosen over a narrow one.
- **Resolved (Wave 2) — `&`/`~` with a log-condition are rejected.** See "Event agents" above.
- **Resolved — `where={…}` field values support `in`/`not_in`.** No grammar change (the generic
  `object_literal`-as-value widening above already permitted the nested shape). Semantics only,
  in `agent_evaluator._eval_log`/`_where_value_matches`. See "Event agents" above.
- **[open] The `log` action `level` enum omits `debug`.** `agent_grammar.yaml`'s enum lists
  `info`, `warning`, `error`, `critical`. `debug(x)` compiles to level `debug`. Verify whether the
  enum is validated at compile time, and reconcile the YAML or the sugar.
- **[open] `rhs := arith_expr`** (the operator-expression form) is not restated in this file. Its
  grammar lives in `parseGivenRhs`'s own comments in `agents.js`.
- **[open, Wave 3] Chevrotain (`agentsLexer.js`/`agentsParser.js`) still throws a fixed
  `"event_stmt is not implemented"` error for any text matching the OLD `ALERT log_tag=...`
  shape — it was never updated to recognize the new `log(...)`/`render(...)` calls as anything
  special, because `WHEN`/`DO`/a bare `call` were already implemented and AST-equal there before
  Wave 2 (the point of this redesign was to SHRINK Chevrotain's gap, not grow it — no new corpus
  capture is needed for the new syntax). The differential harness's `KNOWN_GAP_TEXTS`/
  `isEventStmtGap` ratchet (`agentsChevrotainDiff.test.js`) still catches every OLD-syntax corpus
  string as a known gap regardless of which side rejects it or why; re-capturing the corpus
  against the new grammar (dropping the now-invalid old-syntax strings) is Wave 3 scope.
