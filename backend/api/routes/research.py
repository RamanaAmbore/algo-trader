"""
`/api/research/*` — research-thread CRUD for the /admin/research page.

A research thread captures one MCP-driven session ("Research RELIANCE")
with its transcript, the synthesized thesis, and (after promotion) the
draft Agent it produced. Read by the Lab page; written by the MCP server
(via the operator's JWT) as the chat unfolds.

No GenAI is invoked from this route — it just persists what the operator's
Claude Code session sends back. The transcript is opaque JSONB; the route
doesn't parse tool calls or LLM tokens. That keeps this layer cheap and
free of any LLM-provider dependency.
"""

from __future__ import annotations

import hashlib
import secrets as _secrets
import time
from datetime import datetime
from threading import Lock
from typing import Any

import msgspec
from litestar import Controller, Request, delete, get, patch, post
from litestar.exceptions import HTTPException
from sqlalchemy import select, delete as sa_delete

from backend.api.rbac import cap_guard
from backend.api.database import async_session
from backend.api.models import Agent, McpAudit, ResearchThread
from backend.shared.helpers.ramboq_logger import get_logger

logger = get_logger(__name__)


# ── Schemas ───────────────────────────────────────────────────────────

class ThreadInfo(msgspec.Struct):
    id:                int
    symbol:            str
    title:             str
    thesis_text:       str | None
    confidence:        str            # bull / bear / neutral / unsure
    transcript:        list           # opaque list of {role, content, tool_calls?, ...}
    draft_agent_id:    int | None
    created_by_user_id: int | None
    created_at:        str
    updated_at:        str


class ThreadSummary(msgspec.Struct):
    """Lightweight row for the thread-list rail — no transcript blob."""
    id:                int
    symbol:            str
    title:             str
    confidence:        str
    draft_agent_id:    int | None
    transcript_len:    int
    created_at:        str
    updated_at:        str


class ThreadCreate(msgspec.Struct):
    symbol:            str
    title:             str = ""
    thesis_text:       str | None = None
    confidence:        str = "unsure"
    transcript:        list = msgspec.field(default_factory=list)


class ThreadUpdate(msgspec.Struct):
    title:             str | None = None
    thesis_text:       str | None = None
    confidence:        str | None = None
    transcript:        list | None = None
    draft_agent_id:    int | None = None


class PromoteRequest(msgspec.Struct):
    """Promote a research thread into a draft Agent (status=inactive).

    The agent ships disabled — the operator's next step is "Run in
    Simulator" from /agents to validate the condition tree before
    activating. Per industry pattern (Composer.trade, IBKR TraderGPT),
    no LLM-initiated draft is ever activated automatically.

    Lifespan (Phase 19): the LLM can declare an agent as one-shot or
    time-bound at promote time. The engine already enforces these in
    `run_cycle()` — when the limit is reached the row flips to
    status='completed'. Re-arming requires editing lifespan_* then
    activating again.

      lifespan_type:
        persistent — never expires (default)
        one_shot   — completes after first fire (max_fires implicit 1)
        n_fires    — completes after lifespan_max_fires fires
        until_date — completes when now >= lifespan_expires_at
    """
    name:                str
    conditions:          dict             # v2 grammar condition tree
    actions:             list = msgspec.field(default_factory=list)
    events:              list = msgspec.field(default_factory=list)
    description:         str = ""
    scope:               str = "total"     # total / per_account
    schedule:            str = "market_hours"
    cooldown_minutes:    int = 30
    # Phase 19 — lifespan params for LLM-created agents
    lifespan_type:       str = "persistent"   # persistent / one_shot / n_fires / until_date
    lifespan_max_fires:  int | None = None    # only used when lifespan_type='n_fires'
    lifespan_expires_at: str | None = None    # ISO datetime, only when lifespan_type='until_date'
    # Phase 21 — debounce ("for N minutes"). 0 = fire immediately (default,
    # backwards-compatible). N > 0 = condition must hold N consecutive
    # minutes before firing. Spike-driven false positives suppressed.
    debounce_minutes:    int = 0
    # Phase 22 — tagging + quiet hours.
    tags:                list = msgspec.field(default_factory=list)
    blackout_windows:    list = msgspec.field(default_factory=list)


class MintTokenRequest(msgspec.Struct):
    """Operator's input to the confirm-token mint endpoint.

    `kind` decides which MCP action this token authorises:
      - 'place'      (default) — full order fields apply.
      - 'cancel'     — account + order_id only; the rest are ignored.
      - 'modify'     — account + order_id + the new fields the LLM will
                       pass (quantity / price / order_type / trigger).
      - 'activate'   — agent_slug only. Token authorises flipping ONE
                       specific agent from inactive → active.
      - 'deactivate' — agent_slug only. Same shape; flips active →
                       inactive.

    The purpose hash includes `kind` + the relevant fields per kind,
    so a token minted to CANCEL #1234 cannot be redeemed to PLACE a
    new order, MODIFY #1234, CANCEL #5678, ACTIVATE a different
    agent, or DEACTIVATE the same agent (different kind)."""
    account:           str = ""
    kind:              str = "place"     # place / cancel / modify / activate / deactivate / update
    tradingsymbol:     str = ""
    side:              str = ""           # BUY / SELL (place only)
    quantity:          int = 0
    mode:              str = "paper"     # paper / live (place + cancel/modify)
    order_type:        str = "LIMIT"
    price:             float | None = None
    trigger_price:     float | None = None
    order_id:          str = ""           # cancel / modify only
    agent_slug:        str = ""           # activate / deactivate / update only
    # update kind only — JSON blob of the fields the LLM plans to push.
    # Hashed (canonical JSON) so the operator approves the EXACT change.
    proposed_changes:  dict = msgspec.field(default_factory=dict)


class MintTokenResponse(msgspec.Struct):
    token:          str
    expires_at:     int               # unix epoch seconds
    expires_in:     int               # seconds from now (UI convenience)
    purpose:        str               # human-readable echo of what was minted
    purpose_hash:   str               # for client-side display + debugging


class PlaceOrderRequest(msgspec.Struct):
    """LLM-initiated order. Same shape as MintTokenRequest plus the
    confirm_token the operator pasted in. Optional Lab-page fields
    (chase, aggressiveness) default to safe values; the LLM doesn't
    need to know about them."""
    confirm_token:        str
    account:              str
    tradingsymbol:        str
    side:                 str
    quantity:             int
    mode:                 str = "paper"
    order_type:           str = "LIMIT"
    price:                float | None = None
    trigger_price:        float | None = None
    exchange:             str = "NFO"
    product:              str = "NRML"
    variety:              str = "regular"
    chase:                bool = True
    chase_aggressiveness: str = "low"


class PlaceOrderResponse(msgspec.Struct):
    order_id:    str
    mode:        str
    status:      str
    detail:      str


class CancelOrderRequest(msgspec.Struct):
    """LLM-initiated cancel. Same token-gate pattern as place.

    `mode` selects the destination:
      - 'live' (default) → broker cancel via Kite (existing path).
      - 'paper'          → PaperTradeEngine.cancel_paper_order
                            (order_id must be an AlgoOrder.id integer
                            string for paper orders).
    The mode is part of the token's purpose hash so a paper-cancel
    token can't be redeemed against the broker, or vice versa.
    """
    confirm_token: str
    account:       str
    order_id:      str
    variety:       str = "regular"
    mode:          str = "live"   # live / paper


class ModifyOrderRequest(msgspec.Struct):
    """LLM-initiated modify. Token binds (account, order_id, mode,
    new quantity, new order_type, new price, new trigger).

    Live modify routes through Kite's modify_order; paper modify
    updates the paper engine's open-order dict in place (next chase
    tick picks up the new values)."""
    confirm_token: str
    account:       str
    order_id:      str
    quantity:      int = 0
    order_type:    str = "LIMIT"
    price:         float | None = None
    trigger_price: float | None = None
    variety:       str = "regular"
    validity:      str | None = None
    mode:          str = "live"   # live / paper


