# Plan: Chase info sync + mobile header-overflow fixes + alert agent-attribution consistency

## Task

Audit and fix the chase-related gaps surfaced this session: the header chase
badge's popup is a different (and less informative) view than the "within
order" chase card, `AlgoOrder.attempts` never updates during a live chase
(only at the terminal state), the header popup has no countdown, and
OrderBook can show two confusing "open" cards during a single chase
cancel-and-replace (the old order lingering in the broker's own book until
its cancel is confirmed). Incidentally found during the broker-layer
council review: Kite's and Groww's `cancel_order` adapter calls are
unthrottled, even though chase calls `cancel_order` on every single
re-quote attempt today. Also fixing three related mobile-overflow bugs surfaced in the same pass:
the footer page-name wraps to 2 lines and gets vertically centered against
the single-line brand text instead of top-aligned; `OrderCard`'s status
pill (confirmed live via Playwright) gets clipped off-screen at narrow
viewports because the card itself overflows its container instead of its
content shrinking to fit; and the AlgoTimestamp header row (timestamp +
market badge + button-group) needs its existing shrink/scroll mechanism
verified and fixed against its neighboring button-group on mobile.

Separately, auditing and fixing alert-message consistency across every
channel (Telegram, ntfy, email): every outbound alert must carry its
originating agent's name as the FIRST thing in the message, with "manual"
as the literal fallback when an order wasn't agent-driven (e.g. a ticket
order entered and filled by the operator directly). Already audited this
session (full call-site inventory in hand, not re-auditing): rich/dispatch
alerts carry agent name but not always at the top; fill alerts already
resolve a "manual" sentinel but render it mid-message (`Source: MANUAL
OPERATOR ORDER`, line 4) instead of at the start; the failure-alert path
has a real bug — it classifies origin by `agent_id is not None`, which
doesn't recognize the manual sentinel, so a manual order's FILL alert says
"manual" but its REJECTION alert says `[Agent]`; and several renderers
(template guard/attach-fail, chase-cancel, partial-GTT, gtt-not-accepted)
carry no origin at all. Two unrelated LIVE defects were also found
incidentally in this same code during that audit and are fixed in the same
pass since they sit in the same files: `orders.py:805` calls
`send_ntfy_alert(msg)` with the wrong argument count (raises `TypeError`
inside a `create_task`, silently losing every postback CANCELLED alert),
and five call sites in `background.py` call `_alert_route`, a function that
no longer exists (`ImportError`, killing the `gtt_asymmetric`/OCO-sibling/
ticker-recovered/ticker-degraded/visitor-report alerts; two of those five
call sites have no try/except, so the error reaches the ticker watchdog;
two also embed an unmasked account number).

**Explicitly out of scope**: switching chase from cancel-and-replace to
`modify_order()`-in-place. The 6-agent council (architecture/risk/devil/
UX/perf/research) unanimously flagged this as not worth the risk — it
doesn't eliminate the cancel-race (it inverts it into a worse failure mode:
a live order resting while the DB shows terminal REJECTED), buys no fill-
priority or regulatory benefit, and the original duplicate-order problem it
would address is already fixed with no recent recurrence. Not touched here.

## Agents

- backend: In `backend/api/algo/chase.py`, thread the current `attempt`
  number through `_sync_algo_order_id` → `_ch_apply_broker_sync_fields` so
  `AlgoOrder.attempts` is written on every cancel-and-replace sync (same
  call site that already writes `last_attempt_at`/`next_attempt_at`/
  `current_limit`), not only at `_chase_apply_terminal_mutation`. Correct
  `AlgoOrderInfo.attempts`'s docstring in `backend/api/routes/orders_helpers.py`
  (claims "bumped live on every modify event" — currently false; make it
  true, then the docstring is accurate again). No change to chase's retry/
  cancel-confirmation logic itself.
