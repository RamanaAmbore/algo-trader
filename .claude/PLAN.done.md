# Plan: NavStrip Popup + MCX/Dhan GTT Broker Guard

## Context
Two independent improvements bundled in one deploy:

**1. NavStrip popup anchor** — Clicking any P/M/C/H pill in `PositionStrip.svelte` opens
`NavBreakdown` in a fixed right-side panel. Operator wants it to appear as a floating popup
anchored below the clicked label/value instead.

**2. MCX/Dhan GTT fail-fast + after-market edge cases** — Currently, attaching a template
to an MCX position on a Dhan account only fails at `place_gtt` call time (`dhan.py:1390`),
after lot-size resolution and plan resolution have already run. The `capabilities.py` matrix
already records `gtt_supports_mcx=False` for Dhan but it isn't used as a pre-attach gate.
The fix adds a `validate_gtt_exchange(exchange)` method at the broker-layer ABC so adapters
own the rejection, and wires it as a fail-fast check in `apply_template_to_order` (before plan
resolution). After-market edge cases — MCX evening session hours, Groww OCO split at close
boundary, off-hours GTT attach note — are also addressed.

## Task
1. Anchor NavStrip breakdown popup to the clicked element.
2. Add `validate_gtt_exchange` to broker ABC + Dhan/Groww overrides.
3. Pre-reject MCX+Dhan (and MCX+Groww) templates in `apply_template_to_order` before plan
   resolution, using `caps`, with `_fire_attach_fail_alert`.
4. Add off-hours attach note when GTT-only template is placed outside session hours.
5. Write pytest coverage for cases 2-4.

## Agents
- frontend: Edit `frontend/src/lib/PositionStrip.svelte` per the NavStrip popup steps below.
- broker: Add `validate_gtt_exchange` to `backend/brokers/broker_apis.py` ABC + override in
  `backend/brokers/adapters/dhan.py` + `backend/brokers/adapters/groww.py`. Also call it
  from the top of `apply_plan_live` in `backend/api/algo/template_attach.py`.
- backend: In `backend/api/algo/template_attach.py:apply_template_to_order`, after line 1924
  where `caps` is resolved, add the MCX fail-fast block and off-hours GTT note. No other files.
- backend-test: Add `backend/tests/broker/test_gtt_broker_guard.py` covering: MCX+Dhan raises
  at validate, MCX+Kite passes, Groww MCX raises, NSE on all brokers passes, off-hours GTT-only
  note presence, off-hours wing template blocks (existing C1 behaviour regression test).
- doc: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Implementation — Part 1: NavStrip popup (PositionStrip.svelte)

### 1 — Replace two state vars with one object (lines 35-36)

```js
// OLD:
let _breakdownOpen = $state(false);
let _activeSlot   = $state(/** @type {'P'|'M'|'C'|'H'} */ ('P'));

// NEW:
let _breakdown = $state(
  /** @type {{ open: boolean, slot: 'P'|'M'|'C'|'H', left: number, top: number }} */
  ({ open: false, slot: 'P', left: 0, top: 0 })
);
```

### 2 — Add positioning helper (after state block, before `_load`)

```js
/** @param {MouseEvent|KeyboardEvent} e @param {'P'|'M'|'C'|'H'} slot */
function _openBreakdown(e, slot) {
  const rect = /** @type {HTMLElement} */ (e.currentTarget).getBoundingClientRect();
  const PW = Math.min(28 * 16, window.innerWidth); // 28rem popup width
  let left = rect.left + rect.width / 2 - PW / 2;
  left = Math.max(8, Math.min(left, window.innerWidth - PW - 8));
  const maxH = window.innerHeight * 0.7;
  const top = (rect.bottom + 4 + maxH < window.innerHeight)
    ? rect.bottom + 4
    : rect.top - maxH - 4;
  _breakdown = { open: true, slot, left, top };
}
```

### 3 — Replace all 12 click handlers (pills P/M/C/H, labels + values)

Labels (Enter only):
```js
onclick={(e) => _openBreakdown(e, 'X')}
onkeydown={(e) => e.key === 'Enter' && _openBreakdown(e, 'X')}
```
Values (Enter or Space):
```js
onclick={(e) => _openBreakdown(e, 'X')}
onkeydown={(e) => (e.key === 'Enter' || e.key === ' ') && _openBreakdown(e, 'X')}
```

### 4 — Update panel markup (lines 976-990)

```svelte
{#if _breakdown.open}
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions a11y_click_events_have_key_events -->
  <div class="ps-breakdown-overlay" role="presentation"
       onclick={() => (_breakdown.open = false)}
       onkeydown={(e) => e.key === 'Escape' && (_breakdown.open = false)}>
    <div class="ps-breakdown-panel"
         style="left:{_breakdown.left}px; top:{_breakdown.top}px"
         role="dialog" tabindex="-1"
         onclick={(e) => e.stopPropagation()}
         onkeydown={(e) => e.stopPropagation()}>
      <button type="button" class="ps-breakdown-close"
              onclick={() => (_breakdown.open = false)}
              aria-label="Close breakdown">✕</button>
      <NavBreakdown activeSlot={_breakdown.slot} expiryByAcct={_expiryProfitByAcct} />
    </div>
  </div>
{/if}
```

### 5 — Update CSS (lines 1178-1202)

