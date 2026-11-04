"""Standing rule, 2026-11-04 (operator: "we need to have a standard
pattern for help info for new tokens/functions and parameters while
registering them" / "this should be enforced") — every `SYSTEM_TOKENS`
entry (`agent_grammar.yaml`) and every one of its `params_schema` fields
must carry a `description`.

This is the FAST pytest-gate half of the enforcement — it calls the SAME
`validate_token_descriptions_present()` that also runs at real registry
reload time (`GrammarRegistry.reload()`, see `grammar_registry.py`), so
a missing description is caught both in CI (`/ddev`'s pytest gate) and
at actual app startup / the admin `/grammar/reload` endpoint — not just
one or the other.

Why this exists: the Console's click-to-describe panel (`describeTokenAt`
in `frontend/src/lib/command/grammars/agents.js`) and the `help(<name>)`
DSL action's catalog-description fallback both read this field directly —
there is no fallback to Python reflection for per-parameter help text
(most action resolvers take a generic `(ctx, params: dict)` signature, so
`inspect.signature()` can never recover an individual field's name or
meaning — see the audit that led to this rule, 2026-11-04). A missing
`description` is a genuine, permanent UX gap for whoever clicks that
token/param in the editor, not a cosmetic nit.

How to use this test:
  - Adding a NEW token or a NEW params_schema field? Give it a real
    `description` in `agent_grammar.yaml` — this fails immediately if you
    forget, naming exactly what's missing.
  - Fixing one of the pre-existing `KNOWN_MISSING_PARAM_DESCRIPTIONS`
    entries (defined once, in `grammar_registry.py`, imported here — see
    that module for why it isn't duplicated)? Add the `description` in
    `agent_grammar.yaml`, then remove that entry from the allowlist there
    — this test fails loudly if you fix one and forget to shrink the
    list, so the allowlist can only ever shrink, never silently stay
    stale.
  - Do NOT add a new entry to that allowlist for a token/param you are
    yourself registering — it exists solely to grandfather the 42
    pre-2026-11-04 gaps the original audit found, not as an escape hatch
    for new work.
"""
import pytest

from backend.api.algo.grammar_registry import (
    GrammarDescriptionDriftError,
    validate_token_descriptions_present,
)


def test_no_grammar_description_drift():
    """One call, same function the real registry reload path uses —
    raises GrammarDescriptionDriftError on any new missing description,
    any token with no description at all, or a stale allowlist entry.
    See validate_token_descriptions_present()'s own docstring for the
    three specific conditions it checks."""
    try:
        validate_token_descriptions_present()
    except GrammarDescriptionDriftError as e:
        pytest.fail(str(e))
