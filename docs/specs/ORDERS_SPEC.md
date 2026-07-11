# Orders Specification

Comprehensive single source of truth for order placement, ticket lifecycle,
prefill contract, basket execution, postback fan-out, history surfaces, and
API contracts. Covers modal interaction, multi-account grouping, execution
modes, state machines, validation, and F&O lot convention.

**Version**: 2.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `frontend/src/lib/stores.js` · `backend/api/routes/orders*.py` ·
`frontend/src/lib/order/` · `frontend/src/routes/(algo)/orders/`

---

## Contents

1. [OrderTicket Modal Lifecycle](#1-orderticket-modal-lifecycle)
2. [Surface Variants](#2-surface-variants)
3. [Prefill Contract and Propagation](#3-prefill-contract-and-propagation)
4. [OrderTicket State Machine](#4-orderticket-state-machine)
5. [Execution Modes](#5-execution-modes)
6. [Field-Level Validation Spec](#6-field-level-validation-spec)
7. [F&O Lot Convention](#7-fo-lot-convention)
8. [Preflight](#8-preflight)
9. [Basket Orders](#9-basket-orders)
10. [Order History Surface](#10-order-history-surface)
11. [OrderCard Component](#11-ordercard-component)
12. [OrderTimelineDrawer](#12-ordertimelinedrawer)
13. [Postback Fan-Out](#13-postback-fan-out)
14. [API Contract](#14-api-contract)
15. [Edge Cases](#15-edge-cases)
16. [Audit Cases](#16-audit-cases)
17. [Test Coverage Map](#17-test-coverage-map)

---

## 1. OrderTicket Modal Lifecycle

**Modal store** (`orderTicketModal` in `stores.js`):
- `open` — boolean, true when modal is visible
- `prefill` — optional payload carrying symbol, exchange, side, qty, etc.

**Open/close functions**:
- `openOrderTicketModal(prefill?)` — open modal with optional prefill
- `closeOrderTicketModal()` — close modal and reset prefill to null
  (idempotent; unconditionally clears prefill on close)

**No-arg open semantics**:
- `openOrderTicketModal()` opens a blank ticket with no prefill
- `closeOrderTicketModal()` wipes the prefill to null
- Opening again without arguments results in a clean slate
- Persisted tab state (`ticket` / `chain`) survives the open/close cycle

**Open triggers** (context-dependent):
1. Keyboard `t` (trade) — dispatcher in PageHeaderActions opens blank ticket
2. Context menu on position/holding row — opens with prefill from that row
3. Chart deep-link — opens with symbol + exchange from chart state
4. Alert action — can pass prefill with pre-populated fields
5. API deep-link — URL param ?symbol=X&exchange=Y pre-fills the ticket

---

## 2. Surface Variants

### OrderTicket Modal (Keyboard + Deep-Links)

**Launch**: Keyboard `t` or `openOrderTicketModal(prefill?)`

**Surfaces in**:
- PageHeaderActions (page header) — every `/` route has this mounted
- MarketPulse (symbol grid context menu)
- Dashboard (positions/holdings grids)
- Chart modal (from depth ladder or action menu)

**Stays open until**:
- Operator clicks X / presses Esc
- Order successfully places (auto-closes on DRAFT mode; user can dismiss modal)
- Operator navigates to a different route (SvelteKit unloads the component)

**Tab state persistence**: Active tab (Ticket / Chain / Command) persists
across close/reopen on the same page. Switching pages resets to `defaultTab`.

### /orders Page (Dedicated Surface)

**Path**: `/orders`

**Components**:
1. **Order Entry card** (bucket-card-entry) — embedded SymbolPanel with
   Ticket/Chain/Command tabs, Account selector, Mode pill, Chase controls
2. **Status strip** — 5-card counter grid (All / Open / Filled / Rejected /
   Cancelled) that uncollapse the Activity card when clicked
3. **Chases card** (bucket-card-chase) — active chase orders with Kill button;
   hides when no chases running
4. **Activity card** (bucket-card-activity) — 6-tab LogPanel (Orders / Agents /
   Terminal / Simulator / System / News)

**Order Entry state persistence**: Tab choice + symbol + account + mode are
URL-synced as query params (`?symbol=X&exchange=Y`). Operator can bookmark
or share `/orders?symbol=BANKNIFTY26JUN50000CE&exchange=NFO`.

**Activity card filters**:
- Account multi-select (via ActivityHeaderFilters)
- Status histogram filter (All / Open / Filled / Rejected / Cancelled)
- Level filter (All / Error / Warning / Info)
- Inline Modify / Cancel / Reconcile buttons on OrderCard rows

---

## 3. Prefill Contract and Propagation

**Prefill payload shape** (all fields optional):

```typescript
{
  symbol:        string;          // e.g. "RELIANCE" or "NIFTY25APR22000CE"
  exchange:      string;          // e.g. "NSE", "NFO", "MCX", "CDS"
  side:          'BUY'|'SELL';    // order direction
  qty:           number;          // raw count (equity) or contracts (F&O)
  lots:          number;          // lot count (F&O, alternative to qty)
  price:         number;          // limit price hint (optional)
  product:       string;          // 'MIS'|'CNC'|'NRML'|'COVER'|'BO'
  lotSize:       number;          // instrument lot size (for display)
  currentQty:    number;          // signed held qty (>0 long, <0 short)
  action:        'open'|'close'|'modify'; // intent classification
  account:       string;          // pre-selected account code
  accounts:      string[];        // candidate account list
  orderId?:      string;          // when action='modify'
  triggerSource: string;          // 'chart'|'positions'|'watchlist'|'keyboard'
}
```

**No-arg open contract**: `openOrderTicketModal()` (zero arguments) results in:
- `prefill` set to `null`
- Blank ticket form
- Tab state preserved (if same page)

**Deep-link prefill**:
- Chart → passes `{ symbol, exchange }`
- Positions grid → passes `{ symbol, exchange, side, currentQty, account }`
- Watchlist → passes `{ symbol, exchange }`
- Alert → passes `{ symbol, exchange, account, triggerSource }`

**Prefill write-back**: After modal closes, `prefill` is unconditionally set
to null. The next open (with or without prefill) starts fresh.

**Lots vs Qty field**: F&O instruments (lot_size > 1) display `lots` field;
equity (lot_size ≤ 1) display raw `qty`. Backend always receives LOTS for F&O
via the `_lots` request field.

---

## 4. OrderTicket State Machine

**State flow**: IDLE → LOADING_PREFLIGHT → (PREFLIGHT_OK | PREFLIGHT_FAIL) →
SUBMITTING → (SUBMITTED | ERROR) → back to IDLE

**IDLE**:
- Initial state; form visible; Submit button enabled
- User can enter/change all fields

**LOADING_PREFLIGHT**:
- User clicks Submit or reaches the preflight gate
- Parallel ~300ms validation running (margin, qty guards, capacity)
- Submit button disabled (spinner active)
- Form read-only

**PREFLIGHT_OK**:
- All guards passed
- Proceed to submission based on mode (DRAFT / PAPER / LIVE)

**PREFLIGHT_FAIL**:
- Guard returned error (blocker list)
- Error display inline per-field + optional toast
- Submit button re-enabled
- Operator can retry with adjusted values

**SUBMITTING**:
- Valid preflight reached; now placing at broker (LIVE/PAPER) or paper
  engine (PAPER) or recording locally (DRAFT)
- Submit button disabled (spinner active)
- Form read-only

**SUBMITTED**:
- Broker/engine accepted the order
- Order ID returned + formatted success message inline
- Modal stays open to show success (operator can dismiss or edit next order)
- DRAFT mode skips broker call; no network wait

**ERROR**:
- Broker/engine rejected the order (e.g. 403 margin shortfall)
- Error text displayed prominently
- Submit button re-enabled
- Operator can modify and retry

**Mode resolution** (see §5):
- SIM > REPLAY > branch check > SHADOW > paper_trading_mode > agent.trade_mode

**Early exits**:
- Esc key / X button → closes modal, wipes prefill
- Page navigation → unloads component
- WS order_update event → auto-refresh order list (Chases + Activity cards)

---

## 5. Execution Modes

Five modes form a confidence ladder: sim → paper → shadow → live + parallel replay.

| Mode | Quote | Engine | Branch | Real Orders | Use |
|---|---|---|---|---|---|
| 1-SIM | Fabricated | PaperEngine (sim quotes) | Both | No | Stress test agents, backtest strategies |
| 2-PAPER | Live | PaperEngine (live quotes, 5s cache) | Both | No | End-to-end validation, live market test |
| 3-LIVE | Live | Real broker | Prod only | Yes | Real orders (prod main only) |
| 4-REPLAY | Historical OHLCV | PaperEngine | Both | No | Backtesting with historical data |
| 5-SHADOW | Live | Log payload (no execute) | Prod only | No | Pre-live check, risk audit |

**Branch gate**: Non-main branches force PAPER regardless of DB flags.
Main branch uses DB flags to select mode.

**Mode resolution** (`_resolve_mode` in backend):
1. SIM — if enabled in DB config
2. REPLAY — if enabled in DB config
3. Branch check (non-main → force PAPER)
4. SHADOW — if enabled in DB config + prod branch
5. paper_trading_mode DB flag (if true → PAPER)
6. agent.trade_mode field (final fallback)

**Navbar pill** (reflects effective mode):
- SIM → amber (action)
- PAPER → sky-blue (info)
- LIVE → red (danger)
- SHADOW → orange (warning)
- REPLAY → amber (action)
- IDLE → grey (disabled)

**Demo sessions**: Non-authenticated users get DRAFT mode only; no broker
connection available. Orders are local-only for illustration.

---

## 6. Field-Level Validation Spec

### Symbol + Exchange

**Validation**:
- Symbol required (non-empty string)
- Exchange required (must be in instrument list)
- Symbol must resolve via `getInstrument(symbol)` — lookup in cache

**Error messages**:
- "Symbol not found" → symbol doesn't exist in instrument universe
- "Exchange not supported" → exchange not in the config whitelist

**Lot size resolution**: On symbol change, fetch `lot_size` from instrument
cache; F&O (lot_size > 1) shows lots field; equity (≤1) shows qty.

### Side (BUY / SELL)

**Validation**:
- Required field
- Must be 'BUY' or 'SELL' (case-insensitive; normalized to uppercase)

**Intent classification** (via `classifyIntent` in orderTicketSubmit.js):
- currentQty > 0 + SELL → **close** (reduces long position)
- currentQty < 0 + BUY → **close** (reduces short position)
- Everything else → **open** (new position or pyramid)

### Product (MIS / CNC / NRML / COVER / BO)

**Validation**:
- Depends on exchange:
  - NSE/BSE equity: MIS, CNC only (no NRML)
  - NFO: MIS, NRML, COVER (no CNC)
  - MCX: MIS, NRML (no CNC)
- Product required for all exchanges

**G2 guard**: CNC blocked for non-equity exchanges (403 on validate path).

**Operator mapping**:
- MIS → intraday (square-off at EOD)
- CNC → delivery (carry multi-day)
- NRML → F&O normal (overnight margin applies)
- COVER → F&O with cover margin benefit
- BO → bracket order (parent + protective child)

### Order Type (MARKET / LIMIT / SL / SL-M)

**MARKET**:
- No price field needed (disabled in UI)
- `price` sent as `null` to backend

**LIMIT**:
- Price field required (enabled in UI)
- Operator enters limit price manually
- Backend rounds to nearest tick via `roundToTick()`

**SL (Stop-Loss)**:
- Requires both `price` (limit) and `trigger_price` (SL level)
- Both fields enabled
- Trigger must be ≤ LTP for sell orders, ≥ LTP for buy orders

**SL-M (Stop-Loss Market)**:
- Requires `trigger_price` only
- `price` field disabled (sent as null)
- Triggers at market price once trigger level breached

### Quantity (Qty / Lots)

**Equity** (lot_size ≤ 1):
- Field labeled "Qty"
- Raw share count (e.g., 100 shares)
- Frontend sends `quantity` in request

**F&O** (lot_size > 1):
- Field labeled "Lots"
- Lot count (e.g., 2 lots = 2 × 50 contracts on NIFTY)
- Frontend sends `_lots` in request
- Backend multiplies: `contracts = _lots × lot_size`

**G2 Fat-Finger Cap**:
- NSE/BSE/BFO ≤ 5 lots (checked against lots, not contracts)
- MCX ≤ 20 lots
- **Close intent orders bypass G2** (intent="close" skips cap)
- Error on breach: "qty 21 exceeds 20-lot MCX cap"

**G1 Lot-Multiple** (deprecated):
- Removed from ticket boundary (lots × lot_size is always valid by construction)
- Remaining defenses:
  - `_arm_take_profit` live path has inline G1 before broker.place_order
  - `apply_plan_live` GTT layer has synchronous G1 check
  - `kite.py` adapter 50-lot ceiling (hard-blocked, no intent bypass)

### Price (Limit / SL Price)

**Format**:
- Decimal number (₹ amount)
- Operator enters; backend rounds to nearest tick

**Tick rounding** (via `roundToTick` function):
- Calls broker.quote() → fetch top bid/ask
- Rounds to nearest valid tick (varies by exchange/symbol)

**Validation**:
- Must be > 0 when required (LIMIT / SL orders)
- Disabled (read-only) for MARKET / SL-M orders

**Depth ladder** (OrderDepth component):
- Polls `GET /api/quote?exchange=…&tradingsymbol=…` every 2s
- Shows LTP + top-5 bid/ask depth
- Auto-fills limit price on BUY/SELL flip (top ask for BUY, top bid for SELL)
- Fails gracefully to em-dashes when broker unavailable (off-hours)

### Trigger (SL Price)

**Validation**:
- Required for SL / SL-M orders only
- Must be > 0
- Operator enters; backend rounds to nearest tick
- Smart default: closest-to-LTP trigger level (pre-filled on order type flip)

### Account

**Validation**:
- Required (must select from dropdown)
- Must be in loaded accounts list (from broker registry)
- Each order can target one account (basket groups by account separately)

**Account masking**:
- Display: `ZG0790` (or masked version for demo sessions)
- API: unmasked account ID used in all requests
- Audit: logged as masked account (`mask_account()`)

### Variety (regular / iceberg / auction)

**Validation**:
- Defaults to "regular" if omitted
- Kite supports: regular, iceberg (partial fill visibility)
- Rarely used in manual orders

### Validity (DAY / IOC / FOK)

**Defaults**:
- NSE/BSE: DAY (good for session, auto-cancel at EOD)
- NFO/MCX: IOC preferred (immediate-or-cancel)

**Operator override**: Dropdown allows DAY / IOC / FOK selection

---

## 7. F&O Lot Convention

**Frontend sends**: `_lots` (lot count) for F&O instruments (lot_size > 1)

**Backend converts at request boundary** (`_ticket_validate_input`):
- Input: `_lots = 2` (from frontend)
- Lot size: `50` (from instrument cache)
- Calculation: `contracts = 2 × 50 = 100`
- Broker receives: `quantity=100`

**Critical math**: Double-check every multiplication. Kite ships MCX intraday
fields in lots, NSE in contracts. Off-by-magnitude errors have caused
multi-lakh P&L distortion and 20× over-orders.

**G2 guard**: Checks lots directly, not contracts.
- Cap: NSE/BSE ≤ 5 lots, MCX ≤ 20 lots
- Applied in `_ticket_enforce_lot_and_fat_finger()` only when intent ≠ "close"
- Close intent bypasses G2 (reduces exposure)

**GTT layer**: `apply_plan_live()` in `template_attach.py` must call
`broker.translate_qty()` for EVERY GTT leg + wing order before calling
`broker.place_gtt()`. `place_gtt()` does NOT auto-translate.

**Preflight + Basket**: Both paths respect the lots convention:
- Basket margin endpoint translates qty for each leg (lots → contracts)
- Capacity guard uses the price × quantity (where quantity is contracts)

---

## 8. Preflight

**Endpoint**: `POST /api/orders/preflight`

**Purpose**: Pre-validate an order before it hits the broker. Returns
structured blockers with actionable fix text. Does not place an order.

**Parallel execution** (~300ms total):
- All guards run concurrently via `asyncio.gather()`
- Margin check, capacity guard, symbol validity, lot size
- One broker round-trip for margin (cached 15s)

**Request shape**:

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
  "strategy_id": 123
}
```

**Response shape**:

```json
{
  "status": "ok|rejected",
  "guards": [
    {
      "kind": "G2",
      "passed": true,
      "message": "qty 100 ≤ 5-lot cap"
    }
  ],
  "margin": {
    "available": 500000.0,
    "required": 45000.0
  },
  "capacity": {
    "used": 100000.0,
    "cap": 1000000.0
  }
}
```

**Guards** (in execution order):
1. **Qty Guard (G2)** — fat-finger cap (5-lot NSE/BSE, 20-lot MCX)
2. **Margin Guard** — broker.basket_order_margins for the order
3. **Capacity Guard** — strategy capacity (if strategy_id provided)
4. **Symbol Guard** — symbol exists in instrument universe
5. **Lot Size Guard** — lot_size resolves (for F&O)

**Error handling**:
- Inline per-field errors when preflight fails
- Optional toast notification (configurable)
- Submit button re-enabled; operator can adjust and retry

**PAPER mode**: Routes skip preflight (simulated orders don't need broker
validation). Still records AlgoOrder + registers with paper engine.

**Mode-specific behavior**:
- SIM: skips preflight entirely (fabricated engine)
- PAPER: simplified preflight (no real margin check)
- LIVE: full preflight (all guards)
- SHADOW: full preflight (logs without executing)
- REPLAY: skips (historical simulation)

---

## 9. Basket Orders

**Endpoint**: `POST /api/orders/basket`

**Purpose**: Place multiple correlated orders (spreads, hedges) with
offset-aware margin calculation and shared basket tag.

### Basket API Payload

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
      "product": "MIS",
      "order_type": "LIMIT",
      "trigger_price": null,
      "variety": "regular",
      "validity": "DAY"
    }
  ],
  "target_pct": 0.30
}
```

### Execution Flow

1. **Grouping by account** — Orders fan-out per account group
2. **Parallel dispatch** — Accounts run concurrently via `asyncio.gather()`
3. **Sequential legs** — Orders within an account execute in sequence (Kite
   expects this for basket margin benefit)
4. **Shared basket tag** — All orders tagged `basket_tag=ramboq-basket-<uuid>`
5. **Response collection** — Each leg returns: `{order_id, status, error?}`

### Response Shape

```json
{
  "groups": [
    {
      "account": "ZG0790",
      "basket_id": "ramboq-basket-abc-123",
      "results": [
        {
          "leg_index": 0,
          "order_id": "kite-order-1234",
          "status": "ok|error",
          "error": null
        }
      ]
    }
  ]
}
```

### Target Profit Auto-Attach

- **Trigger**: Parent order FILL
- **Default**: `target_pct = 0.30` (30% relative profit)
- **Fields on AlgoOrder**:
  - `target_pct` — profit % (alternative: `target_abs` for absolute ₹)
  - `parent_order_id` — links child order to parent
  - `basket_tag` — shared UUID across entire batch
- **Skipped if**:
  - Parent stays PENDING (no fill yet)
  - Parent REJECTED / CANCELLED
  - Operator specifies `target_pct=null`

### Basket Preflight

```
POST /api/orders/basket/margin
```

- **Purpose**: Compute offset-aware margin WITHOUT placing orders
- **Response**: Per-account `{required, available, shortfall}` for the entire
  group
- **Admin-only**: 403 for non-admin
- **Demo-blocked**: 403 for demo sessions

### Partial Failure

- **5-order basket placed**
- **3 fill, 2 stay PENDING** (low-liquidity symbols)
- **Operator sees**:
  - Partial fills trickling in via order_update WS events
  - Activity card shows all 5 rows: 3× FILLED, 2× OPEN
  - basket_tag links all rows for tracking
- **Manual cleanup**: Operator can cancel/close the unfilled 2 via context menu

### Chase + Basket Integration

- Each leg can have chase enabled independently
- Chases tagged with same `basket_tag` for cross-reference
- Chase page shows all active chases grouped by basket

---

## 10. Order History Surface

**Location**: `/orders` page (Activity card, Orders tab)

**Grid specification**:
- Rows: Last 30 days, 50 per page (pagination)
- Status histogram above grid (visual 5-card bar)
- Search / filter bar with Account + Status + Mode chips

### Columns (LogPanel Orders tab)

1. **Time** — formatDualTz(order.created_at) in IST | EST
2. **Account** — tinted pill (sky / amber / fuchsia / teal rotation)
3. **Symbol** — clickable (opens new ticket prefilled with that symbol)
4. **Side** — BUY (green) / SELL (red)
5. **Qty** — fulfilled / total (e.g., "30/50" for partial fills; "50" for OPEN)
6. **Price** — limit price OR fill price (whichever is most recent)
7. **Status** — OPEN | FILLED | REJECTED | CANCELLED | UNFILLED | CANCEL_FAILED
8. **Slippage** — ↑/↓ indicator (neutral slate color, not side-relative)
9. **Metadata chips** — exchange, product, order_type, variety, validity,
   mode, chase attempts, trigger_price, tag, template attach status, etc.

### Status Histogram Filter

**Above grid**: 5 status cards (All / Open / Filled / Rejected / Cancelled)
- Click any card to filter the grid to that status
- Card styling: gradient + status-colored border + count number (bold 1.3rem)
- Hover: border opacity increases + card lifts 1px
- Selected: 2px amber inset ring on top

### Filters (ActivityHeaderFilters)

1. **Account multi-select** — checkboxes for each account
2. **Status filter** — All / Open / Filled / Rejected / Cancelled chips
3. **Level filter** — All / Error / Warning / Info (for log entries)
4. **Mode filter** — SIM / PAPER / LIVE / SHADOW / REPLAY pills

### OrderCard Context Menu (per-row)

- **Modify** — Opens SymbolPanel with `action='modify'` pre-filled from the row
- **Cancel** — DELETE request to broker (live orders only)
- **Repeat** — New order with same symbol/side/qty/price
- **Close** (if position exists) — Opens ticket with `action='close'` +
  `currentQty` pre-filled
- **View detail** — Expands OrderTimelineDrawer (see §12)

### CSV Export

- Via GridDownloadButton on /orders header
- Columns: Symbol, Side, Qty, Price, Status, Filled Price, Account, Mode, Time
- Date range: last 30 days

---

## 11. OrderCard Component

**SSOT**: Single component `OrderCard.svelte` used by:
1. `/orders` page (Order Activity grid)
2. Activity modal (Orders tab)
3. Dashboard (legacy orders section)
4. Every LogPanel Orders-tab surface

### Unified Rendering

**Broker vs AlgoOrder shape fallback**:
- Broker OrderRow: `tradingsymbol`, `price`, `average_price`, `order_timestamp`
- AlgoOrderInfo: `symbol`, `initial_price`, `fill_price`, `created_at`
- OrderCard handles both via fallback chain: `order?.tradingsymbol || order?.symbol`

**Status data attributes**:
- `data-status="active"` — COMPLETE / FILLED (green border + chip)
- `data-status="running"` — OPEN / TRIGGER PENDING (amber border + chip)
- `data-status="error"` — REJECTED / CANCELLED / CANCEL_FAILED (red border + chip)
- `data-status="inactive"` — UNFILLED (grey border + chip)

**CANCEL_FAILED special case** (Audit H-2):
- Order stayed LIVE at broker; kill attempt failed
- Rendered as red-orange "⚠ KILL FAILED" pill (danger signal)
- Distinct from CANCELLED grey (operator can tell them apart at a glance)
- Operator must verify broker state + reattempt kill or reconcile

### Quantity Display (Audit L-2)

**OPEN orders** (freshly placed):
- Shows raw qty (e.g., "50" not "0/50")
- Prevents confusion: "0/50" reads like an error state

**Terminal or partial-filled orders**:
- Shows "filled/total" (e.g., "30/50")
- Helps operator track progress through partials

**Partial-fill indicator chip**:
- Cyan palette (info/attention, not error)
- Shows when `filled_quantity > 0 && filled_quantity < quantity`
- Hover text: "Order partially filled — 30 of 50 contracts filled. Reconcile
  the remaining 20 open with the broker."

### Account Color Rotation

**Per-account hue**: sky / amber / fuchsia / teal (4-color cycle)
- Keeps assignment stable via `_ACCT_IDX` Map (O(1) lookup)
- Pre-fix used unbounded `string[]` + O(n) indexOf search
- Large order lists (50+ orders) now render instantly instead of O(n²)

### Metadata Chips

Line 2 onwards: compact `log-chip` format (label:value pairs):
- ex:NFO
- qty:50 or qty:30/50
- type:LIMIT
- price:150.0
- slip:↑2.5 (slippage; neutral slate, not side-relative)
- partial:30 of 50 (cyan; only when unfilled_quantity > 0)
- chase:#3 (number of chase attempts)
- trigger:200.5
- validity:DAY
- product:MIS
- variety:regular
- mode:live
- engine:algo
- time:DD-MMM HH:MM:SS IST | EST
- tag:ramboq-ticket or ramboq-agent-loss (color-coded chips)
- tp:+30.0% (take-profit %; green background)
- tmpl:#template_id ✓+w (full attach with wing) | ✓ (full, no wing) | ✓⚠ (partial)
- parent:#parent_id (link to protective wing parent)
- wing:#wing_id or wings:#id1 #id2 (child order links)
- basket:ramboq-basket-uuid (shared basket tag)
- note:status_message (optional broker message)

### Slippage Calculation

```
fill > limit → unfavorable for SELL (favorable for BUY)
fill < limit → favorable for SELL (unfavorable for BUY)
```

**Display rule**: Neutral directional arrow (↑/↓) in slate color
- Pre-fix used green/red which was misleading for one side
- Arrow alone reads correctly regardless of direction

### Re-attach Button (Audit H-3)

**Visible when**:
- `template_id != null`
- Status = FILLED
- `attached_gtts_json == null` (template attach didn't run / failed)

**On click**: Calls `retryTemplateAttach(order.id)`
- Spinner active while in-flight
- Inline note appears: "Re-attach OK · wing #abc123" or error message
- Note disappears after the next poll cycle (~5s)

### Callback Props

- `onCardClick` — fires when card body clicked / Enter / Space
- `onSymbolClick` — fires when symbol cell clicked (typically opens new ticket
  prefilled)
- `onSymbolContext` — fires on right-click / long-press of symbol (opens
  SymbolContextMenu)
- `actions` (slot) — optional trailing action strip (Modify / Cancel / Repeat
  on /orders; suppressed in LogPanel context)

---

## 12. OrderTimelineDrawer

**Purpose**: Right-edge slide-in drawer showing live event timeline for every
OPEN chase order.

**Trigger**: Click on an OPEN OrderCard or via modal context menu

**Drawer structure**:
- Header: "Chase Timeline" title + close X button
- Body: Scrollable list of order sections (per-order)
- Each section groups all events for that order (reverse-chronological)

### Event Kinds + Colors

| Kind | Color | Background | Meaning |
|---|---|---|---|
| `placed` | sky | blue-15% | Initial placement |
| `chase_modify` | amber | amber-15% | Chase attempt (price adjust) |
| `fill` | green | green-15% | Fully filled |
| `unfill` | red | red-15% | Partially unfilled / reduced |
| `reject` | red | red-15% | Broker rejected |
| `preflight_ok` | grey | grey-15% | Pre-validate passed |
| `preflight_block` | red | red-15% | Pre-validate failed |
| `cancel` | slate | slate-15% | User or system cancelled |
| `postback` | violet | violet-15% | Broker postback received |

### Order Section Layout

```
┌─────────────────────────────────────────┐
│ Symbol  BUY  50  [PAPER]                │  ← order header
├─────────────────────────────────────────┤
│ 14:23:45 IST  placed            ₹150.0 │  ← event row
│ 14:23:46 IST  chase_modify      ₹150.5 │
│ 14:24:01 IST  fill              ₹150.25│
└─────────────────────────────────────────┘
```

**Order header**:
- Symbol (formatted via `formatSymbol`)
- Side pill (BUY green / SELL red)
- Qty
- Mode pill (SIM / PAPER / LIVE / unknown)

**Event rows** (reverse-chronological, newest first):
- Time (shortTime() dual-tz via `logTime`)
- Kind badge (colored pill)
- Price (if event.price or event.limit_price present)

### Terminal State Handling

**Terminal kinds**: fill, unfill, reject, cancel

**Terminal orders** (any event has terminal kind):
- Rendered at bottom of list
- Reduced opacity (0.52) so active chases stand out

**Empty state**: "No open chase orders"

### Keyboard Support

- `Esc` key closes the drawer
- Backdrop click closes the drawer

---

## 13. Postback Fan-Out

**Postback trigger**: Broker order fill event (webhook or polling from Dhan/Groww).

**Flow** (shared `_process_broker_postback` + `_postback_broadcast_fanout`):

1. **Match AlgoOrder** — Query by broker_order_id; fallback to account/symbol/side/qty
2. **Update row** — Set `status`, `fill_price`, `filled_at`, detail
3. **Commit to DB** — Save status change
4. **Write event** — `order_events` table: broker_postback (for chase timeline)
5. **Template attach** — Fire `_fire_template_attach_on_fill` if templated parent
6. **Cache invalidate** — Drop `orders` / `positions` / `holdings` from cache (terminal only)
7. **WS broadcasts**:
   - `order_update` (all statuses)
   - `position_filled` (on COMPLETE only)
   - `book_changed` (on terminal)
8. **Audit log** — Entry tagged `category='order.fill|order.cancel|order.reject|order.expired'`

### Postback Telemetry

- Distinct `request_id` per postback (for trace correlation)
- Full payload logged: broker_id, order_id, status, account, symbol, qty, price
- Masked account in all logs (via `mask_account()`)

### Duplicate Postbacks

**Idempotency guard** in `_fire_template_attach_on_fill`:
- If `attached_gtts_json` already populated → skip (don't double-place GTTs)
- Kite delivery retries can fire the same postback multiple times
- Guard ensures one-time execution

### Cross-Broker Postback Handlers

All brokers route to shared `_process_broker_postback`:
- Kite: dedicated handler in orders.py (HMAC validation + inline sync)
- Dhan: `_process_broker_postback` (Dhan webhook)
- Groww: `_process_broker_postback` (Groww webhook)

**Status mapping** (broker → platform):
- COMPLETE → FILLED
- CANCELLED → CANCELLED
- REJECTED → REJECTED
- EXPIRED → UNFILLED

---

## 14. API Contract

### POST /api/orders/ticket

**Purpose**: Place single order (same validation as basket, single account).

**Request**:

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
  "strategy_lot_allocation": "fifo",
  "template_id": 45,
  "target_pct": 0.30,
  "chase": true,
  "chase_aggressiveness": "low"
}
```

**Response**:

```json
{
  "order_id": "kite-order-1234567",
  "mode": "live",
  "status": "ok",
  "detail": "LIVE BUY 2 NIFTY25APR22000CE @₹150.0 · #1234567"
}
```

**Error codes**:
- 400 Bad Request — missing required field
- 403 Forbidden — demo session / branch mismatch / admin check failed
- 403 Forbidden — G2 fat-finger cap exceeded
- 403 Forbidden — strategy capacity cap exceeded
- 503 Service Unavailable — price resolution failed (capacity guard)

### POST /api/orders/preflight

**Purpose**: Pre-validate order without placing it.

**Request**: Same shape as /ticket

**Response**:

```json
{
  "status": "ok",
  "guards": [
    {"kind": "G2", "passed": true, "message": "qty 100 ≤ 5-lot cap"}
  ],
  "margin": {"available": 500000.0, "required": 45000.0},
  "capacity": {"used": 100000.0, "cap": 1000000.0}
}
```

### POST /api/orders/basket

**Purpose**: Place multi-account batch with offset-aware margin.

**Request**:

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
  ],
  "target_pct": 0.30
}
```

**Response**:

```json
{
  "groups": [
    {
      "account": "ZG0790",
      "basket_id": "ramboq-basket-abc-123",
      "results": [
        {
          "leg_index": 0,
          "order_id": "kite-order-1234",
          "status": "ok",
          "error": null
        }
      ]
    }
  ]
}
```

### GET /api/orders/

**Purpose**: List orders (30d window, paginated).

**Query params**:
- `limit` (default 50)
- `offset` (default 0)
- `status` (optional filter: OPEN / FILLED / REJECTED / CANCELLED)
- `account` (optional filter by account)
- `symbol` (optional filter by symbol)

**Response**:

```json
{
  "rows": [
    {
      "id": 123,
      "order_id": "kite-order-1234",
      "symbol": "NIFTY25APR22000CE",
      "transaction_type": "BUY",
      "quantity": 100,
      "price": 150.0,
      "average_price": 150.25,
      "status": "COMPLETE",
      "account": "ZG0790",
      "mode": "live",
      "created_at": "2026-07-11T14:23:45Z",
      "filled_at": "2026-07-11T14:24:15Z"
    }
  ],
  "total": 127
}
```

### POST /api/orders/postback

**Purpose**: Broker webhook notification (Kite only; Dhan/Groww have dedicated routes).

**Request** (Kite HMAC-signed):

```
POST /api/orders/postback?api_key=...
Authorization: <HMAC-SHA256 signature>
Content-Type: application/x-www-form-urlencoded

order_id=...&status=COMPLETE&...
```

**Response**: 200 OK (best-effort; errors logged but not returned to broker)

### PUT /api/orders/{order_id}

**Purpose**: Modify pending order (price/qty/trigger).

**Request**:

```json
{
  "quantity": 100,
  "price": 150.5,
  "trigger_price": 149.5,
  "validity": "DAY"
}
```

**Response**:

```json
{
  "status": "ok",
  "order_id": "kite-order-1234"
}
```

### DELETE /api/orders/{order_id}

**Purpose**: Cancel pending order.

**Response**:

```json
{
  "status": "ok",
  "order_id": "kite-order-1234",
  "cancelled_quantity": 100
}
```

### POST /api/orders/basket/margin

**Purpose**: Compute offset-aware margin WITHOUT placing orders.

**Request**: BasketOrderRequest (same shape as /basket)

**Response**:

```json
{
  "groups": [
    {
      "account": "ZG0790",
      "required": 45000.0,
      "available": 500000.0,
      "shortfall": 0.0
    }
  ]
}
```

**Auth**: Admin-only (403 for non-admin); demo-blocked (403)

---

## 15. Edge Cases

### F&O Quantity Collision

**Scenario**: Operator enters qty=100 (mentally interpreting as contracts)
**Reality**: lot_size=50, so frontend sent `_lots=2`, backend converted to 100 contracts
**Result**: Correct (despite operator's mental model confusion)
**Lesson**: Always use UI labels; qty field for equity, lots field for F&O

### Basket Partial Fill

**Scenario**: 5-order basket placed
**Result**: 3 fill, 2 stay PENDING (low-liquidity symbols)
**Operator sees**: Partial fills trickling in; basket_tag groups them
**Manual cleanup**: Can cancel/close unfilled 2 via context menu

### Basket Rejection on Capacity

**Scenario**: Preflight detects group exceeds capacity_cap_inr
**Result**: Entire basket rejected with 403
**Error message**: Suggests split size
**Operator action**: Splits into smaller batches manually

### Cross-Account Ordering Error

**Scenario**: Basket has orders for two accounts; Account A's broker fails mid-execution
**Result**: Account B's orders still execute (asyncio groups isolate failures)
**Operator sees**: Error for Account A in WS + activity log; Account B shows success

### Market Closed Mid-Ticket

**Scenario**: Operator fills order ticket during pre-market; clicks submit at 15:31 IST (NSE closed)
**Result**: Preflight may reject with "market closed" (depends on broker acceptance window)
**Outcome**: Ticket stays open; operator can retry at next open or cancel

### Close Order (No Current Position)

**Scenario**: Operator opens close-order ticket with currentQty=0 (no position held)
**Result**: Intent classification yields 'open' (not 'close'); G2 cap enforced
**Warning**: UI should prevent this via conditional disable of close-action button

---

## 16. Audit Cases

List concrete things to verify in an audit:

1. **G2 fat-finger cap enforced** in both ticket and basket paths
   - NSE/BSE ≤ 5 lots, MCX ≤ 20 lots
   - Close-intent orders bypass the cap
   - Cap checked against lots, not contracts

2. **`_lots` sent for F&O** in all submission paths (ticket, basket, template attach)
   - Frontend: `_lots` field populated for lot_size > 1
   - Backend: converts `_lots × lot_size` = contracts

3. **DRAFT mode never calls broker API**
   - No preflight, no broker place_order
   - AlgoOrder recorded locally only

4. **Postback always writes audit log entry**
   - Category mapped: order.fill / order.cancel / order.reject / order.expired
   - Masked account logged
   - Distinct request_id per postback

5. **Basket partial fail**: failing legs have REJECTED status, successes have OPEN/FILLED
   - Per-account grouping works correctly
   - Failures don't block other accounts

6. **Prefill `triggerSource` logged in audit**
   - Tracks where order originated (chart, positions, keyboard, etc.)

7. **Product type CNC blocked for non-equity exchanges**
   - 403 on ticket/basket submission for MCX/NFO/CDS

8. **Price field disabled (not just hidden) for MARKET orders**
   - Frontend: input readonly or display:none
   - Backend: price sent as null

9. **Chase attempts incremented correctly**
   - `attempts` counter increases on each retry
   - Stops at `max_attempts` (default 3)

10. **Template attach fires only once per parent fill** (via `attached_gtts_json` check)
    - Duplicate postbacks don't double-place GTTs

11. **Lots/contracts translation** in capacity guard + basket margin
    - Preflight margin uses translated qty
    - Capacity notional calculated as qty × price (where qty = lots × lot_size)

12. **CANCEL_FAILED status** distinct from CANCELLED
    - Rendered with red-orange pill (danger signal)
    - Hover text explains broker still has the order live

13. **Activity card shares filter state** across modal / /orders page / /activity
    - Tab persistence via activityStore
    - Account filter synchronized

14. **OrderCard shows correct qty format**
    - OPEN orders: "50" (not "0/50")
    - Terminal/partial: "30/50" (filled/total)

15. **Slippage direction neutral** (↑/↓ in slate, not green/red)
    - Fill > limit: ↑ (up arrow)
    - Fill < limit: ↓ (down arrow)
    - Same direction visually regardless of BUY/SELL

---

## 17. Test Coverage Map

### Frontend — Playwright

**Modal interaction**:
- `t` key opens blank ticket
- `openOrderTicketModal(prefill)` opens with prefill + correct tab persistence
- X button / Esc key closes modal + wipes prefill
- Re-opening without args shows blank ticket

**Prefill propagation**:
- Chart/position click opens ticket with correct prefill values
- All fields pre-populated: symbol, exchange, side, qty, account
- No stale prefill bleed across modal open/close cycles

**G2 guard**:
- 6-lot NSE order rejected in preflight (5-lot cap)
- 5-lot accepted
- 21-lot MCX rejected (20-lot cap)
- 20-lot accepted
- Close-intent orders bypass cap

**Quantity field**:
- F&O shows `_lots` (1, 2, 5, etc.)
- Equity shows raw count (100, 500, etc.)
- Toggling lot_size recalculates field label

**Mode selector**:
- Dev branch hides LIVE mode
- Prod shows all modes (SIM/PAPER/LIVE/SHADOW/REPLAY)

**Basket prefill**:
- Multiple rows selected on grid → basket card shows all rows
- Account grouping visible in UI

**Depth ladder**:
- Polls every 2s while modal open
- Pauses when tab hidden (via visibility handler)
- Resumes on tab return
- Falls back to em-dashes on broker unavailable

**Order history**:
- Last 30 days displayed
- Pagination works (click "next" → more rows)
- Status histogram filters grid
- Account filter works
- CSV export includes all columns

**OrderCard**:
- OPEN orders show "50" not "0/50"
- Terminal orders show "30/50" (filled/total)
- CANCEL_FAILED renders with red-orange pill + hover text
- Account color rotation stable across page reloads
- Slippage arrow neutral (slate color)

**Re-attach button**:
- Visible only when template_id + FILLED + no attached_gtts_json
- Clicking fires request + shows spinner
- Success message appears + disappears after poll

**Timeline drawer**:
- Opens on OrderCard click or context menu
- Reverse-chronological event list
- Terminal orders dimmed (0.52 opacity)
- Esc key closes
- Backdrop click closes

### Backend — pytest

**Lot conversion**:
- Frontend `_lots=2` for 50-lot MCX → backend converts to `quantity=100`
- Equity `qty=100` passes through as-is

**Preflight parallel** (~300ms):
- 10 orders preflight in parallel (not sequential)
- One broker margin round-trip per preflight call
- All guards complete within budget

**Capacity guard**:
- Projected notional (open + new) vs cap
- 403 on exceed with actionable error message
- Price resolution: ticker → broker.ltp() fallback

**Postback fan-out**:
- Fill update triggers WS emit + audit log + agent action simultaneously
- Masked account in all audit + log output
- COMPLETE status fires template-attach (if templated parent)

**Basket account grouping**:
- 5 orders (3× Account A, 2× Account B) group correctly
- Per-account fan-out via asyncio.gather
- Shared basket_tag across all legs

**Basket margin**:
- Offset-aware calculation (hedging reduces margin)
- Qty translated per-leg (lots → contracts → kite_qty)
- List response → extract last entry for cumulative total

**G2 close bypass**:
- Close-intent order skips FAT_FINGER cap
- Non-close checked against cap

**Template attach**:
- Fires on parent FILL (postback or reconcile)
- Idempotent: duplicate postbacks don't double-place GTTs
- Per-row lock (Phase 3D #4) prevents race

**Chase reconcile**:
- Paper-mode rows with no open_ids → UNFILLED
- Live-mode rows with terminal broker status → synced to platform status
- Partial fills tracked in filled_quantity

**Known gaps**:
- No e2e test for GTT auto-attach after fill (tested at unit + integration level)
- Playwright doesn't cover all basket permutations (tested at unit level)

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v2.0 complete rewrite from codebase audit; added Surface Variants, State Machine, Field Validation, OrderCard, Timeline Drawer, Audit Cases, Test Map; expanded Preflight, Basket, API Contract sections; F&O lot convention detailed |
| 2026-07-11 | v1.0 initial spec from codebase audit; lot convention, prefill contract, basket execution |
