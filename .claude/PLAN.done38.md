# Plan: Unify the DSL grammar — `WHEN...DO...` as a generic, optional conditional wrapper around any statement; everything outside it is unconditional ("given") by default; `alert` becomes an ordinary call, never a reserved keyword; add `help` introspection; structurally exempt DEBUG from the alert pipeline

## Context

This grew out of a conversation reconciling two systems that both get
casually called "agents" and both involve "alerts" in this codebase:
**cycle agents** (trading-condition agents — `conditions` evaluated against
live market/position/account data) and **event agents**
(`event_agents.py`'s `SEEDED_AGENTS` — `conditions` matched against tagged
log records, dispatched to Telegram/ntfy/email). Both are literally the
same `Agent` DB row/model, differentiated only by `Agent.kind` (`"cycle"`
vs `"event"`) — not a separate architectural concept, which is the root of
the original naming confusion this conversation set out to resolve.

**What's already correct, confirmed by investigation — no change needed:**
the standalone top-level `alert` CLI statement (`alert log_tag=..., render=...,
telegram`) is already a genuinely separate, working statement kind,
compiling to its own `kind='event'` Agent row via `compileAlertClause`. It
already matches the operator's own framing: *"alert is a separate statement
like order."*

**The actual, narrower starting problem**: an `agent` statement's own
grammar had a clause literally named `ALERT` — `GIVEN <bindings/debug>
[WHEN <condition>] [ALERT <alert_clause>] [DO <do_clause>]`
(`agents.js:2714-2800`, documented at `docs/specs/AGENTS_SPEC.md:536`) —
compiling the embedded clause through the exact same `compileAlertClause`
the standalone statement uses. Writing an `agent` statement meant its own
syntax *referred to* "alert" as a reserved sub-keyword.

**That narrow fix generalized, over the course of the conversation, into
the real target: there is no separate "agent" statement kind at all.**
`order`, `bracket`, `alert`, `help`, `debug`, and label-assignment are all
just **statements**, drawn from one shared vocabulary. Any statement,
written bare, is **unconditional** — it executes immediately ("given" by
default, with no keyword required to say so). Wrapping one or more
statements in `WHEN <condition> DO <calls>` is the **one generic,
optional** mechanism for making them conditional instead. What used to be
called "an agent" is nothing more than a `WHEN...DO...`-wrapped bundle of
calls — not a 4th category alongside order/bracket/alert, but the same
grammar's conditional form.

**External research** (TradingView Pine Script, MQL5, QuantConnect, Drools,
Zapier, IFTTT, n8n, AWS EventBridge, Picolabs KRL) found no system with a
dedicated "who gets told, how" grammar stage distinct from "what to do" —
the dominant pattern is one unified post-condition clause where a
notification send is just an ordinary call, not a separately-typed grammar
position. Drools' `when`/`then` was the closest precedent for the
conditional wrapper shape; **the operator chose to keep the existing `DO`
keyword rather than adopt `THEN`** — no new keyword introduced for the
action stage.

**A critical audit found the preamble's "runs unconditionally, every time"
premise was false at the actual engine level — resolved, see Design item 0.**
A dispatched audit (code-grounded, not speculative) found that a preamble
call with no `WHEN` compiles to an always-true sentinel condition, gated by
the SAME 30-minute cooldown every ordered-condition agent uses
(`agent_engine.py:1680`) — meaning a saved cycle agent with `order(...)` in
its preamble would place a real broker order roughly every cooldown window,
indefinitely, not once as the plan originally (incorrectly) promised. This
is resolved by Design item 0 below: side-effecting preamble calls fire
**once per agent activation**, a genuinely new execution path, not a reuse
of the existing cooldown-gated `actions` array `debug(...)` currently
piggybacks on. **This execution-semantics question only applies to a
statement that gets SAVED as a persistent `Agent` row and polled
repeatedly** — a bare statement typed in the CLI and executed immediately
(no save step) just runs once, synchronously, exactly as `order(...)`/
`cancel_all_orders(...)` already do today as one-shot CLI statements. None
of Design item 0's machinery is reachable from that path at all.

## Design

### 0. Execution semantics — saved agents vs. CLI one-shot statements

**Two entirely different execution models share one grammar; this item
defines the boundary precisely, since item 1's "runs unconditionally"
framing is only safe once this is pinned down.**

**A. CLI one-shot execution (no `Agent` row created).** An operator types
any statement — `order(...)`, `debug(...)`, a full `a = 1 when (...) do
...` statement, whatever — into the console and it compiles and executes
immediately, once, synchronously. This is the existing, unchanged behavior
for every current bare CLI statement (`order`, `bracket`, standalone
`alert`, `cancel_all_orders`). Nothing in this plan changes it. A `WHEN`
condition in a CLI-executed statement is evaluated once, against the
current live state, at the moment of execution — not re-polled.

