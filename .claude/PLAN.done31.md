# Plan: Sprint 16 — matched-row fan-out for DO actions (HIGH RISK — live order-dispatch change)

## Task

Read the FULL "Sprint 16" section of `~/.claude/plans/purrfect-marinating-pixel.md`
before starting — extensively negotiated, multiple syntax revisions,
real risk analysis already done. This PLAN.md summarizes but does not
repeat all of it.

**The problem**: `agent_engine.py:_ae_dispatch_survivor_entry` (~line
2673) currently does:
```python
if agent.actions:
    action_ctx = dict(context)
    action_ctx["account"] = "TOTAL"   # hardcoded, discards which row matched
    action_ctx["sim_mode"] = sim_mode_p
    await execute(agent, agent.actions, action_ctx)
```
`agent.actions` fires EXACTLY ONCE per tick regardless of how many rows
matched the condition, with `account` hardcoded to `"TOTAL"`. The
evaluator's real per-row `matches`/`latched_matches` list (each entry
carrying `account` + the full underlying `row` dict, including
`tradingsymbol`/`qty`/etc.) is discarded before actions fire.

**The fix, operator-confirmed**: fan out — fire `agent.actions` ONCE
PER MATCHED ROW in `entry['latched_matches']` (confirm this is the
correct escalation-gated set to iterate, not the full raw `matches` —
read `_cycle_maybe_buffer_fire` again to confirm which list actually
represents "what really fired this tick" after escalation gating), with
`action_ctx["account"]` set to THAT row's real account, and the row's
other columns made available for bare-NAME resolution inside the DO
action's arguments (built on Sprint 15's exposed-column registry,
already shipped).