class SimpleOrderResponse(msgspec.Struct):
    order_id: str
    detail:   str


class AgentStatusRequest(msgspec.Struct):
    """LLM-initiated agent activate / deactivate. The endpoint name
    decides direction (activate vs deactivate); the request body just
    carries the slug + confirm token."""
    confirm_token: str
    agent_slug:    str


class AgentStatusResponse(msgspec.Struct):
    agent_slug:    str
    status:        str
    detail:        str


class AgentUpdateRequest(msgspec.Struct):
    """LLM-initiated edit of an existing Agent's condition tree /
    events / actions / scope / schedule / cooldown / fire_at_time /
    description. The full proposed-changes dict is hashed into the
    confirm token so the LLM cannot tweak any field after mint.

    Only fields in _AGENT_UPDATE_FIELDS are honoured server-side;
    others (status, trade_mode, lifespan_*) are silently dropped —
    the LLM cannot flip an agent active or live via update_agent."""
    confirm_token:    str
    agent_slug:       str
    proposed_changes: dict = msgspec.field(default_factory=dict)


class AuditRow(msgspec.Struct):
    """One audit-log entry for the Audit tab on /admin/research. Args
    are pre-redacted — token material is never written, so this struct
    can be shown to any admin without leaking authorisation state."""
    id:             int
    tool:           str
    user_id:        int | None
    args_redacted:  dict
    result_status:  str         # ok / denied / error
    result_summary: str
    request_id:     str | None
    created_at:     str


class DraftInfo(msgspec.Struct):
    """Joined view: thread → its linked draft agent (status=inactive).

    Drives the Drafts tab on /admin/research. Excludes threads whose
    draft_agent_id is NULL, and threads whose linked agent has been
    activated (status=active) — those graduate out of the Drafts list
    so it always reflects "still pending review"."""
    thread_id:         int
    symbol:            str
    title:             str
    confidence:        str
    thesis_text:       str | None
    agent_id:          int
    agent_slug:        str
    agent_name:        str
    agent_status:      str
    agent_scope:       str
    agent_schedule:    str | None
    agent_cooldown:    int
    agent_trade_mode:  str | None
    created_at:        str
    updated_at:        str


# ── Helpers ───────────────────────────────────────────────────────────

