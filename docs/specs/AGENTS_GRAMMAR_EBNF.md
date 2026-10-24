# Agents CLI Grammar — EBNF

Extracted 2026-10-25 from the header comment of
[`frontend/src/lib/command/grammars/agents.js`](../../frontend/src/lib/command/grammars/agents.js)
(lines 1–635 at extraction time), which was itself described as mirroring an
"authoritative copy" that had only ever lived in the ephemeral `.claude/PLAN.md`
file — overwritten and archived (`.claude/PLAN.done*.md`) every time a new plan
started, with no single persistent source of truth. This file is now that
source of truth. **Keep it in sync with `agents.js` going forward** — when the
grammar changes, update both the implementation and this file in the same
commit, the same way any other `docs/specs/*.md` is expected to track its
implementation.

## ⚠️ KNOWN STALE AS OF EXTRACTION — do not treat as current without checking

This EBNF was last meaningfully updated before the 2026-10-24/25
grammar-unification work (commits `e509bcf1`, `dbcf77ec`, and the related
`GIVEN`-removal / preamble / `help` / `log`-rename sprint documented in
`CLAUDE.md`). Specifically known wrong or missing below:

- **`agent_stmt`/`tail`/`alert_part` describe `ALERT` as a reserved clause
  keyword living *inside* `agent_stmt`** (`tail := alert_part do_part | ...`,
  `alert_part := "ALERT" alert_clause`). This is no longer how the grammar
  works — `GIVEN` is removed entirely, `ALERT` is no longer a reserved clause
  keyword anywhere, `WHEN...DO...` is the one generic optional conditional
  wrapper, and anything before `WHEN` (or the whole statement, if there's no
  `WHEN`) is an unconditional preamble accepting the full statement vocabulary
  (`order`/`bracket`/`alert`/`log`/`help`/`debug`/label-assignment). See
  `docs/specs/AGENTS_SPEC.md` for the current, correct grammar description in
  prose; this EBNF has not yet been brought up to date to match it.
- **No `help` statement/action production** — added after this comment was
  last updated.
- **`event_kw`'s `log(...)` action and its `priority=`/`name=`/`tags=`
  parameters are not described here** — this EBNF still only shows the
  standalone `ALERT log_tag=...` statement form, not the `log(...)` action
  used inside a preamble or `DO`.