- backend: Alert agent-attribution + format consistency, in
  `backend/shared/helpers/alert_utils.py` and
  `backend/api/algo/event_agents.py`:
  1. Add `resolve_origin(agent_id, agent_name) -> str` to `alert_utils.py`
     — returns the real agent name, or the literal string `"manual"` when
     `agent_id` is `None` or the manual sentinel id, or the name is
     `"Manual operator order"`.
  2. Extend `format_notification_header()` (the existing documented single
     source of truth for alert headers, `alert_utils.py:498`) so it ALWAYS
     emits the origin (`Agent: {origin}`, using `resolve_origin()`) as the
     very first line of the message, never omitted and never positioned
     later — today only the rich/dispatch renderers call it, and even
     those put the origin on line 2, not line 1.
  3. Route every event-agent renderer in `event_agents.py` through
     `format_notification_header()` so origin is first-line-consistent
     everywhere: fill (`fill_notify.py`'s `format_fill_message` — currently
     puts `Source:` on line 4, needs to move to line 1 via the shared
     header), failure (`order_failure_messages` — currently a `[Manual]`/
     `[Agent]` label with no name, never routed through the shared
     header), template guard/attach-fail, chase-cancel, partial-GTT,
     gtt-not-accepted (currently carry no origin at all). Thread
     `agent_id`/`agent_name` into each of these call sites' extras so
     `resolve_origin()` has what it needs.
  4. Fix `_classify_order_origin_label` (`alert_utils.py:928`) to check the
     manual-sentinel id/name (via the new `resolve_origin()`) instead of
     bare `agent_id is not None` — this is the root cause of a manual
     order's fill alert saying "manual" while its rejection alert says
     `[Agent]`/`[Agent Bracket]` for the exact same order.
  5. Fix the two incidental live defects in the same pass: `orders.py:805`
     — correct the `send_ntfy_alert(msg)` call to pass `title`/`message`
     (and thread origin through it too, since it currently bypasses the
     event-agent pipeline entirely). `background.py` (lines 4884, 5342,
     6167, 6242, 6460) — replace the calls to the no-longer-existing
     `_alert_route` with the correct current alert-sending path (whatever
     `format_notification_header()` + `send_telegram_alert`/
     `send_ntfy_alert` now resolves to), wrap the two currently-unguarded
     call sites (6167, 6242) in try/except so a future alert-pipeline
     error can't reach the ticker watchdog again, and mask the account
     numbers embedded at lines 4893/5350 (`mask_account`).
