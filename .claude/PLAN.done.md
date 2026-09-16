# Plan: Fix stale MCX/NSE spot prices + loss alerts + DB/SQL/Telegram bugs

## Task

Six independent bugs (4 backend-only, 1 broker-layer, 1 Telegram):

1. **Stale MCX/NSE spot prices on all surfaces** (HIGHEST PRIORITY) — MCX commodity spot (GOLDM, CRUDEOIL) and NSE index spot (NIFTY, BANKNIFTY) in the derivatives snapshot card update only every 30s via batchQuote. Root causes: (a) `_add_nfo_spot_anchors` subscribes bare `("NIFTY", "NSE")` but Kite's tradingsymbol is `"NIFTY 50"` → token lookup fails, never subscribed to KiteTicker. (b) Even when MCX futures are subscribed correctly (CRUDEOIL26OCTFUT), SSE ticks carry `sym="CRUDEOIL26OCTFUT"` while all frontend surfaces read by root key `"CRUDEOIL"` — the tick bus Path 3 matching is fragile and depends on `instrumentsReady`. **Fix: backend virtual-root aliasing** — KiteTicker emits BOTH the real futures sym AND the virtual root sym in the SSE bus, so `mergeSymbolUpdate("CRUDEOIL", ltp)` lands in symbolStore at tick cadence. All surfaces that read `getSnapshot("CRUDEOIL")` or `patchUnderlyingSpot("CRUDEOIL", ltp)` then work at SSE rate with no frontend changes.

