# Plan: Retime MCX/NFO expiry close to 1 hour before close + ntfy on the critical alert

## Task

MCX expiry is in 4 days (2026-11-26). Operator wants unnetted ITM positions closed by
the agent system 1 hour before market close, with every close HELD until the operator
explicitly releases it. Investigation confirmed the hold-by-default behavior (no
auto-release), the live trade_mode, the MCX DST-aware close time (23:55 IST this
winter window), and the 4-rule unnetted-ITM classification are all already correct and
live on prod. The real gaps: the alert/close-action pair fired at 30/25 minutes before
close (not 1 hour); a legacy daily task fired its own close pass at a separately
configured 30-minute offset; the critical close-action alert had no ntfy channel; and
widening the close window to 60 min opened a real coverage gap (no periodic MCX
re-scan between T-60 and close, unlike NFO's existing 30-min re-scan loop).

Mid-implementation, the operator also asked that the lead-time value be a genuine
parameter of the resolver function in the DSL grammar, not a hardcoded constant
duplicated across two files, generalizing the mechanism for MCX and NFO alike.

## Agents

Implemented directly (no subagent dispatch — small, precision-critical, closely
coupled to live DB state verified mid-session via direct SSH/psql reads on both dev
and prod):

- `backend/api/algo/grammar_tokens/metric_is_expiry_preclose_alert_window.py` —
  introduced `_metric_factory_is_expiry_preclose_window(lead_minutes)`, a factory
  closure mirroring the existing `metric_max_drawdown_pnl.py` precedent. The shared
  exchange-loop helpers (`_expiry_preclose_exchanges`, `_expiry_preclose_within`) were
  already generic; only the metric itself was a one-off hardcoded function. Bound
  `_metric_is_expiry_preclose_alert_window = _metric_factory_is_expiry_preclose_window(ALERT_LEAD_MINUTES)`
  with `ALERT_LEAD_MINUTES = 65`.
- `backend/api/algo/grammar_tokens/metric_is_expiry_preclose_close_window.py` —
  imports the same factory, binds `CLOSE_LEAD_MINUTES = 60`.
- `backend/api/algo/agent_engine.py`:
  - `expiry-day-preclose-alert`: description/long_name text updated to 65 min;
    `cooldown_minutes` 180→30 (was "fire once," now "re-fire every 30 min inside the
    window" — periodic re-notification of newly-staged legs).
  - `expiry-day-itm-auto-close`: description/long_name text updated to 60 min;
    `cooldown_minutes` 180→30 (closes the MCX periodic-re-scan gap — each re-fire
    re-runs `ExpiryEngine.scan_positions()`, safe to repeat because
    `record_held_order()`'s per-leg dedup guard no-ops an already-held leg); added
    `{"channel": "ntfy", "enabled": True, "priority": "high"}` to `events`.
  - `_EXPIRY_RISK_AGENTS` (`expiry-nfo-risk-alert`): suppression leaves retimed
    30→65 on both branches (NFO and MCX), so the risk alert stays quiet for the
    agent's whole widened pre-close window, not just the last 30 of 65 minutes.
  - `_AE_CONTENT_MATCHED_COND_MIGRATIONS["expiry-nfo-risk-alert"]`: replaced with the
    exact byte-for-byte 30-min merged `any[]` shape confirmed live on both dev and
    prod via direct DB reads, so the one-time migration force-upgrades the
    already-seeded row to the new 65-min shape on next restart.
  - Explanatory comments above `_EXPIRY_DAY_AWARENESS_AGENTS` and
    `_EXPIRY_RISK_AGENTS` rewritten to describe the new 30-min-cooldown,
    periodic-re-scan rationale (previously documented a "fire once, 180-min
    cooldown" design that no longer applies).
- Tests (written directly, all passing):
  - `backend/tests/test_expiry_day_and_preclose_agents.py` — lead-time assertions
    updated 30/25→65/60; cooldown assertions updated 180→30; ntfy-channel test
    added; `TestExpiryDayAgentsSameDayNoSuppression` extended with a NEW case proving
    a re-fire >30 min later (still inside the same window) is now ALLOWED (the
    periodic re-scan this retime exists for), alongside the existing
    same-exchange-double-fire-suppressed and cross-exchange-independence cases.
  - `backend/tests/test_expiry_same_window_consolidation.py` — suppression-window
    literals updated 30→65 throughout `TestExpiryRiskAlertMerged` (the unrelated
    `market-preclose-nonmcx`/`market-preclose-mcx-v2` tests, which use their own
    separate, untouched 30-min condition, were left unchanged).
  - `backend/tests/test_expiry_logic.py` — two new end-to-end tests driving the REAL
    `_action_live_expiry_auto_close` + REAL `ExpiryEngine.scan_positions()`/
    `close_positions()` (only the broker-touching fetch methods mocked): one proving
    a genuinely unnetted-ITM MCX leg gets staged via `record_held_order()` and never
    reaches `chase_order()` while `hold.expiry_close_released=False`; a second
    proving the reverse (operator releases the hold → routes to `chase_order()`
    instead), confirming the first test actually exercises the branch it claims to.

## Tests
- pytest: yes — 11045 passed, 0 failed, 75 skipped/4 xfailed/2 xpassed (all
  pre-existing), full suite. Broker coverage 88% (≥80% gate). API coverage 68%
  (≥45% gate).
- svelte-check: yes (baseline sanity only — backend-only change) — 0 errors, 5
  pre-existing unrelated warnings.
- playwright: no

## Commit message

fix(agents): retime MCX/NFO expiry pre-close alert+close to 1 hour before close; close the MCX re-scan gap; make the lead time a genuine factory parameter

## Done when

- `ALERT_LEAD_MINUTES == 65`, `CLOSE_LEAD_MINUTES == 60`, both produced by the same
  `_metric_factory_is_expiry_preclose_window(lead_minutes)` factory — DONE.
- `expiry-day-itm-auto-close`'s seeded `events` includes an enabled ntfy(high) entry
  — DONE.
- Both agents' `cooldown_minutes == 30`, enabling periodic re-scan within each
  exchange's own window — DONE.
- `expiry-nfo-risk-alert`'s suppression leaves retimed to 65, with a migration table
  entry that force-upgrades the exact live DB shape (confirmed on dev AND prod) —
  DONE.
- All updated/new tests green, full backend suite green (11045 passed), CC gate
  clean, coverage gates pass — DONE.
- NOT YET DONE (follow-up, tracked outside this plan file): set
  `algo.expiry_start_offset_hours` to `1.0` on dev and prod settings tables (runtime
  config, not code — operator confirmed this should move from 0.5 to 1.0 to match
  the new agent timing and avoid a double-held-row risk); push to dev then prod via
  `/ddev` → `/dprod`; verify live on prod before the 2026-11-26 expiry day.
