# Plan: 6D Audit — /orders page, OrderBook, SymbolPanel, LogPanel

## Context
6-dimension audit of the orders UI surface: `/orders` page (`+page.svelte`), `OrderBook.svelte`,
`SymbolPanel.svelte` (order modal), and `LogPanel.svelte` (order tab + tab content).
Audit dimensions: D1 Correctness · D2 Performance · D3 Dead Code · D4 UX Consistency ·
D5 Broker API Compliance · D6 Documentation Alignment.

---

## Severity-Ranked Punch List

### P1 — Critical (fix before next push)

**OrderBook.svelte**
- P1 D1 `OrderBook.svelte:176` — TRIGGER_PENDING underscore variant unhandled in open-status predicate; broker sends `TRIGGER_PENDING` but filter checks only `TRIGGER PENDING` (space)
- P1 D1 `OrderBook.svelte:140-141` — Date-filter logic inverted: `!(ts && ts !== today && term)` leaks stale non-terminal orders from prior sessions into today's view
- P1 D1 `OrderBook.svelte:167-169` — Empty-string key poisons cancellation Set; `String(o.order_id || o.id || '')` can insert `''` which is never removed, causing spurious disabled cancel buttons

**/orders +page.svelte**
- P1 D4 `+page.svelte:360-375` — Close-action modal missing `account`, `side`, `orderType` props; only place-order path passes them (lines 565-570); close flow opens SymbolPanel without buy/sell context

**SymbolPanel.svelte**
- P1 D1 `SymbolPanel.svelte:738-747` — Template per-leg isolation bug: `leg.template_id != null` strict inequality fails when `template_id === 0`; first leg silently shares template with unrelated orders
- P1 D1 `SymbolPanel.svelte:1002` — CE wing `+= offset`, PE wing `-= offset` is backwards for protective wings; CE should subtract, PE should add (direction reversed)
- P1 D5 `SymbolPanel.svelte:665-668` — lot_size=0/undefined falls through F&O qty gate (`lotSize > 1` check); sends 0-lot margin calc to broker API silently

**LogPanel.svelte**
- P1 D1 `LogPanel.svelte:505` — `algoIds` Set built from `e.order_id` but algo events use `e.id` as primary key; dedup fails, all algo orders appear twice in merged list
- P1 D1 `LogPanel.svelte:510` — `Date.parse(b.ts)` on mixed events: algo events may use different timestamp field names; NaN sort produces non-deterministic ordering
- P1 D4 `LogPanel.svelte:1625` — `.replace(/_/g, ' ')` applied to already-plain-text kinds ('placed', 'fill', 'cancel', 'reject'); produces 'PLACED' correctly but will corrupt any future snake_case addition

---

### P2 — High

**OrderBook.svelte**
- P2 D1 `OrderBook.svelte:113-117` — `_STATUS_PREDICATES.open` only checks `TRIGGER PENDING` (space), misses `TRIGGER_PENDING` (underscore) — partner bug to P1 above
- P2 D4 `OrderBook.svelte:249` — Status chip counts come from unfiltered `orderRows`; filtered row count (`.length !== 1` pluralization) uses `filteredOrderRows`; counts appear inconsistent when a filter is active
- P2 D2 `OrderBook.svelte:256-262` — Status bar re-counts all rows 5 times inline (`orderRows.filter(...)` × 5 per render); compute once in a derived
- P2 D2 `OrderBook.svelte:156-163` — `filteredOrderRows` chains 4 sequential `.filter()` on entire array each render cycle; should short-circuit or memoize by filter key
- P2 D4 `OrderBook.svelte:330` — Empty state shows "No orders." without distinguishing loading state vs filters-removed-all-rows vs truly empty

**/orders +page.svelte**
- P2 D4 `+page.svelte:343-345` — Error banner triggers at `_orderLoadFails >= 3` but counter never resets on success; sticky banner shows forever after any 3-failure run
- P2 D1 `+page.svelte:238` — `Number(o.quantity)` coercion passes 0/falsy qty silently into `orderTicketProps.qty`; no guard before passing to OrderTicket
- P2 D2 `+page.svelte:145` — `_draftOrdersList` spreads `Map.values()` on every render even when `payoffDrafts` unchanged; should be keyed $derived

**SymbolPanel.svelte**
- P2 D2 `SymbolPanel.svelte:977` — `_SYMBOL_PARSE_CACHE` Map grows without bound across session lifetime; unbounded memory leak for long-running operators; needs LRU cap or time-based eviction
- P2 D1 `SymbolPanel.svelte:387` — `activeTab` binding seeded inside `$effect`; host re-bind after mount causes `_activeTabInternal` to diverge, producing tab-switch desync
- P2 D1 `SymbolPanel.svelte:516-521` — `_modalMargin.blocked[0]` accessed without length guard; throws on empty blocked array
- P2 D1 `SymbolPanel.svelte:2228` — `bind:chaseAgg={chaseAgg}` dual-binds between shell's `_sharedChaseAgg` derived and OrderTicket; reactivity drift on multi-leg updates
- P2 D3 `SymbolPanel.svelte:1858-1860` — `handleParsedOrder()` `_props` param received but never used; always forces `activeTab='ticket'` regardless of input

