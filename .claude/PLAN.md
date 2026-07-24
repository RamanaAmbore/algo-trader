# Plan: Order ticket + chase UX — header refactor, LTP display, chase status, pipeline

## Context

Three new UX improvements to the order entry and chase flow, plus the previously-approved order pipeline work:

1. **Chase → header**: Move chase toggle + aggressiveness picker to the order ticket header as the middle zone using `CardHeader.svelte`. Cleaner header with semantic left/middle/right layout.
2. **LTP in body**: In the spot where chase controls were, show the instrument's current LTP from `_lastQuote.ltp` (already populated by `OrderDepth` callback — no extra fetch needed).
3. **Chase status card**: `ChaseCard.svelte` should show time elapsed since last reset, countdown to next re-quote, the current/updated limit price, and a pulsing animation to indicate the chase loop is alive.
4. **Pipeline** (previously approved): Cancel reconciliation, shared open orders store, CandidateLegRow chips, chain +/- position.

## Agents
- frontend: all changes (single agent — all files are frontend-only)
- backend: skip
- playwright: stale-check TCs for chase header presence, LTP display, pipeline items

---

## Part 1 — OrderTicket header: chase to middle zone

**File:** `frontend/src/lib/order/OrderTicket.svelte`

Replace the custom `.ot-header` div (lines 1941-1966) with `<CardHeader>` using left/middle/right snippets:

```svelte
<CardHeader>
  {#snippet left()}
    <div class="ot-symbol">
      <span class="ot-symbol-text"><LegLabel sym={symbol} exchange={exchange || ''} /></span>
      <span class="ot-symbol-meta">
        {exchange ? exchange + ' · ' : ''}
        {kind}{_lotSize ? ' · lot ' + _lotSize : ''}
        {action !== 'open' ? ' · ' + action.toUpperCase() : ''}
      </span>
    </div>
  {/snippet}
  {#snippet middle()}
    {#if showLimit}
      <label class="ot-chase-toggle" title={...}>
        <input type="checkbox" checked={_chase} onchange={...} />
        <span class="ot-chase-label" class:on={_chase}>CHASE</span>
      </label>
      {#if _chase}
        <ChaseAggPicker value={_chaseAgg} onChange={_setChaseAgg} />
      {/if}
    {/if}
  {/snippet}
  {#snippet right()}
    <button type="button" class="ot-refresh-btn" ...>...</button>
    <button type="button" class="ot-close" ...>×</button>
  {/snippet}
</CardHeader>
```

- Import `CardHeader` from `$lib/CardHeader.svelte`
- Remove now-redundant `.ot-header`, `.ot-symbol`, `.ot-header-actions` CSS (or scope to remain for any fallback uses)
- The `CardHeader` middle zone is only rendered when `showLimit` is true (LIMIT/SL orders); for MARKET orders the middle stays empty

---

## Part 2 — LTP display in order body

**File:** `frontend/src/lib/order/OrderTicket.svelte`

Remove the chase block from the body (lines 2306-2316). In its place add an LTP row:

```svelte
<div class="ot-ltp-row">
  <span class="ot-field-label">LTP</span>
  <span class="ot-ltp-val num">{_lastQuote?.ltp != null ? fmtPrice(_lastQuote.ltp) : '—'}</span>
</div>
```

- `_lastQuote.ltp` is already live (updated by `onDepthQuote` callback from `OrderDepth`)
- `fmtPrice` — use the same price formatter already used in the ticket for bid/ask display
- Show `'—'` when no quote yet (depth not loaded or paused)
- CSS: `.ot-ltp-row` — same row layout as other field rows; `.ot-ltp-val` — right-aligned, slightly larger font to give LTP visual prominence

---

## Part 3 — Chase status card improvements

**File:** `frontend/src/lib/order/ChaseCard.svelte`

Add to each active chase entry:

**Time elapsed since last attempt:**
- `last_attempt_at` field from the event — compute `Date.now() - new Date(last_attempt_at).getTime()`
- Display as `"23s ago"` or `"1m 12s"` — update every second via a `setInterval` or reactive tick

**Countdown to next re-quote:**
- `next_attempt_at` field — `new Date(next_attempt_at).getTime() - Date.now()`
- Display as `"next in 17s"` — counts down live
- Already partially present; make it more prominent

**Current limit price:**
- `current_limit` field from event — show as `"@ ₹247.50"` alongside the attempt count chip
- Update reactively as the event stream refreshes (3s poll already in ChaseCard)

