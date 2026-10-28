# Plan: Gate closed-hours broker polling on a once-per-day cached market status; stop the resulting alert/noise risk

## Context

Operator report: during the closed window (23:31 IST → next trading day 08:00 IST), the system
should do nothing except the one existing daily market-status check — no broker polling, no acting
on the websocket feed, no alerts. In practice several background loops keep making real broker
calls all night, and market-open/closed state is independently re-derived, live, by at least five
separate loops every 30s–5min instead of being resolved once per day and read from a cache/DB entry.

A read-only investigation (this session) confirmed:
- No live alert currently fires from the ticker-degraded/recovered path in prod/dev's real runtime
  mode (conn-service/cutover) — that code path is dead there. Not a live bug.
- `_task_order_poll` (`backend/api/algo/order_poll.py`, 5-min cadence), `_task_trail_stop` (30s) and
  `_task_oco_pair_watcher` (5-15s) — all in `backend/api/background.py` — make real broker calls
  (`broker.order_history`, `broker.ltp()`, `broker.get_gtts()`) every cycle with **no closed-hours
  gate at all**.
- `_task_funds_offhours` polls broker every 30 min **by design**, specifically because it's closed —
  this is the opposite of what the operator now wants.
- Market-open/closed state is recomputed live and independently by `_task_market_lifecycle` (30s),
  conn_service's own ticker watchdog (30s), and `_task_performance`'s probe (5 min) — no single
  once-per-day DB write drives all of them.
- `_TICKER_CALENDAR_STATE` (the existing once-per-day ticker-idle marker) has a ~29-minute
  stale-"connected" gap right at 23:31, because it's only cleared on IST calendar-date rollover
  (midnight), not at the actual 23:31 stop.

Operator decisions from this session: **keep the existing 05:35 IST market-status Telegram report**
as the one allowed daily check+notify. On the WebSocket itself, the operator reconsidered and asked
to also cleanly disconnect it overnight rather than leave it idle-connected — a follow-up
investigation (this session) found a strong technical reason to do this anyway, not just to satisfy
"don't poll": Zerodha's access token expires daily at a fixed 06:00 IST, so an overnight-idle
KiteTicker socket would be sitting on a token that's about to invalidate regardless of what we
choose; a deliberate, clean stop/restart cycle is the more correct design, not just the more
aggressive one. This is designed carefully below given the documented history of fragility in
exactly this mechanism (see "Conn-service tick forwarder killed nightly, never revived" in
CLAUDE.md) — the new hook targets the REAL upstream `TickerManager` singleton inside conn_service
itself, a different object from the one that caused that prior incident (the local, already-fixed
`MmapTickReader` forwarder in the API process), so the prior bug class does not recur as-is, but the
same discipline (stop and restart must agree on which concrete object/method they operate on, and
the restart path must be proven to actually fire) applies.

## Approach

Introduce one cached, once-per-day "today's market status" resolution (persisted via the existing
`task_markers` key/value table — same convention already used by `prev_close_fix_date`, no new
migration needed) that captures, for each configured segment: `is_trading_day`, `open_time`,
`close_time`. This is computed once per IST calendar day (piggybacking on the existing 05:30 holiday
refresh tick, which already resolves the holiday calendar for today) and cached in-process by date.
A new helper, `is_segment_open_cached(segment_or_exchange, now)`, combines that cached daily
resolution with a cheap wall-clock compare — no live holiday/special-session DB work on the hot path.

Then gate the four real-broker-call loops on it:
- `_task_order_poll`, `_task_trail_stop`, `_task_oco_pair_watcher` — skip the broker call entirely
  (short-circuit before any `broker.*` call) whenever every relevant segment reads closed via the new
  cached helper. Mirrors the existing `closed_hours_or_broker()` convention in `snapshot_gate.py`.
- `_task_funds_offhours` — this task's whole reason to exist was to poll *because* it's closed.
  Given the operator's explicit instruction, remove its closed-hours broker call; it becomes a no-op
  during the closed window (comment documents this as an intentional scope change, superseding the
  prior "funds freeze to LKG overnight" design note).

