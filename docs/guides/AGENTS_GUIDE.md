# Agents — Operator Test Guide

This file is a pointer stub. The DSL syntax reference is no longer maintained here.

## Where the agent-DSL syntax lives now

The authoritative, up-to-date reference for agent statements, conditions, operators,
`log(...)` conditions, preamble and label syntax, brackets, tokens and the Quick
Reference table is the live in-app page at **`/automation/guide`**. It is the
**Trader Guide** button on `/automation` and the order-ticket CLI tab in
`SymbolPanel`. Source:
[guide page](<../../frontend/src/routes/(algo)/automation/guide/+page.svelte>).

Do not add syntax prose to this file. Update the in-app page instead.

## What stays here

The sections below are repo-engineering reference that the in-app page does not cover.

### Testing ladder

| Stage | Location | What it does | Risk |
|---|---|---|---|
| 1 — Validate | `/automation` → **Validate** | Static: tokens and shape well-formed | none |
| 2 — Dry-run | `/api/agents/<slug>/dry-run` or button | Evaluate against live data, no fire | none |
| 3 — Simulator | `/automation` → **Run in Simulator** | Synthetic ticks, `sim_mode=True` | low |
| 4 — Activate | `/automation` → Status to active | Real ticks, LIVE or paper | full |

Simulator runs bypass cooldown, baseline and schedule gates, so the agent fires on the
first tick. See [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md).

### Runtime rules that matter for real money

- **Once per activation.** A saved agent's `order(...)`, `close_position(...)`,
  `cancel_order(...)` and other non-notify preamble calls fire at most once per activation.
  To re-fire, deactivate, then activate again. Clicking Activate on a running agent does not
  count, and editing an active agent's preamble does not re-arm it.
- **Notify and log preamble calls.** `alert(...)` and non-debug `log(...)` run every cycle,
  rate-limited by the agent's own `cooldown_minutes`. `help(...)` and `debug(x)` are never
  rate-limited and never notify anyone.
- **`bracket(...)`** has no creation handler yet. It logs one warning per activation and does
  nothing.
- **Replay and backtest runs skip the preamble entirely.**
- **Explicit alerts.** A `DO`-level `alert(...)` replaces the automatic breach alert for that
  agent. The flag is stored on the row. Re-saving through the Console can keep or clear it,
  never turn it on. A seed or form-built agent, such as a `loss-*` built-in, cannot be
  converted to explicit mode by editing it in the Console.

### Operational gates — why an agent is not firing

Eight gates sit between tick and dispatch. Dry-run's `blocked_by` field names the one that
applies.

1. **schedule** — `market_hours` skips closed segments (hours from `market_segments` config)
2. **cooldown** — default 30 min; suppresses re-fires
3. **baseline** — rate metrics stay silent for the first 15 min (no history)
4. **fire_at_time** — when set, fires only in a ±15 min window around HH:MM IST
5. **blackout** — `[{start, end}]` windows block firing (midnight-crossing allowed)
6. **debounce** — the condition must hold for N minutes continuously
7. **suppression** — re-fire needs cooldown plus a `|ΔP&L|` threshold
8. **exchange-open** — actions blocked while the target exchange is closed (sim and replay exempt)

### Lifespan

| `lifespan_type` | Behaviour |
|---|---|
| `perpetual` (default) | Runs until deactivated |
| `one_shot` | Auto-deactivates after first fire |
| `n_fires` | Auto-deactivates on the `lifespan_max_fires`-th fire |
| `until_date` | Auto-deactivates when IST passes `lifespan_expires_at` |

Auto-deactivation is final. Set `status` back to `active` to re-enable.

### Action handlers — real vs sim

| Token | Real | Sim |
|---|---|---|
| `order` | Broker call | `AlgoOrder(mode='sim')` at sim LTP |
| `chase_close_positions` | Adaptive chase | SimDriver chase queue |
| `close_position` | LIMIT at LTP | Paper row at sim LTP |
| `cancel_order` / `modify_order` | Real broker | Sim order book |
| `monitor_order` | Poll broker | Poll sim driver |
| `deactivate_agent` | DB flip to inactive | Same (shared state) |
| `set_flag` / `log` | Write state | Same (`sim_mode` tag flows) |
| `help` | Read-only catalog lookup, DEBUG log | Same |

The Order log Mode pill (SIM / PAPER / LIVE / SHADOW) shows which path ran.

### Scoping the fan-out actions — `cancel_all_orders` and `chase_close_positions`

