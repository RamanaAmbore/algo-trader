# Plan: Block template attachment on close/offset orders

## Task
Templates (TP/SL/Wing GTTs) must not attach when an order reduces or closes an existing
opposite position — regardless of whether `intent="close"` was explicitly set.

A BUY order against an existing SHORT (or SELL against an existing LONG) is a de-facto
close at the exchange level. Attaching TP/SL to such a fill would protect a position
that no longer exists.

Gate the template at three layers:
1. **Ticket submit** — check broker position book; if this order offsets a net position,
   clear `template_id` silently before persisting the AlgoOrder row
2. **Postback + reconcile** — skip attach when `is_close_intent=True` OR position-offset
   detected from lot_ledger (`detect_close_intent`)
3. **Frontend** — when `action === 'close'` or net position is opposite to order side,
   hide TemplateBar and clear templateId

---

## How the position-offset check works

```python
async def _is_offsetting_position(sym: str, exchange: str, side: str, account: str) -> bool:
    """Return True when placing `side` would reduce/close an existing position.
    Uses cached broker positions (30s TTL) — lightweight, no extra broker call typically.
    BUY against a net SHORT → True. SELL against a net LONG → True.
    """
    try:
        import asyncio, pandas as pd
        from backend.brokers.broker_apis import fetch_positions as _fp
        dfs = await asyncio.to_thread(_fp)
        for df in dfs:
            if df is None or df.empty:
                continue
            # match on tradingsymbol + account (account col may be named 'user_id' or 'account')
            acct_col = next((c for c in df.columns if 'account' in c.lower() or 'user' in c.lower()), None)
            mask = df['tradingsymbol'].str.upper() == sym.upper()
            if acct_col:
                mask &= df[acct_col].str.upper() == account.upper()
            rows = df[mask]
            if rows.empty:
                continue
            net_qty = float(rows['quantity'].iloc[0])
            if side.upper() == "BUY" and net_qty < 0:
                return True   # BUY closes a SHORT
            if side.upper() == "SELL" and net_qty > 0:
                return True   # SELL closes a LONG
    except Exception:
        pass  # fail-open: don't block the order on a position-fetch failure
    return False
```

---

## Agents

### Agent 1 — backend: orders_place.py + orders_postback.py
Files: `backend/api/routes/orders_place.py`, `backend/api/routes/orders_postback.py`

**Fix A — `_opl_reconcile_attach_eligible(row)` (orders_place.py ~line 356)**
Add at the top of the eligibility checks:
```python
# Close-intent or explicit close flag → never attach template
if (row.intent or "").lower() == "close":
    return False
if getattr(row, "is_close_intent", False):
    return False
```

**Fix B — `_pb_wants_template_attach(_r)` (orders_postback.py ~line 489)**
Add after the existing guards:
```python
# Explicit close flow → skip template
if (getattr(_r, "intent", None) or "").lower() == "close":
    return False
if getattr(_r, "is_close_intent", False):
    return False
# Strategy-lot close heuristic (covers strategy-attributed orders)
# Note: detect_close_intent is async; fire synchronously in the caller context
# via a small helper if needed, or check strategy_id first
if _r.strategy_id:
    # detect_close_intent is called in _pb_dispatch_template_attach body
    # to avoid blocking the sync return; set a deferred flag instead
    pass  # handled in _pb_dispatch_template_attach below
```

In `_pb_dispatch_template_attach`, add an async check before creating the task:
```python
async def _pb_check_and_fire_template_attach(_r):
    """Async wrapper: runs position-offset check before firing template attach."""
    from backend.api.routes.orders_place import _is_offsetting_position
    if await _is_offsetting_position(
        sym=str(_r.symbol or ""),
        exchange=str(_r.exchange or "NFO"),
        side=str(_r.transaction_type or "BUY"),
        account=str(_r.account or ""),
    ):
        logger.info("[TPL-ATTACH] skipping — order offsets existing position for #%s %s", _r.id, _r.symbol)
        return
    from backend.api.routes.orders_place import _fire_template_attach_on_fill
    await _fire_template_attach_on_fill(...)
```

**Fix C — ticket submit boundary (orders_place.py ~line 1924, after MCX gate)**
Add before `_ticket_enforce_lot_and_fat_finger`:
```python
# Close/offset gate: if this order would reduce an existing opposite position,
# strip template_id — a closing fill must not sprout new TP/SL GTTs.
if data.template_id:
    _is_close = (getattr(data, "intent", None) or "").lower() == "close"
    if not _is_close:
        _is_close = await _is_offsetting_position(sym, data.exchange or "NFO", side, account)
    if _is_close:
        logger.info("[TPL-ATTACH] clearing template_id — close/offset order for %s %s", account, sym)
        data = dataclasses.replace(data, template_id=None)  # or set attribute directly
```

Add `_is_offsetting_position` helper function near the other position utilities in orders_place.py.

---

### Agent 2 — frontend: OrderTicket.svelte
File: `frontend/src/lib/order/OrderTicket.svelte`

**Fix D — suppress template when action='close' or position-offsetting**

1. Add `$effect` to clear templateId when action changes to 'close':
```svelte
$effect(() => {
  if (action === 'close' && templateId !== null) {
    templateId = null;
  }
});
```

2. Add derived: `const _isCloseOrder = $derived(action === 'close')`.

3. Wrap the entire template section (wing warning, flash, no-default warning, trigger
   price chips, wing feasibility error, GTT trigger errors, TemplateBar itself) in
   `{#if !_isCloseOrder}`.

4. In `_autoSelectTemplate()`, add early return when `action === 'close'`:
```js
function _autoSelectTemplate() {
  if (action === 'close') return;  // close orders never get a template
  // ... existing logic
}
```

5. In the submit payload builder, force `template_id: null` when `action === 'close'`
   (defence-in-depth, matches backend clearing).

---

### Agent 3 — backend-test: add 4 targeted tests
Add to `backend/tests/test_template_findings.py`:
```python
def test_close_intent_skips_reconcile_attach():
    # AlgoOrder row with intent="close" → _opl_reconcile_attach_eligible returns False

def test_is_close_intent_flag_skips_reconcile():
    # AlgoOrder row with is_close_intent=True → same gate

def test_close_intent_skips_postback_attach():
    # _pb_wants_template_attach with intent="close" → False

def test_offsetting_position_clears_template_at_submit():
    # mock _is_offsetting_position → True; ticket submit → template_id cleared from data
```

---

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(templates): block template attachment on close + offset orders — position-book check at submit, postback gate, frontend hide

## Done when
- `action='close'` in OrderTicket hides TemplateBar and clears templateId
- Ticket submit with an offsetting order (BUY against SHORT, SELL against LONG) → template_id silently cleared
- Reconcile and postback skip template attach for `intent="close"` or `is_close_intent=True` rows
- `_is_offsetting_position` called in postback dispatch path
- 4 new tests green, no existing tests broken
