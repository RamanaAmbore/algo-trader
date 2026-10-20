# Plan: prev_close corruption, market open/close fragmentation, MCX holiday granularity, and Chg%/LTP single-source fixes

## Context

Tonight's investigation (2026-10-20, Dussehra — a holiday, which is what exposed the first defect) started from one symptom: Pulse Positions' GOLD Chg% showing zero/blank inconsistently. Tracing it surfaced six confirmed, interlocking defects plus three restart-safety gaps discovered while verifying the fix design — all variations of the same root problem: **no single arbitrated source of truth for several values that are read from two or more independent, uncoordinated channels.**

Direct DB verification tonight confirmed: `daily_book.prev_close` gets corrupted to equal `ltp` on **100% of positions/holdings rows** (115/115) every single night, via a double-fire of the settlement-snapshot job around midnight IST. This is invisible on every normal day only because the 08:00 IST repair job (`fix_daily_book_prev_close`) silently fixes it before anyone looks — tonight it's visible because that repair correctly refuses to run on a holiday. Chasing *why* the repair's correctness depends on timing led to: (a) the market open/closed determination being answered differently by 6+ different code paths for the same moment in time, (b) MCX's morning/evening holiday split being collapsed into a blanket guess that's already known to be wrong for ~8 of MCX's 21 2026 holidays, (c) the frontend's Chg% percentage reading Kite's raw stale `quote.ohlc.close` instead of the backend's already-correct `daily_book.prev_close`, and (d) — found while verifying the restart-safety requirement — a live, present-tense bug where `_sg_recover_close_reset()` stomps `daily_book.ltp` back to the morning's settlement price on **any** server restart at **any** time after 08:00, independent of tonight's holiday scenario entirely.

The guiding principle for every fix below (operator-stated, confirmed correct by code review): **one canonical source per value, with any secondary channel scoped explicitly as fallback-only — never a parallel, independently-timed input competing to fill the same field — and every piece of "has this already run today" state must survive a process restart correctly in both directions (no missed day, no duplicate/harmful re-run).**

## Scope decision: one plan, landed as separate commits

Six original defects + three restart-safety sub-items. These don't share a blast radius and several are independently revertable. Per CLAUDE.md's "one issue per commit" default and the operator's explicit "fix all the gaps now" override for *scope*: land as **one coordinated implementation pass, but separate commits**, in this dependency order:

1. D1a — kill the `close_settled` double-fire + session-boundary-aware `target_date`
2. D1b — `MarketLifecycle` restart hydration (`_settled_fired`/`_last_close_at`)
3. D1c — DB-backed "already ran today" markers for the 05:30 holiday refresh AND the 08:00 prev_close fix (fixes the confirmed EOD-stomp-on-any-restart bug)
4. D2 — unify all `is_market_open()` call sites through `_segment_is_open()`
5. D3 — MCX per-segment evening-closed holiday granularity (lands with/after D2)
6. D4 — frontend Chg% denominator: single-source `prev_close`, eliminate the live-quote-OHLC read for this calculation
7. D5 — `dayChangePct()`/its caller never renders blank for a held position (same commit as D4 is fine)
8. D6 — underlying-anchor rows: consolidate onto the same `_applyQuoteFields` pipeline real rows use (decided fix, not contingent on D2)
9. Data repair for already-corrupted 2026-10-20 rows — separate ops decision, NOT bundled into D1a's commit (see Risks)

---

## Defect 1 — Nightly `daily_book.prev_close` corruption

