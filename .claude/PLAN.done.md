# Plan: Template system — all 27 audit findings + exhaustive tests

## Task
Fix all 27 findings from the template audit across backend logic, broker adapters, frontend
validation/display, and DB audit trail. Add exhaustive tests for every fix.

---

## Agents

### Agent 1 — backend: template_attach.py + orders_place.py + orders_postback.py

Fix findings #1–#4, #6, #9–#11, #19–#22 in these files:
- `backend/api/algo/template_attach.py`
- `backend/api/routes/orders_place.py`
- `backend/api/routes/orders_postback.py`

**#1 — LIMIT TP slippage offset (`template_attach.py:1085`)**
In `_leg()`, when `tp_order_type="LIMIT"`, set limit price with a 1-tick offset:
- SELL TP GTT: `limit = trigger - 1 tick` (1 tick = 0.05 for NSE options, 1.0 for futures, configurable)
- BUY TP GTT: `limit = trigger + 1 tick`
Add `_tp_limit_offset(trigger, side, exchange)` helper. Default offset: 0.05 for NFO/MCX options, 0.5 for futures. Read from `backend_config.yaml` key `template.tp_limit_tick_offset`.

**#2 — Partial fill SL gap (`orders_place.py:302`)**
Change template attach trigger: fire ONLY when `filled_quantity >= quantity` (full fill), not on first partial. If broker sends partials, accumulate until complete. Guard: check `row.quantity == row.filled_quantity` before calling `apply_template_to_order`. Add `template_attach_deferred=True` flag on the row when first partial arrives.

**#3 — Sub-lot scale GTTs (`template_attach.py:1265`)**
In `_parse_tp_scales`, after computing `qty = parent_qty * close_pct`, round UP to nearest lot_size multiple: `qty = max(lot_size, round(qty / lot_size) * lot_size)`. If rounding causes total scale qty > parent qty, trim last entry. Add warning note in plan.notes if any scale was rounded.

**#4 — Postback race (`orders_place.py:238`)**
Move `attached_gtts_json` read INSIDE the lock (after acquire), not before. Pattern:
```python
async with _get_template_attach_lock(order_id):
    row = await db.get(AlgoOrder, order_id)  # re-fetch inside lock
    if row.attached_gtts_json:
        return  # already attached
    # proceed with attach
```
Remove the pre-lock read that currently does the idempotency check.

**#6 — Wing silent drop (`template_attach.py:549`)**
When `_pick_wing_by_premium` or `_pick_wing_by_strike` returns `(None, None, reason)`:
- Append reason to `plan.notes`
- Call `send_ntfy_alert(f"Wing attach skipped: {reason} for order {order_id}")` immediately
- Set `result.wing_skipped_reason = reason` in `AttachResult`
- Write wing_skipped_reason to `attached_gtts_json` for display on OrderCard

**#9 — Options premium clarity (`template_attach.py:635`)**
Add assertion + docstring in `_tp_trigger` / `_sl_trigger`:
```python
# fill_price is the executed PREMIUM price for options, UNDERLYING price for futures.
# GTT trigger fires when the PREMIUM hits this level on NSE options.
# For equity futures, trigger fires on the underlying price.
assert fill_price > 0, f"fill_price must be positive, got {fill_price}"
```
Add `instrument_type` param to `_tp_trigger`; if `instrument_type == 'FUTMCX'` log a note that trigger is on MCX commodity price.

**#10 — Zero-OI wing hard reject (`template_attach.py:350`)**
Add config key `template.wing_min_oi_hard_reject` (default 0, meaning use fallback). When > 0, if ALL candidates have OI < this threshold, raise `WingAttachError("no liquid wing found")` instead of falling back. Alert operator with ntfy. Store reason in `attached_gtts_json`.

**#11 — Partial scale GTT mismatch (`orders_place.py:493`)**
After build, compare `len(attached_entries)` vs `len(plan.gtts)`. If mismatch:
```python
if len(attached_entries) != len(plan.gtts):
    logger.critical(f"Partial GTT attach: {len(attached_entries)}/{len(plan.gtts)} placed for order {order_id}")
    send_ntfy_alert(f"PARTIAL GTT attach for {order_id}: only {len(attached_entries)}/{len(plan.gtts)} legs placed")
```
Write partial flag to `attached_gtts_json.partial = True`.