- **`GIVEN` is not mentioned anywhere in this file at all** (it predates the
  keyword's introduction *and* its later removal), so there's nothing to
  strike — but a reader relying on this file alone would not know `agent_stmt`
  itself has been restructured around a preamble instead of `when_part? tail`.

A follow-up pass should rewrite the EBNF below to match current reality
rather than patch it incrementally — the structural change (preamble-first,
no `GIVEN`, no `ALERT`-as-clause) touches enough of the top-level productions
that incremental edits would likely leave it inconsistent again.

---

```ebnf
statement      := agent_stmt | event_stmt | order_stmt | bracket_stmt
                 | token_stmt
event_stmt     := "ALERT" event_meta call_list meta_item*
                                                (* Sprint 12 — a bare
                                                    ALERT-only statement
                                                    for notification
                                                    (`kind='event'`)
                                                    agents — their
                                                    "condition" is a
                                                    log-stream
                                                    subscription filter
                                                    (tag/min_level/
                                                    optional where),
                                                    never a polled
                                                    metric condition,
                                                    and their action is
                                                    always exactly one
                                                    render hook, never a
                                                    trading action — so
                                                    this gets its own
                                                    statement shape
                                                    instead of being
                                                    forced through
                                                    agent_stmt's WHEN/
                                                    DO. Dispatched from
                                                    `parseStatement`
                                                    (see its own
                                                    dispatch comment)
                                                    specifically when a
                                                    bare leading ALERT
                                                    (no WHEN before it)
                                                    is immediately
                                                    followed by one of
                                                    event_meta's four
                                                    reserved keys —
                                                    never for an
                                                    ordinary bare-ALERT
                                                    agent_stmt (e.g.
                                                    `ALERT telegram DO
                                                    nop`). *)
event_meta     := event_kw*                     (* log_tag=/min_level=/
                                                    render=/where=/
                                                    gate= — ANY ORDER,
                                                    each at
                                                    most once; a
                                                    duplicate key is a
                                                    PARSE-time error
                                                    (not deferred to
                                                    compile time, per
                                                    the operator's
                                                    explicit
                                                    instruction). *)
event_kw       := ("log_tag" "=" value ",")
                 | ("min_level" "=" value ",")
                 | ("render" "=" value ",")
                 | ("where" "=" object_literal ",")
                 | ("gate" "=" value ",")         (* Sprint 18 —
                                                    boolean, enables
                                                    repeat-dedup
                                                    (RepeatGate) on
                                                    this render
                                                    action's fired
                                                    records; compiles
                                                    onto the single
                                                    actions[0] entry
                                                    as a sibling key
                                                    of `type`/
                                                    `render`, e.g.
                                                    {"type":"render",
                                                    "render":"error",
                                                    "gate":true} —
                                                    OMITTED entirely
                                                    (not written as
                                                    false) when not
                                                    typed, matching
                                                    every other
                                                    seeded agent's
                                                    real shape.
                                                    Validated against
                                                    `catalog.
                                                    renderAction`'s
                                                    own params_schema
                                                    (backend's
                                                    `event_actions.
                                                    render.
                                                    params_schema`),
                                                    not hardcoded. *)
                                                (* `log_tag`/`render`
                                                    are REQUIRED
                                                    (compile-time error
                                                    if either missing).
                                                    `min_level` is
                                                    OPTIONAL, defaults
                                                    to `"INFO"`
                                                    (confirmed against
                                                    event_agents.py:
                                                    validate_seed_spec's
                                                    own default) and is
                                                    the ONE value here
                                                    that DOES get
                                                    case-normalized (to
                                                    UPPERCASE, matching
                                                    the DEBUG/INFO/
                                                    WARNING/ERROR/
                                                    CRITICAL enum) —
                                                    `log_tag`, `render`,
                                                    and every value
                                                    inside `where={...}`
                                                    are preserved
                                                    EXACTLY as typed,
                                                    bypassing this
                                                    grammar's normal
                                                    enum-casing
                                                    normalization
                                                    entirely (`where`
                                                    matches real
                                                    log-record `extra`
                                                    fields by exact
                                                    string equality
                                                    server-side — re-
                                                    casing it would
                                                    silently break
                                                    matches). `where`
                                                    is OPTIONAL. `msg=`
                                                    is explicitly
                                                    rejected anywhere
                                                    in an event_stmt
                                                    (clause-level
                                                    default OR a
                                                    per-channel own
                                                    kwarg) — `render=`
                                                    owns the message
                                                    for an event agent,
                                                    not `msg=`.
                                                    Combining any of
                                                    these five reserved
                                                    keys with WHEN/DO
                                                    is a compile-time
                                                    error (see
                                                    `parseAlertClauseBody`/
                                                    `parseEventStmt`'s
                                                    own comments). *)
                 | ("match" "=" match_value ",")  (* Sprint 18 follow-up
                                                    — a list-of-objects
                                                    alternative to the
                                                    `log_tag=`/
                                                    `min_level=`/
                                                    `where=` triple,
                                                    MUTUALLY EXCLUSIVE
                                                    with all three
                                                    (compile-time error
                                                    if combined — see
                                                    `compileEventStmt`).
                                                    Compiles to
                                                    {"any": [{"log":
                                                    {...}}, {"log":
                                                    {...}}]} — each list
                                                    entry has the SAME
                                                    `tag`/`min_level`/
                                                    `where` sub-keys the
                                                    simple form has
                                                    (spelled `tag=`, not
                                                    `log_tag=`, since
                                                    there is no reserved-
                                                    word collision risk
                                                    inside an object
                                                    literal). *)
match_value    := "[" object_literal ("," object_literal)* "]"
                                                (* Sprint 18 follow-up —
                                                    reuses
                                                    `parseObjectLiteral`
                                                    directly against the
                                                    main token stream,
                                                    same mechanism
                                                    `where=` already
                                                    uses (see
                                                    `parseMatchValue`'s
                                                    own comment for why
                                                    this needs no
                                                    dedicated mini-lexer
                                                    the way
                                                    `tp_scales_json_value`
                                                    does). *)
token_stmt     := call call_list meta_item*     (* Sprint 10 — `token(...)`
                                                    only, restricted to
                                                    standalone use,
                                                    EXACTLY mirroring
                                                    bracket_stmt's own
                                                    "only bracket(...)
                                                    can be used
                                                    standalone" rule/
                                                    error-message style,
                                                    naming "token"
                                                    instead. The HEAD
                                                    call (`token(kind=
                                                    notify|condition)`)
                                                    is immediately
                                                    followed by its BODY
                                                    call_list (no comma
                                                    between the head and
                                                    the first body call
                                                    — commas only join
                                                    the body's own
                                                    elements — e.g.
                                                    `token(kind=notify)
                                                    telegram, email,
                                                    log`). `kind=notify`
                                                    is the only
                                                    supported value this
                                                    sprint; the body is
                                                    a plain channel
                                                    call_list, compiled
                                                    via the EXACT SAME
                                                    path ALERT's own
                                                    call_list already
                                                    uses. `#name=` is
                                                    required (compile-
                                                    time error if
                                                    absent) since
                                                    `AgentTemplate.name`
                                                    is a required
                                                    field; `#tag=`/
                                                    `#group=` are
                                                    rejected — no such
                                                    columns exist on
                                                    `AgentTemplate`. *)
