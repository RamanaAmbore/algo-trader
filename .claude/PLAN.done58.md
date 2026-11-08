# Plan: Track 3, Sub-sprint T3.1 — backend core deletion (Bracket/GTT)

## Task

First of 6 sub-sprints removing the Bracket/Template/GTT subsystem (full detail:
`~/.claude/plans/sparkling-dancing-lobster.md`, "Track 3: GTT/Bracket removal" section — READ
IT IN FULL before starting, it has the complete file-by-file map from 3 Explore agents'
research this session, judgment calls already made and reasoned, and exact line ranges). This
sub-sprint deletes the core engine: `template_attach.py`, `bracket_registry.py`, `templates.py`
routes, the broker-adapter `place_gtt`/`modify_gtt` methods, and edits every generic order
lifecycle file that currently has a Bracket-calling hook embedded in it. It does NOT touch:
background tasks (`_task_trail_stop`/`_task_oco_pair_watcher` — T3.2), the Simulator's own GTT
engine (`sim/gtt_book.py` — T3.2), the generic agent action system's `template_id`/
`template_slug` params (T3.2), any frontend file (T3.3/T3.4), any doc file (T3.5), or any schema/
column drop (T3.6, deliberately last — this sub-sprint stops the code from reading/writing the
columns, but leaves them in the DB schema for now).

**This is the highest-blast-radius single sub-sprint in the whole removal — read the plan
file's judgment calls 1, 2, 3, 5 before touching anything**, especially:
- Judgment call 1: `backend/api/routes/orders_gtt.py` and the broker `get_gtts`/`cancel_gtt`
  methods are KEPT — confirmed independent (a standalone "view/cancel any broker GTT" feature,
  not Bracket-specific). Only `place_gtt`/`modify_gtt` are removed.
- Judgment call 5: `backend/api/routes/orders.py` lines ~561-669 is the single highest-risk
  touchpoint — Bracket fields read inline in otherwise-generic order-serialization code, not
  behind a dedicated helper like every other touchpoint. Refactor-then-strip, don't blind-strip.

## Agents

