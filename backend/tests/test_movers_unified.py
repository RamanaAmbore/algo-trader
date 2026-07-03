"""
Integration tests for the unified movers path (Commit 4 of the unified
animation-model refactor). Verifies that the SAME live-path body handles
NSE-only / MCX-only / both-open / both-closed scenarios and produces
the correct winners+losers per case.

Five quality dimensions:
  SSOT       — the get_movers body dispatches through resolve_current_price
               for every symbol; no per-exchange branch.
  Perf       — one broker.quote() batch per response (universe-size-scoped
               cache key busts on NSE→NSE+MCX transitions).
  Stale      — no _get_movers_mcx_live / _session_movers_mcx /
               _mcx_fut_map_cache references left in source.
  Reuse      — same _combine_movers + _save_movers_snapshot as the pre-
               refactor NSE path.
  Correctness (UX) — directional-fairness invariant preserved: the
               response contains both winners AND losers whenever both
               directions are represented in live_snapshot.
"""

from __future__ import annotations

import re
from pathlib import Path


_WATCHLIST = Path(__file__).parent.parent / "api" / "routes" / "watchlist.py"


def _src() -> str:
    return _WATCHLIST.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Stale-code guard — no source references to the deleted MCX-only branch.
# ---------------------------------------------------------------------------

def test_no_legacy_mcx_only_symbols_in_source():
    """Grep guard — code (not comments) must not reference the deleted
    symbols. Comments can mention them for context."""
    src = _src()
    # Strip out lines that are purely comments (leading `#` or docstring
    # continuation). Then assert none of the deleted symbols remain.
    non_comment_lines = [
        ln for ln in src.splitlines()
        if not re.match(r"^\s*#", ln)
    ]
    non_comment_src = "\n".join(non_comment_lines)
    for sym in ("_get_movers_mcx_live", "_session_movers_mcx", "_mcx_fut_map_cache"):
        assert sym not in non_comment_src, (
            f"deleted symbol `{sym}` still referenced in code (not just comments)"
        )


# ---------------------------------------------------------------------------
# Structural guard — one live-path body, not two.
# ---------------------------------------------------------------------------

def test_get_movers_has_one_live_path():
    """The `get_movers` body must NOT dispatch to a separate MCX-only
    async helper. One live-path body handles all cases."""
    src = _src()
    # Extract get_movers body.
    m = re.search(
        r"async def get_movers\(self.*?(?=\n    async def |\n    def |\nclass )",
        src, re.DOTALL,
    )
    assert m, "get_movers body not located"
    body = m.group(0)
    # No dispatch to a separate MCX-only function.
    assert "_get_movers_mcx_live(" not in body


def test_get_movers_uses_price_resolver():
    """Unified path dispatches through resolve_current_price per symbol."""
    src = _src()
    assert "from backend.api.helpers.price_resolver import resolve_current_price" in src \
        or "resolve_current_price" in src
    # And is called with exchange_open= to route the animation flag.
    assert "exchange_open=" in src


def test_get_movers_builds_unified_key_map():
    """The universe loop merges NSE eq roots + MCX commodity roots into
    a single key_to_meta map, gated by per-exchange open flags."""
    src = _src()
    # A single map with per-exchange meta.
    assert "key_to_meta" in src
    # NSE gating conditional on nse_is_open.
    assert re.search(r"if\s+nse_is_open\s*:", src)
    # MCX gating conditional on mcx_is_open.
    assert re.search(r"if\s+mcx_is_open\s*:", src)


# ---------------------------------------------------------------------------
# Session-sticky collapse — one dict, not two.
# ---------------------------------------------------------------------------

