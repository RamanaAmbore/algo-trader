# Plan: Conn tab unification + LogPanel account dropdown restore

## Context

Two tasks:

**A — Account dropdown restore (correction)**
The previous session removed all dropdowns from LogPanel. The user wants the account dropdown back — but this time it must scroll *with* the tabs on mobile instead of sitting outside the tab strip's scroll container. The level-filter dropdown stays removed.

**B — Conn tab unification**
The LogPanel Conn tab today shows raw `conn_service.log` text lines (via `GET /api/admin/logs/conn`). The admin/brokers page has a structured Connection Log that shows the same events as a table: time, account, broker_id, event_type, detail (from `GET /api/admin/broker-connection-events`, DB-backed). Goal: upgrade the Conn tab to use the structured endpoint so it shows rich fields as compact single rows — paving the way to retire the Connection Log panel later.

---

## Task A — Restore account dropdown with proper scroll

**Root cause of original scroll problem**: `AlgoTabs` renders `.algo-tabs-strip` with `overflow-x: auto; max-width: 100%`. That makes it fill `ch-middle` and scroll internally. Anything placed *after* it in `ch-middle` is pushed off-screen without being part of the scroll.

**Fix**: Override `.algo-tabs-strip` inside LogPanel's header so it expands naturally instead of clipping internally. `ch-middle` (already `overflow-x: auto`) becomes the single scroll container for tabs + dropdown combined.

### File: `frontend/src/lib/LogPanel.svelte`

**Script changes:**
- Add: `import ActivityAccountSelect from '$lib/ActivityAccountSelect.svelte';`  
  (replaces the removed `ActivityHeaderFilters`; `_showLevelFilter` stays gone)
- Restore: `const _showAccountFilter = $derived(['order', 'agent', 'system', 'conn'].includes(logTab));`

**Template — `{#snippet middle()}`** (currently only `<AlgoTabs>`):
```svelte
{#snippet middle()}
  <AlgoTabs
    tabs={VISIBLE_TABS.map(([id, lbl]) => ({ id, label: lbl }))}
    bind:value={logTab}
    onChange={onTabChange}
    compact={true}
  />
  {#if _showAccountFilter && !hideInlineAccountFilter}
    <ActivityAccountSelect
      bind:value={_internalAccountFilter}
      availableAccounts={_availableAccounts} />
  {/if}
{/snippet}
```

**CSS — add in `<style>`:**
```css
/* Override AlgoTabs' self-scroll so ch-middle scrolls tabs+dropdown together.
   Scoped to lp-header-wrap so other AlgoTabs usages are unaffected. */
:global(.lp-header-wrap .algo-tabs-strip) {
  overflow-x: visible;
  max-width: none;
  flex-shrink: 0;
}
```

The account select already renders with `flex-shrink: 0` in `ActivityAccountSelect.svelte`. No change needed there.

---

## Task B — Conn tab: switch to structured events

### Data source change

| | Before | After |
|---|---|---|
| State | `connLog: string[]` | `connEvents: any[]` |
| Fetch fn | `fetchAdminConnLogs(200)` | `fetchBrokerConnectionEvents({ limit: 200 })` |
| Endpoint | `GET /api/admin/logs/conn` | `GET /api/admin/broker-connection-events` |
| Shape | Raw text lines | `{id, account, broker_id, event_type, event_ts, detail}` |

`fetchBrokerConnectionEvents` already exists in `frontend/src/lib/api.js` — no new API function needed.

### File: `frontend/src/lib/LogPanel.svelte`

**Script changes:**

1. Import — add `fetchBrokerConnectionEvents`, remove `fetchAdminConnLogs`:
   ```js
   import { ..., fetchBrokerConnectionEvents } from '$lib/api';
   // remove fetchAdminConnLogs
   ```

2. State — replace `connLog`:
   ```js
   let connEvents = $state(/** @type {any[]} */ ([]));
   ```

3. Severity helper (same logic as `_connEventCls` in brokers/+page.svelte:520):
   ```js
   function _connEvtCls(evType) {
     if (['login_ok','token_ok','fetch_ok_recovery','circuit_close'].includes(evType)) return 'conn-ev-green';
     if (['login_fail','auth_fail','circuit_open','rotation_detected'].includes(evType)) return 'conn-ev-red';
     if (['rate_limited','token_expiry'].includes(evType)) return 'conn-ev-amber';
     return 'conn-ev-muted';
   }
   ```