**B. Saved `Agent` row execution (`kind='cycle'`, re-evaluated every poll
cycle by `agent_engine.py:run_cycle()`).** This is where the preamble's
"unconditional" semantics need real definition, split by statement type:
- **`debug(...)`/`help(...)`/label-assignment** — run on EVERY poll cycle,
  unconditionally, exactly as the plan's original framing describes. No
  change from the earlier draft. These are read-only/diagnostic; repeating
  them every cycle is the explicit point (dynamic state, re-observed each
  time).
- **Side-effecting calls** (`order`, `close_position`, `cancel_order`,
  `bracket`) **fire exactly once, at agent activation** — not on every
  cycle, not cooldown-gated-and-repeating. "Activation" = the agent
  transitioning into an actively-polled state: first save-and-enable, or
  re-enable after being paused/disabled. A dedicated per-agent flag (new
  state, e.g. `Agent.preamble_fired_at` or an in-process/DB marker checked
  at the top of `_cycle_evaluate_agent`) must be checked BEFORE the
  preamble's side-effecting calls run, and set immediately after, so a
  normal subsequent poll cycle skips them entirely. This is **genuinely new
  engine work** in `agent_engine.py`'s cycle-evaluation path, not a reuse
  of the existing `actions`-array hoisting mechanism `debug(...)` uses
  today — that mechanism is cooldown-gated-but-repeating, which is the
  wrong shape for a real broker action.
- **`alert(...)` in the preamble** — runs every cycle, same bucket as
  `debug`/`help` (it's a notification call, not a broker action — no
  real-money risk from repetition, and an operator may legitimately want
  an ongoing per-cycle notice). Confirm this reads naturally once item 1's
  `alert`-as-ordinary-call design is in front of the implementing agent —
  flagged here as the current intent, not an unexamined default.

**C. Producer-log migration** (resolved: DSL-driven with automatic
fallback). Today, `_v2_send_rich_alert()` fires automatically on every
cycle-agent breach, completely independent of the agent's `DO` clause
content — a hardcoded engine behavior, not a DSL statement. Going forward:
if the matched agent's `DO` clause contains an explicit `alert(...)` call,
that call's own compiled `events`/render config fires INSTEAD of the
automatic rich-alert (full operator control: wording, channel, priority).
If `DO` has no `alert(...)` call at all (true for every currently-saved
agent, since explicit `alert(...)` didn't exist as a DO-callable action
before this plan), the engine falls back to today's automatic
`_v2_send_rich_alert()` behavior, unchanged. **This is the backward-
compatibility guarantee**: no currently-saved agent's alerting behavior
changes the moment this ships — only agents an operator explicitly updates
to add a `DO`-level `alert(...)` call get the new, overridable behavior.
Implementation: `_cycle_evaluate_agent`/wherever breach-handling currently
unconditionally calls `_v2_send_rich_alert()` needs an explicit check —
"did this match's compiled `DO` calls include an `alert` action?" — before
deciding which path to take. Confirm the exact detection mechanism (a flag
on the compiled action list? a dedicated marker in `Agent.actions`?)
against the real compiled-agent shape during implementation.

### 1. The unified grammar, confirmed with the operator's own examples

**Core rule**: anything that comes **before `WHEN`** is assumed to be part
of the unconditional preamble ("given") — including when there is no
`WHEN` at all, in which case the entire statement sequence is unconditional.
`WHEN`/`DO` are explicit and optional — when present, they must be paired
(both appear together, wrapping the conditional calls). **`GIVEN` is
removed from the grammar completely** — final decision (superseding an
earlier, more cautious draft of this plan that kept it as optional
backward-compatible syntax). It never carried semantic meaning beyond
marking where the preamble started, and the preamble is now self-delimiting
by position, so there's nothing left for the keyword to do:
```
a = 1 when (a==1) do close_position(...)
```
is the one and only valid form — the old `given a = 1 when (a==1) do
close_position(...)` is no longer valid syntax at all once this lands (see
the migration note under Implementation shape for what that means for any
already-saved agent source text).

**The preamble vocabulary is NOT a restricted subset — it is the full
shared grammar, no narrower than what's allowed inside `DO`.** This is a
correction to an earlier, narrower draft of this plan, which had tried to
limit the preamble to just label-assignments/`debug`/`help`. The operator
confirmed explicitly: `alert`, `bracket`, and `order` statements can all be
used in the preamble too, with no `given` keyword needed, exactly like
`debug`/`help`/label-assignments:
```
order(...)
alert(...)
debug("checking avail_margin before firing")
help(pnl)
a = 1
when (a==1) do close_position(...)
```
Every line before `when` here runs unconditionally, every time; only
`close_position(...)` is gated on the condition. There is, in the end, only
**one** statement vocabulary in the whole grammar (`order`, `bracket`,
`alert`, `help`, `debug`, label-assignment, `close_position`, `cancel_order`,
`set_flag`, etc.) and only **one** thing that distinguishes "runs
unconditionally" from "runs only on match": whether a statement sits inside
an explicit `WHEN ... DO ...` pair or not. `alert(...)` is an **ordinary
call** wherever it appears — preamble or `DO` — never a reserved keyword,
which is what actually satisfies "agent should never refer to alert":
there is no `ALERT` keyword left anywhere in the grammar.