**LogPanel.svelte**
- P2 D2 `LogPanel.svelte:458-513` — `_derivedOrderEvents` fully re-derives on every `filteredOrderRows` change; rebuilds algoIds Set + filters broker array every tick
- P2 D3 `LogPanel.svelte:433-447` — `filteredOrderEvents` derived used only inside `_derivedOrderEvents`; the outer derived then re-filters internally, doubling the work
- P2 D5 `LogPanel.svelte:466` — Uses `o.price` for order price but API field is `initial_price`; fills with wrong value
- P2 D5 `LogPanel.svelte:480` — Uses `o.average_price` for fill price but `OrderCard` uses `fill_price` first; field precedence inconsistent
- P2 D5 `LogPanel.svelte:468` — Status comparison doesn't handle `PARTIAL` or `CANCEL_FAILED` statuses used elsewhere (line 650); unhandled statuses fall to 'placed' kind

---

### P3 — Medium

**/orders +page.svelte**
- P3 D3 `+page.svelte:189` — `_ctxQty` state declared, only written at line 549, never read; dead
- P3 D6 `+page.svelte:95` — Comment says "Default to 'chain'" but `activeTab` defaults to `'ticket'`; stale

**OrderBook.svelte**
- P3 D3 `OrderBook.svelte:233-237` — `_ctxMenu`, `_ctxSym`, `_ctxExch` state vars declared but line 282 reconstructs `_ctxMenu` inline; declarations are dead
- P3 D1 `+page.svelte:32` — `_orderLoadFails` never cleared; banner can't dismiss after recovery

**SymbolPanel.svelte**
- P3 D1 `SymbolPanel.svelte:553-557` — `_lastClearTrigger` never resets after clear fires; rapid re-trigger race condition
- P3 D3 `SymbolPanel.svelte:2343` — `action === 'open'` comment "action-specific" misleading; `_isDemo` now gates first
- P3 D4 `SymbolPanel.svelte:2141-2142` — MCX futures (integers) show trailing ".00" from `toLocaleString('en-IN')` 2-fraction default
- P3 D1 `SymbolPanel.svelte:1392-1396` — `_focusedLeg` resolves via array subscript without bounds check; empty basket returns `undefined`

**LogPanel.svelte**
- P3 D3 `LogPanel.svelte:2044-2063` — CSS `.lp-order-scroll .oc-book-grid` references grid layout never applied to event rows (rows are `.log-row` divs); orphan rule
- P3 D4 `LogPanel.svelte:1631` — Empty state "No orders today." conflates date filter with no-data; should distinguish filter-empty vs genuinely empty
- P3 D6 `LogPanel.svelte:1099` — Comment "Keep colours calm: amber-ish" contradicts code returning orange for negative deltas
- P3 D6 `LogPanel.svelte:59-62` — Comment claims canonical tab order but simulator tab is at wrong position

---

## Fix Plan

Prioritize P1s (all 10) + structural P2s in one pass. Skip P3s unless trivially adjacent.

### Agents
- backend: skip
- frontend: Fix all P1 bugs + the structural P2s listed below. Files: `OrderBook.svelte`, `+page.svelte`, `SymbolPanel.svelte`, `LogPanel.svelte`
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

### Frontend agent scope

**OrderBook.svelte**
1. Fix TRIGGER_PENDING predicate — add underscore variant to `_STATUS_PREDICATES.open`
2. Fix date filter logic — correct the inverted `!(ts && ts !== today && term)` predicate
3. Fix Set poisoning — guard `String(o.order_id || o.id || '')` with `|| 'noop'` suffix or skip empty
4. Fix status chip counts — compute all 5 counts once in `$derived` block, not inline
5. Fix empty state — add loading/filter-empty distinction

**/orders +page.svelte**
6. Fix close modal — pass `account`, `side`, `orderType` props to SymbolPanel in close-action branch
7. Fix error banner — reset `_orderLoadFails` to 0 on successful load
8. Remove dead `_ctxQty` state

**SymbolPanel.svelte**
9. Fix template isolation — change `!= null` to `!== null && !==  undefined` (or `?? -1` sentinel)
10. Fix CE/PE wing direction — swap `+=` and `-=` on protective wing strike calculation (line 1002)
11. Fix `_modalMargin.blocked[0]` — guard with `.length` check
12. Fix `_SYMBOL_PARSE_CACHE` — cap at 500 entries with Map-rotation eviction

**LogPanel.svelte**
13. Fix dedup — build algoIds Set from `e.id || e.order_id` (try both fields)
14. Fix sort — use `e.ts || e.created_at || e.timestamp || ''` with consistent fallback
15. Fix `o.price` → use `o.initial_price ?? o.price ?? ''` for placed message
16. Fix `o.average_price` → use `o.fill_price ?? o.average_price ?? ''` for fill message
17. Add PARTIAL/CANCEL_FAILED to `_deriveKind` switch

## Tests
- pytest: no
- svelte-check: yes
- playwright: no

## Commit message
fix(ui): 6D audit fixes — order modal wing direction, LogPanel dedup/sort, OrderBook filter/Set/date bugs, close modal props, error banner reset

## Done when
- `SymbolPanel.svelte:1002` CE/PE wing strikes go the correct direction
- `LogPanel.svelte` algo/broker events dedup correctly; sort is deterministic
- `OrderBook.svelte` date filter no longer leaks prior-session orders; TRIGGER_PENDING counted as open; cancel button Set not poisoned
- Close modal in `/orders` passes account/side/orderType to SymbolPanel
- Error banner dismisses after successful reload
- `svelte-check` 0 errors

## Critical files
- `frontend/src/routes/(algo)/orders/+page.svelte`
- `frontend/src/lib/OrderBook.svelte`
- `frontend/src/lib/SymbolPanel.svelte`
- `frontend/src/lib/LogPanel.svelte`