4. Detail formatter (same logic as `_fmtConnDetail` added to brokers/+page.svelte today):
   ```js
   function _fmtConnDetail(detail) {
     if (detail == null) return '';
     if (typeof detail === 'string') return detail;
     if (typeof detail === 'object')
       return Object.entries(detail).map(([k, v]) => `${k}: ${v}`).join(' · ');
     return String(detail);
   }
   ```

5. Derived row array (replaces `_connRows`):
   ```js
   const _connEventRows = $derived.by(() => {
     return connEvents.slice()
       .filter(e => {
         if (orderAccountFilter.length > 0 && !orderAccountFilter.includes(e.account)) return false;
         if (levelFilter === 'error')   return _connEvtCls(e.event_type) === 'conn-ev-red';
         if (levelFilter === 'warning') return _connEvtCls(e.event_type) === 'conn-ev-amber';
         if (levelFilter === 'info')    return ['conn-ev-green','conn-ev-muted'].includes(_connEvtCls(e.event_type));
         return true;
       })
       .sort((a, b) => _tsKey(b.event_ts) - _tsKey(a.event_ts));
   });
   ```

6. Loader — replace `_loadConn()`:
   ```js
   async function _loadConn() {
     try {
       const d = await fetchBrokerConnectionEvents({ limit: 200 });
       connEvents = d?.events ?? [];
     } catch (_) { /* keep last-good */ }
   }
   ```

7. Time formatter for `event_ts` (ISO-8601 UTC → IST display):
   ```js
   function _fmtConnEvtTime(iso) {
     if (!iso) return '—';
     try {
       return new Date(iso).toLocaleTimeString('en-IN', {
         timeZone: 'Asia/Kolkata',
         hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
       }) + ' IST';
     } catch (_) { return iso; }
   }
   ```

8. Download CSV for conn tab — update the `_downloadCsv` branch for `logTab === 'conn'` to use `connEventRows` fields (account, broker_id, event_type, event_ts, detail) instead of raw lines.

**Template — replace conn tab block** (currently `{:else if logTab === 'conn'}`):
```svelte
{:else if logTab === 'conn'}
  {#if _connEventRows.length}
    {#each _connEventRows as ev (ev.id ?? ev.event_ts + ev.account + ev.event_type)}
      {@const _cls = _connEvtCls(ev.event_type)}
      {@const _det = _fmtConnDetail(ev.detail)}
      {@const _stripe = /* alternating tint via index */ ''}
      <div class="lp-conn-row {_cls}">
        <span class="lp-conn-time">{_fmtConnEvtTime(ev.event_ts)}</span>
        <span class="lp-conn-acct font-mono">{ev.account || '—'}</span>
        <span class="lp-conn-broker">{ev.broker_id || '—'}</span>
        <span class="lp-conn-type">{ev.event_type}</span>
        {#if _det}
          <span class="lp-conn-det" title={JSON.stringify(ev.detail, null, 2)}>{_det}</span>
        {/if}
      </div>
    {/each}
  {:else}
    <div class="log-row log-debug"><span class="log-row-msg">No connection events yet.</span></div>
  {/if}
```

**CSS — add in `<style>`:**
```css
.lp-conn-row {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.1rem 0.5rem;
  font-size: var(--fs-sm);
  white-space: nowrap;
  overflow: hidden;
}
.lp-conn-row:nth-child(even) { background: var(--row-tint-even, rgba(255,255,255,0.02)); }
.lp-conn-time   { flex-shrink: 0; color: var(--c-info); font-size: var(--fs-xs, 0.6rem); }
.lp-conn-acct   { flex-shrink: 0; min-width: 5rem; color: var(--algo-slate); }
.lp-conn-broker { flex-shrink: 0; min-width: 3rem; color: var(--text-muted); }
.lp-conn-type   { flex-shrink: 0; min-width: 8rem; font-weight: 500; }
.lp-conn-det    { flex: 1 1 0; min-width: 0; overflow: hidden; text-overflow: ellipsis; color: var(--algo-muted); }
/* severity colours — reuse brokers page tokens */
.lp-conn-row.conn-ev-green .lp-conn-type { color: var(--c-long); }
.lp-conn-row.conn-ev-red   .lp-conn-type { color: var(--c-short); }
.lp-conn-row.conn-ev-amber .lp-conn-type { color: var(--c-action); }
.lp-conn-row.conn-ev-muted .lp-conn-type { color: var(--algo-muted); }
```