- broker: In `backend/brokers/adapters/kite.py`, add
  `_KITE_RATE_LIMITER.throttle("orders")` to `cancel_order` (mirrors
  `place_order`'s existing call) and wrap the SDK call in `_kite_exc` for
  consistent error typing with `place_order`/the rest of the adapter. In
  `backend/brokers/adapters/groww.py`, add
  `_GROWW_RATE_LIMITER.throttle("orders")` to `cancel_order` the same way.
  Dhan is unaffected (its SDK proxy already throttles every call generically
  by bucket). Pure hardening — no behavior change on the happy path, just
  closes a real unthrottled-hot-path gap that chase's cancel-and-replace
  already exercises every attempt today.
- frontend: Two independent fixes in the frontend:
  1. `frontend/src/routes/(algo)/+layout.svelte` — swap the header
     `.chase-chip`'s popup body from `OrderTimelineDrawer` (event-history
     log, no countdown) to `<ChaseCard>` (same component already embedded
     "within order" in `SymbolPanel.svelte`, self-polling, has a working
     countdown via `_nextIn()`/`next_attempt_at`). Use non-compact mode so
     the LIVE/mode pill stays visible (dropping it in an aggregated
     real-money view is a safety issue). The drawer shell (not ChaseCard)
     must own the empty/"no active chases" and error states explicitly —
     ChaseCard self-hides when `_chases.length === 0`, so the drawer would
     otherwise go silently blank. Unify the chip's "N chasing" count with
     ChaseCard's own `activeCount` bindable prop so the header number and
     the popup's contents are driven by the same poll and can never
     disagree. Leave `OrderBook.svelte`'s own per-order "view timeline"
     entry point (which also opens `OrderTimelineDrawer`, via
     `linkedOrders`) on the existing event-log view unchanged — that's a
     distinct, legitimate forensic-history use case ChaseCard can't serve
     (it only shows active chases, not terminal ones).
  2. `frontend/src/lib/OrderBook.svelte` — fix the "two open order cards"
     confusion. `_loadOrders()` merges the raw broker book with AlgoOrder
     rows, deduping only on the algo row's *current* `broker_order_id`.
     During a chase cancel-and-replace, the just-superseded broker order_id
     is no longer referenced by anything, so until the broker's own book
     catches up with the cancel, it renders as an independent "open" card
     alongside the new one. Fix: remember each algo row's previous
     `broker_order_id` across polls (diff this poll's algo rows against the
     last poll's), and for a short grace window (~20-30s) treat a
     broker-only row matching a just-superseded id as the same logical
     chase — suppress its own card and fold a brief "repricing — previous
     order cancelling" indicator into the new order's card instead.
     Frontend-only, no backend/chase.py changes, no execution/P&L risk.
  Mobile footer fix is already implemented (`.algo-footer` gets
  `align-items: flex-start` inside the existing `@media (max-width: 640px)`
  block) — just needs a Playwright assertion added.
  3. `frontend/src/lib/order/OrderCard.svelte` / `frontend/src/lib/OrderBook.svelte`
     — mobile status-pill clipping, confirmed live via Playwright at
     360×800: the card's right edge sits past the viewport edge (374px
     vs. a 360px viewport) and `.ob-scroll`'s `overflow-x: auto` lets the
     whole card overflow instead of its content shrinking to fit, so the
     status pill's right edge (where REJECTED/CANCELLED text lives) is
     clipped off-screen. Desktop is unaffected; the pill text/color
     coding itself is correct and not the problem. Fix the card/grid
     sizing (likely `.ob-scroll`'s grid `minmax()` or the card's own
     min-width) so a card fits the viewport width on mobile without
     needing its own horizontal scroll, consistent with how
     `OrderCard`'s symbol span already truncates (`min-w-0 truncate`)
     but the pill (`ml-auto flex-shrink-0`) currently doesn't get the
     room that truncation should free up.
  4. **Root cause, confirmed via audit (supersedes the original guess
     below)**: `frontend/src/routes/(algo)/+layout.svelte:2221` —
     `.page-header { flex-wrap: wrap }` affects ALL 24 algo pages, both
     viewports. The header is `position: fixed` with `.algo-content`'s
     `padding-top` hardcoded to one row's height, so a wrapped second row
     covers the first card, not just "looks untidy". `AlgoTimestamp.svelte`'s
     `.ats-root`/`.ats-scroll` shrink+scroll mechanism never gets a chance
     to run: `.ats-root` has `min-width: 0` but no flex-basis override, so
     a `flex-wrap: wrap` parent decides the line break using its full
     max-content width and moves it to a new line BEFORE `.ats-scroll`
     ever scrolls. Fix: `.page-header { flex-wrap: nowrap }` plus
     `.ats-root { flex: 1 1 0 }` (or equivalent) so it can actually shrink
     within a non-wrapping row. Correct the stale comment at
     `+layout.svelte:2205-2219` claiming the header "wraps to its own line
     on narrow widths" (that's the behavior being removed, not kept).
  5. **Also found by the audit, direct violation of the stated rule**:
     `+layout.svelte:2392-2397` — `.page-header-actions` (the button
     group) is set to `flex-wrap: wrap; flex-shrink: 1; min-width: 0;
     white-space: normal` at ≤640px — the exact opposite of "button group
     must always be displayed, never wrap/get pushed off". The desktop
     rule (2280-2286) is already correct; fix mobile to match (pinned,
     `flex-shrink: 0`, scrolls if it must, never wraps).
  6. `frontend/src/lib/OrderBook.svelte` status-chip row
     (`:998-1006`/`:1137-1147`, mobile) — found adjacent to the
     already-planned OrderCard pill fix, same file: the status-chip row
     (Held/Chase/Open/Filled/Rejected-Cancelled/GTT) is a fixed
     `repeat(6, minmax(0,1fr))` grid with `overflow:hidden;
     text-overflow:ellipsis` on `.ob-sc-l`, so "Rejected/Cancelled" is cut
     off at 320-375px — clipping instead of scrolling. Fix: horizontally
     scrolling flex row (or `minmax(max-content,1fr)` inside an
     `overflow-x: auto` wrapper), matching the rest of this plan's
     overflow-not-clip convention.
  7. Also remove the now-dead `.algo-ts` ellipsis rule at
     `+layout.svelte:2408-2410` (≤480px) — `AlgoTimestamp` renders
     `.ats-*` classes now; `.algo-ts` is only still consumed by
     `PnlPanel.svelte:115`, and the rule itself contradicts the
     overflow-not-clip invariant.

     **Deferred to a follow-up (not in this plan's scope) — lower-impact
     or judgment-call items the same audit found, so they're not lost:**
     `SymbolPanel.svelte:4241` `.oes-tabs` wrap is a *deliberate* 2026-09-30
     design decision per its own comment (needs an operator call, not a
     unilateral reversal); `ChartWorkspace.svelte`'s `.cw-controls`/
     `.cw-picker` toolbar wrap; `LogPanel.svelte`'s `.om-bar` chip
     sub-strip wrap; `OptionChainTab.svelte`'s `.oct-toolbar` wrap
     (low impact, no buttons); `PriceChart.svelte`'s `.chart-header` (no
     overflow handling at all, Simulator/Replay panels); admin
     `/admin/mcp` page's `.audit-head` wrap; `MarketPulse.svelte`'s
     duplicate `.mp-head-tabs` CSS block (dead, harmless) and
     `.mp-section-with-picker` wrap; `SimulatorPanel.svelte`'s
     `.sim-payoff-header`/`.iter-header` (admin only); `PnlPanel.svelte`/
     `UnifiedLog.svelte` header wraps with no button group (low impact).
