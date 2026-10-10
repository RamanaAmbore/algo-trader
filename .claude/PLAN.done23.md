# Plan: Agent CLI grammar — Sprint 8, order/bracket parameter unification

## Task

`order(...)` and `bracket(...)` share ONE TP/SL/Wing kwarg vocabulary —
confirmed against real backend code: `TicketOrderRequest`
(`backend/api/schemas.py:601`) already has `tp_pct_override`/
`sl_pct_override`/`wing_premium_pct_override`/`wing_strike_offset_override`/
`wing_max_spread_pct_override`, and `template_attach.py`'s
`build_adhoc_template(overrides)` exists specifically for "operator
supplied inline TP/SL/Wing overrides with no saved template — package as
an ad-hoc template dict." Saved vs. ad-hoc is already one backend
mechanism; the grammar should reflect that.

**Three use shapes, one shared kwarg set:**
```
# 1. Order with INLINE ad-hoc TP/SL — no saved row, maps to the *_override ticket fields
order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, tp_pct=30, sl_pct=-20)

# 2. Order referencing an EXISTING saved bracket by name — today's main path
order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1, bracket="default-bull")

# 3. Saving a REUSABLE named preset — no order-target fields, #name via Sprint 7's meta clause
bracket(applies_to=buy_any, tp_pct=30, sl_pct=-20, tp_order_type=LIMIT) #name="Default Bull"
```

Shared kwargs (ONE param-mapping helper used by both `order(...)` and
`bracket(...)` — do not write two separate mapping functions): `tp_pct`,
`sl_pct`, `wing_premium_pct` OR `wing_strike_offset` (mutually exclusive —
compile error if a statement sets both, same spirit as the existing
`lots`/`qty` mutual-exclusivity check in `_compileOrderActionParams` —
find and mirror that exact pattern), `wing_max_spread_pct`, `tp_order_type`
(enum: `LIMIT`/`MARKET`), `sl_trail_pct`. `bracket(...)`'s own extra kwarg:
`applies_to` (enum: `buy_any`/`sell_option`/`both`) — not valid on
`order(...)`.

**New grammar primitive — object literal, scoped to ONLY `tp_scales_json`
(a list of `{at_pct, close_pct}` entries), do not generalize further this
sprint.** Decided: reuse `=` as the key/value separator inside the object
literal (NOT a new `:` operator) — this grammar already uses `=` as its
one and only keyword-assignment operator everywhere (`kw_arg := NAME "="
value`); introducing a second, different separator for the exact same
concept inside one nested shape would be an inconsistency with no
benefit. So:
```
object_literal := "{" obj_kwarg ("," obj_kwarg)* "}"
obj_kwarg       := NAME "=" value
tp_scales_json_value := "[" object_literal ("," object_literal)* "]"
```
Example: `tp_scales_json=[{at_pct=30, close_pct=50}, {at_pct=60, close_pct=50}]`.

**Compile-time dispatch**: `statement := agent_stmt | order_stmt |
bracket_stmt`. A call named `order`/`place_order` → ticket/basket path
(shape 1: inline overrides; shape 2: `bracket=` resolves to
`template_slug`/`template_id` on the ticket request). A call named
`bracket` → `bracket_stmt := call`, restricted to standalone use exactly
like `order_stmt`'s existing "only order(...) can be used standalone"
rule, same error-message style. Compiles to the EXISTING
`OrderTemplateController` create/update endpoints
(`backend/api/routes/templates.py:240`/`279`) — no new backend route.

**IMPORTANT access-control fact, confirmed via `backend/api/rbac.py`**:
Order Templates require the `manage_settings` capability, which is
`designated`-role ONLY (`frozenset({"designated"})`) — unlike agents
(`manage_own_agents`: `designated`+`trader`). A `trader`-role operator
typing `bracket(...)` will get a 403 from the real backend. This is
EXISTING, unrelated-to-this-sprint backend behavior — do not change it,
just be aware of it when writing tests (any test exercising the bracket
create/update path needs a `designated`-role test account, not a
`trader`-role one — check which role the existing Playwright
`playwright_agent_test` account has if a Playwright test ever needs this;
not required for THIS sprint's own pytest/vitest coverage, which doesn't
need a real HTTP round-trip).

**Invariant (operator instruction, state explicitly in both agent
briefs)**: an order placed via CLI text — whether shape 1 or shape 2
above — produces the IDENTICAL `AlgoOrder` row, chase loop,
template-attach pipeline, and `OrderCard` display as an order placed via
the Ticket form. The CLI is a new input method only, never a second
execution path.

## Agents

