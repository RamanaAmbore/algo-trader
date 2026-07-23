# Plan: Nav z-index + NavBreakdown + ntfy agent integration + margin shortfall alert

## Context
Four issues:
1. **Hamburger drawer + nav menus behind modals** — drawer at z-index 200, nav panels at 49; full-screen modals at 9998–10600. Nav elements must appear above any modal on mobile and desktop.
2. **NavBreakdown popup** — clicking any sub-value of a NavStrip pill opens a popup with Account + one column per sub-value for THAT pill, TOTAL row matching pill values:
   - P: Day P&L | Lifetime P&L | Expiry P&L (3 columns)
   - M: Avail Margin | Total Margin (2 columns)
   - C: Cash Avail | Total Cash (2 columns)
   - H: Today MTM | Value | Lifetime (3 columns)
3. **ntfy not integrated with agent code** — `_EXPIRY_AGENT_DEFAULTS` and `manual` agent missing ntfy channel. `_ae_sync_existing_builtin` doesn't sync events so existing DB rows were never updated. DB already patched via SQL; code needs to match + forward-sync on deploy.
4. **No margin shortfall warning** — `loss-funds-negative` only fires when margin already hits 0 (too late). Need a warning agent at avail_margin < ₹25,000.

## Already done (prod DB patched live)
- ntfy topic updated in `/opt/ramboq/backend/config/secrets.yaml`: `ramboq-loss-x72km` → `ramboq-alert-x7k2m`
- All 8 agents in DB now have `{"channel": "ntfy", "enabled": true}` in their events

## Agents
- frontend: Fix 1 (z-index) + Fix 2 (NavBreakdown).
- backend: Fix 3 (ntfy agent code) + Fix 4 (margin shortfall agent).
- broker: skip
- doc: skip
- backend-test: pytest for new margin-shortfall agent condition + ntfy channel sync.
- playwright: Update spec for z-index and NavBreakdown columns.

---

## Fix 1 · Z-index — Nav elements above all modals

Highest modal: `ChartModal` at 10600.

### 1a. `frontend/src/app.css` — raise vars

```css
/* Before: */
--z-dropdown: 60;
--z-drawer:   200;

/* After: */
--z-dropdown: 20000;
--z-drawer:   20001;
```

### 1b. `frontend/src/routes/(algo)/+layout.svelte` — switch hardcoded z-indexes

Replace hardcoded values with variables. Search for each occurrence:

| Current value | Element | Replace with |
|---|---|---|
| `z-index: 48` | `.algo-group-overlay` | `z-index: calc(var(--z-dropdown) - 1)` |
| `z-index: 49` | `.algo-group-panel` | `z-index: var(--z-dropdown)` |
| `z-index: 49` | `.algo-mobile-dropdown` | `z-index: var(--z-dropdown)` |
| `z-index: 48` | second overlay (~line 2580) | `z-index: calc(var(--z-dropdown) - 1)` |

Do NOT change `z-index: 40`, `z-index: 45`, `z-index: 61` or any other values unrelated to nav panels.

### 1c. `frontend/src/lib/PositionStrip.svelte` — raise breakdown panel

`.ps-breakdown-panel` currently `z-index: 201`.
Change to `z-index: var(--z-drawer)` (= 20001 after the fix above).

---

## Fix 2 · NavBreakdown + PositionStrip — per-slot per-account table

### What the popup shows per active slot

| Slot | Columns | TOTAL row matches |
|------|---------|-------------------|
| P | Account \| Day P&L \| Lifetime P&L \| Expiry P&L | `dispPositionsToday`, `_livePositionsPnl`, `_expiryProfit` |
| M | Account \| Avail Margin \| Total Margin | `marginAvail`, `marginTotal` |
| C | Account \| Cash Avail \| Total Cash | `liveCashTotal`, `cashTotal` |
| H | Account \| Today MTM \| Value \| Lifetime | `dispHoldingsToday`, `_liveHoldingsValue`, `_liveHoldingsTotal` |

### 2a. `frontend/src/lib/PositionStrip.svelte` — add per-account expiry breakdown

