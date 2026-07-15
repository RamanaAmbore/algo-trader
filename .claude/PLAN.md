# Plan: Fix 17 6D audit findings (P1–P3)

## Task
Fix all 17 remaining issues from the 6D audit. 2 P1s (drain loop logic bug, stale Playwright
assertion), 7 P2s (test coverage gap masking P1, missing executor guard, wing-leg intent,
3 doc gaps, autofocus a11y), and 8 P3s (dead code in notify_deploy + tests, doc diagram,
2 hardcoded hex colours, empty CSS rulesets, 2 missing spec changelog rows).

## Agents

- backend: Fix 3 issues:
  1. `backend/api/persistence/event_queue.py:160-164` **(P1)** — drain loop increments
     `_drain_attempts` on every non-empty-after-flush, not just on failures. Fix: track
     progress by comparing queue size before/after flush; only increment failure counter
     when flush made no progress:
     ```python
     _drain_failures = 0
     while self._queue and _drain_failures < 3:
         prev_size = len(self._queue)
         await self._flush()
         if len(self._queue) >= prev_size:
             _drain_failures += 1
         else:
             _drain_failures = 0
     if self._queue:
         logger.warning(
             f"event_queue[{self.name}]: gave up draining after 3 consecutive failures "
             f"({len(self._queue)} items lost)"
         )
     logger.info(f"event_queue[{self.name}]: stopped, flushed remaining")
     ```
  2. `webhook/notify_deploy.py:65-73` **(P3)** — remove dead `caps` dict + `_cap()` helper
     (unreachable after dev early-exit at line 77). Also remove dead `branch_line` ternary
     at line 131 (always produces `""`  — replace with `branch_line = ""`).
  3. `backend/api/algo/template_attach.py:1300` **(P2)** — `_place_wing_leg` calls
     `broker.place_order(...)` without `intent` kwarg. Wing legs are not close orders so
     the ceiling should apply, but this should be explicit. Read the function first to
     confirm the exact call signature, then add a comment documenting that wing legs
     intentionally omit `intent` (ceiling enforced) so future readers don't add it by
     mistake confusing it with close-intent bypass.

- frontend: Fix 4 issues:
  1. `frontend/e2e/derivatives_payoff_regression.spec.js:648-690` **(P1)** — update all
     `DAY Δ` references to `DAY P&L` to match the rename in commit 388a7c62. Change:
     - `'<span class="ps-k">DAY Δ</span>'` → `'<span class="ps-k">DAY P&L</span>'`
     - `'use the DAY Δ row above for that'` → `'use the DAY P&L row above for that'`
     - Suite/test description strings mentioning `DAY Δ` → `DAY P&L`
  2. `frontend/src/lib/DayPnlBreakup.svelte:83` **(P2)** — remove `autofocus` from the
     close button; instead focus the panel element on open via a Svelte `$effect`:
     ```svelte
     <div class="dpb-panel" bind:this={_panel} ...>
     ```
     ```js
     let _panel;
     $effect(() => { if (open) _panel?.focus(); });
     ```
     Add `tabindex="-1"` to `.dpb-panel` so it is programmatically focusable.
  3. `frontend/src/lib/MarketPulse.svelte:5173-5174` **(P3)** — remove the two empty
     `:global(.ltp-flash-up)` / `:global(.ltp-flash-down)` rulesets; replace with a
     CSS comment above the animation block: `/* ltp-flash-up/down: defined in app.css */`
  4. `frontend/src/routes/(algo)/+layout.svelte:1620,2587` **(P3)** — replace hardcoded
     hex colours with CSS tokens:
     - `color: #c084fc` → `color: var(--algo-violet)`
     - `color: #fbbf24` → `color: var(--c-action)`

- broker: skip

- doc: Fix 5 doc gaps:
  1. `docs/DESIGN_GUIDE.md:4562` **(P2)** — update "Telegram-only since May 2026" to
     reflect that `notify_deploy.py` now sends both Telegram and ntfy (added e74b718f).
  2. `docs/DESIGN_GUIDE.md:781-786` **(P3)** — add `session_factory` parameter to the
     `EventQueue` class description/diagram.
  3. `docs/MIGRATION.md` **(P2)** — add entry for `broker_connection_events` table:
     table created by `create_broker_connection_events_table` in `migrations.py`,
     registered in `_ensure_shared_broker_schema` (shared `ramboq` DB, not per-env).
  4. `docs/specs/BROKER_SPEC.md:329` **(P2)** — add change-log row for c5c5ce90:
     `RemoteBroker.translate_qty` overrides base no-op to forward to conn_service;
     add invariant I13: "Any RemoteBroker proxy must override translate_qty to delegate
     via _call; the base-class no-op is unsafe for MCX/NCO contracts."
  5. `docs/specs/PULSE_SPEC.md` **(P3)** — add change-log row for 81f425f1: bucket
     labels and order-modal close button restored (Svelte 4→5 snippet migration).

- backend-test: Fix 2 test issues:
  1. `backend/tests/test_event_queue.py:139-140` **(P2)** — `test_stop_drains_multiple_batches`
     uses TOTAL=12/BATCH=5 which fits in 3 attempts accidentally. Add a second test
     `test_stop_drains_large_queue_db_healthy` that uses TOTAL=700/BATCH=200 and asserts
     all 700 rows are committed (verifies the drain loop doesn't abort on a healthy DB).
  2. `backend/tests/test_event_queue.py:63-64` **(P3)** — remove dead `_patch_session`
     helper (zero call sites after the session_factory API change).

- playwright: skip (derivatives spec fix is handled by frontend agent above)

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(6d-audit): drain loop progress-track, DAY P&L spec, autofocus a11y, dead code, doc gaps

## Done when
- `event_queue.py` drain loop only counts consecutive failures (not successful flushes)
- New test `test_stop_drains_large_queue_db_healthy` passes with TOTAL=700
- `derivatives_payoff_regression.spec.js` references `DAY P&L` not `DAY Δ`
- `DayPnlBreakup.svelte` focuses panel div on open, not close button; no autofocus attr
- `MarketPulse.svelte` has no empty `:global` rulesets
- `+layout.svelte` uses CSS token vars not hex literals
- `notify_deploy.py` dead `_cap()` and `branch_line` removed
- `_patch_session` helper removed from test_event_queue.py
- DESIGN_GUIDE, MIGRATION, BROKER_SPEC, PULSE_SPEC updated
- pytest green, svelte-check 0 errors
