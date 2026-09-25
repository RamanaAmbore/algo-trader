# Plan: Fix Loss / Rate-of-Change Alert Condition Bugs

## Context

Operator: "look at current loss and rate of change alerts. if margin or cash of 0
should not generate alerts. audit the alerts fix the conditions make sure the
alerts are generated based proper risk events. there are multiple bugs there in
conditions." A read-only audit (code + live prod DB queries + prod log grep over
`ssh ramboq`) found **11 confirmed defects** — a mix of false-positive spam,
missed real risk, and repeat-alert bugs — plus several high-likelihood risks. This
is a genuinely broken subsystem, not one isolated bug: alert values in prod
frequently reflect broker-fetch noise or bookkeeping artifacts rather than real
risk, several agents can never re-arm correctly, and the live prod DB's agent
conditions have already drifted from the code defaults. Fixing the operator's
specific "margin/cash of 0" ask requires touching the same missing-vs-zero
handling that several other confirmed bugs share, so this plan fixes all 11
confirmed defects together as one coherent alerts-correctness pass.

**Important pre-existing fact, not a bug to fix**: `_ae_sync_existing_builtin`
never overwrites an existing agent row's `conditions` in the DB from the code
defaults — this is presumably intentional (agents are operator-editable via
`/agents`, so code changes must not silently clobber an operator's customization).
This means fixing the CODE defaults alone will not reach prod's live rows. See
"Flagged decision" at the end.

## The 11 confirmed defects and their fixes

**1. Rate window is really one ~5-minute delta, not a 10-minute average.**
`agent_evaluator.py:113-160` / `agent_engine.py:775` (`rate_window_min=10`). The
perf loop runs every ~5m05s, so only 2 samples ever fall in the window; the
5-or-more-sample smoothing path never engages. Prod fired -28k/min to -33k/min
"rates" that are just one poll's raw Δ. **Fix**: require a minimum sample count
(≥3) and a minimum time span (≥0.8× the window) before a rate leaf can evaluate
to a real value; otherwise return None (skip, per the None-means-missing
convention established in fix #3). Size the window from the actual poll cadence
rather than a fixed constant.

**2. `all[]` doesn't require the same account.** `agent_evaluator.py:330-338`
evaluates each child leaf over its own row set independently, so
`all[acctA_leaf, acctB_leaf]` can fire when NO single account met both
conditions (prod repro: `loss-margin-low` fired combining one account's
`avail_margin=0` with a different, healthy account's `373,828.52`). **Fix**: when
sibling leaves under `all[]` share a scope that includes `account`, join matches
per-account (row-level AND) instead of per-leaf-independently.

**3. Missing/unmapped funds fields are read as a real 0 (operator's explicit
ask).** `grammar.py:81-85` (`_metric_cash`/`_metric_avail_margin`) and
`agent_engine.py:386-394` both do `float(row.get(col, 0) or 0)` — a genuinely
missing, unmapped, or NaN value collapses to `0`, indistinguishable from a real
zero balance. Dhan (`dhan.py:1974-1984,2016-2036`) and Groww
(`groww.py:452-461,1626`) map some funds fields inconsistently, so several
accounts permanently report `avail_margin=0.00` and fired `loss-margin-low` 61
times in 60 days on nothing but stale/missing data. **Fix**: the resolvers must
return `None` (not `0`) when the source column is absent, NaN, or not supported
by that broker for that field — add a per-broker capability flag if needed so a
broker that genuinely doesn't expose a field isn't treated as reporting a false
zero. `_eval_leaf` already correctly skips a `None` metric (verified-clean per
audit) — this fix just needs the resolvers to actually emit `None` instead of a
coerced `0` in the missing case. A **true** `0` value (broker actively reports
exactly zero) must still be allowed to alert — only *missing* data is suppressed.

**4. `cash` metric reads start-of-day cash, never live cash.** `grammar.py:81-82`
reads `avail opening_balance` (Dhan: `sodLimit`), which cannot move intraday, so
`loss-funds-negative`'s `cash<0` leaf can never fire on a real intraday cash
drop — all 6 of its prod fires were on the `avail_margin` leaf instead. **Fix**:
source this metric from a live cash field (the cash-plan work elsewhere in this
session already identified the correct live-cash field per broker — reuse that
resolution once it lands, or use `avail live_balance`/`avail cash` directly here
if that plan hasn't landed yet), or rename the metric to `sod_cash` and add a
genuinely live `cash` metric alongside it if both are useful.

**5. A missing-data tick is treated as "recovered" and re-arms a latched
alert.** `agent_engine.py:1835-1837` calls `_v2_unlatch` whenever a cycle
produces no matches — but "no matches" also happens when a fetch times out,
fails, or returns an empty frame (positions timeout → empty frame,
`background.py:652-654`; per-account fetch failure → empty frame with
`fetch_failed`, `broker_apis.py:2071-2075`; `attrs` lost at `pd.concat`). The
next good tick then re-fires the *same* breach as if it were new. **Fix**: only
unlatch when the scope's rows were actually present (fetch succeeded, non-empty
frame, no `fetch_failed` flag) AND none breached — never unlatch on an
empty/failed fetch.

**6. Opening-baseline gate doesn't cover the agents that need it.** The 15-minute
post-open suppression gate (`agent_engine.py:317-322,1530-1537`) only applies to
agents whose conditions are ALL rate leaves (`_v2_all_rate_metric`), but both
prod rate agents mix a `day_val` leaf in with the rate leaf, so neither is ever
gated — and even when it would apply, the gate's own baseline
(`_update_pnl_history`, session_start ~08:04 IST) expires ~08:19, before the
09:00 MCX / 09:15 NSE opens it's meant to protect against. **Fix**: (a) apply
the gate per rate LEAF, not per whole agent, so a mixed agent's rate leaf is
still suppressed even though its `day_val` leaf isn't; (b) anchor each segment's
baseline to that segment's actual open time, and don't record P&L history for a
segment while it's still closed.

**7. Topic suppression only lasts one tick; same-tier agents aren't deduped.**
`agent_engine.py:1993-2023` — a suppressed fire records no latch/cooldown, so it
fires for real on the very next tick once the higher-tier agent enters its own
cooldown (prod repro: suppressed at 11:46:49, fired for real at 11:51:58, same
pattern 3× in one day). Separately, `_ae_suppressed_in_group` never suppresses
equal-tier agents against each other, so two critical-tier agents in the same
topic (`loss-rate-acct` + `loss-positions-total`) both fire on the same event,
sending two urgent pushes ~3s apart. **Fix**: record a latch/cooldown for a
suppressed fire too (not just for one that actually alerts), and extend
same-topic dedup to cover equal-tier agents, not only strictly-lower-tier ones.

**8. Rate-agent re-alert check mixes units and uses the wrong cooldown.**
`agent_engine.py:687-712,745-753` — `worst_val` takes the MIN across every
matched leaf regardless of unit (₹ `day_val` vs ₹/min vs %/min), so the ₹
`day_val` leaf's much larger magnitude always wins and the "material delta"
threshold ends up comparing against day P&L instead of the actual rate; wrapping
it in `abs()` also means an *improvement* of the same magnitude wrongly re-fires
the alert. Separately, this gate's cooldown reads the global `cfg['cooldown_min']`
(30) instead of the specific agent's own `cooldown_minutes` (10 for
`loss-rate-acct`), so the documented "10-minute critical cooldown" is actually
30. **Fix**: track the re-alert latch per `(leaf metric, account)` so units are
never mixed, drop the `abs()` (a move toward improvement should never re-fire),
and read the cooldown from the specific agent's own configured value.

**9. Static (non-rate) latch has no hysteresis and no escalation.**
`agent_engine.py:701-704` re-fires on every re-crossing of the threshold with no
buffer, producing repeat spam as a value oscillates around the line (4 fires in
one day for `loss-positions-acct` between -2.37% and -2.52%) — while conversely a
breach that only keeps deepening monotonically never re-alerts at all (a -31k
alert followed by a slide to -200k stays silent). **Fix**: add a re-arm band
(only clear the latch once the value recovers past e.g. 80% of the threshold,
not the instant it re-crosses) and escalation re-fires at meaningfully worse
multiples of the original threshold (e.g. 2× and 4×) even while still latched.

**10. Positions `day_pct`/`pnl_rate_pct` are computed against notional, not
margin, despite being described and thresholded as "% of margin."**
`background.py:267-297,333-349` — denominator is Σ|prev_close × quantity| over
CURRENT rows, but closed (qty=0) rows drop out of the denominator while staying
in the numerator, so a short-premium/mostly-closed book can produce absurd
percentages (worked example: -25k realized + one small open leg → `day_pct`
computes to -500%, firing a -2%-of-margin threshold that the actual P&L
wouldn't). Opening/closing a position also swings the ratio with zero P&L
change, producing phantom "rate" alerts. **Fix**: use actual margin (`util
debits`, matching what the description already claims) as the denominator, not
notional. Flag but do not blind-fix: audit noted MCX `quantity` may still be in
lots vs contracts at this specific point in `background.py` — verify units match
before wiring in the margin denominator (same lots-vs-contracts discipline as
the C1-C7 order-safety fixes elsewhere in this session).

**11. "Worst" scopes never match anything (latent, no built-in agent uses
them).** `grammar.py:408-437` — `holdings.worst_acct`/`worst_symbol` key on
`'day_pct'`, `positions.worst_acct` keys on `'pnl_pct'`, but the actual summary
frames only carry `day_change_percentage` — so `_row_with_min` always returns
`[]`. Only matters for operator-authored agents using these scopes today, but
it's a one-line key fix. **Fix**: correct the dict key lookups to match the
frame's actual column name.

## Also fold in (directly required for #7/#8/#9 to hold across deploys)

`_V2_LAST_ALERT` (the in-memory latch backing the static/rate re-alert logic) is
wiped on every process restart, and this app redeploys on every push to `main` —
so every deploy re-fires every currently-latched standing breach. The audit notes
a DB-backed cooldown gate (`_cycle_in_cooldown`) already exists and is "correct
as written" for a different purpose. Before implementing #7-#9, the backend agent
should determine whether extending that existing DB-backed mechanism to also
cover the per-leaf/per-account latch is the right vehicle (reuse) versus adding
new persistence for `_V2_LAST_ALERT` — investigate both during implementation
and pick whichever fits the existing schema/pattern with less duplication; note
the choice in the commit.

## Files

- `backend/api/algo/agent_evaluator.py` — #1 (rate window sample/span
  requirement), #2 (`all[]` same-account join).
- `backend/api/algo/grammar.py` — #3 (None-for-missing resolvers), #4 (live cash
  metric), #11 (worst-scope key fix).
- `backend/api/algo/agent_engine.py` — #3 (engine-side metric read at
  `:386-394`), #5 (don't unlatch on empty/failed fetch), #6 (per-leaf opening
  gate + segment-anchored baseline), #7 (suppression latch + same-tier dedup),
  #8 (per-leaf/account re-alert unit fix + correct cooldown source), #9
  (hysteresis + escalation), plus the deploy-survival latch fix.
