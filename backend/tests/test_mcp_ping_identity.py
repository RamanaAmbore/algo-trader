"""2026-11-08 audit fix (item 3) regression guard.

`lab._mcp_ping()` used to call `format_notification_header(None, None)`
directly and bake the resulting "Agent: manual" header into the log
record's `tg` field — fabricating a manual origin for a tool call that
has no agent concept at all. The fix moves header construction out of
the producer and into the renderer (`event_agents._render_mcp_ping`),
classifying the record `source_type="process", source_name="MCP"`.
"""
from __future__ import annotations


def test_mcp_ping_logs_process_identity_not_a_prebuilt_manual_header(monkeypatch):
    from backend.api.routes import lab

    calls = []
    monkeypatch.setattr(lab.logger, "info", lambda msg, **kw: calls.append((msg, kw)))

    lab._mcp_ping("order placed via MCP tool call")

    assert len(calls) == 1
    _msg, kw = calls[0]
    extra = kw["extra"]
    assert extra["source_type"] == "process"
    assert extra["source_name"] == "MCP"
    assert extra["alert_event"] == "mcp_ping"
    # The producer must not pre-build any header text itself.
    assert "Agent:" not in extra.get("body", "")
    assert "manual" not in extra.get("body", "").lower()


def test_mcp_ping_renderer_builds_process_header_not_manual():
    from backend.api.algo import event_agents as ea

    rec = {
        "extra": {
            "body": "order placed via MCP tool call",
            "source_type": "process",
            "source_name": "MCP",
        },
        "ts": None,
    }
    _title, body, _tg = ea._render_mcp_ping(rec)
    assert body.startswith("Source: MCP")
    assert "manual" not in body.lower()