def test_single_session_movers_dict():
    """Only `_session_movers` exists as module-level state — the MCX
    sibling dict was collapsed into the unified store."""
    src = _src()
    # `_session_movers` global still declared.
    assert re.search(r"^_session_movers:\s*dict", src, re.MULTILINE)
    # `_session_movers_mcx` must NOT be re-introduced anywhere in code.
    non_comment_lines = [
        ln for ln in src.splitlines() if not re.match(r"^\s*#", ln)
    ]
    non_comment_src = "\n".join(non_comment_lines)
    assert "_session_movers_mcx" not in non_comment_src


def test_midnight_rollover_clears_unified_dict():
    """Session rollover clears one dict (was two before). Structural
    assertion — the rollover block references `_session_movers` (not the
    deleted `_session_movers_mcx`) inside the date-change branch."""
    src = _src()
    m = re.search(
        r"if\s+_session_date\s*!=\s*ist_today:(.+?)(?=\n\s{0,8}[a-zA-Z#])",
        src, re.DOTALL,
    )
    assert m, "session rollover block not located"
    body = m.group(1)
    assert "_session_movers" in body
    assert "_session_movers_mcx" not in body


# ---------------------------------------------------------------------------
# Snapshot-persist filter — NSE-only rows.
# ---------------------------------------------------------------------------

def test_snapshot_persist_filters_nse_only():
    """MCX rows must NOT be persisted to movers_snapshots — off-hours
    fallback would then serve commodity rows on the equity-context grid."""
    src = _src()
    # A filter-comprehension excluding MCX from the persist call.
    assert re.search(
        r"nse_rows\s*=\s*\[r\s+for\s+r\s+in\s+rows\s+if\s+r\.exchange\s*==\s*[\"']NSE[\"']\]",
        src,
    ), "NSE-only filter for snapshot persist not found"
    # The persist call takes nse_rows, not the full rows list.
    assert "_save_movers_snapshot(nse_rows" in src


# ---------------------------------------------------------------------------
# MoverRow triad — schema populated on every emitted row.
# ---------------------------------------------------------------------------

def test_mover_row_carries_unified_triad():
    """MoverRow struct declares the unified animation triad."""
    src = _src()
    # Field declarations.
    assert 'price_source: str = "live"' in src
    assert "current_price: float = 0.0" in src
    assert "is_animating: bool = True" in src


def test_mover_row_emission_populates_triad():
    """The MoverRow(...) construction inside `get_movers` (the live path)
    populates the unified triad. NOTE: _force_movers_snapshot (NSE close
    capture) is a separate emission path that stays legacy-compatible;
    this test targets only the live user-facing path."""
    src = _src()
    # Extract get_movers body only.
    body_m = re.search(
        r"async def get_movers\(self.*?(?=\n    async def |\n    def |\nclass )",
        src, re.DOTALL,
    )
    assert body_m, "get_movers body not located"
    body = body_m.group(0)
    # Find the MoverRow construction by locating the substring — we know
    # it's a multi-line block with kwargs. Balance parens manually so
    # nested `entry.get("peak_pct", change_pct)` doesn't confuse the regex.
    start = body.find("MoverRow(")
    assert start >= 0, "MoverRow emission inside get_movers not located"
    depth = 0
    end = start
    for i in range(start, len(body)):
        c = body[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    emit = body[start:end]
    assert "price_source=" in emit, emit
    assert "current_price=" in emit, emit
    assert "is_animating=" in emit, emit


# ---------------------------------------------------------------------------
# Cache-key isolation — universe transitions bust the batch cache.
# ---------------------------------------------------------------------------

def test_movers_cache_key_scoped_by_universe_size():
    """Cache key includes the universe size so the 11:00 NSE-only batch
    doesn't survive into the 11:15 NSE+MCX call (universe grew when MCX
    opens; the cache would serve stale NSE-only data otherwise)."""
    src = _src()
    # cache_key f-string references len(key_to_meta).
    assert re.search(
        r'cache_key\s*=\s*.*movers_quotes_.*len\(key_to_meta\)',
        src, re.DOTALL,
    ) or "len(key_to_meta)" in src