**#13 — TRADED status mapping (`orders_postback.py:256`)**
In `_status_from_broker`, add mapping for Dhan/Groww "TRADED" → "FILLED". Check each broker adapter's status vocabulary. Add a dict:
```python
_BROKER_FILLED_STATUSES = {
    "zerodha_kite": {"COMPLETE"},
    "dhan": {"COMPLETE", "TRADED", "PARTIALLY_TRADED_AND_CANCELLED"},  # full fill only
    "groww": {"COMPLETE", "EXECUTED"},
}
```
Gate template attach on: `broker_status in _BROKER_FILLED_STATUSES[broker_id]` AND `filled_qty >= qty`.

**#19 — Lock TTL warning**
In `_get_template_attach_lock`, log WARNING when lock entry is already expired:
```python
if lock_entry.created_at < now - timedelta(seconds=_TPL_LOCK_TTL_S):
    logger.warning(f"Stale lock evicted for order {order_id} after {age}s")
```

**#21 — MCX cache miss persistent error**
When `_resolve_lot_size_for_order` fails after retry, set `row.template_attach_error = "lot_size_cache_miss"` in DB and return early. Background reconcile checks `template_attach_error IS NOT NULL` and retries after instruments cache is warm (6AM window).

**#22 — Postback during pre-open guard**
At postback entry (before `apply_template_to_order`), check if exchange is open:
```python
if not await _exchange_open_for_attach(exchange):
    logger.warning(f"Postback arrived for {order_id} while {exchange} closed — deferring wing scan")
    row.template_attach_deferred = True
    # GTT placement still proceeds; only wing scan gated
```

---

### Agent 2 — broker: capabilities.py + kite.py + dhan.py

Fix findings #5, #12, #7 (backend part), #24 (Dhan lot validation).

**#5 — Dhan MCX gate at submit (`capabilities.py:123`)**
In `_ticket_validate_input` (orders_place.py), before calling `apply_template_to_order`, check:
```python
if template_id and broker.capabilities.gtt_supports_mcx is False and exchange in ("MCX", "NCO"):
    raise HTTPException(422, "Selected broker does not support MCX GTT — remove template or switch broker")
```
Also add the check in the ticket preview endpoint so operator sees the error before submit.

**#12 — MARKET→LIMIT coercion (`kite.py:502`)**
Change behavior: instead of silently coercing MARKET to LIMIT, raise `BrokerCapabilityError("Kite GTT does not support MARKET order type; use LIMIT")`. Propagate this error to `template_attach.py` and log it in `plan.notes`. Frontend ticket preview should surface this as a warning chip when `tp_order_type="MARKET"` is selected.

**#7-backend — wing_premium_pct=0 guard (`template_attach.py:707`)**
Add explicit validation: if `wing_premium_pct is not None and wing_premium_pct <= 0`, raise `HTTPException(422, "wing_premium_pct must be > 0")` at the preview/ticket endpoint. At `apply_template_to_order` call path, convert to `None` + alert.

---

### Agent 3 — frontend: TemplateBar + OrderCard + OrderTicket + templates page

Fix findings #7-FE, #8, #15, #16, #17, #18, #23–#27.

**Files:**
- `frontend/src/lib/TemplateBar.svelte`
- `frontend/src/lib/order/OrderCard.svelte`
- `frontend/src/lib/order/OrderTicket.svelte`
- `frontend/src/routes/(algo)/automation/templates/+page.svelte`

**#7-FE — TP%/SL% validation in TemplateBar**
Add inline validation on TP% and SL% inputs:
- `tp_pct`: must be > 0. Show red border + "TP must be > 0" if ≤ 0
- `sl_pct`: must be > 0 and < 100. Show red border + "SL must be > 0" if ≤ 0
- Cross-check: if both set, show warning chip "TP% < SL% — exits may overlap" (non-blocking)
- `wing_premium_pct`: must be > 0 if set. Show red border if ≤ 0
- `wing_strike_offset`: warn if result would be ≤ 0 strike (current_strike + offset ≤ 0)
Disable submit button if any validation error is present.

