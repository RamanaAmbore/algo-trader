# Plan: Expiry alert shows affected roots + real unnetted/to-close positions

## Task

`expiry-day-mcx-v2`/`expiry-day-nse-v2` currently render one flat line via `_v2_reminder_only_line()`
(e.g. "Expiry pre-close — a held MCX position expires today — MCX closes in 30 minutes"), with zero
position detail. Operator wants the alert to also list which underlying root symbols are affected,
and for MCX vs NSE/NFO separately, show which positions are genuinely unnetted/need closing before
settlement — using the SAME real classification the Derivatives Exp-close tab's backend mirror
already computes (operator's explicit choice: "Real netting verdict via ExpiryEngine", confirmed via
a 6-member council review + direct operator decision).

Confirmed from reading `backend/api/algo/expiry.py`: `ExpiryEngine.scan_positions()` is READ-ONLY
(no order placement, no DB writes — fetches positions/instruments/LTP only), already per-account
(`OptionPosition.account`), and implements:
- **NFO**: every ITM/NTM leg is flagged `needs_close` unconditionally (no netting — equity doesn't
  net-settle pairs). OTM legs are left alone (no "netted" claim — different mechanism, must not use
  MCX's wording).
- **MCX**: 4-rule greedy theta-priority netting per `(account, underlying, expiry)` group, plus
  same-underlying futures as delta-offset partners. `residual_qty != 0` → `needs_close=True` with a
  `close_reason` string already built ("MCX unhedged {moneyness} after 4-rule netting (residual qty
  {:+d})"). `residual_qty == 0` → fully netted, no close needed. This is the exact Close-tab mirror
  (module docstring: "mirrors the frontend /admin/options Close tab").

`scan_positions()` returns only `to_close`; the full expiring set (incl. fully-netted legs) is on
`engine.state.positions` after the call. `scan_positions()` is **sync** and makes blocking broker
calls — must run via `asyncio.to_thread()` from the async alert path.

## Approach

1. **New async helper**, `_v2_expiry_position_breakdown(exchange_group: str) -> str | None` in
   `backend/api/algo/agent_engine.py` (`exchange_group` ∈ `{"MCX", "NFO"}`):
   - `engine = ExpiryEngine(); to_close = await asyncio.to_thread(engine.scan_positions)`, then read
     `engine.state.positions` for the full expiring set; filter both to `p.exchange == exchange_group`.
   - Group by `underlying` (root), then by `account` within each root.
   - **MCX wording**: a leg with `needs_close` → "needs closing" line with masked account, side
     (BUY/SELL to close), qty in LOTS (convert via existing lot-size lookup; fall back to raw
     contracts with an explicit "(contracts)" unit label if lot size can't be resolved — never
     mislabel units), and the leg's own `close_reason`. A root with zero `needs_close` legs → one
     demoted "netted: ROOT1, ROOT2" line, listed LAST, with NO "safe"/"no action needed" language —
     state only the neutral fact that it netted to zero, per the council's unanimous "never render an
     assurance" finding.
   - **NFO wording**: ITM/NTM legs → "needs closing" line (reuse `close_reason`, "Equity {moneyness}
     — must close before expiry"); non-flagged legs → demoted "no action (OTM): ROOT1" line, last.
     Never reuse the word "netted" for NFO — there is no netting step for equity.
   - Ordering: action-needed roots first (sorted by root name), then demoted/no-action roots, per the
     UX finding (most-urgent-first under a hard deadline).
   - **Degrade loudly, never silently.** Wrap the whole body in try/except: on any failure (broker
     fetch, instrument cache miss, LTP fetch), log `logger.warning(...)` and return an explicit note
     — `"(position detail unavailable this cycle — check the Positions grid or Exp-close tab)"` —
     never `None`/empty, which would read as "nothing to close."
   - Return `None` only when the filtered set is genuinely empty (no expiring position at all for
     that exchange group right now) — render nothing extra in that case (the base reminder line
     already says a position expires today, so an empty breakdown here would only happen on a
     race between the condition evaluating true and the scan re-fetching; log a warning if so).

2. **Call site** — inside `_v2_send_rich_alert()`'s existing
   `elif (reminder_line := _v2_reminder_only_line(agent, matches)):` branch
   (`backend/api/algo/agent_engine.py`): detect exchange group from `agent.slug`
   (`expiry-day-mcx-v2` → `"MCX"`, `expiry-day-nse-v2` → `"NFO"`; any other reminder-only agent,
   e.g. `market-preclose-mcx-v2`/`market-preclose-nonmcx`, gets no breakdown — unchanged). When
   applicable, `await`-call the new helper and append its text (blank-line separated) to both
   `tg_body` and `email_html` (HTML-escaped per line for email, matching the existing convention).

3. **Audit-log `condition_text` (`_cycle_maybe_buffer_fire`) deliberately excluded.** It stays the
   plain `_v2_reminder_only_line()` text — no breakdown. Documented inline: this function is a sync
   escalation-gate helper with no `context`, and threading a broker-call-bearing enrichment into it
   would be scope creep into an unrelated function (per architect + perf findings). The rich detail
   only needs to reach the channel the operator actually acts on (Telegram/email), not the in-app
   audit trail.

4. **Lot/unit safety.** Reuse the existing lot-size resolution path (same one `apply_template_to_order`
   uses via `get_lot_size()`) for display conversion only — this is read-only formatting, never feeds
   a broker call, so a resolution failure degrades to showing raw contracts with an explicit unit
   label rather than guessing.

## Files

- `backend/api/algo/agent_engine.py` — new `_v2_expiry_position_breakdown()`, call-site wiring in
  `_v2_send_rich_alert()`, comment on `_cycle_maybe_buffer_fire` explaining the deliberate exclusion.
- `backend/tests/test_expiry_day_and_preclose_agents.py` (or a new focused test file) — MCX case with
  one fully-netted root (long CE + long PE, rule 3) and one unnetted root (naked future / unbalanced
  CE); NFO case with one ITM leg (needs close) and one OTM leg (no action, correct wording); a
  broker-fetch-failure case asserting the explicit degraded note (never blank); a multi-account case
  asserting accounts are never pooled.
- `docs/specs/AGENTS_SPEC.md`, `docs/guides/AGENTS_GUIDE.md`, `docs/specs/ALERT_AGENTS_DESIGN.md` —
  document the enrichment, the `ExpiryEngine.scan_positions()` reuse (read-only, account-scoped, same
  classification as the Close tab), the explicit MCX-vs-NFO wording difference, and the audit-log
  exclusion.

## Agents

- backend: implement steps 1–4 above, with pytest coverage per the test list.
- doc: update the three doc files above.

## Tests

- pytest: yes
- svelte-check: no (backend-only change)
- playwright: no

## Commit message

fix(agents): expiry alert lists affected roots and real unnetted positions via ExpiryEngine

## Done when

- `expiry-day-mcx-v2` alert text lists MCX roots affected, with unnetted (needs-closing) legs listed
  first (masked account, lots, reason) and fully-netted roots demoted/last with neutral wording.
- `expiry-day-nse-v2` alert text lists NFO roots affected, ITM/NTM legs flagged for closing, OTM legs
  demoted/last as "no action" — never using MCX's "netted" wording.
- A broker/scan failure renders an explicit "unavailable" note, never a silent blank or false "all
  clear."
- The audit-log `condition_text` path is unchanged (documented, not a silent gap).
- Full backend pytest suite green, coverage gates pass.
