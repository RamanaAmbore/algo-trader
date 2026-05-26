# Lab — chat-driven research with Claude Code + MCP

This is the operator's runbook for the `/admin/research` workspace.
The Lab lets you ask Claude Code questions like *"Research RELIANCE
and propose a 1-week trade"* or *"Build me an agent that closes my
NIFTY puts if delta crosses -0.5"*, and the platform turns those
conversations into draft agents and (with explicit per-call
approval) real broker orders.

The chat itself happens inside **Claude Code** (your terminal). The
Lab page is the persistence + audit + token-mint surface. No paid
GenAI is in the loop — your Claude Code subscription covers the
LLM, and any helper calls (auto-title, news sentiment) use the free
tier of Gemini 2.5 Flash.

**Total incremental cost vs the platform you already pay for: ₹0.**

---

## 1. One-time setup (~3 minutes)

The Lab page's **Settings** tab has all four steps printed out with
copy buttons. Open `/admin/research` → **Settings** for the
authoritative source. The notes below explain *what* each step
does.

### 1a. Mint a JWT

The MCP server (running as a subprocess under Claude Code) talks
back to the RamboQuant API the same way every browser session
does — over JWT auth. Mint one with the existing login endpoint
and export it to your shell:

```bash
export RAMBOQ_TOKEN=$(curl -s -X POST https://dev.ramboq.com/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<your-username>","password":"<your-password>"}' \
  | jq -r .access_token)
```

The token's TTL is 24 hours. Restart this shell once a day; the
MCP server picks up the new token on its next subprocess launch.

### 1b. Register the MCP server

Copy the **`.mcp.json` snippet** from the Settings tab into a file
named `.mcp.json` at the repo root (already in `.gitignore`).
Should look roughly like:

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

Point `RAMBOQ_BASE` at whichever host you want the LLM to read
from — dev for experiments, prod (`https://ramboq.com`) for real
trades. The MCP server doesn't care which.

### 1c. Restart Claude Code

Claude Code reads `.mcp.json` only on startup. After step 1b:

```bash
# In the repo root:
claude --resume   # or just `claude` to start a fresh session
```

You'll see `MCP server "ramboq-research" connected` in the
startup banner. If you see `failed to connect`, the most common
causes are:
- `venv/bin/python` doesn't exist — run `python -m venv venv && venv/bin/pip install -r backend/requirements-api.txt`.
- The `mcp` Python package isn't installed — `venv/bin/pip install mcp>=1.27`.
- `RAMBOQ_TOKEN` isn't exported — re-run step 1a.

### 1d. Smoke test

In Claude Code, ask:

> *"Call get_server_info and show me the response."*

You should get back something like:
```json
{"base_url": "https://dev.ramboq.com", "has_token": true, "token_prefix": "eyJhbGciOi…"}
```

You're connected.

---

## 2. The daily workflow

The full LLM-driven trading loop has seven steps. The first four
happen entirely inside Claude Code; the last three involve the Lab
page in your browser.

### 2.1 Research

```
You:    Research RELIANCE. Pull positions, recent news with
        sentiment, option-chain skew for the nearest expiry, the
        macro snapshot, and give me a 1-week thesis.

Claude: [calls get_positions, get_recent_news(symbol="RELIANCE",
         sentiment=true), get_options_chain_snapshot(
         underlying="RELIANCE", expiry="2026-05-29"),
         get_economic_snapshot, get_ohlcv(symbol="RELIANCE",
         days=30)]
        
        Based on the data:
        - Position: long 200 shares @ ₹2,840 avg, ₹568k exposure
        - News: 6 of 12 headlines bullish (Q2 EBITDA beat, JIO
          spinoff rumours); 2 bearish (broker downgrade).
        - Chain at 2900 strike: CE IV 24%, PE IV 21% — slight call
          skew. ATM straddle priced at ~₹95.
        - Macros: repo at 6.50% (170d stale), CPI 5.2% — neutral
          rate trajectory.
        - OHLCV: tight 30d range, broke above 200DMA last week.
        
        Thesis: bullish, 3-5% upside over 1 week. Entry on a
        pullback to 2820, stop at 2790. Risk:reward ~1:2.
```

That's one prompt → one response. The LLM made 5 tool calls
behind the scenes; you didn't have to write any code.