**Verified mechanism:** `backend/api/algo/market_lifecycle.py` line 164 unconditionally resets `self._settled_fired` for every exchange on calendar-date rollover (`elif self._last_seen_date != today: ... self._settled_fired = {k: False for k in self._settled_fired}`), even for exchanges that already settled hours earlier and never reopened. The real re-arm point already exists correctly at line 195 (open-transition reset). This causes `close_settled` to fire a second time right after midnight IST → `snapshot_daily_book(settled=True)` runs again with `target_date` defaulting to `now_ist.date()` (`daily_snapshot.py:1402-1404`, now tomorrow's date) → inserts a brand-new row rather than updating the 23:46 row → the new row's `prev_close`/holdings fallback reads the broker's not-yet-settled `close_price` field, which is still effectively today's ltp at that moment. Confirmed via direct DB query: 115/115 positions+holdings rows touched by this second write show `prev_close == ltp`, zero exceptions, and the same pattern reproduces on ordinary (non-holiday) nights too — only masked by the 08:00 repair.

### D1a — kill the double-fire, make `target_date` session-boundary-aware

**Files:**
- `backend/api/algo/market_lifecycle.py` — delete the date-rollover reset block (line ~156-164) entirely, including the now-dead `_last_seen_date` field and its test-reset clear. Line 195's open-transition reset is the only re-arm point needed; leave it untouched. Leave `_last_close_at` untouched across rollover (needed for MCX's 23:30+offset window spanning midnight).
- `backend/api/algo/daily_snapshot.py` — `snapshot_daily_book()`, lines 1402-1404: change ONLY the `target_date is None` branch to call `backend.api.algo.expiry_freeze.session_date_of(now_ist)` (sync, mirrors `_SESSION_ANCHOR_CUTOFF_TS_SQL`'s 08:00-IST boundary convention) instead of bare `now_ist.date()`. Do not touch any caller passing an explicit `target_date`.

**⚠️ Pre-implementation gate — verify before writing code, may change the design:** confirm what currently creates the 00:30–07:59-dated `daily_book` row that `fix_daily_book_prev_close()`'s settlement-map UPDATE (`WHERE d.date = :today`) depends on existing at 08:00. If the spurious midnight write is the ONLY thing that currently creates that row (on a day when the repair takes the settlement-map path), removing the double-fire could leave the 08:00 job with zero rows to update. Trace this with a real query before finalizing — do not assume it's safe.

### D1b — `MarketLifecycle` restart hydration

A restart landing inside `[last_close, last_close + settled_offset)` loses in-memory `_last_close_at`, so the settled-close event for that close episode can silently never fire at all post-restart (a missed fire, not a double-fire). Mirror `agent_engine.py`'s `_v2_hydrate_latch()` pattern: add `_hydrate_from_audit()` to `MarketLifecycle`, called once per process (guarded by a `self._hydrated` flag) from inside `poll()`, hydrating from `MarketLifecycleEvent` rows (`backend/api/models.py:2301-2321`, already written with a `fired_at` timestamp) for today's IST date — restore `_last_close_at`/`_settled_fired` state to what it should be, without retroactively firing a settle event for a close episode that's already well past its window.

### D1c — restart-safety for the 05:30 and 08:00 scheduled jobs (confirmed live risk, not just this bug)

Two independent, confirmed-by-direct-code-read gaps:

- **`_task_holiday_refresh`** (`background.py:3713`): pure wall-clock sleep-until-next-05:30 loop, no DB check. A restart after 05:30 silently skips that whole day's calendar refresh with no catch-up (masked by Tier-3 DB cache serving yesterday's data, but a same-day calendar change would be missed).
- **`_sg_recover_close_reset`/`_ds_tick_prev_close_fix`** (`background.py:3081-3087` and `3328`): confirmed via direct read — `_session_guard()` unconditionally calls `_sg_recover_close_reset` whenever `now_t >= 08:00`, with no "already ran" check and no upper bound. The settlement-map UPDATE it triggers (`daily_snapshot.py:1240-1248`) does `SET prev_close = :close_price, ltp = :close_price` unconditionally for every today-dated holdings/positions row. **This means any server restart at any time after 08:00 — not a corner case, every single restart — stomps `daily_book.ltp` back to the morning's settlement reference price, silently erasing that day's price movement.** This is a live, present-tense bug independent of tonight's specific scenario. The loop's own local sentinel (`_prev_close_fix_done`, reset to `None` on every restart) can additionally cause this same fix to double-fire via a second, uncoordinated code path.

**Fix:** give both jobs a DB-backed "did today's run already happen" marker, following the existing, already-correct pattern `_preload_snapshot_sentinels()` uses for its sibling sentinel (seeded from `daily_book` at startup, documented explicitly as restart-safe). Use one shared marker (new small `daily_task_markers(task_name, run_date)` table, or the existing settings-store getter/setter if one fits) consulted by BOTH `_sg_recover_close_reset` and the loop's tick — not two separately-fixed call sites, since fixing only one leaves the double-fire possible via the other.

