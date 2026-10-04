"""Lab chat helper — answers a Lab-page request by running `claude -p`.

The subprocess runs in the repository root with the parent environment
inherited, so `CLAUDE_CODE_OAUTH_TOKEN` (subscription auth) and `PATH`
reach the CLI. Only read-only RamboQuant MCP tools are allowed; the
allow-list is derived from the `@app.tool()` functions in
`backend/mcp/kite_server.py` by name prefix, read via `ast` so importing
this module never starts the FastMCP server.

Concurrency: a module-level asyncio.Lock serialises requests so at most
one `claude` process runs at a time. Timeout: 120 s, then the process is
killed and the caller gets 504.

Secrets: the token value is never logged or returned. Token-looking
strings (`sk-ant-…`) are masked in stderr snippets and replies.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from litestar.exceptions import HTTPException

from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
KITE_SERVER_PATH: Path = REPO_ROOT / "backend" / "mcp" / "kite_server.py"
MCP_SERVER_NAME = "ramboq-research"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
MAX_MESSAGE_CHARS = 4000
LAB_CHAT_TIMEOUT_S: float = 120.0
STDERR_SNIPPET_CHARS = 300

READ_TOOL_PREFIXES: tuple[str, ...] = ("get_", "list_", "dry_run_")
# Belt-and-braces: these names can place/cancel/modify/mint even if a
# future rename ever pushes one of them under a read prefix.
DENIED_TOOLS: frozenset[str] = frozenset({
    "place_order", "cancel_order", "modify_order",
    "activate_agent", "deactivate_agent", "update_agent",
    "save_agent_draft", "save_research_thread",
})

_TOKEN_LIKE = re.compile(r"sk-ant-[A-Za-z0-9_\-]*")

_chat_lock = asyncio.Lock()


@dataclass(frozen=True)
class ChatResult:
    reply: str
    duration_ms: int


def _is_app_tool_decorator(dec: ast.expr) -> bool:
    """True for the `@app.tool()` decorator form used by kite_server.py."""
    return (
        isinstance(dec, ast.Call)
        and isinstance(dec.func, ast.Attribute)
        and dec.func.attr == "tool"
        and isinstance(dec.func.value, ast.Name)
        and dec.func.value.id == "app"
    )


def tool_names_from_source(source: str) -> list[str]:
    """Names of every module-level function decorated with `@app.tool()`."""
    tree = ast.parse(source)
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_app_tool_decorator(d) for d in node.decorator_list)
    ]


def read_only_tool_names(names: list[str]) -> list[str]:
    """Filter tool names to the read-only set (prefix match, minus denylist)."""
    return [
        n for n in names
        if n.startswith(READ_TOOL_PREFIXES) and n not in DENIED_TOOLS
    ]


@lru_cache(maxsize=1)
def allowed_mcp_tools() -> tuple[str, ...]:
    """`mcp__ramboq-research__<tool>` names the chat subprocess may call."""
    source = KITE_SERVER_PATH.read_text(encoding="utf-8")
    names = read_only_tool_names(tool_names_from_source(source))
    return tuple(f"mcp__{MCP_SERVER_NAME}__{n}" for n in names)


def mask_secrets(text: str) -> str:
    """Mask token-looking strings and the live token value, if set."""
    masked = _TOKEN_LIKE.sub("sk-ant-***", text)
    live = os.environ.get(TOKEN_ENV, "")
    if live:
        masked = masked.replace(live, "***")
    return masked


def _missing_requirements() -> list[str]:
    missing: list[str] = []
    if shutil.which("claude") is None:
        missing.append("the `claude` executable was not found on PATH")
    if not os.environ.get(TOKEN_ENV):
        missing.append(f"{TOKEN_ENV} is not set in the server environment")
    return missing


def _reply_from_stdout(stdout: str) -> str:
    """Return the `result` field of `--output-format json`, else raw stdout."""
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout
    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
        return payload["result"]
    return stdout


def _build_argv(message: str, allowed: tuple[str, ...]) -> list[str]:
    # `--` ends option parsing so a message beginning with "-" is taken as the
    # prompt, never as a CLI flag (e.g. --dangerously-skip-permissions).
    return [
        "claude", "-p",
        "--output-format", "json",
        "--allowedTools", *allowed,
        "--permission-mode", "default",
        "--", message,
    ]


async def _run_subprocess(message: str) -> tuple[int, bytes, bytes]:
    allowed = allowed_mcp_tools()
    proc = await asyncio.create_subprocess_exec(
        *_build_argv(message, allowed),
        cwd=str(REPO_ROOT),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=LAB_CHAT_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(status_code=504, detail="timed out")
    return proc.returncode, stdout, stderr


async def run_lab_chat(message: str, *, username: str) -> ChatResult:
    """Run one chat request through `claude -p`. Raises HTTPException on failure."""
    outcome = "error"
    run_started: float | None = None
    try:
        if not message.strip() or len(message) > MAX_MESSAGE_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"message must be 1-{MAX_MESSAGE_CHARS} characters",
            )
        missing = _missing_requirements()
        if missing:
            raise HTTPException(
                status_code=503,
                detail="Lab chat unavailable: " + "; ".join(missing),
            )
        async with _chat_lock:
            # Duration counts only the claude run, not time spent queued behind
            # another request.
            run_started = time.monotonic()
            try:
                rc, stdout_b, stderr_b = await _run_subprocess(message)
            except FileNotFoundError:
                raise HTTPException(
                    status_code=503,
                    detail="Lab chat unavailable: the `claude` executable was not found on PATH",
                )
        if rc != 0:
            snippet = mask_secrets(stderr_b.decode("utf-8", errors="replace"))
            snippet = snippet[:STDERR_SNIPPET_CHARS].strip()
            raise HTTPException(
                status_code=502,
                detail=f"claude exited {rc}: {snippet or '(no stderr)'}",
            )
        reply = mask_secrets(
            _reply_from_stdout(stdout_b.decode("utf-8", errors="replace"))
        )
        outcome = "ok"
        return ChatResult(reply=reply, duration_ms=_elapsed_ms(run_started))
    except HTTPException as exc:
        outcome = f"http_{exc.status_code}"
        raise
    finally:
        logger.info(
            "lab chat request: user=%s at=%s msg_len=%d duration_ms=%d outcome=%s",
            username,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            len(message),
            _elapsed_ms(run_started),
            outcome,
        )


def _elapsed_ms(started: float | None) -> int:
    if started is None:
        return 0
    return int((time.monotonic() - started) * 1000)
