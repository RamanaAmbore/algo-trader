# RamboQuant — Design Notes for Developers

Companion to [`PROCESS_FLOW.md`](PROCESS_FLOW.md). The flow doc shows **what happens**; this doc explains **why it's designed that way** and **what to watch for** when extending. Read both before touching anything in `backend/api/algo/` or `backend/shared/brokers/`.

---

## 1. Core architectural principles

### 1.1 Single source of truth at the broker boundary

The `Broker` abstract base class (`backend/shared/brokers/base.py`) is the **only** place vendor differences should leak. Every route, agent, and background task talks to a `Broker` instance via `get_broker(account)` from the registry. If you find yourself special-casing `if account.startswith('ZG')` in route code, stop — the right answer is a new `BrokerCapabilities` field or a new ABC method that each vendor implements.

The capability matrix (`backend/shared/brokers/capabilities.py`) is the contract. Adding a new GTT shape? Add a boolean to the dataclass, set it explicitly on each broker, and read it in the cap-warning helper (`frontend/src/lib/data/brokerCapWarnings.js`).

### 1.2 Idempotency is the default for everything that touches a broker

Every path that places a broker order or GTT can fire twice — postbacks arrive twice, chase terminals race postbacks, reconcile sweeps re-fire attaches. Three patterns make this safe:

| Pattern | Where | What it guards |
|---|---|---|
| `attached_gtts_json IS NULL` check | `_fire_template_attach_on_fill` | Double-place TP/SL/Wing at broker |
| `_TEMPLATE_ATTACH_LOCKS[parent_row_id]` | Same function | Concurrent races within the same row |
| `_KILLED_ORDER_IDS` dict with 60-min TTL | `chase.py` | Operator kills landing on stale broker_order_id |
| `MAX(prior, cumulative)` in `_record_partial_fill` | `chase.py:540` | Restart causing cumulative to be added again |

**When adding a new fill-time side-effect, ask:** can my handler fire twice for the same parent? If yes, what's the idempotency check?

### 1.3 Database is authoritative for state; in-memory caches are fast-path

The single uvicorn worker (`--workers 1` in prod, intentional — see §5.3 below) means in-process locks are sufficient, but the DB is still the source of truth. After a restart, every chase loop recovers via `recover_from_db` and re-derives state. Don't store anything operationally meaningful in in-process state without a DB write to back it up.

The `attached_gtts_json` column is a deliberate small-state JSON blob rather than a foreign-key normalized table. Trade-off:
- ✅ Atomic write per parent — no half-attached state visible to readers
- ✅ Easy to refactor the GTT spec shape (just version the JSON inside)
- ❌ Harder to JOIN against; we accept this because GTT inspection is rare

### 1.4 Async by default, sync when forced

Everything API-facing is `async def` over asyncpg. Broker SDK calls are sync — Kite/Dhan/Groww use `requests` under the hood — so we wrap them in `asyncio.to_thread(...)` to keep the event loop unblocked. The threadpool sizing is the default (32 workers); we've never seen it saturate because broker API calls are sub-second.

**Anti-pattern to avoid:** `broker.method()` directly in an `async def` route handler. Even if it returns "fast," a single 2-second hang stalls every other request on that worker.

### 1.5 Demo mode = signed-out + prod branch

Demo isn't a separate code path — it's a runtime guard at the API boundary (`backend/api/auth_guard.py`) plus a frontend flag pulled from context. The same routes serve authenticated + demo traffic; the guard masks accounts and blocks writes. This means **a feature works in demo the moment it works for read-only sessions** — there's no separate "demo enablement" step to forget.

---

## 2. The order/chase/template tripod

This is the most complex part of the codebase. Three subsystems with overlapping responsibilities:

### 2.1 What each one owns

| | Owns | Reads |
|---|---|---|
| **Order routing** (`orders.py`) | The broker-facing entry path. Single ticket + basket. | Settings, templates |
| **Chase loop** (`chase.py`) | The per-order placement lifecycle. Cancel + replace + status polling + partial fill accounting. | Broker, AlgoOrder, kill signal |
| **Template attach** (`template_attach.py`) | Post-fill exit-rule wiring. TP/SL/Wing/Scale/Trail GTTs at the broker. | OrderTemplate, AlgoOrder (read-only at attach time) |

### 2.2 Why three? Why not one big "manage this order" function?

History: the chase loop existed first (single ticket → place + chase to fill). Templates were bolted on later (Phase 0–3 + Sprints A–E). The current shape is intentional — each subsystem can be tested in isolation:

