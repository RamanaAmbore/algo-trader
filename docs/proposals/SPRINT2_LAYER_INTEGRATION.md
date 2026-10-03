# Sprint 2 — Layer Integration Proposal

> **Status: PROPOSED — not yet implemented.** Builds on
> `docs/proposals/ORDER_LIFECYCLE_DATA_MODEL.md` (Sprint 1 — the data
> model). This document covers how every existing layer (frontend order
> surfaces, the Agent builder, MCP/Lab) needs to change to actually surface
> Sprint 1's model, what's genuinely new (functionality and pages that
> don't exist today), and — the most important finding — **several
> real, pre-existing bugs this integration work surfaces that are not
> caused by Sprint 1 but must be fixed alongside it**, because Sprint 2's
> own changes are frequently the natural fix for them.

## 0. Sprint breakdown — this is "Sprint 2" as a set of sprints, not one

The sections below (§1-9) are the full analysis. This section maps that
analysis onto separately-shippable sprints, in dependency order. Each one
is independently valuable — none require finishing every other one first
to be worth shipping.

**Sprint 2a — Pre-existing bug fixes (§2).** No dependency on Sprint 1
shipping at all; `broker_order_id` already exists as a column, it's just
missing from the `AlgoOrderInfo` API response shape. Can ship immediately,
in parallel with Sprint 1: the timeline-drawer field-mismatch fix, the
broker/algo dedup fix (which also fixes `ChaseCard`'s dead dedup for
free), the paper-kill-mislabeled-as-MCP fix, the `logs.py` `sim_mode`
hardcode fix, and the Lab page's stale Safety card fix. Lowest risk,
highest immediate value, no new functionality.

**Sprint 2b — Source/Origin plumbing (§3, §4.1 first half).** Depends on
Sprint 1's `source`/`agent_id`/relationship columns existing. Resolve the
naming-collision decision (§3), thread the new fields onto
`AlgoOrderInfo`, add the "Origin" chip to `OrderCard`, fix the `!o.mode`
broker-row test before it breaks Modify/Cancel. Self-contained once
Sprint 1 and 2a are done.

**Sprint 2c — Mode/status rendering safety (§7, §4.1 second half).**
Depends on Sprint 1's `draft`/`armed` values being real. Fix every
LIVE-fallback default (`_modePill`, `ChaseCard._modeCls`,
`OrderTimelineDrawer.modeCls`), add `draft` to the `/orders/algo/recent`
mode whitelist, fix `templateAttachToast`'s false-positive risk on GTT/
wing children, and decide + build where `armed` rows actually render
(§6 item 5 — likely its own small surface rather than forced into
existing status chips). This is the sprint that makes it *safe* for
Sprint 1's new values to exist in the UI at all — should land before or
alongside 2d/2e, not after.

**Sprint 2d — Per-order timeline view (§4.2, §6 item 1).** Depends on 2a
(drawer bug fixed) and ideally 2b (Origin/relationship fields available
to show). The single biggest net-new user-facing feature here — built on
an endpoint that already exists and is just unused today.

**Sprint 2e — Agent builder structured controls (§4.3, §6 item 3).**
Mostly independent of the others, but only genuinely useful once agents
can actually configure chase/templates on their orders at the *backend*
behavior level — which Sprint 1 deliberately scoped out (Sprint 1 is the
data model only, not behavioral unification). Sequence this after
whatever sprint makes chase/templates actually apply to agent-placed
orders uniformly, or the new UI controls would configure something the
backend doesn't yet honor.

**Sprint 2f — Draft persistence (§4.5, §6 item 4).** Depends on Sprint
1's draft mode being real. Smaller than originally scoped, because most
of the UI (`payoffDrafts`, the DRAFT checkbox, ChaseCard's "D" rows)
already exists — this sprint is server-side persistence + fixing the
delete-before-confirmation and no-account bugs, not building new UI from
scratch.

**Sprint 2g — MCP/Lab integration (§5).** The Safety-card fix ships as
part of 2a regardless. The rest (source-to-AlgoOrder threading,
`mcp_audit.request_id` linking, the minimal order-query tool) depends on
2b and 2d respectively and is lowest-urgency of the set.

**Suggested shipping order**: 2a (now, independent) → 1 → 2b → 2c → 2d →
2f → 2e (once agent chase/template behavior exists) → 2g.

## 1. Four findings that shape everything else

1. **`OrderTimelineDrawer.svelte` is already broken today**, independent
   of Sprint 1. It reads fields (`ev.symbol`, `ev.side`, `ev.qty`,
   `ev.mode`, `ev.created_at`, `ev.price`) that don't exist on the real
   `AlgoOrderEventInfo` response (`id, order_id, ts, kind, message,
   payload_json`). In practice: every event shows a blank symbol/side/0
   qty, defaults to a PAPER pill (even for live orders), sorts on an
   empty field, and never shows a price. **This can't be the foundation
   for a richer Sprint 1 timeline until it's fixed on today's schema
   first** — fixing it is a Sprint 2 prerequisite, not a nice-to-have.
2. **Broker rows and `AlgoOrder` rows have no shared identity key in the
   frontend.** `OrderBook.svelte`'s dedup compares a broker `order_id`
   against `AlgoOrderInfo.order_id || .id` — but `AlgoOrderInfo` has
   neither field, so the comparison never matches. **A live,
   algo-tracked order shows up twice** — once as a bare broker row (no
   mode/source), once as an algo row. Sprint 1's `broker_order_id`
   exposure on the API response is the actual fix for this; Sprint 2
   should thread it through rather than treating it as separate scope.
3. **"source" already means three different, unrelated things** —
   covered in full in §3. Sprint 1's new `Order.source` column must not
   become a fourth meaning without an explicit disambiguation decision.
4. **A draft-to-real-order confirm flow already half-exists on the
   client** (`payoffDrafts` + OrderTicket's DRAFT checkbox + ChaseCard's
   "D" rows) — contradicting Sprint 1's assumption that draft
   functionality needed to be built from nothing. Much of the UI
   plumbing is already there; see §6.4.

## 2. Pre-existing bugs this work must fix (not caused by Sprint 1, but Sprint 2 is the natural fix)

- **§1 finding 1** — `OrderTimelineDrawer`'s field mismatch (fix: read
  `kind`/`ts`/`payload_json`, not invented field names).
- **§1 finding 2** — the broker/algo dedup never matching (fix: expose
  `broker_order_id` on `AlgoOrderInfo`, dedup on that).
- **Every operator "Kill" of a paper chase is mislabeled as an MCP
  action.** `PaperTradeEngine._safe_update_algo_order_cancel`
  (`paper.py:414-441`) hard-codes `payload={"source":"mcp"}` and
  `"... via MCP"` in its cancel event, and this same function is called
  by BOTH the real MCP cancel path and the ordinary ChaseCard Kill button
  (`orders.py:758-764`). Every manual paper-chase kill today is
  indistinguishable from an MCP-initiated one in the event log. This is
  exactly the kind of attribution gap Sprint 1's `source` tagging exists
  to fix — but only if this specific call site is corrected, not just the
  live-agent `agent_id` gap already found.
- **`ChaseCard`'s own dedup logic is dead.** It builds its exclusion set
  from `c.broker_order_id`, but `AlgoOrderInfo` has no such field — same
  root cause as finding 2, same fix closes both.
- **`logs.py:178` hard-codes `sim_mode=False` for every order event**,
  i.e. "order events are real-broker only" — false, since paper/sim
  AlgoOrders already write `algo_order_events` too. Worth fixing while
  touching this code, even though it's not strictly part of the
  integration.
- **The Lab page's own Safety card is stale and actively misleading**:
  it states "No order placement from MCP yet... Phase 3 will match"
  (`admin/research/+page.svelte:838,842`), but MCP already has
  token-gated `place_order`/`cancel_order`/`modify_order` tools wired up
  and working. Anyone reading that card believes a materially different
  (and safer-sounding) thing than what's actually true. Fix this
  regardless of anything else in this proposal — it's a safety
  communication bug, not a feature gap.

## 3. The "source" naming collision — needs one explicit decision

Three existing, unrelated meanings of "source" today:

1. `UnifiedLogRow.source` = which *table* a merged log row came from
   (`'order' | 'agent'`) — `backend/api/routes/logs.py:42`, used as part
   of `UnifiedLog.svelte`'s row key.
2. `TicketOrderRequest.source` = a client-supplied (untrusted) field,
   defaults to `"ticket"`, **never actually sent by any frontend caller
   today** (OrderTicket, OptionChainTab, CommandLineTab, basket all skip
   it) — effectively dead, feeds only `agent_events`, never reaches the
   `AlgoOrder` row.
3. `payload_json.source = "mcp"` on cancel events — an event-payload
   value, and (per §2) currently mis-set for non-MCP kills too.

**Decision needed, proposed default**: keep Sprint 1's new column named
`source` in the database and backend code (it's the right name for what
it represents — which code path created the order). In the **frontend
UI**, label it "Origin" wherever it's displayed, specifically to avoid
visual/conceptual collision with `UnifiedLog`'s unrelated `source` field
in the same merged-log views. Separately, either wire up
`TicketOrderRequest.source` to actually reach the `AlgoOrder` row (making
it the real origin of truth for ticket-placed orders) or remove it
outright — leaving it silently unused and silently different from the
new column would recreate the exact confusion this section exists to
resolve.

## 4. Per-surface integration

### 4.1 OrderBook.svelte

- Add `broker_order_id`, `source` (displayed as "Origin"), `agent_id`,
  and the new relationship fields to `AlgoOrderInfo` — fixes the §2
  dedup bug as a side effect of adding what Sprint 2 needs anyway.
- Replace the `!o?.mode` broker-vs-algo-row test (`:398`) with an
  explicit shape flag — today this doubles as "is this a broker row," so
  adding `mode`/`source` to broker rows (a natural Sprint 2 step) would
  silently make Modify/Cancel disappear from every live order unless this
  is fixed first.
- Add an "Origin" chip to `OrderCard`'s existing chip row — reuses the
  established chip-list UI pattern already there (`tag`/`mode`/`engine`
  chips), not a new UI paradigm.
- **Status-chip gap (real risk)**: OrderBook's five fixed chips
  (Chase/Open/Filled/Rejected-Cancelled/GTT) make any other status
  *silently invisible*, not misrendered. A new `armed` or `draft` row
  would vanish rather than show up wrong — worse for debugging, not
  better. Needs an explicit new chip (or folding into an existing one)
  before these statuses ship, not an afterthought.
- **GTT double-count risk**: the existing GTT chip already sources from
  the broker's own GTT book (keyed `account:gtt_id`), not from
  `AlgoOrder`. Promoting Sprint 1's new `armed` GTT rows into this grid
  without deduping against `gtt_order_id` would show every GTT twice —
  once from the broker book, once from the new first-class row.

### 4.2 LogPanel.svelte / OrderTimelineDrawer.svelte

- Fix the field-mismatch bug (§1/§2) first — read the real
  `kind`/`ts`/`payload_json` shape.
- `GET /api/orders/{order_id}/events` **already exists** and nothing in
  the frontend calls it — this is the natural foundation for a real
  per-order timeline (today, `OrderCard`s aren't even clickable; no
  `onCardClick` is wired). Building the Sprint 1 "see the whole story for
  one order" view is mostly "finally call this endpoint and render it
  well," not new backend work.
- Extend the backend response to include the new relationship fields
  (`parent_order_id`, `oco_pair_id`, `basket_tag`) so a single order's
  timeline view can also show its linked siblings/children, per Sprint
  1's §3.
- `templateAttachToast.js`'s false-positive risk: its condition
  (`mode=='live' && template_id!=null && status=='FILLED' &&
  !attached_gtts_json`) will **false-fire on Sprint 1's new GTT/wing
  child rows** if they inherit `template_id` from the parent, since a
  child row has no `attached_gtts_json` of its own. Fix: either don't
  propagate `template_id` onto `source=template_exit` rows, or exempt
  that `source` value from this check explicitly.

### 4.3 The Agent builder (`automation/+page.svelte`)

- Today `place_order`'s action config is a raw JSON textarea with no
  structured chase or product controls — the `place_order` skeleton has
  no `product` key (silently defaults to NRML backend-side) and **no
  chase configuration of any kind exists at either layer** (confirmed:
  no `params.get("chase…")` anywhere in the actions files).
- **New functionality needed, not just wiring**: a structured form for
  `place_order` exposing at minimum `product` and a chase toggle +
  aggressiveness picker. `ChaseAggPicker.svelte` already exists and is
  self-contained/reusable as-is from `OrderTicket.svelte` — this is
  mostly reuse, not new component design.
- A template picker for agent actions is harder to just reuse:
  `TemplateBar.svelte` exists but is keyed on numeric template IDs, while
  agents use `template_slug` — needs a small id↔slug bridge before it's
  a drop-in.
- Fix the stale InfoHint describing a "+ place_order (templated)" button
  that no longer exists (consolidated away already) — small doc-drift
  fix, unrelated to the rest but found in the same file.

### 4.4 OrderTicket.svelte

- Mostly a source of *reusable* patterns for the Agent builder (§4.3)
  rather than something needing its own changes — its chase UI
  (checkbox + `ChaseAggPicker`) is the reference implementation.
- One real gap: `source` is never sent by this component (or
  OptionChainTab/CommandLineTab/basket) despite the field already
  existing on the request schema — see §3's resolution.
- The `_mode === 'draft'` branches already in this file (`:1961, 2246,
  2464`) appear to be **dead code today** — no caller can reach
  `mode='draft'` given the navbar mode store's actual domain
  (idle/sim/replay/paper/shadow/live has no `draft`). Once draft mode is
  real (Sprint 1 §5, confirmed in scope), these branches become live
  code paths for the first time — they need re-verification, not an
  assumption that dead code "already handles" the new mode correctly
  just because it looks like it was written for this.

### 4.5 Drafts — revise Sprint 1's plan given what already exists

Sprint 1 assumed draft functionality needed to be built from scratch.
The survey found a real, working client-side flow already in place:
operator ticks DRAFT on an F&O order → stored in `payoffDrafts` (in-memory,
session-only, no account, no persistence) → shown as a "D" row in
ChaseCard → clicking it reopens the ticket pre-filled → unticking DRAFT
and submitting goes through the full, real `/ticket` validation path.

**Real bugs in that existing flow, found during this survey**:
- The draft is deleted **before** the placement result is known — a
  rejected or timed-out placement loses the draft permanently, with
  nothing to recover it.
- No draft ID or link is ever sent to the backend — there's no way to
  trace "this order came from that draft" even though the UI flow
  clearly intends it.
- No account field on a draft at all.
- Closing the ticket while editing an existing draft deletes it as a
  side effect of closing, not just of submitting.

**Revised Sprint 2 scope, smaller than Sprint 1 assumed**: persist drafts
server-side (giving them real `AlgoOrder` rows with `mode=draft`, per
Sprint 1 §5), thread a `draft_id` through the *existing* `/ticket`
request path (no new submission flow needed), and only remove/mark the
draft once a successful response comes back — not before. This is
substantially less new UI than originally scoped, because the UI
already exists; what's missing is persistence and correct sequencing.

## 5. MCP / Lab layer

- `backend/mcp/kite_server.py` already has token-gated `place_order` /
  `cancel_order` / `modify_order` tools, each routing through the real
  `/ticket` pipeline (`research.py:1481`+) with `source="mcp"` — but like
  every other path found in Sprint 1's audit, that source tag only
  reaches `agent_events`, never the `AlgoOrder` row. Same fix pattern as
  the `agent_id`/`place_order` gap from Sprint 1: thread it through.
- **Confirmed (verified directly, not just surveyed): the MCP
  `place_order` tool already exposes `chase`/`chase_aggressiveness` to
  the calling AI** (`kite_server.py:541-542`) and already routes them
  through to the real engine — chase already works end-to-end for
  MCP-placed orders today, no gap there.
- **Real gap found**: the MCP `place_order` tool has **no
  `template_slug`/`template_id` parameter at all** — an AI can place a
  chased order via MCP, but cannot attach exit protection to it. Adding
  this parameter (mirroring the ticket API's own `template_slug` field)
  is a small, concrete addition that closes a real capability gap, not
  just a tracking one.
- Live and paper cancel/modify are keyed differently (broker order id vs.
  `AlgoOrder.id`) — the same broker-id-to-row resolution problem flagged
  in Sprint 1 §3.3 for the generic "plain modify" case, not a new issue.
- `mcp_audit.request_id` is a locally generated token, never threaded
  into the ticket or `AlgoOrder` — so there's no join from an MCP audit
  row back to the order it created beyond parsing free text out of a
  summary string. Worth linking properly while this layer is being
  touched anyway.
- **Fix the stale Safety card** (§2) — independent of everything else,
  should happen regardless of Sprint 2's timeline.
- No MCP tool exists to *query* orders/events today, only to act on them
  — a natural, small addition once the per-order timeline (§4.2) exists
  to read from.

## 6. New functionality and pages — explicitly called out, not just "integration"

These don't exist today in any form and are genuinely new build, not
modifications to something already there:

1. **A real per-order timeline view**, built on the already-existing but
   unused `GET /api/orders/{order_id}/events` endpoint. OrderBook cards
   aren't even clickable today. This is the direct payoff of Sprint 1's
   data model and doesn't exist in any form currently.
2. **An "Origin" chip** in OrderBook/OrderCard (§3, §4.1) — new UI
   element, small, reuses the existing chip-row pattern.
3. **Structured chase/template controls for the Agent builder's
   `place_order` action** (§4.3) — today this is pure JSON editing with
   no chase option at all. Mostly reuse of existing components
   (`ChaseAggPicker`), but the form/wiring itself is new.
4. **Server-side draft persistence + the `draft_id`-threaded confirm
   flow** (§4.5) — smaller than Sprint 1 assumed, but still new backend
   work (drafts don't persist today at all).
5. **A GTT/"armed" protection view, distinct from the live-order grid** —
   worth considering as its own small surface (e.g. "Protection" or
   "Exits" section showing TP/SL/wing legs attached to a position) rather
   than cramming dormant, non-actionable GTT rows into the same status
   chips as live, actionable orders. This directly resolves the §4.1
   "which chip does `armed` belong to" and "GTT shown twice" problems at
   the design level instead of patching around them.
6. **A minimal MCP order-query tool** (§5) — doesn't exist, natural once
   the timeline view exists to read from.

## 7. Mode/status rendering safety checklist

Several places default an *unknown* mode to **LIVE**, not a neutral
"unknown" state: `_modePill` (`LogPanel.svelte`), `ChaseCard._modeCls`,
`OrderTimelineDrawer.modeCls`. **A new `draft` row would render with a
LIVE pill** wherever this fallback is hit, which is actively
misleading — the opposite of what a draft/sim/paper indicator exists to
prevent. Every one of these fallbacks needs an explicit `draft` (and
`armed`-aware, where relevant) branch before Sprint 1's new values ship,
not just "it'll probably be fine since it's rare."

Also: `/orders/algo/recent`'s mode whitelist is
live/sim/paper/replay/shadow — a `mode=draft` request today falls through
to "return everything unfiltered," silently wrong rather than cleanly
rejected. Needs `draft` added explicitly, not left to fall through.

## 8. Test impact

The e2e directory is actually `frontend/e2e/` — CLAUDE.md's coverage
table says `frontend/tests/`, which is stale and should be fixed
separately regardless of this proposal.

**Specs that hard-assert today's exact shape and will break if a new
status chip is added**: `order_book_chase_chip_and_session_reset.spec.js`
(asserts exactly 5 status chips and exact Open/Filled counts) and
`order_book_filled_predicate_and_mobile_overflow.spec.js` (same 5-chip
assumption, plus a `repeat(5, minmax(0,1fr))` CSS grid assertion). Both
need deliberate updates, not surprise failures, whenever §4.1/§6.5's new
chip ships.

**Specs mocking today's (broken) event-response shape**:
`log_panel_order_scroll.spec.js` and
`derivatives_fill_no_orderbook_mounted.spec.js` mock
`/api/orders/events/recent` in the current shape — fixing the field
mismatch (§2) means these mocks need updating too, in the same commit as
the fix, or they'll mask the real shape going forward.

## 9. Open questions

1. **"Origin" vs "source" labeling** (§3) — proposed default given above;
   confirm or override.
2. **Where does the GTT/"armed" protection view live** (§6.5) — a new
   page, a new OrderBook tab, or a section within the existing per-order
   timeline view? Affects how much new frontend surface this actually
   is.
3. **`TicketOrderRequest.source`** (§3) — wire it up for real, or remove
   it? Currently dead either way.
4. Should the MCP Safety-card fix (§2, §5) ship independently and
   immediately, ahead of the rest of Sprint 2, given it's a standalone
   accuracy/safety-communication issue with no dependency on anything
   else here?
