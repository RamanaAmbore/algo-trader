# Plan: Sprint 2 — position-level close lock inside chase_order()

## Task

Sprint 2 of the exit-rule-agent plan (full detail: `~/.claude/plans/sparkling-dancing-lobster.md`).
Add a per-`(account, symbol, exchange)` lock around the broker-close path to prevent two
independent close-triggers from racing on the same position. Sprint 1's Spike B already
confirmed (via a real test, not assumption) that `_action_live_close_position`
(`actions_live.py`) and `_chase_released` (`order_release.py`, called from `release_held_order`)
are two fully independent call sites with zero shared code before `chase_order()` — each does
its own function-local import, builds its own `ChaseConfig`, runs its own separate pre-chase
verification. **The lock must therefore live inside `chase_order()` itself** (`backend/api/algo/chase.py`)
— the one function both genuinely share — not at either call site individually, which would
leave the other completely unprotected. This sprint does NOT add any hold/release mechanism,
any new Agent columns, or seed any agent — that's Sprint 3/4, later.

## Agents

- backend: Read `backend/api/algo/chase.py`'s `chase_order()` function in full before writing
  anything, plus `backend/api/routes/orders_place.py:_get_template_attach_lock` (the existing
  in-process lock-registry pattern this codebase already uses for a different purpose — follow
  its exact shape/conventions, don't invent a new locking idiom). Also read
  `backend/api/algo/actions_live.py:_action_live_close_position` and
  `backend/api/algo/order_release.py:_chase_released`/`release_held_order` to confirm Sprint 1's
  finding still holds in the current code before building on it.

  Add a per-`(account, symbol, exchange)` in-process lock (an `asyncio.Lock` registry, same
  pattern as `_get_template_attach_lock`) acquired at the top of `chase_order()`, held only
  around the position-resolution + order-placement portion (not the whole multi-minute
  cancel-and-replace retry loop — holding a lock across that long would starve every other
  caller targeting the same symbol for the full chase duration, which is NOT what's wanted; the
  lock should prevent two *concurrent initial resolutions* of the same position, not serialize
  the entire chase lifecycle). Use your own judgment on exact placement after reading the
  function — if `chase_order()`'s structure makes a narrower scope awkward, say so clearly in
  your summary rather than guessing silently.

  A second, concurrent call to `chase_order()` for the same `(account, symbol, exchange)` while
  the first is still resolving must fail closed: log, alert (reuse this codebase's existing
  ntfy/log alert conventions for a rejected/ambiguous close — look at how
  `_ch_cancel_and_capture`'s CRITICAL-log + alert pattern works, same severity class), and return
  a clear non-success result — never silently proceed and never silently drop the second call
  without any trace.

  This is valid as an in-process lock (not needing Redis/DB-level locking) because the live prod
  `ramboq_api` process is confirmed (via `ps aux` on the server, not the repo's stale checked-in
  `.service` file) to run `--workers 1` — a single process, single event loop. State this
  confirmation in your summary so it's not silently assumed.

  Separately: confirm whether the ordinary ticket-placement code path (a manual operator order,
  not through chase/release) ever calls this same `chase_order()` function. If it does, this
  lock covers it for free — note that. If ticket placement has a separate code path that never
  reaches `chase_order()` for a plain new order (as opposed to a close), say so plainly — that's
  an accepted, pre-documented residual, not something to fix in this sprint.

  Do NOT touch: anything in `template_attach.py`, `HoldCategory`, `record_held_order`,
  `release_held_order`'s own logic (beyond confirming it calls `chase_order()` as already
  understood), any `Agent` model column, or any DSL/grammar file. Those are later sprints.

- frontend: skip
- broker: skip
- doc: skip
- backend-test: Write pytest tests proving: (a) two concurrent calls to `chase_order()` for the
  same `(account, symbol, exchange)` — the second fails closed with a clear result/alert, does
  not race with the first; (b) two concurrent calls for *different* symbols (or different
  accounts) proceed independently, unaffected by each other's lock; (c) a single call completes
  normally end-to-end with the lock in place (no deadlock, no behavior change for the
  non-concurrent case). Read what the backend agent actually implemented (exact function names,
  lock scope) before writing tests against assumed internals.
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(chase): add per-(account,symbol,exchange) close lock inside chase_order() — the real shared
chokepoint confirmed by Sprint 1's Spike B, preventing two independent close-triggers (e.g. an
auto-release racing a manual close) from resolving the same position concurrently.

## Done when

- A lock exists inside `chase_order()` (or at the one real shared point the backend agent
  confirms, if different from this plan's assumption — explain why if so) scoped to
  `(account, symbol, exchange)`.
- Two concurrent close-triggers on the same position: the second fails closed with a logged,
  alerted, clear non-success result — never races, never silently drops.
- Two concurrent close-triggers on different symbols/accounts: both proceed independently, no
  cross-contamination.
- The lock does not serialize an entire multi-minute chase lifecycle for unrelated callers — only
  the initial resolution window.
- Full backend suite green.
- No changes to template_attach.py, HoldCategory, record_held_order, release_held_order's core
  logic, any Agent column, or any DSL/grammar file.