- Chase tests use a mock `_order_status` that returns a scripted sequence.
- Template tests build a `TemplatePlan` directly and assert the GTT spec shape.
- Routes are integration-tested with real broker mocks (`backend/tests/`).

### 2.3 The mode pivot

`mode ∈ {sim, paper, live, shadow}` decides which adapter the order actually hits. The pivot happens at submit time (`_resolve_mode` in `backend/api/algo/actions.py`) and is **persisted on the AlgoOrder row** — every downstream branch (chase terminal, postback, reconcile, template attach) reads `row.mode` to decide whether to call a real broker or the paper engine.

**Gotcha:** the chase loop runs the same code regardless of mode. Paper mode is achieved by injecting the paper engine's `place_order` adapter at the broker registry boundary. Don't add `if mode == 'live'` branches inside chase — the abstraction is the broker registry, not the chase.

---

## 3. Template attach — the override merge

Operator overrides flow through multiple layers; understanding the merge order saves hours of debugging.

```
Request:
  template_id=T1, tp_pct_override=20, sl_pct_override=None

Backend persist (orders.py):
  AlgoOrder.template_id          = T1
  AlgoOrder.template_overrides_json = '{"tp_pct": 20}'   # only NON-None overrides

At fill (template_attach.py::_pick):
  tp_pct = _ov.get("tp_pct") or template.get("tp_pct")
       → 20 (override wins)
  sl_pct = _ov.get("sl_pct") or template.get("sl_pct")
       → template's saved sl_pct (no override)
```

**Per-leg vs shell:** when a basket leg has `template_id` set explicitly (not inherited from `_sharedTemplateId`), the SHELL overrides DO NOT flow through. This is the H-3/audit-Sc.12 fix — pre-fix the shell's tp_pct_override silently contaminated a leg that the operator had retargeted to a different template.

```
submitBasket logic:
  effective_template = leg.template_id ?? shell_template
  if leg has its own template_id:
      tp_override = leg.tp_pct_override  (do NOT fall through to shell)
  else:
      tp_override = leg.tp_pct_override ?? shell.tp_pct_override
```

---

## 4. The chase loop's invariants

Six things the chase loop MUST guarantee:

1. **AlgoOrder.broker_order_id always matches the LATEST broker order.** Cancel-and-replace updates this via `_sync_algo_order_id`. Without it the postback handler can't resolve a row by `broker_order_id`.

2. **AlgoOrder.current_limit reflects the latest re-quoted limit.** Added in M-6. ChaseCard renders this when present so the operator sees the live limit, not entry.

3. **AlgoOrder.filled_quantity is monotonic and never exceeds AlgoOrder.quantity.** The `MAX(prior, cumulative)` clamp in `_record_partial_fill` enforces this post C-1 fix. Template attach reads `filled_quantity` to size exit GTTs.

4. **Operator kills take effect on the very next loop iteration.** `mark_killed(broker_order_id)` is synchronous + the loop checks `is_killed(current_order_id)` (a) at status-check time AND (b) immediately after replace (C-2 fix). The dict has a 60-min TTL so a stale kill flag can't survive across days.

5. **Partial fills get persisted on every NEW delta, not just the first.** The branch fires when `cumulative > already_filled` post-C-1 fix.

6. **A chase that hits >= `_MAX_CHASE_ERRORS` consecutive exceptions aborts.** Prevents infinite re-trying against a broker that's down.

Break any of these and template attach sizes wrong, kills get ignored, or zombie chases burn rate limit.

---

## 5. Concurrency model

### 5.1 Why one uvicorn worker?

`--workers 1` in prod is **intentional** for three reasons:

- **Kite session affinity:** multiple workers would invalidate each other's Kite tokens because Kite enforces one active session per IP.
- **In-process locking is enough:** all locks are `asyncio.Lock` instances; we never need multiprocess coordination.
- **Background tasks need shared state:** the trail-stop poller's in-memory state (`_TEMPLATE_ATTACH_LOCKS`, the ticker manager's `_tick_map`) is process-scoped. Multi-worker would require Redis or similar.

If we ever scale horizontally we'd need to externalize: tokens → DB, locks → DB advisory locks or Redis, ticker state → a separate fanout service.

### 5.2 KiteTicker threading

`KiteTicker` runs Twisted internally — **all WebSocket callbacks fire on a Twisted reactor thread**, not the asyncio event loop. The `TickerManager` bridges this:
- Twisted thread writes `_tick_map[token] = ltp` under a `threading.Lock`
- Async handlers read via `get_ltp(token)` — same lock, briefly held

The lock is non-reentrant and the critical section is O(1) so no deadlock risk.

