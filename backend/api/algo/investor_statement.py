"""
Monthly investor-portal PDF statement.

Produces a letterheaded PDF for an (LP user, period) pair containing:
- Investor identity block (name, PAN if on file, period)
- NAV movement — opening / closing / Δ for the period
- LP's portfolio — contribution, share %, period P&L, cumulative P&L
- Daily NAV table — every NavDaily row in the period (firm NAV, LP
  slice, daily Δ%)
- Disclaimer + LLP regn footer

Math is stateless — recomputed per request from `nav_daily` + `users`.
No `monthly_statements` table yet; persistence + auto-email lands in
a follow-up slice. The current design lets the operator re-generate
any historical statement at any time without worrying about whether
"the right one" was saved.

Industry analog: SS&C/GP-Link periodic statements, CAMSonline CAS,
KFin / IIFL Wealth fund-of-fund statements. Same layout shape:
identity → period header → NAV movement → activity → disclosures.
"""

from __future__ import annotations

import calendar
import io
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import desc, select

from backend.api.database import async_session
from backend.api.models import NavDaily, User


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class _DailyRow:
    as_of:       date
    firm_nav:    float
    nav_share:   float
    day_pct:     Optional[float]   # vs prior row's nav_share


@dataclass
class StatementData:
    # Period
    year:        int
    month:       int
    period_start: date
    period_end:   date

    # Investor
    display_name: str
    username:     str
    pan:          Optional[str]
    contribution: float
    share_pct:    float

    # NAV movement (firm-level)
    opening_firm_nav:  float
    opening_as_of:     Optional[date]
    closing_firm_nav:  float
    closing_as_of:     Optional[date]
    firm_period_delta:     float
    firm_period_delta_pct: Optional[float]

    # LP slice
    opening_share:      float
    closing_share:      float
    share_period_delta: float
    share_period_pct:   Optional[float]
    cumulative_pnl:     float
    cumulative_pnl_pct: Optional[float]

    # Daily rows in the period (asc by date)
    daily: list[_DailyRow]

    # Metadata
    generated_at: datetime


# ---------------------------------------------------------------------------
# Math
# ---------------------------------------------------------------------------

def _resolve_opening_nav(opening, period_rows) -> tuple[float, Optional[date]]:
    """Return (opening_firm_nav, opening_as_of) from the pre-period snapshot or first period row."""
    if opening is not None:
        return float(opening.nav or 0.0), opening.as_of_date
    if period_rows:
        return float(period_rows[0].nav or 0.0), period_rows[0].as_of_date
    return 0.0, None


def _resolve_closing_nav(period_rows) -> tuple[Optional[float], Optional[date]]:
    """Return (closing_firm_nav, closing_as_of) from the last period row, or None when empty."""
    if not period_rows:
        return None, None
    return float(period_rows[-1].nav or 0.0), period_rows[-1].as_of_date


def _build_lp_share_math(user_events, all_events, closing_as_of, opening_firm_nav,
                          opening_as_of, closing_firm_nav, slice_value, _cb, user):
    """Compute LP cost basis, opening/closing slice values, deltas, and cumulative P&L."""
    contribution = _cb(user_events, as_of=closing_as_of)
    opening_share, _ = slice_value(user_events, all_events, opening_firm_nav, as_of=opening_as_of)
    closing_share, _ = slice_value(user_events, all_events, closing_firm_nav, as_of=closing_as_of)
    share_delta = closing_share - opening_share
    share_delta_pct = (share_delta / opening_share) if opening_share else None
    cumulative_pnl = closing_share - contribution
    cumulative_pnl_pct = (cumulative_pnl / contribution) if contribution > 0 else None
    return contribution, opening_share, closing_share, share_delta, share_delta_pct, cumulative_pnl, cumulative_pnl_pct


