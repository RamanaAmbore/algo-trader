# Plan: Sprint 17 — parameter-type schema gaps (object-literal typing, conditional requirements, generalized mutual-exclusion)

## Task

Three confirmed, narrowly-scoped gaps in the CLI grammar's parameter
validation, found via direct code reading (not assumed) during an
operator design discussion:

1. **Object-literal entries have no per-key type schema at all.**
   `frontend/src/lib/command/grammars/agents.js`'s object-literal parser
   (originally `tp_scales_json`-only, generalized in Sprint 12 for
   `where=`) parses `{key=value}`-style entries as plain, untyped
   key→value pairs — a typo'd `{at_pct="thirty"}` compiles successfully
   with no validation.
2. **Conditional ("signature-dependent") parameter requirements aren't
   modeled.** `place_order`'s real schema
   (`backend/config/grammars/agent_grammar.yaml:660-761`,
   `backend/config/grammars/order_fields.yaml`) declares `price`/
   `trigger_price` as unconditionally `required: false` — but the real
   requirement is conditional: `price` only actually required when
   `order_type=LIMIT`; `trigger_price` only when `order_type` is `SL`/
   `SL-M`. CONFIRMED (read `order_fields.yaml` in full) that NO other
   field has this pattern — scope this fix to exactly these two.
3. **Mutual-exclusion is hand-rolled per pair, not declared.** Two
   existing one-off XOR checks in `agents.js` (`wing_premium_pct`/
   `wing_strike_offset`, `lots`/`qty`) should become one generic,
   declared mechanism.

## Agents

- backend: In `backend/config/grammars/agent_grammar.yaml`'s
  `place_order` action token's `params_schema` (and `order_fields.yaml`
  if the type-level metadata belongs there instead — read the file's own
  header comment on which properties are "local" vs "shared catalog"
  before deciding where each annotation goes; `required`/`default`/
  `token_ref_ok` are explicitly LOCAL per that file's own documentation,
  so `required_if`/`mutually_exclusive_with` almost certainly belong
  locally in `agent_grammar.yaml` too, not in `order_fields.yaml`):
  1. Add `required_if: {field: order_type, in: [LIMIT, SL]}` to the
     `price` field spec, and `required_if: {field: order_type, in: [SL,
     SL-M]}` to the `trigger_price` field spec — CONFIRMED exact values
     from `order_fields.yaml`'s own description text, already read in
     full this session: `price`'s description literally says "Required
     for LIMIT / SL" (both, not just LIMIT) and `trigger_price`'s says
     "Required for SL / SL-M". Use these exact lists, don't re-derive.
  2. Add a `mutually_exclusive_groups: [[wing_premium_pct_override,
     wing_strike_offset_override]]` key at the `place_order` token level
     (not per-field) listing each exclusive group as a list of field
     names — confirm whether `lots`/`qty` mutual-exclusivity is even
     expressed as backend schema fields at all (grep — it may be a
     CLI-only concept with no backend schema equivalent, in which case
     only document that in your handback, don't invent a fake backend
     field for it).
  3. Confirm `GrammarTokenOut`/whatever struct serves `params_schema` to
     the frontend via `fetchGrammarTokens` does NOT strip unknown keys —
     if it's a strict msgspec Struct with a fixed field list, these new
     keys need to be added there too, or they'll silently vanish before
     reaching the frontend. Check this BEFORE assuming the YAML change
     alone is suficient.

  **Test coverage (mandatory)**: a pytest test confirming
  `GET /api/agents/tokens` (or whatever the real catalog-serving route
  is — check `backend/api/routes/grammar.py`) for the `place_order`
  token includes `required_if`/`mutually_exclusive_groups` in its
  response for `price`/`trigger_price`/`wing_premium_pct_override` —
  this is exactly the "did the new YAML key actually survive to the
  wire" question that must not be assumed.

  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -k "grammar or catalog" -q --tb=line`

- frontend: In `frontend/src/lib/command/grammars/agents.js`:
  1. **Object-literal per-key typing**: add a small
     `OBJECT_LITERAL_SCHEMAS` lookup table (keyed by the field name the
     object-literal is assigned to — `tp_scales_json`:
     `{at_pct: {type:'number'}, close_pct: {type:'number'}}`, `where`:
     no fixed schema — arbitrary key/value, matches real log `extra`
     fields, leave untyped/passthrough). Add a generic
     `_coerceObjectLiteralEntry(entry, schema, label)` mirroring the
     EXISTING `_coerceValueForSpec`'s pattern exactly (same enum/number/
     boolean/string branches) — don't invent new coercion logic, reuse
     by extracting/sharing the core switch if it's cheap to do so.
  2. **Conditional requirement checking**: read the new `required_if`
     key (once the backend half lands) when validating an action call's
     args — if `required_if.field`'s resolved value (from the SAME call's
     other args) is in `required_if.in`, treat this field as required
     even though its own `required` flag says `false`. Needs to run
     AFTER all of a call's args are resolved (since checking `price`'s
     conditional requirement depends on having already resolved
     `order_type`), not during the per-key resolution loop itself.
  3. **Generalized mutual-exclusion**: read `mutually_exclusive_groups`
     from the token-level schema; for each group, if more than one field
     in that group is present in the call's args, raise one generic
     error naming both. Replace the two existing hand-rolled checks
     (`wing_premium_pct`/`wing_strike_offset`, `lots`/`qty` — find both,
     they're in different functions) with calls to this one shared
     helper, UNLESS `lots`/`qty` turns out to be CLI-only (per the
     backend agent's finding in item 2) — in that case keep that one
     specific check as a CLI-local rule (it doesn't need a backend
     schema key if the backend doesn't model it), only generalize the
     wing pair which DOES have a real backend-declared group.

  **Test coverage (mandatory)** in `agentsGrammar.test.js`:
  - `tp_scales_json=[{at_pct="thirty", close_pct=50}]` → compile error
    naming `at_pct` and the expected type.
  - `where={some_key="value"}` still compiles untyped/passthrough (no
    regression — this field deliberately has no fixed schema).
  - `order(..., order_type=LIMIT)` with no `price=` → compile error
    ("price is required when order_type=LIMIT").
  - `order(..., order_type=MARKET)` with no `price=` → still compiles
    fine (no regression — MARKET never needed price).
  - `order(..., order_type=SL, trigger_price=100)` with no `price=` →
    CONFIRMED compile error too (SL requires BOTH price AND
    trigger_price per the exact `required_if.in` lists specified above
    — don't under-scope this to LIMIT-only).
  - mutual-exclusion: `wing_premium_pct` + `wing_strike_offset` both set
    → still errors (regression guard that generalizing didn't silently
    drop this).

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no (pure compiler validation, no new UI surface)

## Commit message
feat(agents): Sprint 17 — object-literal typing, conditional param requirements, generalized mutual-exclusion

## Done when
- A typo'd object-literal value (e.g. a string where `tp_scales_json`
  needs a number) is a clear compile error naming the field and expected
  type.
- `order(order_type=LIMIT)` with no price, and `order(order_type=SL/SL-M)`
  with no trigger_price (and no price, if the closer read above confirms
  SL also needs it), are compile errors instead of silently reaching the
  backend as a 422.
- The wing-pair mutual-exclusion check is declared in the backend schema
  and read generically by the frontend, not hand-rolled.
- Full backend + frontend test suites green.
