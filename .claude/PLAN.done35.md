# Plan: Robust self-healing for the API-side tick-forwarding poller

## Context

Live incident (confirmed via `ssh ramboq` + code read): on both prod and dev, the API
process's local `MmapTickReader._poll_task` — the task that tails the shared-memory
tick buffer (`/dev/shm/ramboq_ticks`, written by the healthy conn-service process) and
republishes ticks to the browser-facing `/api/quotes/stream` SSE endpoint — gets killed
every night at ~23:31 IST by `_snapshot_stop_ticker()` and is never revived by the
intended 08:00 IST `_snapshot_restart_ticker()` job. Root cause: both functions detect
"are we in conn-service mode" via `hasattr(ticker_obj, "stop"/"restart")` instead of the
canonical `is_cutover_on()` flag — and that guess is wrong, because `MmapTickReader` DOES
have a real `.stop()` (which cancels the poll task) but has no `.restart()` (so the
revival silently no-ops). Symptom on screen: LTP freezes at page-load value, no
flash (flash is tick-driven), while Chg% keeps moving because it's computed from an
independent periodic positions poll. This has recurred most nights since at least
2026-09-19 — deploys have been masking it by accident (any process restart revives it).

Operator's explicit ask: "I think the poller design should be robust. It is happening
on and off." A 6-member council (architect/risk/devil's-advocate/perf/ux/research)
reviewed the fix; their findings are folded into the design below. Operator decision:
ship the full robust package in one pass, including a frontend-visible staleness
indicator.

## Approach

### 1. Backend root cause — `backend/api/background.py`

Replace `hasattr()` duck-typing with the canonical `is_cutover_on()` check at all three
sites: `_snapshot_unsub_nonmcx`, `_snapshot_stop_ticker`, `_snapshot_restart_ticker`.
Conn-service mode: stop/unsub become true no-ops; restart calls `ticker.check_and_heal()`
as a defensive second layer. Direct-KiteTicker mode: unchanged. Fixed two misleading
docstrings (wrong "00:30 IST" claim, wrong "has no stop method" claim).

### 2. Self-healing, encapsulated in the broker layer — `backend/brokers/mmap_ticker.py`

Fixed `start()`'s guard to also catch a done-but-non-None task. Added `check_and_heal()`:
heals a dead poll task, logs `[MMAP-WATCHDOG]` once per dead->alive transition with the
captured exception if any, and escalates with a second distinct critical log if 3+ heals
occur within a rolling 10-minute window.

### 3. Continuous liveness supervision — `_task_ticker_watchdog`'s cutover branch

Replaced the pure `sleep(300)` no-op with an active 30s-cadence call to
`ticker.check_and_heal()`, gated on the engine not being idle (added defensively — not
in the original brief, needed to avoid false-firing during dev boot before a trading
mode is picked).

### 4. Frontend — operator-visible staleness indicator

New `lastLiveTickAt` store in `quoteStream.js` (set only on real SSE tick/snapshot
events — deliberately NOT `symbolTickCount`, which also advances on background polls
and would have masked this exact failure). New `tickStaleness.svelte.js` watchdog
(75s threshold, reuses existing market-open check) wired into `RefreshButton.svelte` as
an amber `TICKS STALE@HH:MM` badge + one toast per staleness episode.

## Outcome

Implemented by two parallel specialist agents (backend, frontend), each writing their
own tests. Backend: 110 targeted tests + 425 broader regression, radon clean. Frontend:
svelte-check 0 errors, 3/3 Playwright passed across all viewport projects. Full-suite
gate additionally surfaced one unrelated pre-existing stale-test-fixture bug
(`test_options_spot.py::test_option_on_its_own_expiry_day_does_not_roll_past_a_still_live_future`,
a `_future_month(0)` helper hardcoding day=18 — broke on the 19th+ of any month),
fixed separately in its own commit per "fix one issue at a time." Full suite green:
9305 passed, 0 failed.

## Commit message

`fix(ticker): stop nightly hasattr() bug from killing the local tick poller; add self-heal watchdog + operator-visible staleness indicator`