**If you add anything that runs on the Twisted side**, never call `asyncio.run_coroutine_threadsafe` without testing both directions of the round-trip. The reactor doesn't know about asyncio's event loop.

### 5.3 Background task lifecycle

All background tasks are spawned in `app.on_startup` via `asyncio.create_task(...)`. They run forever; cancellation only happens at app shutdown. Each task is responsible for its own error handling — an uncaught exception kills the task silently. Every task body should be:

```python
async def _task_X():
    while True:
        try:
            ...real work...
        except Exception as e:
            logger.exception(f"_task_X iteration failed: {e}")
        await asyncio.sleep(interval)
```

The `try/except` around the loop body is non-negotiable. We've burned hours debugging "why did the trail stop go silent" only to discover an unhandled `KeyError` ate the task three days earlier.

---

## 6. Frontend state architecture

### 6.1 Why no global store for order state?

Svelte stores would be the obvious pattern but we don't use them for order modal state. Reasons:

- **Modals are short-lived.** The operator opens, fills, submits, closes. State outlives a single modal mount maybe 5% of the time (basket persists across tab flips).
- **Component-local state with bindable props is enough.** `bind:value` on Svelte 5 runes provides bidirectional sync without the boilerplate.
- **One modal at a time.** We don't need a global "current order context" — the modal owns its context.

The exceptions are: `executionMode` (navbar drives every page), `authStore` (every page), `dataCache` (PositionStrip + dashboards share), `orderTemplatesStore` (template CRUD broadcast). These are all narrow — they don't carry order-specific state.

### 6.2 The "shell" pattern

`SymbolPanel.svelte` is a shell. It owns:
- Header (account + symbol pickers)
- Tabs (Ticket / Chain)
- Template row (Default/None pill + override inputs + preview chip)
- Basket bar (when basket has legs)
- Common action footer (margin chip + Submit)

The actual tab content (`OrderTicket.svelte`, `OptionChainTab.svelte`) is mounted as a child. State pipes through:
- **Down via props:** shell → tab (e.g. `_sharedAccount` → `account` prop)
- **Up via callbacks:** tab → shell (e.g. `onMarginUpdate`, `onPreviewPlanUpdate`)
- **Two-way via `bind:`:** for shared mutable state (e.g. `bind:templateId={_sharedTemplateId}`)

When you add a new piece of shell-visible state, decide once:
- Is it tab-specific? → Stay in the tab component.
- Should it survive tab flips? → Lift to shell.
- Does any tab need to READ it? → Pipe down via prop.
- Does any tab need to WRITE it? → `bind:` it.

### 6.3 The preview pipeline

The on-fill preview chip (`on fill → TP ₹250 / SL ₹180 / + Wing BUY ...`) is the single most useful piece of context at submit time. It's computed via two independent pipelines:

- **OrderTicket's `_previewPlan`** ← computed against the Ticket form's symbol/side/qty/price/template.
- **SymbolPanel's `_lastLegPlan`** ← computed against the last basket leg (or operator-focused leg).

The chip render switches between them based on `_activeTab === 'chain' && basketLegs.length > 0`. **Why two pipelines?** Because the inputs are different:
- Ticket: form state, not yet a "leg"
- Last-leg: a fully-formed leg with its own account + symbol + overrides

Both call the same backend endpoint (`previewTicketTemplate`) with the same payload shape. The frontend just feeds them differently.

---

## 7. How to add a new broker

If you're integrating a new vendor (e.g. "Upstox"):

### Backend

1. **Implement the adapter** in `backend/shared/brokers/upstox.py`. Subclass `Broker` (ABC at `base.py`). Implement EVERY method — there's no "partial" mode. If a method genuinely doesn't apply, raise `NotImplementedError` with a clear message rather than returning empty.

2. **Translate to Kite shape.** Every method that returns operator-facing data (orders, positions, ltp, GTTs) must shape its return to match Kite's structure. Frontend renders are Kite-shape; downstream chase + template code expects Kite-shape. Build a `_normalise_*` helper per category. Mirror the patterns in `dhan.py` and `groww.py`.

3. **Status-string normalization.** Add `_UPSTOX_STATUS_TO_KITE = {...}`. Every Kite-canonical status (`COMPLETE`, `OPEN`, `CANCELLED`, `REJECTED`, `EXPIRED`) must map from one Upstox string. The B-1 audit lesson: a single missing entry silently breaks chase fill detection for an entire broker.

4. **Capabilities.** Add `UPSTOX_CAPS` in `capabilities.py` with EVERY field set explicitly. Don't rely on dataclass defaults — being explicit makes capability gaps visible at code review.

5. **Register in `registry.py`.** Add to `_ADAPTERS` map + `CAPS_BY_BROKER_ID`.