`_task_market_lifecycle` is left untouched — it makes no broker calls (pure clock+cached-config
comparison) and needs real-time precision to fire `close_settled`/open-transition events correctly;
reducing its cadence is out of scope here and not what's generating the complaint.

Fix the `_TICKER_CALENDAR_STATE` gap: clear/mark idle explicitly inside `_snapshot_stop_ticker()`
(the function that already runs at the 23:31 tick) instead of relying on the midnight date rollover.

**Real KiteTicker WS disconnect/reconnect, inside conn_service** (new scope, this round). Research
confirmed the exact mechanism already half-exists:
- Disconnect: `get_ticker().stop()` (`backend/brokers/kite_ticker.py:628`) — the canonical clean-stop
  path (SDK `stop_retry()` → `close()` → `stop()`, then a 0.5s drain sleep), already used elsewhere.
- Reconnect: `get_ticker().restart(api_key, access_token, account)` (`:737`) — its own docstring
  already says *"Called at 08:00 IST after the daily token refresh to bind the ticker to the
  freshly-minted access_token"* — this method exists for exactly this purpose but currently has
  **no caller anywhere in the codebase**.
- Token timing is safe: conn_service's `_task_prewarm_tokens()` (`service/app.py:647-696`) mints a
  fresh Kite token fully automatically (TOTP, no manual step) in the 05:45–05:59 IST window, nearly
  2 hours before 08:00 — the 08:00 reconnect reuses the already-fresh, already-stored token (same
  resolution pattern `_snapshot_restart_ticker` already uses at `background.py:3145-3154`), no new
  login needs to be triggered.
- New scheduled task inside conn_service's own process (`backend/brokers/service/app.py`), added
  alongside the existing `_loop.create_task(...)` calls in `_start_kite_ticker()` (`:284-286`), using
  the same IST-timezone date-comparison pattern `_task_prewarm_tokens` already uses — fire-once-per-
  day guards for both the 23:31 stop and the 08:00 restart (never re-fire mid-window on every 30s
  tick). This task lives entirely inside conn_service and calls `get_ticker()` directly in-process —
  no new HTTP/RPC endpoint needed, and `backend/api/background.py`'s existing (already-fixed)
  `_snapshot_stop_ticker`/`_snapshot_restart_ticker` are untouched by this — they remain solely
  responsible for the local `MmapTickReader` forwarder, a separate, already-correct concern.
- Must verify the existing `_ticker_watchdog()` Phase 1 ("not started" → attempt connect,
  `service/app.py:529-544`, gated by `conn_should_attempt_connect()`) does not immediately fight the
  23:31 stop by reconnecting on its very next 30s tick. If the calendar gate doesn't already treat
  23:31–08:00 as "don't attempt," add an explicit shared quiet-window check so the two mechanisms
  agree — this is a required verification/test item, not an assumption.
- Failure handling: if the 08:00 `restart()` call itself fails (network blip, unexpected state), log
  and alert once (not every 30s), and let the existing Phase 1 "not started" retry loop pick it back
  up on subsequent ticks as a backstop — do not build a brand-new bespoke retry loop when the
  existing one already covers "ticker not started, keep trying."

The already-committed `chase_internal` alert-noise fix (`b669c04e`, currently workshop-only) ships
alongside this change on the next `/ddev` push. Once both the broker-call loops above AND the WS
itself stop running overnight, the structural alert-risk angle closes completely — no broker calls,
no tick activity to misclassify, nothing to fail.

## Agents

- backend: Add a small market-status-cache helper (new function(s) in `backend/api/background.py`
  or a focused new module next to `order_poll.py`) that resolves today's per-segment
  `is_trading_day`/`open_time`/`close_time` once per IST day, persists the resolution date via the
  `task_markers` table (same pattern as `prev_close_fix_date` in `daily_snapshot.py`), and exposes
  `is_segment_open_cached(...)`. Wire `_task_order_poll` (`backend/api/algo/order_poll.py`),
  `_task_trail_stop`, `_task_oco_pair_watcher`, and `_task_funds_offhours` (all in
  `backend/api/background.py`) to consult it and skip their real broker call when closed. Fix
  `_snapshot_stop_ticker()` to explicitly clear `_TICKER_CALENDAR_STATE`'s connected flag at 23:31
  instead of waiting for the midnight date rollover. Document `_task_funds_offhours`'s behavior
  change (no longer polls overnight) in its own docstring/comment.
