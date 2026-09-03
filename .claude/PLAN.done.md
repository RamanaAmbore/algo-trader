# Plan: Full doc accuracy pass + SSOT fix (all 28 docs audited)

## Context
Six parallel audit probes covered all 28 documentation surfaces + codebase SSOT.
Findings: 2 P0 code fixes, 1 P0 spec correction, ~20 doc gaps across specs and guides.
Documents verified as accurate (no changes): PULSE_SPEC, NAVSTRIP_SPEC, CHART_SPEC,
DASHBOARD_SPEC, SIMULATOR_SPEC, LAB_SPEC, DATA_LIFECYCLE, ORDER_LIFECYCLE,
LAB_MCP_GUIDE, ACTIVITY_SPEC, AUTOMATION_SPEC, REPLAY_SPEC, MIGRATION.md.

## Code fixes (P0)

**P0-A**: `r_rate = 0.07` hardcoded in `positions.py:1248` and `expiry.py:320`.
Both should import `DEFAULT_RISK_FREE` from `backend/api/algo/derivatives.py:39`.

**P0-B**: `DEFAULT_IV` in DERIVATIVES_SPEC claims 0.30; actual code `derivatives.py:41`
is `DEFAULT_IV = 0.15`. This is a spec-only fix (the code value is correct).

## Agents

- backend: Fix DEFAULT_RISK_FREE SSOT violation (P0-A):
  1. Add `from backend.api.algo.derivatives import DEFAULT_RISK_FREE` to imports in `backend/api/routes/positions.py`. Remove the `r_rate = 0.07` local at line 1248; replace `r_rate` with `DEFAULT_RISK_FREE` at lines 1268-1269.
  2. Add `from backend.api.algo.derivatives import DEFAULT_RISK_FREE` to imports in `backend/api/algo/expiry.py`. Replace local `r = 0.07` at line 320 with `DEFAULT_RISK_FREE`.
  For every file you change, you MUST write or update at least one test covering the changed behaviour:
  - `positions.py` change → pytest in `backend/tests/` verifying the greeks path uses `DEFAULT_RISK_FREE` (not a hardcoded literal).
  - `expiry.py` change → pytest in `backend/tests/` verifying the expiry greeks path uses `DEFAULT_RISK_FREE`.
  No change ships without a corresponding test update.

- frontend: skip

- broker: skip

- doc agent 1 — DESIGN_GUIDE.md (`docs/DESIGN_GUIDE.md`):
  - **Ports**: Add a note distinguishing internal uvicorn ports (8000 prod / 8001 dev, per `webhook/deploy.sh`) from public nginx ports (8502 prod / 8503 dev). Both layers exist; DESIGN_GUIDE documents the public ones — add a sentence clarifying the distinction.
  - **ExchangeSchedule schema**: In the DB model section, rename `reset_time` → `snapshot_reset_time`. Remove any mention of `updated_at` (field does not exist). Do NOT add a `source` column — it does not exist in the model (verified against actual codebase).
  - **position_pair_groups**: Mark POST `/api/positions/pair` and `position_pair_groups` table clearly as **[Future roadmap — not yet implemented]**. They do not exist in the codebase.
  - **pnl_pct token**: In the grammar token definition for `pnl_pct`, add: "When `util debits = 0` (intraday/MIS positions), denominator falls back to `net` margin (available margin). Returns None only when both are zero. Changed 2026-09-03."
  - **spot_prices diagnostic**: In the background task / agent evaluation section, add one bullet: "WARNING logged once per tick when `spot_prices` dict is empty — expiry ITM agents skipped that tick."
  - **Broker resilience** (section 14): Add notes on Dhan-specific staggered pre-warm (per-account delay to prevent token race) and cooloff persist (cooloff state survives process restarts). Instruments TTL cache (24h, daily 08:00 IST refresh) already documented — just confirm correct.

