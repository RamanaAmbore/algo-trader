# Plan: Unify the order grammar into the agent grammar (Phase 4 of 4)

## Task
Phase 4 (full plan: /Users/ramanambore/.claude/plans/purrfect-marinating-pixel.md;
Phases 1-3 already shipped: `a938340c`, `0005aeb8`, `7972b60b`).

Phase 2 made `backend/config/grammars/order_fields.yaml` the real single
source of truth for the overlapping order-field concepts (`qty`,
`order_type`, `price`, `trigger_price`, `product`, `variety`, `tag`,
`chase_level`) — but ONLY on the Python side, where `grammar.py` resolves
`$ref: <key>` markers in `agent_grammar.yaml`'s `place_order` schema. The
frontend CLI grammar (`backend/config/grammars/orders.yaml`, loaded by
`frontend/src/lib/command/grammars/orders.js`) still declares its own
literal `values:`/type info for the SAME concepts, cross-checked against
`order_fields.yaml` only by a pytest drift-guard test — not actually reading
the same file. **Read `order_fields.yaml`'s own header comments in full
first** — the Phase 2 agent already documented exactly why this is harder
than it looks (no cross-file YAML include mechanism available to the JS
loader without a real code change) and left precise notes on what's needed.

## Approach
1. Add `$ref: <key>` markers to `orders.yaml`'s token/kwarg entries for the
   overlapping roles — `qty` (role: qty), `orderType` (role: orderType →
   catalog key `order_type`), `price` (role: price), `chase` (role: chase →
   catalog key `chase_level`), and the `product` kwarg (on both `buy` and
   `sell` verbs) — replacing the current INERT `# catalog: order_fields.<key>`
   comments with a real, machine-readable field, mirroring the EXACT
   convention Phase 2 already established in `agent_grammar.yaml`'s
   `place_order` schema. Do NOT add `$ref` to tokens/kwargs that have no
   catalog equivalent (`account`, `instType`, `symbol`, `strike`, `expiry`,
   `order_id` — these stay exactly as they are, local-only).
2. In `frontend/src/lib/command/grammars/orders.js`: add an
   `order_fields.yaml?raw` import (same Vite `?raw` + `js-yaml` pattern
   already used for `orders.yaml?raw`). Add a resolution step — mirroring
   `grammar.py`'s Python-side `$ref` resolver in spirit, not copying its
   code — that runs BEFORE `_wireTokens`/`_wireKwargs` build their output:
   for any token/kwarg spec carrying a `$ref` key, merge the catalog
   entry's `type`/`enum` into the spec (mapping `enum` → the `values` field
   `_wireTokens`/`_wireKwargs` already read — check their exact current
   output shape first and preserve it exactly), while every LOCAL key on
   the orders.yaml spec itself (`required`, `parse`, `hint`, `kind`) stays
   exactly where it is and wins over anything the catalog might also
   define. This must be a genuine resolution step that actually reads
   `order_fields.yaml`'s content, not just a passthrough that still trusts
   `orders.yaml`'s own literal `values:` — if you keep BOTH the local
   `values:` and a `$ref` on the same entry, the catalog's `enum` must be
   what actually wins (or the two must be asserted equal with a loud error
   if they ever diverge, your call on which is safer — but don't silently
   let `orders.yaml`'s own stale literal win over the catalog once `$ref`
   is present, that would defeat the point of this phase).
3. Update `order_fields.yaml`'s own header comment (currently says a real
   single-sourced JS load "would require a follow-up change to orders.js
   itself... not made this phase") to reflect that this phase now does it.
4. For every file you change, write or update a test. Specifically:
   - A Playwright spec (source-level, `readFileSync` pattern already
     established this session — see `frontend/e2e/order_ticket_depth_pending_submit_gate.spec.js`
     for the exact style) proving `orders.js` now imports `order_fields.yaml?raw`
     and that its `$ref`-resolution logic exists and is wired before
     `_wireTokens`/`_wireKwargs` are built.
   - A test proving the WIRED token/kwarg output (`orderGrammar.verbs.buy.tokens`,
     etc.) is byte-identical to today's — same values, same order, same
     shape — now that it's sourced via `$ref` resolution instead of a bare
     literal. This is the critical regression guard: the CLI's actual
     parsing/suggester/payload-building behavior must not change at all.
   - Update the existing Python-side drift-guard test
     (`backend/tests/test_order_fields_catalog.py`) if its assumptions
     about `orders.yaml`'s structure change (it currently expects literal
     `values:` lists to drift-check against the catalog — if you ADD `$ref`
     alongside the existing `values:` rather than replacing it, this test
     may still work unchanged; if you replace `values:` with `$ref` only,
     this test needs updating to parse the ref instead). State clearly
     which you did and why.
5. Run `cd frontend && npx svelte-check --output machine` (0 new errors),
   `npx vitest run` (no regressions), your new Playwright spec, and the
   full backend suite (`venv/bin/pytest backend/tests/ -q --tb=short`) to
   confirm the Python-side drift-guard test (updated or not) still passes.

## Files
- `backend/config/grammars/orders.yaml` — add `$ref` markers to the
  overlapping token/kwarg entries only.
- `backend/config/grammars/order_fields.yaml` — header comment update
  (Phase 4 note), no structural change to the `fields:`/`chase_levels_cli_reference`
  content itself.
- `frontend/src/lib/command/grammars/orders.js` — `order_fields.yaml?raw`
  import + `$ref` resolution step wired before `_wireTokens`/`_wireKwargs`.
- `backend/tests/test_order_fields_catalog.py` — update if its assumptions
  about `orders.yaml`'s shape change.
- New Playwright spec for the frontend-side resolution + byte-identical
  wired-output regression guard.

## Tests
- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message
refactor(agents): frontend CLI grammar now reads order_fields.yaml directly (Phase 4 of grammar unification)

## Done when
`orders.js` actually reads `order_fields.yaml` for the overlapping field
concepts instead of relying on hand-kept-in-sync literals cross-checked
only by a drift-guard test. CLI parsing/suggester/payload behavior is
byte-identical before and after — proven by tests, not just inspection.
Full pytest suite + vitest + svelte-check + the new Playwright spec green.
