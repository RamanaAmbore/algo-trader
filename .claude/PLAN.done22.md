# Plan: Agent CLI grammar — Sprint 7, meta tags (#name/#desc/#group/#tag)

## Task

Add a trailing `#`-sigil meta clause to `agent_stmt` only (Sprint 8 will
extend the SAME mechanism to `bracket_stmt` once that statement kind
exists — write the meta-parsing/compiling logic as a REUSABLE helper, not
hardcoded into agent_stmt's own function, so Sprint 8 can call it without
duplicating logic).

```
meta_item  := "#" NAME "=" meta_value ("," meta_value)*
meta_value := STRING | BARE_WORD   (* BARE_WORD = unquoted run of chars with
                                       no space/comma/#/=/quote char — quotes
                                       only required once a special char
                                       appears in the value. This needs its
                                       OWN character class, NOT a reuse of
                                       the existing NAME-token scanner —
                                       confirmed gap from an audit pass:
                                       `_isWordChar` is `/[A-Za-z0-9_]/` (no
                                       hyphen), but real examples like
                                       `#group=hedge-book` need hyphens to
                                       lex. Define BARE_WORD's allowed
                                       character set to match the stated
                                       rule precisely (anything except
                                       whitespace/comma/#/=/quote), not
                                       NAME's narrower set. *)
```

Position: **trailing only** (after the last real clause, i.e. after
WHEN/ALERT/DO's `tail`) — not interspersed anywhere in the statement. This
needs no pre-pass; `meta_item`s are just more tokens parsed after
`parseAgentStmt`'s existing `tail` loop finishes.

Example:
```
WHEN pnl@positions.total <= -50000 ALERT telegram DO order(account="ZG0790", symbol="NIFTY25JULFUT", side="SELL", lots=1) #name="NIFTY loss guard" #desc="Closes on -50k drawdown" #tag=risk,nifty #group=hedge-book
```

**Scope — valid on `agent_stmt` only this sprint** (never on a bare
`order_stmt` — that's a compile-time error: `"meta tags only apply to
agents/brackets, not a bare order"`; `bracket_stmt` doesn't exist yet,
Sprint 8 wires it in later using the same helper this sprint builds). Does
NOT touch the order's own existing, unrelated single `tag` field
(`order_fields.yaml` — free-form, already rendered on `OrderCard.svelte`
as a status chip) — untouched, different field, different purpose.

**The four meta keys**:
- `#name=` → `name`.
- `#desc=` → `description`.
- `#tag=` → `tags` (comma-separated → list; maps to the existing
  `Agent.tags` field).
- `#group=` → a **NEW** field, separate from `Agent.topic` — do NOT alias
  or reuse `topic` (it has a real, different, execution-affecting meaning:
  "agents sharing a topic get cross-suppressed by tier"). `group` is
  nullable/optional, holds ONLY custom operator-specified values (never a
  stored type-tag like `"agent"`/`"bracket"` — table identity alone tells
  the browse UI what kind a row is, added in a later sprint).

Compiles into `result.agent.name`/`.description`/`.tags`/`.group`
alongside the existing `.conditions`/`.events`/`.actions`. Decompiler
inverse: render the `#...` clause when any of the four is non-default,
omit entirely when all are unset — an agent created before this sprint
existed (has a real `name` from the Structured tab, but no meta clause in
its `cli_source`) should still decompile with `#name=...` rendered, since
the point is a complete, re-typeable statement, not a lossy one.

## Agents