- backend: delete/edit in this order, one commit per numbered item. **Before writing anything,
  re-verify every file/line reference below against the CURRENT code** — this session's
  research is from earlier today and the codebase may have shifted slightly; trust your own
  fresh read over this plan's line numbers if they disagree, but flag any real discrepancy in
  your summary.

  1. **Delete `backend/api/algo/template_attach.py`** (confirmed 3,676 lines) in full, and
     **`backend/api/algo/bracket_registry.py`** (261 lines) in full.
  2. **Delete `backend/api/routes/templates.py`** (the `OrderTemplateController`, 439 lines) +
     remove its route registration wherever controllers are mounted (find the router aggregator
     — likely `backend/api/app.py`).
  3. **Remove `place_gtt`/`modify_gtt`** from `backend/brokers/base.py`'s ABC and all 3 adapters
     (`kite.py`, `dhan.py`, `groww.py`). **Keep `get_gtts`/`cancel_gtt`** on all of them —
     confirm via grep that `orders_gtt.py` is their only remaining real caller before removing
     anything, to be certain this judgment call still holds in the current code.
  4. **Edit `backend/api/algo/chase.py`**: delete `_ch_check_and_fire_template_attach` and
     `_ch_maybe_fire_template_attach` (the Bracket-calling half of chase's fill hooks), and
     strip the second call inside `_chase_terminal_fire_fill_hooks`. **Keep
     `_ch_maybe_fire_auto_tp` completely untouched** — it's a separate, always-on legacy
     single-target TP shim, explicitly gated to fire only when `template_id is None`, unrelated
     to Bracket. Also keep the position-level lock added in an earlier sprint — read-only,
     do not modify it.
  5. **Edit `backend/api/routes/orders_place.py`**: remove `_opl_reconcile_attach_eligible`,
     `_ticket_strip_template_on_close`, `_fire_template_attach_on_fill`,
     `_maybe_fire_template_attach_for_reconcile`, `_opp_load_row_for_attach`, the sibling-map
     builders (`_opl_build_sibling_map`/`_opl_build_group_sibling_map`/
     `_opp_build_attach_entries`), and every call site for these (including the call at the
     ticket-submit handler). **Keep** `_fetch_net_position_row`/`_qty`/`_product`,
     `_is_offsetting_position`, `_verify_close_intent` — these are shared/generic, used by
     other code (the OCO-closed-position safety net in background.py, `actions_live.py`'s
     close path) — do NOT delete them even though they live in a file with heavy Bracket
     editing.
  6. **Edit `backend/api/routes/orders_postback.py`**: remove the template-attach dispatch
     branch (`_pb_wants_template_attach`/`_pb_dispatch_template_attach`/
     `_pb_check_and_fire_template_attach`) from `_pb_dispatch_take_profit_and_template`'s two
     parallel loops — keep the TP-arm branch (`_pb_wants_take_profit_arm`/
     `_pb_dispatch_take_profit_arm`) completely untouched, that's the same legacy shim as
     `_ch_maybe_fire_auto_tp` above, not Bracket.
  7. **Edit `backend/api/algo/order_poll.py`**: remove the single isolated
     `_maybe_fire_template_attach_for_reconcile` import + call inside `_opo_postprocess` — the
     surrounding reconcile machinery (status correction, DB refresh, WS resubscribe) is
     generic, untouched.
  8. **Edit `backend/api/algo/paper.py`**: remove `_paper_maybe_fire_template_attach` and its
     call site.
  9. **Edit `backend/api/algo/order_hold.py`/`order_hold_gate.py`/`order_release.py`**: remove
     ONLY the `TEMPLATE_EXIT` category member and its 3 dedicated functions
     (`template_exit_held`, `template_exit_override`, `hold_template_exit` in
     `order_hold_gate.py`; `release_template_exit` + its `_RELEASE_HANDLERS` registration in
     `order_release.py`). **Keep the shared framework completely intact** — `held_for`,
     `record_held_order`, `release_held_order`, `get_release_handler`, `cancel_held_order`,
     `effective_hold`, `hold_record`, `parse_hold_record` are used by `EXPIRY_CLOSE` and
     `AGENT_CLOSE` (the live agent from the plan's Track 1, shipped and running in production —
     **do not break it**) and must not be touched beyond removing the one enum member and the
     3 TEMPLATE_EXIT-specific functions.
  10. **Edit `backend/api/routes/agents.py`**: remove `"bracket"` from the `kind` Literal type,
      the create-time refusal block, the update-time read-only guard, and
      `legacy_template_id`/`bracket_config` field exposure from `AgentInfo`. **Do NOT touch**
      `require_release`/`release_timeout_minutes` fields or any other part of this file related
      to the Track-1 agent — that work shipped and is live, unrelated to this removal.
  11. **Edit `backend/api/database.py`**: remove the `migrate_order_templates_to_bracket_agents()`
      boot call from `init_db()`'s lifespan wiring. Do not remove other migrations.
  12. **`backend/api/routes/orders.py` — the highest-risk item, refactor-then-strip, not a blind
      strip.** Delete the `retry-template` admin route cleanly (it's a dedicated, isolated
      controller method, low risk). For the generic order-serialization/detach logic around the
      lines the research flagged (`template_id=r.template_id` reads mixed into otherwise-generic
      response-building code) — read the WHOLE surrounding function first, understand what's
      generic vs Bracket-specific, and refactor so the Bracket-specific reads are removed
      without breaking the generic serialization they're embedded in. If this proves riskier
      than expected once you're actually reading it, STOP and report back with specifics rather
      than guessing — this is explicitly the one place this plan flagged as needing real
      judgment, not a mechanical deletion.
  13. **`backend/api/routes/orders_basket.py`**: strip `template_id=leg.template_id` /
      `template_hold_override=` kwargs. No close-gate exists here to also remove (confirmed
      pre-existing gap in Bracket's own coverage, not something this removal needs to add).

  Delete the ~20 dedicated backend test files this session's research identified (search for
  `template_attach`/`bracket_registry` imports to find the exact current list — don't trust a
  possibly-stale enumeration, re-derive it). Edit the shared test files (confirmed overlapping
  with `EXPIRY_CLOSE`/`AGENT_ORDER`/`AGENT_CLOSE`/chase coverage) to remove ONLY the
  Bracket-specific test cases/classes, preserving everything else. **Do not delete or weaken
  any test covering `superseded_broker_order_ids_json`** — confirmed chase-owned, zero Bracket
  involvement, must survive this removal completely untouched.

- frontend: skip — T3.3/T3.4, later sub-sprints.
- broker: skip (the broker-adapter `place_gtt`/`modify_gtt` removal is small enough to fold into
  the backend agent's own work above, item 3 — no separate broker-agent dispatch needed for
  this sub-sprint).
- doc: skip — T3.5.
- backend-test: covered by the backend agent's own test deletions/edits above (bundle into the
  same agent rather than a separate dispatch, given how interleaved the test changes are with
  the exact functions being removed).
- playwright: skip — no frontend change this sub-sprint.

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

(one per numbered item above, or reasonably grouped — your judgment on commit granularity for a
removal this size, but keep each commit individually revertable)

## Done when

- Full backend suite green.
- Zero remaining `import` of `template_attach` or `bracket_registry` anywhere in the codebase
  (grep-confirmed).
- `orders_gtt.py`'s `get_gtts`/`cancel_gtt`-backed routes still pass their own existing tests,
  completely unaffected.
- The Track-1 agent (`exit-tp-short-option-90`, live in prod) and its full test suite
  (`test_exit_tp_short_option_90_sprint4.py`, `test_agent_close_hold_infra.py`,
  `test_agent_close_auto_release.py`, the chase position-lock tests) all still pass unmodified —
  this removal must not touch anything that agent depends on
  (`order_hold`/`order_hold_gate`/`order_release`'s shared framework, the Sprint-2 lock inside
  `chase_order()`).
- `_ch_maybe_fire_auto_tp` / `_pb_wants_take_profit_arm` (the legacy single-target TP shims,
  NOT Bracket) still work, confirmed by their own existing tests passing unmodified.
- CC gate clean.
- `backend/api/routes/orders.py`'s refactor is reviewed, not blindly stripped — report explicitly
  if this took more care than expected, or less.