bracket_stmt   := call meta_item*               (* `bracket(...)` only —
                                                    restricted to
                                                    standalone use,
                                                    EXACTLY mirroring
                                                    order_stmt's own
                                                    "only order(...) can
                                                    be used standalone"
                                                    rule/error-message
                                                    style, just naming
                                                    "bracket" instead of
                                                    "order" — a single
                                                    call, no call_list
                                                    (order_stmt allows
                                                    2+ calls for a
                                                    basket; a bracket
                                                    save is always
                                                    exactly one preset).
                                                    `#name=` is
                                                    effectively required
                                                    (compile-time error
                                                    if absent) since
                                                    OrderTemplateCreate.
                                                    name is a required
                                                    field. Sprint 8. *)
agent_stmt     := when_part? tail meta_item*  (* WHEN, ALERT, and DO are
                                                 each independently
                                                 optional and may appear
                                                 in any order; omitting
                                                 WHEN means `always`;
                                                 omitting a clause means
                                                 the same as writing it
                                                 as "nop"; at least one
                                                 real ALERT or DO is
                                                 still required — all-
                                                 omitted/all-nop is a
                                                 compile-time error.
                                                 meta_item* is TRAILING
                                                 ONLY — after `tail`
                                                 finishes parsing, never
                                                 interspersed with
                                                 WHEN/ALERT/DO. Sprint 7.
                                                 *** SEE "KNOWN STALE"
                                                 BANNER ABOVE — this
                                                 production predates the
                                                 GIVEN-removal/preamble/
                                                 ALERT-is-no-longer-a-
                                                 clause unification and
                                                 is no longer accurate. ***)
when_part      := "WHEN" condition
tail           := alert_part do_part | do_part alert_part
                 | alert_part | do_part | ε
alert_part     := "ALERT" alert_clause
do_part        := "DO" do_clause
alert_clause   := "nop" | (("msg" "=" value ",")? call_list)
                                                (* "msg" is NOT a
                                                   reserved word — it
                                                   is recognized
                                                   CONTEXTUALLY, only
                                                   as NAME "=" in the
                                                   leading position of
                                                   a non-"nop" ALERT
                                                   clause, before any
                                                   channel. Sprint 6. *)
do_clause      := "nop" | call_list
call_list      := call ("," call)*
order_stmt     := call ("," call)* meta_item*  (* order/place_order only;
                                                   a bare order_stmt
                                                   carrying any meta_item
                                                   is parsed fine but a
                                                   COMPILE-time error —
                                                   meta tags only apply
                                                   to agents/brackets.
                                                   Sprint 7. *)
meta_item      := "#" NAME "=" meta_value ("," meta_value)*
                                                (* trailing-only — see
                                                   agent_stmt/order_stmt
                                                   above. NAME is one of
                                                   the four recognized
                                                   keys (name/desc/tag/
                                                   group) — an unknown
                                                   key is a compile-time
                                                   error, not a parse
                                                   error (same
                                                   parse-permissively/
                                                   validate-at-compile
                                                   split every other
                                                   production in this
                                                   grammar follows).
                                                   Sprint 7. *)
meta_value     := STRING | BARE_WORD           (* Sprint 7 *)
BARE_WORD      := (* an unquoted run of 1+ characters containing NONE
                     of: whitespace, ",", "#", "=", or a quote
                     character — quotes are required only once one of
                     those appears in the value. DELIBERATELY WIDER
                     than NAME's word-char class (letters/digits/
                     underscore only) — e.g. "hedge-book" lexes as one
                     BARE_WORD token (the hyphen is allowed) but would
                     NOT lex as a single NAME token elsewhere in this
                     grammar. Scanned by a dedicated branch in
                     `lexAgentStatement` triggered by "#", not reused
                     from the NAME/word-char scanner. Sprint 7. *)