Both actions take the same scope params. Omitting `scope` (or `scope: total`) means every account,
the existing behaviour for every saved agent.

| Param | Values | Required when | Meaning |
|---|---|---|---|
| `scope` | `total` (default), `account`, `role`, `user` | — | Which accounts the action may touch |
| `account` | account code | `scope: account` | One account |
| `role` | `trader` | `scope: role` | Union of every trader-role user's assigned accounts |
| `user` | trader-role username | `scope: user` | One trader's own assigned accounts |
| `side` | `BUY` / `SELL` | — (`cancel_all_orders` only) | Cancel only orders on this side |
| `symbol` | tradingsymbol | — (`cancel_all_orders` only) | Cancel only orders on this symbol |

- **Only `trader` is a valid `role`.** Every other role (`admin`, `risk`, `designated`, `partner`) is
  firm-wide and has no per-user account list, so `scope: role` with any other role is refused.
- **`scope: user` must name a trader.** Naming any other user is refused, for the same reason.
- A refused scope raises `ValueError` before any broker call, so no order is touched. An unknown
  username fails the same way.
- Enforced in [actions_live.py](../../backend/api/algo/actions_live.py). `cancel_all_orders_scoped()`
  runs its own role/user checks, and `chase_close_positions` goes through
  `_al_resolve_scope_accounts()`. Both use `_al_accounts_for_trader_role()` and
  `_al_accounts_for_trader_user()` for the account lists. The schema lives in
  [agent_grammar.yaml](../../backend/config/grammars/agent_grammar.yaml) under each token's
  `params_schema`.

Example — cancel every open order belonging to one trader's accounts:

```
cancel_all_orders(scope="user", user="asha")
```

### Built-in agents you can study

These slugs are seeded from code on boot. Read the live rows at `/automation` for current
status, thresholds and channels. They change more often than this file does.

| Slug | Topic |
|---|---|
| `loss-positions-acct` | Per-account loss guardrail; `any:` block over two thresholds |
| `loss-rate-acct` | Per-account burn rate; fires only when absolute and percentage rates both breach |
| `loss-positions-underlying-divergence` | Underlying-level divergence guardrail |
| `loss-positions-total` | Book-wide loss guardrail (critical tier) |
| `loss-margin-low` | Margin warning (ships inactive, cross-account false positives on Dhan/Groww) |
| `loss-funds-negative` | Negative cash or margin |
| `loss-pos-total-auto-close` | Auto-close on loss; destructive, ships inactive |
| `expiry-day-positions-alert` | Expiry review notification |
| `expiry-nfo-risk-alert` | Expiry-day risk notice for NFO |
| `expiry-mcx-risk-alert` | Expiry-day risk notice for MCX |
| `market-open-nse` | NSE open notice (fires at 09:15 IST) |
| `market-preclose-mcx-v2` | MCX pre-close notice (fires 30 min before MCX closes today) |
| `market-preclose-nonmcx` | NSE pre-close notice (fires 30 min before NSE closes today) |
| `expiry-day-mcx-preclose-alert` / `expiry-day-nse-preclose-alert` | One per exchange: a held position of that exchange expires today and its close is 65 min away. Lists the positions to close. Active. |
| `expiry-day-mcx-itm-auto-close` / `expiry-day-nse-itm-auto-close` | One per exchange: 60 min before that exchange's close. Stages HELD closing orders for that exchange only. Active. |
| `expiry-day-mcx-morning-preview` / `expiry-day-nse-morning-preview` | Once each morning on an expiry day, after that exchange has been open 5 min. Tentative list at opening prices. Active. |
| `market-status-report` | Market-status notice |
| `manual` | Audit trail for every manual ticket or chain order |

Pre-close reminders follow the real exchange close time, not a fixed clock time. The MCX
reminder fires 30 minutes before MCX closes that day, and the NSE reminder 30 minutes before NSE
closes. If the exchange moves a close time for a day, the reminder moves with it. Each reminder
fires at most once per day.

