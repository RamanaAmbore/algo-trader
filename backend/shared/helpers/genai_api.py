import re
import socket as _socket

from google import genai
from google.genai import types

from backend.shared.helpers.date_time_utils import timestamp_indian, timestamp_est
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.settings import get_int
from backend.shared.helpers.utils import secrets, ramboq_config, is_enabled

logger = get_logger(__name__)

# Force IPv4 for Google API hosts. The server binds per-account IPv6 addresses
# for Kite routing; those addresses don't have public egress to Google, so the
# SDK's default IPv6 preference makes generate_content hang. Scoping the
# override to *googleapis.com keeps Kite's IPv6 binding untouched.
_orig_getaddrinfo = _socket.getaddrinfo

def _getaddrinfo_v4_for_google(host, port, family=0, type=0, proto=0, flags=0):
    if host and 'googleapis.com' in str(host):
        family = _socket.AF_INET
    return _orig_getaddrinfo(host, port, family, type, proto, flags)

_socket.getaddrinfo = _getaddrinfo_v4_for_google


def _extract_underlying(tradingsymbol):
    """Extract the underlying from a Kite tradingsymbol (e.g. NIFTY25APR22500CE → NIFTY)."""
    m = re.match(r'^([A-Z]+)', tradingsymbol or '')
    return m.group(1) if m else tradingsymbol


def _get_portfolio_details():
    """Get portfolio holdings + positions with P&L details for the market report prompt."""
    try:
        import pandas as pd
        from backend.brokers.broker_apis import fetch_holdings, fetch_positions

        lines = []
        underlyings = set()

        # Holdings
        for df in fetch_holdings():
            if df.empty:
                continue
            for _, row in df.iterrows():
                sym = row.get('tradingsymbol', '')
                if not sym:
                    continue
                qty = int(row.get('quantity', 0) or 0)
                if qty == 0:
                    continue
                ltp = float(row.get('close_price', 0) or 0)
                avg = float(row.get('average_price', 0) or 0)
                pnl = float(row.get('pnl', 0) or 0)
                day_chg = float(row.get('day_change_val', 0) or 0)
                day_pct = float(row.get('day_change_percentage', 0) or 0)
                lines.append(f"    • {sym} (holding) qty={qty} avg=₹{avg:.2f} ltp=₹{ltp:.2f} pnl=₹{pnl:,.0f} day_change=₹{day_chg:,.0f} ({day_pct:+.1f}%)")
                underlyings.add(sym)

        # Positions
        for df in fetch_positions():
            if df.empty:
                continue
            for _, row in df.iterrows():
                sym = row.get('tradingsymbol', '')
                if not sym:
                    continue
                qty = int(row.get('quantity', 0) or 0)
                if qty == 0:
                    continue
                ltp = float(row.get('close_price', 0) or row.get('last_price', 0) or 0)
                avg = float(row.get('average_price', 0) or 0)
                pnl = float(row.get('pnl', 0) or 0)
                direction = 'long' if qty > 0 else 'short'
                underlying = _extract_underlying(sym)
                lines.append(f"    • {sym} ({direction} position) qty={qty} avg=₹{avg:.2f} ltp=₹{ltp:.2f} pnl=₹{pnl:,.0f}")
                underlyings.add(underlying)
                underlyings.add(sym)

        logger.info(f"Portfolio for market report: {len(lines)} instruments, {len(underlyings)} underlyings")
        return '\n'.join(lines) if lines else "    • (no current holdings/positions)", sorted(underlyings)
    except Exception as e:
        logger.warning(f"Failed to fetch portfolio details: {e}")
        return "    • (no current holdings/positions)", []


def get_market_update(strict: bool = False):
    """
    Return the AI-generated market report.

    Default (strict=False): on any failure (genai disabled, not prod-capable,
    empty response, exception) returns the YAML static fallback.
    strict=True: return None in those failure cases so callers can decide
    what to show rather than serving stale static content.
    """
    now = timestamp_indian()
    now_est = timestamp_est()
    formatted_ist = now.strftime('%a, %b %d, %Y, %I:%M %p IST')
    formatted_est = now_est.strftime('%a, %b %d, %Y, %I:%M %p %Z')
    logger.info(f'GenAI for market update invoked at {formatted_ist}')

    message = f"Market Report — {formatted_ist} | {formatted_est}"
    fallback = None if strict else ramboq_config['market'].replace("Market Report", message)

    if not is_enabled('genai'):
        logger.info("GenAI skipped — disabled for this environment")
        return fallback

    try:
        client = genai.Client(api_key=secrets["gemini_api_key"])

        # Dynamically inject portfolio with P&L details from holdings + positions
        portfolio_text, underlyings = _get_portfolio_details()
        user_msg = ramboq_config['genai_user_msg'].replace('{{PORTFOLIO_SYMBOLS}}', portfolio_text)
        # Add underlyings list for the model to focus on
        underlying_list = ', '.join(underlyings[:50]) if underlyings else 'none'
        user_msg += f"\n\nKey underlyings to cover (include any major price movements, news, or corporate actions): {underlying_list}"

        prompt = (
            f"Current date and time: {formatted_ist} IST | {formatted_est} EST\n\n"
            f"{user_msg}"
        )

        # gemini-2.5-flash consumes part of max_output_tokens on internal "thinking"
        # before emitting the answer. Cap thinking to a small budget so the full
        # response fits within max_output_tokens — otherwise the market report
        # gets truncated mid-sentence.
        response = client.models.generate_content(
            model=ramboq_config.get('genai_model', 'gemini-2.5-flash'),
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=ramboq_config['genai_system_msg'],
                tools=[types.Tool(google_search=types.GoogleSearch())],
                temperature=float(ramboq_config['genai_temperature']),
                max_output_tokens=int(ramboq_config['genai_max_tokens']),
                thinking_config=types.ThinkingConfig(
                    thinking_budget=get_int(
                        'genai.thinking_budget',
                        int(ramboq_config.get('genai_thinking_budget', 512))),
                ),
            ),
        )

        resp = response.text
        if not resp:
            logger.warning("Gemini returned empty response — using fallback")
            return fallback

        # Strip markdown emphasis markers so the raw report renders cleanly.
        resp = re.sub(r'\*\*([^*]+)\*\*', r'\1', resp)           # **bold** → bold
        resp = resp.replace('**', '')                             # drop stray **
        resp = re.sub(r'\*(\S(?:[^*\n]*?\S)?)\*', r'\1', resp)   # *text* → text
        resp = re.sub(r'(?<!\w)_(\S(?:[^_\n]*?\S)?)_(?!\w)', r'\1', resp)  # _text_ → text
        # Normalize all list markers: leading spaces + * or - or • + any ws → "* "
        resp = re.sub(r'(?m)^[ \t]*[-*•][ \t]+', '* ', resp)
        # But region/category headings (India:, Global:, Asia:, etc.) should
        # render as plain headings, not bullets.
        resp = re.sub(
            r'(?im)^\*\s+((?:india|global|asia|europe|us|usa|commodities|currencies|crypto(?:s|currencies)?|indices|summary|outlook|fii|dii)\b[A-Za-z /&\-]*:)',
            r'\1', resp,
        )

        logger.info(resp)
        return resp

    except Exception as e:
        logger.error(f"Gemini market update failed: {e}")
        return fallback


if __name__ == "__main__":
    print(get_market_update())