**Bare-NAME syntax, FINAL, confirmed after 4 design revisions this
session** (no `matched.` prefix, no leading dot — just the plain column
name, e.g. `close_position(account=account_id, symbol=symbol)`). Safe
because a bare unquoted NAME is ALREADY a compile error today for
string/number typed params (`_coerceValueForSpec`'s existing "free-text
value must be quoted"/"expected a number" errors) — claiming this slot
for row-binding has zero collision with the existing "bare NAME = enum
constant" convention, since enum-typed fields (`side`, `order_type`)
are never the kind of field anyone would bind from a matched row.

**Backward-compatibility guarantee, the single most important
correctness property of this sprint**: an agent whose condition scope
is AGGREGATE (`positions.total`, `funds.any_acct`-summed) produces
exactly ONE synthetic match today — fan-out over that single match MUST
be byte-identical to today's behavior (fires once, `account="TOTAL"`).
Only per-row scopes (`positions.any_acct`, `positions.expiring_today`,
etc.) change from "fires once with TOTAL" to "fires once per matching
row" — this is the actual, intended behavior change, scoped ONLY to
scopes that were already structurally per-row.

## Agents

- backend:
  1. **Fan-out in `_ae_dispatch_survivor_entry`**: replace the single
     `execute()` call with a loop over `entry['latched_matches']` (or
     whichever list is confirmed correct per above), one `execute()`
     call per entry, with `action_ctx["account"]` set to that entry's
     real `account` value (falling back to `"TOTAL"` ONLY when the
     entry represents an aggregate/synthetic match — confirm exactly
     how to detect this: likely `account in (None, 'TOTAL')` already
     distinguishes a real per-row account from the aggregate case,
     mirroring `_all_matches_account_keyed`'s existing check in
     `agent_evaluator.py`).
  2. **Thread the matched row into `action_ctx`** so bare-NAME
     resolution (frontend-compiled action params naming a real exposed
     column) can resolve against it at execution time. The exact
     mechanism: `action_ctx["_matched_row"] = entry.get('row')` (or
     however `resolve_action_params`/`actions.py` already threads
     per-call context — read that file's real signature first). Build
     a small resolver: when an action param's value (at the
     `resolve_action_params` layer, `backend/api/algo/actions.py:391`)
     is a bare column name that doesn't resolve as a literal/expr/
     token_ref, check whether it matches an exposed column name for
     this agent's condition scope (via Sprint 15's
     `grammar.exposed_columns_for_scope()`) and read it off
     `action_ctx["_matched_row"]`. Confirm exactly where this
     resolution needs to happen — likely the FRONTEND already compiles
     `account=account_id` to some marker value the backend needs to
     resolve (coordinate with the frontend agent's exact compiled JSON
     shape — dispatch backend first, state your chosen marker shape
     clearly, frontend agent builds against it, same sequencing
     discipline as Sprint 15).
  3. **Dry-run/simulator fan-out**: find wherever an isolated single-
     agent "run in simulator" path exists (grep `bypass_suppression`'s
     other call sites) and confirm it also fans out N times for N
     matched rows, not just the live path — an operator testing an
     agent in the simulator should see this behavior preview correctly.

  **Test coverage (mandatory, HIGH BAR given the risk)**:
  - Backward-compat regression: take 3+ REAL seeded/example agents
    using an AGGREGATE scope (`positions.total`, `funds.any_acct`-style)
    and prove their action-fire count and `action_ctx["account"]` value
    are BYTE-IDENTICAL before and after this change (one test per real
    agent shape, not synthetic fixtures only).
  - New behavior: a per-row-scoped agent (`positions.any_acct` or
    `positions.expiring_today`-style) with 3 synthetic matching rows
    across 2 different accounts fires `execute()` exactly 3 times, each
    with the correct distinct `account`/row data.
  - A per-row-scoped agent with exactly 1 matching row still fires
    exactly once (not zero, not duplicated) — boundary case.
  - A per-row-scoped agent with ZERO matching rows fires zero times
    (already true today via the `if not matches: return False` early
    exit — regression guard that fan-out didn't break this).
  - Simulator/dry-run path fans out identically to the live path for
    the same synthetic scenario.

  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -k "agent_engine or dispatch or evaluator" -q --tb=line`, then the FULL suite (`venv/bin/pytest backend/tests/ -q --tb=line`) given the blast radius — do not skip the full run for this sprint.

- frontend: (dispatch AFTER backend reports its exact marker/resolution
  shape for bare-NAME action params — do not guess)
  1. Teach the `DO` clause's action-argument value resolver (in
     `frontend/src/lib/command/grammars/agents.js`) to accept a bare,
     unquoted NAME for a string/number-typed param when it matches a
     real exposed column name for the agent's own condition scope
     (reuse Sprint 15's column-catalog lookup, now consulted from the
     action-compile side instead of only the WHEN-leaf side). Compile
     it to whatever marker shape the backend agent specified.
  2. **Compile-time guard**: using a bare column-name reference (e.g.
     `account_id`, `symbol`) inside a `DO` clause whose agent condition
     scope is AGGREGATE (no real per-row identity) is a clear compile
     error — "`account_id` requires a per-row condition scope
     (any_acct/expiring_today/...), not positions.total" — never a
     silent pass-through.
  3. Decompile support: an existing agent's action params referencing
     the matched-row marker shape decompile back to the bare column
     name in CLI text.

  **Test coverage (mandatory)**:
  - `WHEN pnl_pct@positions.any_acct <= -5 DO
    close_position(account=account_id, symbol=symbol)` compiles
    correctly to the backend's confirmed marker shape.
  - Same statement but with `WHEN ... @positions.total` (aggregate
    scope) → compile error naming the field and the reason.
  - Decompile round-trip for an agent using this marker shape.

  Run:
  ```
  cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -30
  cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -30
  ```

## Tests
- pytest: yes — run the FULL suite for this sprint specifically, not
  just a filtered subset, given the live-dispatch blast radius.
- svelte-check: yes
- playwright: no (no new UI surface; this is a dispatch-engine +
  compiler change)

## Commit message
feat(agents): Sprint 16 — fire DO actions once per matched row (fan-out), bare-NAME row binding

## Done when
- Every currently-shaped aggregate-scope agent fires exactly as it did
  before this change — proven via real-shape regression tests, not
  just reasoning.
- A per-row-scoped agent with N matching rows fires its action N times,
  each bound to the correct row's account/column data.
- `close_position(account=account_id, symbol=symbol)` inside a per-row-
  scoped agent resolves to the correct live values at execution time.
- The same syntax inside an aggregate-scoped agent is a clear compile
  error, never a silent `None`/`"TOTAL"`.
- Full backend test suite (whole thing, not filtered) green, plus
  frontend vitest/svelte-check green.