def _build_daily_rows(period_rows, user_events, all_events, opening_share,
                      opening, slice_value) -> list[_DailyRow]:
    """Build the per-day NAV table for the statement."""
    daily: list[_DailyRow] = []
    prev_share: Optional[float] = opening_share if opening is not None else None
    for r in period_rows:
        firm = float(r.nav or 0.0)
        slice_v, _ = slice_value(user_events, all_events, firm, as_of=r.as_of_date)
        day_pct: Optional[float] = None
        if prev_share is not None and prev_share != 0:
            day_pct = (slice_v - prev_share) / prev_share
        daily.append(_DailyRow(as_of=r.as_of_date, firm_nav=firm, nav_share=slice_v, day_pct=day_pct))
        prev_share = slice_v
    return daily


async def compute_statement(user_id: int, year: int, month: int) -> Optional[StatementData]:
    """Build a StatementData for (user, period). Returns None when
    the user doesn't exist or no NavDaily rows fall in the period
    (i.e. the period predates the fund's first snapshot).

    Uses the units-based fund-accounting model: each LP's slice =
    units_held × nav_per_unit. Capital movements in the period
    (subscriptions / redemptions) show up as step changes in the
    daily slice; the closing P&L is closing_slice − cost_basis
    (Σ subscriptions − Σ redemptions across the LP's history)."""
    from backend.api.algo.investor_units import (
        cost_basis as _cb,
        ensure_all_bootstrapped, fetch_all_events, slice_value,
    )
    if month < 1 or month > 12:
        return None
    period_start = date(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    period_end = date(year, month, last_day)

    async with async_session() as s:
        user = (await s.execute(
            select(User).where(User.id == user_id)
        )).scalar_one_or_none()
        if user is None:
            return None

        await ensure_all_bootstrapped(s)
        all_events = await fetch_all_events(s)
        user_events = [e for e in all_events if e.user_id == user.id]

        opening = (await s.execute(
            select(NavDaily)
              .where(NavDaily.as_of_date < period_start)
              .order_by(desc(NavDaily.as_of_date))
              .limit(1)
        )).scalar_one_or_none()

        period_rows = (await s.execute(
            select(NavDaily)
              .where(NavDaily.as_of_date >= period_start,
                     NavDaily.as_of_date <= period_end)
              .order_by(NavDaily.as_of_date.asc())
        )).scalars().all()

        if not period_rows and opening is None:
            return None

    opening_firm_nav, opening_as_of = _resolve_opening_nav(opening, period_rows)
    closing_firm_nav, closing_as_of = _resolve_closing_nav(period_rows)
    if closing_firm_nav is None:
        return None

    firm_delta     = closing_firm_nav - opening_firm_nav
    firm_delta_pct = (firm_delta / opening_firm_nav) if opening_firm_nav else None
    share_pct      = float(user.share_pct or 0.0)

    (contribution, opening_share, closing_share,
     share_delta, share_delta_pct,
     cumulative_pnl, cumulative_pnl_pct) = _build_lp_share_math(
        user_events, all_events, closing_as_of,
        opening_firm_nav, opening_as_of, closing_firm_nav,
        slice_value, _cb, user,
    )

    daily = _build_daily_rows(period_rows, user_events, all_events, opening_share, opening, slice_value)

    return StatementData(
        year=year, month=month,
        period_start=period_start, period_end=period_end,
        display_name=user.display_name or user.username,
        username=user.username,
        pan=user.pan,
        contribution=contribution,
        share_pct=share_pct,
        opening_firm_nav=opening_firm_nav,
        opening_as_of=opening_as_of,
        closing_firm_nav=closing_firm_nav,
        closing_as_of=closing_as_of,
        firm_period_delta=firm_delta,
        firm_period_delta_pct=firm_delta_pct,
        opening_share=opening_share,
        closing_share=closing_share,
        share_period_delta=share_delta,
        share_period_pct=share_delta_pct,
        cumulative_pnl=cumulative_pnl,
        cumulative_pnl_pct=cumulative_pnl_pct,
        daily=daily,
        generated_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

_MONTH_NAMES = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]


def _fmt_inr(v: float) -> str:
    """en-IN grouping with optional L / Cr suffix for headline values.
    Keeps absolute values readable on a single line (Rs.50 L vs
    Rs.50,00,000).

    Uses "Rs." instead of the ₹ glyph because fpdf2's bundled
    Helvetica is Latin-1 only. Bundling a Unicode font (DejaVu, ~200
    KB) is a future-slice trade — Indian LPs are accustomed to
    "Rs." in formal statements (CAMSonline, KFin etc. all use it
    interchangeably with the glyph).
    """
    if v is None:
        return "-"
    abs_v = abs(v)
    if abs_v >= 1e7:
        return f"Rs.{v / 1e7:,.2f} Cr"
    if abs_v >= 1e5:
        return f"Rs.{v / 1e5:,.2f} L"
    sign = "-" if v < 0 else ""
    return f"{sign}Rs.{abs(int(round(v))):,}"


def _fmt_inr_full(v: float) -> str:
    """Full rupee form — for table cells where suffix would compress
    away precision."""
    if v is None:
        return "-"
    sign = "-" if v < 0 else ""
    return f"{sign}Rs.{abs(int(round(v))):,}"


def _fmt_pct(v: Optional[float], signed: bool = True) -> str:
    if v is None:
        return "-"
    sign = "+" if (signed and v >= 0) else ""
    return f"{sign}{(v * 100):.2f}%"


def _fmt_date(d: Optional[date]) -> str:
    if d is None:
        return "-"
    return d.strftime("%d %b %Y")


# ---------------------------------------------------------------------------
# PDF renderer
# ---------------------------------------------------------------------------

def render_statement_pdf(data: StatementData) -> bytes:
    """Render the statement to a PDF byte string. Pure synchronous —
    callers offload to a thread when called from an async route."""
    from fpdf import FPDF

    # See _fmt_inr docstring: we use "Rs." instead of ₹ because the
    # bundled Helvetica is Latin-1 only. Same compromise CAMSonline
    # / KFin make in their plaintext statement variants.
    pdf = FPDF(orientation="P", unit="mm", format="A4")
    # Manual pagination — the table loop checks `get_y() > 268` and
    # adds a page itself. Leaving auto-break on caused the final
    # footer's `set_y(-15)` call to trigger an extra blank page.
    pdf.set_auto_page_break(auto=False)
    pdf.add_page()

    # ── Letterhead ────────────────────────────────────────────────────
    pdf.set_fill_color(212, 146, 12)     # champagne
    pdf.rect(0, 0, 210, 18, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_xy(12, 4)
    pdf.cell(0, 6, "RAMBOQUANT ANALYTICS LLP", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    pdf.set_xy(12, 10)
    pdf.cell(0, 5, "Statement of Account", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(40, 36, 24)
    pdf.set_y(24)

    # ── Identity block ────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 7, "Investor details", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)

    _label_value(pdf, "Investor",   data.display_name)
    if data.pan:
        _label_value(pdf, "PAN",     data.pan)
    _label_value(pdf, "Username",   data.username)
    _label_value(pdf, "Period",     f"{_MONTH_NAMES[data.month]} {data.year}")
    _label_value(pdf, "Generated",
                 data.generated_at.strftime("%d %b %Y, %H:%M UTC"))

    pdf.ln(3)

    # ── Fund NAV movement ─────────────────────────────────────────────
    _section(pdf, "Fund NAV movement")
    _label_value(pdf, "Opening NAV", f"{_fmt_inr(data.opening_firm_nav)}   ({_fmt_date(data.opening_as_of)})")
    _label_value(pdf, "Closing NAV", f"{_fmt_inr(data.closing_firm_nav)}   ({_fmt_date(data.closing_as_of)})")
    _label_value(pdf, "Period change",
                 f"{_fmt_inr_full(data.firm_period_delta)}   ({_fmt_pct(data.firm_period_delta_pct)})",
                 colour=_pnl_colour(data.firm_period_delta))

    pdf.ln(3)

    # ── LP slice ──────────────────────────────────────────────────────
    _section(pdf, "Your portfolio")
    _label_value(pdf, "Contribution",    _fmt_inr(data.contribution))
    _label_value(pdf, "Share %",         f"{data.share_pct:.2f}%")
    _label_value(pdf, "Opening slice",   _fmt_inr(data.opening_share))
    _label_value(pdf, "Closing slice",   _fmt_inr(data.closing_share))
    _label_value(pdf, "Period P&L",
                 f"{_fmt_inr_full(data.share_period_delta)}   ({_fmt_pct(data.share_period_pct)})",
                 colour=_pnl_colour(data.share_period_delta))
    _label_value(pdf, "Since-inception P&L",
                 f"{_fmt_inr_full(data.cumulative_pnl)}   ({_fmt_pct(data.cumulative_pnl_pct)})",
                 colour=_pnl_colour(data.cumulative_pnl))

    pdf.ln(3)

    # ── Daily NAV table ───────────────────────────────────────────────
    _section(pdf, "Daily NAV (your slice)")
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(231, 224, 207)
    pdf.set_text_color(40, 36, 24)
    pdf.cell(34, 6, "Date",       border=0, fill=True)
    pdf.cell(40, 6, "Firm NAV",   border=0, align="R", fill=True)
    pdf.cell(40, 6, "Your slice", border=0, align="R", fill=True)
    pdf.cell(28, 6, "Day Change %", border=0, align="R", fill=True,
             new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 9)
    for row in data.daily:
        # New page if footer would land in the bottom 18mm.
        if pdf.get_y() > 268:
            _footer(pdf)
            pdf.add_page()
            pdf.set_text_color(40, 36, 24)
        pdf.set_text_color(40, 36, 24)
        pdf.cell(34, 5, _fmt_date(row.as_of),                  border=0)
        pdf.cell(40, 5, _fmt_inr_full(row.firm_nav),           border=0, align="R")
        pdf.cell(40, 5, _fmt_inr_full(row.nav_share),          border=0, align="R")
        r, g, b = _pnl_colour(row.day_pct or 0.0)
        pdf.set_text_color(r, g, b)
        pdf.cell(28, 5, _fmt_pct(row.day_pct), border=0, align="R",
                 new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(40, 36, 24)

    pdf.ln(4)

    # ── Disclaimer + footer ───────────────────────────────────────────
    _section(pdf, "Notes")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(80, 70, 50)
    pdf.multi_cell(0, 4,
        "This statement is for your information only. Values are "
        "unaudited and reflect end-of-day positions on the dates "
        "shown. Day Change % is computed against the prior available "
        "snapshot (may skip weekends and exchange holidays). Past "
        "performance does not guarantee future results. For any "
        "questions, contact RamboQuant at rambo@ramboq.com."
    )

    _footer(pdf)

    # Return as bytes — fpdf2 returns str (PDF/latin-1) or bytes
    # depending on version. Normalise to bytes for the route layer.
    out = pdf.output(dest="S")
    if isinstance(out, str):
        out = out.encode("latin-1")
    return bytes(out)


# ---------------------------------------------------------------------------
# Layout primitives
# ---------------------------------------------------------------------------

def _section(pdf, title: str) -> None:
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(40, 36, 24)
    pdf.cell(0, 7, title, new_x="LMARGIN", new_y="NEXT")
    # Champagne underline
    y = pdf.get_y()
    pdf.set_draw_color(212, 146, 12)
    pdf.set_line_width(0.4)
    pdf.line(12, y, 198, y)
    pdf.ln(1.5)


def _label_value(pdf, label: str, value: str,
                 colour: Optional[tuple[int, int, int]] = None) -> None:
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(110, 95, 60)
    pdf.cell(56, 5.5, label, border=0)
    if colour:
        r, g, b = colour
        pdf.set_text_color(r, g, b)
    else:
        pdf.set_text_color(40, 36, 24)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 5.5, value, border=0, new_x="LMARGIN", new_y="NEXT")


def _pnl_colour(v: Optional[float]) -> tuple[int, int, int]:
    if v is None or v == 0:
        return (40, 36, 24)
    if v > 0:
        return (20, 101, 58)         # green-700
    return (150, 45, 45)             # red-700


def _footer(pdf) -> None:
    pdf.set_y(-15)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(120, 110, 80)
    pdf.cell(0, 4,
             "RamboQuant Analytics LLP  |  ramboq.com  |  Generated by the investor portal",
             align="C", new_x="LMARGIN", new_y="NEXT")