condition      := "always" | or_expr
or_expr        := term (("&" | "|") term)*     (* equal precedence, L-to-R *)
term           := "~" primary | primary
primary        := "(" or_expr ")" | leaf
leaf           := metric_ref comparator value
                 | col_ref comparator value                       (* Sprint 15 *)
                 | value comparator (metric_ref | col_ref) comparator value   (* between *)
                 | call                           (* Sprint 10 — a BARE
                                                       call with no "@"
                                                       following is
                                                       syntactically a
                                                       Token ($ref)
                                                       reference — a
                                                       real metric
                                                       ALWAYS requires
                                                       "@ scope_ref", so
                                                       this is parsed
                                                       permissively here
                                                       and resolved at
                                                       COMPILE time
                                                       against the
                                                       catalog (same
                                                       split every other
                                                       semantic check in
                                                       this grammar
                                                       follows): a name
                                                       resolving to a
                                                       catalog entry
                                                       with
                                                       `isFragmentRef`
                                                       compiles to
                                                       `{"$ref": name}`;
                                                       a real metric
                                                       missing its "@"
                                                       still gets the
                                                       original "expected
                                                       '@' after metric"
                                                       error, just at
                                                       compile time
                                                       instead of parse
                                                       time. *)
metric_ref     := call "@" scope_ref
scope_ref      := scope_seg ("." scope_seg)*
scope_seg      := call | qualifier_seg     (* Sprint 19 — a qualifier_seg
                     is trailing-only (never followed by a plain `call`
                     segment); short scope-family aliases (`pos`/`hld`/
                     `fnd`/`wl`, SCOPE_FAMILY_ALIASES) apply to
                     scope_ref's FIRST `call` segment only. *)
qualifier_seg  := NAME ":" qualifier_value (* Sprint 19 — `account:value`/
                     `symbol:value` appended to a scope chain, e.g.
                     `positions.account:ZG0790`. Compiles to a leaf-level
                     `scope_filter` dict (sibling of metric/scope/op/
                     value — the REAL, already-shipped backend shape,
                     commit 1a904a12), never folded into the `scope`
                     string. `*`/`worst` are RESERVED values with NO
                     `scope_filter` — pure aliases onto the EXISTING
                     `<family>.any_acct`/`<family>.worst_acct`/
                     `<family>.worst_symbol` scope tokens (zero new
                     backend mechanism). See `_resolveScopeRefFull`'s
                     own comment for the full resolution algorithm. *)
qualifier_value := "*" | NAME | STRING     (* "*" = wildcard (no filter);
                     a bare NAME of `worst` (case-insensitive) is the
                     "computed worst" sentinel; any other NAME/STRING is
                     a literal filter value. *)
col_ref        := call ("." call)+        (* Sprint 15 — `leaf`'s OTHER
                     alternative, with NO "@": a bare dotted chain,
                     e.g. `positions.total.pnl`. Syntactically
                     IDENTICAL to `scope_ref` (both are just
                     `call ("." call)*`), but semantically distinct —
                     `scope_ref` only ever appears after "@"; `col_ref`
                     only ever appears as a leaf's LHS with no "@" at
                     all. Disambiguation of which trailing segment(s)
                     are the scope vs. the column happens at COMPILE
                     time (`_resolveDotAccess`): try the WHOLE chain
                     against known scope names first (this is what
                     keeps `positions.expiring_today.mcx_unhedged`
                     resolving as a SCOPE, not a column, when it's
                     used standalone with no trailing column segment),
                     falling back to "last segment is a column (or an
                     existing bare zero-arg metric) on the scope
                     resolved so far" only when the full chain isn't
                     itself a known scope. *)
comparator     := "<" | "<=" | ">" | ">=" | "==" | "!=" | "in" | "not in"
value          := NUMBER | STRING | NAME | list_literal
list_literal   := "[" value ("," value)* "]"
call           := NAME | NAME "(" arg_list ")"   (* a NAME in CALL/TOKEN-
                                                     REFERENCE position
                                                     (this production,
                                                     and the bare
                                                     metric/scope/
                                                     channel/action name
                                                     case generally) may
                                                     contain internal
                                                     hyphens, mid-word
                                                     only — Sprint 10,
                                                     see lexAgentStatement's
                                                     NAME-scanning branch
                                                     — so a real seeded
                                                     Token name like
                                                     `notify-critical-
                                                     trio` lexes as ONE
                                                     token. Does NOT
                                                     widen NAME
                                                     elsewhere (kwarg
                                                     KEYS are scanned by
                                                     a wholly separate
                                                     tokenizer,
                                                     `tokenizeCallStyle`
                                                     in engine.js, never
                                                     touched here). *)
