# Plan: Order Ticket Comprehensive Audit — Underlying LTP + Analytics + Guards + Docs

## Context

Full audit of the order ticket, template attach, and chase systems vs. what algo platforms (Sensibull, Dhan Pro, Opstra) show. Three explore agents confirmed:

- Underlying spot price (NIFTY index level, CRUDEOIL spot) is absent from the order modal — user explicitly wants it as the header middle element
- OI, Volume, bid-ask spread, days-to-expiry are all available in the quote API response but not rendered
- Position entry price / unrealized P&L not shown for CLOSE orders (avg_price is in positions data but not wired)
- CHASE toggle lives in header middle — conceptually it's an order parameter, belongs near the price row
- No market-hours guards on template attach or chase entry — off-hours postbacks attempt live placement, MARKET wing legs fail silently
- Chase lacks explicit lot_size=0 raise before normalise_qty (root cause of prior MCX 100× incident)
- USER_GUIDE, DESIGN_GUIDE, and showcase page not synced with recent template/chase changes

Confirmed correct (no fix): lots↔contracts, sideLabel BUY/SELL/ADD/CLOSE, QtyInput display, applies_to enforcement, MCX lot_size retry.

---

## Issue Register

### A — Header Structure
| ID | Issue |
|---|---|
| A1 | Underlying spot LTP missing from header middle — no index/commodity context for options/futures |
| A2 | CHASE toggle in header middle — should be near price row (only meaningful for LIMIT orders) |

### B — Missing Analytics (all data already available, just not displayed)
| ID | Issue | Source |
|---|---|---|
| B1 | Days-to-expiry not shown | Parse from symbol (reuse OptionChainTab DTE logic) |
| B2 | OI not shown | Already in `/api/quote` response |
| B3 | Volume not shown | Already in `/api/quote` response |
| B4 | Bid-ask spread ₹ not shown | Compute from depth data (best_ask − best_bid) |
| B5 | Position entry price + unrealized P&L absent for CLOSE orders | avg_price in positions data; wire via SymbolPanel → OrderTicket prop |
| B6 | ATM/ITM/OTM classification not shown | Compute from underlying spot + strike (once A1 done) |

### C — Backend Guards
| ID | Issue | File:Line |
|---|---|---|
| C1 | No market-hours guard in `apply_template_to_order` | `template_attach.py:1818` |
| C2 | No market-hours guard in `apply_plan_live` | `template_attach.py:1391` |
| C3 | No market-hours pre-flight in `chase_order` | `chase.py:1156` |
| C4 | No explicit lot_size=0 raise in `_place_order` before `normalise_qty` | `chase.py:552` |
| C5 | `_emit_chase_terminal` swallows all exceptions silently | `chase.py:260` |

### D — Off-Market Hours UX
| ID | Issue |
|---|---|
| D1 | No "Exchange Closed" indicator in ticket when market is closed |
| D2 | No warning when template with MARKET wing selected at close — wing silently fails |

### E — Modal UX Interaction
| ID | Issue |
|---|---|
| E1 | When navbar menu is clicked while order modal is open, user gets no feedback — modal should animate its outer border to signal it is active and blocking navigation |

### G — Docs
| ID | Issue |
|---|---|
| G1 | USER_GUIDE missing: off-hours template/chase behavior, applies_to explanation, wing-leg market note |
| G2 | DESIGN_GUIDE needs sync: new guards, underlying LTP, CHASE relocation |
| G3 | Showcase page: template+chase order-entry not highlighted |

---

## Task

### Frontend: A1 + A2 + B1–B6 + D1 + D2 (OrderTicket.svelte + OrderDepth.svelte)

**A0 — Contract LTP in header LEFT section (new):**
- The contract's own LTP should appear prominently in the header left section alongside the symbol name
- Source: the quote already polled by OrderDepth every 2s — bubble `ltp` up to the header via the existing `onDepthQuote` callback → `_lastQuote.ltp`
- Layout (left section):
  ```
  [Symbol · LTP ₹23,450]          ← primary row: symbol + live contract LTP
  [Exchange · CE/PE/FUT · lot N · 12d]  ← meta row: exchange, kind, lotsize, DTE
  ```
- Show `—` before first quote arrives
- Refresh button already exists at line 1969 (right section) — confirm it remains visible and that tapping it re-polls OrderDepth immediately

**A1 — Underlying LTP in header middle:**
- Derive underlying symbol via `rootOf.js` (`frontend/src/lib/data/rootOf.js`) from the resolved `symbol` prop
- For options/futures: rootOf gives NIFTY, BANKNIFTY, CRUDEOIL, etc.
- For equity: skip (underlying = contract itself, redundant)
- Read live LTP: `getSnapshot(rootSymbol)?.ltp` from `symbolStore.svelte.js`
- Reactive: `$derived` so it updates as `symbol` prop changes
- Display in `{#snippet middle()}` of CardHeader: `↑ NIFTY 23,450` with ▲/▼ indicator on day change
- Show `—` when snapshot unavailable

