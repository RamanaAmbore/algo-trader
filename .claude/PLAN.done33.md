# Plan: Sprint 21 frontend — expose orders.* and quote.* scopes in the CLI grammar

## Task

**Do not start until Sprint 20 is committed** — both touch
`frontend/src/lib/command/grammars/agents.js`.

Backend half is committed (`e8b1ad38`). Confirmed shapes, read the real
diff to verify before building (don't trust this summary blindly):

- `orders.any_acct` — account-rooted, one row per open `AlgoOrder`
  (live mode, non-terminal status). Exposed columns:
  `account_id`→account (string), `symbol`→tradingsymbol (string),
  `exchange`→exchange (string), `side`→side (string, BUY/SELL),
  `qty`→quantity (number), `price`→price (number), `status`→status
  (string), `order_id`→order_id (string — matches `cancel_order`/
  `modify_order`'s own `order_id` action param exactly, so
  `{"$row":"order_id"}` fan-out needs zero translation).
- `quote.tracked` — instrument-rooted, requires a non-wildcard
  `scope_filter.symbol` at compile time (no meaningful "all tracked
  symbols" aggregate — reject `quote.tracked` with no symbol qualifier
  or with `symbol:*`, mirroring the backend's own `validate()`
  rejection). Exposed columns: `symbol`→tradingsymbol, `exchange`→
  exchange, `ltp`→last_price (reuses the real `ltp` metric token),
  `bid`→bid, `ask`→ask, `volume`→volume (all number except
  symbol/exchange).
- Both scopes already flow through Sprint 15's `EXPOSED_COLUMNS`
  registry projection into the catalog and Sprint 19's qualifier/alias
  mechanism automatically — confirm this by checking whether
  `catalog.scopes`/`catalog.columns` already contain these two new
  entries once the frontend fetches the live catalog (they should,
  with ZERO code change needed for the base dot-access/qualifier
  grammar — Sprint 15/19's mechanisms are already generic over the
  scope-token list). The ONLY new frontend work needed is the
  compile-time guard specific to `quote.tracked`.

## Agent

- frontend:
  1. **Verify the generic mechanism really does pick these up for
     free** — write a test using the real fetched catalog shape (or a
     realistic fixture mirroring it) proving `orders.any_acct.status`,
     `orders.any_acct.order_id`, `quote.tracked.ltp` (with a
     `symbol:` qualifier) all compile via the EXISTING dot-access code
     path, with no new parser/compiler branch. If this does NOT work
     for free (e.g. because `orders`/`quote` aren't recognized as valid
     scope-family roots somewhere that hardcodes the existing four),
     find and fix that one spot — don't build a parallel mechanism.
  2. **`quote.tracked`'s compile-time guard**: reject `quote.tracked`
     (and its short-alias spelling, if `quote` gets one — check Sprint
     19's `SCOPE_FAMILY_ALIASES`, it probably doesn't need one since
     `quote` is already short) when there's no `symbol:VALUE` qualifier
     or when the qualifier is `symbol:*` — clear compile error ("quote.
     tracked requires a specific symbol: qualifier, e.g.
     quote.tracked.symbol:NIFTY25JULFUT — no 'any symbol' aggregate
     exists for market data").
  3. **`$row` bare-name/`result.field` binding** (Sprint 16's
     mechanism) should now also work for `order_id`/`status`/`side` on
     a matched `orders.any_acct` row, e.g.
     `DO cancel_order(order_id=order_id)` inside a
     `WHEN ... @orders.any_acct` condition. Verify this works via the
     EXISTING Sprint 16 mechanism (no new code expected — confirm with
     a test) given `order_id` is now a real exposed column.

  **Test coverage (mandatory)**:
  - `orders.any_acct.status == "OPEN"` compiles correctly.
  - `cancel_order(order_id=order_id)` inside a `WHEN ... @orders.
    any_acct` agent's `DO` clause compiles to the `$row` marker shape.
  - `quote.tracked.ltp` with NO symbol qualifier → compile error.
  - `quote.tracked.symbol:*.ltp` (wildcard) → compile error (same
    reason).
  - `quote.tracked.symbol:NIFTY25JULFUT` with `.ltp <= 100` → compiles
    successfully.
  - Decompile round-trip for both new scopes.

  Run:
  ```
  cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -30
  cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -30
  ```

## Tests
- pytest: no (backend already shipped)
- svelte-check: yes
- playwright: no

## Commit message
feat(agents): Sprint 21 frontend — expose orders.* and quote.* scopes in the CLI grammar

## Done when
- `orders.any_acct` and `quote.tracked` are typeable in WHEN conditions
  using the exact same dot-access/qualifier mechanism every other scope
  already uses — confirmed via test, not assumed.
- `quote.tracked` without a specific symbol qualifier is a clear
  compile error.
- `$row` fan-out binding works for order columns (`order_id`/`status`).
- Full frontend test suite green.
