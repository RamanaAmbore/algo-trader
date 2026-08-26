# Plan: Fix H slot 2 — holdings cur_val shows inv_val instead of ltp × qty

## Context — exact root cause

**Symptom**: NavStrip H pill slot 2 (current holding value) switches between showing `inv_val` (invested
amount = avg × qty) and `inv_val + P&L` (current market value = ltp × qty). Present state shows the
correct value; the wrong state appears on page load, refresh, or whenever live SSE ticks are absent
(pre-open, Dhan/Groww cold LTP, KiteTicker recycle).

**Slot 2 computation** (`PositionStrip.svelte:510–523`):
```javascript
const _liveHoldingsValue = $derived.by(() => {
  for (const h of holdings) {
    const ltp = getSnapshot(sym)?.ltp;        // SSE or published poll LTP
    if (ltp != null && ltp > 0) s += ltp * qty;  // ← live path (correct)
    else                         s += h?.cur_val; // ← fallback (can be wrong)
  }
});
```

**Why `getSnapshot(sym)?.ltp` is null**: `symbolStore.svelte.js:263` silently drops any write
where `ltp ≤ 0`. When broker returns `last_price = 0`, `_publishHoldingsRows` tries to write
`ltp = 0` → dropped → snapshot is null → fallback fires.

**Why `h.cur_val` equals `inv_val`** (the bug):

`_override_stale_ltp_from_ticker` in `holdings.py:294–323` runs after `_enrich_holdings`.
It patches `last_price` via `apply_ltp_patch` (KiteTicker + LKG cache) for rows that had
`last_price = 0`. It recomputes `day_change_val`, `day_change`, and percentages on patched
rows — **but does NOT recompute `pnl` or `cur_val`**.

So after the function runs:
- `last_price` = patched (correct)
- `day_change_val` = recomputed (correct)
- `pnl` = stale from broker (wrong — was computed against old `last_price = 0`)
- `cur_val` = `inv_val + stale_pnl` (wrong — still reflects old prices)

For Dhan/Groww rows that had `last_price = 0`:
- Dhan adapter computes `pnl = (0 − avg) × qty` = large negative → `cur_val ≈ 0`
- Groww adapter: similar

For Kite rows with broker `pnl = 0` (pre-market window where Kite sends `pnl = 0` explicitly):
- `_build_holdings_pnl_expr` trusts broker pnl when `is_not_null()` = True
- `pnl = 0` → `cur_val = inv_val + 0 = inv_val` ← the "shows invested value" case

**When the fallback is reached**:
When `_override_stale_ltp_from_ticker` FAILS to find an LTP (KiteTicker has no tick for that
symbol AND LKG cache is empty), `last_price` stays 0 → `_publishHoldingsRows` publishes ltp=0
→ symbolStore drops it → `getSnapshot(sym)?.ltp = null` → fallback to `h.cur_val`. At that
point `h.cur_val` is wrong (see above).

## Fix: 2 changes

### Change 1 — `_override_stale_ltp_from_ticker` in `backend/api/routes/holdings.py`

After the existing `day_change_val` + `day_change` recomputation block (after line 311), add
recomputation of `pnl` and `cur_val` on the same patched rows:

```python
# Recompute pnl + cur_val on patched rows so the API response is internally
# consistent: last_price, pnl, and cur_val all reflect the same LTP.
if 'average_price' in raw.columns and 'pnl' in raw.columns:
    _avg_p = pd.to_numeric(raw.loc[_sel, 'average_price'], errors='coerce').fillna(0)
    _pnl_p = (_ltp_p - _avg_p) * _qty_p
    # Only overwrite when the patched LTP is positive (guard same as day_change_val).
    raw.loc[_sel, 'pnl'] = _pnl_p.where(_ltp_p > 0, raw.loc[_sel, 'pnl'])
    if 'inv_val' in raw.columns and 'cur_val' in raw.columns:
        _inv_p2 = pd.to_numeric(raw.loc[_sel, 'inv_val'], errors='coerce').fillna(0)
        raw.loc[_sel, 'cur_val'] = (_inv_p2 + raw.loc[_sel, 'pnl']).where(
            _ltp_p > 0, raw.loc[_sel, 'cur_val']
        )
```

Place this immediately after line 311 (`raw.loc[_sel, 'day_change'] = _ltp_p - _cls_p`) and
before `recompute_row_percentages(raw, _sel)`.

