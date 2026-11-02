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

---

# Plan: Close the DSL's dataframe-access gaps (where-filter, group-derived columns, row-set aggregates, $row-in-alerts)

## Context

This consolidates a long design conversation (not a fresh exploration — every piece below was already
read from the real source and confirmed against actual code during that conversation: the EBNF grammar,
`agents.js`'s parser/compiler, `agent_evaluator.py`'s leaf/tree walker, `agent_engine.py`'s fan-out and
aggregate-classification logic, `grammar.py`'s `EXPOSED_COLUMNS` table, and the existing metric-factory
pattern already shipping for `mean_pnl(30)` etc.). The trigger was the operator asking "can the DSL
process dataframes" and then, step by step, surfacing four concrete, real gaps relative to how comparable
systems (PromQL, Pine Script, Drools, Alertmanager) handle the same problems. Operator's own framing,
verbatim: "there should be where clause for 2nd segment. it should support as many conditions as
possible. use python dataframe syntax if required." and "yes, add generic row-set aggregates."

The four gaps, in the order they were found:

1. **No generic per-row filter on the 8 real source dataframes.** Only `log(...)`'s `where={}` has one;
   `positions.*`/`holdings.*`/`funds.*`/etc. have none — the only way to narrow to "just this account" or
   "just MCX rows" is a bespoke, hand-registered scope token (`positions.expiring_today.mcx_unhedged`),
   one per combination anyone has ever asked for.
