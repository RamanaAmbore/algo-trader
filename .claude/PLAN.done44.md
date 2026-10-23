# Plan: Rate-of-change alert gated by theoretical-vs-market loss divergence (per underlying)

(Written directly for the operator's "plan the rate-of-change theoretical-vs-market-loss alert"
request, synthesizing the two prior read-only scoping investigations from this session — design
feasibility and performance cost — into one implementable plan.)

## Context

The operator wants the existing rate-of-change loss alert to only fire when the gap between a
position's theoretical (Greeks-based) loss and its real observed market loss, for the underlying,
exceeds ₹100,000 — on the reasoning that a rate-of-change move driven by genuine repricing
(theoretical and market loss moving together) is less actionable than one where the market has
diverged sharply from what the Greeks predict. Two prior investigations (this session, read-only)
scoped the feasibility and performance; this plan turns those findings into an implementable
design.

Per the standing "DSL token reuse-first" rule: this plan reuses three pieces of existing,
unmodified infrastructure — the scope/metric token registry's plug-and-play resolution, the
already-computed per-underlying market P&L breakdown, and the existing Black-Scholes math module
— rather than inventing new mechanisms where current ones already fit.

## Current DSL (unchanged by this plan, for reference)

Today's rate-of-change condition (live on `loss-positions-total`, one leaf among four in an `any`
group):
```
{"metric": "pnl_rate_abs", "scope": "positions.any_acct", "op": "<=", "value": -6000}
{"metric": "pnl_rate_pct", "scope": "positions.any_acct", "op": "<=", "value": -0.25}
```
`scope: "positions.any_acct"` resolves (via `GrammarRegistry`'s scope-token kind) to one row per
account; `pnl_rate_abs`/`pnl_rate_pct` then evaluate per account row. Account-scoped only — no
underlying dimension exists anywhere in the condition system today.

## Key architectural finding (this session's investigation)

Scope resolution is itself a registered, plug-and-play token kind (`grammar_registry.py`'s
`('condition', 'scope')` loader) — a scope token resolves to a **list of rows**, and the leaf
evaluator (`agent_evaluator.py`'s `evaluate()`) calls `metric(ctx, row)` once per row, OR-combining
matches. This is exactly the `any_acct` fan-out mechanism, and it's generic: adding a new scope
token that resolves to **one row per underlying with an open position** (instead of one row per
account) gets automatic per-underlying fan-out for free, with zero changes to the core evaluator
loop. New condition shape, mirroring the existing account-scoped one exactly:
```
{"metric": "underlying_loss_divergence", "scope": "positions.any_underlying", "op": ">", "value": 100000}
```

**One real gap found, must be fixed as part of this plan, not worked around**: the match-entry
builder (`agent_evaluator.py` ~line 700) hardcodes `'account': row.get('account')` when recording
a match for latching. An underlying-scoped row has no `account` field, so every underlying's match
would collapse to the same `'TOTAL'` latch key — defeating independent per-underlying re-alert
behavior (the exact class of bug that causes confusing "repeat" alerts, which this plan must not
introduce a new instance of). Fix: the entry also captures `'underlying': row.get('underlying')`,
and `agent_engine.py`'s `_latch_key()` keys on `m.get('account') or m.get('underlying') or
'TOTAL'`.

## New tokens (registered, with info text, per the reuse-first rule)

1. **`positions.any_underlying`** (new scope token) — resolves to one row per distinct underlying
   root with at least one open position (any account), reusing the existing underlying-grouping
   logic `breakdown_positions_by_underlying()` already implements (`backend/shared/helpers/
   summarise.py:80`) rather than re-deriving it. Row shape: `{"underlying": <root>, "accounts":
   [...], ...}` (accounts list needed so the metric resolvers can pull the right position rows).

2. **`underlying_market_loss`** (new metric token) — the underlying's real P&L from
   `breakdown_positions_by_underlying()`'s own `pnl` field, which already exists and is currently
   used ONLY to enrich an alert message after firing, never as a trigger input. This token just
   exposes that same computed value as a condition-evaluable metric.

3. **`underlying_theoretical_loss`** (new metric token) — **genuine new computation, not just
   token exposure.** Groups the underlying's live legs, calls `multileg_greeks`/`expected_value`
   (`backend/api/algo/derivatives.py:1272/1455/1584` — reused verbatim, not reimplemented) using
   live spot + each leg's strike/expiry/IV (IV calibrated from each leg's own known market price,
   confirmed no extra broker call needed). Confirmed by the performance investigation: pure NumPy,
   zero I/O, microseconds-to-low-milliseconds per underlying, safe at the alert engine's existing
   5-minute poll cadence with today's real book size (currently 1 underlying in prod). No
   throttling/separate cadence needed.

4. **`underlying_loss_divergence`** (new metric token) — `abs(underlying_theoretical_loss −
   underlying_market_loss)` for the row's underlying. Built from the SAME computation as tokens 2
   and 3 (shared helper, not duplicated) — this is the metric the gating condition above actually
   compares against 100000. Exposing tokens 2 and 3 independently (not just the divergence) is
   deliberate per the operator's explicit ask ("theoretical value of loss vs market loss" as their
   own values) and lets a future condition reference either side individually.

All four tokens get: a catalog entry in `agent_grammar.yaml` with operator-facing `description`
text (per the reuse-first rule — no silent tokens), resolver wiring following the established
`minutes_since_open`-style pattern, and a golden-baseline amendment following the established
recipe (`agent_grammar_baseline.json` hand-amend + docstring Amendment paragraph in
`test_agent_grammar_yaml_catalog.py`).

## The gated condition itself

The existing rate-of-change leaves move from the top-level `any` group (where they fire
independently of each other and of the divergence check) into a nested `all` wrapping the
divergence gate around an `any` of the two rate leaves, **scoped per-underlying**:
```
all:
  - {"metric": "underlying_loss_divergence", "scope": "positions.any_underlying", "op": ">", "value": 100000}
  - any:
    - {"metric": "pnl_rate_abs", "scope": "positions.any_underlying", "op": "<=", "value": -6000}
    - {"metric": "pnl_rate_pct", "scope": "positions.any_underlying", "op": "<=", "value": -0.25}
```
Note `pnl_rate_abs`/`pnl_rate_pct` ALSO need an underlying-scoped reading (today they only read
`(section, account)` — add an underlying-keyed variant of the rolling P&L history, or resolve
account-level rate and attribute it to the underlying via the same account-to-underlying grouping
`breakdown_positions_by_underlying()` already does). This is new plumbing inside the SAME
`alert_state['pnl_history']` mechanism, not a new mechanism.

Whether this condition becomes a NEW agent (e.g. `loss-positions-underlying-divergence`) or is
edited into the existing `loss-positions-total` agent (replacing its two rate-of-change leaves) is
an operator decision, not made here — default recommendation: new, separate agent, so the existing
`day_val`/`day_pct` leaves on `loss-positions-total` are untouched and this is purely additive,
reducing blast radius on a live, already-tuned alert.

## Scope / Files

- `backend/api/algo/grammar.py` — 4 new resolvers (3 metric, following `_metric_minutes_since_open`
  shape) + registration wiring for the new `positions.any_underlying` scope token (follow the
  existing scope-token registration pattern, e.g. wherever `positions.any_acct` is registered).
- `backend/config/grammars/agent_grammar.yaml` — 4 new catalog entries with description text.
- `backend/api/algo/agent_evaluator.py` — match-entry builder gains `'underlying': row.get(...)`.
- `backend/api/algo/agent_engine.py` — `_latch_key()` amended per the gap above; underlying-keyed
  `pnl_history` plumbing for the rate tokens' underlying-scoped reading.
- `backend/tests/golden/agent_grammar_baseline.json` + `test_agent_grammar_yaml_catalog.py` —
  hand-amended per the established recipe.
- New `backend/tests/test_underlying_loss_divergence_tokens.py` — per-token true/false tests,
  including a multi-underlying scenario proving independent per-underlying latching (the gap
  this plan fixes), and an end-to-end `run_cycle` wiring test.
- No frontend changes — this is a condition/alert-engine-only feature.

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

feat(grammar): gate rate-of-change loss alerts on theoretical-vs-market loss divergence, per underlying

## Done when

- All 4 new tokens resolve correctly and are catalog-registered with info text.
- `underlying_loss_divergence > 100000` correctly gates the rate-of-change leaves in the example
  condition tree above, verified with a multi-underlying test case where one underlying crosses
  the divergence threshold and another doesn't, in the same poll cycle.
- Independent per-underlying latching confirmed (two underlyings both breaching don't collapse
  into one shared latch key).
- No existing agent's behavior changes unless the operator explicitly chooses to edit
  `loss-positions-total` directly (default: new separate agent).
- Golden-baseline catalog test green. Backend pytest green.
