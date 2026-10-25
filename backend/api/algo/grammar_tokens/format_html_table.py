"""Notify format resolver — `html_table`: structured HTML table with
per-kind row colour — for email.

Color constants (`_EMAIL_BG` etc.) are genuinely SHARED across several
email-styling functions that stay in `backend/shared/helpers/
alert_utils.py` (`_branch_banner_html`, `_sim_banner_html`, …) — not
exclusive to this one formatter — so they are imported from there
rather than duplicated or relocated. This is a one-way, module-load-time
dependency (this file -> alert_utils); `alert_utils.py`'s own two
callers of `_html_table` import it back from here LAZILY, inside their
own function bodies, so there is no module-load-time cycle.
"""

from __future__ import annotations

from backend.shared.helpers.alert_utils import (
    _EMAIL_BG,
    _EMAIL_PANEL_BG,
    _EMAIL_PANEL_ALT,
    _EMAIL_BORDER,
    _EMAIL_AMBER,
    _EMAIL_AMBER_SOFT,
    _EMAIL_TEXT,
)


def _html_table(headers, rows):
    """Render a list of string-tuple rows as an HTML table for email,
    styled in the algo dark palette."""
    th_style = (
        f"background-color:{_EMAIL_BG};color:{_EMAIL_AMBER};padding:8px 12px;"
        f"text-align:left;font-family:monospace;font-size:13px;"
        f"font-weight:700;letter-spacing:0.04em;"
        f"border-bottom:2px solid {_EMAIL_AMBER};white-space:nowrap"
    )
    td_style = (
        f"padding:6px 12px;font-family:monospace;font-size:13px;"
        f"background-color:{_EMAIL_PANEL_BG};color:{_EMAIL_TEXT};"
        f"border-bottom:1px solid {_EMAIL_BORDER};white-space:nowrap"
    )
    td_alt_style = td_style.replace(_EMAIL_PANEL_BG, _EMAIL_PANEL_ALT)
    # TOTAL row — stronger amber tint so the rollup stands out from the
    # data rows above it. Matches the on-screen TOTAL row stratum.
    td_total_style = (
        f"padding:8px 12px;font-family:monospace;font-size:13px;"
        f"background-color:#3a2c10;color:{_EMAIL_AMBER};font-weight:700;"
        f"border-top:2px solid {_EMAIL_AMBER};"
        f"border-bottom:1px solid {_EMAIL_AMBER_SOFT};white-space:nowrap"
    )

    def _is_total_row(row):
        # First cell is account / label; flag the rollup row by name.
        return bool(row) and str(row[0]).strip().upper() in ("TOTAL", "TOTALS", "GRAND TOTAL")

    header_cells = "".join(f"<th style='{th_style}'>{h}</th>" for h in headers)
    row_html = ""
    for i, row in enumerate(rows):
        if _is_total_row(row):
            bg = td_total_style
        else:
            bg = td_alt_style if i % 2 else td_style
        cells = "".join(f"<td style='{bg}'>{v}</td>" for v in row)
        row_html += f"<tr>{cells}</tr>"

    return (
        f"<table style='border-collapse:collapse;width:100%;"
        f"background-color:{_EMAIL_PANEL_BG};border:1px solid {_EMAIL_BORDER};"
        f"border-radius:4px;overflow:hidden'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{row_html}</tbody>"
        f"</table>"
    )
