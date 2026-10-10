# Plan: Agent CLI grammar — Sprint 12, notification (event-kind) agents, ALERT-only

## Task

`kind='event'` agents (the seeded notification-dispatch agents —
ERROR_AGENT, CHASE_CANCEL_AGENT, PARTIAL_GTT_AGENT, 15 total in
`event_agents.py:SEEDED_AGENTS`) are semantically ALERT-only — their
"condition" is a log-stream subscription filter (tag + min_level +
optional `where`), not a polled metric condition; their "action" is
always exactly one `{"type":"render","render":"<name>"}`, never a
trading action. Give them their own bare-`ALERT`-only statement shape
instead of forcing them into `WHEN...DO...`.

**Confirmed frontend-only — verified by reading the actual code, not
grepped line numbers:**
- `create_agent`/`update_agent` (`backend/api/routes/agents.py:779-827`,
  `:847-909`) both branch `if kind == "event": _age_validate_event_spec(...)
  else: _age_validate_action_entries(...)` — the generic action-catalog
  validator is correctly skipped for event-kind, confirmed symmetric across
  both create and update (update re-validates the merged row, so editing
  an existing event agent through this same validator works too).
- `_age_validate_threshold_conditions()` runs unconditionally afterward but
  is a verified no-op for event-kind conditions: it calls
  `is_grammar_tree(conditions)` first (`agent_engine.py:514-523`), which
  returns `False` for a `{"log": {...}}` shape (no `$ref`/`all`/`any`/`not`,
  no `metric`+`scope` keys) — so it returns immediately without validating.
- Capability: `manage_own_agents` guards both `POST /api/agents/` and
  `PUT /api/agents/{slug}` — the SAME capability as every other agent kind,
  not designated-only. Console's trader-level access is sufficient.
- Dispatch-cache invalidation on create AND update for kind='event'
  (`agents.py:819-825`, `:905-907`) forces `event_agents._cache["at"] =
  -inf`, so a freshly created/activated row is picked up immediately, not
  after the 60s cache TTL.
- The live dispatch loader query is `select(Agent).where(Agent.kind ==
  "event", Agent.status == "active")` (`event_agents.py:441`) — it reads
  ANY active event-kind row from the DB by kind, not a hardcoded seeded
  list. A CLI-created event agent fires once activated, same as any
  seeded one.

No new backend route, no backend code change. `submitConsoleAgent`/
`slugifyAgentName` (`consoleDispatch.js`, Sprint 9) already solve slug
derivation from `#name=` with a clear 409-on-collision — reuse directly.

**One lightweight backend CONTRACT TEST is still required** (ties the
frontend's compiled payload shape to what the real endpoint accepts —
this is new test code, not new product code, so `pytest: yes` below is
for this one file only): POST the exact JSON the example statement in
this plan compiles to, against a real/test DB session, assert 201, then
assert `event_agents`'s own query picks the row up once activated. This
is the only thing that actually proves the frontend's chosen field names
(`tag`/`min_level`/`where` inside `conditions.log`, `render` inside
`actions[0]`) match `validate_seed_spec()`'s real expectations — don't
skip it just because no backend code changed.

**Finalized syntax**:
```
event_stmt := "ALERT" event_meta call_list meta_item*
event_meta := event_kw*            (* log_tag=, min_level=, render=, where=
                                       — ANY ORDER, each at most once;
                                       duplicate key is a parse error *)
event_kw   := ("log_tag" "=" value ",")
            | ("min_level" "=" value ",")
            | ("render" "=" value ",")
            | ("where" "=" object_literal ",")
```
Example:
```
ALERT log_tag=error, min_level=ERROR, render=error, telegram, email, log #name="Error alert"
```
- `log_tag`/`render` are REQUIRED — compile error if either missing.
  `min_level` defaults to `"INFO"` if omitted (confirm this default
  against `validate_seed_spec`'s own default before assuming — read it,
  don't guess). `where` is optional.
- **Named `log_tag`, deliberately NOT `tag`** — avoids colliding/confusing
  with Sprint 7's trailing `#tag=` meta key (agent-level free-form tags).
  If BOTH `log_tag=error` and `#tag=risk,ops` appear in one statement,
  that's valid and means two different things (filter vs. free-form
  label) — no conflict, but don't let implementation conflate them.
- **Casing — asymmetric, be careful**: `min_level` normalizes to
  UPPERCASE (`error` → `ERROR`) to match the enum
  `DEBUG/INFO/WARNING/ERROR/CRITICAL`. `log_tag`, `render`, and every
  value inside `where={...}` are preserved EXACTLY as typed, NOT run
  through the enum-casing normalization every other bare NAME in this
  grammar gets — `where` matches against real log-record `extra` fields
  by exact string equality server-side, so re-casing it would silently
  break matches.