### Files changed
- `frontend/src/lib/LogPanel.svelte` — both tasks

### No changes needed
- `frontend/src/lib/api.js` — `fetchBrokerConnectionEvents` already exists
- `frontend/src/lib/ActivityAccountSelect.svelte` — used as-is
- Backend routes — both endpoints already exist

---

---

## Task C — Losers / Gainers: broker-fail snapshot fallback + tests

### Root cause

`GET /api/watchlist/movers` in `backend/api/routes/watchlist.py` has two paths:
- **Market closed** → `_movers_offhours_response(ist_today)` (reads `movers_snapshots` DB table) ✓
- **Market open** → live broker quotes via `_movers_fetch_quotes_cached()` → if broker fails returns **empty list** ✗

When the broker (`get_market_data_broker()`) fails during market hours, `quote_data` comes back as `{}`, the route logs `[MOVERS-EMPTY] reason=broker_quote_empty` at `INFO` and returns `movers: []`. Frontend shows blank gainers/losers grids.

### Fix — `backend/api/routes/watchlist.py`

In the live-path handler (~line 2114), after the empty-quote guard, add snapshot fallback:

```python
# before (logs INFO, falls through to empty response):
if not quote_data and key_to_meta:
    logger.info(f"[MOVERS-EMPTY] reason=broker_quote_empty ...")

# after (falls back to snapshot on broker failure):
if not quote_data and key_to_meta:
    logger.warning(f"[MOVERS-EMPTY] reason=broker_quote_empty — snapshot fallback")
    return await _movers_offhours_response(ist_today)
```

One-line logic change + severity bump. `_movers_offhours_response` already reads the `movers_snapshots` table and returns the last captured snapshot with a `captured_at` timestamp.

### Tests — `backend/tests/test_movers_route.py` (new file)

Five pytest cases covering the snapshot gate behaviour for both gainers and losers:

| Test | Setup | Expected |
|---|---|---|
| `test_movers_live_market_hours_ok` | Market open, broker returns quotes | `movers` list non-empty, `captured_at` is None |
| `test_movers_broker_fail_market_hours_returns_snapshot` | Market open, broker raises / returns `{}` | Falls back to snapshot; `captured_at` non-null |
| `test_movers_offhours_returns_snapshot` | Market closed | `_movers_offhours_response` called; `captured_at` non-null |
| `test_movers_snapshot_contains_gainers` | Snapshot row with positive `change_pct` exists | At least one row with `change_pct > 0` in response |
| `test_movers_snapshot_contains_losers` | Snapshot row with negative `change_pct` exists | At least one row with `change_pct < 0` in response |

All tests patch `_any_segment_open` (market state) and `_movers_fetch_quotes_cached` (broker), and seed the `movers_snapshots` table with fixture rows to cover both gainers and losers.

---

## Agents
- frontend: implement Task A and Task B in `frontend/src/lib/LogPanel.svelte`
- backend: implement Task C fix in `backend/api/routes/watchlist.py`
- broker: skip
- doc: skip
- backend-test: write `backend/tests/test_movers_route.py` for Task C
- playwright: skip

## Tests
- pytest: yes (new test_movers_route.py — 5 cases)
- svelte-check: yes
- playwright: no

## Commit message
feat(ui,backend): conn tab structured events, account dropdown scroll fix, movers snapshot fallback

## Done when
- Account dropdown scrolls with tabs on mobile (single scroll container, no nested overflow)
- Account dropdown shows per-tab same as before (hidden on terminal/ticks/news/simulator)
- LogPanel Conn tab shows time · account · broker · event_type · detail as compact single rows
- Severity colours on conn event_type: green/red/amber/muted
- Account filter works on structured conn events
- Movers route: broker failure during market hours falls back to last snapshot (not empty list)
- `test_movers_route.py`: all 5 cases green (market hours OK, broker fail fallback, off-hours, gainers, losers)
- svelte-check: 0 errors