### Change 2 — `_build_holdings_pnl_expr` in `backend/brokers/broker_apis.py`

Tighten the broker-pnl trust policy: treat broker `pnl = 0.0` the same as null when valid
prices exist to compute it. This closes the Kite pre-market window where `pnl = 0` is sent
explicitly and blindly trusted.

Current (line 1503–1510):
```python
if has_pnl:
    _broker_pnl = _col_f64_nullable(lf, "pnl")
    return (
        pl.when(_broker_pnl.is_not_null())
        .then(_broker_pnl)
        .otherwise(_pnl_calc)
        .alias("pnl")
    )
```

Change to:
```python
if has_pnl:
    _broker_pnl = _col_f64_nullable(lf, "pnl")
    # Trust broker pnl only when non-null AND non-zero. A zero from the broker
    # is indistinguishable from "no data" (e.g. Kite pre-market window sends
    # pnl=0 when last_price=0). When broker pnl=0 but valid prices exist,
    # use the computed formula. At true breakeven (ltp==avg) both give 0 anyway.
    _valid_prices = (_ltp > 0) & (_avg > 0)
    return (
        pl.when(_broker_pnl.is_not_null() & (_broker_pnl != 0.0))
        .then(_broker_pnl)
        .when(_valid_prices)
        .then(_pnl_calc)
        .otherwise(pl.lit(0.0))
        .alias("pnl")
    )
```

Note: `_ltp`, `_avg` are already defined earlier in the function as `_col_f64(lf, "last_price")`
and `_col_f64(lf, "average_price")`.

## Agents

- backend: Fix `backend/api/routes/holdings.py` — Change 1. Read `_override_stale_ltp_from_ticker` (lines 276–323) in full before editing. Add the pnl + cur_val recompute block after line 311 (after `raw.loc[_sel, 'day_change'] = _ltp_p - _cls_p`) and before `recompute_row_percentages`. Use `_ltp_p`, `_qty_p` already computed above. For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
- broker: Fix `backend/brokers/broker_apis.py` — Change 2. Read `_build_holdings_pnl_expr` (lines 1489–1516) in full before editing. Change `pl.when(_broker_pnl.is_not_null())` condition to also require `_broker_pnl != 0.0`. Add `_valid_prices = (_ltp > 0) & (_avg > 0)` expression (same pattern as `_pnl_calc` already uses). For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
- frontend: skip
- doc: skip
- backend-test: Write `backend/tests/broker/test_holdings_curval_fix.py`. Three test cases:
  (a) `_override_stale_ltp_from_ticker` — row with `last_price=0`, `pnl=-20000`, `cur_val=0` gets patched to `last_price=150`, verify after patch: `pnl = (150-100)×100 = 5000`, `cur_val = inv_val + 5000 = 15000`. Use a minimal DataFrame with columns matching the production path. Patch via monkeypatching `apply_ltp_patch` to return a dummy result that marks the row as patched.
  (b) `_build_holdings_pnl_expr` via `_enrich_holdings` — row with broker `pnl=0` but `last_price=150`, `average_price=100`, `quantity=100`: after enrichment, `pnl = 5000` (not 0), `cur_val = 15000` (not `inv_val=10000`).
  (c) `_build_holdings_pnl_expr` — breakeven row: broker `pnl=0`, `last_price=100`, `average_price=100`, `quantity=100`: after enrichment, `pnl = 0` (computed formula also gives 0), `cur_val = 10000 = inv_val`. Ensures the fix doesn't regress breakeven positions.
  For every file you change or create, you MUST write or update at least one test that covers the changed behaviour. This is mandatory — not optional.
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(holdings): recompute pnl+cur_val after ltp-override patch; tighten broker pnl=0 trust policy

## Done when

- `_override_stale_ltp_from_ticker` recomputes `pnl` and `cur_val` on all LTP-patched rows
- `_build_holdings_pnl_expr` falls through to computed formula when broker sends `pnl = 0`
- Breakeven positions (ltp == avg) unaffected (both formulas give 0 → no regression)
- H slot 2 fallback (`h.cur_val`) always equals `ltp × qty` for holdings with valid LTP
- All three test cases in `test_holdings_curval_fix.py` pass
- Existing holdings enrichment tests still pass
- pytest green, 0 failures
