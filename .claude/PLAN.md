# Plan: Hedge leg (Chain + DSL), unhedged-short alert agent, and the still-missing expiry-close release mechanism

## Status (updated 2026-11-25)

| Part | Status | Evidence |
|---|---|---|
| **A — Hedge leg (Chain + DSL)** | ✅ **DONE, shipped to main** | `aa5955d3` (Chain checkbox/staging), `c013ff7f` (`hedge_pct` DSL param + concurrent chase) |
| **C — Expiry-close release mechanism** | ✅ **DONE, shipped to main** | `33a2afea` (Wave 1 implementation) + the 20-finding follow-up audit (`d00a860b` … `f010cd02`, CC-gate cleanup `39e9ee05`) — all merged `workshop`→`dev`→`main` |
| **B — Unhedged-short-option 75% alert** | ⏸ **DEFERRED — not started** | Operator instruction 2026-11-25: "defer part b". No `position_pairing.py`, no `waterfall_pair()`, no new metric/agent exist yet. Resume when asked; design below is unchanged and still the agreed approach. |

`workshop`/`dev`/`main` are all in sync at the same commit as of this status update — Parts A and
C are live in production. The sections below are kept as the historical design record and as the
still-current design for Part B whenever work resumes.

## ⚠️ Deadline — Part C is live within 3 days, real money, no-fail

Operator: "exp close is going to be used in next 3 days. make sure, it works with no fail." This
is not a soft priority signal — the next real expiry day this mechanism must correctly handle is
imminent. Consequences:

- **Sequencing changes: Part C ships alone, first, fully verified and deployed to prod — before
  Part A or Part B implementation even starts.** Parts A and B are not time-critical and must not
  share a commit, a test run, or a deploy window with Part C. See the resequenced `## Agents` and
  `## Commit messages` sections below.