6. **Token caching.** Each broker has its own `.log/<broker>_tokens.json`. Follow the connection wrapper pattern in `connections.py`.

7. **Tests.** Add `backend/tests/test_upstox_broker.py`. Mock the vendor SDK at the boundary; assert your `_normalise_*` outputs.

### Frontend

8. **No frontend code change needed.** The `BrokerCapabilities` dataclass is the contract; the cap warning helper at `brokerCapWarnings.js` reads it generically. Operator-visible capabilities surface automatically.

---

## 8. How to add a new template field

Templates have grown organically. The current schema is wide (5 mandatory + 7 optional fields). To add a new one:

### Backend

1. **Add the column** to `OrderTemplate` in `backend/api/models.py`.
2. **Idempotent ALTER TABLE** in `backend/api/database.py::init_db`.
3. **Schema fields** in `backend/api/schemas.py` — `OrderTemplate` (response), `OrderTemplateCreate`, `OrderTemplatePatch`. Also `TicketOrderRequest` + `BasketLeg` if you want a per-submit override.
4. **`_build_overrides_json`** at `orders.py:535` — add the override → JSON key.
5. **`resolve_template_plan`** at `template_attach.py:400` — add the `_pick()` call and the GTT spec emission.
6. **Seeded defaults** — update `SYSTEM_TEMPLATES` in `templates_seed.py` if your field should ship with a value.

### Frontend

7. **Template management UI** at `/automation/templates` — add the input.
8. **Override input** at the shell-level Template container in `SymbolPanel.svelte` — add the override field + reset on template change.
9. **Preview** — `previewTicketTemplate` should already wire it because the backend handles it; double-check the chip render handles the new shape.

### Documents

10. **Add a row** to the §6 4-default matrix in `PROCESS_FLOW.md` if it's a default field.
11. **Update §3 override merge** in `DESIGN_NOTES.md` if your field has unusual merge semantics.

---

## 9. Gotchas the codebase has been bitten by

Documenting these so you don't relearn them:

| Gotcha | Where it bit us |
|---|---|
| **Postback arrives before broker_order_id is committed** | Race window between AlgoOrder pre-persist + seed-broker_id second commit. Fix: fallback recent-NULL-id match (C-3) |
| **Cumulative vs delta in status polls** | Every broker reports `filled_quantity` cumulatively. Pre-fix we added the cumulative value each call → inflation across restarts (C-1) |
| **Kill recorded against old broker_order_id** | Cancel-and-replace creates a new id; kill was only checked against old. Operator's kill silently ignored (C-2) |
| **WeakValueDictionary GC during await** | `_TEMPLATE_ATTACH_LOCKS` could be GC'd between mint and acquire. Fix: strong dict with TTL (M-5) |
| **Reconcile attach BEFORE commit** | Attach pipeline opened its own session and read pre-commit state. Fix: defer to after commit (C-4 single + bulk) |
| **Empty `_normalise_orders` status map** | Groww's "EXECUTED" passed through verbatim; chase loop never saw "COMPLETE" → no fill detection (B-1) |
| **`{@const}` outside immediate `{#if}` parent** | Svelte 5 rule; common when porting from older syntax |
| **`onclick` handler on parent intercepts child clicks** | Use `target.closest('button, input, select')` guard pattern |
| **`tabular-nums` on numeric columns** | Without it, prices jitter horizontally on every quote tick |
| **Comments referencing the current PR** | They rot. Reference design intent, not the task |
| **`np.datetime` / naive `datetime.now()` in DB writes** | Mix with tz-aware columns → "AT TIME ZONE" errors. Always `datetime.now(timezone.utc)` |
| **Kite's `tag` is 20-char max** | We truncate via `_truncate_tag` in `chase.py` |
| **Dhan `ltp()` returned `{}`** by design until B-2. Trail stop silently dead — no log, no Telegram, just zero ratchet | |
| **Groww `cancel_gtt` blind segment fallback** | Could cancel wrong GTT on numeric id collision. Now raises if exchange missing (M-4) |

---

## 10. Testing philosophy

The codebase has fewer tests than ideal — that's a known debt. Where tests exist:

- **`backend/tests/`** — pytest + pytest-asyncio. Run via `pytest backend/tests/`.
- **`frontend/e2e/`** — Playwright. Run via `cd frontend && npx playwright test`.
- **No unit tests for frontend** — relies on `svelte-check` + manual flows + e2e.

The Playwright tests run against `dev.ramboq.com` (deployed dev branch). They're slow but high-confidence. Use them for any UX flow that changes; backend pytest for any algo/broker change.

