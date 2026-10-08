# Plan: Unify the order grammar into the agent grammar (Phase 2 of 3)

## Task
Phase 2 of 3 (full plan: /Users/ramanambore/.claude/plans/purrfect-marinating-pixel.md;
Phase 1 — externalizing the agent grammar to `backend/config/grammars/agent_grammar.yaml`
— is already committed as `a938340c`).

Unify the OVERLAPPING order-field vocabulary between `backend/config/grammars/orders.yaml`
(frontend CLI grammar — `buy`/`sell` verbs) and `backend/api/algo/grammar.py`'s
`place_order` action token's `params_schema` (agent grammar), which today
are two independent, hand-typed definitions of largely the same concepts.

**Important finding from investigation — this is NOT a literal "generate one
schema from the other" task**, because the two layers operate at different
abstraction levels:
- The CLI's `buy`/`sell` verbs express a symbol as `instType` (CALL/PUT/FUT/EQ)
  + `symbol` (underlying) + `strike` + `expiry` — a human-typeable spec that
  the FRONTEND resolves to a concrete tradingsymbol + exchange via
  `resolveInstrument()` (`frontend/src/lib/command/grammars/orders.js`)
  BEFORE building the order payload.
- `place_order`'s params_schema expects an ALREADY-RESOLVED flat `symbol` +
  `exchange` — an agent's condition/scope evaluation already knows the exact
  tradingsymbol, there's no "resolve from underlying+strike+expiry" step at
  that layer.
- The CLI also splits one `price` token into `price`/`trigger_price` via
  post-processing logic (`buildOrderPayload()`) depending on order type;
  `place_order` wants both as separate explicit fields upfront.
- The CLI has a first-class `chase` level (LOW/MED/HIGH) token; `place_order`
  has no equivalent field today.
- `place_order` has template-attachment fields (`template_id`/`template_slug`/
  `tp_pct_override`/`sl_pct_override`/`wing_premium_pct_override`/
  `wing_strike_offset_override`) the CLI has no equivalent for at all.

So the correct design is: extract a SHARED field catalog for the concepts
that are genuinely the SAME at both layers — `qty` (type/description),
`order_type` (enum: MARKET/LIMIT/SL/SL-M — note the CLI calls this
`orderType`, agent calls it `order_type`; same vocabulary, just naming,
unify the name too if that's safe, flag if not), `price`, `trigger_price`,
`product` (enum: the CLI has MIS/NRML/CNC, the agent has MIS/CNC/NRML — same
three values, different order, confirm this is cosmetic not meaningful),
`variety`, `tag`, and `chase_level` (LOW/MED/HIGH + the chase_levels →
band_ticks/retry_seconds mapping already in `orders.yaml`). Each layer keeps
what's genuinely its own: the CLI keeps its `instType`/`symbol`/`strike`/
`expiry` resolution tokens (not shared — there's no agent-layer equivalent);
`place_order` keeps its template-attachment fields (not shared — no CLI
equivalent exists, and that's fine, don't force one).

## Agents
- backend: Create a new shared field catalog — `backend/config/grammars/order_fields.yaml` — containing the field definitions listed above (qty, order_type, price, trigger_price, product, variety, tag, chase_level + the chase_levels band_ticks/retry_seconds mapping), each with type/enum/description, as the single source of truth for these concepts. Update `backend/api/algo/grammar.py`'s `place_order` action's `params_schema` (inside `agent_grammar.yaml`, now that Phase 1 moved it there) to REFERENCE/pull these shared field definitions for the overlapping fields (qty, order_type, price, trigger_price, product, variety, tag) instead of hand-typing them separately — add a `chase_level` optional param to `place_order`'s schema too, sourced from the same shared catalog, since agents currently have no way to specify chase aggressiveness as a `place_order` param (check `actions_live.py`'s real `_action_live_place_order` handler — or wherever `place_order` actually executes live — to see whether it already threads a chase config through some other path; if it does, just wire this new param into that existing path rather than inventing a second one). Update `backend/config/grammars/orders.yaml`'s `buy`/`sell` verb tokens (qty, orderType, price, chase, kwargs.product) to reference the SAME shared field catalog for type/enum/description metadata, while keeping the CLI-specific tokens (instType, symbol, strike, expiry, account) exactly as they are — this file is loaded by BOTH Python (if anything in `backend/` reads it — check) and JS (`frontend/src/lib/command/grammars/orders.js`), so verify the loader on both sides still works identically after this change; the frontend's actual parsing/suggester behavior must not change at all, only where the shared field metadata is defined. Decide carefully on the `orderType`/`order_type` and product-enum-ordering naming questions flagged above — pick ONE canonical name per concept, used in the shared catalog, and confirm (with a test) that each layer's existing external contract (the REST payload shape `place_order`'s actions.py resolver builds, and the REST payload `buildOrderPayload()` in orders.js builds) is UNCHANGED even if the internal field name in the YAML changes — i.e. this is a metadata/schema-definition unification, not a wire-protocol change. For every file you change, write or update a test: a parity test confirming `place_order`'s resolved params_schema still has the exact same fields/types/enums/required-ness as before Phase 2 (except the one new `chase_level` addition, which gets its own explicit test), plus a parity test confirming `orders.yaml`'s wired token definitions (`_wireTokens`/`_wireKwargs` output, or whatever Python-side equivalent exists) are unchanged. Run the full backend suite and confirm 0 regressions. Do NOT touch any frontend JS logic itself (suggesters, `buildOrderPayload()`, `resolveInstrument()`) — only where the shared metadata values come from.
- frontend: skip (the backend agent's brief explicitly keeps `orders.js`'s own logic untouched — if the backend agent finds `orders.yaml`'s loading contract needs ANY frontend-visible change, it should flag that rather than make it, and you'll get a follow-up task)
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
refactor(agents): unify overlapping order-field vocabulary into order_fields.yaml (Phase 2 of grammar unification)

## Done when
`qty`/`order_type`/`price`/`trigger_price`/`product`/`variety`/`tag`/`chase_level`
are each defined ONCE in `order_fields.yaml` and referenced by both
`place_order`'s params_schema and `orders.yaml`'s buy/sell tokens, instead of
hand-duplicated. `place_order` gains a `chase_level` param it didn't have
before. Every existing external contract (REST payload shapes on both sides)
is unchanged — verified by parity tests. Full pytest suite green.
