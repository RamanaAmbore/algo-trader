# Plan: Sprint 3 — generic hold/release/auto-release infrastructure + two must-fix findings

## Task

Sprint 3 of the exit-rule-agent plan (full detail: `~/.claude/plans/sparkling-dancing-lobster.md`
— read the "Corrected from the original draft" section in full before starting, it documents
exactly what was verified against the real code vs. assumed in the original draft). This sprint
builds the generic per-agent hold/release/auto-release mechanism (reused by any future agent,
not just this one) and fixes two correctness gaps Sprint 1's spikes surfaced. It does NOT seed
the real `exit-tp-short-option-90` agent — that's Sprint 4, after this infrastructure is proven
with tests.

## Agents

- backend: implement in this order, one commit per numbered piece. Read
  `backend/api/algo/order_hold.py`, `order_hold_gate.py`, `order_release.py` IN FULL first —
  these already have a generic, working hold/release framework (confirmed this session, not
  assumed): `HoldCategory` enum, `record_held_order()` (close-shaped signature:
  account/symbol/exchange/side/qty/product/reason/price_policy/agent_id — NOT a generic
  action_type/params blob), `release_held_order()` (the `_DEFAULT_RELEASE_HANDLER`, used by any
  UNREGISTERED category — re-verifies the live position, re-quotes a fresh price, places a new
  close order, with its own `.with_for_update()` row lock + `status != "HELD"` guard already
  built in), `get_release_handler(category)` (category-string dispatch, used by the existing
  `POST /api/orders/held/{id}/release` route — "no route code to touch" for a new category),
  `cancel_held_order()` (already generic, any category).

  1. **New `HoldCategory.AGENT_CLOSE`** in `order_hold.py` — do NOT reuse `AGENT_ORDER` (that's
     chase's own repeated-rejection hold, resumes an ALREADY-PLACED order from its own persisted
     fields — wrong semantics; this hold stages a close that was never attempted at the broker).
     **Leave `AGENT_CLOSE` UNREGISTERED in `order_release.py`'s `_RELEASE_HANDLERS` dict** — this
     makes it fall through to `_DEFAULT_RELEASE_HANDLER` (`release_held_order`) automatically,
     which already does exactly the right thing. Do not write a new release function.

  2. **Extend `hold_record()` (`order_hold.py`) with one new optional field**: `auto_release_at:
     str | None` — an absolute UTC ISO timestamp. Thread it through `record_held_order()`
     (`order_hold_gate.py`) as a new optional kwarg, computed by the CALLER as
     `held_at + timedelta(minutes=release_timeout_minutes)` when `release_timeout_minutes` is
     not None, else `None` (meaning "never auto-releases, stays held until explicit operator
     action"). This value is snapshotted onto the HELD row's `hold_json` at stage time — the
     later auto-release scan (piece 6) reads THIS snapshotted value, never a live re-join back to
     `Agent.release_timeout_minutes`. This is deliberate: a later edit to the agent's timeout
     setting must never retroactively change an already-staged hold's deadline.

  3. **New `Agent.require_release: bool` (default `False`) + `Agent.release_timeout_minutes:
     int | None` (default `None`) columns** on the `Agent` model (`backend/api/models.py`) +
     migration (follow this codebase's existing idempotent `_migrate_*` pattern in
     `database.py` — find a recent example like `_migrate_agents_priority` and match its exact
     shape). **Verify, don't just trust this plan, that neither column name collides with an
     existing one.**

     **Critical, already-verified-safe-by-construction, re-confirm it stays that way**: the
     ordinary `PUT /agents/{slug}` `update_agent` handler (`routes/agents.py`) and the AI-refine
     path (`_AI_REFINE_WRITABLE_FIELDS`) are BOTH strict allow-lists — a field not explicitly
     named in either list can never be touched by that write path. Do NOT add
     `require_release`/`release_timeout_minutes` to either list. This is what makes an ordinary
     agent edit safe from silently resetting the hold flag — confirmed this session by reading
     both handlers directly, not assumed.

  4. **The hold check itself, at the single chokepoint**: `_dispatch_live_action(agent,
     action_type, params, context)` in `actions.py`. Before resolving/calling the real handler:
     if `agent.require_release` AND `action_type == "close_position"` specifically (NOT a generic
     check for any action_type — `record_held_order`'s signature is close-shaped, it doesn't fit
     a generic action+params blob, so don't try to make this generic beyond close_position),
     resolve the close parameters the SAME way `_action_live_close_position` already does
     (account/symbol/exchange/side/qty/product — read that function first, reuse its resolution
     logic, don't duplicate it with drift), then call `record_held_order(HoldCategory.AGENT_CLOSE,
     account=..., symbol=..., exchange=..., side=..., qty=..., product=..., reason=...,
     agent_id=agent.id, auto_release_at=...)` (piece 2's new field) plus fire an alert with
     `extra={"action_required": True}` (forces urgent ntfy + a forced email entry per this
     codebase's existing binary action-required gate — confirm this gate exists before assuming,
     search `_apply_action_required_email_gate`/similar in `event_agents.py`), then RETURN —
     never reaching the real close handler.

     **Invariant to preserve, already verified true, don't break it**: `_dispatch_live_action` is
     reached only when `mode == "live"` (decided one level up by `_resolve_mode()`); every other
     mode (sim/replay/shadow/paper) never reaches this code. The new check must stay INSIDE
     `_dispatch_live_action`, never hoisted earlier into a mode-agnostic part of `execute()`'s
     loop — that's exactly the class of bug the 2026-10-31 `expiry-day-itm-auto-close` incident
     was (a safety gate that silently never engaged because the code reached it only in the wrong
     mode).

  5. **Operator-cancel suppression.** `cancel_held_order()` terminates a HELD row but does not
     touch the agent's own condition-latch state in `agent_engine.py` — per this codebase's
     documented escalation rule, an ordered-op leaf can re-fire after cooldown elapses even
     without a fresh breach, if the metric has moved further past threshold. This means an
     operator's explicit Cancel does NOT durably stop a re-stage today. Fix, scoped narrowly to
     the hold-staging check in piece 4 (NOT a change to the shared agent-engine escalation logic
     — other agents depend on that behaving exactly as it does today): before calling
     `record_held_order` in piece 4, check whether a recently-cancelled `AGENT_CLOSE` hold exists
     for the same `(agent_id, account, symbol, exchange, side)` within the remainder of the
     current trading session (use whatever session-boundary helper this codebase already has —
     search for `tradingSessionDateIST`-equivalent on the backend, e.g. near
     `_SESSION_ANCHOR_CUTOFF_TS_SQL` or `session_date_of` in `expiry_freeze.py`, for the
     convention to match). If found, skip staging and log instead of calling
     `record_held_order`. Default: suppress for the rest of the trading session, or until the
     position is closed some other way, whichever comes first.

  6. **Auto-release background scan — one new small task, reuses existing release code
     unmodified.** Add to `background.py`, same scheduling convention as the other 30s/60s loops
     in that file (read a couple of existing examples first to match the convention exactly —
     e.g. `_task_order_poll`'s own scheduling). Selects `AlgoOrder` rows with `status='HELD'`
     whose parsed `hold_json` has `category == 'agent_close'` and a non-null `auto_release_at`
     that has passed (parse via `parse_hold_record` from `order_hold.py`, already exists). For
     each: **first check `agent.status == 'active'`** (a LIVE read of the Agent row at release
     time, not the snapshotted hold-json fields — this is the kill-switch: deactivating the
     agent must stop any pending auto-release even though the hold was staged by an earlier,
     still-live agent state). If the agent is not active, skip (log, do not release, do not
     error). If active, call `get_release_handler("agent_close")(order_id, actor="auto-release")`
     — the IDENTICAL function the manual `POST /api/orders/held/{id}/release` route already
     calls. Do not write any new release logic, any new row-level claim, or any new lock — the
     existing `release_held_order()`'s own `.with_for_update()` lock + `status != "HELD"` guard
     already makes two near-simultaneous triggers (an operator's manual click + this scan,
     landing within the same moment) resolve safely: whichever gets the row lock first wins, the
     other gets a clean "not HELD" failure, not a double-fire. Adding a second claim mechanism on
     top of that existing guard would be redundant, SSOT-violating code — don't do it.

  7. **New, narrow metric `short_option_gain_from_entry_pct`** (new file in
     `backend/api/algo/grammar_tokens/`, same registration pattern as Sprint 1's
     `gain_from_entry_pct` — read that file first and follow its exact shape). This resolves a
     CONFIRMED blocking finding from Sprint 1 (a real test, not a theory): `all[]` of two leaves
     over a fan-out scope (`positions.any_symbol`) cross-joins by ACCOUNT, not row identity
     (`agent_evaluator._eval_all`'s join only recognizes `'acct'`/`'und'` key types) — so
     expressing "short AND option AND gain>=90%" as three separate leaves joined with `all[]`
     would wrongly include a position that only satisfies ONE of the leaves, if another position
     in the same account satisfies a different leaf. Do NOT attempt to fix `_eval_all` itself —
     that's shared engine code with a wider blast radius, explicitly out of scope here. Instead:
     fold BOTH the direction check (`qty < 0`) and the instrument-type check (reuse
     `_option_type()`'s existing digit-anchored CE/PE regex — search for it, don't reinvent it)
     INSIDE this new metric, returning `None` when `qty >= 0` or the row isn't an option. The
     formula for a qualifying row is identical to Sprint 1's `gain_from_entry_pct`'s short-side
     branch: `(avg_price - ltp) / avg_price * 100`, with the SAME None-safety guards Sprint 1
     already built (missing/≤0/non-finite avg_price, missing/≤0 ltp, degraded `price_source` —
     the confirmed real set from Sprint 1 is `{"snapshot_settled", "snapshot_unsettled",
     "pending_open"}`, re-grep to confirm this is still accurate rather than trusting this list
     blindly). The result: Sprint 4's agent condition will be a SINGLE leaf
     (`short_option_gain_from_entry_pct >= 90`), no `all[]`, no cross-join exposure.

  8. **`price_source` wiring fix — a real gap, not a hypothetical.** Sprint 1's tests confirmed:
     on the real live-engine path, `ctx.position_rows` is built in
     `background.py:_perf_run_agent_engine` straight from the raw broker DataFrame
     (`_perf_fetch_all_broker_data`), which carries NO `price_source` column at all — only the
     `/api/positions` HTTP response layer ever stamps one. This means the degraded-tag guard in
     BOTH `gain_from_entry_pct` (Sprint 1) and the new metric (piece 7) can currently never fire
     in production — a pending-open (09:00-09:15 IST) or post-close (15:30-23:30 IST) row is
     silently evaluated at a pinned/frozen price with no signal to either metric that it
     happened. Fix: stamp `price_source` onto the raw DataFrame rows in `_perf_run_agent_engine`
     (or `_pin_pending_open_rows`, whichever is the more natural site once you read both) BEFORE
     they become `ctx.position_rows`. This must land in THIS sprint, not be deferred to Sprint 4
     — Sprint 4 seeds a LIVE agent that depends on this guard actually working.

  Do NOT touch: `template_attach.py`, anything Bracket/GTT-related, the position-level lock from
  Sprint 2 (read-only, already shipped, don't modify), or seed any agent row. Sprint 4 only.

- frontend: skip — the Held Orders list/release/cancel routes are category-agnostic by
  construction (confirmed this session), no new UI needed for a new hold category.
- broker: skip
- doc: skip — a CLAUDE.md entry follows once Sprint 4 seeds the real agent and the final shape
  is known.
- backend-test: write/extend tests for every piece above, specifically:
  - `AGENT_CLOSE` falls through to `release_held_order` (not a new handler) — confirm via the
    SAME dispatch path the real route uses (`get_release_handler("agent_close")`).
  - `auto_release_at` computed correctly and snapshotted (a later change to
    `Agent.release_timeout_minutes` does NOT retroactively change an already-staged hold's
    deadline).
  - The hold check fires ONLY for `action_type == "close_position"` with `require_release=True`,
    and ONLY in `mode == "live"` — never in paper/sim/replay/shadow (test this explicitly, it's
    the exact invariant the 2026-10-31 incident violated).
  - A matching close stages a HELD row + action-required alert with NO broker call made (mock/spy
    `chase_order` never invoked from this path).
  - The cancel-suppression check: an explicit cancel followed by a re-crossing of the threshold
    within the same session does NOT re-stage; a re-crossing in a NEW session does.
  - The auto-release scan: fires exactly once per row once `auto_release_at` has passed, reading
    the row's own snapshotted value; does NOT fire for a deactivated agent (`status != 'active'`)
    even though the hold was staged earlier while active; does NOT fire before the deadline.
  - `short_option_gain_from_entry_pct`: returns `None` for a long row, a non-option row, and
    every degraded-data case from Sprint 1's list (re-verify the real `price_source` set first).
  - The `price_source` wiring fix: construct a test proving `ctx.position_rows` built via the
    REAL `_perf_run_agent_engine`/`_pin_pending_open_rows` path now carries `price_source`, not
    just a unit test of the metric function in isolation (that would miss exactly the gap this
    piece fixes).
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

(one per numbered piece, e.g.)
`feat(orders): add HoldCategory.AGENT_CLOSE + auto_release_at on hold_record`
`feat(orders): add Agent.require_release + Agent.release_timeout_minutes + migration`
`feat(orders): add require_release hold check inside _dispatch_live_action, scoped to close_position`
`fix(orders): suppress AGENT_CLOSE re-staging for the session after an explicit cancel`
`feat(orders): auto-release a HELD agent_close hold once its auto_release_at passes`
`feat(agents): add short_option_gain_from_entry_pct metric, avoiding the all[] cross-join bug`
`fix(agents): stamp price_source onto ctx.position_rows in the live engine path`

## Done when

- `HoldCategory.AGENT_CLOSE` exists, unregistered in `_RELEASE_HANDLERS`, falls through to
  `release_held_order` — verified via the real dispatch path, not asserted.
- `Agent.require_release`/`Agent.release_timeout_minutes` columns exist, migration runs cleanly,
  and are confirmed absent from both `update_agent`'s field list and
  `_AI_REFINE_WRITABLE_FIELDS`.
- A synthetic test agent with `require_release=True` triggers a `close_position` action in LIVE
  mode only → exactly one HELD `AlgoOrder` row (category `agent_close`) + one action-required
  alert, zero broker calls.
- The same in paper/sim/replay/shadow mode → the hold check is never reached (confirmed by the
  mode-gating test).
- An explicit cancel suppresses re-staging for the rest of the session; a new session allows
  re-staging.
- The auto-release scan fires exactly once per row past its deadline, respects the kill switch,
  and never fires early.
- `short_option_gain_from_entry_pct` returns `None` for every missing-data/wrong-direction/
  wrong-instrument-type case, and a correct value for a genuine short-option row.
- `ctx.position_rows` on the real live-engine path now carries `price_source` — proven via a
  test that exercises the real context-building function, not a mocked shortcut.
- Full backend suite green. No changes to `template_attach.py`, any Bracket/GTT path, or
  Sprint 2's lock.