**Active animation:**
- Add a pulsing dot (CSS `@keyframes pulse` — opacity 1→0.3→1, 1.5s) next to the symbol name
- Only show when `status === 'open'` (active chase); hide on terminal states
- Keep the animation subtle — a small 6px colored dot (green for buy, red for sell) that pulses

CSS additions in ChaseCard style block:
```css
.chase-pulse {
  width: 6px; height: 6px; border-radius: 50%;
  animation: chase-pulse 1.5s ease-in-out infinite;
}
@keyframes chase-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.25; }
}
.chase-pulse.buy  { background: var(--c-pos); }
.chase-pulse.sell { background: var(--c-neg); }

.chase-limit-price { font-variant-numeric: tabular-nums; }
.chase-elapsed     { color: var(--c-muted); font-size: 0.75rem; }
.chase-countdown   { color: var(--c-muted); font-size: 0.75rem; }
```

---

## Part 4 — Cancel reconciliation (pipeline, previously approved)

**File:** `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

In the `order_update` WS handler (around line 3799), add position refresh for terminal statuses:

```javascript
if (terminal) {
  const s = String(msg.status || '').toUpperCase();
  if (s === 'CANCELLED' || s === 'REJECTED') loadPositions({ fresh: true });
  return;
}
```

---

## Part 5 — Shared open orders store (pipeline)

**New file:** `frontend/src/lib/data/openOrdersStore.svelte.js`
- Export `openOrderQtyBySymbol` (writable store: `Record<string, number>`)
- Export `pollOpenOrders()` — fetches `fetchOrderEvents(200, 'open')`, builds symbol→qty map

**`frontend/src/routes/(algo)/+layout.svelte`:**
- Import and use `pollOpenOrders` / `openOrderQtyBySymbol` from new store
- Replace local `chaseOrders` + `pollChase()` with `pollOpenOrders()` (keep `_adaptiveInterval` wiring)
- Trigger `pollOpenOrders()` immediately on `book_changed` or terminal `order_update`

---

## Part 6 — CandidateLegRow chips (pipeline)

**`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`:**
- Import `openOrderQtyBySymbol`; pass `pendingQty={$openOrderQtyBySymbol[c.symbol] ?? 0}` to each `<CandidateLegRow>`

**`frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`:**
- Add `pendingQty = 0` prop
- In qty column: if `isClosed` → `closed` chip; else if `pendingQty > 0` → show `{pendingQty}` + `open` chip + remaining qty; else plain qty
- CSS: `.cand-open-chip` (green, compact), `.cand-closed-chip` (muted gray, compact)

---

## Part 7 — Chain +/- button position (pipeline)

**File:** `frontend/src/routes/(algo)/admin/derivatives/OptionChainTab.svelte`

Move +/- buy/sell placement buttons from their current position to sit between the strike column and the bid/ask columns (closer to the strike, not at the far edge).

---

## Files

- `frontend/src/lib/order/OrderTicket.svelte` — chase to CardHeader middle, LTP in body
- `frontend/src/lib/order/ChaseCard.svelte` — elapsed, countdown, current limit, pulse animation
- `frontend/src/lib/data/openOrdersStore.svelte.js` — NEW shared store
- `frontend/src/routes/(algo)/+layout.svelte` — use shared store
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — cancel fix + pendingQty
- `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte` — chips
- `frontend/src/routes/(algo)/admin/derivatives/OptionChainTab.svelte` — +/- column position

## Tests
- svelte-check: yes
- playwright: yes — code-level stale checks:
  - `CardHeader` imported and used in `OrderTicket.svelte`
  - `_lastQuote?.ltp` displayed in body where chase was
  - `chase-pulse` CSS class in ChaseCard
  - `current_limit` rendered in ChaseCard
  - CANCELLED/REJECTED triggers `loadPositions` in WS handler
  - `CandidateLegRow` accepts `pendingQty` prop; open chip conditional render
  - `openOrderQtyBySymbol` exported from `openOrdersStore.svelte.js`

## Commit message
feat(order): chase header + LTP display, chase status card, order pipeline

## Done when
- Chase toggle + agg picker live in OrderTicket header middle zone (CardHeader)
- LTP shown in body where chase was, using `_lastQuote.ltp`
- ChaseCard shows elapsed time, countdown, current limit price, pulsing dot for active chases
- Cancelled/rejected orders trigger position refresh on derivatives page
- Open chip shows pending qty; remaining qty plain; closed chip when qty=0
- Chain +/- buttons between strike and bid/ask
