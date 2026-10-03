# Order Lifecycle Data Model — Sprint 1 Proposal (v3)

> **Status: PROPOSED — not yet implemented.** Design document for operator
> review, not a record of shipped behavior. v3 follows a thorough code
> audit of v2 that found real factual errors — wrong column names, a
> mismodeled OCO structure, a reversed understanding of draft mode's
> actual state, an unsafe existing helper this design depends on, and a
> compliance deadline already past. Every correction below is cited to
> the audit finding that drove it. Several items are now explicit open
> risks requiring operator sign-off, not quietly-deferred nice-to-haves.

## 1. Purpose, and what this does NOT consolidate

"What happened to this trade" is scattered across tables that don't
reference each other: `algo_order_events` (the per-order timeline),
`agent_events` (an agent's own decision bookkeeping — `action_success` /
`_failed` / `_skipped` — written before any order may even exist),
`audit_log` (request-level audit, retained 365 days citing SEBI's 8-year
rule), and `algo_events` (a WebSocket broadcast log with an FK to
`algo_orders` that's never actually populated). Some links already exist
and this proposal reuses rather than duplicates them:
`AlgoOrder.request_id` already joins to `audit_log.request_id`;
`/api/logs/unified` already merges `algo_order_events` and `agent_events`
for display.

**Explicit scope decision**: Sprint 1 only formalizes `AlgoOrder` +
`algo_order_events`. **`agent_events`, `audit_log`, and `algo_events` are
not touched, not consolidated, and not deprecated in this sprint** —
consolidating them is real future work, not something to pretend is
solved here. One concrete consequence: `symbol_resolution_failed` (a
failure that happens *before* any Order row exists) cannot be an
`algo_order_events` row, because `order_id` there is `NOT NULL`. It
should go to the existing `agent_events.action_skipped` path instead — no
schema change needed, just a decision to route it there.

There's also an existing `engine` column on `AlgoOrder`
(sim/paper/live/replay/shadow/target/manual/expiry) that overlaps what
this proposal calls `source`. **These are not the same axis and must not
be conflated**: `engine` (existing, unchanged) = which execution engine
processed the order. `source` (new) = which code path *created* it. Both
live on the same row, answering different questions.

## 2. The entities

### 2.1 Instrument resolution

`Order.exchange` records the specific venue an order executed on. For
**MIS/intraday equity and F&O, exchange is part of position identity** —
confirmed, no change needed. For **CNC/delivery equity, the common case
(a stock settled in demat, sold later on either exchange) is genuinely
exchange-agnostic.**

**Resolved — decided approach predates this proposal, from prior
operator research**: position state is derived **purely from
position-level fields** — open order qty, current qty, average price,
current price — not from tracing which specific exchange individual legs
executed on. This already handles cross-exchange cases correctly at the
position level without needing to resolve same-day netting mechanics
explicitly; whether a given leg happened to execute on NSE or BSE doesn't
change the position-level reconciliation.

**This proposal's job is narrower than originally framed**: not to
re-derive position state (already solved, above), but to **plug
order-level detail onto that already-correct position picture** — i.e.
`Order.exchange` records which venue a specific order executed on
*for traceability*, and position math continues to come from the
existing position-level fields, unchanged. The two pictures (position
state from positions data, causal history from the new Order/OrderEvent
model) are complementary, not competing sources of truth — together they
give the complete picture; neither one alone needs to carry the whole
burden.

### 2.2 Order — the header row (extends `AlgoOrder`, not a new table)

`AlgoOrder` starts at `backend/api/models.py:731` (corrected from an
earlier mis-cite). Fields:

| Field | Status | Notes |
|---|---|---|
| `id` | exists | primary key |
| `mode` | exists, **zero migration needed** | `VARCHAR(8) NOT NULL DEFAULT 'live'` — no Postgres ENUM, no CHECK constraint. Current values: sim/paper/live/replay/shadow. `draft` fits with no DB change at all — only the *application-level* validation that currently rejects it (see §5) needs to change. |
| `source` | **new column** | ticket / basket / agent / chase / template_exit / **take_profit** (the legacy TP child's own tag) / admin_reconcile / postback_recovery / **mcp** / **chain** / **command** / **place** / **pair** (the `/api/orders/pair` admin route) / **expiry_auto_close** — this list was incomplete in v2; these are real existing code paths the audit found with no source tag today. Orthogonal to the existing `engine` column (§1). |
| `agent_id` | exists, **bug is narrower than previously stated** | `_write_live_order` (`actions.py:522`) already sets this correctly for `close_position`, `chase_close`, and other live actions. The gap is specifically `place_order`'s `_AgentShim` (`actions.py:180` → `actions_live.py:200-204`), which has no `id` at all. The same shim also never sets `product` (silently falls back to NRML) or `template_id` — same fix, same call site. |
| `strategy_id` | exists — **not the basket-sibling tag, do not reuse for that (see §3.2)** | real FK to `strategies`, used for attribution/capacity caps/lot ledger. Each basket leg can carry a *different* `leg.strategy_id`. The `Agent` model has no `strategy_id` column at all — giving agent orders one is new schema, not a wiring fix. |
| `basket_tag` | **exists already** | this, not `strategy_id`, is the real basket-sibling grouping key — indexed, written per leg, sent to the broker as `tag` (`orders_basket.py:551,639,703,744`; index at `database.py:387`). No fix needed here, v2 was simply wrong about which column does this job. |
| `chase_session_id` | **new** | groups chase attempts; see §6 for how it relates to `chase_modify` events and §9 for the restart-recovery question |
| `oco_pair_id` | **new, shape depends on broker** | see §3.1 — Kite/Dhan need this to describe one native two-leg GTT, not link two separate rows; only Groww genuinely needs a peer-row link |
| `gtt_order_id` | **exists, unused today** | `models.py:850` — nothing writes it, but `positions.py:188`'s `has_gtt` check already reads it. GTT rows should store the broker's GTT id here, **not** in `broker_order_id` — Kite GTT ids and order ids are both plain numbers, so reusing `broker_order_id` risks a false postback match. |
| `algo_id` | **new column — sourced from one settings placeholder (decided, see §9)** | every order that needs to carry an algo ID reads it from a single settings value (e.g. `compliance.algo_id`, same DB-backed settings pattern as `performance.refresh_interval`), not hardcoded or independently set per call site. Starts as an empty placeholder; the day a real SEBI-issued ID exists, updating that one setting propagates everywhere automatically. This settles the *technical* design — the underlying compliance exposure/timeline question is separate, still open, see §9. |
| `parent_order_id` | exists, **already overloaded today, not purely causal** | the legacy TP idempotency check counts *any* child row (`orders_place.py:946-952`); the admin `/api/orders/pair` route lets an operator set this for non-causal grouping (read by `positions.py`'s `pair_group_key`); postback skip-logic (`orders_postback.py:587-631`) skips any row with a parent, for any reason. New GTT/wing children must be distinguished by `source`, not just by having a parent, or they'll silently interact with these existing checks. |
| `status`, `broker_order_id`, `quantity`, `filled_quantity`, `exchange` | exist | unchanged — but see §3.1/§5 for the new `armed`/draft status values and which existing scanners need auditing before those ship |

### 2.3 OrderEvent — corrects a wrong table description in v2

The real table is `algo_order_events`, with real columns `kind`
(VARCHAR(32)), `ts`, `message` (VARCHAR(500), NOT NULL), `payload_json`
(Text). **v2 invented column names that don't exist** (`event_type`,
`occurred_at`, `detail`). Corrected mapping: use `kind` (not
`event_type`), `ts` (not `occurred_at`), `payload_json` (not `detail`).

**One genuinely new column is needed**: `broker_order_id_at_event` does
not exist today. This *is* a real (additive, nullable) schema change —
v2's claim that extending the event vocabulary needs "no schema change at
all" is only true for new `kind` string values, not for this column.

**Corrected vocabulary.** The real, currently-used `kind` values (a soft
`VALID_KINDS` frozenset that only logs at debug level if violated —
`order_events.py:40-43,65-66`) are: `placed`, `agent_trigger`,
`chase_modify`, `fill`, `unfill`, `reject`, `cancel`, `postback`,
`margin_check`, `preflight_ok`, `preflight_block`, `error`. Two more are
written but not in that set: `killed` and `broker_postback`. **Kite
writes `postback` for the same event Dhan/Groww call `broker_postback`** —
an existing inconsistency Sprint 1 should also fix by picking one
canonical name, not just work around.

v2 used `filled` (doesn't exist — the real value is `fill`) and invented
`chase_attempt`, which duplicates `chase_modify` (paper chase already
writes this per attempt today — `paper.py:915-922`). **Reuse
`chase_modify` for every chase reprice, live included — don't add a
parallel name for the same thing.**

New values actually needed: `chase_cancel_confirmed` /
`chase_cancel_unconfirmed`, `chase_exhausted`, `template_attach_started` /
`_ok` / `_failed`, `gtt_armed`, `gtt_triggered`, `modified` (plain
modify, §3.3), `position_reduced`, `protection_resized`,
`position_closed`, `holding_sale_fill`. A partial fill is **not** a new
`kind` — reuse `fill` with `payload_json.partial = true`, to avoid
vocabulary bloat for something that's a flag, not a different event.

**A real reliability gap this proposal must not make worse**: events are
enqueued through an `EventQueue` with `on_full="drop"` (cap 10,000), and
on any write failure the **entire batch** is re-queued at the front
(`event_queue.py:199-225`) — one row with a bad foreign key (e.g. from an
order already CASCADE-deleted by the 30-day sim purge) blocks every later
event indefinitely until the queue fills and starts silently dropping.
Chase can generate ~30 events per session; widening event-writing as this
proposal does makes this existing fragility materially more likely to
bite. **This needs a per-row fallback on batch failure as a prerequisite
for Sprint 1**, not something inherited silently.

### 2.4 Position — the result (unchanged math, now traceable)

No change to how `positions.py`/`daily_book` compute P&L. Every Order's
terminal event carries enough detail to trace which order caused a
position-row update.

## 3. Order-to-order relationships

Same test as before: **did a specific event on order A cause order B to
exist?** Yes → `parent_order_id` (directional). No, created together by
one action → peers (`oco_pair_id` if they also mutually cancel;
`basket_tag` — corrected from v2's `strategy_id` — if they're just a
submitted-together group with no cancel effect).

### 3.1 GTTs as first-class rows — broker-dependent shape, and an unsolved linking problem

**Kite and Dhan both have native OCO** (`capabilities.py:101,119` —
Dhan's "Forever OCO"): take-profit and stop-loss are **one broker GTT
object with two legs**, not two separate GTTs. **Only Groww**
(`gtt_oco=False`) places two independent GTTs, paired by software
(`_task_oco_pair_watcher`, `background.py:4130`) — v2 had this backwards,
describing Dhan as the emulated case.

Consequence for the model: **the row shape is broker-dependent.** For
Kite/Dhan, model the native OCO GTT as **one** Order-shaped row
(`source=template_exit`, `status=armed`, `gtt_order_id` = the broker's
single GTT id) carrying both legs' trigger prices in its payload, with
two possible `gtt_triggered` events (one per leg, whichever fires). For
Groww, use **two** rows linked by `oco_pair_id`, matching what the
existing watcher already does. **`oco_pair_id` is not needed for the
majority (Kite/Dhan) case at all** — only for Groww.

**Open gap, not solved by this document**: Dhan's Forever/OCO orders
don't support MCX (`gtt_supports_mcx=False`), and template attach has no
GTT path for Dhan MCX today. What actually happens for a Dhan MCX
template exit is not established by this proposal and needs separate
investigation before Sprint 1 claims to cover it.

**Open gap, not solved by this document**: there is no reliable mechanism
today to link a GTT trigger back to the GTT row that spawned it. Kite's
postback handler has no orphan-row branch at all (confirmed) and falls
back to a 60-second fuzzy match by account+symbol+side+qty
(`_pb_fallback_lookup_row`) that **could misattribute a GTT-triggered
fill to an unrelated just-placed order** with the same shape. Dhan/Groww
GTT-fire detection is mostly poll-only, not postback-driven. Order tags
today (`tpl-{template_id}-{label}`) carry neither the GTT id nor the
parent row id. **This document does not have a solved answer for how a
trigger maps back to its row, per broker** — it's an explicit open design
question, not a detail to fill in during implementation.

The wing leg is a plain live order (not a GTT) placed immediately on
template attach — it gets a real `AlgoOrder` row too, same as before, but
its own fill postback hits the same unsolved linking problem above.

The legacy take-profit child (`_arm_take_profit`) already creates real
rows today (`engine="target"`) — give these the `source=take_profit` tag.
Worth flagging: this code path is itself marked for Phase-2 deprecation,
so it's a shaky foundation to build new conventions on.

### 3.2 Basket siblings use `basket_tag`, not `strategy_id`

Corrected from v2: `basket_tag` already exists, is indexed, and already
does this job correctly. No fix needed for basket grouping. `strategy_id`
is a separate, legitimate concept (attribution to a `Strategy` entity for
performance tracking) that agent orders may or may not need — if wanted,
that's new schema (the `Agent` model has no such column today), scoped
honestly as new work, not bundled into "the same bug as `agent_id`."

### 3.3 Partial closure, re-entry, partial *entry* fills, and plain modify

Partial closure and re-entry: unchanged from the prior review round —
independent orders, no forced relationship, `position_reduced` /
`protection_resized` events, re-entry gets no link back to the prior
episode (§3.3 of v2, confirmed sound by the audit, kept as-is).

**Two gaps the audit found that v2 didn't cover:**

- **A plain modify** (price/qty change, no cancel+replace) is a distinct
  path from chase (`PUT /api/orders/{order_id}`, keyed by *broker* order
  id, today writes no event of any kind). Needs a `modified` event and a
  rule for resolving the `AlgoOrder` row from a broker-id-keyed request.
- **A partial fill on an *entry*** (not a close) has no event today
  beyond chase silently overwriting `filled_quantity` in place. Worse:
  **template attach refuses to arm protection on any partial fill**
  (`orders_postback.py:599-631`), and the deferred-retry mechanism for
  this is an unbuilt TODO column. A position that ends up partially
  filled and then UNFILLED (chase gave up) can be left with **no exit
  protection at all, today, for real** — not a hypothetical. Sprint 1
  should at minimum emit a visible event for this state so it's no longer
  silent, even if fully fixing the underlying protection gap is later
  work.

### 3.4 Position-context resolution depends on fixing an existing unsafe helper

The helper this section's design would generalize,
`_fetch_net_position_qty` (`orders_place.py:384-411`), is **unsafe
today**: it ignores its own `exchange` and `product` arguments, returns
the *first* matching row rather than a correctly netted quantity (so the
same account holding both MIS and NRML, or positions on two exchanges,
nets wrong), guesses which column is the account column by substring
match and **silently drops the account filter entirely if the guess
misses** (meaning it can resolve against the wrong account), and reads a
30-second-stale cache. Agent `close_position` compounds this — it
defaults side/product/exchange (SELL/NRML/NFO) without verifying against
the real position at all.

**This is now a required part of Sprint 1, not a future nice-to-have**:
the shared position-context resolver must key on account + product +
exchange explicitly (exchange-agnostic only for settled CNC, per §2.1's
caveat), use a fresh read, and fail closed — refuse to act rather than
guess — when the account can't be unambiguously resolved.

## 4. Holdings sold → converted to a position for the day

Unchanged: the existing P&L-split math isn't touched; the sell order gets
a `holding_sale_fill` event. Per §2.1's resolution, the position-level
reconciliation (open order qty, current qty, average price, current
price) already handles this correctly regardless of which exchange the
sell executed on — this proposal only adds the order-level link for
traceability, not a new source of truth for the P&L math itself.

## 5. Draft mode — v2's framing was backwards

**Decided: yes, making draft orders real, server-accepted functionality
is confirmed in scope** — the 400-rejection guardrail below is to be
removed as part of this work, not just documented as a constraint.

The audit found the real state of the world is the **opposite** of what
v2 described:

- **Draft placement is actively rejected today**, by design:
  `_validate_ticket_mode` returns HTTP 400 — "Drafts are client-side"
  (`orders_place.py:1178-1185`) — and the frontend already coerces a
  draft selection to paper mode before it ever reaches the backend
  (`OrderTicket.svelte:2464`).
- **`draftPositions` (the concept v2 picked as "has real intent") has
  zero writers and was explicitly marked PARKED** (commit `4ffc71d1`,
  2026-10-02) — the opposite of what v2 claimed. `payoffDrafts` is the
  one that's actually wired up and used.

So making `draft` a real, server-accepted mode is **new functionality**,
not formalizing something that half-exists — it requires removing/
changing the 400-rejecting validation, and deciding what "confirming a
draft" actually does given every real placement path (`/ticket` etc.)
builds its own pre-persisted row through full validation (close-intent
verification, lot caps, preflight, market-hours gate). A draft row can't
just flip its `mode` column in place — confirming it has to go back
through that same validation, which likely means the draft row and the
eventually-placed order are two different rows with a link between them,
not one row that mutates.

Also unresolved and needed before this ships: which `status` value draft
rows use (must not collide with `OPEN`-based scanners — see §3.1/§7.4),
an expiry/cleanup policy for abandoned drafts (nothing purges them today),
and exclusion from strategy capacity sums, which currently have no mode
filter at all.

## 6. Worked example (Kite, corrected vocabulary and GTT shape)

```
Order #4821  mode=live  source=agent  agent_id=17  basket_tag=null
             template_id=9  chase_session_id=A1  exchange=NSE

  OrderEvent  agent_trigger   "RSI < 30, qty=50"
  OrderEvent  preflight_ok
  OrderEvent  placed          broker_order_id=K001
  OrderEvent  chase_modify #1 broker_order_id=K001  price=182.40
  OrderEvent  chase_cancel_confirmed  broker_order_id=K001
  OrderEvent  chase_modify #2 broker_order_id=K002  price=182.55
  OrderEvent  fill            broker_order_id=K002  qty=50 @182.55
  OrderEvent  template_attach_started
  OrderEvent  template_attach_ok   gtt_order_id=G771 (ONE native OCO GTT, two legs)

Order #4822  parent_order_id=4821  source=template_exit  status=armed
             gtt_order_id=G771  (both TP and SL legs — one row, Kite native OCO)

  OrderEvent (on #4822)  gtt_triggered  leg=SL  -- mapping this back to #4822
                           reliably is the open problem noted in §3.1

Order #4823  parent_order_id=4822  exchange=NSE  (the live order the SL leg spawned)

  OrderEvent (on #4823)  fill            qty=50 @178.10
  OrderEvent (on #4823)  position_closed
```

`placed` and `chase_modify #1` are distinct, sequential events — the
first chase reprice is a *new* event after the initial placement, not a
duplicate of it.

## 7. Backward compatibility and zero-impact rollout

**Cross-cutting rule, applies to every future consumer of these new
columns, not just one UI note**: because no backfill happens (§7.1),
every one of `source`, `chase_session_id`, `oco_pair_id`, `algo_id`, and
`broker_order_id_at_event` will be `NULL` on every order placed before
this ships, forever. **Any code that reads one of these fields —
backend logic in Sprint 1b, frontend display in Sprint 2, the audit-trail
screen, anything — must treat `NULL` as a valid, expected, permanent
state, not an edge case to patch around later.** Concretely: never assume
`source` is populated when branching on it; render an explicit "unknown"
state rather than defaulting to a misleading value (the mode-rendering
bugs already found in Sprint 2 §7 — unknown mode silently defaulting to
a LIVE pill — are exactly the failure class this rule exists to prevent);
never `NOT NULL`-constrain a query or join on these columns in a way that
would silently exclude every pre-Sprint-1 order. This applies even though
Sprint 1a itself adds no code that reads these fields yet — the
columns exist before anything consumes them, so the NULL-handling
discipline has to be designed in from the first consumer, not
retrofitted after the first bug report.

### 7.1 Schema mechanics, corrected

- `mode = draft` needs **zero database migration** — `mode` has no
  ENUM/CHECK constraint (corrected from v2, which hedged on this).
  Only the application-level rejection in §5 needs to change.
- `source`, `chase_session_id`, `oco_pair_id`, `algo_id` on `AlgoOrder`,
  and `broker_order_id_at_event` on `algo_order_events`, are genuinely new
  nullable columns — additive, but **`ADD COLUMN NULL` still takes a
  brief exclusive table lock** that queues behind any in-flight
  transaction (including the `FOR UPDATE` locks already used elsewhere)
  and blocks queries queued behind it. The current migration path
  (`init_db` at API startup) sets **no `lock_timeout`** — add one, so a
  stuck migration fails loudly instead of hanging the whole API startup.
- New indexes (`chase_session_id`, `oco_pair_id`, `source`) must use
  `CREATE INDEX CONCURRENTLY` outside a transaction — the codebase's
  existing pattern (`CREATE INDEX IF NOT EXISTS`) blocks writes while it
  builds, which is not acceptable for a live orders table.
- A self-referencing FK for `oco_pair_id` should be added `NOT VALID`
  with a background validation scan, not validated inline against the
  whole table.
- No backfill, as before — this is unchanged and still correct.

### 7.2 Tracking must not block real orders — now with the specific mechanism required

The hard rule stands (event writes never block/fail/roll back a real
broker action), but §7.3 below shows *why* the naive version of this rule
isn't enough on its own.

### 7.3 Dual-write for GTT/wing rows — corrected, this needs real machinery

v2's "just write a row alongside the JSON" undersells this. The audit
found: `attached_gtts_json` is **not written once and left alone** — it's
mutated afterward by at least three other writers (the trailing-stop
poller, the OCO pair watcher, and `/retry-template`), and it also **is**
the attach idempotency key (`_opp_load_row_for_attach` checks
`attached_gtts_json IS NOT NULL`). This means:

- If the JSON write and the new row-insert share a transaction, and the
  insert fails, the JSON write rolls back too — the idempotency key is
  gone, and a retry (a chase/postback race, or a manual Re-attach click)
  can **place duplicate live GTTs or a duplicate wing order**. **The JSON
  write must commit first, and the new rows must be written in a
  separate transaction afterward — never the reverse.**
- The three writers that mutate `attached_gtts_json` after the fact need
  to *also* update the new first-class rows, or those rows go stale
  immediately. This is real incremental work in Sprint 1, not something
  that rides along automatically.
- A drift check (comparing JSON entries against row entries periodically)
  is needed precisely because both can now diverge in either direction.

### 7.4 Status collisions — a required pre-ship checklist, not a note

New `armed` (GTT rows) and `draft` (draft rows) statuses must not be
misread as "a normal resting order needing action" by any existing
status-based scanner. The audit found **five** that need to be checked
before this ships: the open-order watchdog, admin reconcile, chase
recovery-on-restart, the postback fuzzy-match fallback (OPEN + NULL
broker_order_id in the last 60s), and strategy capacity sums (which
currently have **no mode filter at all**). Each one needs an explicit
pass/fail check against the new status values before Sprint 1 is
considered done — this is a checklist, not a single sentence of
reassurance.

### 7.5 Retention already exists, and contradicts a "full audit trail" goal

`algo_order_events` already purges after **90 days**
(`retention.algo_order_events_days`, `background.py:5232-5256`);
`algo_events` after 30. `audit_log` is kept 365 days, explicitly citing
SEBI's 8-year retention expectation. **A 90-day purge on the very table
this proposal makes the backbone of the audit trail directly contradicts
any long-term compliance ambition** — this is a real tension for the
operator to resolve (extend retention with its storage-cost tradeoff, or
accept that the "unified audit trail" is a 90-day rolling window, not a
compliance-grade archive), not something to leave implicit.

**Decided**: keep the existing 90-day retention. The unified audit trail
is a 90-day rolling operational window, not a long-term compliance
archive — no retention extension in Sprint 1.

## 7.6 Concrete NULL-handling requirements for Sprint 1b specifically

§7's cross-cutting rule, made concrete for the specific things Sprint 1b
(not yet planned in detail) will actually build — these must be designed
in from Sprint 1b's first draft, not discovered after:

- **Chase session recovery on old orders.** When chase resumes a session
  on an order whose `chase_session_id` is `NULL` (any order created
  before Sprint 1a shipped, or created after but never previously
  chased), the recovery path must treat `NULL` as "no session yet" and
  generate a new one — not crash on a missing id, not assume one already
  exists.
- **GTT triggers on pre-Sprint-1b exits.** A GTT attached before Sprint
  1b ships exists only inside `attached_gtts_json`, with no first-class
  row and no `oco_pair_id`/`gtt_order_id`. If that GTT triggers *after*
  Sprint 1b ships, the trigger-handling code must fall back to the
  legacy JSON-only path for that specific order — it cannot assume every
  GTT has a first-class row just because new ones will.
- **Event attribution on historical rows.** Any event-writing logic that
  attributes an event to an agent/source by reading `AlgoOrder.agent_id`
  or `.source` must handle both being `NULL` on an old row gracefully
  (write the event, mark attribution as unknown) — never skip writing
  the event entirely just because attribution is missing.
- **Status-scanner checklist (§7.4) applies to NULL-column rows too**,
  not just the new `armed` status value — e.g. admin reconcile and chase
  recovery must not treat "row has no `chase_session_id`" as itself a
  signal of anything broken; it's the expected state for most rows for a
  long time after this ships.

## 8. What this sprint does NOT do (and what moved INTO scope since v2)

- Does not change chase's pricing/retry logic, template exit mechanics,
  or holdings/positions P&L math — logging only, as before.
- **Does not consolidate `agent_events`/`audit_log`/`algo_events`** — see
  §1, explicit scope-out.
- **Does not solve GTT-trigger-to-row linking** (§3.1) or **Dhan MCX GTT
  support** (§3.1) — both are open design questions, not shipped in
  Sprint 1.
- **Moved INTO required scope** (not deferred, because this proposal's
  other pieces depend on them being correct): fixing
  `_fetch_net_position_qty` (§3.4), the event-queue per-row-fallback fix
  (§2.3), and the §7.4 status-collision checklist.
- Does not build the polished audit-trail screen UI — a debug "show me
  events for this order, following the parent/OCO chain" view remains a
  reasonable smoke test before that's built.

## 9. Open questions and risks requiring explicit operator sign-off

1. **SEBI `algo_id` — technical design decided, compliance timeline still
   open.** **Decided**: `algo_id` is sourced from a single settings
   placeholder (e.g. `compliance.algo_id`, same DB-backed settings
   pattern already used for `performance.refresh_interval`), read
   consistently everywhere an order needs to carry it — not hardcoded
   per call site. It starts empty; the day a real ID exists, one settings
   update propagates it everywhere.

   **Still genuinely open, and this placeholder doesn't resolve it**: the
   "mandatory from April 2026" deadline cited in earlier drafts has
   already passed as of today (2026-10-03), no broker adapter has any
   algo-id parameter today, and §2's internal-partner-platform context
   (see `project_internal_partner_platform` memory) means it's not yet
   clear whether/how this compliance framework even applies to a closed
   partner-only tool. Building the placeholder mechanism is not the same
   as resolving actual compliance exposure — that remains a real question
   for whoever handles compliance for the LLP, independent of this
   proposal's timeline.
2. **Retention vs. audit-trail ambition** (§7.5) — **Decided: keep the
   existing 90-day purge.** The audit trail is a 90-day rolling window by
   design, not a long-term archive.
3. **CNC same-day cross-exchange netting** (§2.1/§4) — **Resolved.**
   Position state already comes from position-level fields (open order
   qty, current qty, average price, current price), decided in prior
   research — doesn't depend on resolving exchange-netting mechanics.
   This proposal adds order-level traceability on top, not a competing
   source of truth.
4. **`oco_pair_id` shape** — confirmed broker-dependent (§3.1): mostly
   unneeded (Kite/Dhan native OCO is one row), needed only for Groww.
   What happens if the second Groww leg fails to place after the first
   succeeds has no compensation story yet — the existing OCO watcher is
   already best-effort and can leave stale entries on a failed cancel;
   Sprint 1 should at minimum reuse the existing CRITICAL-alert path for
   partial GTT placement rather than inventing a new one.
5. **`chase_session_id` across a restart-recovery** — chase recovery
   after a process restart resumes chasing the same row; undecided
   whether that gets a new session id or continues the old one.
6. **GTT-trigger linking mechanism per broker** (§3.1) — genuinely
   unsolved, needs its own design pass before implementation.