- `backend/api/background.py` — #10 (margin, not notional, as the `day_pct`/
  `pnl_rate_pct` denominator; verify MCX lot/contract units first), #6 (segment
  open anchoring touches `_update_pnl_history`/`_get_segments` here too).
- `backend/brokers/adapters/dhan.py`, `backend/brokers/adapters/groww.py` — fix
  funds-field mapping gaps feeding #3 (report a field as absent/None rather than
  a coerced 0 when the broker's response genuinely doesn't carry it).

## Agents

- **backend**: all of the above (single agent — every file is under
  `backend/api/algo/` or `backend/api/`, all part of one coherent alert-
  evaluation pipeline; splitting would fragment a change where #3/#5/#6/#7/#8/#9
  interact within `agent_engine.py`).
- **broker**: the Dhan/Groww funds-field-mapping half of #3
  (`backend/brokers/adapters/dhan.py`, `groww.py`) — dispatched in parallel with
  the backend agent since it's a separate, narrower file set with no overlap.
- **backend-test**: pytest coverage for all 11 fixes, with particular focus on
  regression tests reproducing each prod-observed worked example from the audit
  (the paired-different-account `all[]` false fire, the one-sample rate spam,
  the missing-data re-arm, the notional-vs-margin `day_pct` blowup).
- **doc**: sync `CLAUDE.md` (there's currently no "alerts" section — add one
  summarizing the missing-vs-zero convention and the per-leaf gating/cooldown
  model, since this is exactly the kind of non-obvious invariant CLAUDE.md exists
  to capture) and note in the `/agents`-related guide (`docs/guides/AGENTS_GUIDE.md`
  if it exists) that fixed code defaults require the explicit prod sync step
  below before they take effect on already-created agent rows.

