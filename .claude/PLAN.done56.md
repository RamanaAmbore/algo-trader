# Plan: Sprint 4 — seed the real exit-tp-short-option-90 agent + end-to-end verification

## Task

Final sprint of the exit-rule-agent plan (full detail:
`~/.claude/plans/sparkling-dancing-lobster.md`). Sprints 1-3 built and shipped everything this
sprint needs: `positions.any_symbol` scope, `gain_from_entry_pct` + the new
`short_option_gain_from_entry_pct` metric (direction + instrument-type checks folded INSIDE the
metric — avoids a confirmed `all[]` cross-join bug in the shared evaluator), the per-position
close lock inside `chase_order()`, and the full generic hold/release/auto-release
infrastructure (`HoldCategory.AGENT_CLOSE`, `Agent.require_release`/`release_timeout_minutes`,
the hold check inside `_dispatch_live_action`, cancel-suppression, the auto-release background
scan with its kill-switch). **This sprint seeds the one real agent and proves the whole chain
end-to-end.** This is the highest-stakes sprint in the plan — the agent, once seeded, is capable
of automatically closing a real live position with real money. Read the ENTIRE
`sparkling-dancing-lobster.md` plan file's Track 1 section (not just this brief) before writing
anything, especially the "Corrected from the original draft" section and the two Sprint-3
deviation notes (the kill-switch check is `status == 'inactive'` blocks, NOT `status ==
'active'` allows — confirmed load-bearing; the cancel-suppression discriminator is
`status=='CANCELLED' AND broker_order_id==''`, not a hold_json read).

## Agents

