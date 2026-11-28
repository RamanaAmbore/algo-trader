# Plan: 6D audit of last 3 days (72 commits) — fix all confirmed defects

## Task

Full 6-dimension audit (D1 correctness, D2 performance, D3 dead code, D4 UX, D5 broker
compliance, D6 doc alignment) of all 72 commits from the last 3 days — the expiry
pre-close alert/auto-close redesign, the Kite Console + Dhan historical backfill
feature, the hedge-leg feature, the DST close-time sweep, and tonight's margin/cash/
close-product fixes. Plus a dedicated investigation into a market-close routing race
condition observed in prod logs. Five parallel audits ran; findings below are
deduplicated and severity-ranked. This plan fixes every P1 and P2, plus the cheap P3s;
it defers a few explicitly-flagged lower-value P3s with a reason given for each.

**Most important finding**: the expiry-day auto-close/alert safety mechanism — the
subject of most of tonight's work — is *still* non-functional in prod, for a reason
distinct from the exchange-gate bug already fixed and deployed tonight. `expiry.py`
compares a string expiry date against a `date` object, which is always False, so
`scan_positions()` finds zero positions every time. Confirmed on real prod logs
2026-11-26 and 2026-11-27 (today).

## Agents

### backend — P1 fixes (real-money / safety, highest priority)