### 2.2 Save the thread

```
You:    Save that thesis as a research thread.

Claude: [calls save_research_thread(symbol="RELIANCE",
         thesis_text=..., confidence="bull", transcript=[...])]
        
        Saved thread #47 to the Lab page.
```

If you don't pass a `title`, the server auto-generates one via
Gemini Flash free tier (`"RELIANCE oversold bounce setup"`). With
Gemini off (dev environment, `cap_in_dev.genai: false`), the
deterministic stub takes the first sentence of the thesis.

The thread shows up immediately on `/admin/research` → **Research**
tab → left rail. Click any row to read the full transcript later.

### 2.3 Draft an agent (optional)

```
You:    Build me an agent: if my RELIANCE position drops 2% in
        any 10-minute window, alert via Telegram + Telegram-only.
        Save the draft.

Claude: [calls list_agents to see existing grammar,
         save_agent_draft(thread_id=47, name="RELIANCE 2%/10m
         loss", conditions={"all": [...]}, events=["telegram"],
         scope="total")]
        
        Created agent reliance-2pct-10m-loss · status=inactive
        (linked to thread #47). Open /agents to inspect, then
        click "Run in Simulator" to validate.
```

The draft ships inactive + `trade_mode=paper` — both hardcoded
server-side. The LLM cannot create an active or live agent. See
section 4 for why.

### 2.4 Review in the simulator

This is the only step that genuinely requires you, not the LLM.
Go to `/agents`, find the draft you just created, click "Run in
Simulator". The existing simulator drives synthetic price moves
through the condition tree and shows you whether the agent fires
where you expect.

If the fires look wrong, edit the condition tree on `/agents`
(YAML / JSON editor) or ask Claude Code to delete + redraft:

```
You:    Delete that draft, the threshold is too sensitive. Make a
        new one at 3% over 15 minutes.
```

### 2.5 Mint an activate token

On `/admin/research` → **Settings** → **0. Mint a confirm token**:

| Field | Value |
|---|---|
| Kind | `ACTIVATE agent` |
| Agent slug | `reliance-2pct-10m-loss` (copy from /agents) |

Click **Mint token**. You'll see:

```
ACTIVATE · agent=reliance-2pct-10m-loss

[copy] 4f8e7d6c5b4a3928…    expires in 47s
```

Single-use, 60-second TTL, bound to *this exact agent slug* + the
activate action. Cannot be re-used to deactivate the same agent,
or to activate a different one.

### 2.6 Activate

```
You:    Activate it. Token: 4f8e7d6c5b4a3928…

Claude: [calls activate_agent(confirm_token="4f8e7d6c5b4a3928…",
         agent_slug="reliance-2pct-10m-loss")]
        
        Agent activated. status=active. Audit row #112 written.
        Telegram ping sent.
```

The agent now fires automatically on every matching tick during
its scheduled hours. You don't need to do anything else.

### 2.7 Wind down (later)

Same gate in reverse — mint a `DEACTIVATE agent` token, paste
into Claude Code, call `deactivate_agent`. Same audit + Telegram
ping. Same 60s window.

---

## 3. Token-gate cheat sheet

Every write tool requires a confirm token. The token is bound to
the action so the LLM can't bait-and-switch between mint and use:

| Tool | Token kind | Purpose hash binds |
|---|---|---|
| `place_order` | `place` | account · symbol · side · qty · mode · order_type · price · trigger |
| `cancel_order` | `cancel` | account · order_id · mode |
| `modify_order` | `modify` | account · order_id · mode · qty · order_type · price · trigger |
| `activate_agent` | `activate` | agent_slug |
| `deactivate_agent` | `deactivate` | agent_slug |

If you change ANY field between mint and use, the call returns
**403 — "Order details do not match the minted token's purpose"**.
That's the gate working as designed; re-mint with the corrected
fields.

The token also expires after 60 seconds and is single-use — a
second redemption gets **403 — "Token already used"**.

---

## 4. The safety property nobody compromises on

Industry standard for production LLM-trading products is **no
LLM-initiated order moves money without explicit per-call
operator approval**. Composer.trade requires a Deploy click. IBKR
TraderGPT requires a confirmation dialog. RamboQuant's per-call
mint serves the same role but with stricter binding:

