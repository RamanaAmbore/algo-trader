# Plan: Order guards + ntfy audit + agent integration + provisional position (consolidated)

## Context

Log scan: 36 order/template rejections across 6 guard types. Full ntfy dispatch-chain audit
reveals primary reason ntfy alerts don't arrive. Broker fill events exist but positions don't
update until broker propagation (up to 5 min). AlgoTimestamp+fire_at_time fixes shipped (bb0cc925).

---

## Part A — Order Guard Fixes

### Bug A1 — MCX G1 LOT_MULTIPLE misfires on close/chase
**File:** `backend/api/algo/actions_preflight.py` line 93
**Cause:** MCX broker returns position qty in LOTS (e.g. 50). G1 check: `50 % 100 ≠ 0` → blocked.
FAT_FINGER_5_LOT_CAP already skips MCX/NCO at line 111. LOT_MULTIPLE must do the same.
**Fix:** Wrap the `qty % _pf_lot != 0` block with `if not _is_mcx_exch`:
```python
_is_mcx_exch = exchange in ("MCX", "NCO")
if _pf_lot > 1 and not _is_mcx_exch:
    if qty % _pf_lot != 0:
        blockers.append({"code": "LOT_MULTIPLE", ...})
```
Equity (lot_size=1 → early return). NFO/BFO/CDS (qty in contracts). MCX/NCO: skipped.

### Bug A2 — GTT-QTY-GUARD on cold instruments cache (12 occurrences)
**File:** `backend/api/algo/template_attach.py` — `_apply_plan_live_gate_qty` + `_translate_gtt_orders`
**Cause:** Template attach fires on postback immediately after fill. If instruments cache
hasn't warmed, `get_lot_size()` returns 0 → translate_qty raises → exits NOT attached.
**Fix:** When lot_size ≤ 1 for MCX/NCO, retry once after 3 s, then use `plan.lot_size_hint`
as fallback if still 0:
```python
if resolved_lot_size <= 1 and plan.parent_exchange in ("MCX", "NCO"):
    await asyncio.sleep(3)
    resolved_lot_size = await get_lot_size(plan.parent_exchange, plan.parent_symbol)
if resolved_lot_size <= 1 and getattr(plan, "lot_size_hint", 0) > 1:
    resolved_lot_size = plan.lot_size_hint
if resolved_lot_size <= 1:
    raise ValueError("[GTT-QTY-GUARD] lot_size still 0 after retry")
```

### Bug A3 — ADAPTER-GTT-QTY-CEILING too low for large MCX positions (9 occurrences)
**Files:** `backend/brokers/adapters/kite.py` lines 285-305, `backend/config/backend_config.yaml`
**Cause:** MCX GTT ceiling hard-coded at 50 lots. Positions >50 lots can never get GTT exits.
**Fix:** Read ceiling from config (default 200 lots):
```python
_mcx_gtt_ceiling = get_int("orders.mcx_gtt_lot_ceiling", 200)
```
Add to `backend/config/backend_config.yaml`:
```yaml
orders:
  mcx_gtt_lot_ceiling: 200
```

### Bug A4 — Side mismatch alert not actionable
**File:** `backend/api/algo/template_attach.py` — `_fire_guard_alert`
**Fix:** Append to alert body:
`"Fix: at /admin/templates, change this template's 'applies_to' to 'both' or 'buy_option'."`

---

## Part B — ntfy Audit Findings + Fixes

### Root cause 1 (PRIMARY) — ntfy_topic not set in secrets.yaml
**File:** `backend/shared/helpers/alert_utils.py` line 905: `topic = secrets.get("ntfy_topic")`
If `ntfy_topic` is missing or empty, function returns immediately — **no alert sent, no log**.
This is the most likely reason no ntfy alerts arrive at all.
**Fix 1 (code):** On startup, warn loudly if ntfy is enabled but `ntfy_topic` is not configured:
```python
# In app startup / seed_agents():
if is_enabled("ntfy") and not secrets.get("ntfy_topic"):
    logger.warning("[NTFY] is_enabled=True but ntfy_topic not set in secrets — all ntfy alerts suppressed")
```
**Fix 2 (config — server only):** Operator must add to `/opt/ramboq/secrets.yaml`:
```yaml
ntfy_topic: <your-topic-name>
ntfy_url: https://ntfy.sh          # or self-hosted URL
```
Plan will document this as a configuration action in the commit message.

