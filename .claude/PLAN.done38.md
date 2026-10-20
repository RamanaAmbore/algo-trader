# Plan: Fix order-placement market-hours gate (`_ae_segment_flags`) + doc sync

## Task

Live bug: operator placed an MCX order during a genuinely-open MCX evening session
(today, a holiday, `evening_open_on_holidays: true`) and got "Exchange MCX is closed."

Root cause (verified directly + by 6-member council audit, all independently reading
the code): `backend/api/algo/agent_engine.py:_ae_segment_flags()` (line ~2129) is a
separate, uncoordinated market-open mechanism that tonight's earlier "one call path"
unification never touched. It computes open/closed from the segment's flat legacy
`hours_start`/`hours_end` window only — it never reads the `sessions` list,
`evening_open_on_holidays`, or DB `special_sessions` at all. On a calendar-holiday
date its *only* way to ever read "open" is `is_trading_day()`'s live Kite
bellwether-probe fallback (`market_probe.probe_market_active`), which is flaky
(stale ticks, broker API gaps, TTL-cached stale verdict) — a poor substitute for the
deterministic rule that already exists in `is_market_open()`/`_segment_is_open()`
(used correctly by `/api/market/status`, the quote gate, watchlist gate, and the
UI's own `RefreshButton` badge).

This function is not a minor path — via `_symbol_exchange_open()`/`_build_now_ctx()`
it is the **sole** market-open gate for 8 real consumers:

| # | Call site | Purpose |
|---|---|---|
| 1 | `orders_place.py:231-232` | ticket helper |
| 2 | `orders_place.py:1494,1498` | ticket live gate (today's exact failure point) |
| 3 | `orders_basket.py:419-420` | basket per-leg gate |
| 4 | `template_attach.py:324-325` | GTT/wing attach (sync path) |
| 5 | `template_attach.py:2398-2400` | GTT/wing attach (second site) |
| 6 | `chase.py:2552-2559` | chase re-pricing gate |
| 7 | `actions.py:162-165` | agent close-all action gate |
| 8 | `agent_engine.py:3129-3131` (`any_market_open`) | `market_hours`-scheduled agent gate (`run_cycle`) — found by devil council, larger blast radius than order placement alone |

CLAUDE.md currently mischaracterizes this as "Deliberately separate... a live Kite
bellwether probe kept as a safety net for when the calendar verdict is wrong." Devil
council traced the git history: this is wrong — `_ae_segment_flags` predates the
`sessions`/`evening_open_on_holidays`/`special_sessions` design entirely (Phase 23,
May 2026, vs. the sessions design shipped today) and was simply missed by the sweep
that unified the other 6 sites, because that sweep grepped for `is_market_open(`
callers and this function calls `is_trading_day()` directly instead.

**Critical fix-shape finding from risk council** (do not skip this): the naive fix —
make `_ae_segment_flags` call `_segment_is_open()` directly — would silently
introduce a NEW correctness bug. `_segment_is_open()` calls `fetch_special_sessions()`
(`broker_apis.py`), which defers through `run_on_main_loop()` — and that helper's
self-deadlock guard (`write_queue.py:108-112`) returns `default=[]` **without
querying the DB** whenever called from a coroutine already running on the main event
loop. 5 of the 8 call sites (everything except `actions.py`'s, which runs via
`asyncio.to_thread` inside `run_cycle`) call `_build_now_ctx()` synchronously from
exactly that context. So wiring in `special_sessions=` naively would make the
MCX-evening-**closed**-holiday override (the `market_special_sessions` zero-width
rows from tonight's earlier D3 fix) silently never apply to real order placement —
looking fixed while remaining broken for the one case it exists to catch. This is why
the fix below is split into two explicit steps.

Confirmed NOT a bug (verified directly + risk council): `MarketLifecycle`'s
`close_settled`/`daily_book.prev_close` reset is already fully per-exchange
independent (separate `_open_state`/`_last_close_at`/`_settled_fired` dicts for
nse/mcx/cds) — NSE closing does not couple to or affect MCX's reset, and vice versa.
One benign, pre-existing, non-corrupting overlap exists at the snapshot-handler layer
(an `nse:close` event snapshots all of that account's rows including still-open MCX
symbols, but MCX's own later close/close_settled always overwrites it correctly) —
not a bug, no action needed.

## Agents

- **backend**:
  1. Rewrite `_ae_segment_flags()` (`agent_engine.py:2129`) — **Step 1 (ships now,
     zero new risk)**: thread `sessions=seg_cfg.get("sessions")` and
     `evening_open_on_holidays=seg_cfg.get("evening_open_on_holidays", False)` into
     its `is_market_open()` call, replacing the current flat-window +
     `is_trading_day()` logic for the open/closed boolean. Do NOT pass
     `special_sessions=` yet (omit/None) — that's Step 2. Keep the existing
     `fetch_holidays()` call for the `{prefix}_holiday` display flag. Fix
     `minutes_since_{prefix}_open/close` to anchor against the session window that
     actually matched `now` (reuse `_parse_session_windows`'s resolution) instead of
     the blind `hours_start`/`hours_end`, so a 17:00 holiday-evening open doesn't
     report `minutes_since_mcx_open≈540`. Preserve the full returned key shape
     (`{prefix}_open/_closed/_holiday/minutes_since_*}`) — `sim/driver.py` depends on
     these exact keys for scenario overrides (devil + architect council).
  2. **Step 2**: make `fetch_special_sessions()` safe to call from an already-running
     event loop (the 7 call sites above other than `actions.py`). Add an async-safe
     path — either a direct-await variant that bypasses `run_on_main_loop()` when
     already on the loop, or `asyncio.to_thread` at the call site, whichever matches
     how `fetch_holidays()` already handles this correctly (check that function's
     pattern first and mirror it). Once safe, wire `special_sessions=fetch_special_sessions(exch)`
     into Step 1's `is_market_open()` call, completing parity with `_segment_is_open()`.
  3. Harden the 409 rejection message (`orders_place.py:1500-1503`) to state the
     actual rule (evening-session timing) instead of generic "(IST holidays apply)"
     boilerplate — now that the check itself is correct, the message should be too.

- **frontend**:
  1. Wire `OrderTicket.svelte`'s existing `_isOpen` (lines ~1467-1483 — already
     correctly computed from `isMcxOpen()`/`isNseOpen()`, currently dead/unread) into
     a visible per-exchange open/closed badge inside the ticket, so a future
     divergence between the ticket's own belief and the submit-time gate is
     immediately visible rather than only discoverable via a rejected order (UX
     council finding).
  2. Fix the submit-error toast truncation (`OrderTicket.svelte:2408`,
     `.slice(0, 80)`) so a 409 market-hours message's full text — including the
     evening-session explanation — is readable on the first error, not just in the
     expandable detail. Exempt 409s from the 80-char cap, or raise it.

- **doc**: Correct CLAUDE.md's "Market open/close determination — one call path"
  section:
  - Scope the "one call path" claim to display/status/quote/movers routes only — it
    is not true for order placement as currently documented.
  - Rewrite the `_ae_segment_flags` bullet: state plainly it was the real
    order-placement/chase/template-attach/agent-action/schedule gate (not a safety
    net), now fixed via Steps 1-2 above, and list the real 8 consumers.
  - Fix stale line refs: `background.py:5728`→`5776` (`_watchdog_check_market_open`);
    `background.py:3300-3301`→`3377-3378` (`_session_guard()`'s `session_date_of`
    usage).
  - Update "deliberately unreconciled" framing — two mechanisms now
    (`exchange_clock`'s EOD-scheduling signal, documented already; and whatever of
    this fix is deliberately left for a later pass, if anything).
  - `docs/specs/BROKER_SPEC.md` §I12 "Basket per-leg guards": name the real
    mechanism (`_symbol_exchange_open`/`_ae_segment_flags`) explicitly.
  - `docs/guides/ADMIN_GUIDE.md` "Market-status resolution" (~line 1237-1255):
    correct the resolution-order description — code is calendar-first/probe-fallback,
    doc currently says broker-API-first/calendar-last (pre-existing inaccuracy, not
    from tonight, surfaced by this audit).
  - `docs/guides/ADMIN_GUIDE.md` "MCX Evening Session on an NSE Holiday" example
    (~line 1505-1509): fix the example request/response to match the real
    `GET /api/market/status` → `{nse_open, mcx_open, any_open, is_holiday}` shape
    (pre-existing inaccuracy, not from tonight).

- **backend-test**: New tests in `backend/tests/test_market_hours_unification.py` (or
  a new file alongside it):
  1. MCX reads `mcx_open=True` at 18:00 IST on a simulated calendar-holiday date with
     `evening_open_on_holidays=True`, with `probe_market_active` mocked to raise/
     return `None` — asserting the result does not depend on the probe at all (the
     exact live-reported scenario).
  2. `minutes_since_mcx_open` stays sane (not ~540) on that same simulated tick.
  3. Once Step 2 lands: a DB-seeded MCX-evening-closed `market_special_sessions` row
     correctly makes one of the previously-broken async call sites (call
     `_build_now_ctx()` directly, not `_segment_is_open()`) read `mcx_open=False` —
     proving the deadlock-guard gap is actually closed, not just theoretically fixed.

- **playwright**: New/extended e2e spec asserting `OrderTicket.svelte`'s per-exchange
  badge matches `/api/market/status`'s live value (DOM/computed-style check).

## Tests

- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message

```
fix(market-hours): order-placement gate (_ae_segment_flags) honors
evening_open_on_holidays + special_sessions; fix async deadlock-guard gap;
surface OrderTicket's own market-state badge; correct CLAUDE.md/BROKER_SPEC/
ADMIN_GUIDE drift found by 6-agent council audit
```

## Done when

- MCX's evening session on a holiday is correctly read as open by order placement,
  basket, GTT/wing attach, chase, and agent close-actions — without depending on a
  live tick probe.
- `minutes_since_mcx_open`/`minutes_since_mcx_close` stay correct under the
  evening-holiday branch.
- A DB-seeded MCX-evening-**closed**-holiday special session correctly blocks order
  placement from the previously-deadlocked async call sites (proven by test, not just
  asserted).
- CLAUDE.md, BROKER_SPEC.md, ADMIN_GUIDE.md no longer misdescribe this mechanism or
  its resolution order.
- OrderTicket shows a live, correctly-sourced per-exchange badge; a 409 market-hours
  rejection is fully readable on first error.
- All new + existing tests green.

## Risks (operator decision, not resolved here)

- Step 2 (making `fetch_special_sessions()` safe from an already-running event loop)
  is the one piece of real complexity in this plan. Step 1 alone fully closes
  today's reported bug (false-reject on a genuinely-open evening session) with zero
  new risk and can ship independently if preferred. Step 2 closes a second,
  currently-latent gap (a DB-confirmed MCX-evening-**closed** holiday not being
  honored by order placement) that hasn't been reported as a live incident yet.
  Both are included in this one plan; flagging the split in case you'd rather phase
  it.
- This fix can change behavior in **both** directions: it fixes today's false-reject,
  but once Step 2 lands it can also newly **block** an order in a case where the old,
  buggy probe-based code might have let it through (a stale/leaked tick from an
  adjacent contract fooling the probe on a day MCX's evening is genuinely,
  DB-confirmed closed). This is a deliberate correctness improvement, not a
  regression — flagging so it isn't a surprise.