1. **`backend/api/algo/expiry.py:566,833` — expiry-day filter never matches (CRITICAL).**
   `OptionPosition.expiry` is populated from `inst.get("expiry")`, which in conn-service
   mode (prod + dev) comes back as a JSON string (e.g. `"2026-11-26"`), not a `date`.
   `p.expiry == today` (a `date` object) is therefore always `False`. Fix: parse/normalise
   `expiry` to a `date` at construction time (wherever `OptionPosition`/`FuturePosition`
   are built from the raw instrument dict — both `scan_positions()`'s own fetch path and
   `classify_from_context()`'s row-building path), so every downstream comparison
   (`p.expiry == today`, and `mcx_non_agri_close_time(pos.expiry)` in `_compute_theta`,
   which would also raise on a string) gets a real `date`. Add a regression test that
   builds `OptionPosition` through the REAL instrument-dict-parsing path (not by passing
   `expiry=<date>` directly, which is how every existing test missed this), confirming
   `scan_positions()` finds a position expiring today when the raw instrument dict carries
   a string date. This single bug is the reason `expiry-day-mcx-itm-auto-close` and the
   pre-close alert renderer (`_v2_expiry_exchange_breakdown_or_empty`) both silently found
   nothing real today, even after tonight's exchange-gate fix.

2. **`backend/api/helpers/snapshot_gate.py:_any_segment_open()` — fail-open race condition.**
   Confirmed via live prod logs (2026-11-27 23:55:30–23:56:30 IST): sporadic single
   requests reach the live broker path while surrounding requests in the same ~40s window
   correctly serve the closed-hours snapshot, with no underlying market-state change (MCX's
   `close_time` was stable all evening). Root cause: `_any_segment_open()`'s
   `except Exception: return True` directly contradicts the documented fail-closed
   contract of `exchange_clock.is_any_segment_open()`, which it wraps (that function
   explicitly fails closed — "empty cache means schedule not loaded, not market open").
   The most likely trigger is a `DetachedInstanceError` on a cached ORM row once
   `exchange_clock._force_refresh()`'s originating session has closed. Fix:
   - Change the except branch to log a warning and `return False` (fail closed), matching
     the wrapped function's own contract.
   - In `exchange_clock.py:_force_refresh()`, expunge/detach the loaded rows (or eager-load
     every column `_is_within_session()`/`_effective_gate_rows()` touch) before the
     `async with` session exits, to remove the likely actual trigger, not just its
     mishandling.
   - Add a regression test: mock `exchange_clock.is_any_segment_open` to raise, assert
     `_any_segment_open()` returns `False`.
   - Flag only (do not fix in this pass): `positions.py:2871-2880`'s fallback branch
     (`source == "snapshot"` but `resp.as_of` falsy → falls through to a live broker call
     unconditionally) is a second, lower-confidence routing-disagreement risk found while
     tracing this — note it in CLAUDE.md as an open follow-up, do not fix now.

3. **`backend/api/algo/actions_preflight.py:~897` — margin-shortfall misclassification
   regression.** Since `e94fb2034`, any leg `error` is re-raised into the except branch,
   whose keyword match (`margin`/`fund`/`balance`) labels any SDK/transport failure whose
   text happens to name a margin method as `MARGIN_SHORTFALL`. This directly contradicts
   `_dlf_inconclusive_message` (`a84e8d6b4`, same file), which exists specifically to avoid
   this. With `preflight.enforce_margin_dhan`/`enforce_margin_groww` on, a transient
   Dhan/Groww calculator error now falsely blocks a real order. Fix: route a leg `error`
   through the same inconclusive/failed classification `_dlf_inconclusive_message` uses,
   not the keyword-match branch. Add a test: a leg error whose text happens to contain
   "margin" (e.g. a generic SDK exception naming its own method `get_margin`) must NOT
   produce `MARGIN_SHORTFALL`.

4. **`backend/api/algo/pnl_math.py` / `positions.py` — baseline-gap loss-guard blind spot.**
   `is_baseline_gap` rows contribute 0 to every Day P&L aggregate, including the
   `loss-positions-total` critical-tier agent's `day_val`/`day_pct`/rate leaves. If one EOD
   `daily_book` write fails for an account, every carried position next session reads as
   contributing zero loss all day — the critical loss guard goes blind in exactly the
   direction it must never fail. Fix: when a baseline gap is detected for a row that still
   has a live, non-zero mark-to-market P&L, the aggregate must not silently zero it —
   either exclude the row from the "confirmed" total and flag the aggregate as
   `degraded`/`partial` (never silently substituting 0), or fall back to the row's own
   lifetime `pnl` as a conservative estimate rather than 0, per the existing "Staleness
   indicator freeze rule" invariant. Add a test proving the critical-tier agent still
   fires when a baseline-gap row is genuinely losing money.

### backend — P2 fixes

5. **`backend/brokers/adapters/dhan.py:2142` — MCX historical backfill stores quantity in
   lots, not contracts.** `_normalise_trades()`'s lot→contract conversion keys on
   `_DHAN_LOT_BY_SECURITY`, built from TODAY's instrument master only
   (`_apply_dhan_instruments`). An expired MCX contract (the vast majority of a historical
   backfill) is missing from it, so quantity is stored raw in lots — wrong by the lot size
   (~100× for CRUDEOIL). Fix: for a `trade_history()`/backfill call specifically, resolve
   lot size from a historical/static lot-size table (or from the stored `daily_book`
   history, which already carries correct contract-based quantities for the same symbol/
   date) instead of only today's live instrument master. If no reliable historical lot-size
   source exists, downgrade the existing "only a warning" to a hard skip + CRITICAL log for
   any expired-contract row where lot size can't be resolved, so wrong numbers are never
   silently stored. This is analytics-only (no live order impact) but must not silently
   corrupt the backfilled P&L history the operator explicitly wants to mine for strategy
   patterns.

6. **`backend/brokers/adapters/dhan.py:82` + `trade_history`/`ledger_entries_raw` — shared
   rate bucket starves live requests.** The historical backfill shares Dhan's 3/s `history`
   rate bucket with live candle/history calls, so a running multi-month backfill starves
   live chart requests. Fix: give the backfill functions their own, lower-priority rate
   bucket (or throttle more conservatively, e.g. 1/s) so a backfill run never blocks a live
   user-facing request.

7. **`backend/brokers/adapters/dhan.py:1092` `trade_history` — client RPC timeout vs.
   paging.** Up to 200 pages run inside one conn_service RPC; the client RPC timeout is 30s
   while the rate bucket allows only 3/s, so a heavy month can time out client-side while
   conn_service keeps paging — `dhan_pnl_sync` then records it as a failed chunk. Fix: either
   raise the specific RPC timeout for this one call, or have the backfill script call it in
   smaller day-ranges per RPC rather than a full month in one call.

8. **`backend/api/routes/history.py:1220` `/ledger`** — loads every matching row (full
   `raw_row` JSONB included) before filtering by segment and paging in Python, even when no
   segment filter is given. Fix: push the segment filter and the offset/limit paging into
   the SQL query.

9. **`backend/api/routes/history.py:1343` `backfill_funds`** — one failed row inside a single
   Postgres transaction aborts every later row in the same session (commit then fails).
   Fix: either commit per-row (accepting slightly more round-trips) or use a savepoint
   per row so one bad row doesn't poison the whole batch. This path is exercised for the
   first time now that the `funds_ledger` SDK-name fix makes it actually return real data,
   so this needs to work correctly, not just "predate the window."

10. **`backend/api/algo/actions_live.py:901` — `hedge_pct` blocks the parent chase.** A live
    `place_order` with `hedge_pct` set resolves the hedge (full instruments scan, batch
    quote with a 12s timeout, positions fetch, hedge preflight) sequentially BEFORE the
    parent order is chased — the parent's limit price goes stale and no chase runs while
    this resolves. Fix: start the parent's chase immediately after it is placed/persisted,
    and resolve + place the hedge leg concurrently (or at minimum, after the parent chase
    has started), not before.

### backend — P3 fixes (cheap, bundle into the same pass)

11. `backend/api/algo/expiry.py:415,550` — BFO legs (SENSEX/BANKEX) are routed into MCX
    instrument lookups via `inst_exchange = "NFO" if exchange in ("NSE","NFO") else "MCX"`,
    so they're never found and never auto-closed/alerted. Fix: add a proper BFO branch
    (own instrument lookup + its own entry in `_MINUTES_UNTIL_CLOSE_RESOLVERS`).
12. `backend/api/helpers/snapshot_gate.py:_segment_today_open_time` — a zero-width holiday
    special-session row (MCX evening-closed holiday) returns `00:00`, making
    `is_segment_pending_open` always False that day (low impact — NSE is also closed on COM
    dates, so the closed-hours snapshot serves anyway, but fix the logic to not silently
    regress the pre-open pin on those dates).
13. `backend/shared/helpers/date_time_utils.py:close_time_for_exchange` — passing a `str`
    date (instead of a `date` object) silently returns the wrong `(23,30)` DST answer
    because `resolve_dst_aware_sessions` swallows the `.year` AttributeError. Make this
    raise or log loudly instead of silently returning a plausible-but-wrong value. Also fix
    the docstring's false claim that DB special-session overrides are honoured here (they
    aren't — they live in the DB, not this YAML-driven function).
14. `backend/shared/helpers/date_time_utils.py:close_time_for_exchange`,
    `backend/api/algo/hedge_strike.py:_resolve_parent_real_expiry_scan`,
    `backend/api/algo/grammar_tokens/metric_is_expiry_preclose_alert_window.py` — add
    per-cycle/per-date memoization so these aren't re-resolving the DST close time or
    linear-scanning the full instruments dump on every row/token/cycle. Low risk, real
    savings given these run every agent cycle.
15. `backend/api/algo/grammar_tokens/metric_is_expiry_preclose_alert_window.py` and
    `scope_positions_expiring_today_mcx_unhedged.py` — docstrings still point at the deleted
    `_v2_expiry_position_breakdown()`. Update to name the real current renderer
    (`_v2_expiry_exchange_breakdown_or_empty`).
16. `backend/config/grammars/agent_grammar.yaml:1453` — `expiry_auto_close` token
    description still names the retired `expiry-day-itm-auto-close` and claims an explicit
    `exchange` param "restricts the scan" when the code actually scans everything then
    filters after. Fix the description text (update the golden baseline in the same
    commit).
17. `backend/config/grammars/agent_grammar.yaml:531,538` — the old combined
    `is_expiry_preclose_{alert,close}_window` tokens are still registered but no builtin
    agent uses them, and they carry exactly the cross-exchange conflation risk the split
    was made to remove. Deprecate/remove them (update golden baseline).
18. `backend/api/algo/actions_live.py:937` — document (CLAUDE.md, not code) the
    `hedge_pct` concurrent-leg naked-short tradeoff explicitly, since it's a deliberate
    design choice, not a bug: alerted via `_al_detect_hedge_pairing_issue` but not prevented.

### frontend — P2 fixes (real correctness/UX risk)

19. **`frontend/src/lib/order/OrderTicket.svelte:147` — `currentQty` defaults to 0 on
    non-position-row ticket openers.** A ticket opened from the watchlist, chart, header
    Order button, Chain→Ticket handoff, or a symbol change in bare-mode picker gets
    `currentQty=0` even when the symbol has a real held position, so a SELL against a held
    long misclassifies as "open short" (shows "Margin required" instead of "cash
    released"), and the client-side 5-lot close exemption misfires. **This is the likely
    actual root cause of tonight's original "long treated as short" report** — tonight's
    `marginChipMode` fix corrected the label table but not this upstream input gap. Fix:
    every ticket-opening path must resolve `currentQty` from the real held position (reuse
    `resolveLegHeldContext`/the portfolio store lookup already used by the Chain-basket fix)
    before rendering the margin/cash chip, not just the paths that already pass it as a
    prop.
20. `frontend/src/lib/order/orderTicketSubmit.js:60` `marginChipMode` — only checks sign via
    `classifyIntent`, not magnitude. An over-close (SELL 3 lots against a 1-lot long) is
    labelled fully "cash released," but 2 of those lots actually open a new short needing
    margin. Fix: thread the same magnitude check `resolveLegHeldContext` already has into
    this chip path, and split the label (or add a caveat) when qty exceeds the held
    position.
21. `frontend/src/lib/order/orderTicketSubmit.js:61` — equity close path always returns
    `'cash_cost'`, so selling an existing equity holding shows "Cost / Cash debit" instead
    of "cash released." Fix: equity close should follow the same close-vs-open branch as
    F&O.
22. `frontend/src/lib/OptionsPayoff.svelte` — the violet "Market P&L" dart plots Day P&L
    (today only) on the lifetime-P&L y-axis, so its height is meaningless next to the
    curves, and its legend label ("Market P&L") is confusingly close to the stat row's
    "Mark P&L" (lifetime), different basis. Fix: either rescale/reposition the dart onto its
    own meaningful reference, or relabel it clearly (e.g. "Day P&L" dart) to avoid the
    name collision with "Mark P&L." Pick whichever requires the smaller change; flag for a
    quick operator confirmation on the final label wording before shipping.
23. DTE now has three different rounding conventions on related surfaces:
    `OptionChainTab.svelte` floors to whole days, `anchorDaysToExpiry` rounds, and the
    Payoff stat row (tonight's fix) shows 2 decimals. Fix: pick ONE convention (2-decimal
    fractional, matching tonight's fix and the underlying `days_to_expiry()` semantics) and
    apply it everywhere DTE is surfaced, or explicitly document why Chain's chip
    legitimately wants a floored whole-day count (space-constrained chip) while Payoff
    wants the fractional value. Also fix the Payoff DTE tooltip text ("calendar days
    remaining") since it no longer matches the now-fractional, close-time-aware value.

### frontend — P3 fixes (bundle, cheap)

24. `frontend/src/lib/derivatives/pageLoad.js:576` — draft legs still use a bare
    `_dInst.x < todayIST()` check, so they vanish at Saturday midnight while real legs on
    the same contract correctly stay until Monday 08:00 (`isPastExpiryFreezeWindow`). Fix:
    route draft-leg expiry checks through the same weekend-aware helper.
25. `frontend/src/lib/order/OptionChainTab.svelte:324` — `_daysToExpiry` applies the
    non-agri 23:30/23:55 close to agri roots (COTTON, MENTHAOIL, CPO), which close earlier.
    Fix: branch agri vs. non-agri close time.
26. `frontend/src/lib/order/OptionChainTab.svelte:811` — Hedge silently no-ops for BFO
    (SENSEX/BANKEX) while the toggle still shows ON, with no message. Fix: either support
    BFO or disable/grey the toggle with an explanatory tooltip for BFO underlyings.
27. `frontend/src/lib/order/OptionChainTab.svelte:904` — changing a parent leg's lots
    silently deletes its hedge leg instead of resizing it, with no toast. Fix: resize the
    hedge leg proportionally, or at minimum toast "hedge removed — lot size changed."
28. `frontend/src/lib/order/ChaseCard.svelte:141` — `activeCount` includes ghost rows, so
    the header chase badge stays lit ~3s after the last real chase ends. Low-value
    cosmetic lag; fix by excluding ghost rows from the count used for the badge/dot
    specifically (keep them in the card's own display grace period).
29. `frontend/src/routes/(algo)/admin/derivatives/+page.svelte:3802` — `placeBasket()` is
    dead code with no caller, and sends contracts where the API expects lots (would place
    oversize orders if ever rewired). Delete it.
30. Palette-token cleanup (bundle together): `OptionChainTab.svelte:1875,1950` hardcoded
    `#1d2a44`/amber literal instead of tokens; `OptionsPayoff.svelte:1393,1543,1622`
    hardcoded `#c084fc`/`#22d3ee` instead of `--algo-violet`/`--algo-cyan`/
    `--algo-cyan-border` (including the spot-pill stroke/text flagged earlier this
    conversation); `ChaseCard.svelte:1161` `.cc-ghost-divider` hardcoded
    `rgba(148,163,184,0.18)` despite its own comment saying "reuse existing tokens only."
31. `frontend/src/lib/dateFormat.js:48,112,113` — `tradingSessionDateIST`,
    `lastWeekdayIsoBefore`, `_firstWeekdayIsoAfter` construct a new `Intl.DateTimeFormat`
    on every call, and are reached per-position/per-tick. Cache the formatter at module
    level.

### doc — write up everything D6 found missing or stale

32. Write new CLAUDE.md entries (or a new doc under `docs/specs/`/`docs/guides/` as
    appropriate) for the four undocumented features: the Kite Console P&L/tradebook/ledger
    backfill (`scripts/kite_console_pnl_sync.py`, cooloff isolation, `--segment`/
    `--ledger-only`, the `broker` column, `get_accumulated_pnl()`); the Dhan historical
    backfill (`scripts/dhan_pnl_sync.py`, `dhan.py:trade_history`/`ledger_entries_raw`, the
    `funds_ledger`→`ledger_report` SDK-name fix); the new history API + MCP tools
    (`/tradebook`, `/ledger`, `/pnl-summary`, the 7 new MCP tools, the seeded
    `pnl-reconciliation-alert`); the `hedge_pct` DSL param and Chain-tab auto-hedge staging
    (also update `AGENTS_GRAMMAR_EBNF.md` per the EBNF-first grammar rule).
33. Correct the stale/contradicted CLAUDE.md entries: the `_exchange_gate_passes` entry
    (now fixed, but note the expiry.py type-mismatch bug found THIS audit, item #1 above,
    so the feature is still not fully working until that ships too); the
    `expiry-nfo-risk-alert`/`expiry-mcx-risk-alert` absorption claim (MCX is live again,
    split back by `28f65346d`); the demoted "netted: ROOT"/"no action (OTM): ROOT" alert
    text (removed, replaced by the simplified account+symbol format); the deleted unsigned
    `underlying_loss_divergence` sibling (fully gone, not "kept"); the stale close-time
    "Still NOT fixed" list (several are now fixed); the stale `marketHours.js` residual
    note (already fixed by `59a597846`/`b81b9b5b4`); the `SEED_VERSION` count (11→13, not
    →12); the `exit-tp-short-option-90` deactivation (note it was turned off in the DB
    tonight, both prod id 103 and dev id 184); the operator guides (`USER_GUIDE.md`,
    `AGENTS_GUIDE.md`, `LAB_MCP_GUIDE.md`, `SIMULATOR_GUIDE.md`) still describing the
    retired combined expiry agents at 30/25 minutes instead of today's four exchange-pure
    agents at 65/60 minutes.
34. Fix the stale code comments found (P3, cheap): `metric_is_expiry_preclose_alert_window.py`,
    `order_release.py:41`, `orders_place.py:464,702`, `models.py:809,823`,
    `orders_helpers.py:621` all still name removed functions (`_v2_expiry_position_breakdown`,
    `_fire_template_attach_on_fill`/`apply_plan_live`) as if live.

### backend-test — ensure coverage for every fix above

35. Every fix above (items 1-18, backend) needs a dedicated test per CLAUDE.md's hard test
    gate — see each item's own "Add a test..." note for the specific scenario. Bundle any
    item without an explicit test note into targeted regression tests covering the exact
    changed lines.

### playwright — frontend regression coverage

36. Add/update Playwright specs for items 19-23 (the real correctness/UX fixes) — especially
    item 19 (ticket opened from watchlist/chart/header on a held symbol shows the correct
    margin/cash label), since this is the highest-value frontend fix in this plan.

## Tests

- pytest: yes (every backend item above)
- svelte-check: yes
- playwright: yes (item 19 especially — this is the practical reproduction of the
  originally-reported "long treated as short" bug)

## Commit message

Will be derived per-commit from the actual diff, split roughly along: (1) expiry.py
type-mismatch fix (the critical one, own commit), (2) snapshot_gate race-condition fix,
(3) preflight margin misclassification fix, (4) baseline-gap loss-guard fix, (5) Dhan
backfill lot-size + rate-limit fixes, (6) history route perf fixes, (7) hedge_pct
sequencing fix, (8) frontend margin/cash chip currentQty + magnitude + equity-close fixes,
(9) frontend P3 bundle (palette tokens, dead code, weekend-boundary straggler, DTE
consistency), (10) doc sync commit(s) for CLAUDE.md + guides + EBNF.

## Done when

- `expiry.py`'s expiry-day filter correctly matches real positions, verified against a
  real/simulated string-dated instrument dict, and a genuine unnetted ITM MCX/NFO position
  on a real expiry day produces a non-empty `scan_positions()` result and a real HELD close
  (not just "no positions need closing").
- `_any_segment_open()` fails closed on any internal exception, with a regression test
  proving it, and a warning log on the failure path for the first time.
- The preflight margin-shortfall gate no longer misclassifies a transient leg error as a
  confirmed shortfall.
- The baseline-gap loss-guard no longer silently reads a stale-baseline row as zero loss.
- A ticket opened from any entry point on a held symbol shows the correct margin/cash
  label (cash released vs. margin required) matching the real held position — reproduced
  live via Playwright, not just unit-tested.
- The Dhan historical MCX backfill either resolves lot size correctly for expired
  contracts or explicitly skips+alerts rather than silently storing wrong quantities.
- All P3 cleanup items are applied or explicitly deferred with a one-line reason.
- CLAUDE.md, the operator guides, and the EBNF grammar doc reflect the actual current
  state of the expiry agents, the backfill features, and the hedge-leg feature.
- Full test gate green: pytest (broker cov ≥80%, api cov ≥45%), svelte-check 0 errors,
  vitest 0 failures, Playwright specs for item 19 passing.
