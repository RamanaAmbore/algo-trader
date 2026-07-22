# Plan: NavStrip font + click breakdown + movers snapshot tests

## Context

Three tasks:

**A — NavStrip label font slightly bigger**  
Labels (P, M, C, H pills) currently use `var(--fs-sm)`. Operator wants slightly larger.

**B — NavStrip values clickable → per-account breakdown panel**  
Clicking any NavStrip value (any slot in P/M/C/H pills) should open a compact panel showing per-account breakdown of all values, UX similar to the broker health chip on the navbar (click element → small panel appears below → Escape/click-outside closes).

`NavBreakdown.svelte` already exists (468 lines, used by dashboard NAV tab) and shows per-account: Cash | Pos M2M | Holdings | NAV. Reuse it inside an overlay panel rather than building a new component.

**C — Movers tests: use real snapshot payload structure**  
The new `test_movers_route.py` mocks `_load_latest_movers_snapshot` with a Python object. The gainers/losers tests should instead build a realistic `MoversSnapshot` with actual `payload_json` content and let `_movers_offhours_response` deserialize it — confirming the real JSON→MoverRow pipeline works, not just that the mock fires. The conftest patches `init_db` to a noop so a real DB table won't exist; use `patch("backend.api.routes.watchlist.async_session")` to inject a fake session that returns the fixture snapshot row.

---

## Task A — NavStrip label font size

**File:** `frontend/src/lib/PositionStrip.svelte`

Find `.ps-agg-k` CSS rule:
```css
/* current */
.ps-agg-k { font-size: var(--fs-sm); ... }

/* change to */
.ps-agg-k { font-size: var(--fs-md); ... }
```

Also update the mobile override at `@media (max-width: 640px)` — bump from `var(--fs-xs)` to `var(--fs-sm)` so the relative increase carries through to mobile.

---

## Task B — NavStrip click → per-account breakdown

**File:** `frontend/src/lib/PositionStrip.svelte`

### State
```js
import NavBreakdown from '$lib/NavBreakdown.svelte';

let _breakdownOpen = $state(false);
```

### Make all pills clickable
Each pill label/group (P, M, C, H) gets `onclick={() => _breakdownOpen = true}` plus `role="button"` + `tabindex="0"` + `onkeydown={(e) => e.key === 'Enter' && (_breakdownOpen = true)`. Apply to the outermost `<span class="ps-agg">` for each pill, or to the label `<span class="ps-agg-k">` — whichever is cleaner given the existing template.

The **P pill's Day P&L slot** already has `onclick={() => _dayPnlBreakupOpen = true}` (the DayPnlBreakup modal). Keep that separate behavior: clicking the Day P&L value still opens DayPnlBreakup; clicking the P label (ps-agg-k) opens the new per-account breakdown panel. Treat the two as coexisting.

### Overlay panel
Below the existing template (before `{#if _dayPnlBreakupOpen}...`), add:

```svelte
{#if _breakdownOpen}
  <!-- svelte-ignore a11y_no_noninteractive_element_interactions -->
  <div class="ps-breakdown-overlay" role="presentation"
       onclick={() => _breakdownOpen = false}
       onkeydown={(e) => e.key === 'Escape' && (_breakdownOpen = false)}>
    <div class="ps-breakdown-panel" onclick={(e) => e.stopPropagation()}>
      <button type="button" class="ps-breakdown-close"
              onclick={() => _breakdownOpen = false}
              aria-label="Close breakdown">✕</button>
      <NavBreakdown />
    </div>
  </div>
{/if}
```

### CSS for overlay + panel
```css
.ps-breakdown-overlay {
  position: fixed;
  inset: 0;
  z-index: 200;
  background: transparent;
}
.ps-breakdown-panel {
  position: fixed;
  top: var(--navstrip-height, 2.5rem);  /* below the navstrip */
  right: 0;
  width: min(28rem, 100vw);
  max-height: 70vh;
  overflow-y: auto;
  background: var(--card-bg, #0f1a2e);
  border: 1px solid var(--border-color, rgba(255,255,255,0.08));
  border-radius: 4px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.5);
  z-index: 201;
  padding: 0.5rem;
}
.ps-breakdown-close {
  position: absolute;
  top: 0.4rem;
  right: 0.5rem;
  background: none;
  border: none;
  color: var(--algo-cyan, #22d3ee);
  cursor: pointer;
  font-size: var(--fs-md);
  line-height: 1;
}
/* Cursor on pills to signal clickability */
.ps-agg {
  cursor: pointer;
}
```

