# Plan: Unify the order grammar into the agent grammar (Phase 1 of 3)

## Task
Phase 1 of a 3-phase plan (full plan: /Users/ramanambore/.claude/plans/purrfect-marinating-pixel.md).
Externalize `backend/api/algo/grammar.py`'s `SYSTEM_TOKENS` (and `LOG_TAG_TOKENS`)
Python list literal into a new YAML file, matching the convention
`backend/config/grammars/orders.yaml` already established for the frontend
CLI grammar. Zero behavior change — this is pure data relocation. Phases 2
(unify order-field vocabulary between agent `place_order` and `orders.yaml`)
and 3 (make action dispatch genuinely registry-driven) come later, each as
their own plan/implementation cycle, not part of this task.

## Agents
- backend: Move `SYSTEM_TOKENS` and `LOG_TAG_TOKENS` (both in `backend/api/algo/grammar.py`) out of the Python list literal into a new YAML file `backend/config/grammars/agent_grammar.yaml`. The YAML holds catalog metadata only — `grammar_kind`, `token_kind`, `token`, `value_type`, `description`, `resolver` (the dotted Python path string, unchanged), `params_schema`, `enum`, etc. — NOT the resolver function bodies, which stay exactly where they are as real Python functions in `grammar.py`/`actions.py`/`actions_live.py`. Load the YAML at module-import time (or wherever `SYSTEM_TOKENS` is currently referenced — check `seed_grammar_tokens()` in `grammar.py` and `grammar_registry.py`'s `GrammarRegistry.reload()` for every consumer) and reconstruct the exact same Python list-of-dicts shape so every downstream consumer (`seed_grammar_tokens()`, `GrammarRegistry.reload()`, any test that imports `SYSTEM_TOKENS` directly) sees byte-for-byte identical data to today. Check for any entries whose dict values aren't YAML-serializable as-is (e.g. a lambda, a non-string enum member) and handle those specifically — flag any such case found rather than silently reshaping it. For every file you change, write or update a test covering the changed behavior: a regression test asserting the YAML-loaded token catalog is byte-for-byte equivalent (same set of tokens, same resolver dotted-paths, same params_schema, same descriptions) to today's literal — snapshot the OLD list's content before making the change (e.g. via `git show HEAD:backend/api/algo/grammar.py` or just capture it in the test as a frozen expected structure) and diff it against the new YAML-loaded version. Also run the EXISTING grammar/registry test suite (`backend/tests/` — grep for `grammar_registry`, `SYSTEM_TOKENS`, `seed_grammar_tokens` test files) and confirm every existing test still passes unchanged, proving this is genuinely behavior-preserving. Run the full backend suite and confirm 0 regressions.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
refactor(agents): externalize SYSTEM_TOKENS/LOG_TAG_TOKENS into agent_grammar.yaml (Phase 1 of grammar unification)

## Done when
`backend/api/algo/grammar.py` no longer contains the SYSTEM_TOKENS/LOG_TAG_TOKENS
Python list literals — both are loaded from `backend/config/grammars/agent_grammar.yaml`
at runtime, with every downstream consumer (seeding, registry reload, existing
tests) behaving identically to before. Full pytest suite green.