### 2. Implementation shape

**Preamble (implicit "given")** — the parser's statement-sequence loop
(today only entered after matching the literal `GIVEN` token, and today
restricted to label-assignments/`debug(...)`) becomes the **default entry
point** for the full statement grammar: it consumes ANY statement (label
assignment, `debug(...)`, `help(...)`, `order(...)`, `bracket(...)`,
`alert(...)`, etc. — the same call vocabulary `DO` already accepts) by
lookahead, stopping at `WHEN` (or consuming the entire remaining input if
`WHEN` never appears). Since the preamble now accepts the same call
vocabulary `DO` does, the parser can likely share one
"parse-a-statement-list" implementation between the two positions rather
than maintaining two separate accept-lists — confirm this during
implementation rather than duplicating the vocabulary definition.

**`GIVEN` is removed from the grammar completely — not kept as optional
syntax.** Confirmed explicitly, final decision: unlike `WHEN`/`DO` (which
stay as real, meaningful reserved keywords), `GIVEN` carried no semantic
meaning even before this plan — it was purely a marker introducing the
preamble, and the preamble is now self-delimiting by position (bounded by
`WHEN`, or end-of-input), so the marker has nothing left to do. It is
dropped from `_RESERVED` and from the parser entirely, not kept as
accepted-but-optional. **Migration implication — confirmed, no action
needed.** Investigated directly (model inspection + grep, not assumed):
`Agent.cli_source` (`backend/api/models.py:1033`) is additive, nullable
text, documented in its own code comment as "nothing reads it yet" — it is
written on create/update for provenance/audit display only and is never
re-parsed or re-executed by anything. Confirmed three ways: (1) backend —
every execution path (`agent_engine.py` cycle evaluation, `event_agents.py`
dispatch) reads only the compiled `conditions`/`events`/`actions`/`given`
JSONB fields, never `cli_source`; (2) frontend edit flow — `updateConsoleAgent`
(`consoleDispatch.js:470-491`) compiles a FRESH `rawText` on every save and
writes it as the new `cli_source`; it never loads or re-parses the OLD
stored value; (3) frontend display — a repo-wide grep (`grep -rln
"cli_source" frontend/src --include="*.svelte"`) returns zero matches, so
no UI component today even shows `cli_source` back to the operator in an
editable field (the only consumer is `AgentInfo.cli_source` in GET
responses, read by tests/API consumers, not rendered into any edit box).
**Net effect: removing `GIVEN` requires no migration of any existing saved
`Agent` row.** An old agent's stored `cli_source` may still literally
contain the word `GIVEN` as a historical artifact — that string is inert,
display-only text that nothing will ever re-parse, so it is harmless to
leave as-is. `OrderTemplate` (Bracket) has no `cli_source`-equivalent field
at all (grepped, zero matches) — brackets store only structured fields, so
there is nothing to migrate there either. The same reasoning covers the
`ALERT`-keyword removal (item 1) and the whole grammar-unification change:
every already-saved Agent/Bracket keeps executing from its already-compiled
JSON exactly as it does today, regardless of what the DSL's surface syntax
looks like going forward.

**Conditional wrapper (`WHEN ... DO ...`)** — stays a paired, optional,
explicit construct. The old separate `ALERT`/`DO` clause split collapses
into one `DO <calls>` list; a call whose action is `alert` still routes
through the existing `compileAlertClause` → `events` field under the hood
(reusing that implementation is fine and intended — only the **DSL-surface
keyword** `ALERT` is removed, not the channel-dispatch machinery it
compiles to).

**A preamble with no `WHEN...DO...` at all is a complete, valid, standalone
statement on its own — not a fragment awaiting a conditional tail.**
Confirmed explicitly: `order(...)` / `debug(...)` / `help(pnl)` / several
such lines in sequence / any mix of them, with nothing else, must parse to
completion successfully at end-of-input. The parser must NOT require or
expect a trailing `WHEN`/`DO` to consider the statement well-formed — the
whole conditional wrapper is optional as a unit, not merely "optional but
expected eventually." This is the same case already shown in the bare-list
examples above, called out here explicitly because it's easy for a parser
implementation to accidentally treat the preamble as a prefix that must be
followed by something rather than a genuinely complete statement in its own
right.