- **Part C's own verification bar is raised beyond "tests pass".** In addition to the unit/
  integration tests already specified, Part C needs, before the operator is told it's ready:
  1. A frozen-clock, fully end-to-end test that drives the REAL `expiry-day-itm-auto-close` agent
     condition → the REAL `close_positions()` → a HELD row with `agent_id` + `auto_release_at` set
     → simulated time passage → the REAL (widened) auto-release scan → a REAL `release_held_order()`
     call → a (mocked-broker) chase placement. Not a unit test of each piece in isolation — one
     test that proves the whole chain actually connects.
  2. A direct live-DB check on BOTH dev and prod, after deploy, confirming
     `expiry-day-itm-auto-close`'s row has `trade_mode='live'` (not `'paper'`) and `status='active'`
     — CLAUDE.md already documents a real prior incident where this exact agent silently shipped
     in paper mode because the builder function never read a `trade_mode` key at all ("`expiry-day-
     itm-auto-close` silently ran in paper mode on its first live trading day", 2026-10-31). Don't
     assume this is fine this time either — check it directly, the same way that incident was
     eventually caught.
  3. Confirm the `hold.expiry_close_released`/`expiry_close_hold_enabled` admin setting's current
     value on prod matches what the operator actually wants for the real expiry day (default is
     HELD — inverted naming, see `admin.py`'s own inline doc) — surface this explicitly rather than
     assuming.
  4. Confirm which exchange(s)/contracts actually expire within the 3-day window (query the real
     holiday/expiry calendar — don't guess) so the test scenarios in (1) match the real symbols/
     exchange(s) this will run against.
- **Do not fold Part C's commits into the same `/depl` run as Parts A/B.** Ship Part C through its
  own complete `/impl` → `/ddev` → `/dprod` cycle, confirm live, THEN start Part A/B.

## Context

Three requests from the same conversation, explicitly asked to be unified into one plan with
cross-reuse audited (operator: "go through this and previous hedge requirements, exp close
requirements, make sure code is reusable for functionality, agents, etc. audit the proposed
changes and existing agent functionality and dsl and come up with a robust plan"):

- **Part A — Hedge leg, at order-entry time, no agent.** On the Chain tab, a "Hedge" checkbox
  auto-stages a second leg (same option type, further OTM, opposite side) whenever the operator
  adds a BUY or SELL option leg — BUY hedges with a SELL at a strike priced ≈50% of the bought
  premium (debit spread); SELL hedges with a BUY at a strike priced ≈25% of the sold premium
  (credit spread). Both legs show in the basket before submit, both chase at the same
  aggressiveness, never applies to a close order. Needs a DSL-expressible equivalent.
- **Part B — Alert when a SOLD option has no hedge and has captured 75% of its premium.**
  Operator: "if any sell position does not have offset leg, when it reaches 75% of profit, it
  should send an alert including ntfy alert... check if you can use an existing action agent...
  tagged correctly... sent to email, telegram and ntfy." No close, no hold — alert only.
- **Part C — The expiry-close hold/auto-release mechanism is still not built.** This is NOT the
  same work as the 2026-11-22 retiming/morning-preview/double-close-guard amendment already
  shipped (confirmed live in the code — see below). The actual release mechanism this whole
  thread started with ("release it 30 mins if user does not respond... whatever it takes") is
  **still missing**, confirmed by direct `grep`/read against the current code, not by trusting
  CLAUDE.md's historical entries alone.

### Audit: what Part C actually still needs (verified against the live tree just now)

| Item | Status | Evidence |
|---|---|---|
| Legacy `_task_expiry_check` still calls `engine.run()` | **NOT fixed** | `background.py:2991` |
| Generic hold auto-release policy resolver | **Never built** | no `_resolve_hold_auto_release_at` anywhere |
| Auto-release scan covers `expiry_close` category | **NOT fixed** | `background.py:6051` still `if rec.get("category") != "agent_close": continue` |
| CANCEL_FAILED verified via real broker truth | **Never built** | no `get_broker_order_truth` call in `expiry.py` |
| MCX netting excludes NTM partners | **NOT fixed** | `_best_opt_partner`/`_best_fut_partner` (`expiry.py:106-170`) still pick by type+sign only, zero moneyness check |
| Futures-partner contract-identity check | **Never built** | `_fetch_future_positions` has no `lookup_future_for_option()` check |
| `price_policy` tag on expiry-close holds | **Never built** | zero occurrences of `price_policy` in `expiry.py` |
| 65/60-min lead windows, morning-preview agents, in-session double-close dedup | **Already shipped** | `metric_is_expiry_preclose_alert_window.py`, `agent_engine.py` (`expiry-day-{mcx,nse}-morning-preview`), `expiry.py:205 _exp_leg_in_flight_this_session` |

So Part C's scope is narrower than the original draft of this plan (no new Stage-A/B agents —
those are superseded by what already shipped) but the actual safety-critical gap (a staged
expiry-close hold that nobody ever releases) is real and unaddressed. This part wires the
**existing** `expiry-day-itm-auto-close` agent's holds into a real auto-release path.

## Cross-cutting reuse audit (what's shared, what's deliberately NOT)

- **Part A's close-intent check** reuses the EXACT same detector shipped earlier today for the
  Chain spread-gate fix (`frontend/src/lib/data/spreadGate.js`, backed by
  `portfolioStore.positions.rows` client-side; `_verify_close_intent`,
  `backend/api/routes/orders_place.py`, server-side). No new close-detection logic anywhere.
- **Part A's chase-priority requirement** is already satisfied by existing basket mechanics —
  `addOptionToBasket` already stamps an explicit `chaseAgg` on every leg; the hedge leg just
  copies the parent's value. No new chase-priority mechanism.
- **Part A and Part C do NOT share a pairing/netting algorithm**, and must not be unified into
  one: Part A's strike search is a *forward-looking, premium-percent match among currently quoted
  strikes* (order-entry time). Part C's netting (`_best_opt_partner`/`_best_fut_partner`) is a
  *backward-looking, theta-priority match among currently held positions* for MCX settlement risk.
  State this explicitly in code comments on both sides so a future reader doesn't try to merge them.
- **Part B's "does this position have a hedge" check is NEW shared infrastructure**, extracted
  from `_auto_pair_positions()` (`backend/api/routes/positions.py`) — that function currently
  operates on route-layer `PositionRow` msgspec structs. Extract its core waterfall-matching
  algorithm (group by `(account, root_symbol, option_type)`, waterfall-match opposite-sign rows
  by quantity, label orphans) into a new shape-agnostic helper,
  `backend/api/algo/position_pairing.py:waterfall_pair(rows: list[PairingRow]) -> PairingResult`,
  where `PairingRow` is a minimal `(id, account, root, option_type, quantity)` tuple/dataclass.
  `_auto_pair_positions()` becomes a thin adapter (`PositionRow` → `PairingRow` → call shared
  helper → write `pair_group_key`/`paired_qty`/`orphan_qty`/`is_orphan` back onto the real rows).
  The new Part B metric resolver becomes a second, independent adapter
  (`ctx.position_rows` dicts → `PairingRow` → same shared helper). **One algorithm, two thin
  call sites** — this is the concrete reuse the operator asked to verify exists.
- **Part B's gain-from-entry math reuses `metric_gain_from_entry_pct.py`'s existing
  `_coerce_positive_finite_float`/`_DEGRADED_PRICE_SOURCES`**, same import `metric_short_option_
  gain_from_entry_pct.py` already does — no new price-degradation logic.
- **Part B's alert channels reuse the exact `events` list shape** already seeded on
  `exit-tp-short-option-90` (`agent_engine.py:3227-3232`: telegram + email + log + ntfy at default
  priority) — this directly answers "check if you can use an existing action agent": yes, the
  *shape* (condition/tier/schedule/events) is fully reused; only the metric and the (empty)
  actions list differ.
- **Part B does not touch the existing 90% close agent.** The two are layered, not merged: 75%
  unhedged → soft alert (prompt the operator to hedge or close); 90% any short → stages a close
  for operator review (existing, unchanged). An unhedged position that's ignored still gets the
  90% safety net it already has today.
- **Part C's hold/release resolver is genuinely reusable beyond expiry** — this was the
  operator's own mid-task instruction earlier in this session ("this kind, hold release cancel
  retry behavior may be common pattern for other agents... should be implementable using agent
  DSL using reusable code"). Built once in `order_hold.py`, callable by any future
  `require_release`-shaped action, not just expiry.
- **Naming collision, confirmed real, called out for every implementer**: "offset" already means
  *a closing order* in this codebase (`_is_offsetting_position`/`_verify_close_intent`). "Wing"
  already means the deleted Bracket/Template-GTT leg-attachment system. Part A's new leg is a
  **"hedge leg"** everywhere — code, comments, DSL param, tests. Never "offset leg", never "wing".

---

## Part A — Hedge leg (Chain tab + DSL) — ✅ DONE (`aa5955d3`, `c013ff7f`)

### Design (from dedicated Plan-agent design pass, file paths and line numbers verified against
the live tree)

- **Naming**: "hedge leg" throughout. New JS functions `resolveHedgeStrike`/`_maybeStageHedgeLeg`;
  new Python `resolve_hedge_strike`/`_al_resolve_hedge_plan`. New leg fields `_hedgeParentKey`/
  `_hedgeParentSig`. Do **not** reuse `resolveWingTradingsymbol()` (`spreadGate.js`) — that's the
  deleted Bracket system's manual-points wing resolver, solving a different problem.
- **UI**: a toolbar-level "HEDGE" checkbox in `OptionChainTab.svelte`'s `.oct-toolbar`, next to the
  existing Spread% field (same construction pattern, lines ~1109-1122), plus two editable percent
  inputs (`hedgeBuyPct` default 50, `hedgeSellPct` default 25) shown only while Hedge is on.
  Local `$state`, not lifted to `SymbolPanel` — Chain-only, no cross-tab sync need. Switching tabs
  away and back resets the toggle to off (component remounts, `{#if _activeTab==='chain'}`) —
  accepted v1 behavior, does not retroactively touch legs already staged.
- **Strike search — `resolveHedgeStrike()`, new pure function in `spreadGate.js`** (JS) and
  **`resolve_hedge_strike()`, new file `backend/api/algo/hedge_strike.py`** (Python), a
  deliberately mirrored pair matching the existing `evaluateLegSpread()`/`evaluate_spread()`
  precedent. Given a parent strike/option-type/premium and a target %, search strikes further OTM
  in the correct direction (CE: ascending above parent; PE: descending below parent) among
  currently-quoted strikes only, price each candidate by the hedge leg's own side convention (BUY
  reads `ask`, SELL reads `bid`), reject any candidate with no real depth-backed quote, and return
  the nearest-to-target match within a tolerance band (default ±50% of the target value — e.g.
  target 50% accepts an actual ratio anywhere in [25%, 75%]). No match within tolerance, no quotes
  loaded, or invalid inputs → a structured "not ok" result with a human-readable reason; never a
  silent fallback to a wrong strike.
- **Wiring**: `addOptionToBasket()` keeps its existing push unchanged, then calls
  `_maybeStageHedgeLeg(parentLeg, strike, optType)`, which: no-ops silently if Hedge is off, if
  `_placeMode` is on (no basket to stage into — ticket mode bypasses Hedge entirely, documented in
  the checkbox tooltip), or if the symbol's exchange isn't NFO (MCX/other — silent no-op, no
  warning, out of scope for v1); returns silently (no warning) if the leg being added is verified
  as closing an existing position (reuse `isCloseIntentLeg`/`portfolioStore.positions.rows`, same
  detector as the spread-gate fix) — closing is the expected, non-alarming case Hedge should never
  react to; otherwise resolves the hedge strike and either stages it via the exact same
  `_pushToBasket` shape manual legs use (1:1 lot ratio with the parent — the only ratio that keeps
  the structure risk-defined; no ratio input in v1), or sets `basketError` with the reason and
  leaves the parent leg exactly as already staged (fail-safe, never fail-open, per the operator's
  explicit "any issue here show warning... don't select offset position").
- **Guard `_mergeIntoBasket`/`_netAgainstBasket`** (lines 596-597, 620-622 of
  `OptionChainTab.svelte`) to exclude any leg carrying `_hedgeParentKey` from being a match
  *target* — otherwise a manual click on the same strike+side a hedge leg occupies would silently
  corrupt the hedge leg's ratio via merge, or wrongly net it away.
- **Staleness — remove, never silently recompute.** A new `$effect` (same shape/placement as the
  existing `quoteArrived`-sync effect) watches `chainBasket`: for every leg carrying
  `_hedgeParentKey`, if its parent leg is gone, or the parent's own
  `${sym}|${side}|${lots}|${account}` signature no longer matches what was snapshotted at staging
  time (`_hedgeParentSig`), remove the hedge leg. This is the literal, faithful reading of "offset
  leg should disappear" — it does **not** re-stage automatically; the operator must re-trigger
  (remove+re-add the parent, or add a fresh leg) to get a new hedge at the updated ratio. Flagged
  explicitly as a real usability cost worth watching after ship, not silently smoothed over.
- **DSL equivalent**: a new optional `hedge_pct` param on the EXISTING `order`/`place_order`
  tokens (`backend/config/grammars/agent_grammar.yaml`, both blocks, kept byte-identical per their
  existing intentional-duplication convention) — not a new token, since that would triplicate an
  already-duplicated schema. Resolved by `_action_place_order` (`actions_live.py`): when
  `hedge_pct` is set and the symbol is an NFO option, resolve the hedge leg via
  `_al_resolve_hedge_plan()` (new helper — reuses `_verify_close_intent`, then
  `backend/api/algo/hedge_strike.py`'s `fetch_hedge_quotes()`, which itself reuses
  `options.py`'s existing chain-quote-building helpers so the Python candidate set is premium-
  basis-identical to the frontend's `chainQuotesMap`), then launches BOTH the parent's and the
  hedge's `chase_order()` calls **concurrently** via `asyncio.gather` — a sequential await would
  leave the position naked for the parent's entire chase window, defeating the hedge's purpose.
  Same `chase_level`/aggressiveness on both. A hedge-resolution failure (no match, closing order,
  non-option symbol) never blocks the parent order — it silently proceeds unhedged, same fail-safe
  rule as the frontend. **Decision** (resolving the design pass's two open questions directly,
  since full autonomy was given): (a) the backend's close-intent check will treat ANY failure to
  positively confirm "not a close" (including a position-fetch failure) as "treat as potential
  close, skip hedge" — i.e. `_al_resolve_hedge_plan` wraps its own call in a broad try/except that
  defaults to `None` (no hedge) on any exception, matching the frontend's conservative default,
  rather than trusting `_verify_close_intent`'s own fail-open convention uncritically; (b) if the
  hedge leg's own chase fails after both were launched, the action still reports its PARENT result
  as the overall success/failure signal (the parent placing correctly is what matters most), with
  the hedge failure logged and surfaced via the existing `self_alerted`-style WARNING convention
  (not re-raised, not silently dropped) so the operator sees a naked, intended-to-be-hedged
  position promptly — the same gap Part B's alert independently exists to catch.
- Golden baseline (`backend/tests/golden/agent_grammar_baseline.json`) must be regenerated after
  the `hedge_pct` schema addition.

### Files to change (Part A)

- `frontend/src/lib/data/spreadGate.js` — `resolveHedgeStrike()`.
- `frontend/src/lib/order/OptionChainTab.svelte` — checkbox + inputs, `_maybeStageHedgeLeg`,
  removal `$effect`, merge/net exclusion guard.
- `backend/config/grammars/agent_grammar.yaml` — `hedge_pct` param on `order` and `place_order`.
- `backend/api/algo/hedge_strike.py` (new) — `resolve_hedge_strike()`, `fetch_hedge_quotes()`.
- `backend/api/algo/actions_live.py` — `_action_place_order`'s hedge branch, `_al_resolve_hedge_plan`.
- `backend/tests/golden/agent_grammar_baseline.json` — regenerate.

---

## Part B — Alert: unhedged short option at 75% captured — ⏸ DEFERRED (not started)

### Design

- **New shared pairing helper** (see cross-cutting section above): extract `_auto_pair_positions`'s
  waterfall algorithm into `backend/api/algo/position_pairing.py:waterfall_pair()`, operating on a
  minimal row shape; `positions.py` and the new metric both adapt to it.
- **New metric token** — `unhedged_short_option_gain_from_entry_pct`
  (`backend/api/algo/grammar_tokens/metric_unhedged_short_option_gain_from_entry_pct.py`): same
  qty<0 + CE/PE-type + degraded-price-source checks `short_option_gain_from_entry_pct` already
  folds in (reuse its logic directly, don't re-derive), PLUS a fold-in of "is this row's pairing
  group fully matched (hedged)" via `waterfall_pair()` run once over `ctx.position_rows` — returns
  the gain% only when the row is a genuine, unmatched short option (orphan in its
  `(account, root, option_type)` group); returns `None` otherwise. **This single-leaf fold-in is
  required, not a style choice** — `agent_evaluator._eval_all`'s cross-row join over a fan-out
  scope only recognises `acct`/`und` join keys, so expressing "short AND unhedged AND gain>=75" as
  three separate `all[]` leaves would wrongly let one leaf's match leak onto a different row in the
  same account (the exact, already-documented, confirmed Sprint-1 finding behind
  `short_option_gain_from_entry_pct`'s own existing design — same fix, same reason, applied here).
- **New seeded agent**, cloned in shape from `exit-tp-short-option-90`
  (`agent_engine.py:3192-3233`) but: `slug: "alert-unhedged-short-option-75"`, single-leaf
  condition `{"op": ">=", "scope": "positions.any_symbol", "metric":
  "unhedged_short_option_gain_from_entry_pct", "value": 75}`, `"actions": []` (alert only, no
  close, no hold), `"tier": "high"` (an unprotected position nearing max profit carries real
  asymmetric downside on any adverse move — more actionable than a routine FYI, chosen since this
  alert exists specifically to prompt operator action), same `events` list as the 90% agent
  (telegram + email + log + ntfy@default) — this is the direct, confirmed reuse of an existing
  action-agent *shape* the operator asked me to check for. `cooldown_minutes: 0` (mirrors the 90%
  agent; per-leaf latch/escalation in the alert engine handles re-fire pacing, not the agent-level
  cooldown). New agent → plain insert, no `SEED_VERSION` bump needed (same precedent as the
  morning-preview agents).
- Tagging/channel routing is automatically correct because this reuses the exact same
  `_v2_send_rich_alert` cycle-agent pipeline every other seeded agent already goes through — no new
  tagging logic to write.

### Files to change (Part B)

- `backend/api/algo/position_pairing.py` (new) — `waterfall_pair()`, extracted from
  `_auto_pair_positions`.
- `backend/api/routes/positions.py` — `_auto_pair_positions()` becomes a thin adapter over the new
  shared helper; **no behavior change**, existing tests must still pass unmodified.
- `backend/api/algo/grammar_tokens/metric_unhedged_short_option_gain_from_entry_pct.py` (new).
- `backend/api/algo/agent_engine.py` — new seeded agent dict, appended to `BUILTIN_AGENTS`.

---

## Part C — Expiry-close: build the actual release mechanism (still missing) — ✅ DONE (`33a2afea` + 20-finding audit)

### Design

- **Retire the legacy race.** Remove `_task_expiry_check`'s `await engine.run()` call
  (`background.py:2991`) — confirmed via grep that its only broadcast (`expiry_day_detected`) has
  zero consumers anywhere. Add a defense-in-depth refusal inside `close_positions()`
  (`expiry.py`) itself: refuse to stage a hold when `agent_id` is `None` (log + alert), so no
  future caller can silently reproduce this exact bug.
- **Generic hold auto-release policy resolver** — `_resolve_hold_auto_release_at(policy, now,
  close_time, held_at, default_chase_tier)` in `order_hold.py`, taking a DSL-expressible policy
  dict (`{"before_close_min": N}` fixed deadline, or `{"after_min": N, "floor_tier": "high"}`
  rolling-clamped-to-floor), where the floor reads the same L/M/H tier table
  `orders_helpers._live_chase_config` uses — not `ExpiryEngine`'s own chase config, since
  `release_held_order()` actually resumes via `_chase_level_from_price_policy(hold_json
  ["price_policy"])`, a different lookup. Genuinely reusable beyond expiry, per the operator's own
  earlier instruction on this exact point.
- **Wire it to the agent that already exists** — `expiry-day-itm-auto-close`'s own action
  (`_action_live_expiry_auto_close`, `actions_live.py`) computes a policy dict per stage and calls
  the resolver, passing the result into `record_held_order(..., auto_release_at=..., price_policy=
  "CHASE_HIGH")`. No new agent is created — the real system already retimed the existing agent's
  firing windows (65/60-min lead) in the 2026-11-22 amendment; this slots the release computation
  into that already-correct timing, it does not re-architect the timing itself.
- **Widen the auto-release scan** — `_task_agent_close_auto_release` → cover
  `{"agent_close", "expiry_close"}`, dispatching via `get_release_handler(rec.get("category"))`
  instead of the hardcoded string, so a third future category needs only a one-line addition.
- **CANCEL_FAILED verified via real broker truth** — before treating a `CANCEL_FAILED` expiry-close
  leg as permanently blocked, check `orders_helpers.get_broker_order_truth()`: confirmed gone →
  unblock; confirmed still resting with no live chase → alert as stale, don't silently skip;
  truth unavailable → stay blocked and alert. Never silently guess.
- **MCX netting must exclude NTM partners** — in `_best_opt_partner`/`_best_fut_partner`
  (`expiry.py:106-170`, confirmed to have zero moneyness awareness today), skip any candidate B (or
  refuse A) whose own `moneyness == "NTM"` — an NTM leg's settlement fate is uncertain and must
  never be used as a netting partner, for an option or a future. The existing both-strictly-ITM
  cross-strike guts case (CLAUDE.md's pinned `test_mcx_unhedged_nets_guts_position_old_heuristic_
  flagged`) must keep passing unmodified — same-strike or both-ITM-beyond-buffer stays a valid
  zero-residual pair; only an NTM candidate is excluded as a partner.
- **Futures-partner contract identity** — `_fetch_future_positions` currently accepts any `FUT` row
  with no expiry/contract check; filter to the option's actual `derivatives.lookup_future_for_
  option()`-resolved contract so an option is never netted against an unrelated future.

### Files to change (Part C)

- `backend/api/background.py` — remove the legacy `engine.run()` call; widen
  `_task_agent_close_auto_release`'s category check and dispatch.
- `backend/api/algo/order_hold.py` — `_resolve_hold_auto_release_at()` (+ a tier-floor helper
  reading `orders_helpers._live_chase_config`'s table — watch for an import cycle; if one exists,
  move the tier table itself to a lower-level shared module both files import from).
- `backend/api/algo/expiry.py` — `_best_opt_partner`/`_best_fut_partner` NTM exclusion;
  `_fetch_future_positions` contract-identity filter; `close_positions()` policy-dict + resolver
  call + `agent_id`-required refusal; CANCEL_FAILED broker-truth check.
- `backend/api/algo/actions_live.py` — `_action_live_expiry_auto_close` computes and passes the
  policy dict.

---

## Agents

**Wave 1 — Part C ONLY. Ships, tests, and deploys to prod completely before Wave 2 starts.** ✅ DONE.

- **backend**: implement Part C in full, per its Design/Files section above — the legacy-task
  removal + defense-in-depth `agent_id` refusal, the generic hold-release resolver, wiring it into
  `expiry-day-itm-auto-close`'s own action, the widened auto-release scan, the CANCEL_FAILED
  broker-truth check, and the MCX netting NTM-exclusion + futures-contract-identity fixes. Given
  the explicit "live within 3 days, no fail" constraint: write pytest coverage for every changed
  function AND the one mandatory end-to-end test described in the Deadline section — a frozen
  clock driving the real condition → real `close_positions()` → real HELD row with `agent_id` +
  `auto_release_at` → simulated time passage → the real widened auto-release scan → real
  `release_held_order()` → a mocked-broker chase placement, asserted as ONE connected test, not
  independent pieces. Also: the NTM-partner-exclusion fix against BOTH the now-rejected
  NTM-as-partner case and the still-accepted both-ITM-cross-strike guts case from CLAUDE.md's
  existing pinned test (must keep passing unmodified).
- **audit**: read-only review of the Wave 1 diff specifically for the "no fail" bar — structurally
  unreachable paths, any place a broker-truth lookup failure could silently skip a leg that needed
  closing, any place `agent_id`/`auto_release_at` could end up unset on a real code path.
- After Wave 1's tests are green and the audit raises nothing blocking: run `/dprod` for Part C
  alone, then directly verify on the live prod DB (not assumed): `expiry-day-itm-auto-close`'s row
  has `trade_mode='live'` and `status='active'` (the exact prior incident this project already
  documents for this exact agent), and confirm the real `hold.expiry_close_released` setting value
  with the operator before relying on it.

**Wave 2 — Parts A and B, started only after Wave 1 is live and verified.** Part A ✅ DONE. Part B
⏸ DEFERRED — not yet started; the bullets below are the agreed design to pick up when resumed.

- **backend**: Part B — the shared `waterfall_pair()` extraction, the new metric file, the new
  seeded agent, per its Design/Files section. Write pytest coverage: `_auto_pair_positions`'s
  EXISTING test suite must still pass unmodified after the refactor; the new metric's
  cross-row-join-safety (a hedged short option in the same account as an unhedged one must never
  cross-contaminate).
- **frontend** (✅ done): Part A's frontend half — the Hedge checkbox + percent inputs,
  `resolveHedgeStrike()`, `_maybeStageHedgeLeg()`, the removal effect, and the merge/net exclusion
  guard. Write or update Vitest coverage: `resolveHedgeStrike()`'s direction/side-premium/depth-
  rejection/tolerance/tie-break cases; a source-grep wiring test (same convention as
  `optionChainTabSpreadGateWiring.test.js`) confirming `_maybeStageHedgeLeg` calls
  `resolveHedgeStrike`, never calls `_mergeIntoBasket`/`_netAgainstBasket` for a fresh hedge push,
  and that both of those functions' match predicates exclude `_hedgeParentKey` legs.
- **backend** (✅ done, sequenced after the Wave-2 frontend agent — needs `hedge_strike.py` to
  exist): Part A's backend half — the `hedge_pct` grammar param (both tokens), `hedge_strike.py`,
  `_action_place_order`'s hedge branch with concurrent `asyncio.gather` chasing, and the golden
  baseline regeneration. Write pytest coverage: `resolve_hedge_strike()`'s mirrored test cases; the
  no-`hedge_pct` regression guard (unchanged single-chase behavior); hedge-skipped-on-close-intent;
  hedge-skipped-on-no-match; both chases launched concurrently (not sequentially) when a hedge
  resolves.
- **doc** (outstanding — depends on Part B): once Part B ships, update CLAUDE.md — correct the
  "Expiry pre-close alert + auto-close" entry's implied-complete state if still stale, add entries
  for the Hedge-leg feature (if not already documented from Part A's ship) and the unhedged-short
  alert agent, and update the Common Tasks table with rows for all three.

## Tests

- pytest: yes
- svelte-check: yes
- playwright: no (Vitest + the source-grep wiring tests cover the frontend logic directly; add a
  spec only if an agent's own judgment says the DOM interaction genuinely can't be proven by
  Vitest alone)

## Commit messages (separate commits per standing one-issue-at-a-time rule; Wave 1 ships and
deploys completely before Wave 2's first commit)

**Wave 1 (Part C) — ✅ shipped as `33a2afea` + the 20-finding follow-up audit (`d00a860b` …
`f010cd02`, `39e9ee05`), all merged to `main`.**
1. ~~`fix(expiry): retire the legacy expiry-check task's close call — it staged unreleasable holds with no agent_id`~~
2. ~~`feat(expiry): generic hold auto-release policy resolver, reused by any future require-release-shaped action`~~
3. ~~`fix(expiry): wire expiry-day-itm-auto-close's holds to the new auto-release resolver and widen the release scan`~~
4. ~~`fix(expiry): verify CANCEL_FAILED legs against real broker truth instead of blocking them forever`~~
5. ~~`fix(expiry): MCX netting must never use an NTM leg, or a mismatched-expiry future, as a hedge partner`~~

**Wave 2 (Parts B, A — only after Wave 1 is live):**
6. `refactor(positions): extract the waterfall pairing algorithm into a shape-agnostic shared helper` — ⏸ not started (Part B deferred)
7. `feat(agents): alert when an unhedged short option captures 75% of its premium` — ⏸ not started (Part B deferred)
8. ~~`feat(order): auto-stage a hedge leg on the Chain tab when the Hedge checkbox is on`~~ — ✅ shipped `aa5955d3`
9. ~~`feat(order): hedge_pct DSL param on order/place_order, concurrent chase for the hedge leg`~~ — ✅ shipped `c013ff7f`

## Done when

- **Part A** (✅ done): adding a BUY/SELL option leg on the Chain tab with Hedge on stages a
  correctly priced, correctly sided second leg at the right tolerance, visible/editable/removable
  like any manual leg, never on a close, never on MCX; changing/removing the parent makes the
  hedge leg disappear; the same capability is callable via `order(..., hedge_pct=...)`/`place_order`.
- **Part B** (⏸ deferred, still outstanding): a genuinely unhedged short option crossing 75%
  captured premium fires exactly one alert, reaching telegram + email + ntfy, correctly
  identified/tagged; a hedged short option at the same gain level never fires it; the existing 90%
  close agent is untouched.
- **Part C** (✅ done): a position staged HELD by `expiry-day-itm-auto-close` and never touched by
  the operator is released and chased automatically before market close, with a correctly computed
  deadline; no HELD expiry-close row can ever lack an `agent_id` or `auto_release_at`; the netting
  fix is verified against the devolvement-driven NTM-as-partner failure case (confirmed to fail on
  the pre-fix code) AND the existing guts-hedge test keeps passing unmodified.
- Full pytest suite green; broker/api coverage gates hold; `_auto_pair_positions`'s existing tests
  pass unmodified after its refactor; full Vitest suite green. (All confirmed true for Parts A/C as
  shipped; still to be confirmed for Part B once it's implemented.)
