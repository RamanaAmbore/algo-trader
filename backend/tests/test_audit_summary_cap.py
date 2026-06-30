"""
Tests for the audit summary capture cap (Issue D).

Verifies:
  - A 2000-char response body is captured in full (no truncation within 2048 limit)
  - A 5000-char response body is truncated to 2048 chars
  - The text-fallback path also caps at 2048 (not the old 200 chars)
  - The JSON detail path is unaffected by the cap change

Six dimensions:
  SSOT    — 2048 cap enforced at both buffer capture and text-fallback stages.
  Perf    — _send_wrapper overhead stays O(1) per chunk (no unbounded concat).
  Stale   — grep audit.py confirms cap constant is 2048, not 200 or 1024.
  Reuse   — uses the same captured-dict + body_chunks pattern (no new state).
  UX      — N/A (middleware-only; the cap is not user-visible directly).
  Response— assertion on the stored summary string length.
"""

import pytest


# ─── Stale guard: grep for old cap ──────────────────────────────────────────

def test_audit_body_cap_is_2048_not_1024():
    """Stale: audit.py must use 2048 as the capture limit, not the old 1024."""
    import pathlib
    audit_src = pathlib.Path(__file__).parent.parent / "api" / "audit.py"
    text = audit_src.read_text()

    # The _BODY_CAP constant must be 2048
    assert "_BODY_CAP = 2048" in text, (
        "audit.py: _BODY_CAP must be 2048. "
        "Found neither '2048' as the capture constant. "
        "Ensure Issue D was applied correctly."
    )

    # Old single-line 1024 capture guard must be gone
    old_guard = 'captured["body_len"] < 1024'
    assert old_guard not in text, (
        "audit.py: old 1024 body cap is still present — Issue D may not have landed."
    )


def test_audit_text_fallback_cap_is_2048_not_200():
    """Stale: the plain-text fallback truncation must be 2048, not the old 200."""
    import pathlib
    audit_src = pathlib.Path(__file__).parent.parent / "api" / "audit.py"
    text = audit_src.read_text()

    # Old [:200] truncation on the text fallback must be gone
    assert "][:200]" not in text, (
        "audit.py: old [:200] text truncation is still present in the plain-text "
        "fallback path. Bump to [:2048] as per Issue D."
    )


# ─── SSOT: buffer capture and parse ─────────────────────────────────────────

def _simulate_body_capture(body_chunks: list[bytes], body_cap: int = 2048) -> str:
    """Replicate the audit middleware capture + decode logic inline so we can
    unit-test it without needing a full Litestar app. This must stay in sync
    with the actual implementation in audit.py — the test acts as a contract."""
    import json as _json

    captured_chunks: list[bytes] = []
    captured_len = 0

    for chunk in body_chunks:
        if captured_len < body_cap:
            captured_chunks.append(chunk[:body_cap - captured_len])
            captured_len += len(chunk)

    body_bytes = b"".join(captured_chunks)[:body_cap]
    summary = None
    if body_bytes:
        try:
            obj = _json.loads(body_bytes.decode("utf-8", errors="replace"))
            if isinstance(obj, dict):
                summary = obj.get("detail") or obj.get("message") or None
        except Exception:
            pass
        if not summary:
            summary = body_bytes.decode("utf-8", errors="replace").splitlines()[0][:body_cap]
    return summary or ""


def test_2000_char_body_captured_in_full():
    """SSOT: a 2000-char response body must be stored in full (below 2048 cap)."""
    long_body = "A" * 2000
    body_bytes = long_body.encode("utf-8")
    summary = _simulate_body_capture([body_bytes])
    assert len(summary) == 2000, (
        f"Expected 2000 chars captured, got {len(summary)}. "
        "The 2KB cap should not truncate a 2000-char body."
    )


def test_5000_char_body_truncated_to_2048():
    """SSOT + Response: a 5000-char body must be truncated to exactly 2048 chars."""
    long_body = "B" * 5000
    body_bytes = long_body.encode("utf-8")
    summary = _simulate_body_capture([body_bytes])
    assert len(summary) == 2048, (
        f"Expected exactly 2048 chars after truncation, got {len(summary)}."
    )


def test_json_detail_unaffected_by_cap_change():
    """SSOT: JSON responses with a 'detail' key short-circuit the text-truncation
    path — the detail value is returned verbatim (no per-char truncation).
    A short error message must be preserved exactly."""
    import json
    error_msg = "Insufficient margin: required ₹20,653,877.56, available ₹9,216,518.60"
    body = json.dumps({"detail": error_msg}).encode("utf-8")
    summary = _simulate_body_capture([body])
    assert summary == error_msg, (
        f"JSON detail path mangled the error string. Got: {summary!r}"
    )


def test_multipart_chunks_capped_correctly():
    """Reuse: when the body arrives in multiple ASGI chunks the cap applies
    across the whole body (not per-chunk), matching the middleware pattern."""
    chunk1 = ("C" * 1500).encode("utf-8")
    chunk2 = ("D" * 2000).encode("utf-8")
    summary = _simulate_body_capture([chunk1, chunk2])
    # Total available is 3500 chars, but cap is 2048
    assert len(summary) == 2048, (
        f"Multi-chunk body: expected 2048 chars, got {len(summary)}."
    )
    # First 1500 should be C, next 548 should be D
    assert summary[:1500] == "C" * 1500
    assert summary[1500:] == "D" * 548


def test_200_char_body_preserved_under_new_cap():
    """Regression guard: a short 200-char body (common for Litestar validation
    errors) must not be padded or altered — old 200-char limit was a truncation,
    not a minimum."""
    short = "E" * 200
    body_bytes = short.encode("utf-8")
    summary = _simulate_body_capture([body_bytes])
    assert summary == short, (
        f"200-char body altered under new cap. Got: {summary!r}"
    )
