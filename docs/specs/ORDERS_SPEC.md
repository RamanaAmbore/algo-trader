# Orders Specification

Single source of truth for order placement, ticket lifecycle, and history surfaces.
Covers modal interaction, prefill propagation, basket execution, and postback fan-out.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/stores.js` · `backend/api/routes/orders_place.py` · `backend/api/routes/orders_basket.py` · `frontend/src/lib/order/` · `backend/api/routes/orders.py`

---

## Contents

1. [OrderTicket Lifecycle](#1-orderticket-lifecycle)
2. [Prefill Contract and Propagation](#2-prefill-contract-and-propagation)
3. [Execution Modes and Validation](#3-execution-modes-and-validation)
4. [F&O Lot Convention](#4-fo-lot-convention)
5. [Basket Orders](#5-basket-orders)
6. [Order History Surface](#6-order-history-surface)
7. [Postback Fan-Out](#7-postback-fan-out)
8. [Edge Cases](#8-edge-cases)
9. [API Contract](#9-api-contract)
10. [Test Coverage Map](#10-test-coverage-map)

---

## 1. OrderTicket Lifecycle

**Modal states**: DRAFT → PAPER → LIVE (or reversed for demo/dev).

**Modal store** (`orderTicketModal` in `stores.js`):
- `open` — boolean, true when modal is visible
- `prefill` — optional payload carrying symbol, exchange, side, qty, etc.

**Open/close functions**:
- `openOrderTicketModal(prefill?)` — open modal with optional prefill; no-arg opens blank ticket
- `closeOrderTicketModal()` — close modal and reset prefill to null (idempotent)

**Form fields**:
- Symbol + Exchange (symbol picker with exchange hint resolution)
- Side (BUY / SELL)
- Quantity: LOTS for F&O, raw count for equity (conversion at request boundary)
- Price (LIMIT / MARKET; LIMIT requires manual price entry)
- Product (MIS / CNC / NRML / COVER / BO; per-exchange whitelist)
- Account selector (multi-account UI)
- Order type toggles (bracket, cover, trail, etc.)

**Submit validates**:
1. G1 (LOT_MULTIPLE) — removed post-lots-convention; lots × lot_size is always valid by construction
2. G2 (FAT_FINGER_5_LOT_CAP) — NSE/BSE ≤5 lots, MCX ≤20 lots; checked against lots directly
3. Preflight (parallel ~300ms): price resolution, capacity guard, margin check

**Close intent**: Detected in preflight; G2 skipped for close orders (bypassed via `intent="close"`).

---

## 2. Prefill Contract and Propagation

**Prefill payload shape**:
```json
{
  "symbol": "RELIANCE",
  "exchange": "NSE",
  "side": "BUY",
  "qty": 5,
  "lots": 1,
  "price": 2850.0,
  "product": "MIS",
  "lotSize": 1,
  "currentQty": 0,
  "action": "place|modify|cancel|close",
  "account": "ZG0790",
  "accounts": ["ZG0790", "ZG7890"],
  "triggerSource": "chart|positions|watchlist|orders"
}
```

**No-arg open**: `openOrderTicketModal()` (no arguments) opens a blank ticket; persisted tab state
and any prior prefill data are discarded and reset to null.

**Deep-link prefill**: Components can pass prefill from other surfaces (chart, positions grid, movers).
Example: clicking a position row passes `{symbol, exchange, side, currentQty, account}` to open
a close-order ticket with pre-filled details.

**Prefill write-back**: After modal closes (`closeOrderTicketModal()`), prefill is set to null
unconditionally. Opening again without arguments results in a clean slate.

---

## 3. Execution Modes and Validation

**Mode stack** (resolution order):
1. Simulator (SIM) — fabricated broker state
2. Replay — historical OHLCV simulation
3. Branch check (dev ≠ main → force PAPER)
4. Shadow (SHADOW) — logs payload, no execute
5. Paper (PAPER) — live quotes, no real orders
6. Live (LIVE) — real broker execution (prod main only)

**Ticket mode selector**: Shows applicable modes based on branch and DB flags.
On dev branches, LIVE is unavailable and SIM/PAPER/REPLAY are the valid choices.

**Validation gates**:
- Quantity validation: `_ticket_enforce_lot_and_fat_finger()`
- Capacity guard: `_enforce_capacity_guard()` resolves price via ticker → broker.ltp() → hard-fail if unresolvable
- Margin check: Parallel preflight calls all guards via `asyncio.gather()` (~300ms total)

---

## 4. F&O Lot Convention

**Input/Output contract**:
- Frontend sends `_lots` for F&O (raw lot count, e.g., 2 for a 2-lot order)
- Backend converts at request boundary: `contracts = lots × lot_size` (e.g., 2 × 50 = 100 contracts)
- Broker receives contracts (MCX 100-lot orders, NSE 100-contract orders)

**Critical math**: Double-check every multiplication. Kite ships MCX intraday fields in lots,
NSE in contracts. Off-by-magnitude errors have caused multi-lakh P&L distortion and 20× over-orders.

**G2 guard**: Checks lots directly, not contracts. Cap: NSE/BSE ≤5 lots, MCX ≤20 lots.
Applied in `_ticket_enforce_lot_and_fat_finger()` only when intent ≠ "close".

**GTT layer**: `apply_plan_live()` in `template_attach.py` must call `broker.translate_qty()` for
EVERY GTT leg + wing order before calling `broker.place_gtt()`. `place_gtt()` does NOT auto-translate.

---

## 5. Basket Orders

**Basket API**: `POST /api/orders/basket`

**Payload shape**:
```json
{
  "orders": [
    {
      "symbol": "NIFTY25APR22000CE",
      "side": "BUY",
      "quantity": 1,
      "_lots": 1,
      "price": 150.0,
      "exchange": "NFO",
      "account": "ZG0790",
      "product": "MIS"
    }
  ]
}
```

**Execution**:
1. Groups orders by account
2. Parallel fan-out via `asyncio.gather()` (one call per account group)
3. Shared `basket_tag=ramboq-basket-<uuid>` labels all orders in the batch
4. Each account's orders execute sequentially within that group

**Take-profit auto-attach**:
- On parent order FILL: auto-attach target-profit order
- Default target: 0.30 (30% relative profit)
- Fields: `target_pct`, `target_abs`, `parent_order_id`, `basket_tag`
- Skipped if parent order stays PENDING or rejected

**Preflight**:
- Parallel ~300ms across all orders (not sequential)
- Validates qty/price/margins for entire batch before executing any order
- On rejection: entire batch fails with first error; other orders not executed

**Postback**: Each account's orders publish their fills to websocket as they land.
Operator sees fills trickling in by account within a few seconds.

---

## 6. Order History Surface

**Location**: `/orders` page

**Grid**: 30-day rolling window, 50 rows per page, infinite scroll.

**Columns**: Order ID, Symbol, Side, Qty, Price, Status, Fill Price, Fill Qty, Filled At, Account, Mode.

**Filters**:
- Status histogram (PENDING, FILLED, REJECTED, CANCELLED)
- Mode chip (SIM, PAPER, LIVE, SHADOW, REPLAY)
- Account selector (multi-select or single-select)

**Status progression**: PENDING → FILLED (or REJECTED / CANCELLED)

**Inline preview**: Hover/click on row to see order detail: full JSON payload, preflight notes, postback timestamp.

**Activity card**: `/orders` page embeds ActivityLogSurface with Orders tab active (see ACTIVITY_SPEC).

---

## 7. Postback Fan-Out

**Postback trigger**: Broker order fill event (webhook or polling).

**Flow**:
1. `routes/orders_postback.py` receives fill from broker
2. Locates matching `AlgoOrder` row (broker order_id match)
3. Updates AlgoOrder: `status=FILLED`, `fill_price`, `fill_qty`, `filled_at`
4. **Fan-out** (`_postback_broadcast_fanout()` in `orders.py`):
   - Emit to WebSocket (active `/orders` subscribers see update immediately)
   - Write audit log entry (category: `order.fill`)
   - Publish agent action (if take-profit or chase-close triggered)

**Postback telemetry**: Distinct `request_id` per postback; full payload logged.

---

## 8. Edge Cases

### F&O quantity collision

- Operator enters qty=100 (interpreting as contracts)
- Actually lot_size=50, so frontend sent _lots=2, backend converted to 100 contracts (correct)
- Operator's mental model: "I wanted 100 contracts" → result is correct despite confusion

### Basket partial fill

- 5-order basket placed
- 3 fill, 2 stay PENDING (low-liquidity symbols)
- Operator sees partial fills trickling in; basket_tag groups them for tracking
- Can manually close/cancel the unfilled 2 via context menu

### Basket rejection on capacity

- Preflight detects group exceeds capacity_cap_inr
- Entire basket rejected with 403
- Operator splits into smaller batches manually
- Mitigation: preflight error message suggests split size

### Cross-account ordering error

- Basket has orders for two accounts
- Account A's broker connection fails mid-execution
- Account B's orders still execute (async groups isolate failures)
- Operator sees error for Account A in WebSocket + activity log; Account B shows success

### Market closed mid-ticket

- Operator fills order ticket during pre-market; clicks submit at 15:31 IST (NSE closed)
- Preflight may reject with "market closed" (depends on broker acceptance window)
- Ticket stays open; operator can retry at next open or cancel

---

## 9. API Contract

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/api/orders/ticket` | POST | Required | Place single order (same validation as basket, single account) |
| `/api/orders/basket` | POST | Required | Place multi-account batch |
| `/api/orders` | GET | Required | List orders, 30d window, paginated |
| `/api/orders/{order_id}` | GET | Required | Single order detail |
| `/api/orders/{order_id}/modify` | PATCH | Required | Modify pending order (price/qty) |
| `/api/orders/{order_id}/cancel` | DELETE | Required | Cancel pending order |
| `/api/orders/postback` | POST | (Webhook) | Broker fill notification |