2. **Loss agents check wrong P&L metric** — they evaluate `pnl` (total unrealized from avg entry price), not `day_change_val` (today's session loss from prev_close). For options that bought cheap and gained then crashed today, `pnl` can be positive while NavStrip shows −₹10L. Fix: add `day_val` conditions to both loss agents + support the metric in the engine extractor.

3. **`gtt_order_id` column missing from DB** — `_fetch_gtt_set` in `positions.py` queries `algo_orders WHERE gtt_order_id IS NOT NULL` but column doesn't exist. Fix: migration + ORM field.

4. **`_fetch_ref_close_map` SQL syntax error** — `(account, symbol) IN :pairs` tuple binding fails asyncpg. Fix: replace with `UNNEST(:accts::text[], :syms::text[])`.

5. **Telegram HTML parse error** — `P&L` in `<code>` block breaks Telegram's HTML parser. Fix: `html.escape(tg_table)`.

## Agents

### broker: kite_ticker.py — virtual root aliasing

File: `backend/brokers/kite_ticker.py`

Add to `TickerManager.__init__` (after `self._sym_to_token`):
```python
self._virtual_root_aliases: dict[int, str] = {}  # token → virtual root sym (e.g. 58312711 → "CRUDEOIL")
```

Add new methods (near `has_sym` / `get_ltp_by_sym`):
```python
def set_virtual_root_alias(self, token: int, root: str) -> None:
    """Register a virtual root alias: when token ticks, also emit {sym: root} to the SSE bus."""
    with self._lock:
        self._virtual_root_aliases[int(token)] = root

def get_token_for_sym(self, sym: str) -> int | None:
    """Return the subscribed token for a tradingsymbol, or None if not subscribed."""
    with self._lock:
        return self._sym_to_token.get(str(sym or '').upper())
```

In `_on_ticks`, inside the main `for t in ticks:` loop, after appending to `to_publish`, also append a virtual root payload when an alias exists (still inside the lock):
```python
to_publish.append({
    "tok": tok,
    "sym": self._token_to_sym.get(tok, ""),
    "ltp": lp_f,
    "ts":  ts,
})
# Virtual root alias — also emit root sym so SSE clients
# can read getSnapshot("CRUDEOIL") without futures-sym mapping.
vr = self._virtual_root_aliases.get(tok)
if vr:
    to_publish.append({"tok": tok, "sym": vr, "ltp": lp_f, "ts": ts})
```

In `snapshot()`, after the main dict comprehension, add virtual root entries so new SSE connections are primed immediately:
```python
with self._lock:
    result = {
        tok: {"ltp": lp, "sym": self._token_to_sym.get(tok, "")}
        for tok, lp in self._tick_map.items()
        if isinstance(lp, (int, float)) and lp > 0
    }
    for tok, vr in self._virtual_root_aliases.items():
        lp = self._tick_map.get(tok)
        if isinstance(lp, (int, float)) and lp > 0:
            result[f"vr_{tok}"] = {"ltp": lp, "sym": vr}
    return result
```
Note: `f"vr_{tok}"` is a string key in the snapshot dict — the frontend `_onSnapshot` iterates `.values()` and uses `v.sym`, so the key type doesn't matter.

Also clear `_virtual_root_aliases` when tokens are reset on account failover (same places where `_sym_to_token.clear()` is called at lines ~947 and ~1002):
```python
self._virtual_root_aliases.clear()
```

### backend: background.py + agent_engine.py + positions.py + database.py + models.py + alert_utils.py

**Fix 1b (NFO spot anchors + alias registration — `background.py`)**:
Change `_add_nfo_spot_anchors(book_pairs: list)` signature to return a `dict[str, str]` of `{sym_upper → root}` alias pairs:
```python
def _add_nfo_spot_anchors(book_pairs: list) -> dict[str, str]:
    """Returns {tradingsymbol.upper(): root} for syms that differ from root (need virtual alias)."""
    from backend.api.algo.derivatives import underlying_ltp_key as _ult_key
    aliases: dict[str, str] = {}
    try:
        already = {s.upper() for s, _ in book_pairs}
        nfo_roots = {
            m.group(1)
            for sym, exch in book_pairs
            if exch in ('NFO', 'BFO') and sym and _re_module.search(r'(CE|PE)$', sym)
            for m in [_re_module.match(r'^([A-Z]+)', sym)]
            if m
        }
        for root in nfo_roots:
            ltp_key = _ult_key(root)
            if ':' not in ltp_key:
                continue
            exch_part, sym_part = ltp_key.split(':', 1)
            if sym_part.upper() not in already:
                book_pairs.append((sym_part, exch_part))
            if sym_part.upper() != root.upper():
                aliases[sym_part.upper()] = root  # e.g. "NIFTY 50" → "NIFTY"
    except Exception as _e:
        logger.debug(f"Background: NFO/BFO spot-anchor subscribe skipped: {_e}")
    return aliases
```

Change `_add_mcx_spot_anchors` to return a `dict[str, str]` of MCX alias pairs:
```python
async def _add_mcx_spot_anchors(book_pairs, book_seen) -> dict[str, str]:
    aliases: dict[str, str] = {}
    try:
        ...
        for root in mcx_roots:
            futs = await _laf(root, 'MCX', limit=1)
            if futs:
                key = (futs[0], 'MCX')
                if key not in book_seen:
                    book_seen.add(key)
                    book_pairs.append(key)
                aliases[futs[0].upper()] = root  # e.g. "CRUDEOIL26OCTFUT" → "CRUDEOIL"
    ...
    return aliases
```

In `_perf_subscribe_book_symbols`, after `_ticker.subscribe_with_sym(_all_batch)`, register the virtual root aliases:
```python
mcx_aliases = await _add_mcx_spot_anchors(_book_pairs, _book_seen)
nfo_aliases = _add_nfo_spot_anchors(_book_pairs)
...
if _all_batch:
    _ticker.subscribe_with_sym(_all_batch)
# Register virtual root aliases so SSE emits root sym alongside futures sym.
all_aliases = {**mcx_aliases, **nfo_aliases}
for sym_upper, root in all_aliases.items():
    tok = _ticker.get_token_for_sym(sym_upper)
    if tok is not None:
        _ticker.set_virtual_root_alias(tok, root)
```

**Fix 2 (day_val metric — `agent_engine.py`)**: In `_v2_extract_pnl_fields()` (~line 383), inside `elif section == 'Positions'`:
```python
if metric == 'day_val':
    pnl = float(row.get('day_change_val', 0) or 0)
else:
    pnl = float(row.get('pnl', 0) or 0)
```
In `loss-positions-acct` conditions, add: `{"metric": "day_val", "scope": "positions.any_acct", "op": "<=", "value": -30000}`.
In `loss-positions-total` conditions, add: `{"metric": "day_val", "scope": "positions.total", "op": "<=", "value": -50000}`.

**Fix 3 (gtt_order_id — `database.py` + `models.py`)**: Add `_migrate_algo_orders_gtt_order_id()` function: `ALTER TABLE algo_orders ADD COLUMN IF NOT EXISTS gtt_order_id VARCHAR(64)`. Register in `init_db()`. In `AlgoOrder`, add `gtt_order_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)`.

**Fix 4 (UNNEST — `positions.py`)**: In `_fetch_ref_close_map()`, replace `IN :pairs` tuple binding with `IN (SELECT a, s FROM UNNEST(:accts::text[], :syms::text[]) AS t(a, s))` and split `closed_pairs` into `accts`/`syms` lists.

**Fix 5 (Telegram HTML — `alert_utils.py`)**: Add `import html`. Wrap `tg_table` in `html.escape()`: `f"<code>{html.escape(tg_table)}</code>"`.

### frontend: skip

No frontend changes needed. The derivatives page tick bus Path 1 already fires when `sym == root` (e.g., "CRUDEOIL") and calls `patchUnderlyingSpot("CRUDEOIL", ltp)`, which updates `_underlyingQuotes` reactively. Once the backend emits virtual root ticks, all surfaces work automatically.

### doc: skip

### backend-test: pytest for all fixes

- `test_kite_ticker.py` (or broker-layer): test `set_virtual_root_alias` + `get_token_for_sym`; assert that when `_on_ticks` fires for a token with an alias, `_bus.publish` is called twice (once with futures sym, once with root sym). Assert `snapshot()` includes `f"vr_{tok}"` entry with correct root sym.
- `test_background.py`: test `_add_nfo_spot_anchors` returns correct aliases — NIFTY root → ("NIFTY 50", "NSE") in book_pairs + alias {"NIFTY 50" → "NIFTY"}; SENSEX → ("SENSEX", "BSE") + no alias (sym == root); RELIANCE → ("RELIANCE", "NSE") + no alias. Test `_add_mcx_spot_anchors` mock returns futures sym + alias {futures.upper() → root}.
- `test_alert_routing.py`: assert loss agents contain `day_val` conditions. Add `TestDayValExtraction` for `_v2_extract_pnl_fields`.
- `test_positions_route.py`: test UNNEST params for `_fetch_ref_close_map`. Test `_fetch_gtt_set` mock.
- `test_alert_utils.py`: assert Telegram body has `&amp;` not raw `&`.

### playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(ticker,alerts,positions): virtual root SSE aliases for live MCX/NSE spot + NFO anchor tradingsymbol fix + day_val loss metric + gtt migration + UNNEST SQL + Telegram HTML escape

## Done when

- SSE tick bus emits `{sym: "CRUDEOIL", ltp: ...}` alongside `{sym: "CRUDEOIL26OCTFUT", ltp: ...}` for MCX futures ticks
- SSE tick bus emits `{sym: "NIFTY", ltp: ...}` alongside `{sym: "NIFTY 50", ltp: ...}` for NSE index ticks
- Snapshot card GOLDM/CRUDEOIL spot updates at 250ms cadence (same as selected underlying), not 30s
- `loss-positions-acct` and `loss-positions-total` have `day_val` condition leaves
- No `gtt_order_id` column-not-found warnings
- No `_fetch_ref_close_map` SQL syntax errors
- No Telegram HTML parse errors for market alerts
- pytest green