arg_list       := kw_arg ("," kw_arg)*          (* EVERY arg is keyword —
                                                     including the first;
                                                     no positional form
                                                     exists in this
                                                     grammar at all *)
kw_arg         := NAME "=" value
object_literal := "{" obj_kwarg ("," obj_kwarg)* "}"   (* Sprint 8 — a
                                                     genuinely NEW value
                                                     shape. GENERALIZED
                                                     in Sprint 12 off its
                                                     original STRICT
                                                     scoping to the
                                                     tp_scales_json_value
                                                     production below —
                                                     `event_stmt`'s
                                                     `where=` (Sprint 12)
                                                     is a SECOND call
                                                     site reusing this
                                                     EXACT SAME
                                                     production/parser
                                                     function
                                                     (`parseObjectLiteral`)
                                                     directly against the
                                                     MAIN statement token
                                                     stream (once "{"/"}"
                                                     are lexed there too
                                                     — see `_PUNCT_SINGLE`)
                                                     rather than
                                                     `tp_scales_json`'s
                                                     own raw-substring
                                                     mini-lexer. Sprint
                                                     18 follow-up adds a
                                                     THIRD call site —
                                                     each entry of
                                                     `match=[...]`'s own
                                                     list (see
                                                     `match_value` below)
                                                     — and widens
                                                     `obj_kwarg`'s own
                                                     `value` to also
                                                     accept a NESTED
                                                     object_literal (a
                                                     match entry's own
                                                     `where={...}`),
                                                     scoped to that one
                                                     case only. Still
                                                     NOT a general
                                                     addition to the
                                                     top-level `value`
                                                     production anywhere
                                                     else in this
                                                     grammar. Reuses
                                                     "=" as the key/value
                                                     separator — this
                                                     grammar's one and
                                                     only keyword-
                                                     assignment operator
                                                     everywhere else — a
                                                     deliberate decision
                                                     NOT to introduce a
                                                     second ":" operator
                                                     just for this one
                                                     nested case. *)
obj_kwarg      := NAME "=" value
tp_scales_json_value := "[" object_literal ("," object_literal)* "]"
                                                 (* Sprint 8 — the ONLY
                                                    legal value shape
                                                    for a call argument
                                                    named `tp_scales_json`
                                                    (case-insensitive),
                                                    on EITHER order(...)
                                                    or bracket(...).
                                                    Parsed by a
                                                    dedicated mini
                                                    lexer/parser (see
                                                    `_parseTpScalesJsonArg`
                                                    below) — NOT routed
                                                    through
                                                    tokenizeCallStyle,
                                                    which has no
                                                    "["/"]"/"{"/"}"
                                                    awareness at all. *)
