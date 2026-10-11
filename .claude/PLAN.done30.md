# Plan: Sprint 15 — per-scope exposed-column registry + catalog projection + dot-attribute grammar

## Task

Read the FULL "Sprint 15" section of `~/.claude/plans/purrfect-marinating-pixel.md`
before starting — it has ~150 lines of already-negotiated design detail
(naming standard, label sync, catalog projection, documentation
rendering) that this PLAN.md summarizes but does not repeat in full.

Add generic `scope.attribute` dot-access to the `WHEN` clause, reading
either a real DataFrame column or an existing bare (zero-arg) metric
token off a scope's row — e.g. `positions.total.pnl <= -50000`
(equivalent to today's `pnl@positions.total <= -50000`, but works for
ANY column already on the row, not just ones with a hand-registered
metric token). Every exposed column goes through a NEW per-scope
registry (`exposed_name → {type, from_column, label}`) — never a raw
column passthrough — and that registry PROJECTS into the live token
catalog (same mechanism Sprint 10 used to merge Token/fragment rows),
so every exposed column is browsable/discoverable in the exact same
place metrics/scopes/channels/actions already are.

**Explicitly OUT of scope for this sprint** (Sprint 16, dispatched
after this one lands): bare-NAME column reads inside `DO` action
arguments (the matched-row-fan-out mechanism), and the `agent_engine.py`
dispatch-loop change that makes `DO` fire once per matched row. This
sprint is WHEN-side (`scope.attribute` dot-access) and catalog/
discoverability only.

## Agents

