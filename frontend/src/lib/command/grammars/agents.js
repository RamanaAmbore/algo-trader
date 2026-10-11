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
//   statement      := agent_stmt | event_stmt | order_stmt | bracket_stmt
//                    | token_stmt
//   event_stmt     := "ALERT" event_meta call_list meta_item*
//                                                   (* Sprint 12 — a bare
//                                                       ALERT-only statement
//                                                       for notification
//                                                       (`kind='event'`)
//                                                       agents — their
//                                                       "condition" is a
//                                                       log-stream
//                                                       subscription filter
//                                                       (tag/min_level/
//                                                       optional where),
//                                                       never a polled
//                                                       metric condition,
//                                                       and their action is
//                                                       always exactly one
//                                                       render hook, never a
//                                                       trading action — so
//                                                       this gets its own
//                                                       statement shape
//                                                       instead of being
//                                                       forced through
//                                                       agent_stmt's WHEN/
//                                                       DO. Dispatched from
//                                                       `parseStatement`
//                                                       (see its own
//                                                       dispatch comment)
//                                                       specifically when a
//                                                       bare leading ALERT
//                                                       (no WHEN before it)
//                                                       is immediately
//                                                       followed by one of
//                                                       event_meta's four
//                                                       reserved keys —
//                                                       never for an
//                                                       ordinary bare-ALERT
//                                                       agent_stmt (e.g.
//                                                       `ALERT telegram DO
//                                                       nop`). *)
//   event_meta     := event_kw*                     (* log_tag=/min_level=/
//                                                       render=/where=/
//                                                       gate= — ANY ORDER,
//                                                       each at
//                                                       most once; a
//                                                       duplicate key is a
//                                                       PARSE-time error
//                                                       (not deferred to
//                                                       compile time, per
//                                                       the operator's
//                                                       explicit
//                                                       instruction). *)
//   event_kw       := ("log_tag" "=" value ",")
//                    | ("min_level" "=" value ",")
//                    | ("render" "=" value ",")
//                    | ("where" "=" object_literal ",")
//                    | ("gate" "=" value ",")         (* Sprint 18 —
//                                                       boolean, enables
//                                                       repeat-dedup
//                                                       (RepeatGate) on
//                                                       this render
//                                                       action's fired
//                                                       records; compiles
//                                                       onto the single
//                                                       actions[0] entry
//                                                       as a sibling key
//                                                       of `type`/
//                                                       `render`, e.g.
//                                                       {"type":"render",
//                                                       "render":"error",
//                                                       "gate":true} —
//                                                       OMITTED entirely
//                                                       (not written as
//                                                       false) when not
//                                                       typed, matching
//                                                       every other
//                                                       seeded agent's
//                                                       real shape.
//                                                       Validated against
//                                                       `catalog.
//                                                       renderAction`'s
//                                                       own params_schema
//                                                       (backend's
//                                                       `event_actions.
//                                                       render.
//                                                       params_schema`),
//                                                       not hardcoded. *)
//                                                   (* `log_tag`/`render`
//                                                       are REQUIRED
//                                                       (compile-time error
//                                                       if either missing).
//                                                       `min_level` is
//                                                       OPTIONAL, defaults
//                                                       to `"INFO"`
//                                                       (confirmed against
//                                                       event_agents.py:
//                                                       validate_seed_spec's
//                                                       own default) and is
//                                                       the ONE value here
//                                                       that DOES get
//                                                       case-normalized (to
//                                                       UPPERCASE, matching
//                                                       the DEBUG/INFO/
//                                                       WARNING/ERROR/
//                                                       CRITICAL enum) —
//                                                       `log_tag`, `render`,
//                                                       and every value
//                                                       inside `where={...}`
//                                                       are preserved
//                                                       EXACTLY as typed,
//                                                       bypassing this
//                                                       grammar's normal
//                                                       enum-casing
//                                                       normalization
//                                                       entirely (`where`
//                                                       matches real
//                                                       log-record `extra`
//                                                       fields by exact
//                                                       string equality
//                                                       server-side — re-
//                                                       casing it would
//                                                       silently break
//                                                       matches). `where`
//                                                       is OPTIONAL. `msg=`
//                                                       is explicitly
//                                                       rejected anywhere
//                                                       in an event_stmt
//                                                       (clause-level
//                                                       default OR a
//                                                       per-channel own
//                                                       kwarg) — `render=`
//                                                       owns the message
//                                                       for an event agent,
//                                                       not `msg=`.
//                                                       Combining any of
//                                                       these five reserved
//                                                       keys with WHEN/DO
//                                                       is a compile-time
//                                                       error (see
//                                                       `parseAlertClauseBody`/
//                                                       `parseEventStmt`'s
//                                                       own comments). *)
//                    | ("match" "=" match_value ",")  (* Sprint 18 follow-up
//                                                       — a list-of-objects
//                                                       alternative to the
//                                                       `log_tag=`/
//                                                       `min_level=`/
//                                                       `where=` triple,
//                                                       MUTUALLY EXCLUSIVE
//                                                       with all three
//                                                       (compile-time error
//                                                       if combined — see
//                                                       `compileEventStmt`).
//                                                       Compiles to
//                                                       {"any": [{"log":
//                                                       {...}}, {"log":
//                                                       {...}}]} — each list
//                                                       entry has the SAME
//                                                       `tag`/`min_level`/
//                                                       `where` sub-keys the
//                                                       simple form has
//                                                       (spelled `tag=`, not
//                                                       `log_tag=`, since
//                                                       there is no reserved-
//                                                       word collision risk
//                                                       inside an object
//                                                       literal). *)
//   match_value    := "[" object_literal ("," object_literal)* "]"
//                                                   (* Sprint 18 follow-up —
//                                                       reuses
//                                                       `parseObjectLiteral`
//                                                       directly against the
//                                                       main token stream,
//                                                       same mechanism
//                                                       `where=` already
//                                                       uses (see
//                                                       `parseMatchValue`'s
//                                                       own comment for why
//                                                       this needs no
//                                                       dedicated mini-lexer
//                                                       the way
//                                                       `tp_scales_json_value`
//                                                       does). *)
//   token_stmt     := call call_list meta_item*     (* Sprint 10 — `token(...)`
//                                                       only, restricted to
//                                                       standalone use,
//                                                       EXACTLY mirroring
//                                                       bracket_stmt's own
//                                                       "only bracket(...)
//                                                       can be used
//                                                       standalone" rule/
//                                                       error-message style,
//                                                       naming "token"
//                                                       instead. The HEAD
//                                                       call (`token(kind=
//                                                       notify|condition)`)
//                                                       is immediately
//                                                       followed by its BODY
//                                                       call_list (no comma
//                                                       between the head and
//                                                       the first body call
//                                                       — commas only join
//                                                       the body's own
//                                                       elements — e.g.
//                                                       `token(kind=notify)
//                                                       telegram, email,
//                                                       log`). `kind=notify`
//                                                       is the only
//                                                       supported value this
//                                                       sprint; the body is
//                                                       a plain channel
//                                                       call_list, compiled
//                                                       via the EXACT SAME
//                                                       path ALERT's own
//                                                       call_list already
//                                                       uses. `#name=` is
//                                                       required (compile-
//                                                       time error if
//                                                       absent) since
//                                                       `AgentTemplate.name`
//                                                       is a required
//                                                       field; `#tag=`/
//                                                       `#group=` are
//                                                       rejected — no such
//                                                       columns exist on
//                                                       `AgentTemplate`. *)
//   bracket_stmt   := call meta_item*               (* `bracket(...)` only —
//                                                       restricted to
//                                                       standalone use,
//                                                       EXACTLY mirroring
//                                                       order_stmt's own
//                                                       "only order(...) can
//                                                       be used standalone"
//                                                       rule/error-message
//                                                       style, just naming
//                                                       "bracket" instead of
//                                                       "order" — a single
//                                                       call, no call_list
//                                                       (order_stmt allows
//                                                       2+ calls for a
//                                                       basket; a bracket
//                                                       save is always
//                                                       exactly one preset).
//                                                       `#name=` is
//                                                       effectively required
//                                                       (compile-time error
//                                                       if absent) since
//                                                       OrderTemplateCreate.
//                                                       name is a required
//                                                       field. Sprint 8. *)
//   agent_stmt     := when_part? tail meta_item*  (* WHEN, ALERT, and DO are
//                                                    each independently
//                                                    optional and may appear
//                                                    in any order; omitting
//                                                    WHEN means `always`;
//                                                    omitting a clause means
//                                                    the same as writing it
//                                                    as "nop"; at least one
//                                                    real ALERT or DO is
//                                                    still required — all-
//                                                    omitted/all-nop is a
//                                                    compile-time error.
//                                                    meta_item* is TRAILING
//                                                    ONLY — after `tail`
//                                                    finishes parsing, never
//                                                    interspersed with
//                                                    WHEN/ALERT/DO. Sprint 7. *)
//   when_part      := "WHEN" condition
//   tail           := alert_part do_part | do_part alert_part
//                    | alert_part | do_part | ε
//   alert_part     := "ALERT" alert_clause
//   do_part        := "DO" do_clause
//   alert_clause   := "nop" | (("msg" "=" value ",")? call_list)
//                                                   (* "msg" is NOT a
//                                                      reserved word — it
//                                                      is recognized
//                                                      CONTEXTUALLY, only
//                                                      as NAME "=" in the
//                                                      leading position of
//                                                      a non-"nop" ALERT
//                                                      clause, before any
//                                                      channel. Sprint 6. *)
//   do_clause      := "nop" | call_list
//   call_list      := call ("," call)*
//   order_stmt     := call ("," call)* meta_item*  (* order/place_order only;
//                                                      a bare order_stmt
//                                                      carrying any meta_item
//                                                      is parsed fine but a
//                                                      COMPILE-time error —
//                                                      meta tags only apply
//                                                      to agents/brackets.
//                                                      Sprint 7. *)
//   meta_item      := "#" NAME "=" meta_value ("," meta_value)*
//                                                   (* trailing-only — see
//                                                      agent_stmt/order_stmt
//                                                      above. NAME is one of
//                                                      the four recognized
//                                                      keys (name/desc/tag/
//                                                      group) — an unknown
//                                                      key is a compile-time
//                                                      error, not a parse
//                                                      error (same
//                                                      parse-permissively/
//                                                      validate-at-compile
//                                                      split every other
//                                                      production in this
//                                                      grammar follows).
//                                                      Sprint 7. *)
//   meta_value     := STRING | BARE_WORD           (* Sprint 7 *)
//   BARE_WORD      := (* an unquoted run of 1+ characters containing NONE
//                        of: whitespace, ",", "#", "=", or a quote
//                        character — quotes are required only once one of
//                        those appears in the value. DELIBERATELY WIDER
//                        than NAME's word-char class (letters/digits/
//                        underscore only) — e.g. "hedge-book" lexes as one
//                        BARE_WORD token (the hyphen is allowed) but would
//                        NOT lex as a single NAME token elsewhere in this
//                        grammar. Scanned by a dedicated branch in
//                        `lexAgentStatement` triggered by "#", not reused
//                        from the NAME/word-char scanner. Sprint 7. *)
//   condition      := "always" | or_expr
//   or_expr        := term (("&" | "|") term)*     (* equal precedence, L-to-R *)
//   term           := "~" primary | primary
//   primary        := "(" or_expr ")" | leaf
//   leaf           := metric_ref comparator value
//                    | col_ref comparator value                       (* Sprint 15 *)
//                    | value comparator (metric_ref | col_ref) comparator value   (* between *)
//                    | call                           (* Sprint 10 — a BARE
//                                                          call with no "@"
//                                                          following is
//                                                          syntactically a
//                                                          Token ($ref)
//                                                          reference — a
//                                                          real metric
//                                                          ALWAYS requires
//                                                          "@ scope_ref", so
//                                                          this is parsed
//                                                          permissively here
//                                                          and resolved at
//                                                          COMPILE time
//                                                          against the
//                                                          catalog (same
//                                                          split every other
//                                                          semantic check in
//                                                          this grammar
//                                                          follows): a name
//                                                          resolving to a
//                                                          catalog entry
//                                                          with
//                                                          `isFragmentRef`
//                                                          compiles to
//                                                          `{"$ref": name}`;
//                                                          a real metric
//                                                          missing its "@"
//                                                          still gets the
//                                                          original "expected
//                                                          '@' after metric"
//                                                          error, just at
//                                                          compile time
//                                                          instead of parse
//                                                          time. *)
//   metric_ref     := call "@" scope_ref
//   scope_ref      := scope_seg ("." scope_seg)*
//   scope_seg      := call | qualifier_seg     (* Sprint 19 — a qualifier_seg
//                        is trailing-only (never followed by a plain `call`
//                        segment); short scope-family aliases (`pos`/`hld`/
//                        `fnd`/`wl`, SCOPE_FAMILY_ALIASES) apply to
//                        scope_ref's FIRST `call` segment only. *)
//   qualifier_seg  := NAME ":" qualifier_value (* Sprint 19 — `account:value`/
//                        `symbol:value` appended to a scope chain, e.g.
//                        `positions.account:ZG0790`. Compiles to a leaf-level
//                        `scope_filter` dict (sibling of metric/scope/op/
//                        value — the REAL, already-shipped backend shape,
//                        commit 1a904a12), never folded into the `scope`
//                        string. `*`/`worst` are RESERVED values with NO
//                        `scope_filter` — pure aliases onto the EXISTING
//                        `<family>.any_acct`/`<family>.worst_acct`/
//                        `<family>.worst_symbol` scope tokens (zero new
//                        backend mechanism). See `_resolveScopeRefFull`'s
//                        own comment for the full resolution algorithm. *)
//   qualifier_value := "*" | NAME | STRING     (* "*" = wildcard (no filter);
//                        a bare NAME of `worst` (case-insensitive) is the
//                        "computed worst" sentinel; any other NAME/STRING is
//                        a literal filter value. *)
//   col_ref        := call ("." call)+        (* Sprint 15 — `leaf`'s OTHER
//                        alternative, with NO "@": a bare dotted chain,
//                        e.g. `positions.total.pnl`. Syntactically
//                        IDENTICAL to `scope_ref` (both are just
//                        `call ("." call)*`), but semantically distinct —
//                        `scope_ref` only ever appears after "@"; `col_ref`
//                        only ever appears as a leaf's LHS with no "@" at
//                        all. Disambiguation of which trailing segment(s)
//                        are the scope vs. the column happens at COMPILE
//                        time (`_resolveDotAccess`): try the WHOLE chain
//                        against known scope names first (this is what
//                        keeps `positions.expiring_today.mcx_unhedged`
//                        resolving as a SCOPE, not a column, when it's
//                        used standalone with no trailing column segment),
//                        falling back to "last segment is a column (or an
//                        existing bare zero-arg metric) on the scope
//                        resolved so far" only when the full chain isn't
//                        itself a known scope. *)
//   comparator     := "<" | "<=" | ">" | ">=" | "==" | "!=" | "in" | "not in"
//   value          := NUMBER | STRING | NAME | list_literal
//   list_literal   := "[" value ("," value)* "]"
//   call           := NAME | NAME "(" arg_list ")"   (* a NAME in CALL/TOKEN-
//                                                        REFERENCE position
//                                                        (this production,
//                                                        and the bare
//                                                        metric/scope/
//                                                        channel/action name
//                                                        case generally) may
//                                                        contain internal
//                                                        hyphens, mid-word
//                                                        only — Sprint 10,
//                                                        see lexAgentStatement's
//                                                        NAME-scanning branch
//                                                        — so a real seeded
//                                                        Token name like
//                                                        `notify-critical-
//                                                        trio` lexes as ONE
//                                                        token. Does NOT
//                                                        widen NAME
//                                                        elsewhere (kwarg
//                                                        KEYS are scanned by
//                                                        a wholly separate
//                                                        tokenizer,
//                                                        `tokenizeCallStyle`
//                                                        in engine.js, never
//                                                        touched here). *)
//   arg_list       := kw_arg ("," kw_arg)*          (* EVERY arg is keyword —
//                                                       including the first;
//                                                       no positional form
//                                                       exists in this
//                                                       grammar at all *)
//   kw_arg         := NAME "=" value
//   object_literal := "{" obj_kwarg ("," obj_kwarg)* "}"   (* Sprint 8 — a
//                                                       genuinely NEW value
//                                                       shape. GENERALIZED
//                                                       in Sprint 12 off its
//                                                       original STRICT
//                                                       scoping to the
//                                                       tp_scales_json_value
//                                                       production below —
//                                                       `event_stmt`'s
//                                                       `where=` (Sprint 12)
//                                                       is a SECOND call
//                                                       site reusing this
//                                                       EXACT SAME
//                                                       production/parser
//                                                       function
//                                                       (`parseObjectLiteral`)
//                                                       directly against the
//                                                       MAIN statement token
//                                                       stream (once "{"/"}"
//                                                       are lexed there too
//                                                       — see `_PUNCT_SINGLE`)
//                                                       rather than
//                                                       `tp_scales_json`'s
//                                                       own raw-substring
//                                                       mini-lexer. Sprint
//                                                       18 follow-up adds a
//                                                       THIRD call site —
//                                                       each entry of
//                                                       `match=[...]`'s own
//                                                       list (see
//                                                       `match_value` below)
//                                                       — and widens
//                                                       `obj_kwarg`'s own
//                                                       `value` to also
//                                                       accept a NESTED
//                                                       object_literal (a
//                                                       match entry's own
//                                                       `where={...}`),
//                                                       scoped to that one
//                                                       case only. Still
//                                                       NOT a general
//                                                       addition to the
//                                                       top-level `value`
//                                                       production anywhere
//                                                       else in this
//                                                       grammar. Reuses
//                                                       "=" as the key/value
//                                                       separator — this
//                                                       grammar's one and
//                                                       only keyword-
//                                                       assignment operator
//                                                       everywhere else — a
//                                                       deliberate decision
//                                                       NOT to introduce a
//                                                       second ":" operator
//                                                       just for this one
//                                                       nested case. *)
//   obj_kwarg      := NAME "=" value
//   tp_scales_json_value := "[" object_literal ("," object_literal)* "]"
//                                                    (* Sprint 8 — the ONLY
//                                                       legal value shape
//                                                       for a call argument
//                                                       named `tp_scales_json`
//                                                       (case-insensitive),
//                                                       on EITHER order(...)
//                                                       or bracket(...).
//                                                       Parsed by a
//                                                       dedicated mini
//                                                       lexer/parser (see
//                                                       `_parseTpScalesJsonArg`
//                                                       below) — NOT routed
//                                                       through
//                                                       tokenizeCallStyle,
//                                                       which has no
//                                                       "["/"]"/"{"/"}"
//                                                       awareness at all. *)
//
// ─────────────────────────────────────────────────────────────────────────
// AST shape (internal, documented for Sprint 3's decompiler / Sprint 4's
// live-preview — NOT the compiled JSON, see compile*() below for that)
// ─────────────────────────────────────────────────────────────────────────
//   Statement  := { kind:'agent', condition, alertCalls, alertMsg, doCalls, meta }
//               | { kind:'event', eventMeta, msgNode, calls: CallNode[], meta }
//                  (* Sprint 12 — `eventMeta` is `{ log_tag?, min_level?,
//                     render?, where? }`, each a ValueNode (object for
//                     `where`); `msgNode` is the optional clause-level
//                     `msg=` default CAPTURED only so `compileEventStmt`
//                     can reject it with a clear message (mirrors
//                     `alertMsg` above, but always an error here, never
//                     accepted); `calls` is the channel call_list,
//                     compiled via the same `compileAlertClause` path
//                     ALERT's own call_list uses. *)
//               | { kind:'order', calls: CallNode[], meta }
//               | { kind:'bracket', call: CallNode, meta }   (* Sprint 8 —
//                  exactly ONE call (never a list, unlike 'order') *)
//               | { kind:'token', head: CallNode, bodyCalls: CallNode[], meta }
//                  (* Sprint 10 — `head` is the `token(kind=...)` call;
//                     `bodyCalls` is the channel call_list compiled via the
//                     same path ALERT's own call_list uses. *)
//   alertMsg   := ValueNode | null   (* the optional clause-level `msg=`
//                  default — ALWAYS null when alertCalls is 'nop' or when
//                  no ALERT clause was written at all; DO has no equivalent
//                  field. Sprint 6. *)
//   meta       := MetaItem[]         (* [] when no trailing `#...` clause was
//                  written at all. Produced by `parseMetaClause` — a
//                  GENERIC, reusable helper (also usable by a future
//                  bracket_stmt). For `kind:'order'`, a non-empty `meta` is
//                  parsed fine but is a COMPILE-time error, not a parse
//                  error. Sprint 7. *)
//   MetaItem   := { key: string, values: MetaValueNode[], start }
//   MetaValueNode := { type:'string'|'bareword', value: string }
//   Condition  := { type:'always' }
//               | { type:'and'|'or', left: Condition, right: Condition }
//               | { type:'not', term: Condition }
//               | { type:'leaf', metric: CallNode, scope: ScopeNode,
//                   op: string|null, value: ValueNode|null }
//               | { type:'between', value1: ValueNode, op1: string,
//                   metric: CallNode, scope: ScopeNode, op2: string,
//                   value2: ValueNode }
//               | { type:'ref', call: CallNode }   (* Sprint 10 — a bare
//                   call with no "@" following; resolved at COMPILE time —
//                   see the `leaf` EBNF production's own comment above. *)
//               | { type:'colref', segments: CallNode[], op: string|null,
//                   value: ValueNode|null }   (* Sprint 15 — a bare dotted
//                   chain with NO "@" at all, e.g. `positions.total.pnl`.
//                   Parsed identically to `scope_ref`'s own "." call)*
//                   shape, but which segments are the SCOPE and which
//                   trailing segment is a COLUMN (or an existing bare
//                   zero-arg metric bound to that scope) is a COMPILE-time
//                   decision — see `_resolveDotAccess`'s "longest known-
//                   scope-chain match first" rule. Distinct from `leaf`
//                   (which always has an explicit "@") so the two can
//                   never be confused structurally. *)
//   CallNode   := { type:'call', name: string, args: ArgEntry[],
//                   hasCall: boolean, start, end }
//   ArgEntry   := { key: string, value: ValueNode }   (* key is NEVER null —
//                   every argument is keyword-only, including the first *)
//   ScopeNode  := { type:'scope', segments: (CallNode|QualifierNode)[] }
//                  (* Sprint 19 — a segment may now be a QualifierNode
//                     (`account:value`/`symbol:value`), always trailing. *)
//   QualifierNode := { type:'qualifier', key: string,
//                       value: { type:'star' } | { type:'name'|'string', value },
//                       start, end }
//   ValueNode  := { type:'number'|'string'|'name', value }
//               | { type:'list', value: ValueNode[] }
//               | { type:'object', value: { [key: string]: ValueNode } }
//                  (* Sprint 8 — object_literal; ONLY ever appears as an
//                     item inside a 'list' ValueNode that is itself the
//                     value of a `tp_scales_json=` argument — never
//                     produced anywhere else in this grammar. *)
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
const _RESERVED = new Set(['when', 'alert', 'do', 'nop', 'always', 'in', 'not', 'and', 'or', 'true', 'false']);
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
const _SCOPE_ALIAS_TO_REAL = Object.fromEntries(
  Object.entries(SCOPE_FAMILY_ALIASES).map(([real, alias]) => [alias.toLowerCase(), real])
);

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
    // Sprint 10 — true only for a merged-in AgentTemplate ("Token") row
    // (backend's GrammarTokenOut.is_fragment_ref) — see the file-header
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
 *  to the `tp_scales_json` object-literal-list parser when the segment's
 *  key matches (case-insensitive) and its value is bracket-shaped;
 *  otherwise tokenizes via the ORIGINAL Sprint 2 path (tokenizeCallStyle +
 *  `_parseArgSegment`), unchanged for every other argument. */