`_expiryProfit` already iterates all positions using `symbolStore` spots and the `expiryPnl()` helper. NavBreakdown cannot replicate this (no symbolStore access). Solution: compute `_expiryProfitByAcct` in PositionStrip and pass as a prop.

Add a derived that produces `Map<string, number>` (account → expiry P&L):
```js
const _expiryProfitByAcct = $derived.by(() => {
  void _throttledTick;
  void _mktTick;
  const map = new Map();
  for (const p of positions) {
    const acct = String(p?.account || '');
    if (!acct) continue;
    const exch = String(p?.exchange || '').toUpperCase();
    if (!_isDerivativeExch(exch)) continue;
    // Same per-position logic as _expiryProfit — call _expiryForPos(p)
    const v = _expiryForPosition(p);  // extract the per-position logic into a helper
    map.set(acct, (map.get(acct) ?? 0) + (v ?? 0));
  }
  return map;
});
```

Extract the existing per-position expiry logic from `_expiryProfit` into a private helper `_expiryForPosition(p)` so it can be reused. The current `_expiryProfit` becomes `$derived.by(() => { ...; return [...map.values()].reduce(... ) })` or keep it as-is and add `_expiryProfitByAcct` alongside.

Pass to NavBreakdown:
```svelte
<NavBreakdown
  activeSlot={_activeSlot}
  expiryByAcct={_expiryProfitByAcct}
/>
```

### 2b. `frontend/src/lib/NavBreakdown.svelte` — slot-specific tables (update current impl)

Current implementation (from last commit) already switches tables per slot with 2 columns for P and H. Update to the correct column counts:

**Props** — add `expiryByAcct`:
```js
let {
  accountFilter = [],
  activeSlot = 'P',
  expiryByAcct = /** @type {Map<string,number>} */ (new Map()),
} = $props();
```

**P slot derivation** — add expiry column:
```js
// _pByAcct already has dayPnl + lifetimePnl per account.
// Extend each row:
const _pRows = $derived.by(() =>
  _scopedAccounts.map(a => ({
    account: a,
    dayPnl:     _pByAcct.get(a)?.dayPnl ?? null,
    lifetimePnl: _pByAcct.get(a)?.lifetimePnl ?? null,
    expiryPnl:  expiryByAcct.get(a) ?? null,
  }))
);
const _pTotal = $derived({
  dayPnl:     _pRows.reduce((s,r) => s + (r.dayPnl ?? 0), 0),
  lifetimePnl: _pRows.reduce((s,r) => s + (r.lifetimePnl ?? 0), 0),
  expiryPnl:  _pRows.reduce((s,r) => s + (r.expiryPnl ?? 0), 0),
});
```

**P slot template** — 3 data columns:
```svelte
{#if activeSlot === 'P'}
<table class="algo-table nav-bd-table">
  <thead><tr>
    <th class="nav-bd-acct">Account</th>
    <th>Day P&L</th>
    <th>Lifetime</th>
    <th>Expiry</th>
  </tr></thead>
  <tbody>
    {#each _pRows as r (r.account)}
    <tr>
      <td class="nav-bd-acct">{r.account}</td>
      <td class="nav-num {_cls(r.dayPnl)}">{_fmt(r.dayPnl)}</td>
      <td class="nav-num {_cls(r.lifetimePnl)}">{_fmt(r.lifetimePnl)}</td>
      <td class="nav-num {_cls(r.expiryPnl)}">{_fmt(r.expiryPnl)}</td>
    </tr>
    {/each}
    <tr class="nav-bd-total">
      <td class="nav-bd-acct">TOTAL</td>
      <td class="nav-num {_cls(_pTotal.dayPnl)}">{_fmt(_pTotal.dayPnl)}</td>
      <td class="nav-num {_cls(_pTotal.lifetimePnl)}">{_fmt(_pTotal.lifetimePnl)}</td>
      <td class="nav-num {_cls(_pTotal.expiryPnl)}">{_fmt(_pTotal.expiryPnl)}</td>
    </tr>
  </tbody>
</table>
```

