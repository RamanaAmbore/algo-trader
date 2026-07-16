# Plan: Fix 21 findings — broker-events audit + NavStrip P-slot + chart range

## Task
Fix 21 defects: 19 from the broker-events feature audit (b5c8cf8c) plus 2 new issues found
in investigation. New P1: NavStrip Day P&L slot shows zero after MCX closes (should freeze at
last settlement value). New P2: Charts page shows identical data for 6M and 1Y — root cause
is exchange normalization mismatch in cache key (`"" vs "NFO"`) plus likely data shortage in
DB for affected symbols. Broker-events: 2 P1s (inactive state masking, timezone clobber),
7 P2s (double banner, hex literals, kite_ticker missing events, auth_fail event type
overloaded, account not masked, 2 coverage gaps), and 10 P3s.

## Agents

- backend: Fix 10 issues in Python layer:
  1. `backend/api/routes/health.py:1061` **(P1)** — timezone clobber.
     Current: `datetime.fromisoformat(since).replace(tzinfo=timezone.utc)`
     Fix: check if tzinfo already present before replace:
     ```python
     dt = datetime.fromisoformat(since)
     since_dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
     since_dt = since_dt.astimezone(timezone.utc)
     ```
  2. `backend/api/routes/health.py:1087` **(P2 Security)** — `"account": row.account`
     returned raw. Change to `"account": mask_account(row.account)`. Ensure `mask_account`
     is imported (check existing imports in health.py).
  3. `backend/brokers/broker_apis.py:762,840` **(P2 D5)** — `event_type="auth_fail"` used
     for ALL `ok=False` paths. Read the function to see the different failure causes, then:
     - Keep `"auth_fail"` for 401/403 HTTP status codes
     - Use `"fetch_fail"` for timeouts, 502, SDK exceptions, `None` returns
     This requires reading the exact conditions in the ok=False branches.
  4. `backend/api/routes/health.py:1041-1048` **(P3)** — endpoint returns bare `dict`.
     Add a `BrokerConnectionEventsResponse` msgspec.Struct near the other response types
     in health.py (or schemas.py — check where `BrokerHealthResponse` is defined). Return
     it from `get_events` instead of the bare dict.
  5. `backend/api/routes/health.py:1067` **(P3)** — hoist `from sqlalchemy import and_`
     to the module-level import at the top of health.py alongside existing sqlalchemy imports.
  6. `backend/brokers/service/conn_events.py:50` **(P3)** — change `detail or {}` to
     just `detail` (pass None through). Update the `enqueue_nowait` call to handle None
     detail correctly — check what `EventQueue.enqueue_nowait` does with it.
  7. `backend/api/persistence/migrations.py:147-152` **(P3)** — add a plain `event_ts DESC`
     index to `create_broker_connection_events_table` for unfiltered time-range queries:
     ```sql
     CREATE INDEX IF NOT EXISTS ix_bce_ts ON broker_connection_events (event_ts DESC)
     ```
  8. `backend/api/persistence/migrations.py:126-127` **(P3)** — update docstring to remove
     "connect / disconnect" event type claims; replace with actual emitted event types:
     `auth_fail`, `fetch_fail`, `token_ok`, `rotation_detected`, `fetch_ok_recovery`,
     `circuit_open`, `circuit_close`.
  9. `backend/api/routes/positions_helpers.py` `build_snapshot_position_row()` **(P1)** —
     NavStrip Day P&L zero after MCX close. Read `build_snapshot_position_row()` and
     `_positions_snapshot()` in `positions.py` (lines 92-112). Verify:
     - `prev_settlement_pnl` is always populated from the `daily_book` table per position row
     - `pnl` and `day_change_val` from the last live snapshot are preserved in the frozen row
     Existing test at `backend/tests/test_day_change_closed_hours.py` lines 441-506
     (`test_positions_snapshot_populates_overnight_quantity`) should catch regressions.
     If `prev_settlement_pnl` is null for any overnight MCX row, trace the
     daily_book JOIN in `_positions_snapshot` and ensure it populates the field for all rows.
  10. `backend/api/routes/options.py:2651` **(P2)** — chart 6M = 1Y exchange key mismatch.
      Cache key built as `(sym, (exchange or "").upper(), days, interval)` but
      `options_helpers.py:_historical_ohlcv_store` normalizes with `exchange or "NFO"`.
      Fix: change line 2651 to `(sym, (exchange or "NFO").upper(), days, interval)` so the
      outer cache lookup is consistent with the inner write. Also note: if affected symbols
      have < 180 days of stored data, run `/api/admin/persistence/backfill` to populate
      more history — this is an operational step, not a code fix.