- backend: seed exactly one new `Agent` row, following this codebase's existing builtin-agent
  seeding convention (find and match the pattern used for other recently-added builtin agents,
  e.g. `expiry-day-itm-auto-close` — same file, same seeding function, same
  `_ae_sync_existing_builtin` restart-safety convention so re-seeding on a future restart
  doesn't clobber an operator's own edits to this row later):

  - `slug`: `exit-tp-short-option-90`
  - `kind`: `'cycle'`
  - `schedule`: `'market_hours'`
  - `tier`: `'medium'` (an opportunity, not a risk event — no critical-tier 3x ntfy repeat; this
    is a DIFFERENT property from the alert's own `action_required=True`, already wired in
    Sprint 3's hold-check code — don't conflate the two)
  - `conditions`: a SINGLE leaf — `short_option_gain_from_entry_pct >= 90` at scope
    `positions.any_symbol`. **Do NOT express this as `all[]` of separate qty/option-type/gain
    leaves** — that's the exact cross-join bug Sprint 3's new metric exists to avoid.
  - `actions`: `close_position` (the existing action, completely unchanged — no new action type
    was ever written across this whole plan) — using whatever `$row`-style per-match parameter
    binding this codebase's fan-out mechanism already uses (confirmed working end-to-end by
    Sprint 1's Spike A — re-read that test before wiring this up) so each matched row's own
    account/symbol/exchange reaches the action correctly, not the first match's values reused
    for every match.
  - `require_release`: `true`
  - `release_timeout_minutes`: `5` (the operator's own stated default — tunable later without a
    code change, since it's a real column, not a constant)
  - `trade_mode`: `"live"` **set explicitly in the spec dict, not omitted.** `_ae_build_agent_row()`
    only reads `trade_mode` from the spec when present — omitting it silently defaults to paper,
    which is EXACTLY the 2026-10-31 `expiry-day-itm-auto-close` incident (a real, prior,
    documented production incident where this exact mistake meant a brand-new agent ran an
    entire live trading day in paper mode with its safety net never engaging). Triple-check this
    one line before finishing — it is the single highest-leverage mistake possible in this
    entire plan.
  - `status`: `'active'` — this is a brand-new slug, so it is not bound by any
    "ships inactive by default" fail-safe guard that might exist for OTHER seed patterns (that
    guard, if it exists, triggers on a specific literal phrase in a seeded description, by
    design — verify this doesn't accidentally apply here before relying on it either way; if it
    DOES accidentally gate this agent inactive, that's a silent failure mode worth knowing about
    and reporting, not fixing by changing the guard).
  - The DO clause fires BOTH `close_position` and an `alert(...)` call in the same statement
    (this codebase's DSL already supports multiple calls per DO — confirmed earlier in this
    plan's own research) — the alert fires immediately at detection/staging time regardless of
    `require_release`; only the broker-touching half is gated by the hold check. The alert text
    must state the auto-release deadline and how to cancel, and must enrich with sibling legs
    grouped by `underlying` (same key `any_underlying` already uses) so the operator has context
    for naked-leg risk even though there's no forced manual review gate — this fires per-leg, so
    a 90%-profit short leg that's actually one side of a spread/strangle needs this context in
    the notification itself.

  After seeding, run the FULL "Done when" checklist from the detailed plan (reproduced below) as
  real integration tests against the actual seeded row — not just unit tests of the pieces in
  isolation (those already exist from Sprints 1-3). This sprint's job is proving the WHOLE CHAIN
  works together with the real agent in place.

- frontend: skip — no new UI, confirmed in Sprint 1/3 planning (Held Orders routes are
  category-agnostic).
- broker: skip
- doc: skip at code-commit time — write the CLAUDE.md entry as a SEPARATE, FINAL commit once
  everything above is green, following this file's own established entry conventions (see how
  other recent agent-seeding entries are written — lead with what shipped, then an invariant
  statement). This is the one sprint where doc is NOT skipped, since "once the final shape is
  known" (the condition this plan deferred docs on) is now true.
- backend-test: integration-level tests proving, against the REAL seeded agent row (not a
  synthetic test agent):
  1. A constructed short-option position crossing 90% profit produces exactly one HELD
     `AlgoOrder` row (category `agent_close`) + one action-required alert, with zero broker
     calls made.
  2. Left unactioned past its `auto_release_at`, that row releases through the unmodified
     `release_held_order`/`get_release_handler()` path, re-verifying the live position first.
  3. Explicitly cancelled before the timeout: the row never auto-releases, AND a re-crossing of
     the 90% threshold for the same position within the same session does not re-stage
     (Sprint 3's actual shipped discriminator: `status=='CANCELLED' AND broker_order_id==''`).
  4. Two concurrent close-triggers on the same position (this agent's release racing an
     unrelated trigger) — the second fails closed at Sprint 2's lock inside `chase_order()`.
  5. `short_option_gain_from_entry_pct` returns `None` for every missing-data/wrong-direction/
     wrong-instrument-type case — re-confirm against the real seeded condition, not just the
     metric's own isolated test suite.
  6. Two different short-option legs in the same account crossing 90% in the same cycle produce
     two independent latches (Sprint 1's `_latch_key` symbol-dimension fix).
  7. A deactivated agent's already-staged hold does not auto-release (Sprint 3's kill-switch —
     confirmed shipped as `status == 'inactive'` blocks, test against THAT, not the literal
     `'active'` wording from the original plan draft).
  8. `trade_mode` resolves to `'live'` on the seeded row, and the hold check actually engages
     (i.e., this agent genuinely reaches `_dispatch_live_action`'s live branch) — this is the
     one check that directly prevents a repeat of the 2026-10-31 incident class; do not skip it
     as "obviously fine."
  9. Confirm no change to any existing Bracket/GTT code path — `git diff` against pre-Sprint-1
     HEAD should show zero touches to `template_attach.py` or anything Bracket-related across
     the whole plan.
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no
- **Verification-gap, state plainly, do not oversell**: dev has no live mode, so
  `_dispatch_live_action`'s hold check for THIS agent is never reached in dev's normal
  operation. This path is proven via tests that call it directly with a mocked broker (already
  built in Sprint 3) plus this sprint's integration tests against the real seeded row (still
  running against test fixtures/mocks, not a live broker). **Prod is the first genuine live
  run.** After deploying, confirm via a live DB read that the agent is seeded correctly
  (`status='active'`, `trade_mode='live'`, `require_release=true`,
  `release_timeout_minutes=5`) — this is a real-world check this plan's own "Done when" section
  requires, do it via `ssh` + a read-only DB query, not assumed from the migration having run.

## Commit message

`feat(agents): seed exit-tp-short-option-90, a 90% short-option profit-target close with a
5-minute timed-release hold`
(separate, final commit) `docs: document the exit-tp-short-option-90 agent in CLAUDE.md`

## Done when

(reproduced from the detailed plan, now checked against the real seeded row)
- `exit-tp-short-option-90` is live (`status='active'`, `trade_mode='live'`) on dev AND prod,
  confirmed via DB read in both places.
- A constructed short-option position crossing 90% profit produces exactly one HELD `AlgoOrder`
  row (category `agent_close`) + one action-required alert, no broker call made.
- Left unactioned past `auto_release_at`, that row releases through the unmodified
  `release_held_order`/`get_release_handler()` path, re-verifying the live position first.
- Explicitly cancelled before the timeout: the row never auto-releases, AND a re-crossing within
  the same session does not re-stage.
- Two concurrent close-triggers on the same position: the second fails closed at Sprint 2's
  real lock chokepoint.
- `short_option_gain_from_entry_pct` returns `None` for every missing-data case — tested against
  the real seeded condition.
- Two different short-option legs in the same account crossing 90% in the same cycle produce two
  independent latches.
- A deactivated agent's already-staged hold does not auto-release.
- Full backend suite green; zero changes to any existing Bracket/GTT code path across the entire
  4-sprint plan.
- CLAUDE.md entry written, documenting what shipped across all 4 sprints as one coherent
  feature, following this file's own entry conventions.

### Critical files
- `backend/api/algo/agent_engine.py` (the one new seeded `Agent` row + its spec dict)
- `backend/tests/` (new integration test file proving the full chain end-to-end)
- `CLAUDE.md` (new entry, final commit of this sprint)