**#8 — tp_scales_json validation in templates page**
In the scale-out textarea validation (`_scalesParseErr`), add:
- Parse each entry: must have `at_pct > 0` and `0 < close_pct <= 100`
- Sum of `close_pct` must not exceed 100% — warn if > 100
- Show per-entry errors: "Entry 2: at_pct must be > 0"
- Block Save if validation fails

**#15 — Absolute trigger prices in order ticket preview**
In `OrderTicket.svelte`, when `_modalPreviewPlan` is available (TemplatePlan from backend preview), extract and display:
- TP trigger: `₹{plan.tp_trigger}` alongside "TP +X%"
- SL trigger: `₹{plan.sl_trigger}` alongside "SL -Y%"
- Wing estimated price: already shown at line 1632 — ensure visible when plan loads
Format: `TP +15% (₹{tp_trigger})` in the preview chip.

**#16 — Side-flip template change feedback**
In `OrderTicket.svelte`, when `_autoSelectTemplate` fires due to side change, briefly flash the template chip:
```svelte
let _templateChanged = $state(false);
// in _autoSelectTemplate, after setting templateId:
_templateChanged = true;
setTimeout(() => _templateChanged = false, 1200);
```
Add `class:tmpl-changed-flash={_templateChanged}` on the template chip with CSS animation (amber background flash for 1.2s).

**#17 — Name failing leg in partial attach chip**
In `OrderCard.svelte`, `_atSummary` helper: when `e.placed_id` is null/missing, include the leg label in the error:
```js
const failedLegs = specs.filter(e => !e.placed_id).map(e => e.label ?? e.kind).join(', ');
// e.g., "TP, Wing"
```
Show in chip: `✓⚠ missing: TP, Wing` instead of generic "partial attach".
Also show `wing_skipped_reason` from `attached_gtts_json.wing_skipped_reason` as a tooltip.

**#18 — Wing params inline on order card**
In `OrderCard.svelte`, extract wing spec from `attached_gtts_json`:
- If `_hasWing`, show additional chip: `W: +500` (strike offset) or `W: 10%p` (premium%)
- Source from `attached_gtts_json` wing entry's symbol or the wing_skipped_reason if absent

**#23 — Trailing stop inline display**
In `OrderCard.svelte`, show trailing stop inline (not just in tooltip):
- If any attached GTT has `sl_trail_pct != null`, add chip: `trail: {sl_trail_pct}%` with amber color
- Place after the `tmpl:` chip in the chip row

**#24 — Inactive default fallback in OrderTicket**
In `_autoSelectTemplate()`, filter out inactive templates:
```js
function _autoSelectTemplate() {
  const candidates = _templates.filter(t => t.is_active && t.is_default && _scopeMatches(t, _side, symType));
  // ...
}
```

**#25 — Unclaimed scope warning**
In `OrderTicket.svelte`, when `_defaultTemplate === null` after auto-select:
- Show warning chip in the template bar: `⚠ No default template for this scope`
- Color: amber (`lv-w` style)
- Don't block submit — just inform

**#26 — Re-attach failure history**
In `OrderCard.svelte`, store re-attach attempts in a `$state` array:
```js
let _retryHistory = $state([]);
```
On each re-attach attempt, push `{ts: Date.now(), error: result.error}`. Show count chip: `⟳ failed ×2` if multiple attempts. Persist via `sessionStorage` keyed by `order_id` so it survives re-render within the session.

**#27 — `applies_to='both'` default shadow warning**
In templates page, when a `both`-scoped template is set as default:
- Show info chip: "This default applies to all scopes — scope-specific defaults take priority"
- In the defaults matrix card, show the `both` default in all 4 slots with a lighter style to indicate it's a catch-all

---

### Agent 4 — backend-test: exhaustive test cases
(Run AFTER agents 1 + 2 complete)

**File:** `backend/tests/broker/test_template_attach.py` (new)

Write tests for ALL 27 findings. Each test name corresponds to the finding number.