Remove `top: calc(3rem + 1px + 1.5rem)` and `right: 0` from `.ps-breakdown-panel`.
`left` and `top` now come from the inline `style=` on the element.

---

## Implementation — Part 2: Broker-layer GTT guard

### A. Add `validate_gtt_exchange` to broker ABC (`backend/brokers/broker_apis.py`)

Find the Broker ABC (or base class). Add:
```python
def validate_gtt_exchange(self, exchange: str) -> None:
    """Raise ValueError if this broker does not support GTT on `exchange`.
    Default: all exchanges supported. Override in adapters that have gaps."""
```

### B. Override in `backend/brokers/adapters/dhan.py`

```python
_GTT_UNSUPPORTED_EXCHANGES = frozenset({"MCX", "NCO"})

def validate_gtt_exchange(self, exchange: str) -> None:
    if exchange in _GTT_UNSUPPORTED_EXCHANGES:
        raise ValueError(
            f"Dhan Forever Order does not support GTT on {exchange} — "
            "use a Kite-mirrored account for MCX/NCO templates"
        )
```

Remove or delegate the duplicate inline guard at `dhan.py:1390-1395` (keep for belt-and-suspenders
if preferred, or remove since `validate_gtt_exchange` now fires earlier).

### C. Override in `backend/brokers/adapters/groww.py`

```python
def validate_gtt_exchange(self, exchange: str) -> None:
    if exchange in {"MCX", "NCO"}:
        raise ValueError(
            f"Groww Smart Order GTT is not supported on {exchange}"
        )
```

### D. Call from `apply_plan_live` (`template_attach.py:1407`, before G1 guard)

```python
# Broker-layer exchange validation — fail fast before any broker call.
try:
    broker.validate_gtt_exchange(plan.parent_exchange)
except ValueError as _ve:
    result.errors.append(str(_ve))
    return result
```

---

## Implementation — Part 3: Fail-fast in `apply_template_to_order`

### E. After `caps` resolution (line 1924), add MCX pre-rejection block

```python
# Pre-attach MCX capability guard — reject before plan resolution so
# lot-size / premium-scan work is not wasted on an unsupported path.
if caps is not None and not caps.gtt_supports_mcx and parent_exchange in ("MCX", "NCO"):
    _err = (
        f"{caps.display_name} does not support GTT on {parent_exchange} — "
        "template attach skipped; use a Kite account for MCX/NCO templates"
    )
    logger.warning("[TEMPLATE-GUARD] %s", _err)
    _mcx_plan = TemplatePlan(
        template_id=template.get("id"),
        template_name=template.get("name") or "(unnamed)",
        template_slug=template.get("slug"),
        parent_account=parent_account, parent_symbol=parent_symbol,
        parent_side=parent_side, parent_qty=parent_qty,
        parent_exchange=parent_exchange, parent_fill_price=float(parent_fill_price),
        parent_lot_size=1,
    )
    _mcx_result = AttachResult(plan=_mcx_plan)
    _mcx_result.errors.append(_err)
    _fire_attach_fail_alert(
        order_id=parent_order_id, symbol=parent_symbol,
        account=parent_account, errors=[_err],
    )
    _mcx_result.guard_alert_fired = True
    return _mcx_result
```

### F. Off-hours GTT-only note (line 1985, after the wing-guard block)

At the point where code says `# GTT-only template — no wing, proceed off-hours`, add:
```python
plan_notes_pending.append(
    f"GTT registered off-hours ({parent_exchange} closed) — "
    "will activate at next session open"
)
```
Append this to `plan.notes` after `resolve_template_plan` returns. Use a module-level list or
pass via `_extra_notes` kwarg so the note survives into the returned `AttachResult`.

*(Implementation detail: simplest approach is a local `_offhours_note` string variable set
in the C1 block, appended to `plan.notes` after `resolve_template_plan` at line 2001.)*

---

## After-market edge cases addressed

| Scenario | Before | After |
|----------|--------|-------|
| MCX+Dhan template on fill | Fails at `dhan.py:place_gtt` after lot-size + plan resolution | Fails at `apply_template_to_order` before plan resolution; alert fired |
| MCX+Groww template on fill | Silently fails (no guard in groww.py) | Same fail-fast path |
| GTT-only template after NSE close | Silently proceeds | Proceeds + attaches note "will activate at session open" |
| Wing template after NSE close | C1 blocks, alert fires | Unchanged (already correct) |
| GTT-only template after MCX close (Kite) | Silently proceeds | Proceeds + note |
| MCX wing template after MCX close (Kite) | C1 blocks correctly | Unchanged |
| Groww OCO split: leg1 ok, market closes before leg2 | Groww rolls back leg1 (`_place_oco_emulated:1147`) | Unchanged (already correct) |

---

## Commit message
feat(template+navstrip): fail-fast MCX/Dhan GTT guard + broker validate_gtt_exchange + navstrip label popup

## Done when
- Clicking any P/M/C/H label/value shows NavBreakdown as popup below the element
- MCX fill on a Dhan account → `apply_template_to_order` returns error before plan resolution; alert fires
- MCX fill on a Groww account → same
- NSE fill on any broker → template attaches normally
- GTT-only template attached off-hours → `AttachResult.notes` contains the off-hours note
- `validate_gtt_exchange` in broker ABC; overridden in Dhan + Groww; called at top of `apply_plan_live`
- pytest green (new `test_gtt_broker_guard.py` + existing tests pass)
- svelte-check 0 errors