- doc agent 2 — User-facing guides (`docs/guides/USER_GUIDE.md`, `docs/guides/AGENTS_GUIDE.md`, `docs/guides/ADMIN_GUIDE.md`, `docs/specs/BROKER_SPEC.md`):
  - **USER_GUIDE § Built-in agents**: Change "16 loss / risk / market-status agents" → "9 built-in agents". List all 9: `loss-positions-acct`, `loss-rate-acct`, `loss-positions-total`, `loss-margin-low`, `loss-funds-negative`, `loss-pos-total-auto-close`, `expiry-day-positions-alert`, `expiry-day-equity-itm-auto-close`, `expiry-day-commodity-itm-auto-close`.
  - **USER_GUIDE § Alerting / notification channels**: Update to list all 6 channels: `telegram`, `email`, `ntfy` (push via ntfy.sh), `inapp` (in-app notification), `websocket` (browser WebSocket), `log` (server log only). Remove any "Telegram + email only" language.
  - **AGENTS_GUIDE § pnl_pct**: Add the intraday fallback note — "When `util debits = 0`, metric uses `net` (available) margin as denominator instead."
  - **AGENTS_GUIDE § rate metrics**: Add explicit cold-start behavior — "`pnl_rate_abs`, `pnl_rate_pct`, `day_rate_abs`, `day_rate_pct` return None until ≥2 samples exist in the rate window (~5 min after session start). These metrics are silent during this period."
  - **AGENTS_GUIDE § built-in agents**: Update list from 6 to all 9 agents. Add `loss-rate-acct`, `loss-margin-low`, `expiry-day-equity-itm-auto-close`.
  - **ADMIN_GUIDE § Settings / SEEDS**: Add a paragraph: "`dev_default` (8th tuple element in SEEDS): when present, non-main branches (workshop/dev) seed this value instead of `default`, allowing dev environments to start with safe defaults (e.g. alerts disabled) without manual DB updates."
  - **BROKER_SPEC § 6AM pre-warm**: Expand per-broker details — Kite pre-warms 05:45–05:59 IST (Kite tokens expire at 06:00); Dhan pre-warms when token age > 22h (expiry at 23h); Groww pre-warms on `_is_token_expired()` check. Implementation: `backend/brokers/service/app.py:_hourly_token_prewarm`.

- doc agent 3 — Backend specs (`docs/specs/AGENTS_SPEC.md`, `docs/specs/SETTINGS_SPEC.md`, `docs/specs/PERSISTENCE_SPEC.md`, `docs/specs/SYMBOLS_SPEC.md`):
  - **AGENTS_SPEC § pnl_pct definition**: Change "P&L as % of cost/value" → "P&L as % of used margin (`util debits`). Falls back to `net` margin when `util debits = 0`. Returns None when both are zero."
  - **AGENTS_SPEC § rate metrics cold-start**: Add: "Rate metrics return None until ≥2 samples are accumulated in the rolling window. No alert fires during the baseline accumulation period (~first 5 min of session)."
  - **AGENTS_SPEC § built-in agents**: Update count from 7 to 9. Add missing: `loss-rate-acct`, `expiry-day-equity-itm-auto-close`.
  - **AGENTS_SPEC § skip_channels**: Document the `skip_channels: frozenset` parameter on `dispatch()` — "Prevents listed channels from firing when a richer alert has already handled them (e.g. skip telegram when inapp fired it)."
  - **SETTINGS_SPEC § notification flags**: Replace the 3-flag list with complete 10-flag list: `agent_alerts_enabled`, `telegram_enabled`, `email_enabled`, `ntfy_enabled`, `market_summary_enabled`, `visitor_report_enabled`, `market_feed_enabled`, `genai_enabled`, `monthly_statement_email`, `notify_on_deploy`.
  - **SETTINGS_SPEC § _upsert_seeds**: Add: "Idempotent — uses ON CONFLICT DO NOTHING; existing DB values are never overwritten by seed updates."
  - **SETTINGS_SPEC § dev_default**: Add the dev_default SEEDS parameter documentation (same as ADMIN_GUIDE entry above).
  - **PERSISTENCE_SPEC § EventQueue**: Add `agent_event_queue` entry: `EventQueue(AgentEvent, name="agent_event", batch_size=500, flush_interval_s=1.0, max_queue=10_000, on_full="drop")` — coalesces N fires/cycle into one bulk INSERT.
  - **SYMBOLS_SPEC § Instruments TTL cache**: Add: "24-hour TTL, refreshed at 08:00 IST via `_instruments_cache` — prevents repeated broker API calls. Cold-boot populates at startup."
  - **SYMBOLS_SPEC § MCX lot-size overrides**: Clarify placement — "Override map keyed by Kite `name` field; applied during instruments fetch in `backend/api/routes/instruments.py`."

