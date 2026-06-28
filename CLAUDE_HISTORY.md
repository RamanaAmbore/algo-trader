# RamboQuant — Historical Notes

Archival content extracted from CLAUDE.md. Load this file only when investigating
"why a thing was done a particular way" or tracing a past refactor. Not autoloaded.

---

## Index

- Performance tuning (multi-wave)
- Data-layer hardening
- Refactoring notes

---

## Performance tuning (multi-wave)

Multi-wave optimization series across backend + frontend:

- **Memoized time formatters** — `formatDualTz` + `clientTimestamp` cache per-minute-key. 2000 calls/3s → 5-10 per page load.
- **Store write guards** — `executionMode` ignores writes when value unchanged (no chain re-fires on tick burst).
- **Viewport-paused polling** — `ChartWorkspace`, `OptionChainTab`, LogPanel lazy-timers via `visibleInterval` (pause on `document.hidden`). 50-70% background reduction.
- **Lazy log tabs** — System + Sim Ticks don't fire until tab activated.
- **OrderTicket/OrderDepth lifecycle** — `suspended` / `paused` props pause preflight + quote poll.
- **WebSocket debounce** — `loadOrders()` 250ms debounce on postback burst.
- **`mask_account(s: str) -> str`** — pure regex (was pandas allocation per call). 16 callsites replaced.
- **Parallel fetches** — preflight 4 awaits → `asyncio.gather`. 300ms (was 800-1200ms). `ensure_all_bootstrapped` set-diff (was N sequential queries). `compute_slice_history` O(D × E) → O(D + E) with running-pointer cumsum. `list_funds` filtered + MIN(date) probe via `asyncio.gather`.
- **Indexes** — `algo_orders (basket_tag, account, symbol)`, `agent_events (sim_mode, agent_id, timestamp DESC)`, `hedge_proxies/investor_events` optimized.

**Result**: `/admin/execution` pages ~20-40 req/min (was 100+).

---

## Data-layer hardening

Comprehensive cascade + index audit (Jun 2026):

**Cascades fixed**:
- `agent_events.agent_id` → `ON DELETE CASCADE` (was NO ACTION, blocked deletes)
- `algo_orders.agent_id` → `ON DELETE SET NULL` (agent deletes were blocked)
- `monthly_statements.user_id` → `CASCADE` (regenerable)
- `investor_events.user_id` → `RESTRICT` (operator must redeem first)
- `investor_tokens.user_id` → `CASCADE` (ephemeral)
- `auth_tokens.user_id` → `CASCADE` (verify/reset)

**FK indexes added** — `algo_events.algo_order_id`, `sim_iterations.parent_run_id` (self-ref).

**Indexes added** — `(kind, date)` on `daily_book`, `(account)` on `algo_orders`, `(token)` on `grammar_tokens`, etc.

**Predicate fixes** — `uq_watchlist_global_pinned` was `((1)) WHERE is_global` (allowed only 1 row total) → `(name) WHERE is_global` (multiple named globals OK).

**Defaults added** — `AlgoOrder.engine` / `.exchange` `server_default=` (raw INSERTs don't NULL them).

---

## Refactoring notes

**Slice X — audit cycle summary** (Jun 2026): Multi-wave audit cycles closed
defects in postback scaffolding (Dhan/Groww), role vocabulary migration (operator
terms), data-layer cascades, perf optimizations (parallel preflight, set-diff
bootstrap, cumsum history), palette consolidation (4 waves), UX consistency
(ConfirmModal on PWA, tab-strip deduplication, auth-check pattern cleanup).

**Key patterns**: `$effect`-gated auth checks on all admin pages. Parallel
`asyncio.gather` for any multi-step broker operations. Module-level caching
(dicts with identity flip on refresh). Canonical components (AlgoTabs, Select,
ConfirmModal, CollapseButton, RefreshButton).
