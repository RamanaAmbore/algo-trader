# Plan: Fix margin-calculation defects found across 3 audits

## Context

Three read-only audits (backend preflight parser, frontend preview-vs-submit consistency,
MCX/NCO lot-unit + buy/sell-side correctness) found real defects in the margin-requirement
checks that run before every order placement — the safety gate meant to stop an order from
executing when the account can't actually cover it. Operator asked to fix all of them, plus
specifically: MCX/NCO lot-vs-qty handled consistently across all brokers (**audited, confirmed
already correct** — no fix needed there, see Verified-clean below), and buy/sell margin
correctness (**found genuinely broken** for the two close-paths, see below).

I independently re-verified the two most severe findings by reading the current code directly
(not just trusting the agent reports) before writing this plan — both confirmed exactly as
described, with one of them (the `chase_close_positions` product hardcode) turning out to be
**more severe** than the audit's own framing: it's not just a margin-preview inaccuracy, it's a
hardcoded `ChaseConfig(product="NRML")` that reaches the REAL `chase_order()` call — closing a
MIS position this way would place the close order under the wrong product, which won't net
against the real position on the broker side, silently leaving it open while creating a stray
NRML position.

**This plan was reviewed by advisor before being written up** — several of the decisions below
(sequencing, the enforce-vs-log-only question, intent-verification scope, test design, frontend
fix direction) come directly from that review, not just my own first pass.

## Findings, by severity

### Critical — live bug today, ships first, independent of everything else

1. **`_action_live_close_position`** (`backend/api/algo/actions_live.py:789-795`) builds its
   preflight dict with no `side`/`transaction_type` key at all, even though the real resolved
   `side` is already in scope (line 784). `_parse_broker_call_fields` (`actions_preflight.py:771`)
   defaults missing side to `"BUY"`. Kite's `basket_order_margins` defaults to
   `consider_positions=True` (position-aware netting) — a SELL-to-close a long position correctly
   nets to near-zero/negative margin under that mode, but priced as the DEFAULTED "BUY" it looks
   like a brand-new long, requiring full fresh SPAN. **Confirmed live-impact scenario**: an
   agent-driven loss-limit close on Kite, with available margin already tight, gets wrongly
   blocked with MARGIN_SHORTFALL or INSUFFICIENT_FUNDS — refusing to close a position exactly when
   closing it matters most. Short-position closes (BUY to close) happen to work today only because
   the wrong default matches by coincidence.

2. **`_chase_build_tasks`** (`actions_live.py:1437-1451`, called from
   `_action_live_chase_close_positions`) has the identical missing-side bug in its own preflight
   dict (`side` is computed at line 1437 but never added to the dict at 1445-1451) — same incorrect
   margin pricing, same false-block risk, for every bulk/agent-driven chase-close.

3. **`_chase_build_tasks` also hardcodes `"product": "NRML"`** in the preflight dict (line 1448)
   AND in the real `ChaseConfig(exchange=exchange, product="NRML", ...)` passed to the actual
   `chase_order()` call (line 1472) — regardless of the real position's product. **This is the
   more severe of the two findings in this function**: for a MIS position, this doesn't just
   mis-price the preflight margin check, it places the real close order under NRML, which does
   NOT net against an MIS position on the broker side — the close silently fails to actually close
   anything, while a stray NRML position gets created and the original MIS position stays open.

### High — fix together, do not ship separately (per advisor review)