**Expiry pre-close alert, auto-close and morning preview (amended 2026-11-27)** — the expiry
agents are now split by exchange: four exchange-pure agents, plus two morning previews. The
earlier combined agents (`expiry-day-preclose-alert` / `expiry-day-itm-auto-close`, 2026-10-29)
are retired. They fired 30 and 25 minutes before close. The current agents fire 65 and 60
minutes before close, and each is active. A pre-close alert or auto-close fires only when a held
F&O position of that exchange expires that day AND that exchange is inside its own lead window.
The lead times are the constants `ALERT_LEAD_MINUTES = 65`
([metric_is_expiry_preclose_alert_window.py:95](../../backend/api/algo/grammar_tokens/metric_is_expiry_preclose_alert_window.py#L95))
and `CLOSE_LEAD_MINUTES = 60`
([metric_is_expiry_preclose_close_window.py:25](../../backend/api/algo/grammar_tokens/metric_is_expiry_preclose_close_window.py#L25)).
Each agent has a 30-minute cooldown, so a leg that turns unnetted late in the window is re-scanned.

- The alert lists only the positions that need closing, by account and symbol. The per-root
  "netted" and "no action (OTM)" lines were removed on 2026-11-26. Positions that need no action
  are not listed at all. The alert uses the same identification as the auto-close agent
  (`ExpiryEngine.scan_positions()`, the backend mirror of the Derivatives Exp-close tab), so the
  two can never disagree. The alert never says "safe" for MCX. A long-CE plus long-PE pair can
  still devolve into a naked future if spot settles between strikes.
- The morning preview is the same identification, priced at the opening LTP. It is labelled
  tentative. The final decision happens at the 60-minute auto-close window.
- The auto-close agent stages the closing orders as **HELD** by default — it identifies and
  records what needs closing, but does not send anything to the broker unless you explicitly turn
  on `expiry_close_hold_enabled=false` on `/admin/settings` (inverted naming: this setting being
  *true*, the default, means closes ARE held). Once released, a close goes through the same chase
  engine (cancel-and-replace at improving prices) every other close-position action uses — nothing
  special about this path once it leaves the hold.
- The alert title is prefixed "Expiry pre-close", so an expiry reminder is never confused with the
  plain, non-expiry pre-close reminder above it
  ([agent_engine.py:1409](../../backend/api/algo/agent_engine.py#L1409)).
- They check your own positions, not the whole exchange, so a day with no expiring position in
  your account stays silent. Channels are resolved by the alert-routing tree, not listed per
  agent, so check `alert_routing.py` for the current set.

Alert channel routing for event agents is not defined per agent. It is resolved by
`alert_routing.py`. See [CLAUDE.md](../../CLAUDE.md) (Alert channels resolved by a routing tree).

### Where everything lives

| Surface | URL | Purpose |
|---|---|---|
| Agents | `/automation?tab=agents` | Rule editor: create, edit, activate, dry-run |
| Brackets | `/automation?tab=brackets` | Per-position exit rules attached at fill |
| Activity | `/activity?tab=agent` | Recent real agent fires |
| Grammar Catalog | `/admin/tokens` | Live metrics, scopes, operators, channels, actions (read-only) |
| CLI | `/automation?tab=console` | Plain-text authoring; syntax in the Trader Guide |
| MCP | `/admin/mcp` | LLM-driven research and draft agents |
| Simulator | `/admin/execution?mode=sim` | Synthetic price-move workspace |

### Troubleshooting (agent-specific)

For general ops troubleshooting see [ADMIN_GUIDE.md](ADMIN_GUIDE.md).

| Symptom | Likely cause | Where to look |
|---|---|---|
| `Validate` rejects with unknown metric token | Typo, or token deactivated | `/admin/tokens` |
| Dry-run `would_fire: false` but you expect true | Threshold vs current state | `matches` array in the dry-run response |
| Dry-run `blocked_by: "schedule"` | Segment closed | Wait for session, or use `schedule: always` for diagnostics |
| Agent never fires on real ticks | Cooldown, suppression, or a rate metric still in baseline | `/automation/<slug>` Events tab |
| Real loss alerts silent while sim fires | Simulator running suppresses real loss alerts | Stop the sim or wait for auto-stop |
| `AlgoOrder` row exists, broker did not see it | `execution.paper_trading_mode: true` | Switch mode to LIVE in the navbar |
| `409 Exchange closed` on prod | Exchange-open gate | Wait for session; sim bypasses |

## See also

- [USER_GUIDE.md](USER_GUIDE.md) — concepts in plain English
- [ADMIN_GUIDE.md](ADMIN_GUIDE.md) — day-to-day ops, anatomy of an agent, tokens, troubleshooting
- [SIMULATOR_GUIDE.md](SIMULATOR_GUIDE.md) — simulator testing workflow
- [LAB_MCP_GUIDE.md](LAB_MCP_GUIDE.md) — LLM-driven agent drafting via Claude Code
- In-app: `/automation/guide` (Trader Guide) — DSL syntax reference
