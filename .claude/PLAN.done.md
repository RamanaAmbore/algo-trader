# Plan: Lot-based position auto-pairing with waterfall matching

## Context

Current pairing is manual and order-based: `pair_group_key` = `str(root AlgoOrder id)`, set only when an OPEN AlgoOrder explicitly links positions via `POST /api/orders/pair`. This means positions that are natural hedges (long 3 lots NIFTY CE + short 2 lots NIFTY PE) show no pairing unless the operator manually wires them.

New requirement: **automatic lot-waterfall pairing** at the position level —
- Group positions by underlying (root symbol, e.g. NIFTY from NIFTY24800CE)
- Long + short on the same underlying → pair candidate
- Match by min(long_qty, short_qty); remainder → orphan
- Each pair gets a sequential ID: "P1", "P2" (distinguishes multiple pairs on same underlying)
- `is_orphan = True` when a position has zero paired lots

---

## Algorithm

For each (account, root_symbol) group:
```
pair_n = 1
longs = sorted([r for r if qty > 0], by abs(qty) desc)
shorts = sorted([r for r if qty < 0], by abs(qty) desc)

while longs and shorts remain:
    l, s = longs[0], shorts[0]
    match_qty = min(abs(l.quantity), abs(s.quantity))
    assign pair_group_key = "P{pair_n}" to l and s
    assign paired_qty = match_qty, orphan_qty = abs(qty) - match_qty
    is_orphan = (paired_qty == 0)
    decrement remaining qty; evict exhausted sides
    pair_n++

remaining unmatched positions → is_orphan=True, pair_group_key=None, orphan_qty=abs(qty)
```

Root symbol extraction: strip trailing digits + CE/PE/FUT/OPT suffix.
e.g. `NIFTY24800CE → NIFTY`, `CRUDEOIL24AUGFUT → CRUDEOIL`, `INFY → INFY`.

---

## Files to change

### 1. `backend/api/schemas.py`
Add two fields to `PositionRow` (after `pair_group_key`):
```python
paired_qty: int = 0    # lots matched into a pair
orphan_qty: int = 0    # unmatched lots on this position
```

### 2. `backend/api/routes/positions.py`
- Replace `_fetch_open_order_map` + `_apply_order_map_to_rows` with new function:
  ```python
  def _auto_pair_positions(rows: list[PositionRow]) -> list[PositionRow]
  ```
- Add helper `_root_symbol(tradingsymbol: str) -> str`
- Both call sites (snapshot path ~line 178, live path ~line 524) call `_auto_pair_positions` instead of `_apply_order_map_to_rows`
- Remove `_fetch_open_order_map` and the DB round-trip it required (pure in-memory computation now)

### 3. `frontend/src/app.css`
Keep existing `.row-pos-orphan` / `.row-pos-paired` row color rules unchanged.

### 4. SSOT — shared sort function (new file or extend `pulseGridSetup.js`)

Extract pair-group sort into a shared helper used by ALL positions grids:

```js
// pairGroupSort(rowNodes) — SSOT for all positions grids
// Sort order: P1 group, P2 group, …, orphan group last.
// Sort key: pair_group_key ?? "ZZZZ" (sorts orphans after all Pn keys).
// Within each group: preserve original broker row order.
```

### 5. `frontend/src/lib/MarketPulse.svelte`
- Replace `_pairGroupPostSort` (lines ~3548–3569) with call to shared `pairGroupSort`
- Chip display in symbol cell renderer (`.ag-col-sym`):
  - If `pair_group_key` set: `<span class="pair-chip">P1</span>`
  - If `orphan_qty > 0 && paired_qty > 0`: also `<span class="orphan-chip">{orphan_qty}L orphan</span>`
  - If fully orphaned: existing amber indicator unchanged
- Apply same `_sourceRowClasses` orphan/paired logic already present

### 6. `frontend/src/lib/PerformancePage.svelte`
- `positionsAllGrid`: replace/compose `postSortGroups2Level` with `pairGroupSort` as primary, then two-level contract-type sub-sort within each pair group
- Add pair/orphan chip cell renderer to symbol column

### 7. `frontend/src/routes/(algo)/dashboard/+page.svelte`
- `_eqPosGrid`: add `pairGroupSort` as `postSortRows`; add chip renderer to symbol column

### 8. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- `cand-grid` (candidates/legs panel — shows live positions with full analytics): add `pairGroupSort` as `postSortRows`; add chip renderer to symbol column
- `leg-grid` (draft leg editor — hypothetical legs, not from positions API): **skip** — no `pair_group_key` from API
- `byund-grid` (by-underlying rollup — already grouped by underlying): **skip** — aggregate view, pairing redundant

### Surfaces summary
| Grid | File | Include | Reason |
|---|---|---|---|
| `gridPositions` | MarketPulse.svelte | ✓ | Primary positions surface |
| `positionsAllGrid` | PerformancePage.svelte | ✓ | Detailed position rows |
| `_eqPosGrid` | dashboard/+page.svelte | ✓ | Equity positions detail |
| `cand-grid` | derivatives/+page.svelte | ✓ | Live positions for analysis |
| `leg-grid` | derivatives/+page.svelte | ✗ | Hypothetical drafts, no API pairing |
| `byund-grid` | derivatives/+page.svelte | ✗ | Aggregate rollup, already grouped |
| Summary grids, holdings, funds | various | ✗ | Not individual position rows |

### 5. `frontend/src/app.css`
Add chip styles:
```css
.pair-chip   { background: rgba(34,211,238,0.18); color: #67e8f9; border: 1px solid rgba(34,211,238,0.4); padding: 1px 5px; border-radius: 3px; font-size: 10px; font-weight: 600; }
.orphan-chip { background: rgba(251,191,36,0.15); color: #fbbf24; border: 1px solid rgba(251,191,36,0.35); padding: 1px 5px; border-radius: 3px; font-size: 10px; font-weight: 600; }
```

---

## Agents
- backend: implement `_root_symbol` + `_auto_pair_positions` in `positions.py`; add `paired_qty`/`orphan_qty` to `schemas.py`; remove `_fetch_open_order_map`
- frontend: (1) extract `pairGroupSort` shared helper; (2) apply chip renderer + `pairGroupSort` to all 4 included grids: MarketPulse `gridPositions`, PerformancePage `positionsAllGrid`, Dashboard `_eqPosGrid`, Derivatives `cand-grid`; add chip CSS to `app.css`
- backend-test: update `backend/tests/test_order_pair.py` for new lot-waterfall algorithm; add lot-split scenarios (3 long + 2 short → P1 + 1 orphan)
- broker: skip
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
feat(positions): lot-waterfall auto-pairing — replace order-based with qty-matched pair chips

## Done when
- NIFTY 3-lot long + 2-lot short → P1 chip on both rows, "1L orphan" chip on long row
- Two distinct pairs (NIFTY + BANKNIFTY hedges) → P1 chip on each pair, correctly grouped
- Fully unmatched position → amber orphan indicator, no pair chip
- Grid row order in ALL 4 surfaces: P1 group → P2 → … → orphans as one trailing group
- Pair chips visible in: MarketPulse, PerformancePage, Dashboard, Derivatives cand-grid
- pytest green, svelte-check 0 errors