**Ticket request schema**:
```json
{
  "symbol": "NIFTY25APR22000CE",
  "exchange": "NFO",
  "side": "BUY",
  "quantity": 100,
  "_lots": 2,
  "price": 150.0,
  "product": "MIS",
  "order_type": "LIMIT",
  "account": "ZG0790",
  "strategy_id": 123,
  "strategy_lot_allocation": "fifo"
}
```

**Preflight response**:
```json
{
  "status": "ok|rejected",
  "guards": [
    {"kind": "G2", "passed": true, "message": "qty 100 ≤ 5-lot cap"}
  ],
  "margin": {"available": 500000.0, "required": 45000.0},
  "capacity": {"used": 100000.0, "cap": 1000000.0}
}
```

---

## 10. Test Coverage Map

### Frontend — Playwright

- **Prefill propagation**: Chart/position click opens ticket with correct prefill values
- **No-arg open**: `openOrderTicketModal()` shows blank ticket (no stale prefill bleed)
- **G2 guard**: 6-lot NSE order rejected; 5-lot accepted; 21-lot MCX rejected; 20-lot accepted
- **Quantity field**: F&O shows _lots (1, 2, 5); equity shows raw count
- **Mode selector**: Dev branch hides LIVE; prod shows all modes
- **Basket prefill**: Multiple rows selected on grid, open basket ticket with account grouping

### Backend — pytest

- **Lot conversion**: Frontend `_lots=2` for 50-lot MCX becomes `quantity=100` at request boundary
- **Preflight parallel**: 10 orders preflight in parallel ~300ms (not sequential)
- **Capacity guard**: Projected notional + current open sum correctly vs cap; 403 on exceed
- **Postback fan-out**: Fill update triggers WebSocket emit + audit log + agent action simultaneously
- **Basket account grouping**: 5 orders (3× Account A, 2× Account B) grouped correctly; fan-out per group
- **G2 close bypass**: Close-intent order skips FAT_FINGER cap; non-close checked

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit; lot convention, prefill contract, basket execution |
