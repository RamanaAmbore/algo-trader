# Plan: Alert channel matrix redesign

## Context
Operator lives in USA; Indian market hours (09:15–15:30 IST = 23:45–06:00 ET) are sleep time.
Goals: ntfy = sound alerts with per-event priority (not time-of-day clock), Telegram = two
channels (ops vs info, using existing keys), email = market summaries only. Nothing hardcoded
— all routing driven by `alert_routing` config table + agent channel dicts.

Two existing Telegram channels:
- `telegram_chat_id` → Ch1 ops (action/alert events)
- `telegram_chat_id_deploy` → Ch2 info (summaries, visitor report)
- Deploy: ntfy `default` only, remove from Telegram

## Architecture

### Config-driven routing (NEW)
Add `alert_routing` section to `backend/config/backend_config.yaml`:

```yaml
alert_routing:
  order_failure:        { telegram: ops,  ntfy: urgent,  email: false }
  ticker_degraded:      { telegram: ops,  ntfy: urgent,  email: false }
  ticker_recovered:     { telegram: ops,  ntfy: high,    email: false }
  gtt_asymmetric:       { telegram: ops,  ntfy: high,    email: false }
  oco_double_fire:      { telegram: ops,  ntfy: urgent,  email: false }
  template_guard:       { telegram: ops,  ntfy: high,    email: false }
  template_attach_fail: { telegram: ops,  ntfy: urgent,  email: false }
  market_open:          { telegram: info, ntfy: false,   email: true  }
  market_close:         { telegram: info, ntfy: false,   email: true  }
  visitor_report:       { telegram: info, ntfy: false,   email: false }
  agent_alert:          { telegram: ops,  ntfy: false,   email: false }
  deploy:               { telegram: false, ntfy: default, email: false }
```

### `_alert_route()` helper (NEW in alert_utils.py)
Single dispatch function that reads the table above:
```python
def _alert_route(event_key, title, ntfy_msg, tg_body=None, email_fn=None):
    routing = config.get('alert_routing', {}).get(event_key, {})
    if routing.get('ntfy'):
        send_ntfy_alert(title=title, message=ntfy_msg, priority=routing['ntfy'])
    ch = routing.get('telegram')
    if ch == 'ops':   _send_telegram(tg_body or ntfy_msg)
    if ch == 'info':  _send_telegram_info(tg_body or ntfy_msg)
    if routing.get('email') and email_fn:
        email_fn()
```

### Agent channel dicts (loss agents in agent_engine.py)
Already config-driven via channel dicts. `_dispatch_channel()` already reads `ch.get("priority")`.
Update `_LOSS_AGENT_DEFAULTS` + per-agent overrides:
- Remove `{"channel": "email"}` from all loss agents (no email for agent alerts)
- Remove `{"channel": "ntfy"}` from defaults
- Per critical-tier agents (`loss-positions-total`, `loss-funds-negative`): add `{"channel": "ntfy", "priority": "urgent"}`
- Per high-tier agents (`loss-positions-acct`, `loss-margin-low`): add `{"channel": "ntfy", "priority": "high"}`

## Target matrix

| Event | TG Ch1 (ops) | TG Ch2 (info) | Email | ntfy |
|---|:---:|:---:|:---:|:---:|
| Loss total / funds negative (critical) | ✅ | ✗ | ✗ | `urgent` |
| Order rejection | ✅ | ✗ | ✗ | `urgent` |
| Ticker degraded | ✅ | ✗ | ✗ | `urgent` |
| Template attach failure | ✅ | ✗ | ✗ | `urgent` |
| OCO double-fire | ✅ | ✗ | ✗ | `urgent` |
| Loss per-account / margin low (high) | ✅ | ✗ | ✗ | `high` |
| GTT asymmetric | ✅ | ✗ | ✗ | `high` |
| Template guard fired | ✅ | ✗ | ✗ | `high` |
| Ticker recovered | ✅ | ✗ | ✗ | `high` |
| MCP place/cancel/modify | ✅ | ✗ | ✗ | ✗ |
| MCP agent toggle | ✅ | ✗ | ✗ | ✗ |
| Market open summary | ✗ | ✅ | ✅ | ✗ |
| Market close summary | ✗ | ✅ | ✅ | ✗ |
| Visitor report | ✗ | ✅ | ✗ | ✗ |
| Deploy OK/FAIL | ✗ | ✗ | ✗ | `default` |

## Task

### 1. `backend/config/backend_config.yaml`
Add the `alert_routing` block above.

### 2. `backend/shared/helpers/alert_utils.py`
- Add `_send_telegram_info(message)` — same as `_send_telegram()` using `telegram_chat_id_deploy` key (fallback to `telegram_chat_id`), same idle/enabled gates
- Add `_alert_route(event_key, title, ntfy_msg, tg_body=None, email_fn=None)` — reads `alert_routing` from config, dispatches to ntfy / Ch1 / Ch2 / email
- Update `_dispatch(msg_type, ...)` — replace hardcoded channel logic with `_alert_route()` call using key map `{'open':'market_open','close':'market_close','alert':'agent_alert'}`
- Update `_send_order_failure_messages()` — replace Telegram call + email block with `_alert_route('order_failure', 'Order rejected', ntfy_msg, tg_body)`

### 3. `backend/api/algo/template_attach.py`
- `_fire_guard_alert()` — replace separate `_do_telegram()` + `_do_ntfy()` with single `_alert_route('template_guard', ...)` call
- `_fire_attach_fail_alert()` — same, use `'template_attach_fail'` key

### 4. `backend/api/background.py`
Replace per-event telegram calls + missing ntfy calls with `_alert_route()`:
- Ticker degraded → `_alert_route('ticker_degraded', ...)`
- Ticker recovered → `_alert_route('ticker_recovered', ...)`
- GTT asymmetric → `_alert_route('gtt_asymmetric', ...)`
- OCO double-fire → `_alert_route('oco_double_fire', ...)`
- Visitor report → `_alert_route('visitor_report', ...)` (no ntfy, goes to Ch2)

### 5. `backend/api/algo/agent_engine.py`
Update `_LOSS_AGENTS` channel configs:
- Move ntfy out of `_LOSS_AGENT_DEFAULTS`, remove email from defaults
- `loss-positions-total`, `loss-funds-negative`: channels += `{"channel":"ntfy","priority":"urgent"}`
- `loss-positions-acct`, `loss-margin-low`: channels += `{"channel":"ntfy","priority":"high"}`

### 6. `webhook/notify_deploy.py`
- Remove Telegram block (~lines 117–134)
- Keep ntfy block as-is (`priority="default"`)

## Agents
- backend: all 6 file changes above
- frontend: skip
- broker: skip
- doc: skip
- backend-test: update/add pytest for `_alert_route()`, `_dispatch()` routing, loss agent channel configs

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
feat(alerts): config-driven alert routing table; telegram ops/info split; ntfy priorities by event; email summaries only

## Done when
- `alert_routing` table in backend_config.yaml drives all system event dispatch
- `_alert_route()` is the single dispatch path for all non-agent system events
- Loss agents have explicit ntfy priority in channel dicts, no email channel
- `_dispatch()` reads from config for market summaries (Ch2 + email) and agent alerts (Ch1, no email)
- Deploy → ntfy `default` only, no Telegram
- pytest green
