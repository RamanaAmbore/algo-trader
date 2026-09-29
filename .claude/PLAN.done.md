# Plan: Fix false "Margin < 0 -₹999,999,999" alert + unclear alert text

## Context

Operator reported (with a screenshot) a false-positive alert firing simultaneously
for every account, each showing the identical, absurd line `Margin < 0
-₹999,999,999` — including two accounts (ZG0790, ZJ6294) that have real positive
fund balances (₹94.74L / ₹69.83L) shown correctly elsewhere in the same alert.
Follow-up: "it should clearly tell what the alert is about. it is not clear from
the alert."

Root-caused via a dedicated research agent (facts below, not a guess):

- `-999999999` is **not** a broker/funds-fetch fallback value — the "Missing-vs-
  zero convention" (`_num_or_none` in `grammar.py`, `_eval_leaf`'s `if val is
  None: continue` in `agent_evaluator.py`) is intact and not implicated.
- It's a **deliberately hand-authored "always-true" threshold literal** on two
  built-in, schedule-only informational agents — `market-open-nse` and
  `market-preclose-mcx` (`backend/api/algo/agent_engine.py:1469,1477`):
  `{"op": ">=", "scope": "funds.any_acct", "metric": "avail_margin", "value":
  -999999999}`. Comment at `agent_engine.py:1445-1446` confirms the intent: this
  makes the condition match every account on every tick, so the REAL trigger is
  each agent's own `fire_at_time` gate, not this condition.
- **Two independent display defects compound to produce the confusing alert:**
  1. `_v2_derive_kind(metric)` (`agent_engine.py:822-834`) maps `metric ==
     'avail_margin'` to `'negative_margin'` **based on metric name alone,
     ignoring `op`** — so this `>=` "always-true" trick leaf gets mislabeled
     exactly the same as a real "margin dropped below floor" breach (e.g. the
     genuine `loss-funds-negative` agent, which legitimately uses `op: "<"`).
  2. `_v2_match_to_alertrow` (`agent_engine.py:884-891`) renders the alert's
     headline number from `match['threshold']` (the fixed, hand-authored
     literal) instead of `match['value']` (the real per-account fetched
     margin) — so every account shows the identical fabricated
     `-₹999,999,999` via `_v2_format_threshold`, even though the real number is
     already correctly available and shown elsewhere in the same alert as the
     Funds/FND figure.

## Fix

### 1. `backend/api/algo/agent_engine.py` — `_v2_derive_kind`

Extend its signature to also take the leaf's `op` (already available on the
match dict built by `_eval_leaf` in `agent_evaluator.py` — confirm exact key
during implementation, likely `match['op']` or similar, thread it through the
one caller). Only classify `metric in ('cash',)` → `'negative_cash'` and
`metric in ('avail_margin',)` → `'negative_margin'` when `op` is a "below-floor"
comparison (`<` / `<=`). For any other op (`>=`, `>`, `==`, ...) on the same
metrics, fall through to whatever this function's existing generic/default kind
already is for non-special metrics — read the full function first to reuse that
path rather than invent a new "kind" label.

### 2. `backend/api/algo/agent_engine.py` — `_v2_match_to_alertrow`

For a genuine threshold-breach kind, the headline number must come from
`match['value']` (the real fetched figure), not `match['threshold']`. Read
`_tg_rule_line` (`backend/shared/helpers/alert_utils.py:1190-1202`) and the
`_KIND_LABEL` map (`alert_utils.py:1119-1127`) in full to see how other
"informational"/non-breach kinds are already rendered elsewhere in this file,
and reuse that existing neutral rendering path for whatever kind
`market-open-nse`/`market-preclose-mcx`'s trick-leaf now falls into after fix
#1 — these two agents are pure scheduled reminders, not threshold breaches, so
their alert text should say what they're actually informing the operator of
(read each agent's own definition/detail text at `agent_engine.py` around
lines 1460-1480 to confirm what that informational content already is), not a
fabricated margin figure.

### 3. Defensive guard (belt-and-suspenders, addresses "should clearly tell
what the alert is about")

In `_v2_format_threshold` or `_tg_rule_line`, add a sanity check that refuses
to render an obviously-nonsensical sentinel magnitude (e.g. `abs(threshold) >=
1e8`) as if it were a real displayable number — log a warning instead and fall
back to a neutral label. This is a second, independent line of defense in case
another agent definition ever reuses a similar "always-true" sentinel trick in
the future; fix #1/#2 address the root cause, this guard prevents recurrence of
the SYMPTOM even if a future config regresses the kind-derivation logic.

## Explicitly out of scope

- Do not change `market-open-nse`/`market-preclose-mcx`'s underlying
  "always-true, funds.any_acct scope + fire_at_time gate" trigger mechanism
  itself — it's a working, intentional pattern for schedule-only agents; only
  its DOWNSTREAM display classification is wrong. Changing the trigger
  mechanism is a larger, unrelated architecture change.
- Do not touch `_num_or_none`, `_eval_leaf`'s None-guard, or any broker/funds-
  fetch code — confirmed not implicated.
- Do not touch the genuine `loss-funds-negative` agent or any other real `<`/`<=`
  margin/cash threshold agent — their existing behavior and display are correct
  and must not change.

## Tests (mandatory, same commit)

Extend `backend/tests/test_mcx_preclose_agent.py` (already has fixtures using
`'threshold': -999999999`, lines 113/262/535) and/or add to
`backend/tests/test_alert_routing.py` (line 765 already references this
pattern):
- A match with `metric='avail_margin', op='>=', threshold=-999999999` must NOT
  produce kind `'negative_margin'` — assert the new neutral kind instead.
- A match with `metric='avail_margin', op='<', threshold=<real number>` (the
  genuine `loss-funds-negative` shape) must still correctly produce kind
  `'negative_margin'` — regression guard, this must not change.
- `_v2_match_to_alertrow` for a genuine negative-margin breach still renders
  the real fetched `value` as the headline number (regression guard).
- The sanity guard: a threshold with `abs() >= 1e8` never appears verbatim in
  rendered alert text.

## Verification

1. `venv/bin/pytest backend/tests/ -q --tb=line` — full suite green, including
   new/updated tests above.
2. Manually trace (or query dev DB / trigger a dry-run) that `market-open-nse`
   and `market-preclose-mcx` still fire at their scheduled times (this is
   informational — don't break the actual notification, only its mislabeled
   display).
3. Confirm the real `loss-funds-negative` agent's alert rendering is byte-for-
   byte unchanged for a genuine breach (regression check).

## Commit message (draft)

`fix(alerts): stop schedule-only informational agents' always-true sentinel condition from rendering as a fake "Margin < 0" breach`

## Done when

- The `market-open-nse`/`market-preclose-mcx` scheduled reminders no longer
  render as a fabricated "Margin < 0 -₹999,999,999" line for every account.
- A genuine margin/cash negative-threshold breach (`loss-funds-negative` or
  similar) is completely unaffected — same kind, same label, same headline
  number as before.
- New/updated tests green; full suite green.