- frontend: Fix 5 issues:
  1. `frontend/src/lib/stores.js:1322-1328` **(P1)** — `_brokerHealthWorstState` filters
     out `inactive` accounts before computing worst state. An `inactive` account (active=True,
     never authenticated) should surface as `amber` in the chip, not be hidden.
     Remove the `.filter(a => a.state !== 'inactive')` line (or change it to include
     `inactive` in the state ranking). Verify the ranking handles `inactive` correctly —
     it should rank worse than `green` but equal to or less than `red`.
  2. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte:3873-3929` **(P2)** —
     Double-banner: `.pos-stale-bar` renders unconditionally and the red full-banner also
     renders when `_brokerWorstState !== 'green'`. Read the template to find both elements,
     then make them mutually exclusive: show `.pos-stale-bar` only when the broker IS green
     (data stale but broker ok) and show the full red banner only when broker is not green.
  3. `frontend/src/lib/BrokerHealthBadge.svelte:315,336,374` **(P2 D4)** — replace
     `#94a3b8` with `var(--text-faint)` in all three locations (inactive dot, inactive
     account text, inactive state label).
  4. `frontend/src/lib/stores.js:1359` **(P3)** — remove dead `stopBrokerHealthPoller`
     function (zero callers; poller is a tab-lifetime singleton).
  5. `frontend/src/lib/data/nav.js` `baseDayPnlForPosition()` **(P1)** — NavStrip P-slot zero
     after MCX close. Read lines 94-106. The authoritative path `return pnl - prevPnl` fires
     only when `prev_settlement_pnl != null`. If snapshot rows have null `prev_settlement_pnl`,
     the fallback fires. The fallback should return `day_change_val` directly when the position
     is an overnight hold (overnight_quantity > 0) instead of recomputing from `close_price`
     (which Kite may return as 0 post-session). Specifically:
     - After the `prevPnl` null-check block, add a guard: if `oq > 0 && day_change_val !== 0`,
       return `p.day_change_val` directly (the value is already correct from snapshot).
     This is a safety net for when backend `prev_settlement_pnl` is unavailable.
     Verify by reading `_todayPnl` derived value and NavStrip P slot 1 binding.

- broker: Fix 2 issues:
  1. `backend/brokers/kite_ticker.py:957-971` **(P2 D5)** — `_on_close`, `_on_error`,
     `_on_reconnect` callbacks never call `_emit_conn_event`. Read these three methods and
     add appropriate emission:
     - `_on_close`: emit `"disconnect"` (or `"ticker_close"`) with `{"code": close_code}`
     - `_on_error`: emit `"ticker_error"` with `{"error": str(error)}`
     - `_on_reconnect`: emit `"ticker_reconnect"` with `{"attempt": attempt}`
     Use the same lazy-import shim pattern as `connections.py:1885`.
  2. `backend/brokers/connections.py:1885`, `broker_apis.py:16`, `dhan.py:49` **(P3)** —
     3 duplicate lazy-import shims for `_emit_conn_event`. Consolidate: move the canonical
     import to `conn_events.py` as a public function and have the others import from there
     directly, OR accept the duplication (CLAUDE.md: three similar lines better than
     premature abstraction). Document the pattern with a single comment if keeping.

- doc: Fix 3 doc gaps:
  1. `docs/specs/BROKER_SPEC.md` **(P3)** — add section on `broker_connection_events`:
     table location (shared ramboq DB), emitted event types, emission call sites
     (connections.py, broker_apis.py, dhan.py, kite_ticker.py after fix), retention policy.
  2. `docs/DESIGN_GUIDE.md` **(P3)** — add `BrokerConnectionEvent` to the model diagram
     and mention the audit log feature in the broker layer section.
  3. `docs/guides/ADMIN_GUIDE.md:1096` **(P3)** — add connection audit log subsection
     under `/admin/brokers`: how to read the log, what event types mean, `since`/`limit`
     filter usage.

- backend-test: Fix 2 test issues:
  1. `backend/tests/test_broker_connection_events.py:272` **(P2 COV)** — `TestBrokerConnectionEventsRoute`
     is fully `@pytest.mark.skip`. Remove the skip and fix the tests to run against a
     real SQLite in-memory DB (same pattern as `test_event_queue.py` sqlite_factory).
     The 7 tests cover: account filter, event_type filter, since filter, limit cap,
     auth guard, empty response, and malformed `since` (400 error). Use `AsyncTestClient`
     or the existing Litestar test client pattern. If live DB is truly required, mark
     only those specific tests with `@pytest.mark.integration` and keep the rest runnable.
  2. `backend/tests/test_broker_connection_events.py:70,76` **(P2 COV)** — replace all
     fixture rows using synthetic `connected`/`disconnected` event types with the actual
     production event types: `auth_fail`, `fetch_fail`, `token_ok`, `rotation_detected`,
     `circuit_open`, `circuit_close`. Verify each test still exercises its intended logic.

- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(broker-events-audit): timezone clobber, inactive masking, account mask, kite_ticker events, double banner, coverage fixes, NavStrip P-slot MCX freeze, chart exchange key, doc gaps

## Done when
- `health.py` `since` param correctly converts any tzinfo to UTC (not replace)
- `health.py` `account` field uses `mask_account()` in response
- `broker_apis.py` distinguishes `auth_fail` (401/403) from `fetch_fail` (network/SDK)
- `stores.js` `inactive` state surfaces in chip color (not filtered out)
- `derivatives/+page.svelte` stale bar and red banner are mutually exclusive
- `BrokerHealthBadge.svelte` uses `var(--text-faint)` not `#94a3b8`
- `kite_ticker.py` `_on_close`/`_on_error`/`_on_reconnect` emit connection events
- `TestBrokerConnectionEventsRoute` runs (not skipped); test fixtures use production event types
- msgspec.Struct response, hoisted import, detail=None, event_ts index, docstring updated
- BROKER_SPEC, DESIGN_GUIDE, ADMIN_GUIDE updated
- NavStrip Day P&L slot preserves non-zero value after MCX close (snapshot freeze)
- `baseDayPnlForPosition` fallback returns `day_change_val` for overnight positions when `prev_settlement_pnl` is null
- `options.py:2651` cache key uses `exchange or "NFO"` (consistent with helper normalization)
- pytest green, svelte-check 0 errors
