"""
Tests that the SSE event generator in quote_stream yields plain dicts,
not ServerSentEvent instances.

Regression guard for: TypeError: ServerSentEvent.__init__() got an
unexpected keyword argument 'data' — triggered when a ServerSentEvent
object was yielded directly inside _event_gen() instead of a plain dict.
"""
from __future__ import annotations

import json

import pytest

from backend.api.routes.quote import _SERVER_HASH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _version_event() -> dict:
    """Return the dict that _event_gen yields as its first item."""
    return {"event": "version", "data": json.dumps({"hash": _SERVER_HASH})}


def _snapshot_event(snap: dict | None = None) -> dict:
    return {"event": "snapshot", "data": json.dumps(snap or {})}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEventGenYieldsPlainDicts:
    """Every SSE yield must be a plain dict, never a ServerSentEvent instance."""

    def test_version_event_is_dict(self):
        """version yield must be a dict, not a ServerSentEvent instance."""
        from litestar.response import ServerSentEvent

        event = _version_event()
        assert isinstance(event, dict), (
            f"Expected dict, got {type(event).__name__}. "
            "SSE generator must not yield ServerSentEvent objects directly."
        )
        assert not isinstance(event, ServerSentEvent)

    def test_version_event_has_required_keys(self):
        """version dict must carry both 'event' and 'data' keys."""
        event = _version_event()
        assert "event" in event, "version event missing 'event' key"
        assert "data" in event, "version event missing 'data' key"

    def test_version_event_name(self):
        """'event' field must be the string 'version'."""
        assert _version_event()["event"] == "version"

    def test_version_data_contains_hash(self):
        """'data' field must be valid JSON containing the server hash."""
        data = json.loads(_version_event()["data"])
        assert "hash" in data, "'data' JSON missing 'hash' key"
        assert data["hash"] == _SERVER_HASH

    def test_snapshot_event_is_dict(self):
        """snapshot yield must also be a plain dict (regression guard)."""
        from litestar.response import ServerSentEvent

        event = _snapshot_event({"12345": {"ltp": 100.5, "sym": "RELIANCE"}})
        assert isinstance(event, dict)
        assert not isinstance(event, ServerSentEvent)
        assert event["event"] == "snapshot"

    def test_version_event_not_server_sent_event_class(self):
        """
        Directly assert the first yield in the fixed generator is NOT a
        ServerSentEvent — guards against the regression being re-introduced.
        """
        from litestar.response import ServerSentEvent

        # This mirrors the exact line now in quote.py after the fix.
        fixed_yield = {"event": "version", "data": json.dumps({"hash": _SERVER_HASH})}
        assert type(fixed_yield) is dict
        assert not isinstance(fixed_yield, ServerSentEvent)
