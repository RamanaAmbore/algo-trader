# Plan: Phase 3 corrected — delete dead action-registry plumbing (not merge)

## Context

This supersedes the original Phase 3 ("Make action dispatch genuinely
registry-driven") from the grammar-unification plan below. Phases 1, 2, and
4 of that plan already shipped (commits `a938340c`, `0005aeb8`, `1a6869b0`)
and are left in this file only as background — nothing further is needed
on them. The queued `qty`/`quantity` schema mismatch was also already fixed
earlier this session.

A background research pass (full read of `actions.py`, `actions_live.py`,
`grammar_registry.py`, `grammar.py`, plus a repo-wide grep) found the
original Phase 3 premise was wrong: there are not two live, competing
dispatch paths to merge. `_LIVE_ACTION_HANDLERS`/`_NOOP_ACTION_HANDLERS`
(`actions.py:196-227`) are the real, sole dispatch SSOT — called from
`_dispatch_live_action`/`_al_run_noop_handler` (`actions.py:230-271`),
routing to real `_action_live_*` functions in `actions_live.py` (signature
`(agent, context, params)`).

The "registered" side is dead code, not a parallel implementation.
`grammar.py`'s `place_order`/`modify_order`/`cancel_order`/
`cancel_all_orders`/`close_position`/`chase_close_positions`/
`expiry_auto_close` stubs (`actions.py:605-646`) are pure `_log_invoke()`
no-ops — their own docstrings admit the real wiring lives elsewhere.
`grammar_registry.py`'s `_load_action` loader (line 72) seeds these stubs
into `REGISTRY.actions[token]`, but `REGISTRY.action(token)` — the only
accessor that would ever call `fn` — **is never called anywhere in the
backend** (verified by grep). Only `len(REGISTRY.actions)` is read, for an
admin stats count (`routes/grammar.py:205`). So "unifying" by changing
`_action_live_*`'s signature to match the registry's expected shape would
mean touching 14 test files and real broker-call code for zero functional
gain — there is nothing live on the other side to converge with.

The 4 noop handlers (`monitor_order`/`deactivate_agent`/`set_flag`/
`emit_log`) are NOT part of this problem — `_NOOP_ACTION_HANDLERS` already
points at the exact same real functions, called with their real
`(context, params)` signature. Nothing to change there.

**Revised Phase 3: delete the dead stub plumbing**, not merge live code
into it. Zero behavior change to any live/paper/sim path. This is the
low-risk version of "make the registry accurately reflect what's real" —
it removes the misleading dead code instead of inventing new risk on
live-order-placing machinery to satisfy an accessor nothing calls.

Scoped to the smallest change that removes the misleading code: `_load_action`
(`grammar_registry.py:72`) already handles a missing resolver gracefully
(`'fn': _import_dotted(r.resolver) if r.resolver else None`) — so once the 7
dead stub functions are deleted and nothing seeds a `resolver` path for
`action_type` rows anymore, `REGISTRY.actions` naturally becomes a catalog of
`{token: {"fn": None, "params_schema": {...}}}` entries with no code change
needed in `grammar_registry.py` itself or in `routes/grammar.py`'s stats
count (`len(REGISTRY.actions)` keeps counting declared action tokens, which
is still an accurate, useful number). `REGISTRY.action()` stays as a 2-line
accessor — confirmed zero callers, but deleting a harmless accessor isn't
worth a second file touched for no behavior change. **Do not** delete
`_load_action`/`self.actions`/`.action()` — that was an earlier, needlessly
larger draft of this plan; the smaller version above is correct.

**Side finding, explicitly out of scope for this plan**: `agent_ai.py`'s
`_grammar_snapshot()` (line 98) iterates `REGISTRY.tokens.values()`, but
`GrammarRegistry` has no `tokens` attribute anywhere in
`grammar_registry.py` — this looks like a live `AttributeError` whenever
that function runs. Not touched here; flagged for a separate, dedicated
fix since it's unrelated to action-dispatch unification.

## Approach

1. In `backend/api/algo/actions.py`, delete the 7 dead broker-action stub
   functions at lines 605-647 (`place_order`/`modify_order`/`cancel_order`/
   `cancel_all_orders`/`chase_close_positions`/`expiry_auto_close`/
   `close_position` grammar-resolver shims — verified exact range: `place_order`
   starts at 605, `close_position` ends at 647, `monitor_order` (a real noop,
   keep) starts at 649). Keep the 4 real noop functions
   (`monitor_order`/`deactivate_agent`/`set_flag`/`emit_log`) — those are
   genuinely called via `_NOOP_ACTION_HANDLERS` and must stay untouched.
2. In `backend/config/grammars/agent_grammar.yaml`, delete the single
   `resolver: backend.api.algo.actions.<fn>` line from each of the 7
   now-dead action entries (verified exact lines: `place_order` resolver at
   570, `modify_order` at 678, `cancel_order` at 703, `cancel_all_orders` at
   726, `chase_close_positions` at 753, `expiry_auto_close` at 786,
   `close_position` at 809) — leave each entry's `token`/`description`/
   `params_schema` untouched. The 4 real noop entries (`monitor_order` at
   855, `deactivate_agent` at 878, `set_flag` at 884, `emit_log` at 898,
   approximate) keep their `resolver:` line unchanged — they point at real,
   kept functions. This also
   prevents a noisy (harmless but confusing) `_import_dotted` failure +
   WARNING log on every `REGISTRY.reload()`, since `_load_action` would
   otherwise try to dynamically import a function that no longer exists.
   Keep seeding `params_schema`/`description` for these rows unchanged —
   `agent_ai.py`'s `_summarise_token()` reads `token.params_schema` directly
   from the DB row (unrelated to `REGISTRY.actions[token]["fn"]`) for the
   Lab-chat agent-builder, and that must keep working.
3. Leave `grammar_registry.py` itself untouched — `_load_action` already
   degrades gracefully when `r.resolver` is falsy (`'fn': ... if r.resolver
   else None`), so `REGISTRY.actions` naturally becomes
   `{token: {"fn": None, "params_schema": {...}}}` with no code change
   needed there, and `routes/grammar.py:205`'s stats count
   (`len(REGISTRY.actions)`) keeps working unchanged — it's still an
   accurate count of declared action tokens.
4. Delete the 2 tests in `test_actions_coverage.py` that exercise only the
   dead stubs (`test_place_order_grammar_handler`,
   `test_close_position_grammar_handler`) — they test code that no longer
   exists. No other test file changes: the 14 files touching
   `_dispatch_live_action`/`_LIVE_ACTION_HANDLERS`/`_action_live_*` are
   exercising the REAL dispatch path, which is untouched by this change.
5. Document in CLAUDE.md (near the existing grammar/registry notes) that
   `_LIVE_ACTION_HANDLERS`/`_NOOP_ACTION_HANDLERS` in `actions.py` are the
   explicit, sole action-dispatch SSOT — the `GrammarRegistry`'s `actions`
   table is catalog metadata only (token + params_schema, for the Lab-chat
   agent-builder), never live-dispatched. This closes the "contradicts the
   registry's own design intent" concern from the original plan by
   correcting the documentation instead of the code.

## Files

- `backend/api/algo/actions.py` — delete 7 dead stub functions (lines
  605-647), keep the 4 real noop handlers (649 onward).
- `backend/api/algo/grammar.py` (or `agent_grammar.yaml`, wherever
  `action_type` tokens are seeded) — stop pointing `resolver` at the
  deleted functions; keep `params_schema`/`description`.
- `backend/tests/test_actions_coverage.py` — remove the 2 dead-stub tests.
- `CLAUDE.md` — one short doc note on the dispatch SSOT.
- `grammar_registry.py` and `routes/grammar.py` — NOT touched (see point 3
  above for why no change is needed there).

## Verification

- Full pytest suite green (no broker-call test files are touched, so this
  is a low-risk regression surface — the 14 files touching the real
  dispatch path should pass unchanged).
- `backend-test` agent confirms the 2 removed tests had no other
  dependents (grep for their names before deleting).
- `venv/bin/python -c "from backend.api.algo import actions, grammar_registry"`
  imports cleanly after deletion (catches any leftover reference to a
  deleted function name).
- After the resolver-seed change, run `REGISTRY.reload()` (or whatever
  exercises `seed_grammar_tokens()` in an existing test) and confirm no new
  WARNING log for a failed `_import_dotted` on an `action_type` token.
- svelte-check / vitest / Playwright: not applicable — backend-only change.

## Done when

The 7 dead `_log_invoke()`-only action stub functions are gone from
`actions.py`; nothing seeds a `resolver` path pointing at them anymore;
the 2 tests that covered only those dead stubs are removed; CLAUDE.md
documents `_LIVE_ACTION_HANDLERS`/`_NOOP_ACTION_HANDLERS` as the real
action-dispatch SSOT; full pytest suite green; `routes/grammar.py`'s
stats endpoint is unchanged and still returns a correct count.

---

# Background: original grammar-unification plan (Phases 1/2/4 — already shipped)

## Context

Across this session's discussion, the operator established a direction: agent
design is fundamentally "operators + operands," operands are named tokens
resolved by registered resolvers (Python or, eventually, SQL), and — the
major piece — order placement must be unified so the SAME grammar serves both
an agent's `place_order` action and an operator typing a command directly
(the existing CLI, `CommandLineTab.svelte`). The grammar should live in a
file, not as a Python literal.

Research (two background investigations) found the real starting point:

- **Agents already place orders** — `place_order` is a real action token in
  `backend/api/algo/grammar.py` (line 971) with its own hand-written
  `params_schema` (account, symbol, exchange, side, qty, order_type, price,
  trigger_price, product, variety, tag, template fields, TP/SL/wing
  overrides).
- **The order vocabulary is hand-duplicated in two unrelated places** — that
  Python dict in `grammar.py`, and `backend/config/grammars/orders.yaml`
  (already an external file, loaded by the frontend CLI via
  `frontend/src/lib/command/grammars/orders.js`). Neither references the
  other. A new order field or a lot-size rule added to one never reaches the
  other.
- **There's already a real, DB-backed token registry** —
  `backend/api/algo/grammar_registry.py`'s `GrammarRegistry` /
  `REGISTRY` singleton. `grammar.py`'s `SYSTEM_TOKENS` list is seeded into a
  `grammar_tokens` DB table (`seed_grammar_tokens()`) and reloaded at runtime
  (`REGISTRY.reload()`), dynamically importing each resolver by its dotted
  path string. **This already works correctly for metric and scope
  tokens** — uniform signatures (`(ctx, row)` and `(ctx)` respectively),
  genuinely add-a-row-no-engine-change extensible.
- **Action tokens are the one place the registry is bypassed** — the
  REGISTERED action resolver (`actions.py`'s thin shims) is NOT what actually
  runs live. `_dispatch_live_action()` (`actions.py:164`) has its OWN
  hardcoded if/elif on the `action_type` string, calling `_action_live_*`
  functions in `actions_live.py` with a DIFFERENT signature
  (`(agent, context, params)` vs the registered shim's `(ctx, params)`).
  Adding a new action today means editing this chain, not just registering a
  token — contradicting the registry's own design intent and the module's
  own docstring claim.
- **SQL-based resolvers aren't possible today, structurally** — `Context`
  (`agent_evaluator.py`) is a plain dataclass carrying only already-fetched
  in-memory data (DataFrames, row lists); no DB session is attached anywhere.
  Making resolvers SQL-capable would mean either breaking the "built once,
  read-only, no mutation" contract, or moving that computation to a different
  layer entirely. Out of scope for this plan — noted as a future option the
  token/resolver interface should not foreclose, not something to build now.
- **The agent grammar lives in Python source, not a file** — `SYSTEM_TOKENS`
  is a list literal in `grammar.py`. The CLI's `orders.yaml` is already
  externalized. This is the concrete gap matching "the grammar should be in
  a file."

This is real, live-order-placing machinery. The plan below is **phased** —
each phase ships, tests, and deploys independently — rather than one big-bang
rewrite, given the blast radius.

## Approach

**Phase 1 — Externalize the agent grammar into a file (no behavior change).**
Move `SYSTEM_TOKENS` (and `LOG_TAG_TOKENS`) out of `grammar.py`'s Python list
literal into a new YAML file (e.g. `backend/config/grammars/agent_grammar.yaml`),
loaded at import/seed time the same way `orders.yaml` is loaded by the
frontend. `seed_grammar_tokens()` reads from the YAML instead of the inline
list; resolver dotted-paths stay Python-side (the YAML holds the catalog
metadata — token name, kind, params_schema, description — not the resolver
bodies themselves, which stay as real Python functions). Zero behavior
change: this is pure data relocation, verified by the seed-sync tests already
covering `SYSTEM_TOKENS` passing unchanged against the new source.

**Phase 2 — Unify the order-field vocabulary.** Make `orders.yaml` the single
source of truth for "what fields describe an order" (account, symbol,
exchange, side, qty, order_type, price, trigger_price, product, variety,
tag, chase level, template fields). `grammar.py`'s `place_order` action's
`params_schema` is generated FROM (or validated against, at seed time)
`orders.yaml`'s token definitions, instead of hand-duplicated. The frontend
CLI continues consuming `orders.yaml` exactly as it does today — this phase
changes where the BACKEND's schema comes from, not the frontend's behavior.
This is also the concrete answer to "operators should be able to place
orders directly also using the grammar for agents": both paths (an agent's
`place_order` action, and an operator's typed `buy`/`sell` command) now read
field definitions from the same file.

**Phase 3 — Make action dispatch genuinely registry-driven.** Unify the
signature `_action_live_*` functions actually use with the signature the
REGISTERED resolver declares, so `_dispatch_live_action()` can call
`REGISTRY.action(token)` directly instead of maintaining its own parallel
if/elif chain. This is the real refactor — every `_action_live_*` function's
call site changes, not just its registration. Mirrors `_al_run_noop_handler`'s
sibling dict too, which has the same problem. Do this LAST, after phases 1-2
have already proven the file-based/unified-schema approach works, since this
phase touches the most sensitive code (every live action handler).

## Files

- **Phase 1**: `backend/api/algo/grammar.py` (remove inline literal, add
  YAML loader), new `backend/config/grammars/agent_grammar.yaml`,
  `backend/api/algo/grammar_registry.py` (confirm `seed_grammar_tokens()`
  wiring needs no change beyond the source), tests for the YAML loader +
  regression tests proving every existing token still resolves identically.
- **Phase 2**: `backend/config/grammars/orders.yaml` (extended if any
  `place_order`-only field is missing from it today — check first),
  `grammar.py`'s `place_order` schema (generated/validated, not hand-typed),
  tests proving the generated schema matches today's hand-written one
  field-for-field (a snapshot/parity test is the safest regression guard
  here — diff the old static dict against the new generated one).
- **Phase 3**: `backend/api/algo/actions.py` (`_dispatch_live_action`,
  `_al_run_noop_handler`), `backend/api/algo/actions_live.py` (every
  `_action_live_*` function's signature), tests — this phase needs the
  heaviest test coverage since it changes a call signature across every
  action handler; full regression on every existing action-firing test.

## Verification

- Each phase: full pytest suite green, coverage gates pass, CC gate clean,
  before moving to the next phase.
- Phase 1: a test asserting the YAML-loaded token catalog is byte-for-byte
  equivalent (same tokens, same resolvers, same params_schema) to today's
  `SYSTEM_TOKENS` literal, so this phase is provably behavior-preserving.
- Phase 2: a parity test diffing the generated `place_order` schema against
  today's hand-written one; a live/paper-mode test placing an order via the
  agent action path and confirming it's unaffected.
- Phase 3: full regression across every existing action type (`place_order`,
  `modify_order`, `cancel_order`, `cancel_all_orders`, `chase_close_positions`,
  `expiry_auto_close`, `emit_log`, etc.) — each must fire identically through
  the new registry-driven path.

**Phase 4 — Frontend `orders.yaml` single-sourcing.** Phase 2 made
`order_fields.yaml` the single source of truth for the overlapping concepts
on the PYTHON side only (via `$ref` resolution in `grammar.py`); the
frontend (`frontend/src/lib/command/grammars/orders.js`) still parses its
own copy of `orders.yaml` with no reference to `order_fields.yaml` at all —
only drift-guard-tested, not actually unified. Phase 4 closes this: add an
`order_fields.yaml?raw` import to `orders.js`, parse it the same way
`orders.yaml?raw` already is, and merge its field metadata (enum values,
descriptions) into the wired token definitions for the overlapping roles
(`qty`, `orderType`, `price`, `chase`, `product` kwarg) at the SAME point
`_wireTokens`/`_wireKwargs` already build them — not a new merge layer, an
extension of the existing one. The CLI's own parsing/suggester LOGIC and
every existing behavior (what gets typed, what gets suggested, what payload
gets built) must not change — this only changes WHERE the shared metadata
values are read from, mirroring Phase 2's Python-side change exactly.
Verify with a Playwright spec (source-level, matching this session's
established pattern) proving the wired token output is unchanged, plus a
drift test proving the frontend and Python sides now actually read the SAME
file for these fields (not just two files kept in sync by hand/comments).

## Out of scope (explicitly, for this plan)

- SQL-based resolvers — structurally blocked by `Context`'s read-only,
  pre-fetched-data contract; a future, separate decision if ever needed.
- Formal BNF/CFG grammar — neither grammar needs recursion/precedence today;
  not building a parser-generator-style grammar without a concrete case that
  needs it.
- The Terminal tab elimination question — parked separately per operator.

## Queued separately (not a phase of this plan)

- **`place_order`'s `qty`/`quantity` schema-vs-runtime mismatch.** Phase 2's
  investigation found `place_order`'s params_schema documents the field as
  `qty`, but its actual executor (`_al_place_resolve_params` in
  `actions_live.py`) reads `params.get("quantity")` — a pre-existing bug,
  unrelated to grammar unification. Fix separately, as its own isolated
  change (one-issue-at-a-time): most likely correct the SCHEMA's documented
  name to `quantity` to match what the code actually reads (zero behavior
  risk — a documentation correction), rather than changing the runtime key,
  which would risk breaking any existing caller that already passes
  `quantity`. Confirm which callers exist before touching either side.