- backend:
  1. **Build the per-scope exposed-column registry.** For EVERY real
     scope token (`positions.total`, `positions.any_acct`,
     `positions.expiring_today`, `positions.expiring_today.nfo`,
     `positions.expiring_today.mcx_unhedged`, `positions.worst_acct`,
     `positions.worst_symbol`, `holdings.total`, `holdings.any_acct`,
     `holdings.worst_acct`, `holdings.worst_symbol`, `funds.total`,
     `funds.any_acct`, `watchlist.all`, `watchlist.default`,
     `watchlist.markets` — confirm this is the complete real list via
     `grep -n "^- grammar_kind: condition" -A2
     backend/config/grammars/agent_grammar.yaml | grep -A1 "token_kind:
     scope"`), discover the REAL columns available on that scope's row
     by reading the actual DataFrame-building code
     (`backend/api/background.py`'s `_rebuild_positions_summary`/
     `_rebuild_holdings_summary` and whatever builds `df_margins`/
     `watchlist_rows`; `backend/api/algo/grammar.py`'s own scope
     selector functions, e.g. `_scope_positions_expiring_today` —
     already read partially this session, confirmed real columns
     include `account`/`pnl`/`day_change_val`/`pnl_percentage` for
     aggregates, `tradingsymbol`/`qty`/`avg_price`/`ltp` for per-
     position rows, `'avail cash'`/`'avail opening_balance'` for funds
     — verify the FULL set yourself, don't assume this list is
     exhaustive). For each real column, add ONE registry entry:
     `{exposed_name, type, from_column, label}`:
     - `exposed_name`: clean identifier (regex `^[A-Za-z_][A-Za-z0-9_]*$`,
       no spaces, no dots). Reuse an EXISTING metric/field name where one
       already exists for the same concept (e.g. funds' `'avail cash'`
       column should expose as `cash`, matching the existing `cash`
       metric token's name — grep `agent_grammar.yaml`'s metric tokens
       for name collisions to reuse deliberately, not accidentally).
     - `type`: `number`/`string`/`boolean`, matching
       `compileLeaf`'s existing `valueType` vocabulary.
     - `from_column`: the REAL, possibly-messy DataFrame column name
       (`'avail cash'`, `tradingsymbol`, etc.) — never renamed, never
       touched in the actual data layer.
     - `label`: the operator-facing display text (e.g. `'Cash'`,
       `'Avail Margin'`) — the plan doc's "label sync" section explains
       why this matters; best-effort match whatever the real UI already
       calls this concept if you can find it (grep frontend grid
       headerNames for the same concept), otherwise a sensible title-case
       of the exposed name.
     - **Semantic-collision check (not just syntactic)**: if a column's
       real meaning differs by scope family in a way that could mislead
       (the plan doc's worked example: `watchlist_rows`' `account` field
       actually holds a watchlist NAME, not a trading account — confirmed
       via `Context`'s own field comment, `agent_evaluator.py:170-174`),
       rename the EXPOSED name to disambiguate (`watchlist_name`, not
       `account`) even though `from_column` stays the real, unchanged
       field name.
     Where exactly to put this registry (a new section in `grammar.py`,
     likely a dict keyed by scope token string) is your call — follow
     the file's existing conventions for this kind of lookup table.
  2. **Build-time enforcement test** (mandatory, not optional): a pytest
     test walking the ENTIRE registry asserting (a) every `exposed_name`
     matches the identifier regex, (b) every `exposed_name` does NOT
     collide with any enum constant value from ANY enum-typed param
     across the whole action/catalog token set (cross-check against the
     full enum-value universe — `order_type`'s `[MARKET,LIMIT,SL,SL-M]`,
     `side`'s `[BUY,SELL]`, `product`'s `[MIS,CNC,NRML]`, every other
     enum field — read `agent_grammar.yaml`/`order_fields.yaml` for the
     complete set, don't hardcode a partial list), (c) every entry has a
     non-empty `label`.
  3. **Catalog projection** (`backend/api/routes/grammar.py`): add a
     `_merge_exposed_column_rows()` sibling to the existing
     `_merge_fragment_rows()` (read that function first, copy its
     gating/merge pattern exactly — likely gated on `grammar ==
     'condition'`, since these are WHEN-side reads). For every registry
     entry, synthesize a `GrammarTokenOut`-shaped row: `token` = however
     you decide to name it so the frontend can resolve it unambiguously
     against its owning scope (your call on exact shape — e.g. a
     compound string like `"positions.total.pnl"`, or the bare
     `exposed_name` with scope carried in a separate field if
     `GrammarTokenOut` already has room for one; confirm which is
     cleaner given the frontend's existing catalog-building code before
     deciding, since the frontend agent in this same sprint needs to
     consume whatever shape you pick — state your exact chosen shape
     clearly in your handback, since the frontend agent depends on it),
     `grammar_kind: 'condition'`, `token_kind: 'column'` (new kind),
     `value_type` = the registry entry's `type`, and `description` =
     a provenance string stating the real `from_column` and which
     function/scope produces it (e.g. `"Current cash balance. Sourced
     from DataFrame column 'avail cash' on ctx.df_margins, populated by
     grammar._scope_funds_total."`).
  4. **Test the projection end-to-end**: a pytest test confirming
     `list_tokens(grammar='condition')` includes the new column rows
     with correct `token_kind='column'`, correct `description`
     mentioning the real `from_column`, and correct `value_type`.

  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -k "grammar or catalog or scope" -q --tb=line`

- frontend: (dispatch AFTER the backend agent reports its chosen
  `GrammarTokenOut` shape for column tokens — read its handback first,
  don't guess the shape)
  1. **Dot-attribute grammar**: extend the `scope_ref` production
     (`frontend/src/lib/command/grammars/agents.js`) so a `leaf`'s
     `scope_ref "." NAME` can resolve its FINAL segment as either (a) a
     deeper scope-chain segment (existing behavior — e.g.
     `positions.expiring_today.mcx_unhedged` is a more specific SCOPE,
     not a column read) or (b) — NEW — a column/bare-metric read on the
     scope resolved so far, when the full chain doesn't match a longer
     known scope name. Resolve ambiguity by checking the longest
     possible scope-chain match FIRST, falling back to "last segment is
     a column read" only when no longer match exists.
  2. **Column resolution**: build a catalog lookup (mirroring
     `_buildCatalogEntry`'s existing pattern) for the new `token_kind:
     'column'` rows the backend now projects, keyed by scope + exposed
     name per whatever shape the backend agent chose. `compileLeaf`
     needs to accept a resolved COLUMN entry as an alternative to a
     resolved METRIC entry — same downstream comparator-type-checking
     (`valueType`-based), just a different resolution path upstream.
  3. **Reuse, don't duplicate**: the "boolean metric only supports
     ==/!=", "numeric comparator requires numeric value", "between
     requires numeric type" checks in `compileLeaf`/`compileBetween`
     should work identically for a resolved column entry as they already
     do for a resolved metric entry — if these checks are currently
     keyed narrowly to "is this a metric", widen the check to "does this
     resolved entry have a `valueType`", not "is it specifically a
     metric-kind token".
  4. **Documentation rendering** (independent sub-task, can be done in
     parallel with 1-3 by the same agent): extend `AgentCliEditor.svelte`
     (Sprint 11's hover/focus description mechanism) so a param carrying
     `required_if` renders an extra generated line (e.g. `"price —
     required for LIMIT/SL"`, built FROM `required_if.field`/
     `required_if.in`, not hand-written per field) and a param inside a
     `mutually_exclusive_groups` entry renders `"X is mutually exclusive
     with Y"` naming the other group member(s). This closes the
     "structured data exists since Sprint 17 but nothing renders it
     human-readably" gap the operator flagged.

  **Test coverage (mandatory)** in `agentsGrammar.test.js`:
  - `positions.total.pnl <= -50000` compiles identically to
    `pnl@positions.total <= -50000` (same output JSON).
  - A column with no comparator, boolean-typed (if any such column
    exists in the real registry) behaves like the existing bare-boolean-
    metric shorthand.
  - A string-typed column compared with a numeric comparator → the
    SAME "comparator requires both sides numeric" error metric leaves
    already produce.
  - `positions.expiring_today.mcx_unhedged` (existing multi-segment
    SCOPE chain) still resolves as a scope, not a column read —
    explicit regression guard that longest-match-first disambiguation
    didn't break existing multi-segment scope references.
  - A `required_if`-carrying param's hover description includes the
    generated requirement text; a `mutually_exclusive_groups` member's
    description names the other member.

  Run:
  ```
  cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -30
  cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -30
  ```

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no (pure compiler/catalog change, no new UI surface this
  sprint — Sprint 16 will need one for the fan-out behavior)

## Commit message
feat(agents): Sprint 15 — generic scope.attribute dot-access, per-scope column registry projected into token catalog

## Done when
- `positions.total.pnl <= -50000` compiles to the same JSON as
  `pnl@positions.total <= -50000`.
- Every real DataFrame column exposed this way appears as a discoverable
  `token_kind:'column'` row in the live catalog, with a description
  stating its real source column and producing function.
- No real DB/DataFrame column was renamed anywhere — confirm via `git
  diff` that `backend/api/background.py` and the scope-selector
  functions in `grammar.py` are UNCHANGED except for whatever net-new
  registry/merge code was added; existing column names stay exactly as
  they are.
- A build-time test proves no exposed name collides with any enum
  constant anywhere in the catalog.
- `AgentCliEditor.svelte`'s hover description renders `required_if`/
  `mutually_exclusive_groups` as plain English.
- Full backend + frontend test suites green.