```python
# P1 tests
def test_01_limit_tp_slippage_offset_nfo_option():
    # _leg() for SELL TP on NFO option → limit = trigger - 0.05
    
def test_01_limit_tp_slippage_offset_futures():
    # _leg() for SELL TP on NSE futures → limit = trigger - 0.5

def test_02_partial_fill_defers_attach():
    # order with qty=50, filled=30 → template_attach_deferred=True, no GTT placed
    
def test_02_full_fill_triggers_attach():
    # order with qty=50, filled=50 → apply_template_to_order called once
    
def test_02_duplicate_postback_idempotent():
    # two concurrent postbacks, only one should place GTT (lock guards)

def test_03_sublot_scale_qty_rounded_to_lot():
    # MCX parent 1 lot (100 contracts), 30% scale → rounds to 100 (1 lot minimum)
    
def test_03_scale_total_capped_at_parent_qty():
    # 3 scales at 40%+40%+40% → last entry trimmed so total ≤ parent qty

def test_04_postback_race_lock_re_fetches_inside():
    # mock: first call sets attached_gtts_json, second call should see it inside lock

def test_05_dhan_mcx_template_gated_at_submit():
    # Dhan broker + MCX exchange + template_id → HTTPException(422)
    
def test_05_dhan_mcx_template_gated_at_preview():
    # same gate in preview endpoint

def test_06_wing_scan_failure_sends_alert():
    # mock _pick_wing_by_premium returning (None,None,"no OI") → ntfy alert fired
    
def test_06_wing_skip_reason_in_attached_json():
    # wing_skipped_reason persisted to attached_gtts_json

def test_07_wing_premium_pct_zero_raises():
    # wing_premium_pct=0 → HTTPException(422) at preview endpoint

def test_08_tp_scales_all_invalid_entries_drops():
    # tp_scales_json with 3 entries all at_pct<=0 → empty ladder, single tp fallback
    # plan.notes contains warning about dropped entries

def test_09_tp_trigger_positive_fill_price():
    # fill_price=0 → AssertionError
    
def test_09_options_premium_trigger_documented():
    # verify _tp_trigger doc/assertion present for options

def test_10_wing_hard_reject_zero_oi_config():
    # wing_min_oi_hard_reject=1 in config → all zero-OI candidates → WingAttachError raised

def test_11_partial_scale_attach_logs_critical():
    # 3 scale GTTs planned, mock broker places 2 → critical log + ntfy alert
    
def test_11_partial_flag_in_attached_json():
    # partial=True written to attached_gtts_json

def test_12_market_gtt_raises_capability_error():
    # tp_order_type="MARKET" → BrokerCapabilityError propagated, in plan.notes

def test_13_dhan_traded_status_triggers_attach():
    # Dhan postback with status="TRADED", filled_qty==qty → template fires
    
def test_13_dhan_partial_traded_no_attach():
    # Dhan status="PARTIALLY_TRADED_AND_CANCELLED" with filled < qty → no attach

def test_14_gtt_audit_trail_structure():
    # after successful attach, attached_gtts_json contains placed_id + label + leg_type

def test_19_stale_lock_logs_warning():
    # lock entry age > TTL → WARNING logged

def test_20_thin_book_depth_zero_scores():
    # _ta_wing_depth_spread with empty depth → returns 0.0 (known issue documented)

def test_21_lot_size_cache_miss_sets_error_flag():
    # cold instruments cache → template_attach_error="lot_size_cache_miss" on row

def test_22_postback_during_pre_open_defers_wing():
    # exchange closed at postback time → template_attach_deferred=True for wing

# P2 validation tests
def test_tp_pct_negative_rejected():
    # tp_pct=-5 via override → HTTPException(422)

def test_sl_pct_over_100_rejected():
    # sl_pct=150 → HTTPException(422)

def test_scale_sum_over_100_warns():
    # scales 60%+60% → warning note in plan.notes

def test_mcx_lot_size_stale_log_critical():
    # lot_size from DB differs from instruments cache → CRITICAL log

def test_kite_gtt_translate_qty_per_leg():
    # 3-leg GTT, all 3 legs get translate_qty called (mock assert call count)
```

Also add tests to `backend/tests/test_broker_client.py`:
```python
def test_remote_broker_translate_qty_forwarded():
    # RemoteBroker.translate_qty delegates to conn service
    
def test_groww_translate_qty_noop_mcx_logged():
    # Groww MCX order → translate_qty returns raw, WARNING logged
```

---

---

### Finding #28 — Wing feasibility pre-flight + post-fill failure handling (new, operator-requested)

**Two-part requirement:**

**Part A — Pre-flight feasibility check (before parent order is placed)**

