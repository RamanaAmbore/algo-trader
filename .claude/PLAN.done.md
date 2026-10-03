# Plan: Sprint 1a — additive schema foundation for the order lifecycle model

## Context

`docs/proposals/ORDER_LIFECYCLE_DATA_MODEL.md` (v3, fully audited and
operator-approved on every open item) designs a unified order data model.
Rather than implement the whole proposal at once, this plan scopes
**Sprint 1a**: the purely additive, lowest-risk slice — schema changes
plus two narrow bug fixes — with zero behavioral change to chase,
template attach, or order placement logic. A 2-agent risk/devil's-advocate
review of this exact plan found two real implementation hazards that are
now corrected below (not hypothetical — both verified against the actual
current code before writing this version):

1. **`CREATE INDEX CONCURRENTLY` cannot run inside `init_db`'s existing
   transaction.** `init_db()` (`database.py:877`) runs all migration
   slices inside one `engine.begin()` block; Postgres hard-errors on
   `CONCURRENTLY` inside a transaction, which would crash the *entire*
   `init_db()` call, not just the new index. Fixed below: the new indexes
   run via a separate connection opened outside that transaction.
2. **Item 3's fix must not touch the shared `_write_live_order`
   constructor directly.** That function (`actions.py:486-540`) is
   shared by `place_order`, `close_position`, and `chase_close_positions`
   — none of which set `product`/`template_id` today. A blanket edit
   would silently change recorded attribution for the other two callers,
   contradicting this plan's own promise not to touch them. Fixed below:
   scoped to a `place_order`-specific branch.

Item 4 (`_fetch_net_position_qty`) was independently re-verified directly
against the live code after the two reviews disagreed on its risk: the
function already wraps its entire body in `try/except Exception: pass →
return None`. The actual bug is that an account-column-guess miss doesn't
raise at all — it silently skips the account filter in normal control
flow (`if acct_col: mask &= ...`, simply never applied when the guess
fails) and returns the first symbol-matching row from *any* account. The
fix is a plain explicit `if acct_col is None: return None` — no exception
handling changes, no caller changes needed, since both existing callers
(`_is_offsetting_position`, fail-open; `_verify_close_intent`,
fail-closed) already handle a `None` return correctly per their own
existing, distinct contracts.

## Scope — Sprint 1a only

1. **Schema additions** (`backend/api/models.py`, migration in
   `database.py`'s init path): nullable, no-default columns —
   `AlgoOrder.source` (VARCHAR), `AlgoOrder.chase_session_id`,
   `AlgoOrder.oco_pair_id` (self-referencing FK, added `NOT VALID`),
   `AlgoOrder.algo_id`, `algo_order_events.broker_order_id_at_event`
   (nullable). Indexes on `source`/`chase_session_id`/`oco_pair_id`:
   **run `CREATE INDEX CONCURRENTLY` via a separate connection/session
   opened outside `init_db`'s `engine.begin()` block** — this is new
   migration machinery, not modeled on an existing pattern, since none
   exists today. Set an explicit `lock_timeout` on the `ADD COLUMN` steps
   that do run inside the normal transaction. No backfill of any kind.
2. **`algo_id` settings placeholder**: new DB-backed setting
   `compliance.algo_id` (empty string default), same pattern as
   `performance.refresh_interval`. No order-placement code reads it yet
   — wiring is Sprint 1b, gated on `source` tagging being wired up first.
3. **Fix the live `place_order` agent-attribution gap**
   (`actions.py:180`, `actions_live.py:200-204`): pass the real agent
   object/id through the `_AgentShim` (confirmed isolated to this one
   call site by grep — safe). **Set `product`/`template_id` via a branch
   specific to `place_order`'s own action-dispatch path, not by editing
   `_write_live_order`'s shared constructor** — `close_position`/
   `chase_close_positions` must end this plan writing exactly what they
   write today, verified by a test that asserts their output is
   unchanged.
4. **Fix `_fetch_net_position_qty`** (`orders_place.py:384-411`): add an
   explicit `if acct_col is None: return None` when the account column
   can't be identified, instead of silently proceeding with no account
   filter. No change to the function's external contract (still returns
   `float | None`), no change to either caller.

## Explicitly NOT in this plan (Sprint 1b, future)

- Wiring `chase.py`/`template_attach.py` to write events (today they
  write zero).
- New event-type vocabulary, `fill`/`postback` naming unification.
- GTT/wing becoming first-class `AlgoOrder` rows (the §7.3 dual-write).
- The §7.4 status-collision checklist (moot until `armed` rows exist).
- Draft mode's 400-rejection removal.
- The event-queue per-row-fallback reliability fix.

## Agents

- backend: Implement all 4 items above, exactly as corrected (CONCURRENTLY
  via a separate connection; product/template_id scoped to a
  place_order-specific branch, not the shared constructor; the
  `_fetch_net_position_qty` fix as a plain early return, nothing more).
  For every file changed, write or update a pytest test covering the
  changed lines (hard gate): a test that live `place_order` now sets
  `agent_id`/`product`/`template_id` correctly; a test that
  `close_position`/`chase_close_positions` output is byte-for-byte
  unchanged (the regression guard the review specifically called for); a
  test that `_fetch_net_position_qty` returns the correct netted quantity
  when unambiguous and returns `None` (not a wrong-account value) when
  the account column can't be identified; a test that API startup
  (`init_db`) does not raise when the new `CONCURRENTLY` indexes are
  created.
- broker/frontend/doc/backend-test/playwright: skip (no frontend or
  broker-layer change; proposal doc already reflects this scope)

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

feat(orders): Sprint 1a — additive schema foundation + agent attribution and position-qty fixes

## Done when

New nullable columns exist with zero impact on existing rows or running
order flow; the new indexes are created without crashing `init_db`;
`compliance.algo_id` setting exists; live `place_order` correctly sets
`agent_id`/`product`/`template_id` while `close_position`/
`chase_close_positions` are provably unchanged; `_fetch_net_position_qty`
returns `None` instead of a wrong-account quantity on an unresolvable
account column; broker and API pytest coverage gates still pass at their
existing thresholds.

---

## Separate, independent item (own commit, dispatched right after this plan is approved — not part of the backend work above)

**Only one InfoHint tooltip popup active at a time.** Currently each
`InfoHint` instance manages its own `open`/`hovered` state independently
— opening a second tooltip elsewhere on the page doesn't close whatever
was already open. Fix: add shared module-level coordination in
`InfoHint.svelte` (a singleton "currently active instance id," set on
open, watched by every instance via an `$effect` that closes itself if
the active id becomes someone else's). Frontend-only, no backend/data
risk, ships as its own commit separate from Sprint 1a.
