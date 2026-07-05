"""Unit tests for the `interpret` command-router helpers extracted from
AgentController.interpret in backend/api/routes/agents.py.
"""
import pytest

from backend.api.routes.agents import _extract_ai_prompt


CREATE_PREFIX = r"^agent\s+ai\s+create\s+"
REFINE_PREFIX = r"^agent\s+ai\s+refine\s+\S+\s+"


class TestExtractCreate:
    def test_double_quoted_prompt(self):
        p = _extract_ai_prompt(
            'agent ai create "hedge NIFTY when SPX drops 2%"',
            CREATE_PREFIX,
        )
        assert p == "hedge NIFTY when SPX drops 2%"

    def test_single_quoted_prompt(self):
        p = _extract_ai_prompt(
            "agent ai create 'hedge NIFTY'",
            CREATE_PREFIX,
        )
        assert p == "hedge NIFTY"

    def test_unquoted_prompt(self):
        p = _extract_ai_prompt(
            "agent ai create hedge NIFTY when SPX drops 2 percent",
            CREATE_PREFIX,
        )
        assert p == "hedge NIFTY when SPX drops 2 percent"

    def test_empty_prompt_after_prefix(self):
        p = _extract_ai_prompt("agent ai create ", CREATE_PREFIX)
        assert p == ""

    def test_missing_prompt_after_prefix(self):
        p = _extract_ai_prompt("agent ai create", CREATE_PREFIX)
        assert p == ""

    def test_case_insensitive_prefix_match(self):
        p = _extract_ai_prompt('AGENT AI CREATE "foo"', CREATE_PREFIX)
        assert p == "foo"

    def test_mismatched_quotes_not_stripped(self):
        p = _extract_ai_prompt(
            "agent ai create \"unclosed'", CREATE_PREFIX,
        )
        assert p == "\"unclosed'"

    def test_single_char_prompt_kept_verbatim(self):
        """A 1-char prompt has no room for matched quotes — must not
        strip its solitary character (regression on len>=2 guard)."""
        p = _extract_ai_prompt("agent ai create x", CREATE_PREFIX)
        assert p == "x"

    def test_extra_whitespace_around_quotes_stripped(self):
        p = _extract_ai_prompt(
            'agent ai create   "  padded  "  ',
            CREATE_PREFIX,
        )
        assert p == "padded"

    def test_pattern_that_fails_to_match_yields_empty(self):
        """Malformed input where the prefix does not match should not
        raise — just return empty so the caller renders usage."""
        p = _extract_ai_prompt(
            "not the right prefix",
            CREATE_PREFIX,
        )
        assert p == ""


class TestExtractRefine:
    def test_refine_with_double_quotes(self):
        p = _extract_ai_prompt(
            'agent ai refine my-slug "add loss guard"',
            REFINE_PREFIX,
        )
        assert p == "add loss guard"

    def test_refine_with_single_quotes(self):
        p = _extract_ai_prompt(
            "agent ai refine my-slug 'add loss guard'",
            REFINE_PREFIX,
        )
        assert p == "add loss guard"

    def test_refine_unquoted(self):
        p = _extract_ai_prompt(
            "agent ai refine my-slug add loss guard",
            REFINE_PREFIX,
        )
        assert p == "add loss guard"

    def test_refine_missing_prompt(self):
        p = _extract_ai_prompt(
            "agent ai refine my-slug ",
            REFINE_PREFIX,
        )
        assert p == ""

    def test_refine_slug_captured_via_dot_star(self):
        """`\\S+` swallows any non-whitespace slug so slugs with dashes
        or dots work exactly like plain identifiers."""
        p = _extract_ai_prompt(
            'agent ai refine slug-with.dashes-and.dots "prompt body"',
            REFINE_PREFIX,
        )
        assert p == "prompt body"
