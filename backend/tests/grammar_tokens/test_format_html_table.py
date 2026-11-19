"""Tests for grammar_tokens/format_html_table.py — `html_table` notify
format."""

import re

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


def test_long_cell_value_wraps_instead_of_nowrap():
    # Some mail-client sanitizers force table-layout:fixed and strip
    # overflow:hidden, which locks column widths regardless of content —
    # a nowrap cell then spills its text past its own background box.
    # word-break/overflow-wrap is the fix that holds under a forced
    # fixed layout, since the cell box cannot grow to fit the content.
    long_value = "A" * 150
    html = _html_table(("Account", "Detail"), [("Z1234", long_value)])
    # Every <td style='...'> block in this render must not contain
    # nowrap, and must contain word-break.
    td_styles = re.findall(r"<td style='([^']*)'>", html)
    assert td_styles, "expected at least one <td> cell"
    for style in td_styles:
        assert "nowrap" not in style
        assert "word-break" in style
        assert "overflow-wrap" in style
    assert long_value in html


def test_long_header_label_wraps_instead_of_nowrap():
    # format_html_table.py serves both the short-header order-rejection
    # table AND the market-summary tables, whose headers (e.g. "Avail
    # Margin") are long relative to a narrow column budget — unlike
    # alert_utils.py's _email_alert_body, which deliberately leaves <th>
    # as nowrap because its own headers are always short/fixed strings,
    # this formatter's <th> must also wrap.
    long_header = "Avail Margin After Settlement And Reconciliation"
    html = _html_table((long_header, "P&L"), [("Z1234", "100")])
    th_styles = re.findall(r"<th style='([^']*)'>", html)
    assert th_styles, "expected at least one <th> cell"
    for style in th_styles:
        assert "nowrap" not in style
        assert "word-break" in style
        assert "overflow-wrap" in style
    assert long_header in html


def test_total_row_style_also_wraps_instead_of_nowrap():
    # td_total_style is its own independent literal (not derived from
    # td_style via substitution like td_alt_style is), so it needs its
    # own explicit wrap fix — confirm it got it.
    html = _html_table(("Account", "P&L"), [("Z1234", "100"), ("TOTAL", "100")])
    total_row_html = html.split("<tbody>", 1)[1]
    td_styles = re.findall(r"<td style='([^']*)'>", total_row_html)
    assert td_styles, "expected TOTAL row <td> cells"
    for style in td_styles:
        assert "nowrap" not in style
        assert "word-break" in style
