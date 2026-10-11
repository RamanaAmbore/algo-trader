# Plan: Sprint 19 backend — parameterized symbol-filtered scope resolution

## Task

Read the full "Sprint 19" section of `~/.claude/plans/purrfect-marinating-pixel.md`
before starting — extensively negotiated this session (account:X/*/worst,
symbol:X/*, short scope aliases, the book/market-data qualifier
unification). This PLAN.md covers ONLY the backend piece that's
genuinely new work — the account-level qualifiers and short aliases
are pure frontend aliases onto EXISTING scope tokens (any_acct/
worst_acct/worst_symbol), zero backend work, and are a SEPARATE
dispatch once `frontend/src/lib/command/grammars/agents.js` is free
(a different agent is mid-edit on it for a Sprint 18 follow-up right
now — do not touch that file).

**The one thing that needs real backend work**: `symbol:VALUE` as a
qualifier within a per-position scope (`positions.expiring_today`,
where one account can hold several different symbols). Confirmed
UNSAFE to express as two separately-ANDed leaves (`symbol==X &
metric<=threshold`) — `_eval_all`'s join key is `account` only, not
`(account, symbol)`, so the two leaves could spuriously agree on
account while referring to different symbol-rows. There's also no
existing scope token to alias onto (unlike `account:*`→`any_acct`).
This needs a genuine new parameterized scope-resolution path.

## Agent

- backend:
  1. **Design the compiled-JSON shape for a symbol-filtered scope.**
     Today's leaf format is `{"metric", "scope", "op", "value"}` where
     `scope` is a plain string resolved via `REGISTRY.scope(scope_tok)`
     — a zero-argument lookup. A symbol-filtered scope needs to carry a
     runtime parameter (the specific symbol). Two real options, pick
     the one that fits the existing resolver architecture better after
     reading `grammar_registry.py`'s real `REGISTRY.scope()` mechanism
     in full:
     - (a) Encode the parameter INTO the scope string itself, mirroring
       how metric factory tokens already encode a parameter (e.g.
       `mean_pnl(30)` is a real, already-working precedent for "a
       string that both names a resolver AND carries its argument") —
       e.g. `"positions.expiring_today(symbol=NIFTY25JULFUT)"` or
       similar, parsed by a new scope-factory resolver mirroring
       `_metric_factory_mean_pnl`'s pattern.
     - (b) Add a new, OPTIONAL key to the leaf JSON shape itself (e.g.
       `"scope_filter": {"symbol": "NIFTY25JULFUT"}`), applied by
       `_eval_leaf` as a row-filter AFTER `scope_fn(ctx)` returns rows
       but BEFORE the metric evaluates each one.
     Recommend (a) for consistency with the existing metric-factory
     precedent (same "parameterized token" pattern, not a parallel
     mechanism) unless reading the real scope-registry code reveals a
     concrete reason (b) is meaningfully simpler — state your reasoning
     and final choice clearly in your handback, a frontend agent will
     build the compiler against your exact chosen shape next.
  2. **Implement the chosen resolver.** Must filter the real per-
     position rows (`ctx.position_rows`) to exactly the rows matching
     the given symbol, reusing the SAME underlying row-selection logic
     `_scope_positions_expiring_today` etc. already use — don't
     duplicate the expiry/MCX-unhedged filtering logic, just add the
     symbol filter as one more predicate on top of it. Support this
     for EVERY existing per-position scope (`expiring_today`,
     `expiring_today.nfo`, `expiring_today.mcx_unhedged`) — the symbol
     filter should compose with whichever base per-position scope it's
     applied to, not require a hand-written combination for each.
  3. **Confirm the save-time validator** (`agent_evaluator.py:validate()`,
     already reads `_resolve_metric_fn`/`REGISTRY.scope()`) correctly
     accepts the new parameterized scope shape — add a test proving an
     agent using it can actually be saved via `create_agent`, not just
     evaluated.

  **Test coverage (mandatory)**:
  - A condition leaf using the new symbol-filtered scope, evaluated
    against a synthetic multi-symbol-same-account `position_rows` list,
    correctly matches ONLY the specified symbol's row(s) — prove the
    exact bug this closes: a different symbol in the SAME account with
    a breaching metric value must NOT fire.
  - The symbol filter composes correctly with `expiring_today.nfo` and
    `expiring_today.mcx_unhedged` (not just the bare `expiring_today`
    scope).
  - Save-path test: `create_agent` accepts a condition using the new
    shape without a 422.
  - Regression: every EXISTING scope token (unfiltered) still resolves
    exactly as before — this change must be purely additive.

  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -k "grammar or evaluator or scope or agent" -q --tb=line`, then confirm zero regressions with the full suite given this touches the condition evaluator's core resolution path.

## Tests
- pytest: yes, full suite given this touches `agent_evaluator.py`
- svelte-check: no (backend-only this dispatch)
- playwright: no

## Commit message
feat(agents): Sprint 19 backend — parameterized symbol-filtered scope resolution

## Done when
- A condition can filter a per-position scope to one specific symbol
  without the cross-leaf join-granularity bug.
- The chosen compiled-JSON shape is documented clearly enough for a
  frontend agent to build a compiler against it with zero guessing.
- Full backend suite green, zero regressions in existing scope
  resolution.
