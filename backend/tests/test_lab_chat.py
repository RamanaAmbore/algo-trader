"""Lab chat endpoint — POST /api/lab/chat runs `claude -p` with read-only MCP tools.

The subprocess is mocked (no real `claude` is run). Covers the allow-list
contents, the 400/403/502/503/504 contract, the token-masking rules, and the
one-at-a-time concurrency guarantee.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from litestar.exceptions import HTTPException

from backend.api.algo import lab_chat

FAKE_TOKEN = "sk-ant-oat01-FAKESECRETVALUE1234567890"

# The 18 read-only tools in kite_server.py at the time of writing.
EXPECTED_READ_TOOLS = {
    "get_positions", "get_holdings", "get_quote", "get_ohlcv",
    "get_recent_news", "get_option_analytics", "get_options_chain_snapshot",
    "get_economic_snapshot", "get_funds_summary", "get_watchlist",
    "get_pnl_attribution", "get_research_thread", "get_audit_recent",
    "get_order_events", "get_server_info", "list_agents",
    "list_research_threads", "dry_run_agent",
}
FORBIDDEN_TOOLS = {
    "place_order", "cancel_order", "modify_order", "activate_agent",
    "deactivate_agent", "update_agent", "save_agent_draft",
    "save_research_thread",
}


class _FakeProc:
    """Stands in for asyncio.subprocess.Process."""

    def __init__(self, stdout: bytes = b"", stderr: bytes = b"",
                 returncode: int = 0, hang: bool = False) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._hang = hang
        self.returncode = returncode
        self.kill = MagicMock(name="kill")
        self.wait = AsyncMock(name="wait", return_value=returncode)

    async def communicate(self) -> tuple[bytes, bytes]:
        if self._hang:
            await asyncio.sleep(3600)
        return self._stdout, self._stderr


def _spawn_returning(proc: _FakeProc):
    """Patch create_subprocess_exec to return `proc`, recording argv."""
    calls: list[tuple] = []

    async def _fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return proc

    return calls, patch(
        "backend.api.algo.lab_chat.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    )


@pytest.fixture
def chat_env(monkeypatch):
    """Token + claude-on-PATH present; fresh lock per test (no loop leakage)."""
    monkeypatch.setenv(lab_chat.TOKEN_ENV, FAKE_TOKEN)
    monkeypatch.setattr(lab_chat.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(lab_chat, "_chat_lock", asyncio.Lock())
    return monkeypatch


def _allowed_from_argv(args: tuple) -> list[str]:
    idx = args.index("--allowedTools")
    out: list[str] = []
    for item in args[idx + 1:]:
        if item.startswith("--"):
            break
        out.append(item)
    return out


def _auth_as(role: str, username: str = "ops"):
    """Patch jwt_guard so the request carries a JWT with the given role."""
    async def _fake(connection, _handler):
        connection.state.token_payload = {"sub": username, "role": role}

    return patch("backend.api.auth_guard.jwt_guard", new=AsyncMock(side_effect=_fake))


# ── allow-list ───────────────────────────────────────────────────────────

def test_allowed_tools_include_every_read_tool_with_mcp_prefix():
    allowed = set(lab_chat.allowed_mcp_tools())
    expected = {f"mcp__ramboq-research__{n}" for n in EXPECTED_READ_TOOLS}
    assert allowed == expected


def test_allowed_tools_exclude_every_place_cancel_modify_mint_tool():
    allowed = set(lab_chat.allowed_mcp_tools())
    for name in FORBIDDEN_TOOLS:
        assert f"mcp__ramboq-research__{name}" not in allowed, name
    assert all(
        t.split("__")[-1].startswith(lab_chat.READ_TOOL_PREFIXES) for t in allowed
    )


def test_read_only_filter_denylist_blocks_even_with_read_prefix():
    names = ["get_positions", "list_agents", "get_place_order_stub"]
    assert lab_chat.read_only_tool_names(names) == [
        "get_positions", "list_agents", "get_place_order_stub",
    ]
    assert lab_chat.read_only_tool_names(["list_agents", "cancel_order"]) == ["list_agents"]


def test_tool_discovery_reads_app_tool_decorators_only():
    source = (
        "@app.tool()\nasync def get_x():\n    pass\n"
        "@other.tool()\nasync def get_y():\n    pass\n"
        "def helper():\n    pass\n"
    )
    assert lab_chat.tool_names_from_source(source) == ["get_x"]


@pytest.mark.asyncio
async def test_argv_passes_allowed_tools_and_message_as_separate_element(chat_env):
    proc = _FakeProc(stdout=json.dumps({"result": "ok"}).encode())
    calls, patcher = _spawn_returning(proc)
    message = "--dangerously-skip-permissions; rm -rf / $(id)"
    with patcher:
        result = await lab_chat.run_lab_chat(message, username="ops")

    assert result.reply == "ok"
    args, kwargs = calls[0]
    assert args[0] == "claude"
    assert "-p" in args
    assert "--output-format" in args and args[args.index("--output-format") + 1] == "json"
    assert args[args.index("--permission-mode") + 1] == "default"
    # Message is one argv element after the `--` terminator, never shell-joined.
    assert args[-2] == "--" and args[-1] == message
    assert kwargs.get("cwd") == str(lab_chat.REPO_ROOT)
    assert "shell" not in kwargs
    allowed = _allowed_from_argv(args)
    assert set(allowed) == set(lab_chat.allowed_mcp_tools())
    assert not any(f"__{n}" in a for a in allowed for n in FORBIDDEN_TOOLS)


# ── 503 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_claude_executable_returns_503_naming_claude(chat_env):
    chat_env.setattr(lab_chat.shutil, "which", lambda _name: None)
    with pytest.raises(HTTPException) as exc:
        await lab_chat.run_lab_chat("hello", username="ops")
    assert exc.value.status_code == 503
    assert "claude" in exc.value.detail


@pytest.mark.asyncio
async def test_missing_token_returns_503_without_leaking_token_value(chat_env):
    chat_env.delenv(lab_chat.TOKEN_ENV, raising=False)
    with pytest.raises(HTTPException) as exc:
        await lab_chat.run_lab_chat("hello", username="ops")
    assert exc.value.status_code == 503
    assert lab_chat.TOKEN_ENV in exc.value.detail
    assert FAKE_TOKEN not in exc.value.detail
    assert "sk-ant-" not in exc.value.detail


@pytest.mark.asyncio
async def test_missing_token_and_claude_both_named_in_detail(chat_env):
    chat_env.delenv(lab_chat.TOKEN_ENV, raising=False)
    chat_env.setattr(lab_chat.shutil, "which", lambda _name: None)
    with pytest.raises(HTTPException) as exc:
        await lab_chat.run_lab_chat("hello", username="ops")
    assert "claude" in exc.value.detail and lab_chat.TOKEN_ENV in exc.value.detail


@pytest.mark.asyncio
async def test_exec_race_file_not_found_returns_503(chat_env):
    with patch(
        "backend.api.algo.lab_chat.asyncio.create_subprocess_exec",
        side_effect=FileNotFoundError("claude"),
    ):
        with pytest.raises(HTTPException) as exc:
            await lab_chat.run_lab_chat("hello", username="ops")
    assert exc.value.status_code == 503
    assert "claude" in exc.value.detail


# ── 504 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_timeout_returns_504_and_kills_process(chat_env, monkeypatch):
    monkeypatch.setattr(lab_chat, "LAB_CHAT_TIMEOUT_S", 0.05)
    proc = _FakeProc(hang=True)
    _, patcher = _spawn_returning(proc)
    with patcher:
        with pytest.raises(HTTPException) as exc:
            await lab_chat.run_lab_chat("hello", username="ops")
    assert exc.value.status_code == 504
    assert exc.value.detail == "timed out"
    proc.kill.assert_called_once()
    proc.wait.assert_awaited()


def test_timeout_constant_is_120_seconds():
    assert lab_chat.LAB_CHAT_TIMEOUT_S == 120.0


# ── 502 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nonzero_exit_returns_502_with_masked_stderr_snippet(chat_env):
    stderr = (
        f"auth failed for token {FAKE_TOKEN} and also sk-ant-oat01-OTHERTOKENXYZ "
        + "x" * 500
    ).encode()
    proc = _FakeProc(stderr=stderr, returncode=1)
    _, patcher = _spawn_returning(proc)
    with patcher:
        with pytest.raises(HTTPException) as exc:
            await lab_chat.run_lab_chat("hello", username="ops")
    assert exc.value.status_code == 502
    detail = exc.value.detail
    assert "auth failed" in detail
    assert FAKE_TOKEN not in detail
    assert "OTHERTOKENXYZ" not in detail
    assert "sk-ant-***" in detail
    # 300-char snippet of stderr (prefix text is short, so bound the body).
    assert len(detail) <= len("claude exited 1: ") + lab_chat.STDERR_SNIPPET_CHARS


def test_mask_secrets_masks_prefix_and_live_token(monkeypatch):
    monkeypatch.setenv(lab_chat.TOKEN_ENV, "plainTokenValue123")
    out = lab_chat.mask_secrets("a sk-ant-abc_123 b plainTokenValue123 c")
    assert "sk-ant-abc" not in out and "plainTokenValue123" not in out
    assert out == "a sk-ant-*** b *** c"


# ── success path + parsing ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_success_returns_result_field_and_masks_reply(chat_env):
    body = {"type": "result", "result": f"positions ok {FAKE_TOKEN}"}
    _, patcher = _spawn_returning(_FakeProc(stdout=json.dumps(body).encode()))
    with patcher:
        result = await lab_chat.run_lab_chat("show positions", username="ops")
    assert result.reply == "positions ok sk-ant-***"
    assert result.duration_ms >= 0


@pytest.mark.asyncio
async def test_unparseable_stdout_returned_raw(chat_env):
    _, patcher = _spawn_returning(_FakeProc(stdout=b"plain text answer"))
    with patcher:
        result = await lab_chat.run_lab_chat("hi", username="ops")
    assert result.reply == "plain text answer"


@pytest.mark.asyncio
async def test_json_without_result_field_returns_raw_stdout(chat_env):
    raw = json.dumps({"type": "result"}).encode()
    _, patcher = _spawn_returning(_FakeProc(stdout=raw))
    with patcher:
        result = await lab_chat.run_lab_chat("hi", username="ops")
    assert result.reply == raw.decode()


# ── 400 ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_message_returns_400(chat_env):
    for msg in ("", "   \n\t"):
        with pytest.raises(HTTPException) as exc:
            await lab_chat.run_lab_chat(msg, username="ops")
        assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_over_length_message_returns_400(chat_env):
    with pytest.raises(HTTPException) as exc:
        await lab_chat.run_lab_chat("a" * (lab_chat.MAX_MESSAGE_CHARS + 1), username="ops")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_message_at_limit_is_accepted(chat_env):
    _, patcher = _spawn_returning(_FakeProc(stdout=b'{"result":"ok"}'))
    with patcher:
        result = await lab_chat.run_lab_chat("a" * lab_chat.MAX_MESSAGE_CHARS, username="ops")
    assert result.reply == "ok"


# ── concurrency ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_concurrent_requests_run_one_at_a_time(chat_env):
    active = 0
    max_active = 0
    entries = 0

    class _SlowProc(_FakeProc):
        async def communicate(self):
            nonlocal active, max_active, entries
            entries += 1
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.05)
            active -= 1
            return b'{"result":"ok"}', b""

    async def _fake_exec(*args, **kwargs):
        return _SlowProc()

    with patch("backend.api.algo.lab_chat.asyncio.create_subprocess_exec",
               side_effect=_fake_exec):
        results = await asyncio.gather(
            lab_chat.run_lab_chat("first", username="a"),
            lab_chat.run_lab_chat("second", username="b"),
        )

    assert [r.reply for r in results] == ["ok", "ok"]
    assert entries == 2
    assert max_active == 1


# ── route-level: 403 / 200 / 400 wiring ──────────────────────────────────

@pytest.mark.asyncio
async def test_partner_role_gets_403(async_client, chat_env):
    with _auth_as("partner"):
        res = await async_client.post("/api/lab/chat", json={"message": "hi"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_trader_role_gets_403_even_with_mcp_tools_cap(async_client, chat_env):
    # use_mcp_tools is held by trader, but chat is designated-only (use_lab_chat).
    with _auth_as("trader"):
        res = await async_client.post("/api/lab/chat", json={"message": "hi"})
    assert res.status_code == 403


@pytest.mark.asyncio
async def test_designated_role_gets_200_with_reply(async_client, chat_env):
    _, patcher = _spawn_returning(_FakeProc(stdout=b'{"result":"hello there"}'))
    with _auth_as("designated"), patcher:
        res = await async_client.post("/api/lab/chat", json={"message": "hi"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["reply"] == "hello there"
    assert isinstance(body["duration_ms"], int)


def test_use_lab_chat_cap_is_designated_only():
    from backend.api.rbac import CAPS
    assert CAPS["use_lab_chat"] == frozenset({"designated"})


@pytest.mark.asyncio
async def test_route_empty_message_returns_400(async_client, chat_env):
    with _auth_as("designated"):
        res = await async_client.post("/api/lab/chat", json={"message": ""})
    assert res.status_code == 400
