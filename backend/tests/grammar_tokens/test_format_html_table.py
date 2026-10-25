"""Tests for grammar_tokens/format_html_table.py — `html_table` notify
format."""

from backend.api.algo.grammar_tokens.format_html_table import _html_table


def test_renders_header_and_data_rows():
    html = _html_table(("Account", "P&L"), [("Z1234", "100"), ("Z5678", "-50")])
    assert "<table" in html
    assert "<th style=" in html
    assert "Account" in html
    assert "Z1234" in html
    assert "-50" in html


def test_total_row_gets_the_total_styling_tier():
    html_with_total = _html_table(("Account", "P&L"), [("Z1234", "100"), ("TOTAL", "100")])
    html_without_total = _html_table(("Account", "P&L"), [("Z1234", "100"), ("Z5678", "100")])
    # The TOTAL row's cells use the dedicated amber-total background
    # (#3a2c10, one per cell on that row); a table with no TOTAL row
    # never uses it at all.
    assert "#3a2c10" in html_with_total
    assert "#3a2c10" not in html_without_total


def test_empty_rows_still_renders_a_valid_table_shell():
    html = _html_table(("Account", "P&L"), [])
    assert "<table" in html
    assert "<tbody></tbody>" in html