2. **Some facts that could be filtered on are computed per-GROUP, not per-row** (e.g. "is this leg
   unhedged" requires grouping by underlying and netting CE/PE quantities) — these aren't exposed as
   ordinary row fields today, so a generic where-filter alone can't reach them.
3. **No generic row-set aggregate** (count / sum / max / avg across the rows of a scope). Only two
   special cases exist: an implicit ANY (baked into `.any_acct`-style scope naming) and a hardcoded
   MIN-selection (`_row_with_min`, used only by the `worst_acct`/`worst_symbol` tokens). No MAX, no COUNT,
   no SUM, no generic AVG, no EXISTS.
4. **`$row` (the matched-row-reference marker) only reaches DO-action params**, never alert/notification
   text — every comparable system (Alertmanager's label templating, Drools' bound facts) makes "what
   matched" available everywhere, not just one call site.

Deliberately NOT in scope (discussed and explicitly rejected): merging multiple leaves' own rows into one
combined `all[]` firing — that's the same mistake "Fix #2" (the cross-account false-AND bug) was built to
prevent; the fix for "which clause fired" is clause-naming via the existing label mechanism, not row
merging. Also not in scope: absolute from/to historical date-range queries — the backing data
(`alert_state['pnl_history']`) is a same-day, in-memory-only rolling buffer with no persisted history to
query; that would need a new persisted time-series table, a materially bigger and separate piece of
infrastructure, not an extension of anything proposed here.

## Approach

All four pieces reuse existing mechanisms — no new grammar productions beyond one new call-argument
shape, no new runtime engine, no new trust boundary. Build in this order (each is independently useful
and the later ones build on the earlier ones' plumbing):

### 1. Group-derived columns (prerequisite for #2's richest examples, but useful alone)

Some scope resolvers compute a per-GROUP fact today and use it only to decide which rows to include
(e.g. `_scope_positions_expiring_today_mcx_unhedged` groups by underlying, nets CE/PE qty, and silently
drops hedged groups). Promote the computed fact itself onto every row in its group as a plain field —
the same shape as pandas' `.groupby(key)[col].transform(...)` — so it becomes an ordinary, filterable
row field instead of being invisible, same-cycle, baked-in resolver logic.

- Start with ONE concrete promotion: add `is_unhedged` (bool) and `net_qty` (number) to every row
  `position_rows` emits for F&O legs with a resolvable underlying, computed once per (account,
  underlying, option_type-pair) group, in the SAME place `position_rows` is already built
  (`background.py`'s row-construction path feeding `Context.position_rows` — reuse the exact grouping
  logic `_scope_positions_expiring_today_mcx_unhedged` already has, do not reimplement it).
  `is_unhedged`/`net_qty` land in `_POSITION_ROW_COLUMNS` (`grammar.py`) so `$row`/where-filters can see
  them under the SAME exposed-column whitelist mechanism everything else uses.
- Do NOT retrofit every other group-derived fact in the same pass — this is a pattern + one proof case;
  additional promotions (e.g. underlying-level divergence) are a follow-up once this one is live and
  reviewed, not bundled here.

### 2. Generic where-filter on a scope segment

New call-argument on the LAST `scope_seg` of a `scope_ref`, reusing the EXISTING
`scope_seg := call | qualifier_seg` grammar hook (the same shape metric factories like `mean_pnl(30)`
already use for parenthesized args) — no new binder, no new punctuation:

```
WHEN pnl@positions.any_acct(exchange == "MCX" & qty > 0) < -5000 DO alert(telegram)
WHEN pnl@positions.expiring_today(exchange == "MCX" & is_unhedged == 1) <= -30000 DO alert(telegram)
```

- Grammar (`AGENTS_GRAMMAR_EBNF.md`): add `where_expr` as an optional call-argument on the final
  `scope_seg`. The where-expression reuses the EXISTING leaf/composite grammar — `&`/`and`, `|`/`or`,
  `~`/`not`, all six comparators, `between` — not a new mini-language, not a quoted pandas-`.query()`
  string. Bare names inside a where-expression resolve directly against THAT row's own exposed columns
  (via `exposed_columns_for_scope`) — never `metric@scope` (no nested `@`; you're already inside one
  row, there is nothing left to scope into).
- Frontend (`agents.js`): parse the where-expression as a real condition sub-tree (reusing
  `parseLeaf`/the `&`/`|`/`~` parsers already built for preamble-label conditions), validate every bare
  name against that scope's `EXPOSED_COLUMNS` entry at COMPILE time (reject unknown columns before
  save, the same class of check `$row`'s collision guard already does).
- Backend (`agent_evaluator.py`): each scope resolver already returns its full candidate row list
  unchanged. Add one filter step between "resolver returns rows" and "leaf extracts metric and
  compares" — walk the where sub-tree once per candidate row (bare names = that row's own field value,
  via the same exposed-column mapping), keep only rows that pass. Everything downstream (metric
  extraction, comparator, fan-out, `_eval_all`'s cross-account join, `$row` binding) is UNCHANGED — it
  only ever sees the already-filtered list. This is why the ordering matters and must not be collapsed
  into one pass.
- This subsumes (replaces the need for) a narrower, previously-discussed "add a dedicated
  `positions.account(X)` scope" idea — `positions.any_acct(account == "X")` already covers that case as
  a special case of the general filter. Do not also add a dedicated per-account scope token.

### 3. Generic row-set aggregates

New metric factories — `count`, `sum(column)`, `max(column)`, `avg(column)` — same `metric@scope`
binding shape everything else uses, same factory-registration pattern as `mean_pnl(30)` (Phase 26 /
Sprint 2's `(args) -> (ctx, row) -> value` shape in `grammar_tokens/`), just reducing ACROSS ROWS of the
current scope snapshot instead of across TIME samples in `pnl_history`:

```
WHEN count@positions.any_acct(exchange == "MCX") > 5 DO alert(telegram)
WHEN sum(qty)@positions.any_acct(exchange == "MCX") > 500 DO alert(telegram)
WHEN max(pnl)@positions.any_acct <= -10000 DO alert(telegram)
```

- Column arguments (`sum(qty)`) validate against `exposed_columns_for_scope` at compile time, same
  mechanism as the where-filter and `$row`.
- **The one real architectural change, and it must be done correctly**: today, whether a leaf fans out
  per-row or collapses to one result depends ONLY on the scope's own suffix (`.total` → one result,
  checked via `_ae_is_aggregate_match`). A row-set-aggregate metric must collapse to one result even
  when paired with a normally-fanning-out scope (`sum(qty)@positions.any_acct` is ONE number, not N
  fan-out executions). Extend `_ae_is_aggregate_match` (`agent_engine.py`) to also return `True` when
  the leaf's metric is one of these new aggregate factories, REGARDLESS of scope suffix — aggregate
  classification becomes a function of (scope suffix OR metric choice), not scope suffix alone.
- Direct, required consequence: `$row` must fail closed on any leaf using a row-set-aggregate metric —
  there is no single "the row that matched" when the result is a reduction over several rows. This is
  the SAME exemption `$row` already has for `.total`-scoped leaves (`resolve_row_ref_params` /
  `_ae_dispatch_survivor_entry`'s `is_aggregate` check) — it now triggers on one more condition, not a
  new concept.
- `exists` is deliberately NOT a new primitive — it is `count(...) > 0`, expressible today once `count`
  ships. Do not add a separate `exists` keyword.

### 4. `$row` reaching alert/notification text

Thread the existing `_matched_row` / `_matched_row_scope` binding (currently set only in `action_ctx` by
`_ae_dispatch_survivor_entry`, consumed only by `resolve_row_ref_params` in `actions.py`) into the
alert-render call path too (`_v2_send_rich_alert` / `event_agents.py`'s renderers), so an alert's own
message text can use the identical `{"$row": "name"}` marker and get the matched row's field filled in.
Same marker, same `exposed_columns_for_scope` resolution, same fail-closed-on-aggregate-scope behavior —
one new call site, not a new mechanism. Lowest-risk of the four; can ship independently of 1-3 if
sequencing needs it.

## Explicit scope boundary

- No new leaf JSON shape beyond the where-expression's call-argument slot; no new trust boundary; no
  new mini-language (pandas `.query()`-style quoted strings were considered and rejected in favor of
  reusing the DSL's own `&`/`|`/`~`/comparator grammar).
- No merging of multiple leaves' matched rows into one combined view (the explicitly-rejected
  alternative to clause-naming, discussed and declined above).
- No absolute from/to historical date-range queries (no persisted backing store exists or is proposed
  here).
- No blanket retrofit of every group-derived fact across every scope — `is_unhedged`/`net_qty` is the
  one proof-of-pattern promotion; others are a follow-up, not bundled.
- No dedicated `positions.account(X)` scope token (subsumed by the generic where-filter).

## Files

- `docs/specs/AGENTS_GRAMMAR_EBNF.md` — document the where-expression call-argument on `scope_seg`, the
  new aggregate metric factories, and the group-derived-column convention.
- `frontend/src/lib/command/grammars/agents.js` — where-expression parsing/validation on the last
  `scope_seg`; compile/decompile for the new call-arg shape; new aggregate-metric call parsing
  (`count`/`sum(col)`/`max(col)`/`avg(col)`), reusing existing call-arg machinery.
- `backend/api/algo/agent_evaluator.py` — the row-filter step between scope resolution and metric
  extraction; per-row where-subtree evaluation against `exposed_columns_for_scope`.
- `backend/api/algo/agent_engine.py` — `_ae_is_aggregate_match` extended for metric-driven aggregate
  classification; `$row` fail-closed wiring for aggregate-metric leaves; threading `_matched_row` into
  the alert-render path for item 4.
- `backend/api/algo/grammar_tokens/` — new factory files (`metric_count.py`, `metric_sum.py`,
  `metric_max.py`, `metric_avg.py`), following the exact shape of the existing `metric_mean_pnl.py`.
- `backend/api/background.py` (or wherever `position_rows` is actually built — confirm exact call site
  before editing) — the `is_unhedged`/`net_qty` group-and-broadcast-back computation, reusing
  `_scope_positions_expiring_today_mcx_unhedged`'s existing grouping logic rather than reimplementing it.
- `backend/api/algo/grammar.py` — `_POSITION_ROW_COLUMNS` gains `is_unhedged`/`net_qty`; no changes to
  `EXPOSED_COLUMN_SCOPE_PROVENANCE`'s shape, just new entries.
- `backend/api/algo/event_agents.py` — alert-render call site for item 4's `$row` threading.
- `backend/api/algo/actions.py` — `resolve_row_ref_params`/`_is_row_ref_marker` reused as-is (no
  changes expected; confirm during implementation that the same functions work unmodified from the new
  call site).
- Tests: `backend/tests/test_grammar_exposed_columns.py` (new columns), a new
  `backend/tests/test_scope_where_filter.py`, a new `backend/tests/test_rowset_aggregates.py`, and
  extensions to whichever test file covers `_ae_is_aggregate_match`/`$row` today.
  `frontend/src/lib/__tests__/agentsGrammar.test.js` for the parser/compiler additions.

## Agents

- backend: implement items 1-4 as scoped above — group-derived column promotion, the where-filter
  evaluation step, the aggregate metric factories + `_ae_is_aggregate_match` extension + `$row`
  fail-closed wiring, and the alert-render `$row` threading. Full pytest coverage per the Files list.
- frontend: where-expression parsing/validation/compile/decompile on `scope_seg`; aggregate-metric call
  parsing. Vitest coverage in `agentsGrammar.test.js`.
- doc: update `AGENTS_GRAMMAR_EBNF.md` and the in-app Trader Guide
  (`frontend/src/routes/(algo)/automation/guide/+page.svelte`) with one worked example per new feature.
- backend-test: adversarial coverage for the fail-closed paths — where-filter referencing an unknown
  column, aggregate metric + `$row` combination (must reject), aggregate metric paired with a `.total`
  scope (should behave sanely, likely a compile-time rejection since aggregating an already-aggregate
  row is meaningless — confirm the right behavior during implementation and test it either way).
- playwright: skip — no new UI surface, this is grammar/JSON only.

## Tests

- pytest: yes
- svelte-check: yes (parser changes)
- playwright: no

## Commit message

feat(agents): where-filter on scope segments, group-derived columns, row-set aggregates, $row in alerts

## Done when

- `positions.any_acct(exchange == "MCX" & qty > 0)` compiles, evaluates, and correctly narrows the
  candidate row list before metric extraction — confirmed via a test with mixed-exchange rows.
- `positions.expiring_today(exchange == "MCX" & is_unhedged == 1)` produces the same result as the
  existing `positions.expiring_today.mcx_unhedged` token on an equivalent fixture (parity check against
  the old bespoke token, proving the general mechanism subsumes it).
- `count@positions.any_acct(...)`, `sum(qty)@positions.any_acct(...)`, `max(pnl)@positions.any_acct`
  each evaluate to a single aggregate number, never fan out, regardless of the underlying scope's own
  suffix.
- `$row` is rejected (compile-time or fail-closed at runtime, whichever is the more correct layer —
  confirm during implementation) on any leaf using a row-set-aggregate metric.
- An alert's own message text can reference `{"$row": "name"}` and get the matched row's field,
  identical resolution to the existing DO-action-param behavior.
- Every existing agent with no where-filter, no aggregate metric, and no alert-text `$row` usage is
  byte-identical in behavior (regression guard — this must be proven additive, not just assumed).
- Full backend pytest suite + coverage gates + svelte-check green.

---

# Addendum: `alert` is missing from the `DO`-clause action lookahead

## Context

Operator-reported, then audited live (browser-tested, not just read from source): "even while editing
agents, I am not info text. The lookahead tokens, [and] parameters should be proper." Live audit found
**click-to-describe works correctly in edit mode** — the original hypothesis (catalog not loaded /
edit-mode-specific gap) does not reproduce; tested against a real decompiled agent, 5 token types
including a parameter key and a channel name, every one showed the correct description panel, identical
to fresh-compose behavior. No fix needed there.

But the audit found a real, separate, **general** (not edit-specific) bug while probing the lookahead
dropdown: typing `DO al` after any condition never offers `alert` as a candidate — `cancel_all_orders`,
`cancel_order`, `chase_close_positions`, `expiry_auto_close` show up, `alert` never does. This is not a
fuzzy-ranking problem: `_fuzzyScore` (`frontend/src/lib/command/engine.js:170`) scores an exact prefix
match `0` (guaranteed first), and `"alert".startsWith("al")` is literally true — if `alert` were in the
candidate pool at all, it would already be ranked first, not merely low. Its total absence means it is
never added to the pool for this clause position, not that it's buried by scoring.

Root cause, located precisely: `agents.js:8872-8873` —
```js
if (clauseKind === 'alert' || clauseKind === 'do') {
  const section = clauseKind === 'alert' ? catalog.channels : catalog.actions;
```
For `clauseKind === 'do'`, the candidate pool is `catalog.actions` — the real, registered YAML
action-token list only. `alert` was deliberately excluded from that catalog by the Unified-DSL
redesign (`alert(...)` compiles through its own `compileAlertClause` machinery, never the generic
action-dispatch table `catalog.actions` is sourced from — see CLAUDE.md's "Unified agent DSL" entry).
So this isn't a ranking bug or an edit-mode gap — it's a structural exclusion: `alert` can never appear
in `catalog.actions` because it was never meant to be a dispatched action, even though `DO alert(...)`
is completely valid, common syntax.

Note there IS already a related special-case for `alert` elsewhere in this file (`agents.js:9173-9178`,
"clauseKind FORCED to 'alert'") — but that path handles suggestions *inside* an already-typed
`alert(...)` call's own parentheses (channel names), a different problem from "is `alert` itself
offered as a candidate when the operator hasn't typed it yet."

## Approach

Inject a synthetic `alert` entry into the `DO`-clause candidate pool, alongside (not replacing)
`catalog.actions`, at or immediately after the `section` assignment on line 8873 — only when
`clauseKind === 'do'`. Give it a real, hand-written description (since it has no catalog entry to pull
one from) consistent with how other hand-compiled, non-catalog constructs in this grammar are
described elsewhere (check `_bareNameCandidates`'s existing `alert`-related special case at
`agents.js:9173-9178` for the established pattern/wording convention before inventing new text). The
exact-prefix-match scoring already in `_fuzzyScore` needs no change — once `alert` is actually in the
pool, typing `al` will correctly rank it first on its own.

Do not add `alert` to `catalog.actions` itself (that's the real action-dispatch catalog, and `alert`
is deliberately not a dispatched action — adding it there would be a bigger, wrong-shaped fix that
could affect action-dispatch validation elsewhere, not just lookahead suggestions).

## Files

- `frontend/src/lib/command/grammars/agents.js` — the `DO`-clause candidate-pool construction around
  line 8872-8873.
- `frontend/src/lib/__tests__/agentsGrammar.test.js` (or wherever lookahead-candidate tests already
  live for this file — confirm exact location during implementation) — new regression test: typing
  `DO al` after any condition must offer `alert` as a candidate, ranked first (exact-prefix scoring).

## Agents

- frontend: the one-line candidate-pool fix + its test.
- playwright: optional — a small regression spec asserting `alert` appears in the `DO`-clause dropdown
  for a fresh statement, if an existing lookahead-dropdown spec file is a natural place to extend
  rather than creating a new file.

## Tests

- pytest: no
- svelte-check: yes
- playwright: optional (see above)

## Commit message

fix(agents): DO-clause lookahead was missing `alert` from its action candidate pool

## Done when

- Typing `DO al` after any condition offers `alert` as a candidate, ranked first.
- `catalog.actions` itself is unchanged — `alert` is still not a dispatched action, only now visible
  as a lookahead suggestion.
- Existing `DO`-clause lookahead behavior for every real action token is unchanged (regression guard).

---

## Outcome (recorded at implementation time, 2026-11-01)

Both the main plan and the alert-lookahead addendum shipped, with scope adjustments found during
implementation:

- **Items 1-3 (group-derived columns, where-filter, row-set aggregates) shipped as scoped.** Backend:
  new `backend/api/algo/grammar_tokens/metric_{count,sum,max,avg}.py` factories; `agent_evaluator.py`
  where-tree machinery; `_ae_is_aggregate_match` extended; `grammar.py`'s `_POSITION_ROW_COLUMNS` gained
  `is_unhedged`/`net_qty` (type `number`, never `boolean` — no boolean leaf type exists in this
  registry); `agent_grammar.yaml` gained the 4 tokens; golden baseline regenerated. Frontend:
  `agents.js` where-expression parser/compiler/decompiler, aggregate-metric call parsing, guard-4
  ($row fails closed on an aggregate-metric leaf regardless of scope suffix — added beyond the
  original plan text once the frontend audited its own existing 3-guard `$row` mechanism and found the
  gap). `backend/tests/test_scope_where_filter.py` (22 tests), `backend/tests/test_rowset_aggregates.py`
  (29 tests), `frontend/src/lib/__tests__/agentsGrammar.test.js` extensions — all green.
- **Item 4 ($row reaching alert text) was built, then REVERTED, not shipped.** The implementing agent
  built it in full (threading `_matched_row` into `alert_context`, a new `events._resolve_alert_message`
  resolver) and then reverted it entirely on the coordinator's explicit instruction, because the only
  real-world producer of a `{"$row":...}` marker in `agent.events[].message` is a frontend compile step
  for `alert(msg=<column>)` that does not exist — shipping the engine-side plumbing for a marker nothing
  can currently produce would have added a context-copy + new branch to every alert fire, and weakened
  the "byte-identical for untouched agents" regression guard, for zero reachable benefit today. Revisit
  if/when the frontend ever compiles such a marker.
- **Critical bug found and fixed post-implementation, before this was committed**: the frontend's
  where-leaf compiler emitted `{metric, op, value}` for a where-leaf; the backend's
  `_where_leaf_well_formed`/`_eval_where_leaf` validator requires `{column, op, value}`. Every
  CLI-authored where-filter would have compiled clean and then failed backend validation with a 422 on
  save — the compile-time tests had pinned the wrong (self-consistent but backend-incompatible) key, so
  they passed while every real save would have failed. Caught by a doc-writing pass that read the real
  backend validator directly rather than trusting the earlier (wrong) claim that `metric` had been
  "confirmed" against the backend. Fixed: `_compileWhereComparatorValue`/`_compileWhereLeaf` (compiler)
  and `_decompileWhereLeaf`/`_decompileWhereExprNode` (decompiler) now use `column`; all affected test
  expectations in `agentsGrammar.test.js` updated to match; the stale, confidently-wrong section-header
  comment that had asserted `metric` was correct was rewritten to state the real fix and why the
  earlier claim was wrong.
- **Doc-flagged known gaps, documented in `AGENTS_GRAMMAR_EBNF.md`, not fixed in this pass** (deferred,
  per scope discipline — do not silently fix without a fresh decision): (a) the per-leaf latch key
  `(agent_slug, metric, scope, account)` ignores `where`/`scope_filter` — two aggregate leaves differing
  only by `where` share a cooldown slot; (b) `positions.any_acct` exposing `exchange`/`qty` for
  where-filter purposes is fixture-only in the frontend test catalog — the real backend's
  `_POSITIONS_SUMMARY_COLUMNS` for that scope is narrower (`account_id`, `pnl`, `day_val`,
  `day_change_pct`, `day_pct_margin`); every real worked example in the shipped tests/docs uses
  `positions.expiring_today` instead, which does expose row-level columns.
- **Alert-lookahead addendum extended beyond its own written scope**: the fix also covers the
  `preamble` clause's candidate pool, which had the IDENTICAL missing-`alert` gap (found by inspection
  after fixing the `DO`-clause case, not reported separately by the operator) — the preamble's own
  section-header comment already claimed parity with `DO`'s candidate set, which was false until this
  fix. Added `tokens.push('alert')` to the preamble branch, mirroring the `DO`-clause fix exactly;
  `agentsSuggest.test.js`'s existing preamble-vocabulary test (whose title already claimed this parity
  but whose assertions never actually checked for `alert`) was strengthened to assert it.

Group-derived-columns EBNF documentation was corrected during this pass per a flagged factual error:
the real grouping key for `is_unhedged`/`net_qty` is `(root, expiry)` **across all accounts** (not
per-account), and it is **MCX-only** — the doc previously said "per account + underlying", which was
never checked against the real shared `compute_mcx_ce_pe_net_groups()` implementation.
