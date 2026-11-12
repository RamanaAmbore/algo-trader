# Plan: Fix audit-found alert identity/formatting bugs, structural silent-drop trap, DSL doc drift, and re-base duplicate expiry classifiers

## Task
A follow-up audit (run against the two already-deployed alert-pipeline commits) found that the
"header and trailing tag always agree" / "no missing timestamps" invariants the last round
claimed were NOT fully true: `order_failure` and `app_message` records still disagree between
header and tag (or show no identity at all), `_render_deploy_sync` has the exact same
`trigger_known` bug `_render_market_status` was just fixed for, and `_error_agent_self_alerted`
has a structural silent-drop trap that can swallow a CRITICAL alert with no consuming agent
(confirmed live for `concurrent_chase_refused`, mitigated today only by a sibling call). The DSL
grammar itself (`alert(...)`) is confirmed clean — no regression from the `notify=` removal — but
its doc (`AGENTS_GRAMMAR_EBNF.md`) and a few comments have drifted. CLAUDE.md is also missing an
entry for the first of the two already-shipped commits. Separately, per explicit operator
decision: `expiry-mcx-risk-alert`/`expiry-nfo-risk-alert` must be re-based to call
`ExpiryEngine.scan_positions()` instead of their own independent CE/PE-grouping classifiers, so
they can never disagree with `expiry-day-preclose-alert` about the same position.