In `orders_place.py` ticket preview endpoint AND submit handler, when `template_id` is set and template has a wing:
1. Run `_wing_scan_candidates(symbol, exchange, side, qty, lot_size)` synchronously during preview
2. If no liquid candidates found (`best is None` AND fallback also None):
   - **HARD BLOCK** at both preview and submit: `HTTPException(422, "Wing infeasible — no liquid strikes available. Remove wing from template or select a different expiry.")`
   - Return `wing_feasible: false` + `wing_scan_reason: "<reason>"` in preview response
   - Frontend (`OrderTicket.svelte` preview chip): show RED error chip: "✕ Wing unavailable — cannot place order"
   - Submit button disabled when `wing_feasible === false`
   - No config flag — block is always enforced when template has a wing

**Part B — Post-fill wing failure (after parent fills)**

When `apply_plan_live` finds no wing candidate at attach time (current finding #6):
1. Immediately send ntfy alert: `"ORDER {id} FILLED — wing protection FAILED: {reason}. Position UNPROTECTED. Re-attach or close position."`
2. Write to `attached_gtts_json`:
   ```json
   { "wing_status": "failed", "wing_failed_reason": "no_liquid_strike", "wing_failed_at": "<ts>", "wing_retryable": true }
   ```
3. Set `row.template_attach_error = "wing_failed"` on the AlgoOrder DB row
4. Re-attach endpoint: when `wing_retryable=true`, operator can call re-attach with `relax_oi=true` param — triggers a second scan with OI filter halved and spread filter doubled
5. Background reconcile: every 5 min, retry rows with `template_attach_error="wing_failed"` during market hours (auto-retry up to 3 times), then give up and send final alert

**Tests for #28:**
```python
def test_28a_wing_infeasible_preview_returns_flag():
    # mock _wing_scan returning (None,None,reason) → preview response has wing_feasible=False
    
def test_28a_wing_infeasible_always_blocks_submit():
    # no liquid wing → HTTPException(422) at submit — no config flag, always hard block

def test_28a_wing_infeasible_blocks_preview_too():
    # preview endpoint also returns 422 when wing infeasible

def test_28a_no_wing_in_template_not_blocked():
    # template with tp+sl only (no wing) → submit unaffected by wing scan

def test_28b_post_fill_wing_fail_sends_alert():
    # attach time wing scan fails → ntfy alert fired with "UNPROTECTED" message
    
def test_28b_post_fill_wing_fail_sets_error_flag():
    # template_attach_error="wing_failed" written to row

def test_28b_reattach_with_relax_oi():
    # re-attach with relax_oi=True → OI threshold halved, scan retried

def test_28b_background_reconcile_retries_wing():
    # row with template_attach_error="wing_failed" → reconcile retries during market hours
```

---

### Finding #29 — GTT trigger price validation against LTP + circuit limits (only when template selected)

**Backend — `orders_place.py` preview + submit**

When `template_id` is set, after resolving TP/SL trigger prices, validate against current LTP:

```python
async def _validate_gtt_triggers(plan, ltp, exchange):
    circuit_pct = 0.20  # NSE ±20% intraday circuit
    circuit_hi = ltp * (1 + circuit_pct)
    circuit_lo = ltp * (1 - circuit_pct)

    errors = []
    if plan.tp_trigger:
        if plan.side == "BUY" and plan.tp_trigger <= ltp:
            errors.append(f"TP trigger ₹{plan.tp_trigger} must be above LTP ₹{ltp} for long position")
        if plan.side == "SELL" and plan.tp_trigger >= ltp:
            errors.append(f"TP trigger ₹{plan.tp_trigger} must be below LTP ₹{ltp} for short position")
        if not (circuit_lo <= plan.tp_trigger <= circuit_hi):
            errors.append(f"TP trigger ₹{plan.tp_trigger} outside circuit band ₹{circuit_lo:.2f}–₹{circuit_hi:.2f}")
    if plan.sl_trigger:
        if plan.side == "BUY" and plan.sl_trigger >= ltp:
            errors.append(f"SL trigger ₹{plan.sl_trigger} must be below LTP ₹{ltp} for long position")
        if plan.side == "SELL" and plan.sl_trigger <= ltp:
            errors.append(f"SL trigger ₹{plan.sl_trigger} must be above LTP ₹{ltp} for short position")
        if not (circuit_lo <= plan.sl_trigger <= circuit_hi):
            errors.append(f"SL trigger ₹{plan.sl_trigger} outside circuit band")
    return errors
```

- Fetch LTP via `get_ltp(symbol, exchange)` during preview (non-blocking, cached 5s)
- If errors: return `gtt_trigger_errors: [...]` in preview response + **block submit** with HTTPException(422)
- Gate: ONLY when template is selected (`template_id is not None`). Orders without template skip this check entirely.
- MCX: circuit band is ±4% for commodities — read from `backend_config.yaml` key `template.circuit_pct_by_exchange`

**Frontend — `OrderTicket.svelte` preview chip**
- Show TP/SL trigger validation errors as red chips below the template bar
- Each error on its own line: `✕ TP trigger ₹240.5 below current LTP ₹250`
- Submit button disabled when any gtt_trigger_errors present

**Tests for #29:**
```python
def test_29_buy_tp_below_ltp_blocked():
    # BUY order, TP trigger < LTP → HTTPException(422)

def test_29_buy_sl_above_ltp_blocked():
    # BUY order, SL trigger > LTP → HTTPException(422)

def test_29_sell_tp_above_ltp_blocked():
    # SELL order, TP trigger > LTP → HTTPException(422)

def test_29_sell_sl_below_ltp_blocked():
    # SELL order, SL trigger < LTP → HTTPException(422)

def test_29_trigger_outside_circuit_blocked():
    # TP trigger > LTP * 1.20 → circuit band error

def test_29_no_template_skips_check():
    # order without template_id → no LTP fetch, no circuit check

def test_29_mcx_circuit_pct_4pct():
    # MCX exchange → circuit band ±4%, not ±20%

def test_29_valid_triggers_pass():
    # BUY order, TP > LTP, SL < LTP, both within circuit → no errors
```

---

### Finding #30 — Full template parameter override at order placement

**Requirement:** Operator should be able to view AND edit ALL template parameters inline during order placement (not just TP%/SL%/wing overrides). Changes apply to this order only — do not persist back to the template definition.

**Backend — no changes needed:** `apply_template_to_order` already accepts per-field overrides via `TemplateOverrides`. All fields already passable.

**Frontend — `TemplateBar.svelte`**

Add an **expand toggle** (▾/▴) that reveals the full parameter set:

**Always visible (compact row):**
- Template chip (name + Default/None toggle)
- TP% override input
- SL% override input

**Expanded (▾ clicked):**
- Wing premium % OR wing strike offset (toggle between modes)
- Trailing stop %
- TP order type: LIMIT / MARKET toggle
- Scale-out ladder: text input (JSON) with inline validator showing parsed entries as chips
  e.g. `[{at_pct: 50, close_pct: 50}, {at_pct: 80, close_pct: 100}]` → chips: `50% @ +50%`, `100% @ +80%`

**UX rules:**
- Overrides are shown with a `*` indicator: `TP 20%*` (asterisk = user-modified from template default)
- "Reset to template defaults" link clears all overrides
- Expanded state persists per-session (survives side/symbol change, resets on modal close)
- All validation from finding #7-FE applies to expanded fields too

**Tests:**
```python
def test_30_all_overrides_accepted_by_backend():
    # POST preview with tp_pct, sl_pct, wing_premium_pct, sl_trail_pct, tp_order_type, tp_scales_json all overridden
    # → resolved plan reflects all overrides, not template defaults

def test_30_partial_override_inherits_rest_from_template():
    # only tp_pct overridden → sl_pct, wing still from template defaults

def test_30_overrides_not_persisted_to_template():
    # place order with overrides → GET /api/admin/templates/{id} still shows original values
```

---

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(templates): 27-finding audit — GTT slippage, partial fill, race, broker gates, UI validation + tests

## Done when
- All 27 findings addressed per spec above
- `backend/tests/broker/test_template_attach.py` passes with 30+ test cases covering every finding
- No existing tests broken
- svelte-check 0 errors
- TemplateBar blocks submit on invalid TP%/SL%
- OrderCard shows wing params, trailing stop, and failed leg names inline
- Absolute GTT trigger prices visible in order ticket preview