### Root cause 2 — No auth token support
**File:** `backend/shared/helpers/alert_utils.py` send_ntfy_alert headers
Self-hosted ntfy instances require `Authorization: Bearer <token>`. Currently no header sent.
Fails silently (HTTP 401 caught by `except Exception`, logged as WARNING).
**Fix:** Support optional `ntfy_token` in secrets:
```python
ntfy_token = secrets.get("ntfy_token")
if ntfy_token:
    req.add_header("Authorization", f"Bearer {ntfy_token}")
```

### Root cause 3 — market-open-nse stuck in 22h cooldown after fire_at_time fix
**Cause:** Before bb0cc925 fix, NULL fire_at_time meant market-open-nse evaluated every cycle.
It fired at random time → entered 22h cooldown → `last_triggered_at` set → cooldown persists
across restarts (not reset by `seed_agents()`). Next fire is 22h after the wrong-time fire.
**Fix:** In `_ae_sync_existing_builtin`, when fire_at_time changes for an agent that is in
cooldown status, reset the cooldown so the corrected schedule takes effect immediately:
```python
if existing.fire_at_time != new_fire_at_time:
    existing.fire_at_time = new_fire_at_time
    # Reset cooldown so the corrected window takes effect on next cycle
    if existing.status == "cooldown":
        existing.status = "active"
        existing.last_triggered_at = None
```

### Root cause 4 — cap_in_dev.ntfy=False silences all dev ntfy alerts
**File:** `backend/config/backend_config.yaml` line 35
Dev environment has `ntfy: False`. Expected (opt-in). No code change needed.
Operator can override via DB: `notifications.ntfy_enabled=1` in dev settings table.
Document in commit message.

### Root cause 5 — Missing startup validation for ntfy config
**Fix:** Add a `_validate_ntfy_config()` call in `seed_agents()` or app startup:
- Warn if `ntfy_topic` empty and ntfy is enabled in prod
- Warn if `ntfy_url` is default (ntfy.sh) but `ntfy_token` is set (token only needed for self-hosted)
- Log success path: "ntfy configured: topic=<topic>, url=<url>"

### Priority=None in events.py — NOT A BUG
`send_ntfy_alert` already handles `priority=None` via clock-based fallback
(urgent 22:00–07:00 ET, high 07:00–22:00 ET). Loss/expiry agents without priority set will
use this automatic clock-based prioritization, which is correct behaviour.

---

## Part C — Provisional Position After Fill (Feature)

**Context:** Backend already emits `position_filled` WS event on every confirmed fill
(orders.py lines 577-586) with `{event, account, exchange, tradingsymbol, qty, fill_price, ts}`.
Also emits `positions_refreshed` (after polling broker up to 5×). But if broker propagation
takes >5 s, the frontend shows stale positions until the next 5-min poll.

**Implementation:** Frontend creates a synthetic provisional row on `position_filled`,
merges into the positions grid with a visual indicator, drops on `positions_refreshed`.

### `frontend/src/lib/data/provisionalPositions.svelte.js` (new file)
```js
export let provisionalPositions = $state(new Map());

export function applyFill({ account, exchange, tradingsymbol, qty, fill_price }) {
  const key = `${exchange}:${tradingsymbol}:${account}`;
  provisionalPositions.set(key, {
    account, exchange, tradingsymbol,
    quantity: qty,
    average_price: fill_price,
    last_price: fill_price,
    pnl: 0, day_change_val: 0,
    mode: 'live',
    _provisional: true,
  });
}

export function clearFill({ exchange, tradingsymbol, account }) {
  provisionalPositions.delete(`${exchange}:${tradingsymbol}:${account}`);
}
```

