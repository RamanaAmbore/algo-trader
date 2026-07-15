# Plan: ntfy as alert channel + remove loss-holdings agents

## Context

Two changes:

**A. Remove loss-holdings agents** — `loss-holdings-acct` and `loss-holdings-total` are no longer wanted. Remove both from `BUILTIN_AGENTS` in `agent_engine.py`. No other references in codebase.

**B. ntfy as a first-class delivery channel** — ntfy joins Telegram, Email, Log, In-App in the dispatch system. Architecture mirrors the existing channel pattern exactly:

- `events.py dispatch()` gains an `elif channel == "ntfy"` branch
- Gated by `is_enabled("ntfy")` (same `cap_in_prod` / DB override pattern as telegram/mail)
- `_LOSS_AGENT_DEFAULTS` gains `{"channel": "ntfy", "enabled": True}` → all active loss agents get ntfy automatically
- `template_registry.py` gains a `notify-ntfy-critical` template for future custom agents

Priority is ET clock-based (operator in NH):
- **22:00–07:00 ET** → `Priority: urgent` — bypasses silent + DnD, loud alarm (user's night = Indian market hours)
- **07:00–22:00 ET** → `Priority: high` — normal sound + blink

---

## Files to Modify

### 1. `backend/shared/helpers/alert_utils.py`

Add `send_ntfy_alert(title, message)` near the bottom of the file:

```python
def send_ntfy_alert(title: str, message: str) -> None:
    """Deliver via ntfy.sh (or self-hosted ntfy). Priority is ET clock-based:
      22:00–07:00 ET → urgent (operator's night / Indian market hours)
      07:00–22:00 ET → high   (operator's day)
    Configurable via ntfy_night_start / ntfy_night_end in secrets (24h hours, ET).
    """
    import zoneinfo
    from datetime import datetime
    from backend.config.settings import get_settings

    cfg = get_settings()
    topic = cfg.secrets.get("ntfy_topic")
    if not topic:
        return  # not configured — silent no-op

    base_url = cfg.secrets.get("ntfy_url", "https://ntfy.sh")
    night_start = int(cfg.secrets.get("ntfy_night_start", 22))
    night_end   = int(cfg.secrets.get("ntfy_night_end",   7))

    et_hour = datetime.now(zoneinfo.ZoneInfo("America/New_York")).hour
    is_night = (et_hour >= night_start or et_hour < night_end) if night_start > night_end \
               else (night_start <= et_hour < night_end)
    priority = "urgent" if is_night else "high"

    try:
        import httpx
        httpx.post(
            f"{base_url.rstrip('/')}/{topic}",
            content=message.encode(),
            headers={"Title": title, "Priority": priority, "Tags": "rotating_light"},
            timeout=5,
        )
    except Exception:
        pass  # never block the main alert path
```

`httpx` is already a dependency — no new package needed.

### 2. `backend/api/algo/events.py`

In `dispatch()`, after the existing `elif channel == "email"` branch, add:

```python
elif channel == "ntfy" and is_enabled("ntfy"):
    from backend.shared.helpers.alert_utils import send_ntfy_alert
    send_ntfy_alert(title=agent.name, message=plain_text)
```

(`plain_text` is the existing plain-text render already present in the function for the log channel — reuse it.)

### 3. `backend/api/algo/agent_engine.py`

**A. Remove** `loss-holdings-acct` (lines 836–851) and `loss-holdings-total` (lines 854–869) from `_LOSS_AGENTS`.

**B. Add ntfy to `_LOSS_AGENT_DEFAULTS`** (around line 919–936):

```python
_LOSS_AGENT_DEFAULTS = dict(
    events=[
        {"channel": "telegram", "enabled": True},
        {"channel": "email",    "enabled": True},
        {"channel": "log",      "enabled": True},
        {"channel": "ntfy",     "enabled": True},   # ← add this line
    ],
    ...
)
```

This automatically applies to all remaining active loss agents: `loss-positions-acct`, `loss-positions-total`, `loss-funds-negative`, `loss-pos-total-auto-close`.

### 4. `backend/api/algo/template_registry.py`

Add a new template after `notify-telegram-only` for use in future custom agents:

```python
{
    "kind": "notify",
    "name": "notify-ntfy-critical",
    "description": "Telegram + ntfy + log. For critical alerts that must wake the operator.",
    "body": [
        {"channel": "telegram", "enabled": True},
        {"channel": "ntfy",     "enabled": True},
        {"channel": "log",      "enabled": True},
    ],
},
```

### 5. `backend/config/backend_config.yaml`

Add `ntfy: false` to `cap_in_dev` section (opt-in on dev, like telegram/mail):

```yaml
cap_in_dev:
  ntfy: false    # ← add
  telegram: false
  mail: false
  ...
```

`cap_in_prod` does NOT need a `ntfy` key — missing key defaults to `True` on prod (same as telegram/mail).

### 6. `backend/config/secrets.yaml` — operator edits on server only

```yaml
ntfy_topic: "ramboq-critical-alerts-x7k2m"
ntfy_url: "https://ntfy.sh"
ntfy_night_start: 22
ntfy_night_end: 7
```

Claude must NOT edit this file.

---

## Agents
- backend: Implement changes in alert_utils.py, events.py, agent_engine.py, template_registry.py, backend_config.yaml as described. Do NOT touch secrets.yaml.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Write pytest tests for send_ntfy_alert (mock httpx.post + freeze datetime): (1) ET hour=23 → urgent, (2) ET hour=3 → urgent (past midnight), (3) ET hour=14 → high, (4) no ntfy_topic → no-op (httpx.post not called), (5) ET hour=22 exactly → urgent (start of night), (6) ET hour=7 exactly → high (end of night).
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
feat(alerts): ntfy as delivery channel (urgent/night, high/day); remove loss-holdings agents

## Done when
- `loss-holdings-acct` and `loss-holdings-total` removed from BUILTIN_AGENTS.
- `send_ntfy_alert()` in alert_utils.py with ET clock-based priority.
- `dispatch()` in events.py handles `channel == "ntfy"` gated by `is_enabled("ntfy")`.
- `_LOSS_AGENT_DEFAULTS` includes ntfy channel entry.
- `notify-ntfy-critical` template exists in template_registry.py.
- `cap_in_dev.ntfy: false` in backend_config.yaml.
- pytest green (including 6 new ntfy tests).
- Operator adds ntfy_topic to secrets.yaml on server to activate.