```

## AST shape

(Internal, documented for the decompiler / live-preview — NOT the compiled
JSON; see `compile*()` functions in `agents.js` for that.)

```
Statement  := { kind:'agent', condition, alertCalls, alertMsg, doCalls, meta }
            | { kind:'event', eventMeta, msgNode, calls: CallNode[], meta }
               (* Sprint 12 — `eventMeta` is `{ log_tag?, min_level?,
                  render?, where? }`, each a ValueNode (object for
                  `where`); `msgNode` is the optional clause-level
                  `msg=` default CAPTURED only so `compileEventStmt`
                  can reject it with a clear message (mirrors
                  `alertMsg` above, but always an error here, never
                  accepted); `calls` is the channel call_list,
                  compiled via the same `compileAlertClause` path
                  ALERT's own call_list uses. *)
            | { kind:'order', calls: CallNode[], meta }
            | { kind:'bracket', call: CallNode, meta }   (* Sprint 8 —
               exactly ONE call (never a list, unlike 'order') *)
            | { kind:'token', head: CallNode, bodyCalls: CallNode[], meta }
               (* Sprint 10 — `head` is the `token(kind=...)` call;
                  `bodyCalls` is the channel call_list compiled via the
                  same path ALERT's own call_list uses. *)
alertMsg   := ValueNode | null   (* the optional clause-level `msg=`
               default — ALWAYS null when alertCalls is 'nop' or when
               no ALERT clause was written at all; DO has no equivalent
               field. Sprint 6. *)
meta       := MetaItem[]         (* [] when no trailing `#...` clause was
               written at all. Produced by `parseMetaClause` — a
               GENERIC, reusable helper (also usable by a future
               bracket_stmt). For `kind:'order'`, a non-empty `meta` is
               parsed fine but is a COMPILE-time error, not a parse
               error. Sprint 7. *)
MetaItem   := { key: string, values: MetaValueNode[], start }
MetaValueNode := { type:'string'|'bareword', value: string }
Condition  := { type:'always' }
            | { type:'and'|'or', left: Condition, right: Condition }
            | { type:'not', term: Condition }
            | { type:'leaf', metric: CallNode, scope: ScopeNode,
                op: string|null, value: ValueNode|null }
            | { type:'between', value1: ValueNode, op1: string,
                metric: CallNode, scope: ScopeNode, op2: string,
                value2: ValueNode }
            | { type:'ref', call: CallNode }   (* Sprint 10 — a bare
                call with no "@" following; resolved at COMPILE time —
                see the `leaf` EBNF production's own comment above. *)
            | { type:'colref', segments: CallNode[], op: string|null,
                value: ValueNode|null }   (* Sprint 15 — a bare dotted
                chain with NO "@" at all, e.g. `positions.total.pnl`.
                Parsed identically to `scope_ref`'s own "." call)*
                shape, but which segments are the SCOPE and which
                trailing segment is a COLUMN (or an existing bare
                zero-arg metric bound to that scope) is a COMPILE-time
                decision — see `_resolveDotAccess`'s "longest known-
                scope-chain match first" rule. Distinct from `leaf`
                (which always has an explicit "@") so the two can
                never be confused structurally. *)
CallNode   := { type:'call', name: string, args: ArgEntry[],
                hasCall: boolean, start, end }
ArgEntry   := { key: string, value: ValueNode }   (* key is NEVER null —
                every argument is keyword-only, including the first *)
ScopeNode  := { type:'scope', segments: (CallNode|QualifierNode)[] }
               (* Sprint 19 — a segment may now be a QualifierNode
                  (`account:value`/`symbol:value`), always trailing. *)
QualifierNode := { type:'qualifier', key: string,
                    value: { type:'star' } | { type:'name'|'string', value },
                    start, end }
ValueNode  := { type:'number'|'string'|'name', value }
            | { type:'list', value: ValueNode[] }
            | { type:'object', value: { [key: string]: ValueNode } }
               (* Sprint 8 — object_literal; ONLY ever appears as an
                  item inside a 'list' ValueNode that is itself the
                  value of a `tp_scales_json=` argument — never
                  produced anywhere else in this grammar. *)
```

## Known gaps between this design and the live backend catalog

(Documented here rather than silently "fixed" — implement the mechanism
generically per the design doc; these are catalog-content facts, not
grammar bugs.)

- `always`'s sentinel scope is `funds.any_acct` (confirmed against the real
  `market-open-nse` builtin agent in `agent_engine.py`), not `funds.total` as
  one earlier draft guessed.
- The boolean-metric shorthand is keyed off `value_type === 'boolean'`
  exactly as specified, but no metric in the live catalog is actually typed
  `'boolean'` today — `is_itm`/`is_ntm` are `value_type: 'number'` and
  `is_future` has no `value_type` at all. The mechanism is correct and
  future-proof; it simply has nothing to fire against yet. Tests exercise it
  via a fixture catalog that declares a real boolean metric.
- `ntfy` (used in the design doc's own canonical example 2) does not exist
  in the live `notify`/`channel` catalog. Implemented generically; tests use
  a fixture catalog that adds it.
- `side` (used in the design doc's own operator-table example, `side in
  [BUY, SELL]`) is not a real condition/metric token in the live catalog.
  `in`/`not_in` are implemented generically against any metric; tests use a
  fixture metric for this.
- **Resolved** (kept as a record of why the design changed): `params_schema`
  is stored as Postgres JSONB (`backend/api/models.py: GrammarToken.
  params_schema`), which does not guarantee object key order survives a
  round-trip through the DB. The original design read `paramKeys[0]` as "the
  positional-or-keyword slot," which would have silently depended on that
  unguaranteed order in production even though every test (JS object
  literals do preserve order) would have passed regardless. The fix: remove
  positional arguments from the grammar entirely — every argument is always
  keyword (`arg_list := kw_arg ("," kw_arg)*` above), so `_mapCallArgsToKeys`
  only ever looks up a key by name, never by position. `paramKeys`'s array
  order is now used only for cosmetic/display purposes (if at all), never
  for binding correctness — no backend change was needed.