- backend: In `backend/api/routes/templates.py`:
  1. Add `group: str | None = None` and `tags: list | None = None` to
     `OrderTemplateCreate` and whatever the update request struct is
     called (grep the file to confirm the exact name — Sprint 7 added
     `OrderTemplate.group_name`/`.tags` ORM columns but did NOT touch
     this controller or its request schemas at all, by design, so this
     sprint is the first thing to actually wire them up). Map `group` →
     `group_name` explicitly (NOT via any generic setattr loop if one
     exists here, same reasoning Sprint 7's backend agent already
     documented for the Agent side — field name ≠ attribute name). `tags`
     maps directly (same name both sides).
  2. Confirm `TicketOrderRequest`'s existing `tp_pct_override`/
     `sl_pct_override`/`wing_premium_pct_override`/
     `wing_strike_offset_override`/`wing_max_spread_pct_override`/
     `template_id` fields (schemas.py:601) and the `/orders/ticket` route
     handler already apply them correctly via
     `template_attach.build_adhoc_template`/`resolve_template_plan` for a
     request with NO `template_id` set but override fields present — read
     that flow to CONFIRM it already works end-to-end (it should, per the
     Sprint 8 investigation that found `build_adhoc_template` already
     exists for exactly this case) rather than assuming; if you find it
     does NOT actually work standalone (e.g. silently requires a
     `template_id` somewhere), fix that gap and flag it clearly in your
     handback — this would be a genuine pre-existing bug, not something
     to silently route around.
  3. No new backend route needed for `bracket="<name>"` resolving to
     `template_id`/`template_slug` on a ticket — `TicketOrderRequest`
     already accepts `template_id` directly; the FRONTEND compiler
     resolves the NAME to an ID/slug (read `backend/api/routes/
     templates.py`'s `GET /` list endpoint to see what identifier the
     frontend should resolve against — probably needs a lookup call from
     the frontend, or the ticket endpoint may already accept a slug
     directly; confirm and tell the frontend agent via your handback if
     this affects what the frontend needs to do).

  **Test coverage (mandatory)**: a test confirming `OrderTemplateCreate`/
  the update request accept and persist `group`/`tags` correctly (mirror
  whatever test pattern already covers `applies_to`/`tp_pct` on this
  controller). A test confirming a ticket request with override fields
  and no `template_id` actually produces the ad-hoc-template behavior
  described above (or documents+fixes the gap if it doesn't).

  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -q --tb=short -k "template or ticket or order"`
  then full suite: `venv/bin/pytest backend/tests/ -q --tb=line`

- frontend: In `frontend/src/lib/command/grammars/agents.js`:
  1. New object-literal value production, scoped to `tp_scales_json`
     only, per the EBNF above (reusing `=` as the separator, not a new
     `:` operator).
  2. New shared TP/SL/Wing kwarg-mapping used by BOTH `order(...)`
     (compiles to `*_override` field names on the ticket request) and the
     new `bracket(...)` call (compiles to the OrderTemplateCreate field
     names, no `_override` suffix) — one function, parameterized by
     destination, not two separate implementations. Mutual-exclusivity
     check for `wing_premium_pct`/`wing_strike_offset` (compile error if
     both set) — mirror the existing `lots`/`qty` exclusivity check's
     exact error-message style.
  3. New `bracket=` kwarg on `order(...)` — resolves to
     `template_id`/`template_slug` on the compiled ticket request (ask
     the backend agent's handback what the exact resolution mechanism
     should be if it's not obvious from the existing ticket schema — this
     task is dispatched in parallel with backend, so note this as a
     possible follow-up fix after reading backend's report, don't block
     on it if the shape is already clear from `TicketOrderRequest`
     accepting `template_id`/a slug directly).
  4. New `bracket_stmt := call`, restricted to standalone use (mirror
     `order_stmt`'s existing "only order(...) can be used standalone"
     error pattern exactly, same message style, naming `bracket` instead
     of `order`). Wire into `statement`'s top-level dispatch alongside the
     existing `agent_stmt`/`order_stmt` arms.
  5. `bracket_stmt` calls `parseMetaClause`/`compileMetaClause` (Sprint
     7's generic helpers — read them, they were written specifically so
     this sprint could reuse them without modification) for its
     `#name=`/`#desc=`/`#group=`/`#tag=` — `#name` should be effectively
     required for a real save (compile error if missing, matching the
     plan's "name required via meta clause" note — exact error wording is
     your call, keep it consistent with this file's existing error style).
  6. Decompiler: a bracket's current OrderTemplate row decompiles to
     `bracket(...)  #name=...` text, reusing `decompileMetaClause`
     unchanged.

  **Test coverage (mandatory)** in `agentsGrammar.test.js`: object-literal
  parsing for `tp_scales_json` (single entry, multiple entries, reuses
  `=` correctly); `order(...)` with inline `tp_pct`/`sl_pct` compiles to
  the right override field names; `wing_premium_pct` + `wing_strike_offset`
  together is a compile error; `order(..., bracket="x")` compiles to the
  right template reference shape; standalone `bracket(...)` compiles to
  the OrderTemplateCreate shape; `bracket(...)` with no `#name=` is a
  compile error; `bracket(...)` embedded inside a WHEN/DO (not standalone)
  is rejected the same way a non-order call inside DO already would be.
  In `agentsDecompiler.test.js`: round-trip for a bracket with all fields
  set including a `tp_scales_json` object-literal list.

  Run: `cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20`
  then `npx svelte-check --output machine 2>&1 | tail -20`

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no (no new UI surface — this is pure grammar/compiler work,
  console/automation UI integration is Sprint 9)

## Commit message
feat(agents): Sprint 8 — unify order/bracket TP-SL-Wing vocabulary, add bracket_stmt

## Done when
- `order(..., tp_pct=30, sl_pct=-20)` compiles to a ticket request with
  the correct `*_override` fields, no saved template row created.
- `order(..., bracket="default-bull")` compiles to a ticket request
  referencing the existing template.
- `bracket(applies_to=buy_any, tp_pct=30) #name="X"` compiles to an
  `OrderTemplateCreate`-shaped request.
- `tp_scales_json=[{at_pct=30, close_pct=50}]` parses and compiles
  correctly using `=` inside the object literal.
- `wing_premium_pct` + `wing_strike_offset` together is a clear compile
  error, not silently accepted.
- Full backend + frontend test suites green, CC clean.