def _to_info(row: ResearchThread) -> ThreadInfo:
    return ThreadInfo(
        id=row.id,
        symbol=row.symbol,
        title=row.title or "",
        thesis_text=row.thesis_text,
        confidence=row.confidence or "unsure",
        transcript=row.transcript or [],
        draft_agent_id=row.draft_agent_id,
        created_by_user_id=row.created_by_user_id,
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def _to_summary(row: ResearchThread) -> ThreadSummary:
    return ThreadSummary(
        id=row.id,
        symbol=row.symbol,
        title=row.title or "",
        confidence=row.confidence or "unsure",
        draft_agent_id=row.draft_agent_id,
        transcript_len=len(row.transcript or []),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


_VALID_CONF  = {"bull", "bear", "neutral", "unsure"}
_VALID_SCOPE = {"total", "per_account"}


def _public_base_url() -> str:
    """Best-effort public URL for THIS instance. Used to build the
    audit deep-link in Telegram pings — the link goes from the
    operator's phone straight to /admin/research?audit_request=<id>
    on the right host.

    Derives from deploy_branch since the API doesn't otherwise know
    its own public hostname. Operators on a different domain can
    override via backend_config.yaml::public_base_url."""
    try:
        from backend.shared.helpers.utils import config as _cfg
        override = (_cfg or {}).get("public_base_url")
        if override:
            return str(override).rstrip("/")
        branch = (_cfg or {}).get("deploy_branch") or ""
        if branch == "main":
            return "https://ramboq.com"
    except Exception:
        pass
    return "https://dev.ramboq.com"


def _audit_link_html(request_id: str) -> str:
    """HTML fragment for Telegram (parse_mode=HTML) — clickable
    request_id that opens the Lab page Audit tab pre-filtered to
    THIS exact row. Operator on their phone gets a one-tap forensic
    drill-down."""
    rid = (request_id or "").strip()
    if not rid:
        return ""
    href = f"{_public_base_url()}/admin/research?audit_request={rid}"
    return f'<a href="{href}">{rid}</a>'

# ── Phase-3 per-call confirm-token store ──────────────────────────────
# In-process dict — keyed by token, valued by {user_id, purpose_hash,
# expires_at_epoch, used}. Single-use + 60s TTL means the universe of
# live tokens is tiny (< 100 even on the busiest research day), so
# in-memory is fine. Restarting the API invalidates all live tokens;
# the operator just re-mints — exactly the conservative behavior we
# want for a safety surface.
_TOKEN_TTL_SECONDS = 60
_token_lock: Lock = Lock()
_confirm_tokens: dict[str, dict[str, Any]] = {}


def _purpose_hash_place(account: str, symbol: str, side: str, qty: int,
                  order_type: str, mode: str, price: Any, trigger_price: Any) -> str:
    """Identity fingerprint for a PLACE order. Includes every field the
    LLM passes so it can't swap the symbol / side / qty / price."""
    parts = [
        "place",
        (account or "").upper().strip(),
        (symbol  or "").upper().strip(),
        (side    or "").upper().strip(),
        str(int(qty or 0)),
        (order_type or "").upper().strip(),
        (mode or "").lower().strip(),
        f"{float(price or 0):.4f}",
        f"{float(trigger_price or 0):.4f}",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _purpose_hash_cancel(account: str, order_id: str, mode: str = "live") -> str:
    """CANCEL pins (account, order_id, mode). The mode binding stops
    a paper-cancel token from being redeemed against the broker, or
    vice versa — different destinations, different risks."""
    parts = [
        "cancel",
        (account  or "").upper().strip(),
        (order_id or "").strip(),
        (mode or "live").lower().strip(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _purpose_hash_modify(account: str, order_id: str, qty: int,
                         order_type: str, price: Any, trigger_price: Any,
                         mode: str = "live") -> str:
    """MODIFY pins (account, order_id, mode) plus the new values the
    LLM plans to push so it can't bait-and-switch the new price / qty
    after the operator approves a different combination."""
    parts = [
        "modify",
        (account  or "").upper().strip(),
        (order_id or "").strip(),
        str(int(qty or 0)),
        (order_type or "").upper().strip(),
        f"{float(price or 0):.4f}",
        f"{float(trigger_price or 0):.4f}",
        (mode or "live").lower().strip(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# Whitelist of agent fields an LLM-driven update is allowed to touch.
# Anything else (status, trade_mode, lifespan_*) stays operator-only —
# the LLM cannot flip an agent active or live through update_agent.
_AGENT_UPDATE_FIELDS = (
    "conditions", "events", "actions",
    "scope", "schedule", "cooldown_minutes", "debounce_minutes",
    "fire_at_time", "description",
    # Phase 22 — operator-tunable tagging + quiet hours
    "tags", "blackout_windows",
)


def _canonical_changes(payload: dict) -> dict:
    """Filter + normalise the operator's proposed-changes dict before
    hashing. Keeping only whitelisted fields prevents the LLM from
    sneaking a status='active' through update_agent."""
    if not isinstance(payload, dict):
        return {}
    out = {}
    for k in _AGENT_UPDATE_FIELDS:
        if k in payload and payload[k] is not None:
            out[k] = payload[k]
    return out


def _purpose_hash_update_agent(agent_slug: str, changes: dict) -> str:
    """UPDATE pins (agent_slug + canonical-JSON of the changes). The
    LLM cannot tweak any field after mint — every key in the
    proposed-changes dict is part of the hash. Empty changes → 400
    at the mint endpoint, so the hash is always over a meaningful
    payload."""
    import json
    canonical = json.dumps(
        _canonical_changes(changes),
        sort_keys=True, separators=(",", ":"),
    )
    parts = [
        "update_agent",
        (agent_slug or "").strip(),
        hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _purpose_hash_agent_status(action: str, agent_slug: str) -> str:
    """ACTIVATE / DEACTIVATE pins (action, agent_slug). The action is
    part of the kind+hash so a deactivate token cannot be redeemed to
    activate (and vice versa), even for the same agent — symmetric
    protection against the LLM bait-and-switching the direction."""
    parts = [
        action.lower().strip(),     # 'activate' or 'deactivate'
        (agent_slug or "").strip(),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _mint_token(user_id: int | None, purpose_hash: str) -> tuple[str, int]:
    """Generate a fresh 32-char token, return (token, expires_epoch).
    Cleans up expired entries opportunistically on each mint so the
    dict can't grow without bound."""
    tok = _secrets.token_hex(16)
    now = int(time.time())
    expires = now + _TOKEN_TTL_SECONDS
    with _token_lock:
        # Prune expired entries first.
        stale = [t for t, v in _confirm_tokens.items() if v.get("expires_at", 0) < now]
        for t in stale:
            _confirm_tokens.pop(t, None)
        _confirm_tokens[tok] = {
            "user_id":      user_id,
            "purpose_hash": purpose_hash,
            "expires_at":   expires,
            "used":         False,
        }
    return tok, expires


def _consume_token(token: str, user_id: int | None, purpose_hash: str) -> str | None:
    """Validate + consume a token in one atomic step. Returns None on
    success; a short error string otherwise (so the route can surface
    a precise reason to the operator)."""
    now = int(time.time())
    with _token_lock:
        # Phase 13 — prune expired entries on every consume so the
        # dict doesn't accumulate stale tokens during long idle
        # periods between mints. Cheap (≤ a few hundred entries) and
        # symmetric with the prune-on-mint path.
        stale = [t for t, v in _confirm_tokens.items() if v.get("expires_at", 0) < now]
        for t in stale:
            _confirm_tokens.pop(t, None)

        entry = _confirm_tokens.get(token)
        if not entry:
            return "Unknown or already-used token"
        if entry.get("used"):
            return "Token already used"
        if entry.get("expires_at", 0) < now:
            _confirm_tokens.pop(token, None)
            return "Token expired (60s window)"
        # User binding — token issued to one user_id must be redeemed
        # by the same. Anonymous mints (user_id=None) bind to anyone
        # with the same purpose, which is fine since the auth gate
        # ahead of us already enforces admin role.
        token_user = entry.get("user_id")
        if token_user is not None and user_id is not None and token_user != user_id:
            return "Token issued to a different user"
        if entry.get("purpose_hash") != purpose_hash:
            return "Order details do not match the minted token's purpose"
        # Consume — mark used + drop the entry (single-use).
        _confirm_tokens.pop(token, None)
    return None


def _slugify(s: str) -> str:
    """Lower-kebab-case, ascii-safe. Used for auto-generating an Agent
    slug from a thread + name pair when the LLM doesn't supply one."""
    out = []
    prev_dash = True
    for ch in (s or "").lower():
        if ch.isalnum():
            out.append(ch)
            prev_dash = False
        elif not prev_dash:
            out.append("-")
            prev_dash = True
    return "".join(out).strip("-") or "draft"


async def _unique_slug(session, base: str) -> str:
    """Append -2, -3, … until the slug is unique in the agents table."""
    candidate = base
    n = 2
    while True:
        existing = await session.execute(select(Agent).where(Agent.slug == candidate))
        if existing.scalar_one_or_none() is None:
            return candidate
        candidate = f"{base}-{n}"
        n += 1


def _user_id(connection) -> int | None:
    payload = getattr(connection.state, "token_payload", {}) or {}
    sub = payload.get("sub")
    try:
        return int(sub) if sub is not None else None
    except (TypeError, ValueError):
        return None


# ── Controller ────────────────────────────────────────────────────────

class ResearchController(Controller):
    path = "/api/research"
    # Per-route caps. Reads (threads + drafts) use `view_lab` which
    # includes demo so the showcase tour's Lab step can populate a
    # real page (threads + drafts list, which carry operator research
    # notes — not sensitive orders). The audit-tab endpoint
    # tightens to `view_audit` (designated/risk/admin only; demo excluded).
    # Mutations use `manage_lab_threads` (designated/trader). MCP write
    # actions tighten to `use_mcp_tools` (designated/trader) — already
    # gated by confirm-token besides the cap check.

    @get("/threads", guards=[cap_guard("view_lab")])
    async def list_threads(self, symbol: str | None = None, limit: int = 100) -> list[ThreadSummary]:
        async with async_session() as s:
            q = select(ResearchThread).order_by(ResearchThread.updated_at.desc())
            if symbol:
                q = q.where(ResearchThread.symbol == symbol.upper())
            q = q.limit(max(1, min(500, limit)))
            rows = (await s.execute(q)).scalars().all()
        return [_to_summary(r) for r in rows]

    @get("/threads/{thread_id:int}", guards=[cap_guard("view_lab")])
    async def get_thread(self, thread_id: int) -> ThreadInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(ResearchThread).where(ResearchThread.id == thread_id)
            )).scalar_one_or_none()
        if not row:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
        return _to_info(row)

    @post("/threads", guards=[cap_guard("manage_lab_threads")])
    async def create_thread(self, data: ThreadCreate, request: Request) -> ThreadInfo:
        sym = (data.symbol or "").upper().strip()
        if not sym:
            raise HTTPException(status_code=400, detail="symbol is required")
        if data.confidence not in _VALID_CONF:
            raise HTTPException(status_code=400,
                                detail=f"confidence must be one of {sorted(_VALID_CONF)}")
        # Auto-title via Gemini Flash free tier when title is blank.
        # Defensive: the helper falls back to a deterministic stub if
        # genai is disabled / SDK missing / quota exhausted / parse
        # fails, so this never blocks thread creation.
        title = (data.title or "").strip()
        if not title:
            try:
                from backend.shared.helpers.genai_helpers import auto_title
                title = auto_title(sym, data.thesis_text)
            except Exception as e:
                logger.warning(f"auto_title raised (using stub): {e}")
                title = f"{sym} research"
        async with async_session() as s:
            row = ResearchThread(
                symbol=sym,
                title=title[:256],
                thesis_text=data.thesis_text,
                confidence=data.confidence,
                transcript=data.transcript or [],
                created_by_user_id=_user_id(request),
            )
            s.add(row)
            await s.commit()
            await s.refresh(row)
        logger.info(f"research thread created: id={row.id} sym={sym}")
        return _to_info(row)

    @patch("/threads/{thread_id:int}", guards=[cap_guard("manage_lab_threads")])
    async def update_thread(self, thread_id: int, data: ThreadUpdate) -> ThreadInfo:
        async with async_session() as s:
            row = (await s.execute(
                select(ResearchThread).where(ResearchThread.id == thread_id)
            )).scalar_one_or_none()
            if not row:
                raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
            if data.title is not None:
                row.title = data.title[:256]
            if data.thesis_text is not None:
                row.thesis_text = data.thesis_text
            if data.confidence is not None:
                if data.confidence not in _VALID_CONF:
                    raise HTTPException(status_code=400,
                                        detail=f"confidence must be one of {sorted(_VALID_CONF)}")
                row.confidence = data.confidence
            if data.transcript is not None:
                row.transcript = data.transcript
            if data.draft_agent_id is not None:
                row.draft_agent_id = data.draft_agent_id
            row.updated_at = datetime.utcnow()
            await s.commit()
            await s.refresh(row)
        return _to_info(row)

    @delete("/threads/{thread_id:int}", status_code=204, guards=[cap_guard("manage_lab_threads")])
    async def delete_thread(self, thread_id: int) -> None:
        async with async_session() as s:
            result = await s.execute(
                sa_delete(ResearchThread).where(ResearchThread.id == thread_id)
            )
            await s.commit()
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

    @post("/threads/{thread_id:int}/promote", guards=[cap_guard("manage_lab_threads")])
    async def promote_thread(self, thread_id: int, data: PromoteRequest) -> DraftInfo:
        """Promote a research thread into an inactive draft Agent.

        Creates a new agent row with status=inactive, sets
        thread.draft_agent_id = agent.id, returns the joined view. If
        the thread already has a draft_agent_id, returns 409 — operator
        must un-link first to avoid silently overwriting.

        Industry-standard safety: the agent ships INACTIVE and PAPER
        (trade_mode=paper) regardless of what the caller asks. Operator
        must explicitly flip status + trade_mode on /agents — this
        endpoint cannot create an active or live agent.
        """
        # Validate inputs first.
        if not data.name or not data.name.strip():
            raise HTTPException(status_code=400, detail="name is required")
        if data.scope not in _VALID_SCOPE:
            raise HTTPException(status_code=400,
                                detail=f"scope must be one of {sorted(_VALID_SCOPE)}")
        if not isinstance(data.conditions, dict) or not data.conditions:
            raise HTTPException(status_code=400, detail="conditions must be a non-empty dict")
        # Optional grammar dry-check — surface a precise error if the
        # condition tree references unknown tokens. Keeps the operator
        # from landing a broken draft in the Drafts tab.
        try:
            from backend.api.algo.agent_evaluator import validate as validate_condition
            errors = validate_condition(data.conditions)
            if errors:
                raise HTTPException(status_code=400,
                                    detail=f"condition validation: {'; '.join(errors)}")
        except HTTPException:
            raise
        except Exception as e:
            # Grammar registry not loaded yet — log + continue (the
            # /agents page's own validator will catch it before activate).
            logger.warning(f"promote: skipped grammar validation: {e}")

        async with async_session() as s:
            thread = (await s.execute(
                select(ResearchThread).where(ResearchThread.id == thread_id)
            )).scalar_one_or_none()
            if not thread:
                raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")
            if thread.draft_agent_id:
                raise HTTPException(status_code=409,
                    detail=f"Thread already promoted to agent #{thread.draft_agent_id}")

            base = _slugify(f"{thread.symbol}-{data.name}")[:48]
            slug = await _unique_slug(s, base)

            # Phase 19 — validate lifespan params before constructing
            # the Agent row. The engine enforces the terminal state in
            # run_cycle; the route enforces well-formed inputs.
            lifespan_type = (data.lifespan_type or "persistent").lower().strip()
            if lifespan_type not in ("persistent", "one_shot", "n_fires", "until_date"):
                raise HTTPException(status_code=400,
                    detail="lifespan_type must be one of persistent, one_shot, n_fires, until_date")
            lifespan_max_fires = data.lifespan_max_fires
            if lifespan_type == "n_fires":
                if not lifespan_max_fires or int(lifespan_max_fires) < 1:
                    raise HTTPException(status_code=400,
                        detail="lifespan_max_fires (≥ 1) is required when lifespan_type='n_fires'")
            elif lifespan_type != "n_fires":
                # Clear max_fires on non-n_fires types so the column never carries stale data.
                lifespan_max_fires = None
            lifespan_expires_at = None
            if lifespan_type == "until_date":
                if not data.lifespan_expires_at:
                    raise HTTPException(status_code=400,
                        detail="lifespan_expires_at (ISO datetime) is required when lifespan_type='until_date'")
                try:
                    lifespan_expires_at = datetime.fromisoformat(data.lifespan_expires_at)
                except (TypeError, ValueError):
                    raise HTTPException(status_code=400,
                        detail=f"lifespan_expires_at must be ISO format, got {data.lifespan_expires_at!r}")

            agent = Agent(
                slug=slug,
                name=data.name.strip()[:128],
                description=data.description.strip()[:1024] if data.description else
                            f"Promoted from research thread #{thread.id} ({thread.symbol})",
                conditions=data.conditions,
                events=data.events,
                actions=data.actions,
                scope=data.scope,
                schedule=data.schedule,
                cooldown_minutes=max(1, int(data.cooldown_minutes or 30)),
                debounce_minutes=max(0, int(data.debounce_minutes or 0)),
                status="inactive",
                trade_mode="paper",
                lifespan_type=lifespan_type,
                lifespan_max_fires=lifespan_max_fires,
                lifespan_expires_at=lifespan_expires_at,
                tags=list(data.tags or []),
                blackout_windows=list(data.blackout_windows or []),
            )
            s.add(agent)
            await s.flush()           # populate agent.id
            thread.draft_agent_id = agent.id
            await s.commit()
            await s.refresh(thread)
            await s.refresh(agent)

        logger.info(
            f"research thread #{thread.id} promoted → agent #{agent.id} "
            f"slug={slug!r} (status=inactive, trade_mode=paper)"
        )
        return DraftInfo(
            thread_id=thread.id,
            symbol=thread.symbol,
            title=thread.title or "",
            confidence=thread.confidence or "unsure",
            thesis_text=thread.thesis_text,
            agent_id=agent.id,
            agent_slug=agent.slug,
            agent_name=agent.name,
            agent_status=agent.status,
            agent_scope=agent.scope,
            agent_schedule=agent.schedule,
            agent_cooldown=int(agent.cooldown_minutes or 0),
            agent_trade_mode=agent.trade_mode,
            created_at=thread.created_at.isoformat() if thread.created_at else "",
            updated_at=thread.updated_at.isoformat() if thread.updated_at else "",
        )

    @get("/drafts", guards=[cap_guard("view_lab")])
    async def list_drafts(self, limit: int = 200) -> list[DraftInfo]:
        """Threads with a linked draft Agent that's still inactive.

        Activating an agent on /agents naturally graduates it out of
        this list — no manual cleanup needed."""
        async with async_session() as s:
            rows = (await s.execute(
                select(ResearchThread, Agent)
                .join(Agent, Agent.id == ResearchThread.draft_agent_id)
                .where(ResearchThread.draft_agent_id.is_not(None))
                .where(Agent.status == "inactive")
                .order_by(ResearchThread.updated_at.desc())
                .limit(max(1, min(500, limit)))
            )).all()
        return [
            DraftInfo(
                thread_id=t.id, symbol=t.symbol, title=t.title or "",
                confidence=t.confidence or "unsure",
                thesis_text=t.thesis_text,
                agent_id=a.id, agent_slug=a.slug, agent_name=a.name,
                agent_status=a.status, agent_scope=a.scope,
                agent_schedule=a.schedule,
                agent_cooldown=int(a.cooldown_minutes or 0),
                agent_trade_mode=a.trade_mode,
                created_at=t.created_at.isoformat() if t.created_at else "",
                updated_at=t.updated_at.isoformat() if t.updated_at else "",
            )
            for (t, a) in rows
        ]

    @get("/audit", guards=[cap_guard("view_audit")])
    async def list_audit(
        self,
        tool:       str | None = None,
        status:     str | None = None,
        since:      str | None = None,
        request_id: str | None = None,
        limit:      int = 200,
    ) -> list[AuditRow]:
        """Forensic trail for the Lab page's Audit tab. Returns
        mcp_audit rows in reverse-chronological order. Args are
        already redacted (token material is never persisted), so
        every admin can see this without leaking authorisation state.

        Args:
            tool:       Optional filter — e.g. 'place_order'.
            status:     Optional filter — 'ok' / 'denied' / 'error'.
            since:      Optional ISO datetime — only return rows with
                        created_at >= since. Bad input silently ignored.
            request_id: Optional exact-match — used by the Telegram
                        deep-link to surface ONE specific row.
            limit:      Max rows (default 200, max 1000).
        """
        async with async_session() as s:
            q = select(McpAudit).order_by(McpAudit.created_at.desc())
            if tool:
                q = q.where(McpAudit.tool == tool.strip())
            if status:
                q = q.where(McpAudit.result_status == status.strip().lower())
            if request_id:
                q = q.where(McpAudit.request_id == request_id.strip())
            if since:
                try:
                    since_dt = datetime.fromisoformat(since)
                    q = q.where(McpAudit.created_at >= since_dt)
                except (TypeError, ValueError):
                    logger.debug(f"audit: ignoring bad `since` value: {since!r}")
            q = q.limit(max(1, min(1000, int(limit or 200))))
            rows = (await s.execute(q)).scalars().all()
        return [
            AuditRow(
                id=r.id, tool=r.tool, user_id=r.user_id,
                args_redacted=r.args_redacted or {},
                result_status=r.result_status,
                result_summary=r.result_summary or "",
                request_id=r.request_id,
                created_at=r.created_at.isoformat() if r.created_at else "",
            )
            for r in rows
        ]

    # ── Phase 3 — confirm-token mint + gated place_order ──────────────

    @post("/confirm-token", guards=[cap_guard("manage_lab_threads")])
    async def mint_confirm_token(self, data: MintTokenRequest, request: Request) -> MintTokenResponse:
        """Operator-only — mint a single-use 60s token that authorises
        ONE specific MCP place_order call. The LLM cannot call this
        endpoint via MCP (no MCP tool wraps it); only the Lab page UI
        does. The operator copies the returned token + pastes into
        Claude Code, which then passes it to place_order along with
        the IDENTICAL order details. The purpose hash binds the token
        to the exact order so it can't be swapped for a different
        symbol / side / qty.

        Industry analog: Composer.trade's "Deploy" click + IBKR
        TraderGPT's per-trade confirm. The token-based variant scales
        cleanly to multi-step LLM chains (LLM calls place_order
        without needing a UI round-trip mid-conversation, as long as
        the operator pre-minted the token)."""
        kind = (data.kind or "place").lower().strip()
        acct = (data.account or "").strip()
        if kind not in ("place", "cancel", "modify", "activate", "deactivate", "update"):
            raise HTTPException(status_code=400,
                detail="kind must be place, cancel, modify, activate, deactivate, or update")
        # Agent kinds don't need an account.
        if kind in ("place", "cancel", "modify") and not acct:
            raise HTTPException(status_code=400, detail="account is required")

        order_type = (data.order_type or "LIMIT").upper().strip()
        qty = int(data.quantity or 0)
        oid = (data.order_id or "").strip()
        agent_slug = (data.agent_slug or "").strip()

        if kind == "place":
            side = (data.side or "").upper().strip()
            mode = (data.mode or "paper").lower().strip()
            sym = (data.tradingsymbol or "").upper().strip()
            if side not in ("BUY", "SELL"):
                raise HTTPException(status_code=400, detail="side must be BUY or SELL")
            if mode not in ("paper", "live"):
                raise HTTPException(status_code=400, detail="mode must be paper or live")
            if not sym or qty <= 0:
                raise HTTPException(status_code=400,
                    detail="tradingsymbol and quantity > 0 are required for place")
            ph = _purpose_hash_place(acct, sym, side, qty, order_type, mode,
                                     data.price, data.trigger_price)
            price_chunk = (
                f" @₹{data.price:g}" if data.price else
                (f" trig=₹{data.trigger_price:g}" if data.trigger_price else "")
            )
            purpose = f"PLACE [{mode.upper()}] · {side} {qty} {sym}{price_chunk} · acct={acct}"
        elif kind == "cancel":
            if not oid:
                raise HTTPException(status_code=400, detail="order_id is required for cancel")
            mode_cm = (data.mode or "live").lower().strip()
            if mode_cm not in ("paper", "live"):
                raise HTTPException(status_code=400, detail="mode must be paper or live")
            ph = _purpose_hash_cancel(acct, oid, mode_cm)
            purpose = f"CANCEL [{mode_cm.upper()}] · order_id={oid} · acct={acct}"
        elif kind == "modify":
            if not oid:
                raise HTTPException(status_code=400, detail="order_id is required for modify")
            mode_cm = (data.mode or "live").lower().strip()
            if mode_cm not in ("paper", "live"):
                raise HTTPException(status_code=400, detail="mode must be paper or live")
            ph = _purpose_hash_modify(acct, oid, qty, order_type, data.price, data.trigger_price, mode_cm)
            mod_chunk = " ".join(filter(None, [
                f"qty={qty}" if qty else None,
                f"type={order_type}" if order_type else None,
                f"@₹{data.price:g}" if data.price else None,
                f"trig=₹{data.trigger_price:g}" if data.trigger_price else None,
            ])) or "no changes"
            purpose = f"MODIFY [{mode_cm.upper()}] · order_id={oid} · {mod_chunk} · acct={acct}"
        elif kind in ("activate", "deactivate"):
            if not agent_slug:
                raise HTTPException(status_code=400,
                    detail=f"agent_slug is required for {kind}")
            ph = _purpose_hash_agent_status(kind, agent_slug)
            purpose = f"{kind.upper()} · agent={agent_slug}"
        else:
            # update — agent_slug + non-empty whitelisted-changes dict
            if not agent_slug:
                raise HTTPException(status_code=400, detail="agent_slug is required for update")
            filtered = _canonical_changes(data.proposed_changes or {})
            if not filtered:
                raise HTTPException(status_code=400,
                    detail=(
                        "proposed_changes must include at least one of: "
                        + ", ".join(_AGENT_UPDATE_FIELDS)
                    ))
            ph = _purpose_hash_update_agent(agent_slug, filtered)
            # Short, human-readable purpose label — JSON canonical form
            # is too long for a Telegram subject, so just enumerate which
            # fields are changing.
            purpose = f"UPDATE · agent={agent_slug} · fields={','.join(sorted(filtered.keys()))}"

        token, expires = _mint_token(_user_id(request), ph)
        now = int(time.time())
        logger.info(
            f"confirm-token minted: user={_user_id(request)} kind={kind} "
            f"purpose={purpose!r} ttl={_TOKEN_TTL_SECONDS}s"
        )
        return MintTokenResponse(
            token=token,
            expires_at=expires,
            expires_in=max(0, expires - now),
            purpose=purpose,
            purpose_hash=ph,
        )

    @post("/place-order", guards=[cap_guard("use_mcp_tools")])
    async def place_order(self, data: PlaceOrderRequest, request: Request) -> PlaceOrderResponse:
        """Gated order placement — requires a valid confirm token minted
        from /confirm-token. The MCP place_order tool calls this. Every
        call (success or failure) is recorded in the mcp_audit table.

        Mode resolution: dev branch forces paper regardless of `mode`;
        on prod, mode='live' goes live only when execution.paper_trading_mode
        is False (matches the existing /api/orders/ticket gate)."""
        # Compute purpose hash from the LLM's payload first — the
        # validator checks this against the token's stored hash.
        side = (data.side or "").upper().strip()
        mode = (data.mode or "paper").lower().strip()
        sym = (data.tradingsymbol or "").upper().strip()
        acct = (data.account or "").strip()
        order_type = (data.order_type or "LIMIT").upper().strip()
        qty = int(data.quantity or 0)
        ph = _purpose_hash_place(acct, sym, side, qty, order_type, mode,
                                 data.price, data.trigger_price)

        # Redact + persist the call before doing anything risky.
        request_id = _secrets.token_hex(6)
        user_id = _user_id(request)

        async def _audit(status_: str, summary: str) -> None:
            try:
                async with async_session() as s:
                    s.add(McpAudit(
                        tool="place_order",
                        user_id=user_id,
                        args_redacted={
                            "account":       acct,
                            "tradingsymbol": sym,
                            "side":          side,
                            "quantity":      qty,
                            "mode":          mode,
                            "order_type":    order_type,
                            "price":         data.price,
                            "trigger_price": data.trigger_price,
                            # token is NEVER persisted — only its presence is logged
                            "had_token":     bool(data.confirm_token),
                        },
                        result_status=status_,
                        result_summary=summary[:1024],
                        request_id=request_id,
                    ))
                    await s.commit()
            except Exception as e:
                logger.warning(f"mcp_audit insert failed: {e}")

        # Token gate.
        err = _consume_token(data.confirm_token or "", user_id, ph)
        if err:
            await _audit("denied", err)
            raise HTTPException(status_code=403, detail=f"Confirm token: {err}")

        # Forward to the existing ticket pipeline. Reusing the route
        # function keeps the validation / mode resolution / paper-engine
        # registration / Kite call all in one place.
        from backend.api.routes.orders import OrdersController
        from backend.api.schemas import TicketOrderRequest

        ticket = TicketOrderRequest(
            mode=mode, side=side, tradingsymbol=sym, quantity=qty,
            exchange=data.exchange, product=data.product,
            order_type=order_type, variety=data.variety,
            price=data.price, trigger_price=data.trigger_price,
            account=acct, chase=data.chase,
            chase_aggressiveness=data.chase_aggressiveness,
            source="mcp",
        )
        try:
            ctrl = OrdersController(owner=None)
            # `.fn(ctrl, ...)` — bypass the @post route-handler wrapper.
            # The Phase-3 place_order path landed before this bug was
            # spotted; switching to `.fn` keeps the live mode functional.
            res = await OrdersController.ticket_order.fn(
                ctrl, data=ticket, request=request,
            )
        except HTTPException as e:
            await _audit("error", f"{e.status_code}: {e.detail}")
            raise
        except Exception as e:
            await _audit("error", f"unexpected: {e}")
            logger.exception("MCP place_order: ticket pipeline raised")
            raise HTTPException(status_code=500, detail=str(e))

        await _audit(
            "ok",
            f"order_id={res.order_id} mode={res.mode} status={res.status}",
        )
        logger.info(
            f"MCP place_order OK: user={user_id} order_id={res.order_id} "
            f"mode={res.mode} {side} {qty} {sym} acct={acct}"
        )

        # Phase 3c — Telegram ping. Defense in depth: even when the
        # operator is in a meeting / away from the Lab page, every
        # LLM-initiated order pings the alerts channel. Routes through
        # the same `is_enabled('telegram')` + secrets gate the existing
        # alert pipeline uses; silent no-op when telegram is off.
        try:
            from backend.shared.helpers.alert_utils import _send_telegram
            price_chunk = (
                f" @₹{data.price:g}" if data.price else
                (f" trig=₹{data.trigger_price:g}" if data.trigger_price else "")
            )
            mode_pill = f"[{res.mode.upper()}]" if res.mode else "[?]"
            tg_msg = (
                f"<b>MCP {mode_pill}</b> {side} {qty} {sym}{price_chunk}\n"
                f"acct={acct} · order_id=<code>{res.order_id}</code> · "
                f"status={res.status}\n"
                f"<i>request_id={_audit_link_html(request_id)} · user_id={user_id or '-'}</i>"
            )
            _send_telegram(tg_msg)
        except Exception as e:
            logger.warning(f"MCP place_order Telegram ping failed: {e}")
        return PlaceOrderResponse(
            order_id=res.order_id, mode=res.mode,
            status=res.status, detail=res.detail,
        )

    # ── Phase 4 — gated cancel + modify (same token-gate pattern) ─────

    @post("/cancel-order", guards=[cap_guard("use_mcp_tools")])
    async def cancel_order(self, data: CancelOrderRequest, request: Request) -> SimpleOrderResponse:
        """LLM-initiated cancel. Requires a confirm token minted with
        kind='cancel' for THIS (account, order_id, mode). Token cannot
        be redeemed against a different order_id, mode, or account.

        Dispatch:
          - mode='live' (default) → broker cancel via Kite
          - mode='paper'          → PaperTradeEngine.cancel_paper_order
                                     (order_id is the AlgoOrder.id int)
        """
        acct = (data.account or "").strip()
        oid = (data.order_id or "").strip()
        mode = (data.mode or "live").lower().strip()
        if not acct or not oid:
            raise HTTPException(status_code=400, detail="account and order_id are required")
        if mode not in ("paper", "live"):
            raise HTTPException(status_code=400, detail="mode must be paper or live")
        ph = _purpose_hash_cancel(acct, oid, mode)

        request_id = _secrets.token_hex(6)
        user_id = _user_id(request)

        async def _audit(status_: str, summary: str) -> None:
            try:
                async with async_session() as s:
                    s.add(McpAudit(
                        tool="cancel_order",
                        user_id=user_id,
                        args_redacted={
                            "account":  acct,
                            "order_id": oid,
                            "mode":     mode,
                            "variety":  data.variety,
                            "had_token": bool(data.confirm_token),
                        },
                        result_status=status_,
                        result_summary=summary[:1024],
                        request_id=request_id,
                    ))
                    await s.commit()
            except Exception as e:
                logger.warning(f"mcp_audit insert failed: {e}")

        err = _consume_token(data.confirm_token or "", user_id, ph)
        if err:
            await _audit("denied", err)
            raise HTTPException(status_code=403, detail=f"Confirm token: {err}")

        # Dispatch by mode.
        if mode == "paper":
            try:
                algo_order_id = int(oid)
            except (TypeError, ValueError):
                await _audit("error", "paper cancel: order_id must be an integer AlgoOrder.id")
                raise HTTPException(status_code=400,
                    detail="For paper cancel, order_id must be the integer AlgoOrder.id")
            try:
                from backend.api.algo.paper import get_prod_paper_engine
                engine = get_prod_paper_engine()
                ok = engine.cancel_paper_order(algo_order_id)
            except Exception as e:
                await _audit("error", f"paper engine raised: {e}")
                logger.exception("MCP paper cancel raised")
                raise HTTPException(status_code=500, detail=str(e))
            if not ok:
                await _audit("error", f"no OPEN paper order with id={algo_order_id}")
                raise HTTPException(status_code=404,
                    detail=f"No OPEN paper order with AlgoOrder.id={algo_order_id}")
            await _audit("ok", f"paper order_id={algo_order_id} CANCELLED")
            logger.info(f"MCP cancel_order (paper) OK: user={user_id} id={algo_order_id} acct={acct}")
            try:
                from backend.shared.helpers.alert_utils import _send_telegram
                _send_telegram(
                    f"<b>MCP CANCEL [PAPER]</b> AlgoOrder.id=<code>{algo_order_id}</code>\n"
                    f"acct={acct}\n"
                    f"<i>request_id={_audit_link_html(request_id)} · user_id={user_id or '-'}</i>"
                )
            except Exception as e:
                logger.warning(f"MCP cancel_order (paper) Telegram ping failed: {e}")
            return SimpleOrderResponse(order_id=oid, detail="cancelled (paper)")

        # Live mode — forward to existing broker cancel handler. Use
        # `.fn(ctrl, ...)` to bypass the @delete decorator wrapper
        # (calling the decorated method directly returns a route-handler
        # object, not a coroutine — "object delete can't be used in
        # 'await' expression"). Same workaround as activate/deactivate.
        from backend.api.routes.orders import OrdersController
        try:
            ctrl = OrdersController(owner=None)
            res = await OrdersController.cancel_order.fn(
                ctrl,
                order_id=oid, request=request,
                account=acct, variety=data.variety,
            )
        except HTTPException as e:
            await _audit("error", f"{e.status_code}: {e.detail}")
            raise
        except Exception as e:
            await _audit("error", f"unexpected: {e}")
            logger.exception("MCP cancel_order: underlying handler raised")
            raise HTTPException(status_code=500, detail=str(e))

        await _audit("ok", f"order_id={res.order_id}")
        logger.info(f"MCP cancel_order (live) OK: user={user_id} order_id={res.order_id} acct={acct}")

        try:
            from backend.shared.helpers.alert_utils import _send_telegram
            _send_telegram(
                f"<b>MCP CANCEL [LIVE]</b> order_id=<code>{res.order_id}</code>\n"
                f"acct={acct}\n"
                f"<i>request_id={_audit_link_html(request_id)} · user_id={user_id or '-'}</i>"
            )
        except Exception as e:
            logger.warning(f"MCP cancel_order Telegram ping failed: {e}")

        return SimpleOrderResponse(order_id=res.order_id, detail="cancelled (live)")

    @post("/modify-order", guards=[cap_guard("use_mcp_tools")])
    async def modify_order(self, data: ModifyOrderRequest, request: Request) -> SimpleOrderResponse:
        """LLM-initiated modify. Token binds (account, order_id, mode)
        PLUS the new values the LLM plans to push — bait-and-switch on
        the new price / qty after the operator approves is blocked.

        Dispatch:
          - mode='live' (default) → Kite modify_order
          - mode='paper'          → PaperTradeEngine.modify_paper_order
                                     (order_id is the AlgoOrder.id int;
                                     next chase tick uses the new values)
        """
        acct = (data.account or "").strip()
        oid = (data.order_id or "").strip()
        qty = int(data.quantity or 0)
        otype = (data.order_type or "LIMIT").upper().strip()
        mode = (data.mode or "live").lower().strip()
        if not acct or not oid:
            raise HTTPException(status_code=400, detail="account and order_id are required")
        if mode not in ("paper", "live"):
            raise HTTPException(status_code=400, detail="mode must be paper or live")
        ph = _purpose_hash_modify(acct, oid, qty, otype, data.price, data.trigger_price, mode)

        request_id = _secrets.token_hex(6)
        user_id = _user_id(request)

        async def _audit(status_: str, summary: str) -> None:
            try:
                async with async_session() as s:
                    s.add(McpAudit(
                        tool="modify_order",
                        user_id=user_id,
                        args_redacted={
                            "account":       acct,
                            "order_id":      oid,
                            "mode":          mode,
                            "quantity":      qty or None,
                            "order_type":    otype,
                            "price":         data.price,
                            "trigger_price": data.trigger_price,
                            "variety":       data.variety,
                            "validity":      data.validity,
                            "had_token":     bool(data.confirm_token),
                        },
                        result_status=status_,
                        result_summary=summary[:1024],
                        request_id=request_id,
                    ))
                    await s.commit()
            except Exception as e:
                logger.warning(f"mcp_audit insert failed: {e}")

        err = _consume_token(data.confirm_token or "", user_id, ph)
        if err:
            await _audit("denied", err)
            raise HTTPException(status_code=403, detail=f"Confirm token: {err}")

        chunks = " ".join(filter(None, [
            f"qty={qty}" if qty else None,
            f"type={otype}" if otype else None,
            f"@₹{data.price:g}" if data.price else None,
            f"trig=₹{data.trigger_price:g}" if data.trigger_price else None,
        ])) or "(no-op)"

        # Dispatch by mode.
        if mode == "paper":
            try:
                algo_order_id = int(oid)
            except (TypeError, ValueError):
                await _audit("error", "paper modify: order_id must be an integer AlgoOrder.id")
                raise HTTPException(status_code=400,
                    detail="For paper modify, order_id must be the integer AlgoOrder.id")
            try:
                from backend.api.algo.paper import get_prod_paper_engine
                engine = get_prod_paper_engine()
                ok = engine.modify_paper_order(
                    algo_order_id,
                    new_qty=qty if qty else None,
                    new_price=data.price,
                    new_trigger=data.trigger_price,
                    new_order_type=otype if otype != "LIMIT" else None,
                )
            except Exception as e:
                await _audit("error", f"paper engine raised: {e}")
                logger.exception("MCP paper modify raised")
                raise HTTPException(status_code=500, detail=str(e))
            if not ok:
                await _audit("error", f"no OPEN paper order matching id={algo_order_id} or no fields changed")
                raise HTTPException(status_code=404,
                    detail=f"No OPEN paper order with AlgoOrder.id={algo_order_id} (or no fields changed)")
            await _audit("ok", f"paper order_id={algo_order_id} modified")
            logger.info(f"MCP modify_order (paper) OK: user={user_id} id={algo_order_id} acct={acct}")
            try:
                from backend.shared.helpers.alert_utils import _send_telegram
                _send_telegram(
                    f"<b>MCP MODIFY [PAPER]</b> AlgoOrder.id=<code>{algo_order_id}</code>\n"
                    f"acct={acct} · {chunks}\n"
                    f"<i>request_id={_audit_link_html(request_id)} · user_id={user_id or '-'}</i>"
                )
            except Exception as e:
                logger.warning(f"MCP modify_order (paper) Telegram ping failed: {e}")
            return SimpleOrderResponse(order_id=oid, detail="modified (paper)")

        # Live mode — forward to existing broker modify handler.
        from backend.api.routes.orders import OrdersController
        from backend.api.schemas import ModifyOrderRequest as TicketModifyRequest

        modify_req = TicketModifyRequest(
            account=acct,
            quantity=qty if qty else None,
            price=data.price,
            order_type=otype,
            trigger_price=data.trigger_price,
            validity=data.validity,
            variety=data.variety,
        )
        try:
            ctrl = OrdersController(owner=None)
            # `.fn(ctrl, ...)` — bypass the @put route-handler wrapper.
            res = await OrdersController.modify_order.fn(
                ctrl, order_id=oid, data=modify_req, request=request,
            )
        except HTTPException as e:
            await _audit("error", f"{e.status_code}: {e.detail}")
            raise
        except Exception as e:
            await _audit("error", f"unexpected: {e}")
            logger.exception("MCP modify_order: underlying handler raised")
            raise HTTPException(status_code=500, detail=str(e))

        await _audit("ok", f"order_id={res.order_id}")
        logger.info(f"MCP modify_order (live) OK: user={user_id} order_id={res.order_id} acct={acct}")

        try:
            from backend.shared.helpers.alert_utils import _send_telegram
            _send_telegram(
                f"<b>MCP MODIFY [LIVE]</b> order_id=<code>{res.order_id}</code>\n"
                f"acct={acct} · {chunks}\n"
                f"<i>request_id={_audit_link_html(request_id)} · user_id={user_id or '-'}</i>"
            )
        except Exception as e:
            logger.warning(f"MCP modify_order Telegram ping failed: {e}")

        return SimpleOrderResponse(order_id=res.order_id, detail="modified (live)")

    # ── Phase 12 — gated activate / deactivate ────────────────────────

    async def _agent_status_change(
        self, data: AgentStatusRequest, request: Request,
        *, action: str,
    ) -> AgentStatusResponse:
        """Shared body for both activate + deactivate routes — only the
        action verb differs. Token gate, audit row, Telegram ping all
        share the same shape."""
        assert action in ("activate", "deactivate")
        slug = (data.agent_slug or "").strip()
        if not slug:
            raise HTTPException(status_code=400, detail="agent_slug is required")
        ph = _purpose_hash_agent_status(action, slug)

        request_id = _secrets.token_hex(6)
        user_id = _user_id(request)

        async def _audit(status_: str, summary: str) -> None:
            try:
                async with async_session() as s:
                    s.add(McpAudit(
                        tool=f"{action}_agent",
                        user_id=user_id,
                        args_redacted={
                            "agent_slug": slug,
                            "had_token":  bool(data.confirm_token),
                        },
                        result_status=status_,
                        result_summary=summary[:1024],
                        request_id=request_id,
                    ))
                    await s.commit()
            except Exception as e:
                logger.warning(f"mcp_audit insert failed: {e}")

        err = _consume_token(data.confirm_token or "", user_id, ph)
        if err:
            await _audit("denied", err)
            raise HTTPException(status_code=403, detail=f"Confirm token: {err}")

        # Inline the agent status flip — calling AgentController's
        # @put-decorated methods directly fails ("object put can't be
        # used in 'await' expression") because Litestar wraps decorated
        # methods in route-handler objects. The flip is 3 lines, so
        # just do it here.
        new_status = "active" if action == "activate" else "inactive"
        try:
            async with async_session() as s:
                agent = (await s.execute(
                    select(Agent).where(Agent.slug == slug)
                )).scalar_one_or_none()
                if not agent:
                    raise HTTPException(status_code=404,
                                        detail=f"Agent '{slug}' not found")
                agent.status = new_status
                await s.commit()
        except HTTPException as e:
            await _audit("error", f"{e.status_code}: {e.detail}")
            raise
        except Exception as e:
            await _audit("error", f"unexpected: {e}")
            logger.exception(f"MCP {action}_agent: status flip failed")
            raise HTTPException(status_code=500, detail=str(e))

        await _audit("ok", f"agent={slug} → {new_status}")
        logger.info(
            f"MCP {action}_agent OK: user={user_id} agent={slug} "
            f"new_status={new_status}"
        )

        try:
            from backend.shared.helpers.alert_utils import _send_telegram
            verb = "ACTIVATE" if action == "activate" else "DEACTIVATE"
            _send_telegram(
                f"<b>MCP {verb}</b> agent=<code>{slug}</code> → status={new_status}\n"
                f"<i>request_id={_audit_link_html(request_id)} · user_id={user_id or '-'}</i>"
            )
        except Exception as e:
            logger.warning(f"MCP {action}_agent Telegram ping failed: {e}")

        return AgentStatusResponse(
            agent_slug=slug, status=new_status,
            detail=f"agent {slug!r} {action}d",
        )

    @post("/activate-agent", guards=[cap_guard("use_mcp_tools")])
    async def activate_agent(self, data: AgentStatusRequest, request: Request) -> AgentStatusResponse:
        """LLM-initiated agent activation. Requires a confirm token
        minted with kind='activate' for THIS specific agent_slug. The
        token cannot be redeemed against:
          - a different agent_slug (purpose hash binds it)
          - a deactivate request (the action verb is part of the hash)
          - twice (single-use)
        Activation is the highest-stakes write the LLM can request
        because it lets the agent fire automatically on subsequent
        ticks; the token gate makes it impossible for an LLM to flip
        an agent on without explicit per-call operator approval."""
        return await self._agent_status_change(data, request, action="activate")

    @post("/deactivate-agent", guards=[cap_guard("use_mcp_tools")])
    async def deactivate_agent(self, data: AgentStatusRequest, request: Request) -> AgentStatusResponse:
        """LLM-initiated agent deactivation. Same gate as activate.
        Lower-stakes (turns off automatic firing) but still requires
        a separate token kind so a deactivate token can't be reused
        to activate, and vice versa."""
        return await self._agent_status_change(data, request, action="deactivate")

    @post("/update-agent", guards=[cap_guard("use_mcp_tools")])
    async def update_agent(self, data: AgentUpdateRequest, request: Request) -> AgentStatusResponse:
        """LLM-initiated agent edit. Requires a confirm token minted
        with kind='update' for THIS agent_slug + THIS exact
        proposed-changes JSON. The whole canonical-JSON of changes is
        part of the purpose hash — so the LLM cannot tweak any field
        between mint and update.

        Only whitelisted fields (conditions, events, actions, scope,
        schedule, cooldown_minutes, fire_at_time, description) are
        honoured. status / trade_mode / lifespan_* are silently
        dropped — the LLM cannot flip an agent active or live via
        update_agent (that's what activate_agent's separate gate is
        for, and it never opens for trade_mode).

        For inactive drafts, delete + re-promote is still fine — this
        endpoint exists mainly for ACTIVE agents where deactivating
        first would miss alerts during the window.
        """
        slug = (data.agent_slug or "").strip()
        if not slug:
            raise HTTPException(status_code=400, detail="agent_slug is required")
        filtered = _canonical_changes(data.proposed_changes or {})
        if not filtered:
            raise HTTPException(status_code=400,
                detail=(
                    "proposed_changes must include at least one of: "
                    + ", ".join(_AGENT_UPDATE_FIELDS)
                ))
        ph = _purpose_hash_update_agent(slug, filtered)

        request_id = _secrets.token_hex(6)
        user_id = _user_id(request)

        async def _audit(status_: str, summary: str) -> None:
            try:
                async with async_session() as s:
                    s.add(McpAudit(
                        tool="update_agent",
                        user_id=user_id,
                        args_redacted={
                            "agent_slug":     slug,
                            "changed_fields": sorted(filtered.keys()),
                            "had_token":      bool(data.confirm_token),
                        },
                        result_status=status_,
                        result_summary=summary[:1024],
                        request_id=request_id,
                    ))
                    await s.commit()
            except Exception as e:
                logger.warning(f"mcp_audit insert failed: {e}")

        err = _consume_token(data.confirm_token or "", user_id, ph)
        if err:
            await _audit("denied", err)
            raise HTTPException(status_code=403, detail=f"Confirm token: {err}")

        # Apply the whitelisted fields inline (same pattern as
        # activate/deactivate — avoid the @put decorator wrapper).
        try:
            async with async_session() as s:
                agent = (await s.execute(
                    select(Agent).where(Agent.slug == slug)
                )).scalar_one_or_none()
                if not agent:
                    raise HTTPException(status_code=404,
                                        detail=f"Agent '{slug}' not found")
                for key, val in filtered.items():
                    setattr(agent, key, val)
                await s.commit()
                await s.refresh(agent)
                current_status = agent.status
        except HTTPException as e:
            await _audit("error", f"{e.status_code}: {e.detail}")
            raise
        except Exception as e:
            await _audit("error", f"unexpected: {e}")
            logger.exception("MCP update_agent: write failed")
            raise HTTPException(status_code=500, detail=str(e))

        changed_list = ", ".join(sorted(filtered.keys()))
        await _audit("ok", f"agent={slug} fields={changed_list}")
        logger.info(
            f"MCP update_agent OK: user={user_id} agent={slug} "
            f"fields=[{changed_list}]"
        )

        try:
            from backend.shared.helpers.alert_utils import _send_telegram
            _send_telegram(
                f"<b>MCP UPDATE</b> agent=<code>{slug}</code>\n"
                f"changed: {changed_list}\n"
                f"<i>request_id={_audit_link_html(request_id)} · user_id={user_id or '-'}</i>"
            )
        except Exception as e:
            logger.warning(f"MCP update_agent Telegram ping failed: {e}")

        return AgentStatusResponse(
            agent_slug=slug, status=current_status,
            detail=f"agent {slug!r} updated: {changed_list}",
        )
