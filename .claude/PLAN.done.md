# Plan: Fix 18 audit defects (P1–P4)

## Task
Fix all 18 defects surfaced by the 2-day audit across backend, broker, frontend, tests, and
infra. 4 P1 bugs (two production-silent failures, one DB schema mismatch, one UI data bug),
6 P2 risks (event-loop block, over-broad network patch, silent error swallow, drain hang,
missing test coverage, race condition), and 8 P3/P4 cleanups (broken teardown, fragile mock,
dead code, focus trap, comment, inert attribute).

## Agents

- backend: Fix four issues in Python layer:
  1. `webhook/notify_deploy.py:147-152` **(P1)** — Replace `import httpx / httpx.post(...)` block with
     `urllib.request.urlopen` following exact pattern from `alert_utils.py:send_ntfy_alert`.
     Also remove the dead `not is_non_main` condition at line 144 (always True; script exits
     at line 79 for dev) **(P3)**.
  2. `backend/api/algo/events.py:173-175` **(P1)** — Wrap the bare `send_ntfy_alert(...)` call in
     `run_in_executor` to match `_send_telegram` (line 214):
     ```python
     loop = asyncio.get_running_loop()
     await loop.run_in_executor(None, send_ntfy_alert, agent.name, telegram_body)
     ```
  3. `backend/api/database.py:565-569` vs `857-904` **(P1)** — Move
     `create_broker_connection_events_table(conn)` call from `_migrate_persistence_tables`
     into `_ensure_shared_broker_schema` using `_shared_engine.begin()`, following the
     `BrokerAccount` pattern. Remove it from `_migrate_persistence_tables`. This ensures
     the table lands in `ramboq` (shared DB) not `ramboq_dev`.
  4. `backend/shared/helpers/alert_utils.py:44-45` **(P2)** — Remove
     `urllib3.util.connection.HAS_IPV6 = False` (over-broad, kills IPv6 for all broker
     adapters). The urllib.request usage in `send_ntfy_alert` already forces IPv4.
  5. `backend/shared/helpers/alert_utils.py:939` **(P2)** — Change bare `except Exception: pass`
     to `except Exception as e: logger.warning("ntfy alert failed: %s", e)`.
  6. `backend/api/persistence/event_queue.py:160-161` **(P2)** — Add a max-drain-attempts counter
     (3 attempts) to `stop()` drain loop so it doesn't hang indefinitely when DB is down at
     shutdown:
     ```python
     _drain_attempts = 0
     while self._queue and _drain_attempts < 3:
         await self._flush()
         if self._queue:
             _drain_attempts += 1
     if self._queue:
         logger.warning(f"event_queue[{self.name}]: gave up draining after 3 attempts")
     ```

- frontend: Fix four issues in Svelte/JS layer:
  1. `frontend/src/lib/PerformancePage.svelte:794-805` **(P1)** — Add `inv_val: total_inv_val`
     to the `makeHoldingsTotals()` return object so the TOTAL pinned row shows the correct
     invested amount.
  2. `frontend/src/lib/PerformancePage.svelte:528-530` **(P2)** — Remove the `valueGetter` from
     the `inv_val` column (which used `quantity` instead of `opening_quantity`) and use
     `field: 'inv_val'` directly to read the server-computed value:
     ```js
     { field: 'inv_val', headerName: 'Invested', width: 88,
       valueFormatter: aggFmtGrid, type: 'numericColumn', headerClass: numericHdr },
     ```
  3. `frontend/src/routes/(algo)/admin/brokers/+page.svelte` **(P2, P4)**:
     - Add an in-flight guard to `loadConnEvents()` so concurrent fetches don't produce stale
       log display. Use a `let _loadingConnEvents = $state(false)` flag: set true at top,
       reset in `finally`.
     - Fix error coercion: replace `e.message || 'Failed...'` with
       `String(e?.message ?? e ?? 'Failed to load connection events')`.
     - Remove inert `data-status="inactive"` attribute from the connection log card **(P4)**.
  4. `frontend/src/lib/DayPnlBreakup.svelte` **(P3)** — Add empty-state message when
     `positions.length === 0`: a centered `<p>` with "No positions" inside the panel body.
     Add `autofocus` to the close button (or a wrapping `<dialog>` `autofocus` attribute) so
     keyboard focus enters the modal on open.

- broker: Fix one issue in broker layer:
  1. `backend/brokers/service/conn_events.py:44-46` **(P4)** — Replace direct `_task` attribute
     access with the public `get_health()` API:
     ```python
     if not broker_conn_event_queue.get_health().get("worker_alive"):
         return
     ```

- doc: skip

- backend-test: Fix three test issues:
  1. `backend/tests/test_broker_connection_events.py:264-270` **(P3)** — Fix the broken teardown
     in the (currently skipped) fixture. Replace `session.delete(select(...))` with:
     ```python
     from sqlalchemy import delete
     await session.execute(
         delete(BrokerConnectionEvent).where(
             BrokerConnectionEvent.account.in_(["ZG0001", "ZG0002"])
         )
     )
     await session.commit()
     ```
  2. `backend/tests/test_ntfy_alert.py` **(P3)** — Change datetime mock target from the top-level
     module path to `backend.shared.helpers.alert_utils.datetime` in all 13 priority tests.
     Add one new test that asserts `urllib.request.urlopen` is called (not httpx) by checking
     the mock is invoked — this verifies the IPv4-force path without needing socket introspection.

- playwright: Fix one e2e spec gap:
  1. `frontend/e2e/day_pnl_breakup.spec.js` **(P2)** — Add a test for the `overnight_quantity=0
     && pnl≠0` rescue path. The test should:
     - Mock or identify a new-position row (overnight_qty=0) in the modal if dev data has one,
       OR add a conditional skip with a note if no such position exists in dev.
     - Assert that for such a row the formula text shows `pnl` (not `day_change_val=0`), i.e.,
       the "Opened today" formula branch is rendered, not the overnight formula.
     - Assert the row's displayed value is non-zero when `pnl≠0`.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
fix(audit): 18-defect patch — ntfy IPv4 deploy ping, event loop block, shared DB schema, holdings TOTAL invested, drain loop timeout, urllib3 patch removed, race guard, test teardown, mock paths

## Done when
- `webhook/notify_deploy.py` uses `urllib.request` for ntfy, dead guard removed
- `events.py` ntfy channel uses `run_in_executor`
- `database.py` `broker_connection_events` DDL inside `_ensure_shared_broker_schema` only
- `alert_utils.py` has no urllib3 monkey-patch; ntfy errors log a warning
- `event_queue.py` drain loop exits after 3 failed attempts
- `PerformancePage.svelte` TOTAL row shows correct Invested value; per-row column reads `field: 'inv_val'`
- `brokers/+page.svelte` has in-flight guard and correct error coercion; data-status removed
- `DayPnlBreakup.svelte` shows "No positions" empty state; close button has autofocus
- `conn_events.py` uses `get_health()` not `_task`
- `test_broker_connection_events.py` teardown fixed; `test_ntfy_alert.py` mock path corrected + IPv4 test added
- `day_pnl_breakup.spec.js` has `overnight_quantity=0` rescue path test
- pytest green, svelte-check green, playwright green on dev
