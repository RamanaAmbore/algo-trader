# Plan: Chase-aware order cancellation + `cancel_all_orders(...)` CLI statement

## Task

Operator asked for CLI-typed support across the full order lifecycle (place
with every order type, modify, cancel pending, GTT) plus chase/template
recognition of cancellation. A 4-agent council (architect/risk/ux/devil's
advocate) investigated the actual code and converged on a materially
different, better-scoped picture than the literal request:

**Premise correction**: `modify_order`/`cancel_order`/`cancel_all_orders`
are NOT dead stubs (an earlier CLAUDE.md note about a different, unrelated
catalog table misled this). They already fire for real today via any
agent's `DO` clause, routed through `actions.py` → `actions_live.py`.

**The real, concrete bug** (found independently by both the architect and
risk agents): none of the three existing cancel paths — the admin
`cancel_order` REST route (`orders.py`), `_action_live_cancel_order`, or
`_action_live_cancel_all_orders` (`actions_live.py`) — call chase's
`mark_killed()`. Chase's poll loop only treats a cancel as a real kill when
`is_killed(broker_order_id)` is true (set ONLY by the dedicated
`kill_chase` route). So cancelling a chase-managed order via any of these
three paths today gets silently undone: chase sees CANCELLED,
`is_killed()==False`, takes the `cancelled_continue` branch, and
**re-places the order at a fresh price**. This is exactly the "chase should
recognize [cancellation]" gap in the operator's request — and it's a
pre-existing bug, not something the CLI feature would introduce; the CLI
would just make operators exercise it far more often.

**Already true, no work needed**: LIMIT/SL/SL-M via CLI already works —
`order(...)` already supports `order_type=MARKET|LIMIT|SL|SL-M` with
conditionally-required `price`/`trigger_price`. Template attach already
fires strictly on FILLED (not partial), per the existing
`ALGO_ORDER_FINAL_STATUSES` + `filled_quantity` gate — verified by reading
`template_attach.py`/`chase.py` directly, not just the docs.

**Scoped out of this round** (devil's advocate + risk flagged these as
needing either infrastructure that doesn't exist yet, or an explicit
operator re-confirmation before building):
- Single-order `cancel_order(...)`/`modify_order(...)` as CLI bare
  statements — blocked on a "pick from live pending orders" dropdown
  (order_ids are 15-16 digit strings, unsafe to type from memory). The
  UX agent says this dropdown belongs in v1 if we build this at all,
  reusing the `account=`/`symbol=` live-dropdown infra just shipped — but
  that's real new work (resolving + rendering the operator's own open
  orders) that deserves its own round, not bundled silently into this one.
- GTT-via-CLI — no standalone "place a GTT" REST route exists today; real
  GTT placement only happens automatically inside `template_attach.py` on
  a parent's full fill. Needs a scope decision (compose into the existing
  bracket/template compile path vs. a genuinely new route) before any
  code gets written — flagging to operator rather than guessing.

This round ships the one slice every council member agreed is both
genuinely useful and safely buildable now: fix the chase-recognition bug
at its root (benefits the EXISTING admin-UI cancel and agent-DO cancel
paths too, not just new CLI code), and add `cancel_all_orders(...)` as a
new CLI bare statement — the only slice with zero order-identification
ambiguity (it is deliberately fleet/account-wide, never a single specific
order to mis-target).

## Agents

- backend: In `backend/api/algo/chase.py`, confirm `mark_killed(broker_order_id)`'s
  exact signature/import path (already used by `_rco_kill_live_mode` in
  `orders.py`). Then: (1) in `backend/api/routes/orders.py`'s admin
  `cancel_order` HTTP handler (~line 2641), call `mark_killed()` for the
  order's `broker_order_id` immediately after a successful broker-level
  cancel. (2) In `backend/api/algo/actions_live.py`, make
  `_action_live_cancel_order` (~line 1066) do the same. (3) In the same
  file, `_action_live_cancel_all_orders` (~line 1198) loops over
  `_al_cancel_broker_orders` per broker — thread a `mark_killed()` call
  into that per-order-cancelled loop (inside `_al_cancel_broker_orders`,
  for each order actually cancelled, not just logged). Then: extract
  `_action_live_cancel_all_orders`'s body into a shared, reusable
  async helper (same module) that both the existing agent-action wrapper
  AND a new lightweight REST route can call — the route is
  `POST /api/orders/cancel-all` in `orders.py`, accepting
  `{scope?, account?, symbol?, side?}` with the SAME refusal rule already
  documented in `_action_live_cancel_all_orders`'s docstring
  (`scope="account"` without `account` raises). Wire it with normal
  RBAC/auth matching the existing admin cancel_order route (not a bare
  unauthenticated endpoint). For every file you change, write or update a
  pytest test in `backend/tests/` covering the changed lines — specifically:
  a test proving chase's poll loop no longer re-places an order cancelled
  via each of the three paths (mock `is_killed`/`mark_killed`, assert the
  poll loop takes the killed branch, not `cancelled_continue`), and a test
  for the new `/api/orders/cancel-all` route (happy path + the
  scope=account-without-account refusal).

