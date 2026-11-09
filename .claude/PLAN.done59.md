# Plan: Track 3, Sub-sprint T3.2 — background tasks + Simulator GTT engine + remaining DSL params

## Task

Second of 6 sub-sprints removing Bracket/Template/GTT (full detail:
`~/.claude/plans/sparkling-dancing-lobster.md`, "Track 3" section — read it in full, especially
T3.1's actual findings just added, since T3.1 already removed the core engine and fixed several
call sites this sub-sprint might otherwise assume still need fixing). T3.1 (already shipped,
live on prod) deleted `template_attach.py`/`bracket_registry.py`/`templates.py`, removed
`place_gtt`/`modify_gtt` from the broker layer, and — as a NECESSARY scope expansion T3.1 itself
discovered — already neutralized dangling imports in `actions.py`/`actions_live.py`/
`actions_sim.py`/`lab.py`/`event_agents.py` (the call sites that would otherwise `ImportError`
after `template_attach.py`'s deletion). **Re-read those files' CURRENT state before assuming
anything in this brief about them is still accurate** — T3.1's own report flagged this exact
risk.

This sub-sprint's real remaining scope:
1. Delete the two background safety-net task clusters (`_task_trail_stop`,
   `_task_oco_pair_watcher`) — confirmed NOT touched by T3.1.
2. Delete the Simulator's own separate GTT engine (`sim/gtt_book.py`) + its wiring in
   `sim/driver.py`/`sim/replay_driver.py` — T3.1 confirmed it only removed the FEEDER call site
   into this engine, the engine module itself and its driver wiring are still present and now
   simply never fed. Confirm this is still true, then remove the now-dead module and wiring.
3. Remove the generic agent DSL's `template_id`/`template_slug`/override param SCHEMA itself
   from `place_order`/`order` action definitions in
   `backend/config/grammars/agent_grammar.yaml` + regenerate
   `backend/tests/golden/agent_grammar_baseline.json`. T3.1 already removed the CODE that reads
   these params and acts on them (the call sites) — this sub-sprint removes the catalog/schema
   entries themselves so the DSL no longer advertises params that do nothing.
4. `backend/api/routes/orders_basket.py`/`lab.py`/`simulator.py`: confirm T3.1's scope expansion
   already covered `lab.py`'s MCP `template_slug` resolver (it did, per T3.1's report) — verify,
   don't re-do. Strip any remaining `template_id` passthrough in `orders_basket.py` (confirmed
   partially done by T3.1 — re-check exactly what remains) and `simulator.py` (NOT mentioned in
   T3.1's report — check it fresh, it may still have a passthrough).
5. `backend/api/schemas.py`: remove `OrderTemplateOut`/`Create`/`Patch` structs and the
   override-field kwargs from ticket-request structs (`tp_pct_override`, `sl_pct_override`,
   `wing_*_override`, `sl_trail_pct_override`, `tp_scales_json_override`,
   `hold_template_exit`). **Keep `GttRow`/`GttListResponse`/`GttCancelResponse`** —
   `orders_gtt.py` (confirmed independent, kept in T3.1) depends on them.

## Agents

- backend: implement items 1-5 above, one commit per item (or grouped by your judgment, kept
  individually revertable). Before touching `actions.py`/`actions_live.py`/`actions_sim.py`/
  `lab.py`/`event_agents.py`, run `git log -p` or `git diff` against T3.1's commit
  (`9b8ded95`/`77fff97b` on workshop) to see EXACTLY what T3.1 already removed there, so you
  don't duplicate work or — worse — assume something is still present that T3.1 already deleted.

  1. Delete `_task_trail_stop` and `_task_oco_pair_watcher` clusters from
     `backend/api/background.py` (confirmed self-contained line ranges by this session's earlier
     research — re-verify the exact current ranges, don't trust a possibly-stale line number)
     plus their 2 `asyncio.create_task(_supervised(...))` registration lines. These are
     Bracket-exclusive (confirmed: `_task_trail_stop` reads `attached_gtts_json`/
     `sl_trail_pct`-shaped entries; `_task_oco_pair_watcher` reads `sibling_id`/`sibling_ids`
     entries — both fields only Bracket ever wrote, confirmed in T3.1's own research). Delete
     the associated backend test files too (search for real imports/patches of these two task
     names, not a guess).

  2. Delete `backend/api/algo/sim/gtt_book.py` (the `SimGttBook` class) in full. Remove its
     wiring from `backend/api/algo/sim/driver.py` (`self._gtt_book`, the tick-loop
     `check_triggers()` call, the `place_gtt`/`cancel_gtt`/`modify_gtt`-shaped facade methods) and
     `backend/api/algo/sim/replay_driver.py` (`_replay_place_gtt`/`_replay_trigger_gtt`, the
     `_gtt_book` wiring). Confirm via the sim/replay test suite that removing this doesn't break
     anything else the Simulator does — this module's docstring (per this session's earlier
     research) states it exists *solely* to serve the Bracket flow, so a clean removal should
     leave the rest of `SimDriver`/`ReplayDriver` fully intact.

  3. Edit `backend/config/grammars/agent_grammar.yaml`: remove the `template_id`/`template_slug`/
     override param blocks from the `place_order` and `order` action token schemas (there were
     TWO duplicate catalog blocks per this session's earlier research — find and edit both, or
     confirm there's genuinely only one now if something already consolidated them). Regenerate/
     update `backend/tests/golden/agent_grammar_baseline.json` to match — this is a byte-for-byte
     equality test fixture, update it precisely, not approximately.

  4. Re-check `orders_basket.py` (what remains after T3.1), `simulator.py` (check fresh, verify
     whether a `template_id` passthrough still exists there), and confirm `lab.py`'s state
     matches what T3.1 already reported doing. Strip whatever Bracket-related passthrough
     remains in any of these three files.

  5. Edit `backend/api/schemas.py`: remove `OrderTemplateOut`/`Create`/`Patch` structs (if not
     already removed — `templates.py`'s own deletion in T3.1 may have made these orphaned but
     not yet deleted) and the override-field kwargs from ticket/basket request structs. Keep
     `GttRow`/`GttListResponse`/`GttCancelResponse` — `orders_gtt.py` needs them, confirmed kept
     in T3.1.

  Do NOT touch: anything already shipped and live from Track 1 (the `exit-tp-short-option-90`
  agent and its hold/release/auto-release/lock infrastructure), `orders_gtt.py` itself, or any
  frontend/doc file (later sub-sprints).

- frontend: skip
- broker: skip
- doc: skip
- backend-test: bundle into the backend agent's own work (same reasoning as T3.1 — the test
  changes are tightly interleaved with the exact functions being removed). Cover: the two
  background tasks are genuinely gone (no registration, no residual references); the Simulator
  still runs a full sim/replay cycle correctly with `sim/gtt_book.py` gone; the grammar catalog
  no longer advertises `template_id`/`template_slug` on `place_order`/`order` (test against the
  real YAML + golden baseline, not just a unit assumption); `orders_gtt.py`'s own tests still
  pass unaffected; the Track-1 agent's full test suite still passes byte-identical.
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

(one per item, or grouped — your judgment, individually revertable)

## Done when

- Zero references to `_task_trail_stop`/`_task_oco_pair_watcher`/`sim/gtt_book`/
  `SimGttBook` anywhere in the codebase (grep-confirmed).
- `agent_grammar.yaml` + the golden baseline no longer list `template_id`/`template_slug`/any
  override param on `place_order`/`order` — confirmed via the real catalog, not assumed.
- `orders_gtt.py` and its tests completely unaffected.
- The Track-1 `exit-tp-short-option-90` agent's full test suite still passes, byte-identical
  diff for those specific files.
- Full backend suite green, CC gate clean.
- Report explicitly: did anything in this brief turn out to already be done by T3.1 (don't
  silently re-do it, note it), and did `simulator.py` actually have a lingering passthrough or
  not.