**A2 — Relocate CHASE toggle to price row:**
- Move `{#snippet middle()}` CHASE toggle + ChaseAggPicker to just above or inline with the limit price input row
- Guard: same `showLimit && !modeChaseHidden` condition as today
- Visual: row of `[CHASE □] [L|M|H]` followed by `[Price ₹XXXXX]` — groups execution intent with the price

**B1 — Days to expiry chip:**
- Parse expiry date from tradingsymbol (reuse existing OptionChainTab DTE logic at line 279-287)
- Show as small amber chip in the symbol meta line (left header section): `· 12d`
- Only for F&O (skip equity)
- Reactive with `symbol` prop change

**B2 + B3 — OI + Volume in OrderDepth:**
- The `/api/quote` endpoint already returns `oi`, `volume` from Kite — these fields just aren't rendered
- Add below the LTP/prev-close header in OrderDepth: `OI 12.3L · Vol 4.5L`
- Format with Indian lakh notation using existing format helpers

**B4 — Bid-ask spread:**
- Compute from top-of-book: `depth.sell[0].price − depth.buy[0].price`
- Show as small grey chip next to OI/Vol: `Sprd ₹1.50`

**B5 — Position context for CLOSE orders:**
- Add `avgCost: number | null` and `unrealizedPnl: number | null` props to OrderTicket
- SymbolPanel passes these when opening ticket from positions row (positions data has `average_price` and `pnl`)
- Display in a context row below QtyInput when `action === 'close'`: `Entry ₹2,345 · P&L +₹3,200 (+4.2%)`
- Color: green if positive, red if negative

**B6 — ATM/ITM/OTM indicator:**
- Once underlying LTP is known (A1), and strike is known (from parsed symbol):
  - ITM CE: `underlying > strike` → show "ITM" chip (green)
  - OTM CE: `underlying < strike` → show "OTM" chip (muted)
  - ATM: `|underlying − strike| / underlying < 0.005` → show "ATM" chip (amber)
  - Mirror logic for PE
- Show as small chip in symbol meta line next to DTE

**D1 — Exchange closed badge:**
- Check `$isMarketOpen` (or equivalent reactive signal from marketDataStores)
- If market closed, show amber "Closed" badge in header left section next to exchange meta
- Does NOT disable the submit button (GTT orders are valid off-hours; broker decides)

**D2 — Wing warning when template selected at close:**
- If `_selectedTemplate?.has_wing && !isMarketOpen`: show amber inline warning in the template section:
  `⚠ Wing leg (MARKET) will be skipped — market closed at fill time`

**E1 — Modal border animation on navbar click:**
- When the order modal is open and user clicks any navbar menu item, animate the modal's outer border with a brief pulse/glow (CSS `@keyframes` — amber ring flash, ~600ms, e.g. `box-shadow` throb)
- Implementation: add a writable store `orderModalFocusPing` (or increment signal) in `stores.js`; navbar click handler sets it when modal is open; SymbolPanel or the modal shell listens with `$effect` and applies `.ot-modal--ping` CSS class for 600ms via `setTimeout`
- The `.ot-modal--ping` class applies: `animation: modalRingPulse 0.6s ease-out forwards` with amber `box-shadow` expanding then fading
- Does NOT close the modal or navigate — purely visual feedback that modal is active

### Backend: C1–C5 (template_attach.py + chase.py)

**C1 — Market-hours guard in `apply_template_to_order` (template_attach.py:~1850):**
```python
# After resolving lot_size, before resolving plan:
from backend.api.algo.agent_engine import _symbol_exchange_open
if not _symbol_exchange_open(parent_exchange, now_ctx()):
    if _plan_has_wing_leg(template_row):
        _fire_guard_alert(f"[TEMPLATE-GUARD] {parent_exchange} closed — wing MARKET leg skipped")
        return AttachResult(errors=[f"Exchange {parent_exchange} closed — wing requires open market"])
    # GTT-only template: allow (Kite accepts GTTs off-hours)
```

**C2 — Market-hours guard in `apply_plan_live` (template_attach.py:~1410):**
```python
# At top of apply_plan_live, before G1 check:
if not _symbol_exchange_open(parent_exchange, now_ctx()):
    return AttachResult(errors=[f"Exchange {parent_exchange} closed — GTT placement deferred"])
```

**C3 — Market-hours pre-flight in `chase_order` (chase.py:~1200):**
```python
# Before first _place_order:
if not _exchange_open(cfg.exchange):
    _alert_operator(f"Chase not started — {cfg.exchange} closed")
    return ChaseResult(status=ChaseStatus.FAILED, detail=f"{cfg.exchange} closed — chase not started")
```

**C4 — lot_size=0 raise in `_place_order` (chase.py:552):**
```python
if lot_size == 0:
    raise ValueError(f"[CHASE-GUARD] lot_size=0 for {cfg.exchange}/{cfg.symbol} — cache miss")
```

**C5 — Promote `_emit_chase_terminal` exception (chase.py:260):**
Change `logger.debug(f"_emit_chase_terminal: {_e}")` →
`logger.warning(f"_emit_chase_terminal failed — template attach may not have fired: {_e}", exc_info=True)`

