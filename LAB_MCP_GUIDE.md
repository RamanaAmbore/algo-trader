# Lab — chat-driven research with Claude Code + MCP

The Lab is RamboQuant's chat-driven research and agent-building
workspace. You ask Claude Code in plain English ("research RELIANCE
and propose a 1-week trade", "build an agent that closes my NIFTY
puts if delta crosses -0.5"), and the platform turns those
conversations into draft agents and — with explicit per-call
approval — real broker orders.

The chat itself runs inside **Claude Code** (your terminal). The
`/admin/research` page in your browser is the persistence, audit,
and token-mint surface.

**Total incremental cost vs the platform you already pay for: ₹0.**
Your Claude Code subscription covers the LLM. Server-side helpers
(thread auto-title, news sentiment) use the free tier of Gemini 2.5
Flash. Nothing else gets billed.

---

## Table of contents

1. [What you're getting — the 30-second mental model](#1-what-youre-getting--the-30-second-mental-model)
2. [GenAI in this platform](#2-genai-in-this-platform)
3. [One-time setup (~3 minutes)](#3-one-time-setup-3-minutes)
4. [The daily workflow — 7 steps](#4-the-daily-workflow--7-steps)
5. [The 24 MCP tools](#5-the-24-mcp-tools)
6. [The confirm-token gate](#6-the-confirm-token-gate)
7. [Agents — built-in and custom](#7-agents--built-in-and-custom)
8. [Configuration knobs](#8-configuration-knobs)
9. [Troubleshooting](#9-troubleshooting)
10. [Codebase pointers](#10-codebase-pointers)
11. [Phase history](#11-phase-history)

---

## 1. What you're getting — the 30-second mental model

Three actors:

| Actor | What it does | Where it lives |
|---|---|---|
| **Claude Code** (you, on Mac/Linux) | The LLM. Reasons over the platform's data, plans, drafts agents, drafts orders. | Your terminal. Your subscription. |
| **RamboQuant API** | The book of record. Live broker access, agent engine, simulator, audit trail, paper engine. | `dev.ramboq.com` / `ramboq.com`. |
| **MCP server** | A thin bridge — stdio subprocess Claude Code spawns. Forwards tool calls from the LLM to the API. | `backend/mcp/kite_server.py`, lives in your repo, runs locally during your Claude session. |

The MCP server has **24 tools** (16 read, 2 persist, 6 gated write).
Every write needs an operator-minted, single-use, 60-second,
purpose-bound confirm token. No LLM-initiated trade or activation
moves a rupee without that token.

---

## 2. GenAI in this platform

Two LLM tiers are in play. Knowing which one fires where is the
secret to keeping the cost at ₹0.

### Claude Code (your subscription)

The reasoning layer. Everything that runs **in your terminal** when
you chat with Claude — searching its own tool list, picking the
right MCP tools, synthesizing answers, drafting agents. Billed
against your existing Claude Code subscription. No extra setup.

Sonnet 4.6 is the default; Opus 4.7 is available on Max.

### Gemini 2.5 Flash (free tier)

The server-side helper layer. Runs on `dev.ramboq.com` / `ramboq.com`
when:

| Helper | When it fires | What happens if it fails |
|---|---|---|
| **Thread auto-title** | `POST /api/research/threads` with `title=""` | Falls back to first-sentence-of-thesis stub. |
| **News sentiment** | `GET /api/news/?sentiment=true` (the MCP `get_recent_news` tool always passes this) | Falls back to a keyword-regex stub (bull/bear/neutral). |
| **Market summary** | The existing `/api/market` endpoint (predates the Lab) | Falls back to the static YAML report. |

All three helpers go through the same `is_enabled('genai')` gate:

- On the **`main`** branch (`ramboq.com`), genai is **always on** —
  the flag returns True unconditionally.
- On any **non-main** branch (`dev.ramboq.com`), the
  `cap_in_dev.genai` flag in `backend/config/backend_config.yaml`
  decides. **Default is False on dev** — the helpers fall back to
  their deterministic stubs.

Free-tier limits (as of 2026-05) are 10 RPM / 250 RPD / 250k TPM —
far above what these three sparse helpers consume.

### When NEITHER fires

Nothing in this platform calls a paid LLM. There is no OpenAI,
Anthropic API-token, or premium-Gemini integration. Disable
`is_enabled('genai')` everywhere → the platform still works; the
auto-title becomes a first-sentence stub and news sentiment uses
keywords.

### What the LLM in Claude Code can and cannot do

CAN (with no operator approval mid-session):
- Call any of the 16 **read** MCP tools
- Call the 2 **persist** tools (`save_research_thread`,
  `save_agent_draft` — drafts always land inactive + paper-mode)

CANNOT (without a per-call operator-minted confirm token):
- Place an order
- Cancel or modify an order
- Activate or deactivate an agent
- Edit an existing agent's condition tree

The token is the safety property the whole architecture is built
around. See [section 6](#6-the-confirm-token-gate).

---

## 3. One-time setup (~3 minutes)

The Lab page's **Settings** tab walks you through this with
copy-buttons. Open `/admin/research` → **Settings** while reading
the notes below.

### 3.1 Mint a JWT

The MCP server (a subprocess of Claude Code) talks back to the
RamboQuant API the same way every browser session does — over JWT
auth.

**Fastest path** (Phase 16 shortcut):
- You're already signed in on the Lab page. The Settings tab's
  "1. Bootstrap your JWT" card shows your live session token as an
  `export RAMBOQ_TOKEN='<the-jwt>'` line.
- Click **Copy export line**. Paste into the shell where you'll
  launch Claude Code. Done.
- Re-copy after 24 hours when the JWT rolls over.

**For automation** (cron jobs, no browser open):

```bash
export RAMBOQ_TOKEN=$(curl -s -X POST https://dev.ramboq.com/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<your-username>","password":"<your-password>"}' \
  | jq -r .access_token)
```

24 h TTL either way.

### 3.2 Register the MCP server

Copy the `.mcp.json` snippet from the Settings tab into a file named
`.mcp.json` at the repo root (already in `.gitignore`). It looks
like:

```json
{
  "mcpServers": {
    "ramboq-research": {
      "command": "venv/bin/python",
      "args": ["-m", "backend.mcp.kite_server"],
      "env": {
        "RAMBOQ_BASE": "https://dev.ramboq.com",
        "RAMBOQ_TOKEN": "${RAMBOQ_TOKEN}"
      }
    }
  }
}
```

Point `RAMBOQ_BASE` wherever you want the LLM to read from — dev
for experiments, prod (`https://ramboq.com`) for real trades. The
MCP server doesn't care which.

### 3.3 Restart Claude Code

Claude Code reads `.mcp.json` only on startup:

```bash
# In the repo root:
claude
```

The startup banner should show `MCP server "ramboq-research"
connected`.

If it doesn't, the usual fixes:
- `venv/bin/python` missing → `python -m venv venv && venv/bin/pip install -r backend/requirements-api.txt`
- `mcp` package missing → `venv/bin/pip install 'mcp>=1.27'`
- `RAMBOQ_TOKEN` not exported in the shell that launched Claude
  Code → re-run section 3.1, restart Claude Code

### 3.4 Smoke test

In Claude Code, ask:

> *"Call get_server_info."*

You should get back something like:

```json
{
  "base_url": "https://dev.ramboq.com",
  "has_token": true,
  "token_prefix": "eyJhbGciOi…"
}
```

You're wired up.

---

## 4. The daily workflow — 7 steps

The full LLM-driven trading loop. The first four steps are entirely
in Claude Code; the last three involve the Lab page in your
browser.

### 4.1 Research

```
You:    Research RELIANCE. Pull positions, holdings, recent news
        with sentiment, option-chain skew for the nearest expiry,
        the macro snapshot, and 30 days of OHLCV. Give me a
        1-week thesis.

Claude: [calls get_positions, get_holdings, get_recent_news,
         get_options_chain_snapshot, get_economic_snapshot,
         get_ohlcv — 6 read tool calls]

        Based on the data:
        - Long 200 RELIANCE shares @ ₹2,840 avg, ₹568k exposure
        - 6 of 12 headlines bullish, 2 bearish (broker downgrade)
        - Chain at 2900 strike: CE IV 24%, PE IV 21% (call skew)
        - Macros: repo 6.50% (170d stale), CPI 5.2% — neutral rate
        - OHLCV: broke above 200DMA last week, tight range

        Thesis: bullish, 3-5% upside over 1 week. Entry on a
        pullback to 2820, stop at 2790. R:R ~1:2.
```

One prompt → one synthesis. The LLM made 6 tool calls behind the
scenes.

### 4.2 Save the thread

```
You:    Save that thesis as a research thread.

Claude: [calls save_research_thread(symbol="RELIANCE",
         thesis_text=..., confidence="bull", transcript=[...])]

        Saved thread #47.
```

If you don't pass a `title`, the server auto-fills via Gemini Flash
(or the first-sentence stub when genai is off). The thread shows
up immediately on `/admin/research` → **Research** tab.

### 4.3 Draft an agent (optional)

```
You:    Build me an agent: if my RELIANCE position drops 2% in
        any 10-minute window, alert via Telegram. Save the draft.

Claude: [calls list_agents to see existing grammar tokens,
         save_agent_draft(thread_id=47, name="RELIANCE 2%/10m loss",
         conditions={"all": [...]}, events=["telegram"],
         scope="total")]

        Created agent reliance-2pct-10m-loss · status=inactive
        (linked to thread #47).
```

The draft ships `status=inactive` + `trade_mode=paper` — both
hardcoded server-side. The MCP cannot create an active or live
agent.

### 4.4 Review in the simulator

This is the only step that genuinely requires human attention. Go
to `/agents`, find the draft, click **Run in Simulator**. The
existing simulator drives synthetic price moves through the
condition tree so you can see whether the agent fires where you
expect.

Need to tweak? Either:
- Ask Claude Code to delete + redraft (fine for inactive drafts), or
- Use Phase 14's `update_agent` MCP tool (see section 7).

### 4.5 Mint an activate token

On `/admin/research` → **Settings** → **0. Mint a confirm token**:

| Field | Value |
|---|---|
| Kind | `ACTIVATE agent` |
| Agent slug | `reliance-2pct-10m-loss` (copy from /agents) |

Click **Mint token** → you see a 32-char hex string with a 60-second
countdown.

### 4.6 Activate

```
You:    Activate it. Token: 4f8e7d6c5b4a3928…

Claude: [calls activate_agent(confirm_token="4f8e7d6c5b4a3928…",
         agent_slug="reliance-2pct-10m-loss")]

        Agent activated. status=active. Audit row #112. Telegram
        ping sent.
```

The agent now fires automatically on every matching tick during
its `schedule` window.

### 4.7 Wind down (later)

Mint a `DEACTIVATE agent` token, paste it, call `deactivate_agent`.
Same gate, same audit, same Telegram ping.

---

## 5. The 24 MCP tools

### Read (16) — no token required

| Tool | What it returns |
|---|---|
| `get_positions(account?)` | Intraday positions across loaded broker accounts |
| `get_holdings(account?)` | Long-term holdings |
| `get_quote(symbols)` | Live LTP + OHLC + change% (up to 300 symbols) |
| `get_ohlcv(symbol, days, exchange)` | Historical daily candles |
| `get_recent_news(symbol?, sentiment?)` | Indian-market headlines with bull/bear/neutral tags |
| `get_option_analytics(symbol, qty?)` | Greeks + payoff + risk for ONE leg |
| `get_options_chain_snapshot(underlying, expiry, atm_window)` | LTP + Greeks for every strike ATM ± N (Phase 8) |
| `get_economic_snapshot()` | Repo / CPI / IIP / GDP / USD-INR with freshness flags |
| `get_funds_summary(account?)` | Cash + available/used margin per account |
| `get_watchlist(name)` | Symbols in a curated watchlist |
| `get_pnl_attribution(period, mode)` | P&L grouped by agent |
| `list_agents(status?)` | Existing agents with their status |
| `list_research_threads(symbol?)` | Past saved threads (summaries) |
| `get_research_thread(thread_id)` | Full transcript + thesis for one thread |
| `get_audit_recent(tool?, status?)` | Reverse-chrono MCP-action trail (Phase 7 — LLM self-check) |
| `get_server_info()` | Base URL + token presence (diagnostic) |

### Persist (2) — no token required (drafts always land inactive)

| Tool | What it does |
|---|---|
| `save_research_thread(symbol, thesis_text, confidence, transcript, title?)` | Persist a chat session to the Lab page |
| `save_agent_draft(thread_id, name, conditions, ...)` | Promote thread → inactive draft Agent in paper mode |

### Gated write (6) — operator-minted confirm token required

| Tool | Token kind | What it does |
|---|---|---|
| `place_order(...)` | `place` | Submit a new order (paper or live) |
| `cancel_order(...)` | `cancel` | Cancel a working order (live broker OR paper engine) |
| `modify_order(...)` | `modify` | Modify a working order (live OR paper) |
| `activate_agent(...)` | `activate` | Flip an agent inactive → active |
| `deactivate_agent(...)` | `deactivate` | Flip an agent active → inactive |
| `update_agent(...)` | `update` | Edit conditions / cooldown / events / etc. on an existing agent (Phase 14) |

---

## 6. The confirm-token gate

Every gated write requires a token that is:

- **16-byte hex** (32 characters)
- **Single-use** — atomic consume on first valid redemption
- **60-second TTL**
- **Bound to the operator's `user_id`** at mint time — only the same
  user can redeem it
- **Bound to a purpose hash** of the exact action fields, so the
  LLM cannot bait-and-switch between mint and redemption
- **In-memory only** — restart invalidates everything (conservative
  by design; operator just re-mints)

Purpose-hash binding per kind:

| Kind | Hash includes |
|---|---|
| `place` | account · symbol · side · qty · order_type · mode · price · trigger |
| `cancel` | account · order_id · mode |
| `modify` | account · order_id · mode · new qty · new order_type · new price · new trigger |
| `activate` | action verb · agent_slug |
| `deactivate` | action verb · agent_slug |
| `update` | agent_slug · canonical-JSON of whitelisted proposed_changes |

Any mismatch returns `403 — "Order details do not match the minted
token's purpose"`. Replay returns `403 — "Token already used"`.
Expiration returns `403 — "Token expired (60s window)"`.

### Why the action verb is part of the activate/deactivate hash

So a `deactivate` token cannot be redeemed to activate the same
agent — and vice versa. Symmetric protection against
direction-swap.

### Why `update_agent` hashes the canonical JSON of proposed changes

Because update touches many fields. The whole payload is part of
the hash so the LLM cannot tweak any single field after operator
approval. The server also filters to a whitelist of allowed fields
(`conditions`, `events`, `actions`, `scope`, `schedule`,
`cooldown_minutes`, `fire_at_time`, `description`) — `status`,
`trade_mode`, and `lifespan_*` are silently dropped. The LLM
cannot flip an agent active or live via `update_agent`. That's
what `activate_agent` is for, and even there `trade_mode='live'`
needs the prod-side `execution.paper_trading_mode=False` master.

### Mode resolution (unchanged)

The MCP layer adds the token gate on top; it never bypasses the
existing dev/prod/paper rules:

- **dev branch** forces paper regardless of the token's `mode`
- **prod + `execution.paper_trading_mode=True`** forces paper
- **prod + `paper_trading_mode=False` + `mode='live'`** → real Kite
  order

Default `paper_trading_mode` on a fresh install is **False** (LIVE).
Most operators flip it to `True` (PAPER) once via the navbar; from
then on, even a `mode='live'` token gets downgraded at the
resolver.

### Telegram pings carry deep-links

Every successful gated write fires a Telegram message. The
`request_id` in the message is now a clickable HTML link (Phase 18)
that opens `/admin/research?audit_request=<id>` — your phone gets a
one-tap drill-down to the exact audit row.

---

## 7. Agents — built-in and custom

Agents are the platform's rules-layer. Every alert, every
auto-close, every loss-cut is an agent row in the `agents` table.

### Built-in agents (ship with the codebase)

Seeded from `BUILTIN_AGENTS` in `backend/api/algo/agent_engine.py`
on every boot. **7 agents today** (consolidated from 15 in the
2026-05-26 cleanup):

| Slug | Tier | Topic | Status | Fires when |
|---|---|---|---|---|
| `loss-positions-acct` | high | positions_loss | **active** | ANY account's positions trip per-account thresholds: −2% margin OR −₹30k OR −₹3k/min OR −0.25 %/min |
| `loss-positions-total` | critical | positions_loss | **active** | Book-wide positions trip total thresholds: −2% margin OR −₹50k OR −₹6k/min OR −0.25 %/min |
| `loss-holdings-acct` | high | holdings_loss | **active** | ANY account's holdings trip per-account: −3% day OR −₹2k/min OR −0.15 %/min |
| `loss-holdings-total` | critical | holdings_loss | **active** | Book-wide holdings trip total: −5% day OR −₹4k/min OR −0.15 %/min |
| `loss-funds-negative` | critical | funds_warning | **active** | ANY account's cash OR available margin dips below 0 |
| `loss-pos-total-auto-close` | critical | positions_loss | **inactive** | Destructive — chase-closes every position when total pnl ≤ −₹50k. Run in simulator first, then flip on. |
| `manual` | — | — | **active** | Captures every operator-initiated order (ticket / chain / console) into the agent_events stream |

Each loss-* agent uses `any:` blocks to OR multiple threshold types
together — one agent per (topic, scope) pair instead of 4 separate
agents per threshold variant. The alert renderer produces ONE
Telegram message per fire, with one detail row per matched leaf, so
no information is lost.

Why per-account + total stay as SEPARATE agents per topic (not
collapsed):

- **Tier diverges** — acct is `high`, total is `critical`
- **Notify channels often diverge** in practice — acct = telegram-only,
  total = telegram + email
- **Actions diverge** — total may eventually carry a kill-switch
  action that doesn't make sense at the account level
- Keeping the seam means future config can diverge without
  re-splitting agents

Edit any built-in's condition tree from the `/agents` page — the
seeder preserves your edits across deploys. Reset to defaults by
deleting the row + restarting.

#### Retired slugs (consolidated 2026-05-26)

The following slugs no longer exist as built-in agents. The seeder
auto-prunes any DB rows matching them on startup and logs a
WARNING-level migration notice (visible on `/admin/logs`). If you
had operator-tuned thresholds on any of these, the customisations
are NOT preserved — set them on the new consolidated slugs above.

```
loss-hold-acct-static-pct       → loss-holdings-acct
loss-hold-total-static-pct      → loss-holdings-total
loss-pos-acct-static-pct        → loss-positions-acct
loss-pos-total-static-pct       → loss-positions-total
loss-pos-acct-static-abs        → loss-positions-acct
loss-pos-total-static-abs       → loss-positions-total
loss-hold-acct-rate-abs         → loss-holdings-acct
loss-hold-total-rate-abs        → loss-holdings-total
loss-hold-any-rate-pct          → split into loss-holdings-acct + loss-holdings-total
loss-pos-acct-rate-abs          → loss-positions-acct
loss-pos-total-rate-abs         → loss-positions-total
loss-pos-any-rate-pct           → split into loss-positions-acct + loss-positions-total
loss-funds-cash-negative        → loss-funds-negative
loss-funds-margin-negative      → loss-funds-negative
```

### Custom agents (you / the LLM drafts)

Two paths into the `agents` table:

1. **`/agents` page** — the existing UI. Compose conditions in the
   JSON editor, save as inactive, run in simulator, activate.
2. **`save_agent_draft` MCP tool** — Claude Code builds the
   condition tree from your chat, lands as inactive + paper-mode,
   appears on `/admin/research` → Drafts tab + on `/agents`.

Either way, the agent goes through the simulator review step
before live activation. There is no path to ship an agent active
without explicit human approval (button click on `/agents` OR
operator-minted activate token via the Lab).

### The condition tree (v2 grammar)

```
condition  ::=  leaf
             |  { "all": [condition, …] }       # AND
             |  { "any": [condition, …] }       # OR
             |  { "not": condition }            # NOT

leaf       ::=  { "metric": <metric-token>,
                  "scope":  <scope-token>,
                  "op":     <op-token>,
                  "value":  <literal> }
```

Tokens are registered in the `grammar_tokens` table. View / edit on
`/admin/tokens`. The LLM can call `list_agents` to inspect existing
condition shapes and reuse the same tokens.

Example — *"alert if total positions P&L drops 3% over 10 min"*:

```json
{
  "all": [
    { "metric": "pnl_rate_pct",
      "scope":  "positions.total",
      "op":     "<=",
      "value":  -0.3 }
  ]
}
```

### Run-in-Simulator (the safety check before activate)

Every agent row on `/agents` has a **Run in Simulator** button.
Behind the scenes, the synthesizer (`backend/api/algo/sim/synthesize.py`)
walks the condition tree, picks the leaf nearest to firing, and
builds an inline scenario that drives the metric across that
threshold. Drops you on
`/admin/execution?mode=sim&agent_id=<id>` with the synthesized
scenario armed.

Watch the fire — does it land where you expect? Tighten or widen
the condition tree on `/agents`, repeat. When happy, mint an
activate token + flip it on.

### Agent → order pipeline

Activated agents fire on every matching tick. When they fire:
- Notify channels (telegram / email / log / websocket) get pinged
- If the agent has `actions`, they execute: `place_order`,
  `chase_close_positions`, etc.
- Orders route through `_resolve_mode()` which honours
  `paper_trading_mode`
- All fires land in `agent_events`; all resulting orders land in
  `algo_orders` with the originating `agent_id`

`get_pnl_attribution` slices `algo_orders` by `agent_id` so you can
ask Claude: *"which agents made money this week?"*

---

## 8. Configuration knobs

Three places where settings live, in order of how often you touch
them:

### 8.1 `/admin/settings` — DB-backed, live-tunable

Tune from the UI without a deploy:

| Setting | Default | What it does |
|---|---|---|
| `mcp.audit_retention_days` | 90 | Days of `mcp_audit` rows kept. 0 = forever. |
| `alerts.cooldown_minutes` | 30 | Re-fire gap on any rate / threshold agent |
| `alerts.rate_window_min` | 10 | Window for ΔP&L / Δmin agents |
| `alerts.baseline_offset_min` | 15 | Quiet period after market open before rate agents fire |
| `notifications.telegram_enabled` | True | Master switch for Telegram pings |
| `notifications.email_enabled` | True | Master switch for SMTP |
| `execution.paper_trading_mode` | False (= LIVE on fresh install) | Master kill-switch — when True, every order forced to paper |
| `execution.shadow_mode` | False | When True on prod, log Kite payload + validate via basket_margin without executing |
| `simulator.default_rate_ms` | 2000 | Default tick rate for new sim runs |
| `simulator.chase_max_attempts` | 5 | Max chase re-quotes before UNFILLED |

### 8.2 `backend/config/backend_config.yaml` — server-side, edit-then-restart

Hand-edited on the server. The deploy script preserves operator
edits across deploys for the keys below:

| Section | Used for |
|---|---|
| `cap_in_dev` | Per-capability toggles on dev branches: `genai`, `telegram`, `mail`, `notify_on_deploy`, `market_feed`, `simulator`, `replay` |
| `cap_in_prod` | Same shape, for the prod branch |
| `macros:` | Repo rate / CPI / IIP / GDP / USD-INR + as_of dates that feed `get_economic_snapshot`. Update monthly after RBI / MoSPI releases. |
| `public_base_url` | Optional override for the Telegram deep-link host (defaults to ramboq.com on main, dev.ramboq.com otherwise) |

### 8.3 `backend/config/secrets.yaml` — gitignored, server-only

Never in git. Server-side credentials only:

| Key | Purpose |
|---|---|
| `cookie_secret` | JWT signing secret. Rotating invalidates all live JWTs + the broker_accounts encryption key. Rare. |
| `gemini_api_key` | Google AI Studio key for Gemini Flash. Free tier works. |
| `smtp_user` / `smtp_pass` | SMTP via Hostinger for email alerts |
| `telegram_bot_token` | @RamboQuantBot |
| `telegram_chat_id` | Group: RamboQuant Alerts |
| `alert_emails` / `market_emails` | Recipient lists |
| `kite_accounts` (legacy) | Seeds the `broker_accounts` table on first boot only. After that, edit broker creds on `/admin/brokers`. |

Per the project rule: **never commit `secrets.yaml`**. Edits go
through SSH `sed` on both server paths (`/opt/ramboq`,
`/opt/ramboq_dev`) individually.

### 8.4 The branch is the hard outer gate

Two branches, two destinations:

| Branch | Server path | Domain | DB |
|---|---|---|---|
| `main` | `/opt/ramboq` | ramboq.com | `ramboq` |
| any other | `/opt/ramboq_dev` | dev.ramboq.com | `ramboq_dev` |

On `main` (= prod): every capability is always on regardless of the
`cap_in_dev`/`cap_in_prod` flags. The DB-backed
`execution.paper_trading_mode` decides paper vs live.

On any non-`main` branch: every broker-hitting action is forced to
paper regardless of any flag. Dev is the safe playground.

---

## 9. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `MCP server failed to connect` on Claude Code startup | venv missing, `mcp` package not installed, `RAMBOQ_TOKEN` not exported | Re-run setup 3.1 + 3.2. Check `venv/bin/python -c "from mcp.server.fastmcp import FastMCP"` returns no error. |
| Tool calls return 401 | JWT expired (24h TTL) | Refresh `/admin/research` → Settings, re-copy the export line, restart shell + Claude Code. |
| Tool calls return 502 | Broker offline or rate-limited | Check `/admin/health` — DISCONNECTED account means credentials need refreshing on `/admin/brokers`. |
| `place_order` returns 403 with "Order details do not match" | Token field doesn't match mint exactly | Re-mint with the corrected field. Same units (price = exact tick, qty = integer). |
| `place_order` returns 403 with "Token already used" | LLM tried to reuse a token | Mint a fresh one. |
| `update_agent` returns 403 with the same error | Proposed-changes JSON differs from mint payload by even one byte | Copy the JSON from the LLM into the mint widget verbatim; pass it back in the tool call byte-for-byte. |
| Order placed but Audit shows `result_status=error` | Underlying ticket pipeline rejected (basket margin, bad symbol, etc.) | Check the `result_summary` column — broker's exact error is there. |
| Telegram pings stopped | `notifications.telegram_enabled=false` in `/admin/settings`, bot token rotated, or `is_enabled('telegram')` is False on this branch | Check `/admin/settings`. Telegram bot token + chat_id live in server-side `secrets.yaml`. |
| Lab page shows "No threads yet" but you DID save one | Backend running an older build, or `RAMBOQ_BASE` mismatch | `get_server_info()` returns the base URL the MCP server is talking to. Confirm it matches the Lab page URL. |
| `get_economic_snapshot` shows everything as stale | Hand-maintained `macros:` block needs an update | SSH the server, edit `backend/config/backend_config.yaml::macros:`, restart. Deploy preserves the edit. |
| Tool auto-titled my thread to something generic like "PWAUTO research" | Genai is disabled (`cap_in_dev.genai: false` on dev) | The deterministic stub is firing. Either flip the cap on, or pass an explicit `title=` when saving. |

---

## 10. Codebase pointers

| Need to read | Path |
|---|---|
| MCP server entry point | [backend/mcp/kite_server.py](backend/mcp/kite_server.py) |
| Per-tool implementation | same file — one `@app.tool()` function per tool |
| Confirm-token logic | [backend/api/routes/research.py](backend/api/routes/research.py) — `_purpose_hash_*` / `_mint_token` / `_consume_token` / `_audit_link_html` |
| Audit model | [backend/api/models.py](backend/api/models.py) — `McpAudit` |
| Audit retention task | [backend/api/background.py](backend/api/background.py) — `_task_mcp_audit_cleanup` (03:15 IST daily) |
| Lab page UI | [frontend/src/routes/(algo)/admin/research/+page.svelte](frontend/src/routes/(algo)/admin/research/+page.svelte) |
| Gemini Flash helpers | [backend/shared/helpers/genai_helpers.py](backend/shared/helpers/genai_helpers.py) — `auto_title`, `sentiment_scores`; deterministic stubs when `is_enabled('genai')` is False |
| Agent engine | [backend/api/algo/agent_engine.py](backend/api/algo/agent_engine.py) — `BUILTIN_AGENTS`, `run_cycle()`, `seed_agents()` |
| Agent evaluator | [backend/api/algo/agent_evaluator.py](backend/api/algo/agent_evaluator.py) — `evaluate()`, `validate()` |
| Grammar registry | [backend/api/algo/grammar.py](backend/api/algo/grammar.py), [grammar_registry.py](backend/api/algo/grammar_registry.py) |
| Settings seeder | [backend/shared/helpers/settings.py](backend/shared/helpers/settings.py) — `SEEDS` |
| Test specs | `frontend/e2e/research_*.spec.js` — 10 specs, 46 tests |

---

## 11. Phase history

| Phase | Commit | Scope |
|---|---|---|
| 1 | `726e69a` | MCP foundation + Lab page + ResearchThread model |
| 2a / 2b / 2c | `71196a2` / `bc51933` / `5d84f71` | Draft pipeline · Gemini Flash helpers · Macros |
| 3 | `99c2057` | place_order with confirm token + mcp_audit table |
| 3b+3c+4 | `9a5686c` | Audit tab + Telegram pings + cancel/modify |
| 5 | `1ec801c` | Paper-aware cancel + modify |
| 6+7 | `baf8aeb` | Audit retention + get_audit_recent |
| 8 | `f6690ad` | Bulk option-chain snapshot |
| 9+10+11 | `51770d4` | Watchlist + P&L + funds tools |
| 12 + hotfix | `70dd25d3` / `ddff6ae9` / `d8efe7b1` | Activate/deactivate + `.fn(ctrl,…)` wrapper fix |
| Select swap | `5ee35b5f` / `eab410d9` | Native dropdowns → custom Select |
| 15 | `40b93b0f` | Audit since-window filter |
| Docs | `3fc6e988` | Initial operator runbook |
| 16 | `c1f547bf` | JWT bootstrap shortcut |
| CLAUDE.md | `8202840c` | Architecture section for future Claude sessions |
| 13 + 14 | `ae84c484` | Stale-token cleanup + update_agent |
| 17 + 18 | `7552c54f` | Empty-state polish + Telegram→Audit deep-link |

**Total cost across 18 phases: ₹0.** 24 MCP tools, 46 Playwright
tests, 6 gated writes, end-to-end documented.
