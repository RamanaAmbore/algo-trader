# Plan: Pair/Orphan/GTT color column + Lots before LTP + remove chips

## Context

Replace the chip-in-symbol approach with a dedicated first column that uses colored cell backgrounds to communicate position state: paired (cyan), orphan (amber), GTT (green). Also move Lots column before LTP for faster F&O scanning. Remove all existing chip DOM from symbol cells.

Three position states:
- **Paired** — has a lot-matched peer (`pair_group_key` set, `is_orphan=False`)
- **Orphan** — no peer (`is_orphan=True`)
- **GTT** — has an active GTT order in DB (`has_gtt=True`); takes visual priority over paired/orphan

---

## Backend changes

### 1. `backend/api/schemas.py`
Add to `PositionRow` after `orphan_qty`:
```python
has_gtt: bool = False   # True when an OPEN AlgoOrder with a GTT id exists for this (account, symbol)
```

### 2. `backend/api/routes/positions.py`
Add lightweight GTT query (runs once per request, cheap indexed scan):
```python
async def _fetch_gtt_set(session) -> set[tuple[str, str]]:
    """Return {(account, symbol)} for OPEN AlgoOrders that have a gtt_order_id."""
    rows = await session.execute(_sql_text(
        "SELECT account, symbol FROM algo_orders "
        "WHERE status = 'OPEN' AND gtt_order_id IS NOT NULL"
    ))
    return {(r.account, r.symbol) for r in rows}

def _annotate_gtt(rows: list[PositionRow], gtt_set: set) -> list[PositionRow]:
    return [_msc.structs.replace(r, has_gtt=(r.account, r.tradingsymbol) in gtt_set) for r in rows]
```

In both snapshot path (~line 178) and live path (~line 524): after `_auto_pair_positions`, call `_annotate_gtt`. Wrap the DB call in try/except (warn + use empty set on failure, never block positions).

---

## Frontend changes

### 3. `frontend/src/lib/data/pulseColumns.js`

**New pair/state column** — insert as first element in `mkRightColDefs()` (before `symColRight`):
```js
{
  headerName: '', field: 'pair_group_key', colId: 'pos_state',
  width: 38, minWidth: 38, maxWidth: 38,
  pinned: 'left', resizable: false, sortable: false, suppressMovable: true,
  headerTooltip: 'Position state: Paired (cyan) / Orphan (amber) / GTT (green)',
  cellStyle: (p) => {
    const d = p.data;
    if (!d || d._isTotal) return {};
    if (d.has_gtt)        return { background: 'rgba(74,222,128,0.20)',  color: '#4ade80' };
    if (d.pair_group_key) return { background: 'rgba(34,211,238,0.18)', color: '#67e8f9' };
    if (d.is_orphan)      return { background: 'rgba(251,191,36,0.15)', color: '#fbbf24' };
    return {};
  },
  cellRenderer: (p) => {
    const d = p.data;
    if (!d || d._isTotal) return '';
    if (d.has_gtt)        return 'GTT';
    if (d.pair_group_key) return d.pair_group_key;   // P1, P2, …
    if (d.is_orphan)      return '○';
    return '';
  },
  cellClass: 'ag-cell-pair-state',   // for shared font/size CSS
}
```

**Move Lots column** — cut `lotsCol` definition from its current position (after Qty, line ~512) and paste it immediately after `sparkCol` (before `ltpCol`). New order: Symbol → Sparkline → **Lots** → LTP → Avg → …

### 4. `frontend/src/app.css`
- Add `.ag-cell-pair-state` rule: `font-size: 9px; font-weight: 700; text-align: center; letter-spacing: 0.02em;`
- Remove `.pair-chip` and `.orphan-chip` CSS rules (no longer used)

### 5. `frontend/src/lib/MarketPulse.svelte`
- Remove `pairChipHtml` import and the `pairChips` injection at line ~3367 in `symRenderer`
- Keep `pairGroupSort` import + `_pairGroupPostSort` wiring (still needed for row grouping)

### 6. `frontend/src/lib/PerformancePage.svelte`
- `positionsAllGrid`: prepend the same `pos_state` column definition as first element
- Remove `pairChipHtml` import and chip injection (lines ~654-658 in `_posSymRenderer`)

### 7. `frontend/src/routes/(algo)/admin/derivatives/CandidateLegRow.svelte`
- Remove the `pair_group_key` chip block (lines ~291-294)
- Remove the `orphan_qty` chip block (lines ~296-299)
- Add a colored pair-state indicator in the first cell position of the CSS grid row

### 8. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`
- Update `.cand-grid` CSS `grid-template-columns` to add a 38px leading column for the state indicator

### 9. `frontend/src/lib/data/pairGroupSort.js`
- Remove `pairChipHtml` export (no longer needed; `pairGroupSort` stays)

---

## Agents
- backend: add `has_gtt` to `schemas.py`; add `_fetch_gtt_set` + `_annotate_gtt` to `positions.py`; wire in both paths
- frontend: apply all CSS/column changes across pulseColumns.js, app.css, MarketPulse.svelte, PerformancePage.svelte, CandidateLegRow.svelte, derivatives/+page.svelte, pairGroupSort.js
- backend-test: add tests for `_fetch_gtt_set` + `_annotate_gtt`; update existing pair tests for `has_gtt` default field
- broker: skip
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
feat(positions): Pair/Orphan/GTT state column + Lots before LTP + remove symbol chips

## Done when
- First column shows: `P1`/`P2` cyan for paired, `○` amber for orphan, `GTT` green for GTT
- Lots column appears immediately before LTP in all positions grids
- No pair/orphan chips remain in symbol cells
- svelte-check 0 errors, pytest green
