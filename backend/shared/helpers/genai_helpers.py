"""
Low-volume Gemini Flash helpers — stays inside the free tier (250 RPD,
10 RPM, 250k TPM as of 2026). Used by:

  - Research thread auto-title (POST /api/research/threads when title="")
  - News-headline sentiment scoring (GET /api/news/ sentiment query param)

Both helpers are deterministic-fallback safe: when `is_enabled('genai')`
is False, when the SDK is missing, when Gemini returns empty/None, or
when ANY exception fires, we return a sane stub instead of raising.
That keeps the Lab page and news feed working even if Google's quota
flips us off mid-day.
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import is_enabled, secrets, ramboq_config

logger = get_logger(__name__)


def _client_and_types():
    """Lazy import — `google-genai` is only required when GenAI is on.
    Returns (client, types_module) or (None, None) on any failure."""
    try:
        from google import genai
        from google.genai import types as _types
        api_key = (secrets or {}).get("gemini_api_key") or ""
        if not api_key:
            return None, None
        return genai.Client(api_key=api_key), _types
    except Exception as e:
        logger.debug(f"genai client unavailable: {e}")
        return None, None


def _model() -> str:
    # Flash-only — Pro burns through the free quota much faster.
    return (ramboq_config or {}).get("genai_model", "gemini-2.5-flash")


def _generate(prompt: str, system: str, *, max_tokens: int = 64) -> str | None:
    """Single-shot Gemini Flash call with tight token budget. Returns
    None on any failure so callers fall back to their stub. No grounding,
    no tools — minimises latency + token spend so the call stays under
    the free-tier RPM cap even under burst load."""
    client, types_mod = _client_and_types()
    if not client or not types_mod:
        return None
    try:
        resp = client.models.generate_content(
            model=_model(),
            contents=prompt,
            config=types_mod.GenerateContentConfig(
                system_instruction=system,
                temperature=0.2,
                max_output_tokens=int(max_tokens),
                # No thinking budget — these tasks are direct extraction,
                # giving the model a thinking budget eats output tokens
                # and starves the actual response on Flash.
                thinking_config=types_mod.ThinkingConfig(thinking_budget=0),
            ),
        )
        txt = (resp.text or "").strip() if resp else ""
        return txt or None
    except Exception as e:
        logger.warning(f"Gemini call failed (silent fallback): {e}")
        return None


# ── Helper 1: auto-title a research thread ────────────────────────────

_TITLE_SYSTEM = (
    "You generate concise 4-7 word titles summarising a stock-research "
    "thesis. Output the title only — no quotes, no trailing period, "
    "no commentary. Use title case. Reference the stock symbol when "
    "given. Examples: 'RELIANCE oversold short-term rebound likely', "
    "'NIFTY OI buildup signals bearish week'."
)


def _stub_title(symbol: str, thesis_text: str | None) -> str:
    """Deterministic fallback when genai is off / fails. Picks the
    first meaningful clause of the thesis, falls back to the symbol."""
    sym = (symbol or "").strip().upper() or "RESEARCH"
    if not thesis_text:
        return f"{sym} research"
    # First sentence, max 60 chars.
    head = re.split(r"[.!?\n]", thesis_text.strip(), maxsplit=1)[0].strip()
    if not head:
        return f"{sym} research"
    if len(head) > 60:
        head = head[:57].rstrip() + "…"
    return head


def auto_title(symbol: str, thesis_text: str | None) -> str:
    """Return a short 4-7 word title for a research thread. Gemini Flash
    when enabled; deterministic stub otherwise. Caps at 200 chars to
    fit the DB column."""
    if not is_enabled("genai"):
        return _stub_title(symbol, thesis_text)[:200]
    sym = (symbol or "").upper().strip() or "RESEARCH"
    body = (thesis_text or "").strip()
    if not body:
        return f"{sym} research"
    prompt = (
        f"Symbol: {sym}\n"
        f"Thesis:\n{body[:600]}\n\n"
        f"Write the title now."
    )
    out = _generate(prompt, _TITLE_SYSTEM, max_tokens=48)
    if not out:
        return _stub_title(sym, body)[:200]
    # Strip quotes / trailing period the model sometimes adds.
    out = out.strip().strip('"“”‘’').rstrip(".")
    return out[:200] or _stub_title(sym, body)[:200]


# ── Helper 2: sentiment score for news headlines ──────────────────────

_SENT_SYSTEM = (
    "You score Indian-market news headlines for trader sentiment. "
    "Reply with a SINGLE JSON object of the form "
    "{\"scores\":[{\"i\":0,\"s\":\"bull|bear|neutral\"}, ...]} where "
    "each i matches the input index and s is one of bull / bear / neutral. "
    "No other text, no commentary, no markdown fences."
)


def _stub_sentiment(headlines: list[str]) -> list[str]:
    """Keyword heuristic when genai is off. Bull-leaning vs bear-leaning
    keyword lists are sufficient for the surface and cost ₹0."""
    # Trailing \w* allows inflected forms (jumps / plunged / gaining)
    # without enumerating every tense. Word-start \b still anchors to
    # token boundaries so "outperform" doesn't fire on "underperform".
    bull = re.compile(
        r"\b(jump|surge|rally|gain|rise|risen|risi|rose|"
        r"upgrad|outperform|profit|growth|breakout|advance|"
        r"record|beat|strong|bullish|soar|climb)\w*\b",
        re.IGNORECASE,
    )
    bear = re.compile(
        r"\b(plunge|drop|fall|fell|slump|slip|crash|tumble|"
        r"miss|downgrad|loss|losses|decline|cut|fear|"
        r"sell\s?off|warning|bearish|weak|sink|skid)\w*\b",
        re.IGNORECASE,
    )
    out = []
    for h in headlines:
        h = h or ""
        b = len(bull.findall(h))
        d = len(bear.findall(h))
        if b > d:    out.append("bull")
        elif d > b:  out.append("bear")
        else:        out.append("neutral")
    return out


def _parse_sentiment_response(
    txt: str, headlines: list[str], sub: list[str],
) -> "list[str] | None":
    """Parse Gemini JSON response into per-headline sentiment list.

    Returns the scored list on success, or None to signal caller
    should fall back to _stub_sentiment.
    """
    if txt.startswith("```"):
        txt = re.sub(r"^```[a-zA-Z]*\n?", "", txt)
        txt = re.sub(r"\n?```$", "", txt).strip()
    try:
        obj: Any = json.loads(txt)
        rows = obj.get("scores", []) if isinstance(obj, dict) else []
    except Exception as e:
        logger.warning(f"sentiment parse failed: {e!r} :: {txt[:120]!r}")
        return None

    scored: list[str] = ["neutral"] * len(headlines)
    valid = {"bull", "bear", "neutral"}
    for row in rows:
        try:
            i = int(row.get("i"))
            s = str(row.get("s", "")).lower()
            if 0 <= i < len(headlines) and s in valid:
                scored[i] = s
        except Exception:
            pass
    if len(headlines) > len(sub):
        scored[len(sub):] = _stub_sentiment(headlines[len(sub):])
    return scored


def sentiment_scores(headlines: list[str]) -> list[str]:
    """Return one of bull / bear / neutral per headline. Single-batch
    Gemini Flash call when enabled; keyword stub otherwise. Empty list
    in → empty list out.

    Batched intentionally — Gemini Flash free tier is 10 RPM, so 30
    headlines per single call is far better than 30 separate calls.
    """
    headlines = [str(h or "") for h in (headlines or [])]
    if not headlines:
        return []
    if not is_enabled("genai"):
        return _stub_sentiment(headlines)

    sub = headlines[:50]
    bullet = "\n".join(f"{i}. {h[:160]}" for i, h in enumerate(sub))
    prompt = (
        f"Score these {len(sub)} Indian-market news headlines:\n"
        f"{bullet}\n\n"
        f"Reply with the JSON object now."
    )
    out = _generate(prompt, _SENT_SYSTEM, max_tokens=24 + len(sub) * 8)
    if not out:
        return _stub_sentiment(headlines)

    result = _parse_sentiment_response(out.strip(), headlines, sub)
    return result if result is not None else _stub_sentiment(headlines)