### Docs: G1–G3

**G1 — USER_GUIDE.md additions:**
- Off-hours template behavior: GTT legs accepted off-hours; MARKET wing leg skipped with operator alert
- applies_to field: explain the 6 gate values (both, none, buy_any, sell_any, buy_option, sell_option) with examples
- Market-closed chase: fails immediately with alert rather than spinning against rejections

**G2 — DESIGN_GUIDE.md sync:**
- §7 (chase): add market-hours pre-flight to chase sequence diagram
- §9 (template attach): add market-hours guard and wing-skip logic
- Order ticket section: document underlying LTP, DTE chip, CHASE relocation, OI/Vol display, position context

**G3 — showcase/+page.svelte:**
- Expand or add feature card for order entry: "Underlying spot in modal header, DTE chip, position P&L context, template TP/SL/Wing preview"
- Add card highlighting chase aggressiveness levels (L/M/H adaptive pegging)

---

## Agents

- frontend: Implement A0, A1, A2, B1-B6, D1, D2, E1 across `frontend/src/lib/order/OrderTicket.svelte`, `frontend/src/lib/order/OrderDepth.svelte`, and related files. For A0: bubble `_lastQuote.ltp` from OrderDepth to header left section (symbol row); confirm refresh button (line 1969) remains visible. For A1: add underlying spot LTP to header middle via `rootOf.js` + `getSnapshot()`; `$derived`, updates with symbol. For A2: relocate CHASE toggle + ChaseAggPicker from header middle to above/inline with the limit price row. For B1: parse DTE from symbol (reuse OptionChainTab DTE logic, line 279-287); show as `· 12d` chip in symbol meta. For B2+B3: add OI+Volume to OrderDepth display (already in quote response). For B4: compute bid-ask spread from top-of-book depth; show as `Sprd ₹1.50`. For B5: accept `avgCost`/`unrealizedPnl` props; display `Entry ₹X · P&L ₹Y` row when action=close. For B6: show ITM/ATM/OTM chip (amber/green/muted) from underlying+strike. For D1: amber "Closed" badge in header left when market closed. For D2: amber inline warning in template section when template has wing + market closed. For E1: add `orderModalFocusPing` writable store; wire navbar click handler to ping it when modal open; apply `.ot-modal--ping` CSS animation (amber ring pulse, 600ms). For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. Frontend changes → add/update a Playwright spec in `frontend/tests/`.
- backend: Add market-hours guard to `apply_template_to_order` (template_attach.py:~1850) and `apply_plan_live` (template_attach.py:~1410) using `_symbol_exchange_open()`; add exchange-closed pre-flight to `chase_order` (chase.py:~1200) with operator alert; add explicit `lot_size==0` raise in `_place_order` (chase.py:552); promote `_emit_chase_terminal` exception to `logger.warning` with exc_info (chase.py:260). For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. Backend changes → add/update a pytest test in `backend/tests/`.
- backend-test: Write pytest tests covering: (1) template attach returns AttachResult.errors when exchange closed + has wing leg, (2) template attach proceeds (GTT-only) when exchange closed + no wing leg, (3) chase_order returns FAILED immediately when exchange is closed, (4) _place_order raises ValueError on lot_size=0, (5) _emit_chase_terminal logs warning on exception. Place in `backend/tests/broker/` or appropriate test file. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour.
- doc: Update `docs/guides/USER_GUIDE.md` (off-hours edge cases, applies_to explanation, wing-leg note). Update `docs/DESIGN_GUIDE.md` §7/§9/order-ticket sections for new guards, underlying LTP, CHASE relocation, OI/Vol. Update `frontend/src/routes/(algo)/showcase/+page.svelte` to add order-entry feature highlights (underlying LTP, DTE, position context, template TP/SL preview, chase L/M/H). For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. Frontend changes → add/update a Playwright spec in `frontend/tests/`.
- playwright: skip
- broker: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
feat(order-ticket): contract LTP + underlying spot in header; OI/Vol/DTE/spread/position-context; modal ping animation; market-hours guards for template+chase; docs sync

## Done when
- Order modal header left shows contract LTP live next to symbol (updates every 2s via OrderDepth)
- Header middle shows underlying index/commodity spot LTP; updates as symbol changes
- CHASE toggle relocated to price row area (visible only for LIMIT orders)
- DTE chip in symbol meta for F&O (e.g. `· 12d`)
- OI + Volume + bid-ask spread shown in OrderDepth
- Position entry price + unrealized P&L shown for CLOSE orders
- ATM/ITM/OTM chip shown for options
- Amber "Closed" badge in header when exchange closed; wing warning in template section when wing + closed
- Navbar click while modal open triggers amber ring pulse animation (600ms) on modal border
- Template attach returns early with operator alert when exchange closed + wing template
- Chase fails immediately with operator alert when exchange closed at start
- Chase `_place_order` raises explicitly on lot_size=0
- `_emit_chase_terminal` exceptions logged as warning with stack trace
- All pytest green; svelte-check 0 errors
- USER_GUIDE, DESIGN_GUIDE, showcase updated