- backend-test: pytest coverage for each of the four gated loops (broker call skipped when the
  cached segment status says closed; broker call proceeds normally when open) and for the
  `_TICKER_CALENDAR_STATE` 23:31-clear fix. Place broker-call-site tests in
  `backend/tests/test_order_poll.py` (or existing equivalent) and background-loop tests alongside
  the existing `test_ticker_calendar_gate.py`. Add broker-layer coverage (likely
  `backend/tests/broker/test_kite_ticker.py` or a new file) for the nightly cycle task: `stop()`
  fires exactly once at 23:31, `restart()` fires exactly once at 08:00 with the correct
  account/token, neither re-fires mid-window, and the watchdog's Phase 1 does not reopen the socket
  between the 23:31 stop and the 08:00 restart.
- broker: Add the new scheduled nightly-cycle task inside `backend/brokers/service/app.py`
  (alongside the existing `_ticker_watchdog()`/`_health_heartbeat()`/`_task_prewarm_tokens()` tasks
  spawned in `_start_kite_ticker()`): call `get_ticker().stop()` once at 23:31 IST, and
  `get_ticker().restart(api_key, access_token, account)` once at 08:00 IST reusing the already-fresh
  token from `_task_prewarm_tokens`'s 05:45–05:59 mint. Fire-once-per-day guards (IST date compare,
  same pattern as `_task_prewarm_tokens`). Verify/handle the Phase 1 `conn_should_attempt_connect()`
  interaction so the watchdog doesn't immediately reopen the socket right after the 23:31 stop.
- doc: update CLAUDE.md's "Market daily window" section (new entry documenting
  `is_segment_open_cached()`, the four newly-gated loops, and the new conn_service nightly
  stop/restart task) and the "Common Tasks" table (closed-hours polling gate row + ticker nightly
  cycle row). Note `_task_funds_offhours`'s reversed behavior explicitly, since it amends a
  previously-documented design decision.
- frontend / playwright: skip — no UI surface touched.

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(background,broker): gate overnight broker polling + cleanly cycle the KiteTicker WS daily

order_poll/trail_stop/oco_pair_watcher/funds_offhours skip their real broker call during the
23:31→08:00 IST closed window, reading a once-per-day cached segment-status resolution (persisted
via task_markers) instead of each independently recomputing it live every cycle. conn_service now
cleanly stops the real KiteTicker WS at 23:31 and restarts it at 08:00 using the already-fresh
daily token (get_ticker().stop()/.restart(), both pre-existing but the restart path had no caller).
Ticker idle state now clears at 23:31, not at the midnight date rollover. Ships alongside the
already-committed chase_internal alert-noise fix.

## Done when

- `_task_order_poll`, `_task_trail_stop`, `_task_oco_pair_watcher`, `_task_funds_offhours` make zero
  real broker calls while every relevant segment is confirmed closed (23:31→08:00 IST), verified by
  test.
- Today's trading-day/segment-open/close resolution happens once per IST day (cached via
  `task_markers`), not independently re-derived by each of the four loops.
- The real KiteTicker WS inside conn_service cleanly disconnects at 23:31 IST and reconnects at
  08:00 IST with a valid, already-fresh token, verified by test (including that the watchdog's
  Phase 1 doesn't fight the 23:31 stop).
- `_TICKER_CALENDAR_STATE` reflects idle immediately at 23:31, not up to 29 minutes late.
- The 05:35 IST market-status report is unaffected (still fires as today).
- `chase_internal` fix (`b669c04e`) and this change ship together on the next `/ddev`/`/dprod`.
- Full backend pytest suite green, CC gate clean.
