# Plan: Unify the order grammar into the agent grammar (Phase 3 of 3)

## Task
Phase 3 of 3 (full plan: /Users/ramanambore/.claude/plans/purrfect-marinating-pixel.md;
Phase 1 — a938340c — externalized the agent grammar to YAML; Phase 2 — 0005aeb8
— unified the overlapping order-field vocabulary into order_fields.yaml).

Make action dispatch registry-driven instead of a hardcoded if/elif chain.
**Refined scope after re-reading `backend/api/algo/actions.py` directly**: this
is SIMPLER than originally scoped — no function signature changes are
needed anywhere. Every live handler already takes `(agent, context, params)`
uniformly; every noop handler already takes `(context, params)` uniformly.
The only problem is that `_dispatch_live_action()` (line ~164) routes via a
hardcoded `if action_type == "place_order": ... elif ...` chain instead of a
lookup table, and `_al_run_noop_handler()` has the same problem with its
`_raising: dict[str, object]` literal (which IS already dict-shaped, just
built inline inside the function body every call instead of being a
module-level registry something can register into).

## Approach
Build two module-level registries in `actions.py`, replacing the two
hardcoded dispatch chains, with IDENTICAL behavior (verify with tests, not
just by inspection):

1. `_LIVE_ACTION_HANDLERS: dict[str, Callable]` — built once at module import
   (imports inside a function to preserve the existing lazy-import/circular-
   dependency-avoidance pattern are fine; the DICT LITERAL mapping names to
   those lazily-imported functions needs to exist once, not be rebuilt as an
   if/elif every call). Must preserve: the `chase_close`/`chase_close_positions`
   alias (both map to `_action_live_chase_close_positions`), and the
   "unhandled action_type" warning log for anything not in the dict (don't
   let an unregistered action silently no-op without the existing warning).
2. `_NOOP_ACTION_HANDLERS: dict[str, Callable]` — same treatment for
   `_al_run_noop_handler`'s `_raising` dict, PLUS fold in `send_summary` and
   `chase_close` (currently special-cased with early returns ABOVE the dict)
   so the whole noop dispatch is one lookup, not "check two special cases,
   then fall through to a dict lookup for the rest." Preserve the exact
   exception-handling semantics: `send_summary`/`chase_close` currently do
   NOT get wrapped in the try/except that the `_raising` dict's handlers get
   — if you fold them into the same dict, make sure error-handling behavior
   for those two doesn't change (they may need to stay wrapped the same
   un-wrapped way they are today, via a small per-entry flag or by keeping
   them as a documented exception within the registry approach — don't
   silently add exception-swallowing to two handlers that don't have it
   today, or silently remove it from the others).
3. Do NOT touch the REGISTERED resolver shims in `actions.py` that
   `agent_grammar.yaml`'s action tokens point to (e.g. `actions.place_order`,
   `actions.expiry_auto_close` etc.) — those are a separate, already-correct
   layer (schema/logging shims, confirmed in Phase 1/2 research). This phase
   only touches the LIVE/NOOP execution dispatch, not that registration.
4. Do NOT change `BROKER_ACTIONS`, `_resolve_mode`, `_action_target_exchanges`,
   `_exchange_gate_passes`, or any gating logic — those stay exactly as-is;
   only the two dispatch functions' internal routing mechanism changes.

## Files
- `backend/api/algo/actions.py` — `_dispatch_live_action`, `_al_run_noop_handler`
  (and `_dispatch_noop_action` if it needs updating to match).
- Tests: full regression across every existing action type via the existing
  action-dispatch test suite (grep `backend/tests/` for `_dispatch_live_action`,
  `_al_run_noop_handler`, `test_actions.py`, and any agent-engine integration
  test that fires each action type end-to-end) — every single action_type
  string that exists today (`place_order`, `modify_order`, `cancel_order`,
  `cancel_all_orders`, `close_position`, `chase_close`, `chase_close_positions`,
  `expiry_auto_close`, `send_summary`, `monitor_order`, `deactivate_agent`,
  `set_flag`, `emit_log`, and an unrecognized/unknown action_type string) must
  dispatch to the identical handler with identical error-handling behavior as
  before. Add an explicit test proving a BOGUS/unregistered action_type still
  logs the same warning it does today (for noop) / produces the same "no
  wired handler" warning (for live) — this is the regression most likely to
  silently break with a registry-lookup refactor (a `.get(key, default)`
  with the wrong default, or a KeyError where there used to be a graceful
  warning).

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
refactor(agents): replace hardcoded action-dispatch chains with registries (Phase 3 of grammar unification)

## Done when
`_dispatch_live_action`/`_al_run_noop_handler` route via dict lookups, not
if/elif chains. Adding a new action type going forward means registering an
entry in one of these two dicts, nothing else. Every existing action type's
behavior (including the unregistered-action-type warning path) is identical
before and after, proven by tests, not just code inspection. Full pytest
suite green, coverage gates pass, CC gate clean.