- backend-test: pytest coverage for the `attempts` per-attempt write
  (assert `AlgoOrder.attempts` reflects the current attempt mid-chase, not
  just at terminal) and for the Kite/Groww `cancel_order` throttle call
  (assert the rate limiter's `throttle` is invoked, matching the existing
  `place_order` test pattern for each adapter). Plus: `resolve_origin()`
  (agent name / manual / sentinel cases), `format_notification_header()`
  always emitting origin as the first line, `_classify_order_origin_label`
  recognizing the manual sentinel consistently across fill AND failure
  alerts for the same order, the fixed `orders.py:805` call, and each
  repaired `background.py` `_alert_route` call site (golden-render style
  tests, matching this codebase's existing alert-test pattern, e.g.
  `test_alert_golden.py`).
- playwright: e2e coverage for (a) the header chase badge opening a
  ChaseCard-format popup with a live countdown and correct empty/error
  state, (b) OrderBook not showing two simultaneous "open" cards for a
  mocked cancel-and-replace sequence, (c) the mobile footer page-name
  top-aligning with the brand text at a narrow viewport (extend
  `frontend/e2e/footer_credits.spec.js`), (d) a mocked REJECTED and a
  mocked CANCELLED order card both staying fully visible (status pill not
  clipped) at a 360px viewport, (e) `.page-header` (title/timestamp/badge +
  button-group, representative page e.g. `/orders`) not wrapping to a
  second row at 360px — button group stays pinned and visible, middle
  content scrolls instead, (f) OrderBook's status-chip row ("Rejected/
  Cancelled" among others) staying fully readable (not ellipsis-clipped)
  at 360px.

## Tests

- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message

fix(chase+alerts): sync header badge popup with live order-card view, dedupe cancel-replace cards, throttle broker cancel_order; make every alert channel lead with agent/manual origin; fix mobile header-overflow bugs (footer, order card, market badge)

## Done when

- `AlgoOrder.attempts` updates live during an in-flight chase, not only at
  the terminal state.
- The header chase badge's popup shows the same live-status format
  (countdown, Kill) as the "within order" ChaseCard, with its own
  empty/error state and a chip count that matches the popup contents.
- OrderBook never shows two independent "open" cards for the old and new
  order of a single chase cancel-and-replace attempt.
- Kite's and Groww's `cancel_order` calls are rate-limited like
  `place_order`.
- Footer page-name top-aligns with the brand text on mobile, verified via
  Playwright.
- A REJECTED or CANCELLED order card's status pill stays fully visible
  (not clipped) at a 360px mobile viewport.
- `.page-header` never wraps to a second row on any of the 24 algo pages
  at mobile widths — the button group stays pinned/visible and the
  title/timestamp/badge area scrolls instead, verified on `/orders` (or
  another representative page) via Playwright.
- OrderBook's status-chip row ("Rejected/Cancelled" etc.) is fully
  readable, not ellipsis-clipped, at a 360px mobile viewport.
- Every alert — Telegram, ntfy, email, every renderer (rich, dispatch,
  fill, failure, template, chase-cancel, partial-GTT, gtt-not-accepted) —
  leads with `Agent: {name}` or `Agent: manual` as the first line, never
  omitted, never positioned later in the message.
- A manual order's fill alert and its failure/rejection alert (if any)
  show the SAME origin ("manual"), never one saying "manual" and the other
  "[Agent]".
- `orders.py:805`'s postback CANCELLED alert actually sends (no more
  silent `TypeError`). All 5 `background.py` `_alert_route` call sites
  work again (no more `ImportError`), the two previously-unguarded ones
  are wrapped in try/except, and the two unmasked-account lines are
  masked.
- All gates green: pytest, svelte-check, vitest, playwright.