1. **You can't pre-stage tokens for a session.** The mint UI is
   one token at a time.
2. **You can't re-use a token.** Single-use atomic consume.
3. **You can't redirect a token.** Purpose hash binds every field.
4. **The LLM can't mint its own tokens.** The mint endpoint is
   admin-guarded; no MCP tool wraps it.

Combined with the existing branch/mode gates:

- **dev branch** forces every order to paper, regardless of
  `mode` in the request.
- **prod + `execution.paper_trading_mode=True`** forces paper.
- **prod + `execution.paper_trading_mode=False` + token with
  `mode='live'`** = real Kite order.

Default `paper_trading_mode` on a fresh install is **`False`**
(LIVE) per the seeder. Most operators flip it to `True` (PAPER)
once via the navbar; from then on, even a successfully-minted
`mode='live'` token gets downgraded to paper at the resolver.

---

## 5. Tool reference (23 tools)

### Read (16)
| Tool | What it returns |
|---|---|
| `get_positions(account?)` | Intraday positions across loaded accounts |
| `get_holdings(account?)` | Long-term holdings |
| `get_quote(symbols)` | Live LTP + OHLC + change% for up to 300 keys |
| `get_ohlcv(symbol, days, exchange)` | Historical daily candles |
| `get_recent_news(symbol?, sentiment?)` | Indian-market headlines with bull/bear/neutral tags |
| `get_option_analytics(symbol, qty?)` | Greeks + payoff + risk for ONE leg |
| `get_options_chain_snapshot(underlying, expiry, atm_window)` | LTP + Greeks for ALL strikes ATM ± N |
| `get_economic_snapshot()` | Repo / CPI / IIP / GDP / USD-INR + freshness flags |
| `get_funds_summary(account?)` | Cash + available/used margin |
| `get_watchlist(name)` | Symbols in a curated watchlist |
| `get_pnl_attribution(period, mode)` | P&L grouped by agent |
| `list_agents(status?)` | Existing agents with their status |
| `list_research_threads(symbol?)` | Past saved threads |
| `get_research_thread(thread_id)` | Full transcript + thesis |
| `get_audit_recent(tool?, status?)` | Reverse-chrono MCP-action trail (self-check) |
| `get_server_info()` | Base URL + token presence (diagnostic) |

### Persist (2)
| Tool | What it does |
|---|---|
| `save_research_thread(symbol, thesis_text, confidence, transcript, title?)` | Persist a chat session to the Lab page |
| `save_agent_draft(thread_id, name, conditions, ...)` | Promote thread → inactive draft Agent |

### Gated write (5) — require operator-minted confirm token
| Tool | Requires kind |
|---|---|
| `place_order(confirm_token, account, symbol, side, qty, ...)` | `place` |
| `cancel_order(confirm_token, account, order_id, mode)` | `cancel` |
| `modify_order(confirm_token, account, order_id, ...)` | `modify` |
| `activate_agent(confirm_token, agent_slug)` | `activate` |
| `deactivate_agent(confirm_token, agent_slug)` | `deactivate` |

---

## 6. Common patterns

### "Did my last action land?"

Ask Claude Code to call `get_audit_recent` filtered to the right
tool:

```
You:    Did the activate_agent call go through?

Claude: [calls get_audit_recent(tool="activate_agent", limit=3)]
        
        Yes — request_id=4f8e7d, status=ok, at 14:32:18 IST.
        Agent reliance-2pct-10m-loss is now status=active.
```

### "What changed in my book today?"

```
You:    Pull my P&L attribution for today and tell me which agents
        are pulling weight.

Claude: [calls get_pnl_attribution(period="today", mode="all")]
        
        3 agents fired today:
        - reliance-2pct-10m-loss · 2 orders, ₹+820 gross
        - loss-pos-total-3pct  · 0 orders (no fires yet)
        - nifty-iv-spike       · 4 orders, ₹-1,240 gross — losing
        
        The nifty-iv-spike rule lost ₹1,240 on 4 fills today.
        Consider widening the threshold or deactivating until
        you've reviewed the simulator.
```

### "Show me only the cancelled orders this week"

```
You:    What did I cancel this week?

Claude: [calls get_audit_recent(tool="cancel_order",
         status="ok", limit=50)]
```

Or use the Lab page's **Audit** tab directly — Since="Last 7
days" + Tool="cancel_order" + Status="ok" gives the same view.