### Tests
- `backend/tests/test_market_lifecycle.py`: rollover-after-settled sequence (currently zero coverage); restart-mid-settle-window hydration case (fires once; does not re-fire if already settled).
- `backend/tests/test_daily_snapshot.py`: `snapshot_daily_book(target_date=None)` at 23:50 vs 00:05 same session → same `target_date`.
- `backend/tests/test_background_helpers.py` (new cases): restart-after-05:30 catches up once; restart-after-08:00-already-done does not re-run; restart-after-EOD-with-fix-already-done leaves today's EOD `ltp` untouched (the confirmed stomp bug).

### Verification
Re-run nightly for a week post-fix:
```sql
SELECT count(*) FROM daily_book d1
JOIN daily_book d2 ON d1.account=d2.account AND d1.symbol=d2.symbol AND d1.kind=d2.kind
WHERE d2.date = d1.date + 1
  AND d1.prev_close IS DISTINCT FROM d1.ltp
  AND d2.prev_close = d2.ltp;  -- must be 0 going forward
```

---

## Defect 2 — Fragmented market open/close determination

**Verified:** only `_segment_is_open` (`date_time_utils.py:299-327`) and `_exchange_is_open` (`market_lifecycle.py:409-445`) correctly thread `sessions=`/`evening_open_on_holidays=`/`special_sessions=` through `is_market_open()`. Six other call sites use the legacy positional form and get a different (currently wrong, for tonight's MCX evening) answer: `background.py:996` (`_perf_probe_open_segments`), `background.py:5461` (`_watchdog_check_market_open`), `routes/market.py:153-191` (`_compute_market_status` — user-visible, currently returns `mcx_open: False` live right now), `routes/quote.py:287-306`, `routes/simulator.py:187-233`, `routes/watchlist.py:1385-1404`. A fourth, fully independent mechanism exists in `agent_engine.py:_ae_segment_flags` (live Kite bellwether probe) — deliberately left separate (see Recommendation). A newly-found **fifth, independent mechanism**: `backend/api/helpers/exchange_clock.py`'s DB-schedule-based open/closed signal, used throughout `background.py` for EOD scheduling — out of scope for this pass, flagged as a known risk.

**Fix:** route all 6 broken call sites through `_segment_is_open(seg_cfg, now)` rather than threading flags into each individually — gives Defect 3's new signal exactly one place to plug in. Files: `background.py` (2 sites), `routes/market.py`, `routes/quote.py`, `routes/simulator.py` (2 sites), `routes/watchlist.py`.

**Known risk to flag in the PR:** `simulator.py` and the ticker watchdog currently call the positional form without `exchange=`, so `is_trading_day()`'s live-probe fallback never fires for them today. After unification (which always passes `exchange=`), they'll start incurring real Kite-probe calls in the holiday/in-window edge case — monitor call volume; add an optional `probe=False` passthrough to `_segment_is_open` if this becomes measurable cost.

**`agent_engine.py`'s live-probe path:** leave as a deliberate secondary safety net, not reconciled in this pass — it exists precisely to catch cases where the calendar-based verdict is wrong.

### Tests
Unit test asserting all 6 unified call sites return the same boolean for a constructed "MCX holiday evening, in-window" fixture.

### Verification
`/market/status`'s `mcx_open` must agree with `MarketLifecycle.get_state()["open"]["mcx"]` live during an MCX evening session (confirmed divergent tonight).

---

## Defect 3 — MCX morning/evening holiday granularity

**Verified:** NSE's own feed carries per-segment `morning_session`/`evening_session` fields (confirmed live: MCX's `COM` segment shows `evening=Open` for today, `evening=Closed` for ~8 of MCX's 21 2026 holidays). `_fetch_holidays_from_nse` (`broker_apis.py:3289-3316`) discards this, keeping only the bare date. In its place, a static `evening_open_on_holidays: true` flag assumes every MCX holiday reopens at 17:00 — wrong for those ~8 dates. `market_special_sessions` already supports a zero-width `(start_time == end_time)` row evaluating "closed all day" via existing logic in `_check_special_sessions` — no schema change needed.

**Fix (reuse existing mechanism, option b over a new column):**
- `broker_apis.py:_fetch_holidays_from_nse`: also capture `morning_session`/`evening_session` from the `COM` segment entries.
- `_task_holiday_refresh`: after the holiday UPSERT, auto-insert a zero-width `market_special_sessions` row (`exchange="MCX"`, `date=d`, `start=end=00:00:00`) for each evening-closed date — **excluding any date that also falls on a weekend** (verified: `next_market_open_ist()`'s weekend branch treats row-existence as "open," so an auto-inserted closed row on a weekend date would incorrectly flip that Sunday to "open"; 2026-11-08 is confirmed a Sunday and must be excluded). Add a `source` column to `MarketSpecialSession` if none exists, to prevent auto-ingestion colliding with an operator-entered Muhurat-style row for the same date.
- `date_time_utils.py:is_market_open`: add a missing `now.weekday() < 5` guard to the holiday branch (confirmed absent — a holiday landing on a weekend with `evening_open_on_holidays=True` currently reads "open" at 17:05 even on Saturday/Sunday, independent of the special-session fix).

### Tests
Diwali Laxmi Pujan (2026-11-08) with seeded zero-width row → `is_market_open` returns `False` for MCX at 18:00; confirm the weekend-exclusion guard (row never inserted for that date); weekday-guard regression test.

### Verification
```sql
SELECT * FROM market_special_sessions WHERE exchange='MCX' AND date >= '2026-10-20' ORDER BY date;
```

---

## Defect 4 + 5 — Frontend Chg% basis and blank-vs-zero

**Verified, reframed per "single source" principle:** `_applyQuoteFields` (`pulseUnified.js:204-231`) writes `row.close` from live-quote sources first (`snap.close`/`liveQ.close` — ultimately Kite's raw `quote.ohlc.close`, confirmed via `quote.py:155-163`), with the backend's correct `prev_close` only as last resort. `finalizeRows` computes `_prev_market_value` purely from this `row.close`, which `dayChangePct()` (`nav.js:139-143`) divides into `day_pnl` — a poll-sourced numerator against a live-quote-sourced denominator, on the same row. `dayChangePct()` also returns `null` (blank cell) rather than falling back when the denominator is 0, unlike the backend's own `day_change_percentage` which has an `|avg×qty|` fallback.

**Fix (elimination, not reordering):** add a dedicated accumulator field (`_prevCloseSsot`, accumulated per-leg across positions+holdings composite rows, mirroring how `_avg_num` is already accumulated-then-consumed) populated ONLY from the backend's `r.prev_close` — never touched by any live-quote code path. `finalizeRows` computes `_prev_market_value` from this new field instead of `row.close`. `row.close` itself is UNCHANGED and keeps serving bid/ask/depth-adjacent display, where live-quote freshness is correctly desired — this is additive, not a rewrite of an existing field's meaning. For Defect 5, when the new accumulator is 0 (position opened today, no prior settlement), fall back to `|avg_cost × qty|` using the already-in-scope `_avg_num` locals, mirroring the backend's existing fallback pattern — implement at the accumulator/call-site level, not inside `dayChangePct()` itself.

**Files:** `frontend/src/lib/data/pulseUnified.js` (both merge loops + `finalizeRows`), `frontend/src/lib/data/pulseColumns.js` (check lines 282/307's `change_pct ?? day_pnl_pct` — verify whether this feeds the same Chg% column or a genuinely different surface like movers badges before deciding whether it needs the same fix).

### Tests
Vitest: stale live-quote `close` present alongside correct `r.prev_close` → `_prev_market_value` uses `r.prev_close`; multi-leg positions+holdings composite row accumulation; zero-denominator fallback renders a real percentage, never blank.

### Verification
Load the Positions grid for a live-ticking symbol; Chg% and ₹ Day P&L must agree in sign (today they can disagree because they use different bases).

---

## Defect 6 — Underlying-anchor rows: consolidate onto the shared quote-apply pipeline

**Verified:** `mergeUnderlyingAnchors` (`pulseUnified.js:668-727`) calls a bespoke `_applyUnderlyingQuoteFields(row, q)` at two call sites (~704, ~726) — a structurally parallel but separate code path from `_applyQuoteFields(row, snap, liveQ)`, which every real position/holding/watchlist row uses. `_applyUnderlyingQuoteFields` also uses fill-only-when-null semantics (vs. `_applyQuoteFields`'s overwrite-on-fresh-snap semantics) — a second source of possible staleness beyond just "different pipeline." `loadUnderlyingSpots()` already calls `publishPulseQuotes()` to push its batch-quote results into the shared `symbolStore` — the data-source side is already partially unified; this fix finishes the job on the consuming side.

**Fix (decided, consolidation not a bespoke patch):** replace both `_applyUnderlyingQuoteFields(row, q)` call sites with the same `_applyQuoteFields(row, snap, liveQ)` entry point real rows use, sourcing `snap`/`liveQ` the same way (`snapOf(sym)` / the live-quote bag) for the anchor's resolved future/underlying symbol. Confirm during implementation that the object shape `_applyQuoteFields` expects (ltp/bid/ask/open/close/day_change/day_change_pct/volume/oi) is compatible with what the anchor's quote bag currently provides — adapt the shape at the call site if needed, rather than forking `_applyQuoteFields` itself. Delete `_applyUnderlyingQuoteFields` once both call sites are migrated — don't leave a dead duplicate. This directly resolves tonight's live observation (anchor shows 0% while the real option legs show nonzero Chg%) via the same, already-correct, tick-preferred chain — independent of whether Defect 2's `_qt_is_seg_open` fix would also have helped; no need to sequence D6 behind D2.

Visual distinction for anchor rows already exists and is reasonably thorough (`_isUnderlyingAnchor` → glyph "X" in `positionStateBadge.js:82`, `sym-anchor-bar` CSS class, `chg-up`/`chg-down` suppressed per `pulseColumns.js:151-152,173-174`). Add only a small tooltip/label clarifying the anchor reflects the front-month future, not the held option — a UX addition, not a pricing-pipeline change.

### Tests
Verify both call sites migrated and `_applyUnderlyingQuoteFields` deleted (grep-based check is fine); simulate a quote-bag transition from closed (0%) to open (nonzero) across two refresh cycles and assert the anchor row updates without the stale fill-only-if-null guard blocking it.

### Movers — scope note, not a fix in this batch
Movers has its own separate backend poller — a known, pre-existing gap, not one of tonight's confirmed defects. Not blocking this batch; mention only if it turns out trivially bundleable with D6's consolidation work.

---

## Architectural principle ("single arbitrated source per field") — codified concretely

| Field | Primary source | Secondary (fallback-only) | Status after this batch |
|---|---|---|---|
| Positions/Holdings `ltp` | SSE/WS tick (`snap.ltp`) | REST batch-quote (`liveQ.ltp`) → broker-poll `last_price` seed | No change — already correct |
| Positions/Holdings `day_pnl` | Backend poll, baseline-diff formula | none (structurally poll-only) | No change — correct by design |
| Positions/Holdings Chg% denominator | Backend poll-derived `prev_close` (same poll feeding `day_pnl`) | none | **Fixed by D4/D5** |
| Watchlist/pinned LTP | SSE/WS tick | REST fallback | No change — already correct |
| Underlying-anchor close/change/change_pct | SSE/WS tick via `_applyQuoteFields` | REST batch-quote fallback | **Fixed by D6** |
| Movers LTP | Own poller (unchanged this batch) | n/a | Documented gap, not fixed |

"Done when" for D4/D5/D6 is keyed to this table holding, not merely to tonight's specific numbers looking right. The documented "tick forwarder killed, LTP froze while Chg% moved" incident is the same underlying shape (two channels feeding one row with no arbitration) — not rearchitected further in this pass, but the test suite above should not regress it.

---

## Agents
- backend: Defect 1 (market_lifecycle.py, daily_snapshot.py, background.py restart-safety markers), Defect 2 (background.py, routes/market.py, routes/quote.py, routes/simulator.py, routes/watchlist.py, date_time_utils.py), Defect 3 (broker_apis.py, models.py, date_time_utils.py, background.py)
- frontend: Defect 4, 5, 6 (pulseUnified.js, pulseColumns.js, nav.js, anchor-row tooltip component)
- broker: skip (Defect 3 touches the NSE holiday-feed parser in broker_apis.py, not a trading adapter)
- doc: update CLAUDE.md's documented invariants with the "single arbitrated source per field" principle and corrected prev_close/is_market_open conventions once D1-D4 land
- backend-test: test_market_lifecycle.py, test_daily_snapshot.py, test_background_helpers.py (new restart-safety cases), test_date_time_utils.py (unified call-site + weekday-guard tests)
- playwright: skip — these are data-correctness/scheduling/calculation fixes; manual live-grid verification per defect is more direct

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message (one per item)
1. `fix(market-lifecycle): stop close_settled double-firing on midnight date rollover`
2. `fix(market-lifecycle): rehydrate settled-close state from audit log on restart`
3. `fix(background): make holiday-refresh and prev-close-fix cron jobs restart-safe`
4. `fix(market-hours): unify all is_market_open() call sites through _segment_is_open`
5. `fix(holidays): capture per-segment MCX evening-session closure from NSE feed`
6. `fix(pulse): Chg% denominator reads backend prev_close exclusively, never live quote OHLC`
7. `fix(pulse): dayChangePct never renders blank for a held position`
8. `fix(pulse): underlying-anchor rows consolidate onto the shared quote-apply pipeline`

Each ends with:
```
Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>
```

## Done when
- D1: midnight-rollover regression test passes; a week of post-fix data shows zero corrupted next-day rows; restart-mid-settle-window test passes; both scheduled jobs' catch-up/no-duplicate-rerun tests pass (including the confirmed EOD-stomp case).
- D2: all 6 unified call sites return identical booleans for the same fixture; `/market/status` agrees with `MarketLifecycle.get_state()` live.
- D3: correct zero-width rows exist for all non-weekend 2026 MCX evening-closed holidays, confirmed absent for weekend dates; weekday-guard test passes.
- D4/D5: Chg% and ₹ Day P&L agree in sign for a live-ticking symbol; qty≠0 rows never render blank; multi-leg accumulator test passes.
- D6: anchor row's close/change_pct come from `_applyQuoteFields`; `_applyUnderlyingQuoteFields` deleted; §architectural-principle table holds for anchor rows.
- Data repair: operator has explicitly chosen one of the options below — not defaulted into one.

## Risks / Open Questions (not decided here)

**Data repair for already-corrupted 2026-10-20 rows:**
1. *No backfill* — corruption is scoped to today only; tomorrow's normal 08:00 repair (once D1 ships) populates 10-21 fresh from broker settlement data, bypassing today's row entirely in the common path. Lowest effort, tolerates wrong numbers for the rest of today only.
2. *Manual, reviewed, idempotent backfill* (separate PR from D1a) — restore each affected row's `prev_close` from its own prior-dated row (confirmed still correct), never from `prev_close_backup` (confirmed stale/frozen from first-ever creation). Must branch by exchange: NSE holdings (fully closed all day) vs. MCX positions that genuinely traded tonight need different reference logic — a blind uniform rule risks overwriting legitimately-updated MCX values.
3. *Wait and re-check tomorrow* using the verification query above before deciding — cheapest, buys information, narrows the decision.

Recommendation: option 3 first, falling back to option 1 or the exchange-split option 2 based on what tomorrow's check shows.

**Other flagged, not fixed in this pass:**
- `positions.py` vs `holdings.py` use two independently-written row-selection queries that can disagree on which `daily_book` row is "current" for the same instrument — full reconciliation out of scope; D1a must be verified not to worsen this.
- `exchange_clock`'s independent DB-schedule-based open/closed mechanism remains a separate, unreconciled source of truth after this batch — flagged for a future pass, not fixed here.
- Defect 2's unification may increase live-probe (Kite API) call volume for `simulator.py`/the ticker watchdog — monitor after deploy.

### Critical files
`backend/api/algo/market_lifecycle.py`, `backend/api/algo/daily_snapshot.py`, `backend/api/background.py`, `backend/shared/helpers/date_time_utils.py`, `backend/brokers/broker_apis.py`, `backend/api/models.py`, `frontend/src/lib/data/pulseUnified.js`, `frontend/src/lib/data/pulseColumns.js`, `frontend/src/lib/data/nav.js`
