# RamboQuant Order Lifecycle Reference

**Audit date:** 2026-08-09  
**Scope:** ticket orders, basket orders, agent-driven orders, templates/GTT, broker sync, reconciliation

---

## 1. Entry Points

| Entry | Route / Trigger | File |
|---|---|---|
| Manual ticket | `POST /api/orders/ticket` | `orders_place.py:1912` |
| Basket | `POST /api/orders/basket` | `orders_basket.py:206` |
| Agent-driven | `actions.execute()` after condition match | `actions.py:336` |
| Chase (TP/SL) | `_arm_take_profit()` on parent FILLED | `orders_place.py:761` |
| Template attach | `_fire_template_attach_on_fill()` on FILLED | `orders_place.py:561` |

---

## 2. Validation Pipeline (Ticket Orders)

In sequence — first failure returns immediately:

1. **Mode & demo gate** (`orders_place.py:1013`) — rejects draft mode; traders can't place
   live/shadow orders.
2. **RBAC strategy scope** (`orders_place.py:1026`) — trader roles may only trade assigned
   strategies.
3. **Enum validation** (`orders_place.py:1068`) — side (BUY/SELL), exchange, product,
   order_type, variety all checked; LIMIT/SL require price/trigger_price.
4. **F&O lot-size resolution** (`orders_place.py:1088`) — for NFO/MCX/CDS/BFO/BCD/NCO,
   resolves lot_size from instruments cache (503 on cold cache). Converts input **LOTS** →
   internal **CONTRACTS**: `contracts = input_qty × lot_size`.
5. **Account validation** (`orders_place.py:1159`) — account must be in `_loaded_accounts()`.
6. **G1 (lot multiple)** — removed after lots-convention refactor; `lots × lot_size` is
   always a valid multiple by construction.
7. **G2 (fat-finger 5-lot cap)** (`orders_place.py:1188`) — reject if F&O lots > 5. **Bypassed
   when `intent="close"`** (operator must be able to exit any size position).
8. **MCX 20-lot cap** (`orders_place.py:1386`) — reject MCX/NCO if lots > 20. Exempt for
   `intent="close"`.
9. **Capacity guard** (`orders_place.py:140`) — `current_open_notional + new_notional ≤ cap`.
   Price from: price_hint → ticker LTP → `broker.ltp()` → 503. Skipped entirely when
   `intent="close"` (reducing exposure is OK).
10. **Market-hours gate + price tick alignment** (`orders_place.py:1211`) — rejects orders
    on closed exchanges (409); snaps price to tick grid via `_align_price_to_tick`.
11. **Preflight (LIVE only)** (`orders_place.py:1280`) — calls `actions.run_preflight()`;
    checks MARGIN_SHORTFALL, SEGMENT_INACTIVE, QTY_FREEZE, ACCOUNT_UNKNOWN. On block:
    persists AlgoOrder(status=REJECTED) + `preflight_block` event, returns 422.

---

## 3. Open / Close Intent Convention (ADE equivalent)

RamboQuant does not use an "ADE" (Add/Delete/Execute) vocabulary. The equivalent is the
**`intent` field** on `AlgoOrder` and the ticket request:

| Intent value | Meaning | G2? | MCX cap? | Capacity check? | Template attach? | 50-lot Kite ceiling? |
|---|---|---|---|---|---|---|
| `None` / `"open"` | New position | Yes | Yes | Yes | Yes | Yes |
| `"close"` | Close/reduce existing position | **Bypassed** | **Exempt** | **Skipped** | **Never** | **Bypassed** |

**Where intent is set:**

| Source | Mechanism |
|---|---|
| Ticket endpoint | User passes `data.intent` |
| Basket endpoint | Per-leg `leg.intent` |
| Offset auto-detect | `classifyIntent()` in frontend (`orderTicketSubmit.js:39`) detects existing position → sets `"close"` |
| Chase/TP arms | Hardcoded `intent="close"` (`orders_place.py:856, 905`) |
| Agent fire | From agent strategy config's action params |
| Chase recovery | Re-injected from stored AlgoOrder row (`orders_place.py:417`) |

---

## 4. Placement Modes

### PAPER mode (`orders_place.py:1885`)
1. Persist `AlgoOrder(status=OPEN, engine=paper, mode=paper)`.
2. Check idempotency via `request_id` within 60s.
3. Register with `PaperTradeEngine.register_open_order()`.
4. Write `AlgoOrderEvent(kind="placed")`.
5. Call `record_manual_event()` (async).
6. Preview template attachment (no broker call yet) → populate response with planned GTT info.
7. Return `TicketOrderResponse(status=OPEN)`.