**Rule of thumb:** if you're touching `chase.py`, `template_attach.py`, or any broker adapter, add a pytest test. If you're touching SymbolPanel / OrderTicket flow, add a Playwright spec.

---

## 11. Logging discipline

Three log files matter:

- `api_log_file` — full API log (5MB rotating × 5). Read this first when debugging.
- `api_error_file` — stdout+stderr tee from systemd. Catches uncaught exceptions.
- `hook.log` — webhook listener output.

Log levels by intent:
- `DEBUG` — for trace-style detail. Verbose; filtered out in prod.
- `INFO` — operator-visible events. Order placed, agent fired, chase replaced.
- `WARNING` — recoverable failures. Broker auth retry, asymmetric GTT, partial OCO failure.
- `ERROR` — uncaught exceptions, lost state. Should also trigger Telegram.

**Don't log inside hot loops** without a rate limit. `_task_performance` ran a `logger.info` per row early on; quickly buried `api_log_file` under non-actionable noise.

---

## 12. Sprint history (high-impact fixes worth knowing)

These previous fixes are documented in code via comment headers. Knowing them saves you from re-introducing the bug:

| Sprint | What it fixed | Lookup |
|---|---|---|
| Sprint A | Reconcile + paper-engine fire template attach paths | grep `Sprint A` |
| Sprint B | Partial-fill DB persistence + lock TTL | grep `Sprint B` |
| Sprint C | Dhan two-leg `modify_gtt` (ENTRY_LEG + TARGET_LEG) + Groww emulated OCO | grep `Sprint C` |
| Sprint D | OrderCard CANCELLED chip + PROXY chip stale-β + MCX unit-mismatch fix | grep `Sprint D` |
| Sprint E | Composite `(mode, status)` index + ChaseStatus.PARTIAL + rate-limit sweep | grep `Sprint E` |
| Phase 0–3 | Templates → on-fill GTT pipeline (the whole template_attach stack) | grep `Phase \d` |

---

## 13. Deployment notes

Both `dev` and `main` deploy via webhook. Push triggers:

```
GitHub push → webhook.ramboq.com → /etc/webhook/dispatch.sh
  → main:  /opt/ramboq/webhook/deploy.sh prod main
  → other: /opt/ramboq_dev/webhook/deploy.sh dev <branch>
```

`deploy.sh` (per env):
1. `git pull`
2. `pip install` (production deps)
3. `npm run build` (vite)
4. `systemctl restart ramboq_api.service` / `ramboq_dev_api.service`
5. `notify_deploy.py` (Telegram-only since May 2026)

**Per-environment serialisation:** a host-wide `/tmp/ramboq_deploy.lock` prevents concurrent prod + dev builds from race-condition npm conflicts. `nice -n 19 ionice -c 3` on npm so background builds never starve API responsiveness.

**Manual server work after SSH:** always `chown -R www-data:www-data /opt/ramboq /opt/ramboq_dev`. Webhook deploys fail silently if file owner is wrong.

---

## 14. Reading order for a new developer

If you've got a week to onboard:

**Day 1 — understand the shape:**
- This doc + `PROCESS_FLOW.md` end-to-end
- `CLAUDE.md` skim (it's the operator-facing manual)
- `backend/api/app.py` startup wiring
- `backend/api/models.py` schema

**Day 2 — order flow:**
- `frontend/src/lib/SymbolPanel.svelte` + `OrderTicket.svelte` (the modal)
- `backend/api/routes/orders.py::ticket_order` (single submit path)
- `backend/api/algo/chase.py::chase_order` (the loop)

**Day 3 — templates:**
- `backend/api/algo/template_attach.py` (resolve + apply)
- `backend/api/algo/templates_seed.py` (the matrix)
- Trace one BUY CE order from click → fill → attach end-to-end

**Day 4 — brokers:**
- `backend/shared/brokers/base.py` (the ABC)
- `backend/shared/brokers/kite.py` (reference impl)
- `backend/shared/brokers/dhan.py` + `groww.py` (vendor quirks)

**Day 5 — background + extras:**
- `backend/api/background.py` (every task)
- `backend/api/algo/actions.py` (agent action handlers)
- `frontend/src/lib/order/ChaseCard.svelte` + `OrderCard.svelte` (display)

If you've got a day: read the §6 chase loop section in `PROCESS_FLOW.md`, then read `chase.py::chase_order` source. Everything else extends from that one function.

---

## 15. When in doubt

Open an `Agent` with `subagent_type=audit` and ask it to trace your specific scenario. The audit agents in this codebase are well-calibrated for finding subtle issues. Don't merge a change to `chase.py` or `template_attach.py` without one.