**M slot** — 2 columns (avail, total) — keep as current impl but verify TOTAL matches `marginAvail`/`marginTotal`.

**C slot** — 2 columns (live cash, total cash) — keep as current impl but verify TOTAL matches `liveCashTotal`/`cashTotal`.

**H slot** — 3 columns (today MTM, value, lifetime) — keep as current but update TOTAL to match `dispHoldingsToday`, `_liveHoldingsValue`, `_liveHoldingsTotal`.

**Caption per slot:**
- P: "Day P&L | Lifetime P&L (Σ pnl) | Expiry P&L (lognormal projection)"
- M: "Available = Total − used margin | Total = used + available"
- C: "Cash Avail (CA) = live deployable cash | Total = CA + long option premiums"
- H: "Today MTM | Current Value | Lifetime P&L"

---

---

## Fix 3 · ntfy agent code integration

**File:** `backend/api/algo/agent_engine.py`

### 3a. Add ntfy to `_EXPIRY_AGENT_DEFAULTS` (line ~1008)
```python
_EXPIRY_AGENT_DEFAULTS = dict(
    events=[
        {"channel": "telegram", "enabled": True},
        {"channel": "email",    "enabled": True},
        {"channel": "log",      "enabled": True},
        {"channel": "ntfy",     "enabled": True},   # ADD THIS
    ],
    ...
)
```

### 3b. Add ntfy to `manual` agent events (line ~1032)
```python
events=[
    {"channel": "telegram", "enabled": True},
    {"channel": "email",    "enabled": True},
    {"channel": "log",      "enabled": True},
    {"channel": "ntfy",     "enabled": True},   # ADD THIS
],
```

### 3c. Additive channel sync in `_ae_sync_existing_builtin` (line ~1059)
Add after the existing sync block — for system agents, merge any channel present in the code default but missing from the DB row (additive only, never removes operator-added channels):
```python
# Additive-sync events: add any default channel missing from the stored events
code_events = agent_def.get("events", [])
stored_channels = {e["channel"] for e in (existing.events or [])}
missing = [e for e in code_events if e["channel"] not in stored_channels]
if missing:
    existing.events = list(existing.events or []) + missing
```

---

## Fix 4 · Margin shortfall warning agent

**File:** `backend/api/algo/agent_engine.py`

Add a new `loss-margin-low` agent to `_LOSS_AGENTS` list (before `loss-funds-negative`):

```python
{
    "slug": "loss-margin-low",
    "name": "Margin shortfall warning",
    "long_name": "when:funds.any_acct.avail_margin<25000   alert:high/tg+email+ntfy+log   do:notify-only",
    "description": "Fires when available margin on any account drops below ₹25,000 — warning before margin goes negative.",
    "conditions": {"op": "<", "scope": "funds.any_acct", "value": 25000, "metric": "avail_margin"},
    "actions": [],
    "status": "active",
    "tier": "high",
    "topic": "funds_warning",
},
```

This inherits `_LOSS_AGENT_DEFAULTS` (telegram + email + log + ntfy, cooldown 30 min).

---

## Tests
- pytest: yes — test `loss-margin-low` condition fires at avail_margin < 25000 and is suppressed above; test `_ae_sync_existing_builtin` adds missing ntfy channel to existing agent row without removing existing channels.
- svelte-check: yes
- playwright: yes — update spec:
  - `OrderTimelineDrawer` renders with `z-index` ≥ 20000
  - `.algo-group-panel` has `z-index` ≥ 20000
  - P slot popup: 3 data columns (Day P&L, Lifetime, Expiry) + Account + TOTAL row
  - M slot popup: 2 data columns + TOTAL
  - C slot popup: 2 data columns + TOTAL
  - H slot popup: 3 data columns + TOTAL