### LIVE mode (`orders_place.py:1713`)
1. Check mode gates: prod branch, paper_trading_mode master kill-switch, circuit breaker.
2. Persist `AlgoOrder(status=OPEN, broker_order_id=NULL)`.
3. **Chase path** (LIMIT + `chase=True` + price > 0): spawn `_start_live_chase()` — background
   loop re-quotes on each tick.
4. **Direct path** (MARKET/SL-M or no-chase): call `broker.place_order(intent=...)`.
5. Write back `broker_order_id` (best-effort).
6. Invalidate "orders" cache. Write `manual_event`. Clear circuit-breaker.
7. Return `TicketOrderResponse(order_id, status=OPEN)`.

### SHADOW / SIM / REPLAY modes
- Shadow: computes margin, does NOT submit to broker. Row persisted with `mode=shadow`.
- Sim: routed via `SimDriver` through paper engine's state machine (same OPEN→FILLED/UNFILLED
  lifecycle) against fabricated tick data.
- Replay: same as sim but against historical data.

---

## 5. Broker Adapter Qty Convention

| Exchange | Kite API expects | translate_qty direction |
|---|---|---|
| NFO / CDS / BFO | Contracts | Pass-through |
| MCX / NCO | **Lots** | `contracts ÷ lot_size` (e.g. CRUDEOIL 100 contracts → 1 lot) |

**50-lot ceiling** in `kite.py:place_order`:
- `intent="close"` → no ceiling (any size close allowed)
- MCX/NCO → 20-lot cap
- Everything else → 50-lot cap

---

## 6. Template / GTT Lifecycle

### Trigger: parent order FILLED
Both paper fills (paper engine detect bid/ask cross) and live fills (broker postback) call
`_fire_template_attach_on_fill()` (`orders_place.py:561`).

### Attach sequence
1. **Per-row asyncio.Lock** (`orders_place.py:310`) — 4h TTL, strong dict. Prevents double
   placement from concurrent postback + chase terminal signals.
2. **Idempotency gate** — skip if `attached_gtts_json` already populated.
3. **Load parent row** — return None if row vanished or already attached.
4. **Resolve template** → `apply_template_to_order()` (`template_attach.py`) with
   `apply_path="live"`.
5. **Place GTT orders** — one per spec (TP, SL, OCO). For each: call `broker.place_gtt()`.
6. **Wing order** (if configured) — scan chain for protective option leg, place MARKET order.
7. **Persist `attached_gtts_json`** — list of `{kind, label, id, sibling_id, sl_trail_pct}`.
8. **Partial GTT alert** — if placed count < planned count: log CRITICAL + send ntfy with
   priority="urgent" (`orders_place.py:625`).

### Template attach guards

| Guard | Location | Action |
|---|---|---|
| Off-hours wing gate | `template_attach.py:229` | Defer wing; GTT-only plans allowed 24×7 |
| applies_to mismatch | `template_attach.py:154` | Alert + no-attach |
| GTT trigger direction | `orders_place.py:228` | Warn if TP/SL deviate > 50% from fill |

### GTT background tracking

| Task | Cycle | File |
|---|---|---|
| OCO watcher (`_task_oco_pair_watcher`) | Every 15s | `background.py:2920` |
| Trailing stop updater (`_task_trail_stop_updater`) | Every 30s | `background.py:2148` |

Both poll `broker.get_gtts()`. **No webhook for GTT fire** (Kite SDK limitation) — detection
lag is up to 30s.

---

## 7. Order State Machine

```
               ┌──────────────────────────────────────────────────────┐
               │                                                      │
   Submit ───► OPEN ──► FILLED ──► (template_attach fires GTTs) ──► done
               │ │
               │ └──► CANCEL_FAILED  (kill attempt failed — retry)
               │
               ├──► CANCELLED   (operator/system cancel — terminal)
               ├──► REJECTED    (broker rejected — terminal)
               └──► UNFILLED    (chase expired / reconcile no-match — terminal)
```

| Status | Terminal | Set by |
|---|---|---|
| OPEN | No | Placement |
| FILLED | Yes | Postback / chase / reconcile |
| CANCELLED | Yes | Operator cancel / system cancel |
| REJECTED | Yes | Broker reject or preflight block |
| UNFILLED | Yes | Chase expiry or reconcile missing |
| CANCEL_FAILED | No | Failed kill attempt |

### AlgoOrder event kinds

`placed · chase_modify · fill · unfill · reject · cancel · postback · margin_check ·
preflight_ok · preflight_block · error`

---

## 8. Postback / Broker Sync Pipeline