- doc agent 4 — Trading specs (`docs/specs/DERIVATIVES_SPEC.md`, `docs/specs/EXECUTION_SPEC.md`, `docs/specs/GTT_SPEC.md`, `docs/specs/ORDERS_SPEC.md`, `docs/specs/HEDGE_SPEC.md`, `docs/specs/NAV_SPEC.md`):
  - **DERIVATIVES_SPEC § DEFAULT_IV**: Fix value from `0.30` → `0.15` (actual value in `derivatives.py:41`).
  - **DERIVATIVES_SPEC § DEFAULT_RISK_FREE**: Add note that this constant (`derivatives.py:39`) is the canonical source — `positions.py` and `expiry.py` now import from it (as of 2026-09-03 fix).
  - **DERIVATIVES_SPEC § payoff stale-while-revalidate**: Add brief note — "During underlying switch, the previous payoff curve remains visible until the new one loads (stale-while-revalidate pattern in frontend)."
  - **EXECUTION_SPEC § G1 guard**: Clarify — G1 (LOT_MULTIPLE) removed from ticket handler boundary, but G1 check still runs at top of `apply_plan_live()` in the GTT layer before any broker call. Two separate checkpoints.
  - **EXECUTION_SPEC § 50-lot ceiling**: Add — "The ceiling only applies in `place_order`. `place_gtt` has no intent bypass — a separate ceiling applies unconditionally."
  - **GTT_SPEC § apply_plan_live ordering**: Clarify that G1 synchronous guard runs BEFORE `translate_qty` call per leg (not after). G1 → translate_qty → broker.place_gtt.
  - **ORDERS_SPEC § Dhan postback**: Add note — "Dhan webhook URL must be manually configured in Dhan partner dashboard (`https://ramboq.com/api/orders/dhan_postback`). If not configured, fills only detected at next 5-min `_task_performance` poll."
  - **HEDGE_SPEC § β negative case**: Add — "Negative β indicates inverse correlation; proxy still valid but displayed with negative sign. Warning threshold r < 0.7 — below this confidence the hedge is flagged as unreliable."
  - **HEDGE_SPEC § window defaults**: Add explicit defaults — "Equity: 60-day window; MCX: 30-day window (tighter due to seasonal patterns and shorter contract life)."
  - **NAV_SPEC § prev_close fallback chain**: Document the fallback — `daily_book.ltp` (prior session settlement LTP, `captured_at < 08:00 IST`) is the canonical source. If unavailable, falls back to broker `close_price` (stale until 08:00 IST next day). COALESCE pattern explicitly noted as deprecated.

- doc agent 5 — Minor docs (`docs/deployment.md`, `docs/guides/TLM_GUIDE.md`):
  - **DEPLOYMENT.md § branch naming**: Standardize to "workshop/dev/main" throughout. Replace any "non-main branch" phrasing with the specific branch name.
  - **DEPLOYMENT.md § DB init caveat**: After "Tables auto-created on first startup", add: "(1) Only if `RAMBOQ_SKIP_INIT_DB` is not set. (2) Schema migrations are NOT auto-applied — run migration SQL manually for column changes in production."
  - **TLM_GUIDE § Tool Catalog table**: Add CONNCHECK row — "Verifies broker connection health; checks UDS socket reachability and registry state."

- backend-test: skip (backend agent handles tests per standing rule)
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(ssot): DEFAULT_RISK_FREE import in positions+expiry + full doc accuracy pass (28 docs)

## Done when
- `positions.py:1248` and `expiry.py:320` use `DEFAULT_RISK_FREE` import (not hardcoded 0.07)
- Tests added for both changed files; pytest green; broker cov ≥ 80%; api cov ≥ 45%
- DESIGN_GUIDE: port layers clarified, ExchangeSchedule schema corrected, position_pair_groups marked Future, pnl_pct fallback + spot_prices warning documented, Dhan stagger documented
- USER_GUIDE: 9 agents (not 16), 6 channels (not 2)
- AGENTS_GUIDE: pnl_pct fallback + rate cold-start documented, 9 agents listed
- ADMIN_GUIDE: dev_default SEEDS documented
- BROKER_SPEC: per-broker 6AM pre-warm timing documented
- AGENTS_SPEC: pnl_pct definition correct, cold-start documented, 9 agents, skip_channels
- SETTINGS_SPEC: all 10 notification flags listed, dev_default, _upsert_seeds behavior
- PERSISTENCE_SPEC: agent_event_queue batched insert listed
- SYMBOLS_SPEC: instruments TTL cache + MCX overrides placement clarified
- DERIVATIVES_SPEC: DEFAULT_IV = 0.15, RISK_FREE SSOT note, payoff stale-while-revalidate
- EXECUTION_SPEC: G1 two-checkpoint clarification, place_gtt ceiling distinction
- GTT_SPEC: G1 → translate_qty ordering documented
- ORDERS_SPEC: Dhan postback manual config URL documented
- HEDGE_SPEC: β negative case + MCX window defaults (30d/60d)
- NAV_SPEC: daily_book.ltp fallback chain documented
- DEPLOYMENT.md: branch naming consistent, DB init caveats added
- TLM_GUIDE: CONNCHECK tool in catalog
