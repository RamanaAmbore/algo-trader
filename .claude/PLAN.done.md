# Plan: Fix pledged holdings — collateral_quantity ignored → quantity=0 hides 56 Kite holdings in Pulse/PositionStrip

## Context
Kite returns `quantity=0` for pledged shares and puts the actual count in `collateral_quantity`.
Our `_enrich_holdings` pipeline uses only `quantity`, so pledged holdings contribute 0 to every
value column. Live Kite API confirms (queried via RemoteBroker today):
- ZJ6294: 20/30 holdings pledged (quantity=0, collateral_quantity>0, t1_quantity=0)
- ZG0790: 36/57 holdings pledged (quantity=0, collateral_quantity>0, t1_quantity=0)
- 56 total Kite holdings are effectively invisible during market hours

Downstream effects:
- `inv_val = avg_price × 0 = 0` → no invested value shown in Pulse for pledged rows
- Frontend `pulseUnified.js:mergeHoldingRows` reads `heldQty = Number(r.quantity) = 0`
  → `denom = 0` → `avg_combined = null` → shows "—" for avg price in Pulse
- `PositionStrip._liveHoldingsValue` uses `qty = h.quantity = 0` → slot 2 shows ~16.52L
  instead of ~1.80C (only non-pledged holdings contribute)

**Why DB snapshot looks fine**: `daily_snapshot.py` uses
`qty = r.get("quantity") or r.get("opening_quantity") or 0`. When `quantity=0` (pledged),
it falls back to `opening_quantity` which mirrors the full pledge count. Snapshot shows
correct counts; the live path does not.

**Not a T+1 issue**: `t1_quantity=0` for all 56 affected rows. Pledged shares are owned
but locked as margin collateral — Kite separates them into `collateral_quantity` and
sets `quantity=0` for the free-deliverable count.

## Task
In `backend/brokers/broker_apis.py:_enrich_holdings`, add a pre-computation step that
merges `collateral_quantity` (and `t1_quantity` as a belt-and-suspenders for future T+1
edge cases) into `quantity` so the effective owned share count is used for all downstream
calculations.

Insert just before line 1731 (before `_qty_col_name = _enrich_holdings_qty_col(cols)`):

```python
# Merge pledged (collateral_quantity) and unsettled (t1_quantity) shares into
# effective quantity so holdings locked as margin collateral remain visible.
# Kite returns quantity=0, collateral_quantity=N for pledged holdings —
# leaving them separate zeros out inv_val/cur_val and hides them in Pulse.
if "quantity" in cols:
    _qty = pd.to_numeric(df["quantity"], errors="coerce").fillna(0).astype(int)
    if "collateral_quantity" in cols:
        _qty = _qty + pd.to_numeric(df["collateral_quantity"], errors="coerce").fillna(0).astype(int)
    if "t1_quantity" in cols:
        _qty = _qty + pd.to_numeric(df["t1_quantity"], errors="coerce").fillna(0).astype(int)
    df["quantity"] = _qty
    cols = set(df.columns)  # refresh after mutate
```

No frontend changes needed — `h.quantity` picks up the corrected value automatically,
fixing Pulse `avg_combined` and PositionStrip slot 2 for all pledged holdings.

## Agents
- backend: skip
- frontend: skip
- broker: In `backend/brokers/broker_apis.py:_enrich_holdings` (line ~1726), add the
  collateral+t1 merge block shown in the Task section BEFORE the `_qty_col_name`
  derivation at line 1731. No other files need changing. For every file you change or
  create, you MUST write or update at least one test covering the changed behaviour.
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(holdings): merge collateral_quantity into quantity so pledged Kite holdings show inv_val, cur_val, and avg_price in Pulse/PositionStrip

## Done when
- `backend/brokers/broker_apis.py:_enrich_holdings` merges collateral_quantity (and t1_quantity) into quantity before inv_val/cur_val
- Test asserts a pledged holding (qty=0, collateral_qty=N, t1_qty=0) produces correct inv_val and non-null effective quantity
- `venv/bin/pytest backend/tests/ -q` passes