- frontend: In `frontend/src/lib/command/grammars/agents.js`, add a new
  bare-statement compile kind `'cancel_all_orders'` parallel to the
  existing `kind:'order'` path for `order(...)` — reuse
  `cancel_all_orders`'s EXISTING `grammar_kind=action` params_schema
  (scope/account/symbol/side) already in `agent_grammar.yaml`, just add
  the bare (non-DO-wrapped) statement support the same way `order(...)`
  has both a DO-action form AND its own standalone `kind:'order'` compile
  branch. In `frontend/src/lib/command/AgentCliEditor.svelte`: extend
  `handlePlaceOrder`/the submit button to also handle
  `compiled.kind === 'cancel_all_orders'` — reuse the SAME button (label
  swaps to "Cancel N orders (LIVE/PAPER)" per the UX agent's
  recommendation, not a new parallel button), call the new
  `POST /api/orders/cancel-all` route directly (NOT through the
  agent-grammar DO-action dispatch — keeps interactive CLI decoupled from
  agent runtime, per the architect's recommendation). Use
  `--close-btn-danger-bg` (already in app.css) for this button's color
  when the compiled kind is a cancel. Render the resolved scope
  (account/symbol/side as typed) in the existing JSON preview area so the
  operator sees exactly what will be cancelled before clicking — this
  preview pane IS the confirm step (no new modal), per the UX agent.
  Submit status text should read "Cancel requested" not "Cancelled" (a
  real `CANCEL_FAILED` terminal state exists). Mount this on the same
  three CLI surfaces `order(...)` already has: Automation Console tab,
  Orders page's `OrderCliTab.svelte`, and `SymbolPanel.svelte`'s
  order-ticket modal CLI tab. For every file you change, write or update a
  Playwright spec in `frontend/e2e/` covering the changed flow — compile
  success/failure, the button label/color swap, the POST payload shape,
  and that it's NOT reachable via the agent-grammar DO-action path from
  this new bare-statement entry point.

- doc: Add a new CLAUDE.md entry (same style as the existing
  "Order-lifecycle races — final-status guards and locking" section)
  documenting the `mark_killed()` fix: which three paths now call it, why
  the gap existed (chase's kill-tracking was keyed to one dedicated route
  only), and the invariant ("any operator-initiated cancel — UI, agent,
  or CLI — must call mark_killed so chase never re-places a genuinely
  cancelled order"). Also note in the same entry (or a new one) that
  LIMIT/SL/SL-M order types already work via `order(...)`'s existing
  `order_type=` param — no new capability there, just confirming existing
  behavior for future reference. Add one row to the "Common Tasks" table:
  `cancel_all_orders CLI statement | agents.js (compile kind) + orders.py
  (POST /api/orders/cancel-all) + actions_live.py (shared helper)`.

## Tests

- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message

fix(chase): cancel paths now call mark_killed so chase never re-places a genuinely cancelled order; add cancel_all_orders(...) as a standalone CLI statement

## Done when

- Admin `cancel_order` route, `_action_live_cancel_order`, and
  `_action_live_cancel_all_orders` all call `mark_killed()` for every
  order they successfully cancel.
- A test proves chase's poll loop treats an order cancelled via each of
  these three paths as a real kill (no silent re-place).
- `POST /api/orders/cancel-all` exists, reuses the same underlying helper
  as the agent action, and enforces the same scope=account-without-account
  refusal.
- `cancel_all_orders(...)` compiles and submits from all three existing
  CLI mount points, with a button label/color that clearly reads as a
  destructive cancel action, and the preview pane shows the resolved
  scope before submit.
- pytest, svelte-check, and the new/updated Playwright specs are green.
- Single-order cancel/modify CLI statements and GTT-via-CLI are
  explicitly NOT built this round — flagged to the operator as follow-up
  decisions, not silently dropped or silently expanded into.