## Agents
- backend:
  **Alert identity/formatting fixes.** Files: `backend/shared/helpers/alert_utils.py`,
  `backend/api/algo/event_agents.py`, `backend/shared/helpers/app_message.py`,
  `backend/api/algo/grammar_tokens/action_send_market_status.py`.
  1. **`order_failure` header/tag disagreement.** `order_failure_messages()` (`alert_utils.py`
     ~1497-1500) builds the HEADER with `source_type`/`source_name`, but the producer
     `_send_order_failure_messages()` (~1571-1580) never attaches those same keys to the log
     record's own `extra`. Fix: attach them to `extra` too, so `resolve_source()` — and therefore
     the trailing tag AND the Alerts tab, which both read the record's `extra` independently of
     the header string — agree with what the header actually says.
  2. **`order_failure`'s mode pill and subject are inconsistent with every other alert.** It shows
     a `[LIVE]` pill (`alert_utils.py` ~1460/1504) even though the "live stays silent" rule says a
     live-mode alert gets no pill at all; its subject (~1534) keeps the old `[Manual] RamboQuant
     Order Rejected:` shape instead of the convention every other alert subject now uses. Fix
     both to match the established convention (check 2-3 other subject-building call sites first).
  3. **CRITICAL `app_message` has no identity line and no timestamp at all.** `_route_sinks()`
     (`app_message.py` ~211-217) sets no `source_type`/`source_name` on the record it logs, and
     `_render_deploy_sync` (`event_agents.py` ~545) skips the header entirely when neither is
     present. Give the log call `source_type="process"`, `source_name="App message"` (or similar).
  4. **`_render_deploy_sync` has the exact same `trigger_known` bug `_render_market_status` was
     just fixed for.** It still hardcodes `trigger_known=not has_process_identity`
     (`event_agents.py` ~553), which is always `True` for this renderer's producers (they always
     set `source_type`/`source_name`), so a real agent trigger would always be silently dropped —
     latent today (no live `generic_notice` producer attaches agent keys), but the exact same bug
     class. Fix to `trigger_known=_order_scope_trigger_known(x)`, same as the market-status fix.
  5. **Stale comment.** `action_send_market_status.py` ~222-233 still says "KNOWN RENDERER GAP …
     not fixed here" — false now that item 4's sibling fix landed for `_render_market_status`.
     Remove or correct it so nobody "fixes" the renderer a second time or distrusts a working fix.
  6. **INFO-level `AppMessage` with a `deploy`/`alert` tag is silently dropped.** `_route_sinks`'s
     `should_alert` gate treats `level in (error, critical) OR tag in (deploy, alert)` as
     alert-worthy (`app_message.py` ~202), but `APP_MESSAGE_AGENT`'s `min_level` requires WARNING+
     (`event_agents.py` ~2277), and the unrouted catch-all also requires WARNING+ — so an INFO
     message that qualifies via the tag clause reaches NO channel. Not reachable by today's one
     caller (`background.py` ~8733, which logs `level="info"` with unrelated tags), but a real
     structural gap. Read both gates carefully, decide the minimal correct fix (lower
     `APP_MESSAGE_AGENT`'s `min_level`, or tighten `should_alert` to not treat a bare tag match at
     INFO as alert-worthy) — don't guess, verify which is actually intended by re-reading
     `_route_sinks`'s own full docstring/intent first.
  7. **Structural silent-drop trap in `_error_agent_self_alerted`.** A CRITICAL record carrying an
     `alert_event` that ERROR_AGENT's own condition matches gets treated as "already routed" by
     `_record_matches_any_agent`, so the unrouted catch-all never fires — then
     `_error_agent_self_alerted` ALSO skips ERROR_AGENT itself, assuming some other dedicated
     agent already consumed it. When no dedicated agent actually exists for that `alert_event`
     (confirmed live for `concurrent_chase_refused` in `chase.py` ~3411-3418 — mitigated today only
     by a separate `send_order_failure_alert(..., bypass_gates=True)` sibling call at ~3420-3428,
     not by this mechanism), the record is silently dropped. Fix structurally: have
     `_error_agent_self_alerted` skip a CRITICAL+`alert_event` record only when some ACTIVE seeded
     agent genuinely consumes that specific `alert_event` (check `SEEDED_AGENTS`/
     `ALERT_EVENT_DISPATCH` for a real match on the alert_event value), not merely because
     ERROR_AGENT's own generic condition happens to match it.
  8. **Nice-to-fix bundle** (lower risk, do these too since they're all in the same files):
     - `_render_error`'s stray blank line (`f"{origin_line}\n{ts}\n\n{msg}"`, `event_agents.py`
       ~838) and its redundant re-derivation of what `format_notification_header()` already
       returns via `.splitlines()[0]` plus a separately-computed `ts` — simplify to call the
       header function once and use its full output.
     - `order_correction`'s body ends with a redundant `(source=order-poll)` suffix
       (`event_agents.py` ~435) that repeats the header's own "Source: order poll" — remove the
       suffix.
     - `_alert_agent_source_slug` (`event_agents.py` ~968) inconsistently returns a SLUG for
       collapsed-event agents but a NAME for dedicated agents, producing mismatched
       `#via_order_failure_alert` vs `#via_order_cancelled` (name, not slug) in the trailing tag.
       Standardize to always use the slug (consistent, and already what `_tag_token()` expects).
     - Remove confirmed-dead code: `_send_email_to_recipients` (`alert_utils.py` ~535, zero
       callers) and `_relabel_agent_label_to_alert` (~752, a permanent no-op) — re-verify zero
       callers yourself before deleting either.
  9. **Notify-strip migration gap.** `_migrate_agents_strip_notify_from_actions`
     (`backend/api/database.py`) strips a `notify` key from `actions` entries only, not from
     `preamble` entries; `_age_validate_action_entries` (`routes/agents.py`) also validates
     `actions` only. Confirmed zero live rows affected today, but close the gap for
     future-proofing: extend both to also cover `preamble`.
  10. Write/update a test for every change (see Tests below).

  **Expiry classifier re-base** (separate agent, different files — operator-approved design
  change). Files: `backend/api/algo/grammar_tokens/scope_positions_expiring_today_mcx_unhedged.py`
  and whatever the NFO-side equivalent file is (locate it — the audit names the condition as
  `expiry-nfo-risk-alert`'s `is_itm`/`is_future` check; find its real resolver file), plus
  whatever module exposes `ExpiryEngine.scan_positions()` (used by `expiry-day-preclose-alert`'s
  action — locate it, likely `backend/api/algo/actions_live.py` or a dedicated expiry module).
  1. Re-base `expiry-mcx-risk-alert`'s condition resolver so its classification comes from calling
     `ExpiryEngine.scan_positions()` (the SAME classifier `expiry-day-preclose-alert`'s action
     already uses) instead of its own independent CE/PE net-qty grouping logic. Keep the agent
     itself (its own schedule/cadence/tier), only its classification source changes.
  2. Do the same for `expiry-nfo-risk-alert`'s `is_itm`/`is_future` check.
  3. Confirm both agents still fire at their own existing cadence/timing — this is a
     classifier-source change, not a scheduling change.
  4. Write/update a test proving both re-based conditions now agree with
     `ExpiryEngine.scan_positions()`'s own output for the same position data (the exact disagreement
     the audit reproduced should no longer be reproducible).