### `frontend/src/routes/(algo)/+layout.svelte`
In existing `position_filled` handler → also call `applyFill(msg)`.
In existing `positions_refreshed` handler → also call `clearFill(msg)`.
Auto-clear after 60 s (safety fallback if `positions_refreshed` never arrives):
```js
setTimeout(() => clearFill(msg), 60_000);
```

### `frontend/src/lib/PositionStrip.svelte`
Merge provisional rows into the display list before rendering:
```js
const _rows = $derived.by(() => {
  const real = positionsStore.value ?? [];
  const realKeys = new Set(real.map(r => `${r.exchange}:${r.tradingsymbol}:${r.account}`));
  const prov = [...provisionalPositions.values()]
    .filter(p => !realKeys.has(`${p.exchange}:${p.tradingsymbol}:${p.account}`));
  return [...real, ...prov];
});
```
Mark provisional rows visually (opacity 0.6 + `~` prefix on tradingsymbol, or a CSS class).
Payoff chart reads a pre-computed API curve — not affected; updates naturally on `positions_refreshed`.

---

## Files to change

| File | Change |
|---|---|
| `backend/api/algo/actions_preflight.py` | A1: skip LOT_MULTIPLE for MCX/NCO |
| `backend/api/algo/template_attach.py` | A2: retry on lot_size=0; A4: alert fix hint |
| `backend/brokers/adapters/kite.py` | A3: configurable MCX GTT ceiling |
| `backend/config/backend_config.yaml` | A3: `orders.mcx_gtt_lot_ceiling: 200` |
| `backend/shared/helpers/alert_utils.py` | B2: ntfy_token auth header; B5: startup validation |
| `backend/api/algo/agent_engine.py` | B3: reset cooldown when fire_at_time changes; B5: startup ntfy check |
| `frontend/src/lib/data/provisionalPositions.svelte.js` | C: new file |
| `frontend/src/routes/(algo)/+layout.svelte` | C: wire fill_event ↔ provisional store |
| `frontend/src/lib/PositionStrip.svelte` | C: merge provisional rows into display |

---

## Agents
- backend: Part A (actions_preflight, template_attach, kite adapter, backend_config) + Part B (alert_utils, agent_engine)
- frontend: Part C (provisionalPositions store, +layout.svelte, PositionStrip)
- backend-test: pytest cases below
- playwright: Playwright specs below

---

## Test Cases

### Backend (pytest)

**A1 — MCX G1 skip:**
- `test_mcx_close_passes_g1`: MCX exchange, qty=50, lot_size=100 → no LOT_MULTIPLE blocker
- `test_nfo_g1_still_fires`: NFO exchange, qty=25, lot_size=50 → LOT_MULTIPLE blocked (existing behaviour preserved)
- `test_equity_g1_noop`: exchange=NSE, lot_size=1, qty=100 → empty blockers

**A2 — GTT cache-miss retry:**
- `test_gtt_attach_retries_on_zero_lot_size`: mock `get_lot_size` to return 0 on first call, 100 on second → attach succeeds after retry
- `test_gtt_attach_fails_after_retry_still_zero`: mock returns 0 on both calls → raises GTT-QTY-GUARD error
- `test_gtt_uses_lot_size_hint_fallback`: mock returns 0, plan has lot_size_hint=100 → attach uses hint

**A3 — Configurable GTT ceiling:**
- `test_mcx_gtt_ceiling_default_200`: default config, qty=150 lots → allowed
- `test_mcx_gtt_ceiling_config_override`: set `orders.mcx_gtt_lot_ceiling=50` via config mock, qty=100 → refused
- `test_nfo_gtt_ceiling_unchanged`: NFO qty=40000 → allowed; qty=60000 → refused

**A4 — Side mismatch alert text:**
- `test_mismatch_alert_contains_fix_hint`: `_fire_guard_alert` with applies_to mismatch → body contains "Fix:"

