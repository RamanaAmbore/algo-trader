# Plan: Agent CLI grammar — Sprint 6, unified alert dispatch + msg= override

## Task

Two linked changes, backend-first:

1. **Move `log` (only) onto the log-tag-driven dispatch pipeline.**
   Today `telegram`/`email`/`ntfy` write ONE tagged log record
   (`events.py:dispatch()`'s `recorded_channels` + a single `logger.info(...,
   extra={"channels": recorded_channels, "alert_event": "breach", ...})`
   call) which flows through `LogStoreHandler` → `log_store.flush_once()`
   → `event_agents.dispatch_rows()` → a seeded event-agent
   (`event_agents.py` ~line 542, `conditions.log = {tag:"agent",
   min_level:"INFO", where:{alert_event:"breach"}}`) whose `_render_breach`
   renderer (line 173) fans out via `_send_channel`. `log` instead
   dispatches DIRECTLY and IMMEDIATELY inside `_dispatch_channel`
   (events.py:268-290, `_log_channel`), bypassing this entirely — add it
   to the same pipeline telegram/email/ntfy already use.

   **`websocket`/`inapp` are explicitly OUT OF SCOPE for this sprint —
   leave them exactly as they are today (direct, immediate dispatch,
   unchanged).** Operator correction: the original ask was narrower than
   "unify all six channels" — only `log` joins the pipeline. Moving
   websocket/inapp would add a new dependency on the log-flush task
   staying alive (today they have none) and a new DB write per fire
   (today they write nothing) — real costs not worth taking on here. Do
   NOT touch `_broadcast_websocket`/`_broadcast_inapp` or their call sites
   in `_dispatch_channel` at all.

2. **Add `msg=` to the ALERT grammar** — a clause-level default message
   and/or a per-channel override, resolved at COMPILE TIME into a
   `message` key written directly onto each channel's entry in the
   compiled `events` JSON. Channels sharing the identical resolved message
   stay bundled into ONE log record (today's mechanism, unchanged);
   `events.py:dispatch()` must now GROUP channels by their resolved
   message and emit one `logger.info()` call per distinct message group,
   not always exactly one call total.

**Backward compatibility, non-negotiable**: an agent's `events` entries
that carry NO `message` key at all (every existing agent today, and any
agent authored without `msg=`) must behave EXACTLY as before — the
existing auto-composed body (agent name + condition text) is used, grouped
together in the one shared log record, identical output to today. This is
not optional — it's the majority of production traffic.

## Agents

- backend: Three changes in `backend/api/algo/events.py` and
  `backend/api/algo/event_agents.py`, plus one catalog addition:

  **(A0) CRITICAL, fix first — the existing `rec_ch` whitelist silently
  drops `message`.** `events.py:168`: `rec_ch = {k: v for k, v in
  ch.items() if k in ("channel", "priority", "tags")}`. This is the EXACT
  dict that becomes each `recorded_channels` entry. As written today, a
  compiled channel's new `message` key (everything `msg=` depends on)
  gets silently stripped right here, before grouping ever sees it — NO
  error, NO symptom, the whole feature just silently does nothing. Add
  `"message"` to that tuple. Fix this BEFORE writing the grouping logic
  in (A) below, or (A)'s own grouping-by-message will have nothing to
  group on and you won't notice until a manual end-to-end test fails
  confusingly.

  **(A) Message grouping in `events.py:dispatch()`** (currently builds
  `recorded_channels` as a flat list, then one `logger.info()` call after
  the loop). Change: group `recorded_channels` entries by their resolved
  message BEFORE logging.
  - A channel's "resolved message" = `ch.get("message")` if the channel
    dict itself carries one (set by the CLI compiler via `msg=`), else
    `None` (meaning: use the existing auto-composed `telegram_body`/
    `ntfy_body`/`email_body` — unchanged behavior).
  - Group by this key: ALL channels with `None` (the common/default case)
    stay in ONE group, emitting exactly one `logger.info()` call with the
    EXISTING `extra={"channels": [...], "telegram_body": telegram_body,
    "ntfy_body": ntfy_body, "email_subject": ..., "email_body": ...}`
    shape — byte-for-byte what happens today when no agent uses `msg=`.
  - Each channel with a non-`None` custom message gets its OWN separate
    `logger.info()` call — `channels: [that one channel]`, and freshly
    render `telegram_body`/`ntfy_body`/`email_body` FROM THAT CHANNEL'S
    OWN message text (reuse the same body-building logic already in
    `dispatch()`, just parameterized by message text instead of the fixed
    `condition_text`-derived body). Each such call still carries its own
    `agent_name`/`alert_event: "breach"`/etc. — `_render_breach` needs no
    changes at all, since it already just reads whatever `channels`/
    `*_body` fields are on the record it's given; it has no opinion on
    how many records exist per agent fire.

  **(B) Move `log` (only) onto the SAME pipeline.** Remove its
  direct-dispatch branch from `_dispatch_channel` (the `elif channel ==
  "log": _log_channel(...)` line, ~289) — add `"log"` to
  `_RECORDED_CHANNELS` (currently `{"telegram", "email", "ntfy"}`, line
  210) instead. **Do not touch the `websocket`/`inapp` branches at all —
  they stay exactly as-is, unchanged, still dispatching directly inside
  `_dispatch_channel`.** This means:
  - `log` now ALSO goes through the `recorded_channels`/grouping logic
    from (A) above — same message-grouping rules apply to it.
  - `event_agents.py`'s `_send_channel` (line 303) needs ONE new entry in
    its `CHANNELS` dict (line 280, currently `{"ntfy": (...), "telegram":
    (...), "telegram_info": (...), "email": (...)}`) for `log` — port
    `_log_channel`'s existing `logger.warning(...)` call (events.py line
    260) into this new entry's send function. `log` needs no
    `broadcast_fn`/`_broadcast_event` wiring at all (it's just a plain
    log write), so this is a small, self-contained addition — no change
    needed to `_render_breach`'s returned tuple shape for this.
  - `events.py`'s `_log_channel` function and its call site inside
    `_dispatch_channel` can be DELETED once nothing calls it directly
    anymore (confirm via grep before deleting — don't leave dead code).

  **(C) Register `ntfy` as a real catalog token** in
  `backend/config/grammars/agent_grammar.yaml`'s NOTIFY — CHANNELS section
  (alongside the existing `telegram`/`email`/`websocket`/`log`/`inapp`
  rows) — `grammar_kind: notify, token_kind: channel, token: ntfy,
  value_type: enum`, with a `priority` param (`params_schema: {priority:
  {type: enum, enum: [low, default, high, urgent], required: false}}`,
  matching what `_send_ntfy_channel`/`_send_channel` already read from
  `ch.get("priority")`).

  **Test coverage (mandatory, this is a live production alert path)**:
  - `backend/tests/test_events_dispatch.py` or wherever the existing
    telegram/email/ntfy dispatch tests live (grep for `recorded_channels`/
    `_dispatch_channel`/`_RECORDED_CHANNELS` in `backend/tests/` to find
    them) — add tests for: (1) the backward-compat case (no `message` key
    anywhere → identical single-record output to today, byte-for-byte);
    (2) a mix of channels where SOME have a custom `message` and some
    don't → correct grouping (shared group for the no-message ones, one
    separate record per custom-message channel); (3) `log` now flows
    through `recorded_channels` and is absent from `_dispatch_channel`'s
    direct-send branches; (4) `event_agents.py`'s `_send_channel`/
    `CHANNELS` correctly dispatches the new `log` entry; (5) `websocket`/
    `inapp` are UNCHANGED — still dispatch directly inside
    `_dispatch_channel`, still absent from `_RECORDED_CHANNELS` — a
    regression guard proving this sprint didn't touch them.
  - A test confirming `ntfy` resolves correctly via
    `grammar.py`'s catalog loader (same pattern as
    `test_agent_kind_tier_topic.py` or wherever existing catalog-row tests
    live).

- frontend: In `frontend/src/lib/command/grammars/agents.js`:
  1. Extend `alert_clause` per the already-written EBNF in
     `.claude/plans/purrfect-marinating-pixel.md`'s Sprint 6 section:
     `alert_clause := "nop" | (("msg" "=" value ",")? call_list)`. `msg`
     is recognized CONTEXTUALLY — only as `NAME "="` immediately after
     `ALERT`, before any channel — NOT a reserved word (don't add it to
     `_RESERVED`).
  2. `compileAlertClause` (or wherever the ALERT clause currently compiles
     — grep for it) resolves each channel's message: own `msg=`/`message=`
     kwarg on that channel's own call (if it has one) wins; else the
     clause-level default (if present); else — **compile error**:
     `"channel '<name>' has no message — set msg= on ALERT or on this
     channel directly"`. When a message resolves, write it onto the
     compiled channel dict as `message: "<text>"` (matching exactly what
     the backend Agent task above reads via `ch.get("message")`). When no
     `msg=` is used ANYWHERE in the clause at all (today's normal case),
     compiled channels carry NO `message` key — unchanged output,
     unchanged backward-compat guarantee.
  3. A channel relying purely on the clause-level default stays BARE
     (`telegram`, never `telegram()`) — the existing "zero-own-arg call
     must be bare" rule is unchanged; inheriting the default isn't an
     argument TO the channel.
  4. Decompiler (`decompileAlertClause`/wherever ALERT decompiles):
     inverse of the above — if every enabled channel shares the IDENTICAL
     `message` value, hoist it to a single clause-level `msg=` default and
     omit `message=`/`msg=` from each channel; if messages differ, render
     each channel's own `msg=`/`message=` individually, no shared default.
     Channels with NO `message` key at all decompile exactly as today (no
     msg= anywhere).

  **Test coverage (mandatory)**:
  - Extend `frontend/src/lib/__tests__/agentsGrammar.test.js`: clause
    default applies to all bare channels; per-channel override wins over
    the default; a channel with neither errors with the exact message
    above; `telegram()` (empty parens, relying only on the default) is
    still rejected per the existing zero-arg-bare rule; case-insensitive
    `msg`/`MSG` both work; `msg=` combined with the already-shipped
    reordered/omitted ALERT/DO forms still works correctly.
  - Extend `frontend/src/lib/__tests__/agentsDecompiler.test.js`: round-
    trip for shared-default-message agents (hoists to one `msg=`), round-
    trip for per-channel-distinct-message agents (no hoisting), round-trip
    for no-message agents (unchanged, no `msg=` anywhere in output).

  Run after making changes:
  ```
  cd /Users/ramanambore/projects/ramboq/frontend && npx vitest run 2>&1 | tail -20
  cd /Users/ramanambore/projects/ramboq/frontend && npx svelte-check --output machine 2>&1 | tail -20
  ```

## Tests
- pytest: yes — this is a live production alert-dispatch path, coverage is
  non-negotiable, not just "nice to have."
- svelte-check: yes
- playwright: no (no new UI surface this sprint — the existing CLI tab
  already exposes typing arbitrary ALERT clause text; no new component)

## Commit message
feat(agents): Sprint 6 — unify alert channels onto log-tag dispatch, add msg= override

## Done when
- An agent with no `msg=` anywhere produces byte-identical dispatch output
  to before this sprint (same single log record, same auto-composed body).
- `ALERT msg="Stop loss hit", telegram, ntfy(priority="urgent")` sends the
  SAME message text to both telegram and ntfy, in ONE log record.
- `ALERT telegram(msg="custom A"), ntfy(msg="custom B", priority="urgent")`
  produces TWO separate log records, one per channel, each with its own
  message.
- `log` now dispatches through `event_agents.py`'s `_send_channel`/
  `CHANNELS`, not through `events.py:_dispatch_channel`'s old
  direct-send branch (deleted once confirmed dead). `websocket`/`inapp`
  are completely unchanged — still direct, still immediate.
- `ntfy` is a real, autocompletable catalog token.
- Full backend + frontend test suites green, CC clean.
