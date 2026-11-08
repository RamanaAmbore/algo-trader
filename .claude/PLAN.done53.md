# Plan: Sprint 1 — exit-rule agent DSL foundation + verification spikes

## Task

Sprint 1 of a larger, multi-sprint plan (full detail in `~/.claude/plans/sparkling-dancing-lobster.md`)
to add one new condition-agent to RamboQuant's agent engine that auto-closes a short option
position once it reaches 90% profit, staged through a hold-for-review mechanism with a timed
auto-release. **This sprint is foundation-only.** It adds two independent DSL pieces to the
agent-engine's condition grammar, fixes a pre-existing latch-keying bug, and runs two
verification spikes that later sprints depend on. It does NOT seed the real agent, add any
hold/release mechanism, add any position lock, or touch any live order-placement code — those
are explicitly later sprints, deferred until this sprint's spikes confirm their assumptions.

## Agents

- backend: Add two new DSL pieces to RamboQuant's agent condition-evaluation engine
  (`backend/api/algo/`), plus a latch-keying fix, plus two verification spikes. Read
  `backend/api/algo/grammar.py`, `backend/api/algo/grammar_tokens/` (for the pattern other
  scope/metric tokens follow), `backend/api/algo/agent_engine.py`, `backend/api/algo/actions.py`,
  `backend/api/algo/actions_live.py`, and `backend/api/algo/chase.py` before writing anything —
  match this codebase's exact existing conventions, don't invent new ones.

  1. New scope token `positions.any_symbol`, registered in `grammar.py`'s `EXPOSED_COLUMNS`/
     `EXPOSED_COLUMN_SCOPE_PROVENANCE`, the YAML catalog (`backend/config/grammars/agent_grammar.yaml`),
     and the golden baseline test fixture. Must return EVERY held position row independently (one
     row per position), unlike `any_underlying` (aggregate) or `worst_symbol` (single row).

  2. New metric `gain_from_entry_pct` — long (qty>0): `(ltp - avg_price) / avg_price * 100`.
     Short (qty<0): `(avg_price - ltp) / avg_price * 100`. MUST return `None` (never a coerced 0)
     when: `avg_price` is missing, <=0, or non-finite; `ltp` is missing or <=0; or the row's
     `price_source` field is a degraded tag — grep `positions.py`, `positions_helpers.py`,
     `holdings.py` for the CURRENT full list of degraded `price_source` values before finalizing
     (at minimum `snapshot_settled` and `pending_open` are confirmed to exist; do not assume
     that's exhaustive). `avg_price` means the broker's blended cost-basis for the net position —
     same field used everywhere else in this codebase for P&L (e.g. Holdings' `inv_val = avg ×
     quantity`) — NOT per-tranche/per-entry tracking, which doesn't exist for this purpose and is
     out of scope.

  3. Latch-key fix: find `_latch_key` in `agent_engine.py`. It currently keys on
     `(agent_slug, metric, scope, account_or_underlying)` with no symbol dimension — two
     different legs in the same account can collapse onto one latch. Add a `tradingsymbol`
     fallback, following the exact same pattern as the existing `underlying` fallback already in
     that function.

  4. Verification spike A (write a real test, not a verbal assertion): confirm that when an
     agent's condition uses a fan-out scope (matching multiple rows in one cycle — trace how
     per-row/per-match binding works for dispatch in `agent_engine.py`/`execute()`), each matched
     row produces its own independent action dispatch carrying THAT row's own
     account/symbol/exchange — not the first matched row's values reused for all matches, and
     not one aggregated dispatch. If this does NOT work correctly, STOP and report it as a
     blocking finding in your summary instead of working around it — a larger planned feature
     depends on this behaving correctly, and silently patching around a structural problem here
     would hide it from the next sprint.

  5. Verification spike B (also a real test): trace where `chase_order()` is actually called
     from, starting at `backend/api/algo/actions_live.py`'s `_action_live_close_position` AND at
     `backend/api/algo/order_release.py`'s `_chase_released` (called from `release_held_order`).
     Confirm and report whether these two paths share a common chokepoint before reaching the
     broker, or are two independent call sites that both call `chase_order()` directly with
     nothing shared between them. Report this finding clearly — a later sprint adds a
     position-level lock at whichever point is actually shared, and this finding decides where.
     Do not add any lock yourself this sprint — report only.

  Do NOT touch: `template_attach.py`, anything Bracket/GTT-related, `HoldCategory`,
  `record_held_order`, `release_held_order`, `_dispatch_live_action`'s existing behavior, or any
  `Agent` model columns. Those belong to later sprints and are explicitly out of scope here.

- frontend: skip
- broker: skip
- doc: skip
- backend-test: Write pytest tests for all 5 pieces above: (a) `gain_from_entry_pct` correct for
  a normal long and short case, and returns `None` for each degraded-data case (missing
  avg_price, avg_price<=0, missing/zero ltp, each degraded `price_source` tag the backend agent
  actually found); (b) the latch-key fix produces two independent latch keys for two different
  tradingsymbols in the same account/scope/metric; (c) spike A's per-row fan-out binding test;
  (d) spike B's finding, expressed as a test assertion where possible or a clearly documented
  comment if it genuinely can't be an assertion. Read what the backend agent actually implemented
  before writing tests against assumed signatures — do not guess function names.
- playwright: skip

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

feat(agents): Sprint 1 of exit-rule agent — positions.any_symbol scope, gain_from_entry_pct
metric, _latch_key tradingsymbol fallback, and two verification spikes (per-row dispatch
fan-out, chase_order chokepoint) for the planned 90%-profit auto-close agent.

## Done when

- `positions.any_symbol` scope and `gain_from_entry_pct` metric are registered and reachable
  through the grammar catalog (golden baseline updated).
- `_latch_key` includes a `tradingsymbol` fallback.
- A test proves per-row fan-out dispatch binds each row's own account/symbol/exchange correctly
  — or the spike has reported a blocking finding instead of a silent workaround.
- A test or clearly documented finding establishes where `chase_order()`'s real shared
  chokepoint is (or confirms there isn't one) for a future position-level lock.
- `gain_from_entry_pct` returns `None`, not a false reading, for every missing-data case listed
  above — tested explicitly, including whatever full list of degraded `price_source` tags the
  backend agent actually found in the codebase.
- Full backend suite green.
- No changes to `template_attach.py`, any Bracket/GTT code path, `HoldCategory`, or any `Agent`
  model column.
