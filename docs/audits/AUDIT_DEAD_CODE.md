## Dead-Code + Stale-Data Audit — 2026-06-01

### Dead code (safe to delete)

- [backend/shared/helpers/utils.py:100] `is_prod_capable()` — defined but zero call-sites in the entire repo. CLAUDE.md documents it as a "back-compat shim" but no caller exists.
- [backend/shared/helpers/utils.py:120,141,162,180,210,218,237,380] Seven dead utility functions: `word_width`, `delete_folder_contents`, `read_file_content`, `parse_value`, `create_instr_symbol_xref`, `reverse_dict`, `rec_to_dict`, `validate_pin` — each has zero callers outside utils.py itself, confirmed with grep across all .py, .js, .svelte.
- [backend/shared/helpers/broker_apis.py:221–272] 52-line commented-out block — three deprecated `fetch_books`, `fetch_all_*`, and `fetch_data` functions predating the `@for_all_accounts` pattern. The `if __name__ == "__main__"` block below them (lines 275–284) is also effectively dead since this module is never run directly in prod.
- [backend/api/routes/algo.py:271–292] `AlgoController.reload_grammar` at `POST /api/algo/grammar/reload` — duplicate of `POST /api/admin/grammar/reload` in `grammar.py`. Frontend only calls the `/admin/` path. The grammar.py file itself notes this at line 10.
- [backend/shared/helpers/connections.py:10] `from urllib.parse import urlparse, parse_qs` — imported but never used beyond the import line. grep of the 1200-line file finds no other reference.
- [backend/shared/helpers/alert_utils.py:34] `timedelta` imported from datetime — zero uses beyond the import line in this file.

### Likely-dead code (verify before deleting)

- [backend/api/routes/algo.py:153–265] `AlgoController` endpoints `/algo/status`, `/algo/positions`, `/algo/orders`, `/algo/start`, `/algo/stop`, `/algo/chase` — no frontend caller exists in api.js or any .svelte page. The expiry engine now runs as a background task. These were manual-trigger endpoints from before background automation. Uncertain: they may be intended as an operator escape hatch; check if the admin console still hits them.
- [backend/shared/helpers/settings.py:106] Seed key `pulse.tick_interval_ms` is read by `MarketPulse.svelte` directly from the settings API (not via `get_int`/`get_string`) — but there is no backend `get_int("pulse.tick_interval_ms")` consumer. This is fine architecturally (frontend reads via REST), but the SEEDS tuple carries a misleading "performance" category when it's actually frontend-only. Low risk; document the pattern.
- [backend/api/routes/algo.py:295+] `algo_ws_handler` at `/ws/algo` — still actively used by `AgentToast.svelte` and `agents/+page.svelte`. NOT dead. Included here to note that `AlgoController` (same file) is likely dead while the WS handler is live.
- [backend/config/backend_config.yaml: logging.* keys] `file_log_level`, `error_log_level`, `console_log_level` — `backend_config.yaml` says "Live changes from /admin/settings" but these keys are absent from `SEEDS`, so `/admin/settings` UI never shows or creates them. The `_apply_log_level` handler in `settings.py` is correct; the gap is that DB rows must be inserted manually rather than auto-seeded.

### Stale references / hardcoded values

- [backend/config/backend_config.yaml:97–106] All five macro `as_of` dates are in 2025 (repo_rate 2025-12-06, CPI/IIP 2025-11-30, GDP 2025-09-30, INR/USD 2025-12-15). Current date is 2026-06-01 — every entry is ≥6 months stale. The MCP `get_economic_snapshot` tool surfaces a "stale > 90 days" warning to the LLM, so this is surfaced downstream, but the values should be updated.
- [backend/shared/helpers/utils.py: no `market.bellwether_symbols` in SEEDS] `market_probe.py` reads `get_string("market.bellwether_symbols", "")` but this key is absent from `SEEDS`. Operator can't set it from `/admin/settings`; the only way is a raw DB INSERT. Either add it to SEEDS or remove the settings-lookup and hardcode the default.
- [backend/shared/helpers/broker_registry.py / connections.py] `connections.sparkline_account` is read via `get_string("connections.sparkline_account", "")` (registry.py:406) but absent from `SEEDS` — same gap as `bellwether_symbols` above. CLAUDE.md documents the setting as operator-configurable.
- [frontend/src/routes/(public)/+layout.svelte:170,177] `© RamboQuant Analytics LLP` — no year shown. This is actually fine (no stale year). Flagged and cleared.
- [backend/api/algo/derivatives.py:480,505 and backend/api/routes/options.py:868,980] Four TODO comments say "thread MCX close_time=(23,30) when sim driver exposes segment context." The sim driver stores `exchange` on every position row (`driver.py:1168`), so the blocker no longer applies. These TODOs are stale; the fix is a one-liner reading `row.get("exchange","").upper()` and branching on `"MCX"`.

### Stale TODOs (work already done)

- [frontend/src/lib/data/instruments.js:111] `TODO(FIX-25): migrate to api.js _get once a fetchInstruments helper is added` — a `fetchInstruments` helper could be added to api.js now; the dynamic-import workaround was needed to avoid SSR issues but the pattern is mature. The TODO's blocker has been gone for many sprints.
- [frontend/src/lib/command/grammars/orders.js:78] `TODO(FIX-26): migrate to fetchQuote from api.js` — `fetchQuote` already exists in api.js (line 497). The TODO's stated prerequisite ("once it can be imported without SSR issues") is the same as FIX-25; same resolution path.
- [backend/api/algo/derivatives.py:480,505] (see Stale references above) — the "when the sim driver exposes the segment" condition is already met since `row["exchange"]` is populated in `driver.py:1168`.

### Notes

- **Groww + Dhan brokers** (`shared/brokers/groww.py`, `shared/brokers/dhan.py`) are large files (~540 and ~400 LOC respectively) registered in `registry.py` but not instantiated by any current `broker_id` in the live deployment (both accounts in prod are Zerodha Kite). They are clearly intentional future-broker stubs, not dead code — but they pull `growwapi` SDK at import time inside try/except, which is safe. Not flagged as dead.
- **`logging.*` settings** are handled by `_apply_log_level` in settings.py correctly, but aren't in SEEDS. This means they won't appear in the `/admin/settings` UI and can't be tuned without a raw DB INSERT. Given `backend_config.yaml` documents them as live-tunable, the three keys (`logging.file_log_level`, `logging.console_log_level`, `logging.error_log_level`) should be added to SEEDS.
- Could not verify whether `/api/algo/start`, `/api/algo/stop`, `/api/algo/chase` are called from the admin console's command interpreter (`backend/api/routes/algo.py` `interpret` path) — that path wasn't traced fully. Treat as "likely dead" rather than "confirmed dead."