```
Broker (Kite/Dhan/Groww)
    │ webhook POST
    ▼
kite_postback_handler / order_postback_dhan / order_postback_groww (orders_postback.py)
    │ HMAC validation (SHA-256)
    ▼
_sync_algo_order_rows (orders_postback.py:155)
    ├─ Direct lookup by broker_order_id
    ├─ Fallback fuzzy match: (account, symbol, side, qty) within 60s window
    └─ Orphan row creation if no match (never silently drop a postback)
    │
    ▼
_sync_apply_row_status (orders_postback.py:134)
    │ status, fill_price, filled_at updated
    ▼
_postback_broadcast_fanout (orders.py:529)
    ├─ Always:          invalidate("orders")
    ├─ Terminal:        invalidate(positions, holdings, margins) + broadcast book_changed
    └─ FILLED:          broadcast position_filled + _positions_refresh_after_fill (5s poll)
    │
    ▼ (async, post-commit)
_fire_template_attach_on_fill (orders_place.py:561)  ← See §6
```

**Broker status maps:**

| Broker | Fill status | Maps to |
|---|---|---|
| Kite/Zerodha | COMPLETE | FILLED |
| Dhan | TRADED | FILLED |
| Groww | EXECUTED / COMPLETED | FILLED |
| All | CANCELLED / REJECTED / EXPIRED | CANCELLED / REJECTED / UNFILLED |

---

## 9. Reconciliation Paths

| Path | Trigger | Frequency | File |
|---|---|---|---|
| Chase polling | Every 20s during active chase | Automatic | `chase.py:861` |
| Chase inline reconcile | End of each active chase cycle | Automatic | `orders.py:646` |
| Single-order reconcile | `POST /api/orders/{id}/reconcile` | Manual | `orders.py:1350` |
| Bulk account reconcile | `POST /api/orders/algo/reconcile` | Manual | `orders.py:1289` |
| GTT OCO detection | Every 15s | Automatic | `background.py:2920` |
| GTT trail update | Every 30s | Automatic | `background.py:2148` |

**No scheduled background reconciliation** for OPEN orders without an active chase. If a
postback is lost and no chase is running, the row stays OPEN indefinitely until the operator
hits the reconcile endpoint.

---

## 10. Alert Pipeline

| Event | Alert type | Trigger |
|---|---|---|
| Order failure | Telegram + SMTP | Broker rejects; `orders_place.py:1554` |
| Sustained rejections | Rate-limited ntfy | Circuit-breaker trip; `orders_helpers.py:90` |
| Partial GTT placement | ntfy priority=urgent | placed < planned; `orders_place.py:625` |
| Agent condition met | WS broadcast + AgentEvent | `agent_engine.py` |

**No alert on:** order cancellation, silent HTTP timeout before broker response, paper engine
unfill.

---

## 11. Frontend Order Event Flow

| Signal | Transport | Latency |
|---|---|---|
| LTP ticks | SSE `/api/quotes/stream` | Real-time |
| Performance / P&L | WS `/ws/performance` | Push on change |
| Order status changes | Poll `GET /api/orders/` | 15s cache |
| Order fill (position delta) | WS `position_filled` message | Push on postback |

Frontend does **not** receive per-order status in real-time. After a fill, `position_filled`
WS event triggers `invalidateAll()` on the orders page; actual order status (fill_price, etc.)
requires the 15s cache to expire or a forced refresh.

---

## 12. Gap Analysis

### GAP-1 ✅ Fixed — Scheduled reconciliation for non-chased OPEN orders

**Status:** `_task_open_order_watchdog` added to `background.py`, runs every 5 min
(configurable via `orders.open_order_watchdog_seconds`); calls `_rco_reconcile_account`
for stale OPEN+live rows.

---

### GAP-2 ✅ Fixed — GTT fire detection lag

**Status:** OCO pair-watcher default interval changed from 15s to 5s
(`templates.oco_pair_poll_seconds`).

---

### GAP-3 ✅ Fixed — Alert on order cancellation

**Status:** `_postback_broadcast_fanout` in `orders.py` now fires `_opp_send_cancel_alert`
task when status=CANCELLED.

---

### GAP-4 ⏸ Deferred — Frontend order status real-time patch

**Status:** `order_update` WS already triggers cache-busted `_debouncedLoadOrders()` reload
(250ms lag); real-time per-row patch is low priority.

---

### GAP-5 ✅ Fixed — Broker placement event for LIVE path

**Status:** `write_event("placed", ...)` added to `_opp_live_handle_success` in `orders_place.py`
(live path now mirrors paper path).

---

### GAP-6 ✅ Fixed — Agent-driven order events logged to AlgoOrderEvent

**Status:** `write_event("agent_trigger", ...)` added in `_write_live_order` (`actions.py`);
`"agent_trigger"` added to `VALID_KINDS` in `order_events.py`.

---

### GAP-7 ⏸ Deferred — GTT cancellation on manual position close

**Status:** `cancel_gtt` call site not found in order placement path; only appears in
`_oco_cancel_survivor` (OCO sibling path); under investigation.