**Files** (`frontend/src/lib/command/grammars/agents.js`):
- `parseAgentStmt` (`:2714-2800`) — rework into: parse the implicit
  preamble (now the FULL statement-call vocabulary, not a restricted
  accept-list) by lookahead, then an optional `WHEN <condition> DO <calls>`
  pair using the same statement-list parser. Collapse the current
  `alertCalls`/`alertMsg`/`doCalls` AST fields into one `doCalls` list
  (shared shape with the preamble's own call list).
  `_RESERVED` (`:667`): `ALERT` and `GIVEN` are both removed entirely; `DO`
  and `WHEN` remain exactly as today.
- `compileAgentStmt` (`:5450`, `~5473`) — compile every call in both the
  preamble and `doCalls` uniformly through the same per-call compiler
  (including `help`, item 3, and `alert`/`order`/`bracket`, each routed to
  whatever its existing standalone-statement compiler already does — reuse,
  don't reimplement).
- Confirm the existing statement-separator convention inside today's
  `GIVEN <bindings/debug>` list (comma? newline? both?) and preserve it
  exactly for the now-broadened preamble — this plan does not invent a new
  separator syntax.
- `docs/specs/AGENTS_SPEC.md:536`'s documented grammar updates to:
  `<preamble: any statement>* [WHEN <condition> DO <calls>]`, with the
  order/alert/bracket-in-preamble example included verbatim as the
  canonical illustration, and an explicit migration note that the old
  `GIVEN <bindings/debug> [WHEN ...] [ALERT ...] [DO ...]` four-clause form
  is no longer valid syntax.

### 3. New `help` action — `_NOOP_ACTION_HANDLERS` only (no broker call)

Copy the exact 3-piece pattern `deactivate_agent`/`set_flag` already use:

**YAML row** (`backend/config/grammars/agent_grammar.yaml`, same block as
other `action_type` entries, near `deactivate_agent`/`set_flag` around line
1273):
```yaml
- grammar_kind: action
  token_kind: action_type
  token: help
  value_type: void
  description: Look up a token/function/operator's catalog description, parameter signature, and docstring — logged at DEBUG, never alert-eligible.
  resolver: backend.api.algo.actions.help_lookup
  params_schema:
    name:
      type: string
      required: true
```
(`help` as a Python function name collides with the builtin — resolver
function is named `help_lookup`, matching this codebase's existing
convention of YAML `token` differing from its Python symbol when needed.)

**Handler** (`backend/api/algo/actions.py`,
`async def help_lookup(ctx, params: dict) -> dict:`):
1. Reverse-resolve `params["name"]` to its `GrammarToken` row(s) — add a
   small helper on `GrammarRegistry` (`grammar_registry.py`), since
   `self.tokens` is keyed by DB id, not token string. Simplest correct
   approach (no new cache/invalidation surface): iterate
   `self.tokens.values()` filtering `.token == name` — this is an
   on-demand diagnostic call, not a hot path; do not over-build a reverse
   index. If more than one row matches (a name reused across
   `token_kind`s), include all matches rather than guessing which was
   meant.
2. For each matching row, resolve the bound callable via the matching
   `REGISTRY` accessor keyed by `token_kind` (`.metric()`, `.scope()`,
   `.op()`, `.action()` [returns a dict, read `["fn"]`], `.channel()`,
   `.fmt()`, `.template()`).
3. Reflect: `inspect.getdoc(fn)` for the docstring, `inspect.signature(fn)`
   for the parameter shape — wrapped in try/except (a resolver can be
   `None` for a known-dead stub per this codebase's existing "7 dead
   grammar-resolver stub functions" precedent, or non-introspectable).
4. Compose the DEBUG log line from all three pieces — catalog
   `description`, signature, docstring — **never hide a missing piece**;
   render explicit "(no catalog description)" / "(signature unavailable)"
   rather than silent omission, so a documentation gap stays visible
   (explicit operator instruction).
5. `logger.debug(...)` — **no `extra={"tags": [...], "alert_event": ...}`
   at all**. Belt-and-suspenders on top of item 4's structural guard, not
   the real protection.

**Dispatch table entry** (`actions.py`, next to `deactivate_agent`/`set_flag`
in `_NOOP_ACTION_HANDLERS`): `"help": _HandlerRef(__name__, "help_lookup", True),`

### 4. CLI bare-statement: `help(<name>)`

Confirmed small addition, following the recently-added `cancel_all_orders`
bare-statement template exactly (`frontend/src/lib/command/grammars/agents.js`):
- New `isHelpStmt` dispatch condition alongside `isCancelAllOrdersStmt`
  (~line 3130-3137).
- New `parseHelpStmt` (mirror `parseCancelAllOrdersStmt`, ~line 2983-3008)
  — single `NAME` arg, no WHEN clause, no live/paper mode gate (read-only,
  safe in any mode).
- New `compileHelpStmt` (mirror `compileCancelAllOrdersStmt`, ~line
  6196-6228) — reuse `_compileGenericActionParams` against the `help`
  action's `params_schema` from item 3, same as every other action.
- Wire into `compileStatement`'s `ast.kind === 'help'` branch (~line
  6249-6279).
- **CLI surface scope — confirmed by code, corrects an earlier draft of this
  item.** There are two distinct CLI mounts of the same `agents.js` grammar,
  separated by the compiler's own `allowedKinds` option
  (`compileStatement`), not a UI-level restriction: `OrderCliTab.svelte`
  (`_ALLOWED = ['order', 'cancel_all_orders']`, shared by the `/orders` CLI
  tab and the SymbolPanel order-ticket modal's CLI tab) is deliberately
  gated to order-fleet operations only — its own header comment says so
  explicitly ("never an agent/bracket/token authoring scope, which stays
  restricted to Automation → Console"). `ConsoleTab.svelte`
  (`/automation?tab=console`) is the one unrestricted mount — no
  `allowedKinds` — where any statement kind (order/bracket/alert/agent/
  token/event) is authored today. `help(<name>)` is a read-only catalog
  lookup, not an order-fleet operation (`cancel_all_orders` earned its spot
  in `OrderCliTab`'s allow-list specifically because it's order-management,
  per that same comment) — **it is wired into `ConsoleTab.svelte`/
  `consoleDispatch.js` only, NOT into `OrderCliTab.svelte`.** Do not widen
  `OrderCliTab`'s `_ALLOWED` list for this plan.
- Frontend submit wrapper in `frontend/src/lib/command/consoleDispatch.js`
  (mirror `submitConsoleCancelAllOrders`, ~line 283-292), wired into
  `ConsoleTab.svelte`'s submit handler only.
- **Backend**: reuse the `_NOOP_ACTION_HANDLERS["help"]` resolution path
  directly — do NOT invent a parallel implementation. A thin new endpoint
  (e.g. `POST /api/agents/help`) calls the same `help_lookup(ctx, params)`
  function the grammar action dispatch uses, returning its result as JSON
  for the Console tab to render inline (an operator typing `help pnl` wants
  to SEE the answer immediately, not just have it logged). Confirm whether
  `ConsoleTab.svelte` already has a pattern for "render an inline
  synchronous result" vs. "fire and poll the log" before choosing between a
  direct response and a log-then-poll approach.

### 5. Structural DEBUG exemption — `event_agents.py:_dispatch_agent`

Confirmed exact insertion point: inside the `for rec in records:` loop
(`event_agents.py` line ~561), **as the first statement, before**
`if not evaluate(agent.conditions, ...)`:
```python
for rec in records:
    if rec.get("level") == "DEBUG":
        continue
    if not evaluate(agent.conditions, Context(log_records=[rec])):
        continue
    ...
```
Runs inside `_dispatch_agent`, called once per agent — applies uniformly to
every `kind='event'` agent (seeded AND any future operator-authored one),
not a hardcoded list. `"DEBUG"` string compare matches the existing rank
table in `agent_evaluator.py` (`_LEVEL_NO = {"DEBUG": 10, "INFO": 20, ...}`)
— reuse that constant rather than a bare literal if convenient. This
protects both `help` (item 3) and bare `debug(...)` (confirmed already
working today, compiling to `emit_log`) from ever reaching a real
notification channel, regardless of what any event agent's own conditions
claim to match.

**Also verify** (don't assume): does `debug(...)` actually tag its log
record at `level="DEBUG"` today? If it logs at a different level, that's a
separate, pre-existing correctness gap this plan's structural guard would
newly expose — note it explicitly either way, don't assume.

### 7. Rename `emit_log` → `log` — our own token, no migration risk, confirmed

Operator request: keep DSL words simple/small — `log`, not `emit_log`.
Confirmed via live DB check (`ssh ramboq`, both prod `ramboq` and dev
`ramboq_dev`): **zero existing `Agent` rows contain `emit_log` anywhere in
their compiled `actions`/`events`/`given` JSON**, on either environment —
it's never been used operator-side (the YAML's own description calls it "a
quiet action for testing agent wiring"). This is our own internal grammar
token, not an external/broker-mandated name, so there is no backward-
compat constraint from outside the codebase either. **Net: a clean rename,
no migration script, no backward-compat dispatch alias needed** — nothing
live depends on the old string.

**Scope of the rename** (confirmed by repo-wide grep before writing this):
- `backend/config/grammars/agent_grammar.yaml`: `token: emit_log` →
  `token: log` (keep `description`; `resolver:
  backend.api.algo.actions.emit_log` stays pointing at the same Python
  function — the dotted resolver path does not need to match the DSL
  token name, same precedent as `help`/`help_lookup` in item 3, so the
  Python function itself is NOT renamed, minimizing the diff).
- `backend/api/algo/actions.py`: `_NOOP_ACTION_HANDLERS` dict key
  `"emit_log"` → `"log"` (the dispatch key MUST match the grammar token
  string exactly, since that's how dispatch resolves it) —
  `_HandlerRef(__name__, "emit_log", True)` keeps its function-name
  argument unchanged.
- `frontend/src/lib/command/grammars/agents.js`: every place `debug(x)`
  compiles its action literal (`action: { type: 'emit_log', ... }`,
  currently 3 sites per the grep — `:5217`, `:5230`, `:5233`) → `type:
  'log'`. The decompiler's reverse check (recognizing an `emit_log`-typed
  action with `level: 'debug'` as sugar-compiled `debug(x)`, for
  round-tripping back to source) must also be updated to match on `'log'`
  — confirm every such match site via grep, don't assume only the 3
  compile sites exist.
- Tests referencing the literal string `"emit_log"` as a DSL-visible
  token/action-type (not as a Python function name) — confirmed present
  in: `agentsGrammar.test.js`, `agentsDecompiler.test.js`,
  `agentsSuggest.test.js`, `describeTokenAt.test.js`,
  `consoleDispatch.test.js`, `test_order_place_order_unification.py`,
  `test_grammar_registry.py`, `test_watchlist_live_path_and_quote_fanout.py`,
  `test_actions_agent_kind_context.py`, `test_agent_grammar_yaml_catalog.py`,
  `test_actions_coverage.py` — update each to `"log"`. Any reference to the
  retained Python function name `actions.emit_log` (not the token string)
  is left unchanged.
- Docs: `docs/specs/AUTOMATION_SPEC.md`, `docs/guides/AGENTS_GUIDE.md`,
  and `CLAUDE.md`'s own "Agent action-dispatch SSOT" entry (which lists
  `emit_log` by name in its noop-handler list) — update the DSL-visible
  name to `log`.
- Item 5's structural DEBUG exemption text above ("compiling to
  `emit_log`") is corrected by this item to "compiling to `log`" —
  resequence item 5 after item 7 during implementation if that reads more
  naturally, or just fix the reference; no functional dependency either
  way.

**`debug(x)` stays purely diagnostic — it is only for debugging, full stop
(operator, explicit, reconfirming item 5, not a new rule).** `debug(x)`
always compiles to `log(level="debug", message=...)` and nothing else —
no priority, no urgency, no extra params; the sugar form stays exactly
one bare argument. Item 5's structural exemption (a `DEBUG`-level log
record is skipped as the FIRST line of `event_agents.py:_dispatch_agent`'s
loop, before any agent's `conditions` are even evaluated) is what makes
this a hard guarantee rather than a convention: a `debug(...)` call can
never reach a real notification channel, regardless of what params a
future version of `log` grows, because `_dispatch_agent` throws the record
away before any of those params are ever read.

**`log(...)`, hand-typed with a non-debug level, grows real alert-metadata
params** (operator request — "log should have options to add urgency,
info level, priority"). This is new scope beyond a bare rename. Extended
`params_schema` (`backend/config/grammars/agent_grammar.yaml`):
```yaml
- grammar_kind: action
  token_kind: action_type
  token: log
  value_type: void
  description: Write a message to the app log, with optional alert-routing metadata for any downstream event agent that matches it.
  resolver: backend.api.algo.actions.emit_log
  params_schema:
    level:
      type: enum
      enum: [info, warning, error, critical]
      default: info
    message:
      type: string
      required: true
      token_ref_ok: true
    priority:
      type: enum
      enum: [low, default, high, urgent]
      required: false
      description: ntfy-routing priority for a downstream event agent that matches this record. Reuses the existing ntfy priority vocabulary (alert_utils.py's `_TIER_TO_NTFY_PRIORITY` value set), not a new one.
```
Still excludes `debug` from the hand-typed `level` enum — unchanged from
today's behavior, `debug` is reachable only via the `debug(x)` sugar, never
by hand-typing `log(level=debug, ...)`. `critical` is new (today's enum
stops at `error`); add it only if a real use case needs a level above
`error` for routing purposes — confirm during implementation rather than
assuming, since `priority` already carries the routing signal independent
of `level`.
**Mechanism, reusing existing plumbing, not inventing new dispatch**: `log`
remains a producer-side action — it writes a tagged log record, it does
not itself dispatch anywhere (same "producer decides what happened, the
agent decides who gets told and how" split `ALERT_AGENTS_DESIGN.md`
already documents). The new `priority` param is attached to the log
record's `extra` (same mechanism `_v2_send_rich_alert()` already uses to
thread a cycle-agent's real `tier` through to `RICH_ALERT_AGENT`, fixed
under "Rich-alert ntfy priority must come from the REAL triggering agent's
tier" in CLAUDE.md). A `kind='event'` agent matching this log record reads
`record.priority` (when present) in `_dispatch_agent`'s per-channel ntfy
resolution, the same place that already resolves priority from a tracked
`tier` — `priority` on the record takes precedence over a tier-derived
default when both are present, since it's the more specific, explicit
signal. This is the generalization of item 0.C's `alert(...)`-overrides-
automatic-rich-alert design to hand-authored `log(...)` calls too: an
explicit per-call signal always wins over an inferred default.
**backend-test addition**: a hand-typed `log(level=warning,
priority=urgent, message=...)` call resolves to `urgent` ntfy priority on
dispatch, overriding what the matching event agent's own `tier` would
otherwise resolve to; a `log(...)` with no `priority` falls back to the
existing tier-derived resolution unchanged (zero regression for every
current log-matching event agent); `debug(x)`'s compiled record is
confirmed to carry no `priority` field at all (sugar never accepts one).

### 6. Docs / examples

- `docs/specs/AGENTS_SPEC.md` — rewrite the grammar documentation per item
  2 (the new `<preamble>* [WHEN <condition> DO <calls>]` grammar, `GIVEN`
  no longer part of it); add `help` to the action vocabulary with a usage
  example.
- `docs/specs/ALERT_AGENTS_DESIGN.md` — already has the right producer/
  alert language; add: the DEBUG-exemption invariant explicitly; `help` as
  the canonical DEBUG-tier example; and a short note on the unified-grammar
  shape (no more `ALERT` keyword, no more "agent" as a 4th statement kind —
  it's the `WHEN...DO...` conditional form of the one shared grammar)
  referencing the external-DSL research for why no 5th "post-processing"
  stage was introduced.
- CLAUDE.md — new entry once this lands, per the standing doc-sync workflow
  (not pre-written here).

## Agents

- **backend (engine)**: item 0 — the once-per-activation execution path for
  side-effecting preamble calls, and the producer-log DSL-driven-with-
  fallback migration. Both touch `agent_engine.py`'s cycle-evaluation
  internals (`_cycle_evaluate_agent`, breach handling around
  `_v2_send_rich_alert()`) — genuinely new engine logic, not a grammar-only
  change. Sequence this agent FIRST and independently — items 1-2's
  compiler work in `agents.js` produces the AST/compiled shape this agent's
  runtime logic consumes (which preamble calls are side-effecting vs.
  diagnostic, whether `DO` contains an explicit `alert`), so confirm the
  exact compiled shape against what item 1-2 actually ships, don't assume
  it from this plan's prose alone.
- **frontend**: items 1-2 (grammar unification in `agents.js` — parser +
  compiler + `AGENTS_SPEC.md` grammar doc update) and item 4 (CLI `help`
  wiring) — same file family, one agent, sequential commits (grammar
  unification first, since `help`'s CLI wiring is more natural to add
  against the already-unified shape).
- **backend**: item 3 (`help` action), item 5 (DEBUG exemption +
  `debug(...)` level verification), and item 7 (`emit_log` → `log` rename
  + the new `priority` param + its dispatch-resolution wiring) — single
  agent, same files (`actions.py`, `grammar_registry.py`,
  `event_agents.py`, `agent_grammar.yaml`) — sequential commits, item 7
  before item 5 so item 5's exemption text references the final `log`
  name rather than `emit_log`.
- **doc**: item 6, after both land.
- **backend-test**: pytest covering — item 0's once-per-activation logic
  (a saved agent with an `order(...)` preamble call fires exactly once on
  activation, NOT on a second/third poll cycle — the direct regression
  test for the audit's P1 finding); the producer-log fallback (an agent
  with NO `alert(...)` in `DO` still gets the automatic `_v2_send_rich_alert()`
  behavior unchanged; an agent WITH an explicit `alert(...)` gets that
  instead, not both); `help_lookup` resolves a metric/scope/operator/
  action/channel/format/template correctly; renders all 3 pieces even when
  one is missing, with explicit "(unavailable)" text, never silent
  omission; multiple-match case; the DEBUG exemption itself (a DEBUG-level
  record with `alert_event` set to match a real seeded agent's condition
  must NOT dispatch — the regression test that would have failed before
  item 5); `help`'s own log call carries no `tags`/`alert_event` extra;
  `debug(...)`'s actual logged level, whatever it's found to be; item 7's
  `log(level=..., priority=..., message=...)` resolves to the given
  `priority` on dispatch (overriding the matching event agent's own
  tier-derived default), a `log(...)` with no `priority` keeps today's
  tier-derived resolution unchanged, and `debug(x)`'s compiled record
  carries no `priority` field.
- **playwright**: e2e for — a bare unconditional CLI statement (`debug(...)`/
  `help(...)`/`order(...)`/`alert(...)`/`bracket(...)` with no WHEN/DO,
  typed and executed once, immediately, no save step) parsing and executing
  correctly; a preamble with MULTIPLE such statements ahead of a
  `when (...) do ...` tail in a CLI-executed statement, confirming every
  preamble statement fires and only the `DO` call is gated; a preamble-only
  statement (no `WHEN`/`DO` anywhere) parsing to completion as a standalone
  statement; the CLI `help(<name>)` statement end-to-end (type it, see
  inline result); confirm a literal `GIVEN` token is no longer accepted by
  the parser (negative test, not a no-op/backward-compat test). (The
  once-per-activation and producer-log-fallback behaviors are saved-agent/
  backend-engine concerns, covered by backend-test above, not playwright —
  they require multiple simulated poll cycles, not a single page
  interaction.)

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
`feat(grammar): unify the DSL — WHEN...DO... becomes a generic optional conditional wrapper around any statement, bare CLI statements (including order/bracket/alert) execute immediately, saved-agent preambles split debug/help/alert (every cycle) from order/close_position/bracket (once per activation), GIVEN removed from the grammar entirely, ALERT is no longer a reserved keyword, producer breach-alerts migrate to DSL-driven with automatic fallback, add help introspection action, rename emit_log to log and give it an explicit priority param, structurally exempt DEBUG from the alert pipeline`

## Done when
- A bare CLI statement (`order`, `bracket`, `alert`, `help`, `debug`,
  label-assignment), typed and executed with no save step, executes
  immediately and unconditionally — proven by test for every one of these
  six.
- In a SAVED cycle agent's preamble: `debug`/`help`/`alert`/
  label-assignment run on every poll cycle; `order`/`close_position`/
  `cancel_order`/`bracket` fire exactly once, at agent activation, and do
  NOT re-fire on subsequent poll cycles — proven by a test that simulates
  multiple poll cycles for the same active agent, not just a single
  evaluation. This is the direct regression test for the audit's confirmed
  P1 finding.
- A cycle agent with an explicit `alert(...)` in `DO` has that call's own
  config fire instead of the automatic `_v2_send_rich_alert()`; a cycle
  agent with no `alert(...)` in `DO` keeps today's automatic behavior
  completely unchanged — proven by test for both cases, confirming zero
  regression for every currently-saved agent.
- `GIVEN` is not part of the grammar at all — `a = 1 when (a==1) do ...` is
  the only valid form; a literal `GIVEN` token is confirmed rejected by a
  negative test, not merely "not required."
- A statement consisting entirely of preamble (one or several `order`/
  `alert`/`bracket`/`debug`/`help`/label-assignment lines, no `WHEN`/`DO`
  at all) parses to completion as a fully valid, standalone statement — the
  parser never requires or expects a trailing conditional wrapper.
- No `ALERT` keyword exists anywhere in the grammar — `alert(...)` is
  callable inside `DO` exactly like `order`/`close_position`/`help`.
- `help(<name>)` works both as a typed CLI statement and as an ordinary
  call inside `DO` or the unconditional preamble, returning description +
  parameter signature + docstring, explicit about any missing piece.
- A DEBUG-level log record can never reach any event agent's dispatch,
  regardless of that agent's own `conditions` — proven by a regression
  test, not just code reading. `debug(x)` has no way to set a `priority`
  — the exemption above makes it moot regardless, but the sugar form
  itself accepts none.
- `emit_log` no longer exists as a DSL-visible token anywhere (grammar
  YAML, dispatch table key, compiled action-type strings, tests, docs) —
  renamed to `log`, confirmed zero existing saved `Agent` rows depended on
  the old string (live DB check, no migration needed). `log(level=...,
  priority=..., message=...)` accepts the new `priority` param
  (`low`/`default`/`high`/`urgent`, reusing the existing ntfy vocabulary);
  a matching event agent's ntfy dispatch uses an explicit `priority` when
  present, falling back to its own tier-derived default otherwise —
  proven by test for both branches.
- Confirmed (no migration step required): no existing saved `Agent` or
  `OrderTemplate` row needs any change when `GIVEN`/`ALERT` are removed
  from the grammar — execution reads only compiled `conditions`/`events`/
  `actions`/`given` JSON, `cli_source` is write-only/display-only and never
  re-parsed, and no frontend component renders `cli_source` into an
  editable field. See Design item 0/2's "Migration implication" note.
- pytest green, svelte-check 0 errors, playwright green, CC gate clean.

---

## Explicitly deferred (not in this plan's scope)

**The Bracket/Template terminology consistency pass** (surveyed separately,
inventory ready: ~10 operator-facing occurrences across
`template_attach.py`, `event_agents.py` titles/Agent-row `name` values, and
`USER_GUIDE.md`/`ADMIN_GUIDE.md` headings+prose — DB/API/identifier names
explicitly untouched) — independent of this plan, ready to scope as its own
plan whenever the operator wants it.

**The Kite `default-long-option` seed template bug** (`templates_seed.py:67-82`
— `tp_order_type: "MARKET"` hardcoded into the system default bracket for
every BUY-option scope; Kite's `place_gtt` rejects any MARKET leg; this is
the exact defect the 2H cap-warning was built to warn about, but the seed
template itself is still broken) — live-impact DB check was interrupted
mid-investigation; still needs resolution (confirm impact, then fix the
seed to LIMIT) as its own small, urgent, separate fix — not bundled here.
