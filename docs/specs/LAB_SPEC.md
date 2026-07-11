# Lab Page and MCP Server Specification

Single source of truth for the research lab interface (`/admin/research`) and the
MCP (Model Context Protocol) server that powers Claude Code-driven market research.
Operators chat with Claude, leverage 25 read/write/gated tools, and promote research
threads into draft trading agents.

**Version**: 1.0 — 2026-07-11  
**Owner**: Platform  
**Linked files**: `backend/mcp/kite_server.py` · `backend/api/routes/research.py` · 
`backend/api/models.py` (ResearchThread, McpAudit) · 
`frontend/src/routes/(algo)/admin/research/+page.svelte`

---

## Contents

1. [Lab Page UI](#1-lab-page-ui)
2. [MCP Server Architecture](#2-mcp-server-architecture)
3. [Tool Categories](#3-tool-categories)
4. [Confirm-Token Gate](#4-confirm-token-gate)
5. [Research Threads](#5-research-threads)
6. [Audit Trail](#6-audit-trail)
7. [Edge Cases](#7-edge-cases)
8. [Test Coverage Map](#8-test-coverage-map)

---

## 1. Lab Page UI

**`/admin/research`** — Research thread management interface.

**Left panel: Thread list**
- Create thread button (New Research)
- Search / filter threads by symbol + title
- Thread previews: title · symbol · confidence (bull/bear/neutral) · transcript_len · created_at
- Sorted by created_at descending (most recent first)
- Click to open thread in right panel

**Right panel: Thread detail**
- Transcript (chat history with Claude Code)
- Thesis text (operator summary of findings)
- Confidence level (bull / bear / neutral / unsure)
- Draft Agent field (if thread promoted to agent)
- Promote to Agent button (saves thread as draft Agent, status=inactive)

**Transcript render**:
- System messages: gray background (Claude instructions)
- Claude messages: white background (analysis, tool results, recommendations)
- Operator messages: blue background (questions, corrections, "run this tool")
- Tool call blocks: monospace font, input + output visible
- Tool results: collapsed by default (click to expand)

**Promotion flow**:
1. Operator writes thesis + sets confidence level
2. Clicks "Promote to Agent"
3. Backend creates a new Agent row (status=inactive, condition from thesis, etc.)
4. Thread updated with draft_agent_id
5. Agent then editable in `/automation` page (rules, alerts, notify channels)

---

## 2. MCP Server Architecture

**Process model**: Standalone FastMCP subprocess launched by Claude Code via `.mcp.json`.
Environment-based configuration (no hardcoded URLs/secrets).

**Environment variables**:

| Var | Default | Description |
|---|---|---|
| RAMBOQ_BASE | https://dev.ramboq.com | API base URL |
| RAMBOQ_TOKEN | (required) | JWT from POST /api/auth/login |

**HTTP client**: httpx.AsyncClient with 30s timeout (10s connect, 20s read).

**Base headers**: `Accept: application/json` + `Authorization: Bearer {RAMBOQ_TOKEN}`

**Tool transport**: MCP text-based protocol (stdin/stdout). Claude Code sends tool
requests, MCP server responds with results. No webhook callbacks; request-response only.

**Error handling**: `raise_for_status()` on HTTP errors. Tool fails with HTTP status
+ error body in the response (Claude Code displays to operator).

---

## 3. Tool Categories

**25 tools total**: 17 read-only + 2 persist + 6 gated write.

### Read-Only (17 tools) — No confirm token required

| Tool | Returns | Use |
|---|---|---|
| `get_positions` | Current positions (qty, avg, LTP, P&L, account) | Market state snap |
| `get_holdings` | Current holdings (qty, avg, LTP, P&L, account) | Portfolio composition |
| `get_quote` | Live LTP + OHLC + change% (batch, ≤300 symbols) | Price checks |
| `get_ohlcv` | Historical daily bars (date, OHLCV, volume) | Backtest data |
| `get_recent_news` | Indian-market headlines + sentiment tags | Market context |
| `get_option_analytics` | Greeks, payoff, IV, R:R for a single option | Derivatives analysis |
| `list_agents` | Active agents + conditions + actions | Agent inventory |
| `get_option_strategy` | Multi-leg payoff + aggregate Greeks (strategy analytics) | Strategy design |
| `get_broker_status` | Broker health + stale count + failures per account | Connectivity check |
| (9 more read tools) | ... | ... |

### Persist (2 tools) — Transcript + findings saved to DB

| Tool | Input | Effect |
|---|---|---|
| `save_research_thread` | symbol, title, thesis, confidence, transcript | Upsert ResearchThread row |
| `get_research_thread` | thread_id | Retrieve ResearchThread + transcript |

### Gated Write (6 tools) — Require 60s single-use confirm token

| Tool | Input | Effect | Permission |
|---|---|---|---|
| `place_order` | symbol, qty, side, limit_price | Create AlgoOrder (draft) | cap_research_lab + confirm_token |
| `cancel_order` | order_id | Cancel live/draft order | cap_research_lab + confirm_token |
| `modify_order` | order_id, new_qty, new_price | Modify live order | cap_research_lab + confirm_token |
| `activate_agent` | agent_id | Enable inactive agent | cap_research_lab + confirm_token |
| `deactivate_agent` | agent_id | Disable active agent | cap_research_lab + confirm_token |
| `update_agent` | agent_id, condition, notify, actions | Edit agent rule | cap_research_lab + confirm_token |

---

## 4. Confirm-Token Gate

**Single-use token flow** (gates all 6 write tools):

1. Claude Code calls a gated tool (e.g., `place_order`)
2. Backend generates a 60s TTL confirm token
   - Token shape: 64-char hex, 32-byte entropy
   - Bound to purpose: hash(tool_name, operator_user_id)
   - TTL: 60 seconds absolute (no sliding refresh)
3. Token returned in error response (HTTP 202 Pending Confirmation)
   - `{"confirm_token": "abc123...", "expires_at": "2026-07-11T10:15:60Z", "purpose": "place_order"}`
4. Claude Code displays token to operator + waits for consent
5. Operator clicks "Confirm" button in the Lab page modal
6. Lab sends confirm token back to backend (POST /api/research/confirm-action)
7. Backend validates:
   - Token not expired (within 60s)
   - Purpose hash matches (tool_name + user_id)
   - Token not yet used (single-use, delete row on consumption)
8. If valid, backend executes the tool (place_order, cancel, etc.) + returns result
9. If invalid, returns 401 (expired/revoked/mismatch)

**Token storage** (`ConfirmToken` table):
```json
{
  "id": 42,
  "user_id": 1,
  "token": "abc123...",
  "purpose_hash": "sha256(place_order|user_id=1)",
  "expires_at": "2026-07-11T10:15:60Z",
  "consumed_at": null,
  "created_at": "2026-07-11T10:14:00Z"
}
```

On consumption, update `consumed_at` timestamp. On cleanup (hourly task), delete
rows where expires_at < now (no delete on confirmation; token remains for 60s then
auto-cleaned).

---

## 5. Research Threads

**ResearchThread table**:

| Column | Type | Description |
|---|---|---|
| id | INT PK | Auto-increment |
| user_id | INT FK | Operator (User.id) |
| symbol | VARCHAR | Market symbol (e.g. RELIANCE, NIFTY25APRFUT) |
| title | VARCHAR | Research topic (e.g. "Bullish breakout pattern") |
| thesis_text | TEXT | Operator summary of findings (markdown) |
| confidence | ENUM | bull / bear / neutral / unsure |
| transcript | JSONB | List of messages [{role, content, tool_calls, ...}] |
| draft_agent_id | INT FK | Agent.id (if promoted, else null) |
| created_at | TIMESTAMP | UTC |
| updated_at | TIMESTAMP | UTC (on transcript append or edit) |

**Lifecycle**:

1. **Create**: Operator clicks "New Research" → POST /api/research with symbol + title
   - ResearchThread row created with empty transcript
2. **Append transcript**: Claude Code calls save_research_thread after each LLM turn
   - Transcript JSONB appended (not replaced)
   - updated_at refreshed
3. **Edit thesis**: Operator PATCH /api/research/{id} with new thesis + confidence
4. **Promote**: Operator clicks "Promote to Agent"
   - New Agent row created (status=inactive)
   - draft_agent_id set (thread now linked to agent)
   - Agent inherits thesis as description + confidence as metadata

**Soft-delete policy**: Threads never deleted. Operator can "archive" via a
status=archived flag (future feature). All LLM conversations remain forensic record.

---

## 6. Audit Trail

**McpAudit table** — Every tool call logged automatically.

| Column | Type | Description |
|---|---|---|
| id | BIGINT PK | Auto-increment |
| user_id | INT FK | Operator (User.id) |
| tool_name | VARCHAR | Called tool (place_order, get_positions, etc.) |
| tool_input | JSONB | Input arguments (scrubbed of PII) |
| tool_output | JSONB | Response shape (first 1000 chars, truncated) |
| status | VARCHAR | success / error / pending_confirmation |
| error_message | TEXT | If status=error, the exception message |
| confirm_token_id | INT FK | If status=pending_confirmation, the token row |
| executed_at | TIMESTAMP | UTC of actual execution (null if pending) |
| created_at | TIMESTAMP | UTC of call arrival |

**Write path** (`mcp_audit_queue` EventQueue):
- Every tool invocation captured in McpAudit immediately (async batched write)
- on_full="sync" so audit rows never drop even if queue full
- Batch size 500, flush interval 1s (typical 10-50 rows/sec → 1-2 flushes/min)

**Retention**: Configurable via `/admin/settings` (default 90 days, max 365). Daily
cleanup deletes rows older than retention window.

**Permission**: cap_research_lab required to see McpAudit (same cap as lab access).
Operators can query audit to see what Claude Code did + which tools confirmed vs rejected.

---

## 7. Edge Cases

### Expired confirm token (60s passed, operator still confirming)
- POST /api/research/confirm-action with expired token → 401 Unauthorized
- Backend message: "Token expired. Please run the action again."
- Operator re-runs tool in Claude Code, gets fresh token

### Single-use enforcement (token consumed, operator re-submits same token)
- consumed_at timestamp checked; 401 if already consumed
- Force operator to get fresh token (prevents replay attacks)

### Concurrent tool calls with same token
- Should not happen (token single-use), but if operator somehow submits twice:
  - First request consumes token (sets consumed_at)
  - Second request sees consumed_at ≠ null → 401
  - No double-execute

### MCP subprocess crash (connection lost)
- Claude Code loses the connection to MCP server
- Claude Code displays error to operator ("Connection lost, retrying...")
- Operator can restart the MCP server via `.mcp.json` run command
- Existing McpAudit + ResearchThread rows untouched (DB survives restart)

### Tool call with missing confirm token for gated tool (place_order, etc.)
- Backend detects gated tool + no token in request
- Returns 202 Pending Confirmation with generated token (see §4 flow)
- Operator must confirm via Lab UI

### Malformed tool input (invalid JSON, missing required field)
- Backend validation fails before tool runs
- McpAudit row created with status=error + error_message
- 400 Bad Request returned to Claude Code
- Claude Code sees error + suggests correction

### Confirm token purpose mismatch (token generated for place_order, used for cancel_order)
- purpose_hash check fails (sha256 mismatch)
- 401 Unauthorized returned
- Operator must generate token for the actual tool

---

## 8. Test Coverage Map

### Backend — covered

- `test_mcp_tools_read.py` — get_positions, get_holdings, get_quote return correct shape
- `test_mcp_tools_write_gated.py` — place_order, cancel_order require confirm token
- `test_confirm_token_lifecycle.py` — generate, validate, consume, expire
- `test_research_thread_crud.py` — create, update transcript, promote to agent
- `test_mcp_audit_write.py` — every tool call logged, retention cleanup

### Backend — gaps

- MCP subprocess stdout/stdin protocol (tool request/response framing)
- Tool input scrubbing (PII removal before audit logging)
- Concurrent token consumption (race condition test)
- Audit truncation (tool_output > 1000 chars)

### Frontend — covered

- `lab_page.spec.js` — Thread list, detail panel, transcript render
- `lab_promote_agent.spec.js` — Promote button creates Agent, updates draft_agent_id
- `lab_confirm_token_modal.spec.js` — Confirm button sends token back to backend

### Frontend — gaps

- Transcript auto-scroll (new messages append to bottom)
- Tool result expand/collapse toggle
- Archive thread (soft-delete UI)
- Search / filter threads by symbol + title

---

## Change log

| Date | Change |
|---|---|
| 2026-07-11 | v1.0 initial spec from codebase audit |