---

## 7. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `MCP server failed to connect` on Claude Code startup | venv missing, mcp package not installed, or `RAMBOQ_TOKEN` not exported | Re-run section 1a + 1b. Check `venv/bin/python -c "from mcp.server.fastmcp import FastMCP"` returns no error. |
| Tool calls return 401 | JWT expired (24h TTL) | Re-mint via section 1a, restart Claude Code. |
| Tool calls return 502 | Broker offline or rate-limited | Check `/admin/health` — if a Kite handle shows DISCONNECTED, the credentials need refreshing on `/admin/brokers`. |
| `place_order` returns 403 with "Order details do not match" | Token field doesn't match mint exactly | Re-mint with the corrected field. Use the same units (price = exact Kite tick, qty = integer). |
| `place_order` returns 403 with "Token already used" | LLM tried to reuse a token | Mint a fresh one. |
| Order placed but Audit shows `result_status=error` | Underlying ticket pipeline rejected (basket margin, bad symbol, etc.) | Check the `result_summary` column — it carries the broker's exact error. |
| Telegram pings stop landing | `notifications.telegram_enabled=false` in `/admin/settings`, or telegram bot token rotated | Re-check via `/admin/settings`. The system uses `secrets.telegram_bot_token` from server-side `secrets.yaml`. |
| Lab page shows "No threads yet" but you DID save one | Backend running an older build, or thread saved against a different `RAMBOQ_BASE` | Check the `base_url` in `get_server_info()` matches what the Lab page is loading from. |

---

## 8. What this platform is NOT

- **Not a backtesting engine.** Historical replay lives in
  `/admin/execution?mode=replay`. The MCP tool surface is
  read-only for past data (`get_ohlcv`) and write-only for live
  / paper.
- **Not a strategy library.** The grammar in `/admin/tokens` is
  the building block; agents you build are unique to your book.
- **Not autonomous trading.** Every order requires a per-call
  operator-minted token. There is no "auto-approve any LLM
  trade under ₹X" mode. By design.
- **Not a replacement for the existing `/agents` UI.** The
  agents page remains the canonical surface for editing
  condition trees, viewing recent fires, and toggling status
  outside the Lab pipeline. The Lab is the **LLM-friendly**
  surface — not the exclusive one.

---

## 9. Where to look in the codebase

| Need to read… | Path |
|---|---|
| MCP server entry point | `backend/mcp/kite_server.py` |
| Per-tool implementation | same file — each `@app.tool()` function |
| Confirm-token logic | `backend/api/routes/research.py` (`_purpose_hash_*`, `_mint_token`, `_consume_token`) |
| Audit table model | `backend/api/models.py` (`McpAudit`) |
| Lab page UI | `frontend/src/routes/(algo)/admin/research/+page.svelte` |
| Settings entry for retention | `backend/shared/helpers/settings.py` (`mcp.audit_retention_days`) |
| Background cleanup | `backend/api/background.py` (`_task_mcp_audit_cleanup`) |
| Test specs | `frontend/e2e/research_*.spec.js` (41 tests across 9 specs) |

---

## 10. Phase-by-phase history

The Lab + MCP infrastructure shipped across 12 phases between
2026-05-25 and 2026-05-26:

| Phase | What landed |
|---|---|
| 1 | Foundation — MCP server, Lab page, ResearchThread model |
| 2a | save_agent_draft → promote pipeline |
| 2b | Gemini Flash free-tier auto-title + news sentiment |
| 2c | get_economic_snapshot (macro reference data) |
| 3 | place_order with confirm token + mcp_audit table |
| 3b | Audit tab (forensic view) |
| 3c | Telegram pings on every write |
| 4 | cancel_order + modify_order tools |
| 5 | Paper-aware cancel + modify (closes the paper-mode gap) |
| 6 | Audit retention background task (90-day default) |
| 7 | get_audit_recent self-check tool |
| 8 | get_options_chain_snapshot bulk tool |
| 9–11 | get_watchlist + get_pnl_attribution + get_funds_summary |
| 12 | activate_agent + deactivate_agent tools |
| 15 | Audit since-window filter |

41 Playwright tests cover the path end-to-end against
`dev.ramboq.com`. Total incremental cost: ₹0.