## SSOT consistency rule
NavBreakdown TOTAL rows are computed bottom-up (Σ per-account values). If the TOTAL ≠ the corresponding NavStrip pill value, the NavStrip formula is wrong — fix the NavStrip derivation to match. The per-account breakdown is the source of truth. Use the same formula in both places:
- P Day: `baseDayPnlForPosition(p)` — same helper NavStrip uses via `livePositionDayPnl`
- P Lifetime: `Σ p.pnl` — same as `_livePositionsPnl`
- P Expiry: per-account slice of `_expiryProfit` computed in PositionStrip (passed as prop)
- M Avail: `f.avail_margin` — same as `marginAvail`
- M Total: `f.used_margin + f.avail_margin` — same as `marginTotal`
- C Live: `f.live_cash ?? f.cash` — same as `liveCashTotal`
- C Total: live cash + option premium per account — same as `cashTotal`
- H Today: `Σ h.day_change_val` — same as `dispHoldingsToday`
- H Value: `Σ h.cur_val` — same as `_liveHoldingsValue`
- H Lifetime: `Σ h.pnl` — same as `_liveHoldingsTotal`

If any total diverges on the live site, instrument both the NavStrip variable and the NavBreakdown sum with `console.log` to identify which formula differs.

---

## Fix 5 · Market-open + MCX-close informational agents + deploy alert priority

### 5a. `send_ntfy_alert` priority override (`backend/shared/helpers/alert_utils.py`)
Add optional `priority: str | None = None` parameter. When `None`, keep existing time-based auto-detection (urgent/high). When set, use that value directly:
```python
def send_ntfy_alert(title: str, message: str, priority: str | None = None) -> None:
    ...
    if priority is None:
        priority = "urgent" if is_night else "high"
    # rest unchanged
```

### 5b. Thread priority through `_dispatch_channel` (`backend/api/algo/events.py`)
Change `_dispatch_channel` to receive the full channel config dict (`ch: dict`) instead of just `channel: str`. Extract `channel = ch.get("channel", "")` inside. For ntfy, read `ntfy_priority = ch.get("priority")` and pass to `send_ntfy_alert`. Update the call site in `dispatch()` to pass `ch` (full dict).

### 5c. New informational agents (`backend/api/algo/agent_engine.py`)
Add `_INFO_AGENT_DEFAULTS` and `_INFO_AGENTS` list with two agents that fire once daily via `fire_at_time`, cooldown 22h, ntfy `default` priority, no email:
- `market-open-nse`: `fire_at_time: "09:15"`, `schedule: "always"`, `tier: "info"`, `topic: "market_status"`, condition `avail_margin >= -999999999` (always true when fund data exists), events `[telegram, log, ntfy(priority=default)]`
- `market-close-mcx`: `fire_at_time: "23:30"`, same defaults, same condition

### 5d. Deploy alert priority (`webhook/notify_deploy.py`)
Change `"Priority": "high"` → `"Priority": "default"` in the ntfy POST headers.

---

## Done when
1. Hamburger drawer z-index ≥ 20001; nav dropdown panels ≥ 20000 — both above ChartModal (10600)
2. NavBreakdown P popup: Account | Day P&L | Lifetime | Expiry — 3 per-account rows + TOTAL matching NavStrip P values
3. NavBreakdown M popup: Account | Avail | Total — TOTAL matches marginAvail/marginTotal
4. NavBreakdown C popup: Account | Cash Avail | Total Cash — TOTAL matches liveCashTotal/cashTotal
5. NavBreakdown H popup: Account | Today MTM | Value | Lifetime — TOTAL matches NavStrip H values
6. If any TOTAL ≠ NavStrip: NavStrip formula corrected to use the same per-position/per-fund calculation
7. `_EXPIRY_AGENT_DEFAULTS` and `manual` events include ntfy in code
8. `_ae_sync_existing_builtin` additively syncs missing channels into existing system agent rows on deploy
9. `loss-margin-low` agent active on prod — fires ntfy + telegram when avail_margin < ₹25,000 on any account
10. `market-open-nse` fires ntfy(default) at 09:15 IST; `market-close-mcx` fires ntfy(default) at 23:30 IST
11. Deploy ntfy alert uses `Priority: default`
12. `svelte-check` clean, pytest green, Playwright green