- **`msg=` is rejected on event_stmt, explicitly** — the renderer owns
  the message body for an event agent (that's the whole point of
  `render=`); Sprint 6's `msg=` leading-kwarg mechanism must NOT carry
  over when reusing the channel call_list compiler. If `msg=` appears
  anywhere in an `event_stmt`'s channel list, raise a clear compile error
  ("msg= doesn't apply to event agents — set render= instead"), don't
  silently accept or silently drop it. Relatedly: compiling the channel
  list for an event_stmt must not require every channel to resolve a
  message (unlike ALERT's normal per-channel message-inheritance rule) —
  event-agent channels carry no message at all, the renderer supplies it.
- **`where=` object-literal syntax must be generalized, not duplicated.**
  Sprint 8 added object-literal parsing (`{key=value}`) SCOPED only to
  `tp_scales_json` — read how that was implemented and lift the
  restriction so `where=` can reuse the exact same parser/compiler
  function rather than copy-pasting a second one. If the existing
  implementation hardcoded "only valid for tp_scales_json" as a gate,
  remove that gate (make it a generic reusable object-literal production),
  don't add a parallel code path.
- **Dispatch rule, precise**: a statement starting with bare `ALERT` (no
  `WHEN` before it) routes to `event_stmt` specifically when its first
  kwarg — before any bare channel name — is `log_tag=`, `min_level=`,
  `render=`, or `where=`. These names are reserved for this purpose and
  never collide with Sprint 6's own leading `msg=`. Needed error cases,
  not just the happy path:
  - `log_tag=`/`render=`/`min_level=`/`where=` appearing on a statement
    that ALSO has `WHEN` or `DO` → clear compile error ("log_tag=/render=
    are only valid on a bare ALERT-only statement, not combined with
    WHEN/DO") — don't let it silently fall through to "unknown channel
    log_tag" from the ordinary agent_stmt compiler.
  - `log_tag=` and `msg=` both present → clear compile error naming both
    keys, not a generic parse failure.

**Compiles to** — CONFIRMED against a real backend contract test (12/12 green,
see below):
```json
{
  "kind": "event",
  "conditions": {"log": {"tag": "<log_tag>", "min_level": "<min_level, default INFO, UPPERCASE>", "where": {"<...>": "<...>"}}},
  "events": [{"channel": "<name>", "enabled": true}],
  "actions": [{"type": "render", "render": "<render>"}],
  "name": "<from #name=, required>",
  "...": "rest of Sprint 7's meta fields (description/tags/group)"
}
```
**CRITICAL — `events[]` entries MUST include `"enabled": true`.** Found by the
backend contract test, not originally in this plan: `event_agents.py`'s
`_send_channel()` does `if spec is None or not ch.get("enabled"): return False`
— no default. A channel entry missing `enabled` silently never fires; the
agent creates fine, activates fine, is picked up by the loader fine, and then
NOTHING ever sends. This is NOT the same shape as ALERT's ordinary
threshold-agent channel-list compile (which doesn't need `enabled` at all) —
event_stmt's channel-list compile step must be its own small wrapper that adds
`enabled: true` to each entry, not a bare reuse of the threshold-agent
decompile/compile helper verbatim.
(`where` key omitted entirely from `conditions.log` when not supplied —
don't emit `"where": {}`, which could be read as an active empty-match
filter by validate_seed_spec rather than "no extra filter.")

## Agents

- backend-test: Add ONE new pytest file,
  `backend/tests/test_event_agent_cli_contract.py`:
  - POST to `/api/agents/` (via the existing test client / app fixture —
    follow the pattern in `backend/tests/test_agents_routes.py` or
    whichever existing test file posts to this same controller) with a
    payload matching EXACTLY the JSON shape in this plan's "Compiles to"
    section (hardcode it — this test exists to catch frontend/backend
    shape drift, so it must NOT import anything from the frontend
    compiler).
  - Assert 201/the create-success response, no 422.
  - Assert the row is queryable via the same shape
    `event_agents.py:441`'s loader query uses
    (`select(Agent).where(Agent.kind=="event", Agent.status=="active")`)
    once activated via the existing activate endpoint.
  - Assert a MISSING `render` key in `actions[0]` → 422 (negative case,
    proves `_age_validate_event_spec` really is wired for this shape).
  - No product code changes expected. If this test reveals the frontend's
    planned field names (`tag`/`min_level`/`where` inside
    `conditions.log`, `render` inside the single actions entry) don't
    actually match what `validate_seed_spec()` expects, STOP and report
    back — that would mean the "frontend-only" premise above is wrong and
    needs revisiting before the frontend agent proceeds.
  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/test_event_agent_cli_contract.py -v`

- frontend: In `frontend/src/lib/command/grammars/agents.js`:
  1. New `parseEventStmt`/`compileEventStmt`, dispatched from
     `parseStatement` per the precise dispatch rule above (including the
     WHEN/DO-combined and msg=-combined error cases — don't skip these,
     they're the difference between a clear error and a confusing one).
  2. `log_tag`/`render` required; `min_level` optional, defaults to
     `"INFO"`, normalizes to uppercase — confirm this default against
     `event_agents.validate_seed_spec()`'s real default (read the function,
     don't assume `"INFO"` is correct without checking).
  3. `where=` reuses Sprint 8's object-literal parsing, generalized off
     its current `tp_scales_json`-only restriction (see note above — fix
     the restriction, don't duplicate the parser).
  4. `log_tag`, `render`, and every `where` value bypass the grammar's
     normal case-insensitive-enum normalization — preserved exactly as
     typed (quoted OR bare, doesn't matter, no re-casing either way).
     `min_level` is the one value in this clause that DOES get
     case-normalized (to uppercase).
  5. Meta clause (`#name=`/`#desc=`/`#group=`/`#tag=`) reuses Sprint 7's
     existing helpers unchanged.
  6. Decompiler: an existing `kind='event'` agent's
     conditions/events/actions decompile back to this exact
     `ALERT log_tag=... render=...` shape, reusing ALERT's existing
     channel-list decompiling logic for `events`.

  **Test coverage (mandatory)** in `agentsGrammar.test.js`: full compile
  of the example statement to the exact JSON shape above; `log_tag`
  missing → compile error; `render` missing → compile error; `min_level`
  omitted → defaults to `"INFO"`; `min_level=error` (lowercase input) →
  compiles to `"ERROR"`; `where={alert_event=cancel_unconfirmed}` compiles
  with the value preserved as the literal string `"cancel_unconfirmed"`,
  NOT re-cased; `msg=` combined with `log_tag=` → compile error;
  `log_tag=` combined with `WHEN`/`DO` → compile error; dispatch correctly
  distinguishes `ALERT log_tag=...` from an ordinary WHEN-omitted
  `ALERT msg=... telegram` statement. In `agentsDecompiler.test.js`:
  round-trip using a REAL seeded agent's actual
  `conditions`/`events`/`actions` (grep `event_agents.py:SEEDED_AGENTS`
  for one, e.g. `ERROR_AGENT`) — note in the test comment which of that
  agent's OTHER fields (if any, e.g. `priority`, a capability gate) are
  NOT expressible by this syntax yet, so the round-trip assertion is
  scoped to conditions/events/actions only, not every field on the row.

- frontend: In `frontend/src/lib/command/consoleDispatch.js`: extend
  `submitConsoleAgent` (read the exact current code first — today it
  hardcodes `kind: 'threshold'` on the create payload) so the payload's
  `kind` is conditional on what the compiler actually produced
  (`'event'` vs `'threshold'`), not a second hardcoded value.

  **Test coverage (mandatory)**: extend `consoleDispatch.test.js` — a
  `kind:'event'` compiled agent submits with `kind:'event'` on the
  payload; an ordinary non-event result still submits with
  `kind:'threshold'` exactly as before (regression guard).

  Run: `cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20`
  then `npx svelte-check --output machine 2>&1 | tail -20`

## Tests
- pytest: yes — ONE new contract-test file only (`backend-test` agent
  above), no existing suite expected to change.
- svelte-check: yes
- playwright: no (console's CLI already covers typing arbitrary text; no
  new UI surface)

## Commit message
feat(agents): Sprint 12 — ALERT-only statement for notification (event-kind) agents

## Done when
- `ALERT log_tag=error, min_level=ERROR, render=error, telegram, email, log #name="Error alert"`
  compiles to a correct `kind:'event'` agent request and creates a real
  row via the existing `createAgent` endpoint.
- The new backend contract test proves that exact shape is accepted by
  the real endpoint and picked up by the real dispatch-loader query.
- Missing `log_tag=`/`render=` are clear compile errors; `msg=` and
  WHEN/DO combined with `log_tag=`/`render=`/`min_level=`/`where=` are
  clear compile errors, not confusing fallthroughs.
- A real seeded event-agent's JSON round-trips through decompile→recompile
  unchanged (scoped to conditions/events/actions).
- Full frontend test suite green, svelte-check clean, new backend test green.