If `NavBreakdown` requires props (positions, holdings, funds arrays), pass them through — they are already in scope in PositionStrip's script.

---

## Task C — Movers tests: realistic snapshot payload

**File:** `backend/tests/test_movers_route.py`

Update `test_movers_snapshot_contains_gainers` and `test_movers_snapshot_contains_losers`:

Instead of mocking `_load_latest_movers_snapshot` to return a Python object directly, build a `MoversSnapshot` row with real `payload_json` content (a JSON-serialised list of `MoverRow`-shaped dicts) and inject it via a patched `async_session`. Then do NOT mock `_movers_offhours_response` — let it run the real deserialization path.

```python
import json
from backend.api.models import MoversSnapshot
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import date, datetime, timezone

def _make_snapshot(rows: list[dict]) -> MoversSnapshot:
    snap = MoversSnapshot()
    snap.id = 1
    snap.date = date.today()
    snap.payload_json = json.dumps(rows)
    snap.captured_at = datetime.now(tz=timezone.utc)
    return snap

async def test_movers_snapshot_contains_gainers(async_client):
    gainer_row = {
        "tradingsymbol": "RELIANCE", "exchange": "NSE",
        "last_price": 2500.0, "previous_close": 2400.0,
        "change_pct": 4.17, "peak_pct": 4.17,
        "sticky": False, "price_source": "snapshot",
        "is_animating": False, "quote_symbol": None,
    }
    snap = _make_snapshot([gainer_row])

    # Patch async_session so _load_latest_movers_snapshot gets our fixture row
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = snap
    mock_session = AsyncMock()
    mock_session.__aenter__.return_value.execute = AsyncMock(return_value=mock_result)

    with patch('backend.api.routes.watchlist._movers_probe_market_state', return_value=(False, False)), \
         patch('backend.api.routes.watchlist.async_session', return_value=mock_session), \
         patch('backend.api.auth_guard.is_authenticated_request', return_value=True), \
         patch('backend.api.auth_guard.jwt_guard', new_callable=AsyncMock):
        response = await async_client.get("/api/watchlist/movers")
        assert response.status_code == 200
        movers = response.json()["movers"]
        assert any(m["change_pct"] > 0 for m in movers), "Expected at least one gainer"
        assert response.json()["captured_at"] is not None

async def test_movers_snapshot_contains_losers(async_client):
    loser_row = {
        "tradingsymbol": "HDFC", "exchange": "NSE",
        "last_price": 1600.0, "previous_close": 1700.0,
        "change_pct": -5.88, "peak_pct": -5.88,
        "sticky": False, "price_source": "snapshot",
        "is_animating": False, "quote_symbol": None,
    }
    snap = _make_snapshot([loser_row])
    # ... same mock_session / patching pattern as above ...
    assert any(m["change_pct"] < 0 for m in movers), "Expected at least one loser"
```

This tests the actual JSON deserialization in `_movers_offhours_response` not just the mock return.

---

## Agents
- frontend: Task A (font) + Task B (click/panel) in `frontend/src/lib/PositionStrip.svelte`
- backend: skip
- broker: skip
- doc: skip
- backend-test: Task C — update `backend/tests/test_movers_route.py` gainers/losers tests
- playwright: skip

## Tests
- pytest: yes (test_movers_route.py)
- svelte-check: yes
- playwright: no

## Commit message
feat(ui,test): navstrip font bump, click-to-breakdown panel, movers snapshot payload test

## Done when
- NavStrip P/M/C/H pill labels are visibly larger on both desktop and mobile
- Clicking any NavStrip pill opens a per-account breakdown panel (NavBreakdown) anchored below the strip; Escape and click-outside close it
- Day P&L breakup (DayPnlBreakup modal on P value) still works independently
- Movers gainers/losers tests use real payload_json deserialization, not mocked return objects
- pytest green, svelte-check 0 errors
