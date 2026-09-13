# Plan: Legs Day P&L + Flash LTP-only + Loss/ROC Alert routing

---

## Issue 1 — Legs Day P&L zero

Same MCX stale-ticker root cause as snapshot: `baseDayPnlForPosition(c)` returns 0 when
`day_change_val = 0` (Kite stale poll after settlement reset). Candidate objects for real
positions spread all broker fields (`...p` in `pageLoad.js:318`) so `pnl`, `overnight_quantity`,
`day_change_val` ARE present — but `pnl ≈ 0` post-settlement AND `dcv = 0` → formula gives 0.

**Fix**: replace `baseDayPnlForPosition(c)` with `positionsDayPnlStore.byKey[sym] ?? baseDayPnlForPosition(c)` at all 4 per-leg Day P&L call sites. `byKey` already uses `livePositionDayPnl` rescue path.

**File**: `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` only.

Add inline helper (not a new function — use a `const` inside `<script>`):
```js
// after positionsDayPnlStore is used for _fnoDayPnlByRoot
const _candDayPnl = (c) => {
  const sym = String(c?.tradingsymbol || c?.symbol || '').toUpperCase();
  return positionsDayPnlStore.byKey[sym] ?? baseDayPnlForPosition(c);
};
```

Replace `baseDayPnlForPosition(c)` with `_candDayPnl(c)` at:
1. `flash.update(\`leg:${k}:day\`, baseDayPnlForPosition(c))` (~line 1063)
2. `candidatesDayPnl` accumulator (~line 1944): `s += baseDayPnlForPosition(c)`
3. Per-leg prop (~line 4630): `dayPnl={baseDayPnlForPosition(c)}`
4. `_totalDcv` template const (~line 4691): `.reduce((s,c) => s + baseDayPnlForPosition(c), 0)`

---

## Issue 2 — Flash: scope to LTP only

**Current**: Day P&L, P&L, Exp P&L, Greeks, EV, KV, and TOTAL cells all flash with background
color on every tick. **User wants**: ONLY LTP cells flash with background color.

**Approach**: Remove `{flash.classOf(...)}` from all non-LTP template cells. The `flash.update()`
calls can stay (state maintained for potential future use — no visual effect without classOf).
LTP flash on `leg-ltp` class and snapshot LTP column stays unchanged.

**In `+page.svelte` — remove `flash.classOf()` from**:
- Snapshot per-row: `day_w` and `pnl_w` spans (~lines 4864–4865)
- Snapshot TOTAL row: day, pnl, exp cells (~lines 4884–4886)
- Legs TOTAL row: day, pnl, exp cells (~lines 4704–4716)
- Payoff Greeks chips: delta, gamma, theta, vega, rho (~lines 4399–4415, 4913–4929)
- KV card: pop, ev, ev_pct, max_profit, max_loss (~lines 4967–4976, 1051–1052)
- Payoff EV chip (~line 4378)

**In `CandidateLegRow.svelte` — remove `flash.classOf()` from**:
- Day P&L cell (line 353), P&L cell (line 357), Exp P&L cell (line 361)
- **Keep** `flash.classOf()` on the `leg-ltp` span (line 314) — LTP stays

**Keep `flash.classOf()` on**: `${g.underlying}:ltp` span in snapshot grid only.

---

## Issue 3 — Loss + ROC alerts not reaching user

**Root cause** (confirmed): On prod (main branch), `is_engine_idle()` always returns `False`
and `is_prod_branch()` is `True` — the agent engine DOES run during market hours. But
`alert_routing.agent_alert.ntfy: false` means loss/ROC alerts route ONLY via Telegram + email,
bypassing ntfy entirely. All other critical alerts (order_failure, ticker_degraded, gtt_asymmetric,
etc.) use ntfy. If the user monitors ntfy for alerts, loss alerts are invisible.

**Secondary cause**: sim_active check in `_perf_run_agent_engine()` — if the backend simulation
engine is running, real agent evaluation is skipped. Not a code bug — expected behavior.

**Fix**: `backend/config/backend_config.yaml` — one line change:

```yaml
# Before:
agent_alert: { telegram: ops, ntfy: false,  email: true }

# After:
agent_alert: { telegram: ops, ntfy: urgent, email: true }
```

`urgent` priority matches `order_failure` routing — appropriate for loss events.

**Additional context for operator** (include in foreground output after impl):
- `loss-rate-acct` ROC alert is blocked for first **10 min** after market open (baseline window — by design, not a bug)
- `loss-rate-acct` has **10-min cooldown** after each fire
- `loss-positions-acct` has **30-min cooldown** after each fire
- `loss-positions-total` (critical tier) suppresses `loss-positions-acct` (high tier) on same topic fire
- If simulator engine is running: real loss alerts are suppressed (disable sim before expecting live alerts)

---

## Agents
- frontend: Issues 1 + 2 (legs Day P&L + flash LTP scoping in +page.svelte + CandidateLegRow.svelte)
- backend: Issue 3 — change `backend/config/backend_config.yaml` line 206: `agent_alert.ntfy: false → urgent`

## Tests
- svelte-check: yes
- vitest: yes
- pytest: no (config-only backend change, no logic change)

## Commit message
fix(derivatives): legs Day P&L via positionsDayPnlStore; LTP-only flash; route loss alerts via ntfy

## Done when
- Legs per-leg + TOTAL Day P&L show correct non-zero values matching NavStrip F&O portion
- Only LTP cells animate on tick; Day P&L, Greeks, EV, KV cells are static
- `agent_alert.ntfy = urgent` in backend_config.yaml committed
- svelte-check 0 errors, vitest 971 passed