- backend: Add a nullable `group` column to BOTH `agents` and
  `order_templates` tables (idempotent `ALTER TABLE ... ADD COLUMN IF NOT
  EXISTS group_name TEXT` — name it `group_name` not `group` at the SQL/
  column level since `group` is a reserved word in some SQL dialects/ORM
  contexts; confirm SQLAlchemy's own handling before deciding, but
  `group_name` is the safe default). Follow the EXACT idempotent-migration
  pattern already used for `Agent.cli_source`
  (`backend/api/database.py:_migrate_agents_cli_source` — read it, copy
  its shape: `SET LOCAL lock_timeout`, wired into `init_db()`'s
  `engine.begin()` block). Also add the equivalent migration for
  `order_templates` (new function, same pattern). Check whether
  `OrderTemplate` already has a `tags`-equivalent column (read
  `backend/api/models.py` around line 1517 fully) — if not, add a nullable
  JSON/array `tags` column there too (same idempotent migration style).
  Add the new `group`/`tags` fields to `backend/api/models.py`'s `Agent`
  and `OrderTemplate` ORM classes, and to `AgentCreateRequest`/
  `AgentUpdateRequest` in `backend/api/routes/agents.py` (nullable,
  `None` = unchanged on update, same convention as `cli_source`). Do NOT
  touch `OrderTemplateController`/its request schemas yet — Sprint 8
  wires bracket creation through the API, not this sprint; just get the
  DB column + model field in place so Sprint 8 has something to write to.

  **Test coverage (mandatory)**: migration idempotency test (mirroring
  `test_agents_cli_source_migration.py`'s pattern) for both new
  `group_name` columns; a test confirming `AgentCreateRequest`/
  `AgentUpdateRequest` accept and round-trip the new `group`/`tags` fields
  with the same `None`-means-unchanged semantics `cli_source` already has.

  Run: `cd /Users/ramanambore/projects/ramboq && venv/bin/pytest backend/tests/ -q --tb=short -k "agent or migration"`
  then the full suite once: `venv/bin/pytest backend/tests/ -q --tb=line`

- frontend: In `frontend/src/lib/command/grammars/agents.js`:
  1. Lexer: add `#` as a new structural character (not currently handled
     anywhere in the char-dispatch chain — confirm this via reading
     `lexAgentStatement` before assuming where to add it). Add a `BARE_WORD`
     scanning rule for meta values — its own character class (anything
     except whitespace/`,`/`#`/`=`/quote chars), distinct from the
     existing NAME-token scanner (which uses the narrower `_isWordChar`).
  2. New reusable function `parseMetaClause(state)` — parses zero or more
     trailing `meta_item`s, returns `{name, description, tags, group}`
     (each `null`/absent if not present). Called from `parseAgentStmt`
     after its existing `tail`-parsing loop finishes. Write it generically
     enough that Sprint 8 can call the SAME function for `bracket_stmt`
     later without modification.
  3. New reusable `compileMetaClause(meta, errors)` — validates/normalizes
     the parsed meta dict into the JSON shape (`tags` as a real array from
     comma-split, `name`/`description`/`group` as plain strings). Called
     from `compileAgentStmt`, merged into its returned `result.agent`
     alongside `.conditions`/`.events`/`.actions`.
  4. Explicit compile-time rejection: if a bare `order_stmt` has ANY
     trailing `#...` tokens, error with `"meta tags only apply to
     agents/brackets, not a bare order"` (don't silently parse-and-drop).
  5. Decompiler: new `decompileMetaClause(agent)` inverse of (3) — renders
     the `#...` clause from an agent's real `name`/`description`/`tags`/
     `group` fields, omitting any key that's empty/unset, omitting the
     whole clause if all four are empty/unset.
  6. Update the EBNF header comment (top of the file) to add the
     `meta_item`/`meta_value`/`BARE_WORD` productions, matching what's in
     `.claude/plans/purrfect-marinating-pixel.md`'s Sprint 7 section.

  **Test coverage (mandatory)**: in `agentsGrammar.test.js` — each of the
  four keys compiles correctly individually and combined; `#tag=` with
  multiple comma-separated values produces a real array; BARE_WORD values
  with hyphens (`#group=hedge-book`) lex and compile correctly (the
  specific gap found in the audit — test this explicitly, don't assume);
  quoted values with spaces still work; a bare `order(...)` with trailing
  `#...` produces the exact compile-error text above; meta clause combined
  with the already-shipped ALERT/DO reordering/omission forms still works.
  In `agentsDecompiler.test.js`: round-trip for an agent with all four
  meta fields set, round-trip for an agent with none set (no `#...`
  rendered at all), round-trip for an agent created before this sprint
  (has a real `name` but nothing else) — confirm it still renders
  `#name=...` correctly.

  Run after changes:
  ```
  cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20
  cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -20
  ```

## Tests
- pytest: yes (new migration + request-field tests)
- svelte-check: yes
- playwright: no (no new UI surface this sprint — meta clause is CLI-text
  only for now; Sprint 9's group-grids are what actually surface `group`
  visually, separate sprint)

## Commit message
feat(agents): Sprint 7 — meta tags (#name/#desc/#group/#tag) on agent_stmt

## Done when
- `#name=/#desc=/#group=/#tag=` all compile correctly, individually and
  combined, on an `agent_stmt`.
- A bare order with trailing `#...` is a clear compile error, not silently
  accepted.
- `group` is a new, separate field from `topic` on both `agents` and
  `order_templates` — migration idempotent, matches the `cli_source`
  pattern.
- `parseMetaClause`/`compileMetaClause`/`decompileMetaClause` are written
  generically enough for Sprint 8 to reuse for `bracket_stmt` without
  modification.
- Full backend + frontend test suites green, CC clean.