**B2 — ntfy auth token:**
- `test_ntfy_sends_auth_header_when_token_set`: mock secrets with ntfy_token → request has Authorization header
- `test_ntfy_omits_auth_header_when_no_token`: no ntfy_token in secrets → no Authorization header

**B3 — Cooldown reset on fire_at_time change:**
- `test_cooldown_reset_when_fire_at_time_changes`: agent in "cooldown" status, fire_at_time changes from None to "09:15" → status reset to "active", last_triggered_at=None
- `test_cooldown_preserved_when_fire_at_time_unchanged`: agent in cooldown, fire_at_time already "09:15" → no change to status

**B5 — Startup ntfy validation:**
- `test_startup_warns_if_ntfy_enabled_but_no_topic`: prod branch, ntfy enabled, no ntfy_topic → WARNING log emitted
- `test_startup_logs_ntfy_config_when_valid`: topic set → INFO log with topic name

**Existing preflight regression:**
- `test_fat_finger_still_fires_for_nfo`: NFO, qty=10 lots (>5) → FAT_FINGER_5_LOT_CAP fires
- `test_fat_finger_skipped_for_mcx`: MCX, qty=100 lots (>5) → no FAT_FINGER blocker (route-level guards apply)

---

### Frontend (Playwright)

**Provisional position — `tests/provisional-position.spec.ts`:**
- Mock `position_filled` WS event → PositionStrip shows new row with `~` prefix immediately
- Mock `positions_refreshed` WS event after `position_filled` → provisional row disappears, real row stays
- Provisional row absent if a real position row already exists for same exchange+symbol+account
- Auto-clear: provisional row disappears after 60 s even without `positions_refreshed`
- Multiple fills for different symbols → each creates its own provisional row

**PositionStrip existing behaviour — `tests/position-strip.spec.ts`:**
- P&L column shows `—` when no positions (not 0 or blank)
- MCX positions show lot qty (not contract qty) in quantity column
- Equity positions show share qty
- Row flash animation triggers on price change (`.is-animating` class applied)
- Stale account rows show stale badge when `account_stale=true`

**AlgoTimestamp existing behaviour — `tests/algo-timestamp.spec.ts`:**
- Desktop: timestamp visible, no tap interaction (pointer-events: none)
- Mobile viewport (`(hover: none) and (pointer: coarse)`): tap toggles between current time and refresh time
- After refresh, refresh timestamp ≤ current timestamp (never shows future time)
- On page nav (SPA route change), refresh timestamp visible immediately (no blink-in)

**NavBreakdown P-slot — `tests/nav-breakdown.spec.ts`:**
- With derivative positions and expiryByAcct prop populated: P slot shows expiry P&L per account
- With expiryByAcct empty but derivative positions present: fallback shows sum of position pnl
- Equity-only portfolio: P slot shows `—`

**Side mismatch alert text — `tests/template-guard.spec.ts`:**
- Mock template attach guard failure → verify alert notification body contains "Fix: at /admin/templates"

**ntfy startup warning — backend log assertion (pytest, not Playwright)**

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
fix(orders,agents,ui): MCX G1 skip, GTT cache retry, ceiling cfg, ntfy topic check + auth token, cooldown reset on fire_at_time fix, provisional positions on fill

## Done when
1. MCX close_position with 50 lots passes G1 (no LOT_MULTIPLE block)
2. GTT template attach succeeds after 3 s retry when instruments cache is cold
3. MCX positions >50 lots can place GTT exits (ceiling 200)
4. Side mismatch alert includes fix hint
5. Startup logs "[NTFY] ntfy configured: topic=..." or warning if topic missing
6. ntfy_token from secrets.yaml sent as Authorization: Bearer header
7. market-open-nse cooldown reset on startup (fires at 09:15 today, not stuck in 22h lock)
8. Order fill → provisional position row appears in PositionStrip immediately, drops on positions_refreshed
9. pytest green, svelte-check 0 errors