4. **`_preflight_leg_required`** (`actions_preflight.py:263-284`) only reads `entry["final"]
   ["total"]`, `entry["initial"]["total"]`, or the top-level `entry["required"]`. Dhan
   (`dhan.py:_margin_for_order`, line ~1316) and Groww (`groww.py`, line ~1150) both return a FLAT
   `{"total": ..., "var": ..., "exposure": ..., "available": {...}}` — none of the three keys the
   parser checks exist in that shape. **Confirmed by directly running the parser function myself**:
   `_preflight_leg_required({"total": 150000.0})` returns `0.0`. Net effect: `required` is always
   `0` for every Dhan/Groww order, on both buy and sell, every exchange — `INSUFFICIENT_FUNDS`
   needs `required > 0` to fire and can't; the shortfall computation (`max(0, required -
   available)`) is always `0` too. **Our own margin preflight gate is completely inert for 2 of 3
   brokers** — the broker's own RMS is still the real backstop, so this isn't unlimited exposure,
   but it's a silently-dead safety check.

5. **Dhan/Groww adapters break the missing-vs-zero convention** in their margin functions:
   `_dhan_num(None)` and Groww's `float(data.get("total_margin", 0) or 0)` both turn a genuinely
   absent field into `0.0` rather than `None` ("unknown"). Once item 4 is fixed, a field-name
   drift or a broker-side schema change would silently read as "zero margin needed" instead of
   failing visibly.

6. **Turning on the Dhan/Groww gate needs a close exemption, or it will start false-blocking real
   closes the moment item 4 ships** (this is why 4, 5, and this item must land together — advisor
   flagged this explicitly as a release-blocking dependency, not an optional nice-to-have). Kite's
   `basket_order_margins` nets automatically via `consider_positions=True`; Dhan's
   `margin_calculator` and Groww's `get_order_margin_details` price each order independently and
   have NO concept of existing positions. Once `required` stops being hardcoded to `0`, a
   genuinely risk-reducing close on Dhan/Groww would be priced as if it's a brand-new position in
   that direction — and could get blocked by a margin check that exists specifically to let closes
   through when margin is tight. Needed: a close-margin exemption gated on **server-verified**
   intent (reuse the existing `_verify_close_intent` machinery — sign AND magnitude checked against
   the real broker position, never the raw client/caller claim) applied uniformly across every
   `run_preflight` caller, not branched ad hoc per broker.

### Medium — frontend, real but fails closed (confirmed by the frontend audit)

7. **Template-preview (TP/SL/wing planning) sends quantity as CONTRACTS where the backend's
   `/orders/ticket/preview` endpoint expects LOTS** (`OrderTicket.svelte:2679`,
   `SymbolPanel.svelte:877`, vs. `OptionChainTab.svelte:1027` which correctly sends lots). The
   backend multiplies by `lot_size` again, inflating the planned wing quantity by a factor of
   `lot_size` (e.g. `lot_size²` contracts instead of `lot_size`). This skews the margin preview's
   "+wing" figure and the TP/SL preview chips for any templated/bracket order. **Does not affect
   real order placement** — `buildPlacePayload` already sends lots correctly; this is a
   preview-display bug, not a placement bug.

8. **The margin preview nets the wing (shows hedged/spread margin); the real submit-time preflight
   check does not** (`OrderTicket.svelte:2120-2146` sends `paired_legs`; `_ticket_run_preflight`
   calls `run_preflight` with no `paired_orders` — the wing is only placed on parent FILL, so naked
   margin is genuinely what the broker needs at placement time). The preview can show "sufficient
   margin" for a hedged position while submit correctly rejects the naked requirement. Fails
   closed (submit still enforces correctly) but is a real "two numbers for one fact" trust problem.
   **Per advisor: fix this by making the preview compute/show NAKED margin as the enforced figure
   — never by adding `paired_orders` to the real submit-time preflight**, which would loosen an
   actual real-money gate to match a display bug.

9. **Margin-preview reactivity gaps**: the preview's watcher list omits `_exchange`/
   `_resolvedExchange` (only watches the bare `exchange` prop), so switching the Exchange Select
   doesn't re-fetch the preview — same class of bug as the just-fixed `eae36fca` exchange-prop
   issue, in the preview path instead of the submit path. Separately, the wing used in the margin
   preview is read untracked (inside a `setTimeout`), racing the template-plan fetch — a strike/side
   change can pair the WRONG previously-fetched wing into the currently-shown margin number. And in
   bare-underlying mode, the template preview plans against the root symbol instead of the
   resolved contract.

### Lower priority — real, smaller, bundled in since explicitly requested

10. **`_preflight_margin_shortfall_fix_qty`** (`actions_preflight.py:~420`) only rounds its
    suggested reduced quantity down to a lot multiple for the MCX/NCO branch — the NFO/BFO/CDS
    branch returns a raw `int(available / per_contract)` with no lot-size rounding, so the
    suggested "reduce qty to N" can itself be a fractional-lot count that would trip the G1
    lot-multiple guard if the operator actually typed it in.

11. **`_basket_margin_validate`** (`actions_preflight.py:~50`, paper-mode dry-run path only, low
    severity) hardcodes Kite's `to_kite_qty` regardless of broker — for a Groww MCX leg this sends
    the margin endpoint a lots-shaped qty while Groww's real order sends contracts, undercounting
    margin by a factor of `lot_size` in the paper check only.

12. **`RemoteBroker` has no `validate_gtt_exchange` override** (`backend/brokers/client/
    remote_broker.py`) — in conn-service mode (prod), `apply_plan_live`'s gate falls back to the
    base no-op. Dhan's own adapter still refuses MCX/NCO GTT internally, but Groww's adapter has no
    internal refusal, so in prod a templated MCX fill on a Groww account could attempt a Groww GTT
    that should have been gated out earlier. Touches `backend/brokers/` — triggers the broker
    coverage gate and a conn-service restart on deploy, so this gets its own commit.

## Verified-clean (confirmed correct, no fix needed)

- **MCX/NCO lot-vs-contract units are consistent across all 3 brokers, for both margin requests
  and real order placement** — both paths go through the same `broker.translate_qty`/
  `normalise_qty` call, confirmed with a concrete table (1 lot CRUDEOIL, lot_size 100: Kite margin
  qty=1/order qty=1, Dhan margin qty=1/order qty=1, Groww margin qty=100/order qty=100 — correct
  per-broker convention on both sides, no double-conversion anywhere).
- **Side (buy/sell) correctly reaches each broker's margin API** everywhere EXCEPT the two close
  paths above (items 1-2) — ticket, basket, the agent `place_order` action, and `/preflight`
  preview all thread `transaction_type` through correctly today.
- **The `required < 0` skip branch** (Kite's position-netting credit) cannot be accidentally
  triggered by a mis-signed short-sell margin figure — nothing in our own code applies `abs()` or
  flips a sign; it only fires on a genuinely negative broker-returned figure, which only Kite's
  nested shape can produce (Dhan/Groww's flat shape can't reach it either way).
- **NCO fails closed correctly today** on Dhan (not in `_EXCHANGE_TO_DHAN`, raises) and effectively
  on Groww (`get_lot_size` returns 0 for an uncached instrument, `translate_qty` raises).

## Approach

### Sequencing (per advisor review — follow this order, each step is its own commit)

**Phase 1 — ships alone, first, independent of everything else below:**
- Items 1 + 2 + 3 together (all three are in the same two closely-related functions in
  `actions_live.py`, and item 3's `ChaseConfig` fix must land with item 2's preflight-dict fix —
  fixing only the side without fixing the product would still false-block MIS closes). Read the
  real product off the position row (`p.get("product")` in `_chase_build_tasks` — confirm the
  actual column name the live `context['df_positions']` DataFrame carries before wiring it in;
  fall back to `"NRML"` only if genuinely absent, preserving today's behavior as a fallback, not a
  silent default).

**Phase 2 — the Dhan/Groww margin-gate activation, items 4+5+6 together, in that internal order:**
- Fix the missing-vs-zero adapter bug (5) and the parser (4) in the same pass — read the flat
  `total` only when the nested Kite-shaped keys are absent; a missing/unparseable value becomes
  `None` (unknown), not `0`; a multi-leg sum containing any `None` leg is itself unknown (fail
  open on the WHOLE basket, not a silent partial sum); keep each per-leg error placeholder's error
  string intact so the existing keyword-matching fallback branch can still fire.
- Add the close exemption (6) gated on `_verify_close_intent`, applied at the one shared
  `run_preflight`/parsing layer so every caller gets it uniformly — **before** enabling (4) to
  read real numbers, not after. List every `run_preflight` caller first (ticket, basket, the agent
  `place_order` action, the two close paths from Phase 1, `/preflight` preview) and confirm none of
  them pass the RAW, unverified client-claimed intent into a path that could reach this exemption
  — if `/preflight` (preview-only, read-only) does, that's fine; if any LIVE-placement caller does,
  that's a margin-bypass bug of the same class CLAUDE.md's "Verified-intent propagation" entry
  already documents, and must be fixed as part of this same step, not deferred.
- **Required verification gate before deciding enforce-vs-log-only (do not skip, do not guess)**:
  deploy this phase to dev, then make ONE real read-only margin-calculator call per broker (Dhan,
  Groww) — an NFO short and a 1-lot MCX order, no `place_order` involved — and read the actual raw
  response via server logs (temporarily log the raw `bm_result`/`data` dict before parsing, as
  part of implementing this fix, then remove or downgrade that logging once confirmed). Confirm:
  the field names really are flat `total` as assumed, the MCX quantity unit the margin endpoint
  expects actually matches what `translate_qty` sends to the real order endpoint, and what the
  "available" figure actually represents (Groww's `clear_cash` may exclude pledged collateral,
  which would make the fixed gate OVER-block once it's finally doing real math).
- **This is an explicit operator decision, not something I pick silently**: once real numbers are
  confirmed from the live calls above, decide enforce (block on MARGIN_SHORTFALL for real) vs.
  log-only (compute and log the would-be verdict at WARNING, do not actually block) for Dhan/Groww
  specifically. My recommendation: start log-only for Dhan/Groww until the live verification above
  confirms the numbers are sane across a few real accounts/symbols, then flip to enforcing in a
  fast, separate follow-up once confirmed — Kite's gate (already numerically trustworthy, already
  enforcing today) is unaffected either way.

**Phase 3 — frontend (items 7, 8, 9), after Phase 1+2 land:**
- Item 7: frontend sends LOTS to `/orders/ticket/preview`, matching `OptionChainTab.svelte`'s
  existing convention — fix the frontend caller, do not change the backend's documented contract.
- Item 8: preview computes/shows NAKED margin as the enforced number (drop `paired_legs` from the
  margin-preview call, or keep it as a clearly-labeled secondary "if hedged" info value) — never
  loosen the real submit-time check to match.
- Item 9: add `_exchange`/`_resolvedExchange` to the margin-preview watcher list; make the wing
  read tracked (not inside the untracked `setTimeout`) so it can't pair a stale wing; fix the
  bare-underlying-mode template-preview symbol to use `_resolvedSymbol || symbol`.

**Phase 4 — lower-priority items (10, 11, 12), each its own small commit:**
- 10: round the NFO/BFO/CDS fix-qty suggestion down to a lot multiple (reuse the same rounding
  logic the MCX/NCO branch already has); show the hint in lots, not contracts, matching the F&O
  ticket input convention.
- 11: `_basket_margin_validate` uses `broker.translate_qty` instead of hardcoded `to_kite_qty`.
- 12: add `RemoteBroker.validate_gtt_exchange` (own commit — touches `backend/brokers/`, triggers
  the broker coverage gate + conn-service restart on deploy).

**Phase 5 — docs, last, after all code lands:**
- Fix the stale comments found across all 3 audits: `actions_preflight.py:233-237`'s "operator
  sees the actual margin they'll be charged" claim (false before item 8 ships, and needs rewording
  to reflect the naked-margin-is-authoritative design once it does); Dhan's `_margin_for_order`
  docstring claiming its shape is "Kite-shape" (it isn't — Kite nests `total` under `final`/
  `initial`, Dhan doesn't); `/preflight`'s docstring claiming demo callers see 401 (code raises
  403); `_preflight_margin_shortfall_fix_qty`'s docstring claiming `required` "prices the order in
  LOT units" (it's rupees, the explanation is wrong though the math is right).
- Add a note to CLAUDE.md's "Verified-intent propagation" entry documenting the new close-margin
  exemption from item 6 and which callers get it.
- This also covers the operator's standing "make sure comments, spec, code are in sync" request
  for everything touched in this plan specifically — a full repo-wide sync pass is out of scope
  here (already done twice tonight for the market-hours surface); this pass is scoped to the
  margin-calculation code this plan touches.

### Tests (per advisor review — build from real adapter shapes, not hand-built Kite-only stubs)

- Phase 1: a test confirming both close-path preflight dicts carry the real resolved `side` (not
  the `"BUY"` default) and the real position `product` (not a hardcoded `"NRML"`) — for both a
  long-close and a short-close, and for both an MIS and an NRML position.
- Phase 2: call `_margin_for_order`/the Groww equivalent with a MOCKED SDK response matching the
  real flat shape (not a hand-built dict shaped like Kite's — audit found this is exactly how the
  original bug went undetected: `backend/tests/test_funds_order_margin.py` only ever stubbed
  Kite's nested shape). Add a test for the missing-vs-zero fix (a response with a genuinely absent
  margin field must parse to `None`, never `0`). Add a test for the close-exemption's intent
  verification — a FAKE/unverified close claim must NOT receive the exemption.
- Phase 3: Playwright, following the same request-interception convention established in
  `order_ticket_exchange_submit_payload.spec.js` and `order_ticket_template_toggle_regression.spec.js`
  — intercept `/orders/ticket/preview` and assert the request body sends lots not contracts;
  intercept the margin-preview call and confirm it's unaffected by a stale `paired_legs`/exchange
  change after the fix.
- Phase 4/5: unit tests per item, matching this codebase's existing style for each touched file.

### Process notes

- `actions_preflight.py` is already a branchy file — run `radon cc` after EVERY commit that
  touches it, not just at the end, so a CC regression (the `634ce8e4` pattern from the market-hours
  round) is caught immediately, not after several commits have stacked on top of it.
- Dispatch backend agent(s) for Phase 1 and Phase 2 (sequential, since Phase 2 depends on Phase 1's
  close-path fixes being in place for the exemption's test matrix to be meaningful); frontend agent
  for Phase 3; either agent (small enough either way) for Phase 4; doc agent for Phase 5, last.
- Do not push past `dev` without an explicit "deploy" instruction — and do NOT merge Phase 2 to
  `dev` at all until the live read-only margin-call verification step has actually run and the
  enforce-vs-log-only decision has been made explicitly (not defaulted silently).

## Done when

- Phase 1 (the live Kite close-margin bug) is fixed, tested, and can ship independently of
  everything else.
- Phase 2 ships only after: the parser + missing-vs-zero fix, the close exemption, AND the live
  read-only verification calls against dev for both Dhan and Groww — with an explicit, stated
  enforce-vs-log-only decision for each, not a silent default.
- Phase 3's frontend preview fixes are verified live against dev.ramboq.com (matching this
  session's established verification discipline for order-ticket changes), not just source-scanned.
- Full backend suite green, broker/api coverage gates pass, CC clean after every
  `actions_preflight.py` commit specifically, svelte-check clean, vitest green.
- Pushed to `dev`. Stops short of `main` pending an explicit "deploy" instruction.

---

**Status note (added on archive, 2026-10-21):** All five phases were completed and shipped to
`dev` across this session: Phase 1 (`12b8e166`, `3a28bcbd`), Phase 2 (`fa0e3a42`, `3ea29a24`,
`2c53e43f`), Phase 3 (`e048baa7`, `62313b95`, `9f1dc3cf`, `1557a0e0`), Phase 4 item 12
(`7183244b`, later fixed for a deploy-coupling regression in `0b935935`), and Phase 5 docs
(`8c2a544a`, `fd535003`). The live read-only verification step for Phase 2 surfaced two
additional defects (the `validate_gtt_exchange` deploy-coupling regression and Groww's wrong SDK
call signature in `basket_order_margins`), both fixed same-day (`0b935935`). Both
`preflight.enforce_margin_dhan`/`enforce_margin_groww` remain `False` (log-only) — the
enforce-vs-log-only decision is still pending explicit operator input after a prod deploy makes
Groww's fix live-verifiable (see CLAUDE.md's "Critical math guards" entries for both fixes).