function _parseOneArgFromRawSegment(seg, labelToken) {
  const leadWs = seg.text.length - seg.text.replace(/^\s+/, '').length;
  const trimmed = seg.text.trim();
  if (trimmed === '') throw new _ParseError('empty argument (trailing/leading/double comma)', labelToken);
  const base = seg.start + leadWs;

  const m = _TP_SCALES_JSON_ARG_RE.exec(trimmed);
  if (m && m[1].toLowerCase() === 'tp_scales_json') {
    const bracketLocalIdx = trimmed.indexOf('[', m[1].length);
    const bracketText = trimmed.slice(bracketLocalIdx);
    const value = _parseTpScalesJsonArg(bracketText, base + bracketLocalIdx);
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
 *  `{ kind: 'colref', segments: CallNode[] }` or
 *  `{ kind: 'metric', metric: CallNode, scope: ScopeNode }`. */
function parseMetricOrColRef(state) {
  const first = parseCall(state);
  if (_peek(state).type === 'DOT') {
    const segments = [first];
    while (_peek(state).type === 'DOT') {
      _advance(state);
      segments.push(parseCall(state));
    }
    return { kind: 'colref', segments };
  }
  const at = _peek(state);
  if (at.type !== 'AT') throw new _ParseError(`expected "@" after metric '${first.name}'`, at);
  _advance(state);
  const scope = parseScopeRef(state);
  return { kind: 'metric', metric: first, scope };
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
    const segments = [firstCall];
    while (_peek(state).type === 'DOT') {
      _advance(state);
      segments.push(parseCall(state));
    }
    if (_isComparatorStart(_peek(state))) {
      const op = parseComparator(state);
      const value = parseValue(state);
      return { type: 'colref', segments, op, value };
    }
    return { type: 'colref', segments, op: null, value: null };
  }
  if (_peek(state).type !== 'AT') {
    // No "@" and no "." follows — a real metric ALWAYS requires
    // "@ scope_ref", so this can only be a bare Token ($ref) reference.
    // Deferred to COMPILE time (see `compileRef`) — parse permissively,
    // validate once the catalog is known, same split this grammar already
    // uses everywhere else.
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

/** `alert_clause := "nop" | (("msg" "=" value ",")? call_list)` — Sprint 6.
 *  ALERT-only (DO has no equivalent; `parseClauseBody` above is unchanged
 *  and still used for DO). The leading `msg="..."` default is recognized
 *  CONTEXTUALLY — "msg" is NOT in `_RESERVED` — purely by lookahead: a
 *  bare (non-call) NAME spelled "msg" (case-insensitive) immediately
 *  followed by a lone `EQ` token. A hand-typed channel that happens to be
 *  literally named "msg" but is NOT followed by "=" (e.g. a bare `msg`
 *  channel, or `msg(...)`, which lexes with `hasCall: true`) falls straight
 *  through to the ordinary `parseCallList` branch below — no reserved-word
 *  collision is possible. The value's STRING-ness is deliberately NOT
 *  enforced here (same "parse permissively, validate at compile time"
 *  split every other field in this grammar follows — see `parseValue`'s
 *  callers elsewhere); `compileAlertClause` rejects a non-string default
 *  with the same "must be quoted" wording other free-text fields use.
 *  @returns {{ msgDefault: any, calls: 'nop' | any[] }} */
function parseAlertClauseBody(state) {
  const tok = _peek(state);
  if (tok.type === 'NAME' && _wordIs(tok, 'nop')) {
    if (tok.hasCall) throw new _ParseError('"nop" takes no arguments — nop() is a parse error', tok);
    _advance(state);
    return { msgDefault: null, calls: 'nop' };
  }
  // Sprint 12 — log_tag=/min_level=/render=/where= are event_stmt-ONLY
  // reserved kwargs (see `_EVENT_META_KEYS`'s own comment). Hitting one
  // here means the operator combined them with WHEN/DO (this function is
  // only ever reached from the ordinary agent_stmt's ALERT clause, never
  // from a bare event_stmt — see `parseEventStmt`) — reject with a clear,
  // specific message instead of falling through to `parseCallList` (which
  // would choke on the unconsumed trailing "=" with a confusing generic
  // "unexpected trailing input" parse error) or, worse, silently parsing
  // as a bare channel named e.g. "log_tag" and failing later with a
  // confusing "unknown channel 'log_tag'" compile error.
  if (tok.type === 'NAME' && !tok.hasCall && _EVENT_META_KEYS.has(tok.value.toLowerCase())) {
    const evNext = state.tokens[state.pos + 1];
    if (evNext && evNext.type === 'EQ') {
      throw new _ParseError(
        `'${tok.value.toLowerCase()}=' is only valid on a bare ALERT-only statement, not combined with WHEN/DO`,
        tok,
      );
    }
  }
  let msgDefault = null;
  if (tok.type === 'NAME' && !tok.hasCall && tok.value.toLowerCase() === 'msg') {
    const next = state.tokens[state.pos + 1];
    if (next && next.type === 'EQ') {
      _advance(state); // consume "msg"
      _advance(state); // consume "="
      msgDefault = parseValue(state);
      const comma = _peek(state);
      if (comma.type !== 'COMMA') {
        throw new _ParseError('expected "," after msg="..." default, followed by a channel list', comma);
      }
      _advance(state);
    }
  }
  const calls = parseCallList(state);
  return { msgDefault, calls };
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

/** `agent_stmt := "WHEN" condition tail`
 *  `tail := alert_part do_part | do_part alert_part | alert_part | do_part | ε`
 *
 *  `ALERT` and `DO` are each independently optional (at most one of each,
 *  never both omitted — that's still a semantic error, checked later, same
 *  as the existing `ALERT nop DO nop` rejection) and may appear in EITHER
 *  order — `WHEN ... ALERT ... DO ...`, `WHEN ... DO ... ALERT ...`,
 *  `WHEN ... DO ...` alone, or `WHEN ... ALERT ...` alone are all valid.
 *  Omitting a clause entirely means the same thing as writing it as `nop`
 *  — both are accepted, `nop` is kept as explicit, backward-compatible
 *  sugar for the same empty state, not replaced by omission. A completely
 *  bare `WHEN condition` (tail = ε) is syntactically valid here; the
 *  "at least one of ALERT or DO" check happens at compile time, same as
 *  it always has — `alertCalls`/`doCalls` default to `'nop'` when their
 *  clause never appeared at all, so the compiler never needs to know
 *  "omitted" from "explicit nop" as a separate case. */
export function parseAgentStmt(state) {
  // WHEN itself is now optional (per operator instruction — an alert/action
  // should be placeable with no condition at all). Omitting it means the
  // same thing as writing it as `always` explicitly — both produce the
  // identical `{ type: 'always' }` AST node, so compileCondition/the rest
  // of this function never need to know "omitted" from "explicit always".
  let condition;
  if (_wordIs(_peek(state), 'when')) {
    _advance(state);
    condition = parseCondition(state, 0);
  } else {
    condition = { type: 'always' };
  }
  /** @type {'nop' | any[]} */
  let alertCalls = 'nop';
  /** @type {any} the clause-level `msg=` default ValueNode, or null — Sprint 6 */
  let alertMsg = null;
  /** @type {'nop' | any[]} */
  let doCalls = 'nop';
  let sawAlert = false;
  let sawDo = false;
  for (let i = 0; i < 2; i++) {
    const tok = _peek(state);
    if (!sawAlert && _wordIs(tok, 'alert')) {
      _advance(state);
      const body = parseAlertClauseBody(state);
      alertCalls = body.calls;
      alertMsg = body.msgDefault;
      sawAlert = true;
    } else if (!sawDo && _wordIs(tok, 'do')) {
      _advance(state);
      doCalls = parseClauseBody(state);
      sawDo = true;
    } else {
      break;
    }
  }
  // meta_item* — trailing-only, parsed AFTER the tail loop above completes.
  // See `parseMetaClause`'s own docstring for why no pre-pass is needed.
  const meta = parseMetaClause(state);
  return { kind: 'agent', condition, alertCalls, alertMsg, doCalls, meta };
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
function parseMatchValue(state) {
  const open = _peek(state);
  if (open.type !== 'LBRACKET') {
    throw new _ParseError('match= requires a list of {...} entries, e.g. [{tag=orders, min_level=WARNING}]', open);
  }
  _advance(state);
  const items = [];
  if (_peek(state).type !== 'RBRACKET') {
    items.push(parseObjectLiteral(state));
    while (_peek(state).type === 'COMMA') {
      _advance(state);
      if (_peek(state).type === 'RBRACKET') throw new _ParseError('trailing comma in match= list', _peek(state));
      items.push(parseObjectLiteral(state));
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

/** `token_stmt := call call_list meta_item*` — Sprint 10. A standalone
 *  `token(kind=...)` HEAD call — restricted to standalone use, EXACTLY
 *  mirroring `parseBracketStmt`'s own "only bracket(...) can be used
 *  standalone" rule/error-message style above, naming "token" instead —
 *  immediately followed by its BODY call_list (`telegram, email, log`,
 *  reusing `parseCallList` unchanged — the SAME production ALERT's own
 *  call_list uses). At least one body call is required (`parseCallList`
 *  never returns an empty array) — a Token with zero channels has no
 *  useful body. */
export function parseTokenStmt(state) {
  const head = parseCall(state);
  if (head.name.toLowerCase() !== 'token') {
    throw new _ParseError(
      'only token(...) can be used standalone — wrap other actions in a WHEN ... DO ... agent',
      { start: head.start },
    );
  }
  const bodyCalls = parseCallList(state);
  const meta = parseMetaClause(state);
  return { kind: 'token', head, bodyCalls, meta };
}

/** `statement := agent_stmt | order_stmt | bracket_stmt | token_stmt` —
 *  top-level entry point. Returns `{ ast, errors }` — `ast` is `null` on
 *  any parse failure. */
export function parseStatement(text) {
  const { tokens, errors: lexErrors } = lexAgentStatement(text);
  if (lexErrors.length) return { ast: null, errors: lexErrors };
  const state = { tokens, pos: 0 };
  try {
    // WHEN is optional (an agent_stmt may start directly with ALERT or DO —
    // see parseAgentStmt) — ALERT/DO are reserved words that can never be a
    // real order/place_order call name, so dispatching on them here is
    // unambiguous, same as WHEN always was. "bracket"/"token" are likewise
    // never real metric/scope/channel/action names, so dispatching a
    // leading `bracket(...)` (Sprint 8) or `token(...)` (Sprint 10) call
    // straight to its own standalone-statement parser is unambiguous too —
    // mirrors how WHEN/ALERT/DO dispatch to agent_stmt.
    //
    // Sprint 12 — event_stmt dispatch rule, PRECISE: a bare leading `ALERT`
    // (no `WHEN` before it) routes to `parseEventStmt` specifically when
    // its FIRST kwarg — before any bare channel name — is one of the five
    // reserved event-meta keys (`log_tag=`/`min_level=`/`render=`/`where=`/
    // `gate=`, see `_EVENT_META_KEYS`); this is checked via a 2-token lookahead
    // (NAME matching one of those keys, immediately followed by a lone
    // `EQ`) PAST the `ALERT` token itself. An ordinary bare-ALERT agent_stmt
    // (e.g. `ALERT telegram DO nop`) never matches this lookahead (no real
    // channel is named one of these five reserved words), so it falls
    // through to `isAgentStmt` unchanged.
    const first = _peek(state);
    const firstIsAlert = first.type === 'NAME' && !first.hasCall && _wordIs(first, 'alert');
    const evSecond = firstIsAlert ? state.tokens[state.pos + 1] : null;
    const evThird = firstIsAlert ? state.tokens[state.pos + 2] : null;
    const isEventStmt = firstIsAlert && evSecond && evSecond.type === 'NAME' && !evSecond.hasCall
      && _EVENT_META_KEYS.has(evSecond.value.toLowerCase()) && evThird && evThird.type === 'EQ';
    const isAgentStmt = !isEventStmt && first.type === 'NAME' && !first.hasCall
      && (_wordIs(first, 'when') || _wordIs(first, 'alert') || _wordIs(first, 'do'));
    const isBracketStmt = !isEventStmt && !isAgentStmt && first.type === 'NAME' && first.hasCall && _wordIs(first, 'bracket');
    const isTokenStmt = !isEventStmt && !isAgentStmt && !isBracketStmt && first.type === 'NAME' && first.hasCall && _wordIs(first, 'token');
    const ast = isEventStmt ? parseEventStmt(state)
      : isAgentStmt ? parseAgentStmt(state)
      : isBracketStmt ? parseBracketStmt(state)
      : isTokenStmt ? parseTokenStmt(state)
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
const _QUALIFIER_COLUMN_NAME = Object.freeze({ account: 'account_id', symbol: 'symbol' });
const _QUALIFIER_COLUMN_NAME_REVERSE = Object.freeze({ account_id: 'account', symbol: 'symbol' });

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
      errors.push(_err(`unknown scope qualifier '${q.key}:' — only 'account:'/'symbol:' are supported`));
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
    scopeFilter[column] = _qualifierValueText(q);
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
function _resolveDotAccess(segments, catalog, errors) {
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
function compileColRef(node, catalog) {
  const errors = [];
  const resolved = _resolveDotAccess(node.segments, catalog, errors);
  if (errors.length) return { conditions: null, errors };
  const conditions = _compileComparatorValue(resolved.metricStr, resolved.scopeStr, resolved.valueType, node.op, node.value, errors);
  if (errors.length) return { conditions: null, errors };
  return { conditions, errors: [] };
}

const _BETWEEN_FLIP = { '<': '>', '<=': '>=', '>': '<', '>=': '<=' };

function compileBetween(node, catalog) {
  const errors = [];
  let metricStr, scopeStr, valueType, scopeFilter;
  if (node.colSegments) {
    const resolved = _resolveDotAccess(node.colSegments, catalog, errors);
    if (errors.length) return { conditions: null, errors };
    ({ metricStr, scopeStr, valueType } = resolved);
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
      const coerced = _coerceValueForSpec(qtyVal, entry.paramsSchema.qty, 'qty', entry.token);
      if (coerced.error) { errors.push(coerced.error); return { params: null, errors }; }
      params.qty = coerced.value;
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
 *       real, existing agents. */
function compileAlertClause(alertCalls, alertMsg, catalog) {
  if (alertCalls === 'nop') return { events: [], errors: [] };
  const errors = [];

  let defaultMessage = null;
  if (alertMsg != null) {
    const r = _coerceValueForSpec(alertMsg, _MESSAGE_SPEC, 'msg', 'ALERT');
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
    const entry = catalog.channels.get(call.name.toLowerCase());
    if (!entry) {
      const other = _describeKind(call.name, catalog);
      errors.push(_err(other
        ? `'${call.name}' is ${other} — only channels are allowed in ALERT`
        : `unknown channel '${call.name}'`));
      continue;
    }

    // Sprint 10 — a bare Token reference (`isFragmentRef`) compiles to
    // `{"$ref": <token>}` directly, bypassing the own-message/params
    // machinery below entirely — a Token reference expands into OTHER
    // channels server-side (`template_registry.py:resolve_events`), so it
    // can never carry its own params or its own `msg=`.
    if (entry.isFragmentRef) {
      if (call.args && call.args.length) {
        errors.push(_err(`'${call.name}' is a Token reference and takes no arguments`));
        continue;
      }
      events.push({ '$ref': entry.token });
      continue;
    }

    const msgArgs = ownMsgArgsByCall[i];
    if (msgArgs.length > 1) {
      errors.push(_err(`${entry.token}: duplicate parameter 'msg'/'message'`));
      continue;
    }
    let ownMessage = null;
    if (msgArgs.length === 1) {
      const r = _coerceValueForSpec(msgArgs[0].value, _MESSAGE_SPEC, msgArgs[0].key.toLowerCase(), entry.token);
      if (r.error) { errors.push(r.error); continue; }
      ownMessage = r.value;
    }
    const strippedCall = { ...call, args: (call.args || []).filter((a) => !(a.key && _MESSAGE_KEYS.has(a.key.toLowerCase()))) };

    const r = _compileGenericActionParams(strippedCall, entry);
    errors.push(...r.errors);
    if (r.errors.length) continue;

    let resolvedMessage = null;
    if (ownMessage != null) {
      resolvedMessage = ownMessage;
    } else if (defaultMessage != null) {
      resolvedMessage = defaultMessage;
    } else if (anyMsgUsed) {
      errors.push(_err(`channel '${entry.token}' has no message — set msg= on ALERT or on this channel directly`));
      continue;
    }

    const event = { channel: entry.token, enabled: true, ...r.params };
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

function compileDoClause(doCalls, catalog, opts, rowRefCtx) {
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
      const r = _compileOrderActionParams(call, entry, opts, rowRefCtx);
      errors.push(...r.errors);
      if (!r.errors.length) actions.push({ type: 'place_order', params: r.params });
      continue;
    }
    const r = _compileGenericActionParams(call, entry, rowRefCtx);
    errors.push(...r.errors);
    if (!r.errors.length) actions.push({ type: entry.token, params: r.params });
  }
  return { actions: errors.length ? null : actions, errors };
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

function compileAgentStmt(ast, catalog, opts) {
  const errors = [];
  const condResult = compileCondition(ast.condition, catalog);
  errors.push(...condResult.errors);
  const eventsResult = compileAlertClause(ast.alertCalls, ast.alertMsg, catalog);
  errors.push(...eventsResult.errors);
  // Sprint 16 — matched-row ($row) binding context, built from THIS
  // statement's own compiled condition tree (every real leaf's `scope`,
  // see `_collectLeafScopes`) so `compileDoClause` can recognise/validate
  // bare-NAME action-param values against it. See the "Sprint 16" header
  // comment above `_resolveRowRefOrNull` for the full design.
  const leafScopes = new Set();
  const scopeState = { hasNegation: false, hasTokenRef: false };
  _collectLeafScopes(condResult.conditions, leafScopes, scopeState);
  const rowRefCtx = {
    catalog, leafScopes, allExposedNames: _allExposedColumnNames(catalog),
    hasNegation: scopeState.hasNegation, hasTokenRef: scopeState.hasTokenRef,
  };
  const actionsResult = compileDoClause(ast.doCalls, catalog, opts, rowRefCtx);
  errors.push(...actionsResult.errors);
  const metaFields = compileMetaClause(ast.meta, errors);
  if (ast.alertCalls === 'nop' && ast.doCalls === 'nop') {
    errors.push(_err('an agent must have at least one of ALERT or DO'));
  }
  if (errors.length) return { kind: 'agent', errors, agent: null };
  return {
    kind: 'agent', errors: [],
    agent: { conditions: condResult.conditions, events: eventsResult.events, actions: actionsResult.actions, ...metaFields },
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
  const entry = catalog.actions.get('place_order');
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
// `_APPLIES_TO_CHOICES` set (`backend/api/routes/templates.py:39`:
// {"buy_any", "sell_option", "sell_any", "both"}), which this enum had
// silently drifted out of sync with.
const _BRACKET_APPLIES_TO_SPEC = Object.freeze({ type: 'enum', enum: ['buy_any', 'sell_option', 'sell_any', 'both'] });

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
function compileBracketStmt(ast, catalog, opts = {}) {
  const errors = [];
  const call = ast.call;
  if (!call || call.name.toLowerCase() !== 'bracket') {
    errors.push(_err('only bracket(...) can be used standalone'));
    return { errors, template: null };
  }

  const { tpSlWingArgs, call: afterTpSlWing } = _splitTpSlWingArgs(call);
  const appliesToArgs = (afterTpSlWing.args || []).filter(a => a.key && a.key.toLowerCase() === 'applies_to');
  const otherArgs = (afterTpSlWing.args || []).filter(a => !(a.key && a.key.toLowerCase() === 'applies_to'));
  if (appliesToArgs.length > 1) errors.push(_err("bracket: duplicate parameter 'applies_to'"));
  for (const a of otherArgs) errors.push(_err(`bracket: unknown parameter '${a.key}'`));

  const placeOrderEntry = catalog && catalog.actions ? catalog.actions.get('place_order') : null;
  const tpSlWingResult = _compileTpSlWingFields(tpSlWingArgs, 'template', 'bracket', placeOrderEntry && placeOrderEntry.mutuallyExclusiveGroups);
  errors.push(...tpSlWingResult.errors);

  let applies_to;
  if (appliesToArgs.length === 1) {
    const r = _coerceValueForSpec(appliesToArgs[0].value, _BRACKET_APPLIES_TO_SPEC, 'applies_to', 'bracket');
    if (r.error) errors.push(r.error); else applies_to = r.value;
  }

  const metaFields = compileMetaClause(ast.meta, errors);
  if (!metaFields.name) {
    errors.push(_err("bracket: missing required meta tag '#name' — a saved bracket must have a name"));
  }

  if (errors.length) return { errors, template: null };

  const template = {
    ...metaFields,
    ...(applies_to !== undefined ? { applies_to } : {}),
    ...tpSlWingResult.fields,
  };
  return { errors: [], template };
}

const _TOKEN_KIND_SPEC = Object.freeze({ type: 'enum', enum: ['notify', 'condition'] });

/** Compile a standalone `token(kind=...)` statement to a
 *  `POST /api/admin/fragments`-shaped request object (Sprint 10) —
 *  `{kind, name, body, description?}`. `kind=notify` is the ONLY
 *  supported value this sprint — `kind=condition` is a clear compile
 *  error (edit it via the Structured tab instead). The body call_list is
 *  compiled via the EXACT SAME `compileAlertClause` path ALERT's own
 *  call_list already uses (no clause-level `msg=` default — `null` —
 *  since a Token body is a plain channel list, not an ALERT clause).
 *  `#name=` is required (`AgentTemplate.name` is a required field, same
 *  required-field style as `compileBracketStmt`'s own `#name=`
 *  requirement); `#tag=`/`#group=` are rejected outright — `AgentTemplate`
 *  has no such columns. */
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
  if (kind === 'condition') {
    errors.push(_err("condition-kind Token creation isn't supported from the CLI yet — edit it via the Structured tab"));
  }

  const bodyResult = compileAlertClause(ast.bodyCalls, null, catalog);
  errors.push(...bodyResult.errors);

  const metaFields = compileMetaClause(ast.meta, errors);
  if (metaFields.tags) errors.push(_err('token: #tag is not supported for a Token — Token has no tags field'));
  if (metaFields.group) errors.push(_err('token: #group is not supported for a Token — Token has no group field'));
  if (!metaFields.name) errors.push(_err("token: missing required meta tag '#name' — a saved Token must have a name"));

  if (errors.length) return { errors, fragment: null };

  const fragment = { kind, name: metaFields.name, body: bodyResult.events };
  if (metaFields.description) fragment.description = metaFields.description;
  return { errors: [], fragment };
}

/** Compile an already-parsed AST (from `parseStatement`) to its JSON
 *  request shape. Returns `{ kind, errors, agent, ticket, basket,
 *  template, fragment }` — non-empty `errors` means every payload field is
 *  `null`. */
export function compileStatement(ast, catalog, opts = {}) {
  if (ast.kind === 'agent') {
    const r = compileAgentStmt(ast, catalog, opts);
    return { kind: 'agent', errors: r.errors, agent: r.agent, ticket: null, basket: null, template: null, fragment: null };
  }
  if (ast.kind === 'event') {
    const r = compileEventStmt(ast, catalog);
    return { kind: 'event', errors: r.errors, agent: r.agent, ticket: null, basket: null, template: null, fragment: null };
  }
  if (ast.kind === 'bracket') {
    const r = compileBracketStmt(ast, catalog, opts);
    return { kind: 'bracket', errors: r.errors, agent: null, ticket: null, basket: null, template: r.template, fragment: null };
  }
  if (ast.kind === 'token') {
    const r = compileTokenStmt(ast, catalog, opts);
    return { kind: 'token', errors: r.errors, agent: null, ticket: null, basket: null, template: null, fragment: r.fragment };
  }
  const r = compileOrderStmt(ast, catalog, opts);
  return { ...r, template: null, fragment: null };
}

/** One-shot parse + compile — the main entry point for Sprint 4's UI and
 *  for this sprint's tests. Never throws; always returns a structured
 *  result. `opts.mode` ("paper"|"live") is required for a single bare
 *  order_stmt; `opts.lotSizeOf(symbol) -> number|null` overrides the
 *  default instruments-cache lot-size lookup (used by tests). */
export function compileAgentCliStatement(text, catalog, opts = {}) {
  const { ast, errors: parseErrors } = parseStatement(text);
  if (parseErrors.length) {
    return { ok: false, errors: parseErrors, kind: null, agent: null, ticket: null, basket: null, template: null, fragment: null };
  }
  const result = compileStatement(ast, catalog, opts);
  if (result.errors && result.errors.length) {
    return { ok: false, errors: result.errors, kind: result.kind, agent: null, ticket: null, basket: null, template: null, fragment: null };
  }
  return {
    ok: true, errors: [], kind: result.kind,
    agent: result.agent || null, ticket: result.ticket || null, basket: result.basket || null,
    template: result.template || null, fragment: result.fragment || null,
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
    // A bare-column metric has no "@" form at all, so a scope_filter
    // (which only ever renders as a qualifier suffix on the "@"-scope
    // side) has nowhere to attach — fail loud rather than guess at a
    // syntax this grammar doesn't have.
    if (node.scope_filter !== undefined) {
      errors.push(_err(`leaf '${node.metric}@${node.scope}': a scope_filter combined with a bare-column metric has no CLI representation yet`));
      return null;
    }
    const colKey = `${s.text}.${node.metric}`.toLowerCase();
    const colEntry = catalog.columns && catalog.columns.get(colKey);
    if (!colEntry) { errors.push(_err(m.error)); return null; }
    refText = colEntry.token;
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
export function decompileEvents(events, catalog) {
  if (!Array.isArray(events)) return { text: null, errors: [_err(`'events' must be an array, got ${JSON.stringify(events)}`)] };
  if (events.length === 0) return { text: 'nop', errors: [] };
  const errors = [];
  const rendered = [];
  for (const ev of events) {
    // Sprint 10 — a `{"$ref": "<name>"}` entry (a Token reference expanded
    // by `template_registry.py:resolve_events`) renders as the bare Token
    // name, same as `_decompileRef`'s condition-side mirror. Excluded from
    // the message-hoisting bookkeeping below entirely — a reference can
    // never carry params or its own `msg=` (see `compileAlertClause`'s
    // `isFragmentRef` branch), so it's tracked separately via `ref: true`.
    if (ev && typeof ev === 'object' && '$ref' in ev) {
      const name = ev['$ref'];
      if (typeof name !== 'string' || !name) {
        errors.push(_err(`'$ref' must be a non-empty string, got ${JSON.stringify(name)}`));
        continue;
      }
      const refEntry = catalog.channels.get(name.toLowerCase());
      if (!refEntry || !refEntry.isFragmentRef) {
        errors.push(_err(`unknown Token reference '${name}' referenced in stored events`));
        continue;
      }
      rendered.push({ ref: true, text: refEntry.token });
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
    const entry = catalog.channels.get(ev.channel.toLowerCase());
    if (!entry) { errors.push(_err(`unknown channel '${ev.channel}' referenced in stored events`)); continue; }
    const extraKeys = Object.keys(ev).filter(k => k !== 'channel' && k !== 'enabled' && k !== 'message');
    const argParts = [];
    let bad = false;
    for (const k of extraKeys) {
      const spec = entry.paramsSchema[k];
      if (!spec) { errors.push(_err(`event '${entry.token}': unknown parameter '${k}' (not declared in its catalog schema)`)); bad = true; continue; }
      const r = _renderParamValue(ev[k], spec);
      if (r.error) { errors.push(_err(`event '${entry.token}'.${k}: ${r.message}`)); bad = true; continue; }
      argParts.push(`${k}=${r.text}`);
    }
    let message; // undefined = no 'message' key at all (the normal case)
    if ('message' in ev) {
      if (typeof ev.message !== 'string') {
        errors.push(_err(`event '${entry.token}'.message: must be a string, got ${JSON.stringify(ev.message)}`));
        bad = true;
      } else {
        message = ev.message;
      }
    }
    if (bad) continue;
    rendered.push({ entry, argParts, message });
  }
  if (errors.length) return { text: null, errors };

  // Message hoisting (Sprint 6) only ever considers PLAIN channel entries —
  // a `ref: true` entry (Sprint 10) is excluded from both the numerator and
  // denominator below, since it can never carry a `message` at all.
  const plain = rendered.filter(r => !r.ref);
  const withMsg = plain.filter(r => typeof r.message === 'string');
  if (withMsg.length > 0 && withMsg.length < plain.length) {
    const missing = plain.find(r => typeof r.message !== 'string');
    return {
      text: null,
      errors: [_err(`channel '${missing.entry.token}' has no message while other channels in this ALERT do — this shape has no CLI representation`)],
    };
  }

  const uniform = withMsg.length === plain.length && plain.length > 0
    && plain.every(r => r.message === plain[0].message);

  let defaultPrefix = '';
  if (uniform) {
    const q = _quoteString(plain[0].message);
    if (q.error) return { text: null, errors: [_err(q.message)] };
    defaultPrefix = `msg=${q.text}, `;
  }

  const parts = [];
  for (const r of rendered) {
    if (r.ref) { parts.push(r.text); continue; }
    const argParts = [...r.argParts];
    if (!uniform && typeof r.message === 'string') {
      const q = _quoteString(r.message);
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

export function decompileAgent({ conditions, events, actions, name = undefined, description = undefined, tags = undefined, group = undefined }, catalog, opts = {}) {
  const condResult = decompileCondition(conditions, catalog);
  const eventsResult = decompileEvents(events, catalog);
  const actionsResult = decompileActions(actions, catalog, opts);
  const metaResult = decompileMetaClause({ name, description, tags, group });
  const errors = [...condResult.errors, ...eventsResult.errors, ...actionsResult.errors, ...metaResult.errors];
  const evLen = Array.isArray(events) ? events.length : null;
  const acLen = Array.isArray(actions) ? actions.length : null;
  if (evLen === 0 && acLen === 0) errors.push(_err('an agent must have at least one of ALERT or DO'));
  if (errors.length) return { text: null, errors };

  const metaSuffix = metaResult.text ? ` ${metaResult.text}` : '';
  const text = `WHEN ${condResult.text} ALERT ${eventsResult.text} DO ${actionsResult.text}${metaSuffix}`;
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
      `syntax yet; resaving via /console (create-only) would silently ` +
      `change this row's ${unexpressible.join('/')} instead of preserving ` +
      `it. Edit this field via the structured form at /automation instead.`
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

/** Nearest non-whitespace char before `pos` in `text` — used to detect
 *  "immediately after @ or ." (scope-ref position — see `_suggestScopeRef`
 *  below, Sprint 5) and to bias catalog lookup toward `catalog.scopes` for
 *  a call sitting right after "@"/".". */
function _nearestNonSpaceBefore(text, pos) {
  let i = pos - 1;
  while (i >= 0 && /\s/.test(text[i])) i--;
  return i >= 0 ? text[i] : '';
}

/** Determine which clause (when/alert/do/order) `tokens` (a token stream
 *  from lexing some PREFIX of the statement) currently ends in — the last
 *  WHEN/ALERT/DO reserved-word marker seen wins; no WHEN at all means a
 *  bare order_stmt. */
function _clauseKindFromTokenPrefix(tokens) {
  let kind = null;
  for (const t of tokens) {
    if (t.type !== 'NAME' || !t.reserved) continue;
    const w = String(t.value).toLowerCase();
    if (w === 'when') kind = 'when';
    else if (w === 'alert') kind = 'alert';
    else if (w === 'do') kind = 'do';
  }
  return kind || 'order';
}

/** Look up a call's catalog entry by name, trying the catalog section(s)
 *  implied by `clauseKind`/`scopeHint` first (disambiguation only — the
 *  grammar's token namespaces don't collide in practice). */
function _lookupAnyCatalogEntry(name, catalog, clauseKind, scopeHint) {
  if (!catalog) return null;
  const key = String(name).toLowerCase();
  let order;
  if (scopeHint) order = ['scopes', 'metrics', 'channels', 'actions'];
  else if (clauseKind === 'alert') order = ['channels', 'metrics', 'scopes', 'actions'];
  else if (clauseKind === 'do' || clauseKind === 'order') order = ['actions', 'metrics', 'scopes', 'channels'];
  else order = ['metrics', 'scopes', 'channels', 'actions'];
  for (const section of order) {
    const m = catalog[section];
    if (m && m.has(key)) return m.get(key);
  }
  return null;
}

function _candidateTokensForClause(clauseKind, catalog) {
  if (!catalog) return null;
  if (clauseKind === 'when') return [...catalog.metrics.values()].map((e) => e.token);
  if (clauseKind === 'alert') return [...catalog.channels.values()].map((e) => e.token);
  if (clauseKind === 'do' || clauseKind === 'order') return [...catalog.actions.values()].map((e) => e.token);
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
  else if (clauseKind === 'do' || clauseKind === 'order') section = catalog.actions;
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

  const scopePart = typedSoFar.slice(0, lastDot);
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
  return { suggestions: [], descriptions: [], labels: [], replaceRange: [cursor, cursor], kind: null };
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

/** Suggest param-name or enum-value completions for the call whose "("
 *  sits at `parenPos`, given everything typed so far up to `cursor`. */
function _paramOrEnumSuggestion(text, parenPos, cursor, entry) {
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
    if (!spec || spec.type !== 'enum' || !Array.isArray(spec.enum)) return _emptySuggest(cursor);
    const valueAbsStart = lastAbsStart + eqIdx + 1;
    const valuePartialRaw = text.slice(valueAbsStart, cursor);
    const valuePartial = valuePartialRaw.replace(/^['"]/, '');
    const ranked = fuzzyFilter(valuePartial.trim(), spec.enum);
    // No per-VALUE description exists anywhere in the backend schema
    // (agent_grammar.yaml / order_fields.yaml enum lists carry no
    // per-entry text, only the param itself does) — degrade to empty
    // strings rather than inventing text.
    return { suggestions: ranked, descriptions: ranked.map(() => ''), labels: ranked.map(() => null), replaceRange: [valueAbsStart, cursor], kind: 'enum' };
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

function _suggestAgentCliAtImpl(text, cursor, catalog) {
  if (typeof text !== 'string') return _emptySuggest(0);
  const n = text.length;
  const pos = Math.max(0, Math.min(Number(cursor) || 0, n));

  // 1. Inside a call's (possibly still-open) argument list?
  const openCall = _findOpenCallAt(text, pos);
  if (openCall) {
    const name = text.slice(openCall.nameStart, openCall.nameEnd);
    const scopeHint = _nearestNonSpaceBefore(text, openCall.nameStart) === '@'
      || _nearestNonSpaceBefore(text, openCall.nameStart) === '.';
    const prefixLex = lexAgentStatement(text.slice(0, openCall.nameStart));
    const clauseKind = prefixLex.errors.length ? null : _clauseKindFromTokenPrefix(prefixLex.tokens);
    const entry = _lookupAnyCatalogEntry(name, catalog, clauseKind, scopeHint);
    if (!entry) return _emptySuggest(pos);
    return _paramOrEnumSuggestion(text, openCall.parenPos, pos, entry);
  }

  // 2. Not inside a call — bare metric/channel/action NAME being typed.
  //    Lex only the text UP TO THE CURSOR so a half-typed later clause
  //    never blanks out suggestions for the clause actually being edited.
  const { tokens, errors } = lexAgentStatement(text.slice(0, pos));
  if (errors.length) return _emptySuggest(pos);
  if (!tokens.length) return _emptySuggest(pos);

  // Drop the synthetic EOF token lexAgentStatement always appends.
  const real = tokens[tokens.length - 1].type === 'EOF' ? tokens.slice(0, -1) : tokens;
  const lastTok = real[real.length - 1];

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

  const clauseKind = _clauseKindFromTokenPrefix(real);
  const candidates = _candidateTokensForClause(clauseKind, catalog);
  if (!candidates) return _emptySuggest(pos);

  let partial = '';
  let replaceStart = pos;
  if (lastTok && lastTok.type === 'NAME' && !lastTok.hasCall && lastTok.end === pos) {
    // Cursor glued to the end of a bare NAME — still typing it (or it's
    // a just-typed WHEN/ALERT/DO/NOP/ALWAYS keyword itself, never a fresh
    // metric/channel/action position).
    if (lastTok.reserved) return _emptySuggest(pos);
    partial = lastTok.value;
    replaceStart = lastTok.start;
  } else if (!_isFreshNamePosition(lastTok)) {
    // Gap position (whitespace, or right after a closed call's ")"/a
    // value/a comparator/etc.) — only a legal metric/channel/action slot
    // when the token immediately before it is one that the grammar
    // actually allows a call/NAME to follow: a WHEN/ALERT/DO clause
    // marker, a boolean connective (&/|/~), a grouping "(", or a
    // call-list "," — e.g. NOT right after a comparator or a value.
    return _emptySuggest(pos);
  }

  const ranked = fuzzyFilter(partial, candidates);
  const descMap = _descMapForClause(clauseKind, catalog);
  const descriptions = ranked.map((t) => (descMap && descMap.get(t)) || '');
  return { suggestions: ranked, descriptions, labels: ranked.map(() => null), replaceRange: [replaceStart, pos], kind: 'token' };
}

/** True when `lastTok` (the last REAL token lexed before the cursor, or
 *  `undefined` at the very start of the text) legally precedes a fresh
 *  metric/channel/action NAME per the grammar — see call site above. */
function _isFreshNamePosition(lastTok) {
  if (!lastTok) return true; // start of text — nothing typed yet
  if (lastTok.type === 'NAME' && lastTok.reserved) {
    const w = String(lastTok.value).toLowerCase();
    // Sprint 20 (item 3) — 'and'/'or'/'not' legally precede a fresh
    // metric/scope name too, same as 'when'/'alert'/'do' already did,
    // now that they're accepted word aliases for '&'/'|'/'~'.
    return w === 'when' || w === 'alert' || w === 'do' || w === 'and' || w === 'or' || w === 'not';
  }
  return lastTok.type === 'AMP' || lastTok.type === 'PIPE' || lastTok.type === 'TILDE'
    || lastTok.type === 'LPAREN' || lastTok.type === 'COMMA';
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
export function suggestAgentCliAt(text, cursor, catalog) {
  try {
    return _suggestAgentCliAtImpl(text, cursor, catalog);
  } catch {
    return _emptySuggest(Number(cursor) || 0);
  }
}