- frontend:
  Files: `frontend/src/lib/command/grammars/agents.js`, `frontend/src/lib/command/consoleDispatch.js`,
  `docs/specs/AGENTS_GRAMMAR_EBNF.md`.
  1. Fix `AGENTS_GRAMMAR_EBNF.md` doc drift: `bracket`/`bracket_stmt`/`BracketAst` are still
     described as live (L16/36/79/110/602) despite being rejected by the code
     (`agents.js` ~5250) since the T3.4 removal — correct the doc. The `token_stmt` production
     (~L86) should read `(condition_body | call_list)`, not just `condition_body`, since
     `parseTokenStmt` still accepts a `call_list` body for `kind=notify` even though it can no
     longer COMPILE (parse succeeds, compile fails — same documented split as `alert(...)`). The
     claim at ~L660 that `ntfy` "does not exist in the live catalog" is false — it exists in
     `agent_grammar.yaml:931` — correct it.
  2. Clean up stale docstrings: `_resolveChannelCall` (`agents.js` ~4706-4734) still names
     "the new action-level `notify=[...]`" as a second caller — its only caller now is
     `compileAlertClause` (~4827); `parseTokenStmt` (~2701-2718) still calls the notify/call_list
     body "the default" — update both to reflect the current, post-removal reality.
  3. Remove the dead `kind:'notify'` handling branch in `consoleDispatch.js`'s `submitConsoleToken`
     (~354-386) — a notify-kind Token can no longer compile, so this branch is unreachable.
  4. Add the missing test: a committed test that round-trips a PREAMBLE `alert(...)` statement
     through decompile (existing tests only cover compile) — confirmed working via an ad-hoc
     check, just never committed as a real test.
  5. Write/update a test for every change.

- broker: skip
- doc: After implementation lands, sync CLAUDE.md:
  1. Add a short entry for commit `0216d534` ("remove action-layer channel-naming") — missing
     despite `.claude/PLAN.done65.md`'s own doc brief requiring one.
  2. Mark the "Wave 2 — the `notify=[...]` action param" entry and the "SEEDED_AGENTS grew …
     to 13 … `ACTION_NOTIFY_AGENT`" entry as superseded — `notify=` is fully removed and the
     count is now 16.
  3. Fix the Common Tasks row that still tells implementers to put `channels` in `extra` for a
     new action — this directly contradicts the retirement and is actively misleading guidance.
  4. Fix the "(2026-11-08, uncommitted)" label on the recently-added identity entry (it's
     committed now) and correct its "renders unknown" claim for header-less `generic_notice`
     records, which was false until this round's `app_message` fix landed.
  5. Document the expiry-classifier re-base (new entry, same style as other recent entries).

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(alerts): order_failure/app_message identity gaps, deploy_sync trigger_known bug, structural self-alerted silent-drop trap, re-base duplicate expiry classifiers

A follow-up audit found the prior round's identity-consistency claims weren't fully true:
order_failure's header and trailing tag disagreed, app_message had no identity at all, and
_render_deploy_sync carried the exact same trigger_known bug just fixed for market_status.
Also closes a structural silent-drop trap where a CRITICAL+alert_event record with no real
consuming agent could be silently dropped (confirmed live for concurrent_chase_refused, mitigated
today only by a sibling call). Per explicit operator decision, expiry-mcx-risk-alert and
expiry-nfo-risk-alert are re-based onto ExpiryEngine.scan_positions() — the same classifier
expiry-day-preclose-alert already uses — so the two can never disagree about the same position.

## Done when
- order_failure's header, trailing tag, and Alerts-tab row all agree on the same identity; its
  mode pill and subject match every other alert's convention.
- A CRITICAL app_message record shows a real identity line and timestamp.
- _render_deploy_sync's trigger_known bug is fixed, matching _render_market_status's fix.
- A CRITICAL+alert_event record with no real consuming agent is never silently dropped by
  _error_agent_self_alerted.
- expiry-mcx-risk-alert and expiry-nfo-risk-alert classify off the same ExpiryEngine data
  expiry-day-preclose-alert uses — the audit's reproduced disagreement no longer reproduces.
- AGENTS_GRAMMAR_EBNF.md no longer describes bracket as live and no longer claims ntfy doesn't exist.
- pytest is green; CLAUDE.md has an entry for 0216d534 and no longer tells implementers to use
  the retired channels-in-extra pattern.