## Tests

- pytest: yes — `venv/bin/pytest backend/tests/ -q --tb=line`, new/updated
  coverage for every fix above.
- No frontend files are touched by this plan (`/agents` UI itself is out of
  scope — the audit didn't review it and no defect was found there); svelte-check
  should stay green as a byproduct but isn't the focus.

## Commit message

fix(alerts): correct 11 confirmed loss/rate-of-change alert condition bugs —
missing-data-as-zero, single-sample rate spam, cross-account all[] false
positives, notional-vs-margin day_pct, and re-alert/latch lifecycle gaps

## Done when

All 11 fixes have a regression test reproducing the original prod-observed
behavior and passing after the fix; `venv/bin/pytest backend/tests/ -q --tb=line`
green; self-audit confirms the None-for-missing convention is applied
consistently everywhere `_eval_leaf` reads a metric, not just at the two sites
named above.

## Flagged decision — prod agent-row conditions have already drifted from code

Prod's live `agents` DB rows for `loss-rate-acct` and `loss-pos-total-auto-close`
already differ from the code's current defaults (and `_ae_sync_existing_builtin`
will not silently overwrite them, by design, to protect operator customization).
After this fix ships, the corrected code defaults will not reach those two
already-created prod rows automatically. Before this plan can be considered fully
effective in prod, the operator needs to decide: (a) manually re-apply the
corrected conditions to those specific rows via `/agents`, (b) have me write a
one-off, explicitly-scoped migration that updates only those named rows (not a
blanket re-sync that could clobber other operator customizations), or (c) leave
prod's current (drifted) conditions in place and treat this fix as only affecting
newly-created agents. I'll surface this again with the specific before/after
condition diff once implementation lands, rather than deciding it now.
