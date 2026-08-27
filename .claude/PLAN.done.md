# Plan: Fix market summary Telegram alerts routing to wrong group

## Task

Market summary alerts (`market_open`, `market_close`, `visitor_report`) are landing in the
deploy alerts Telegram group instead of the RamboQuant alerts group.

**Root cause**: `_send_telegram_info()` in `alert_utils.py:274-275` looks for
`telegram_chat_id_deploy` as primary key, then falls back to `telegram_chat_id`.
The server's `telegram_chat_id` points to the deploy group (set up first).
Market summaries fall through to that same key → wrong group.

**Fix**: Change `_send_telegram_info()` to look for `telegram_chat_id_ramboquant`
(primary) + `telegram_bot_token_ramboquant` (primary token), with the existing
`telegram_chat_id` / `telegram_bot_token` as fallback. The server then needs
`telegram_chat_id_ramboquant` added to secrets.yaml with the RamboQuant group's chat_id.

## Agents

- backend: skip
- frontend: skip
- broker: skip
- doc: skip
- backend-test: skip
- playwright: skip

**Implementation** (broker agent handles this — it owns alert_utils.py via shared helpers):
- backend: In `backend/shared/helpers/alert_utils.py` lines 257–291:
  1. Change line 274: `telegram_bot_token_deploy` → `telegram_bot_token_ramboquant`
  2. Change line 275: `telegram_chat_id_deploy` → `telegram_chat_id_ramboquant`
  3. Update docstring on `_send_telegram_info()` to say "RamboQuant alerts group"
     instead of "info/deploy channel"
  4. Write a unit test in `backend/tests/` that patches `secrets` dict with
     `telegram_chat_id_ramboquant` and confirms `_send_telegram_info()` uses that key
     (not `_deploy`); also test fallback to `telegram_chat_id` when `_ramboquant` absent.
  For every file you change or create, you MUST write or update at least one test.

## Tests

- pytest: yes
- svelte-check: no
- playwright: no

## Commit message

fix(alerts): market summary Telegram → telegram_chat_id_ramboquant (was _deploy)

## Done when

- `_send_telegram_info()` reads `telegram_chat_id_ramboquant` first, falls back to `telegram_chat_id`
- Unit tests confirm the key lookup order
- pytest green (broker ≥ 80%, API ≥ 45%)
- **Server step (manual — operator)**: SSH to prod server and add
  `telegram_chat_id_ramboquant: <ramboquant group chat_id>` to
  `/opt/ramboq/backend/config/secrets.yaml`. If a separate bot token is used for that group,
  also add `telegram_bot_token_ramboquant: <token>` (otherwise the same bot works fine
  since fallback uses `telegram_bot_token`). Restart `ramboq_api` after editing.
