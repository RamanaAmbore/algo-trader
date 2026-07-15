# Plan: fix(alerts): ntfy IPv6 — switch send_ntfy_alert from httpx to urllib

## Context
`send_ntfy_alert()` uses httpx which does happy-eyeballs and picks IPv6 on the prod server.
ntfy.sh returns HTTP 200 over IPv6 but FCM push never reaches the phone. curl uses IPv4
(confirmed: 159.203.148.75) and works. urllib.request uses OS getaddrinfo which returns
AF_INET first on this server, so it connects over IPv4. urllib3 already has HAS_IPV6=False
at line 46 of alert_utils.py but that doesn't affect httpx. Fix: replace httpx with urllib
in send_ntfy_alert only.

## Task
In `backend/shared/helpers/alert_utils.py:send_ntfy_alert()`, replace the `httpx.post`
call with `urllib.request.Request` + `urlopen`. Use `Content-Type: text/plain` header.
Update `backend/tests/test_ntfy_alert.py` to mock `urllib.request.urlopen` instead of
`httpx.post`. The mock response needs `.status` attribute returning 200.

## Agents
- backend: In `backend/shared/helpers/alert_utils.py`, replace the httpx block in
  `send_ntfy_alert()` (lines 922-933) with urllib.request. New block:
  ```python
  import urllib.request as _urlreq
  url = f"{base_url.rstrip('/')}/{topic}"
  send_count = 3 if priority == "urgent" else 1
  for _ in range(send_count):
      req = _urlreq.Request(
          url,
          data=message.encode(),
          headers={
              "Title": title,
              "Priority": priority,
              "Tags": "rotating_light",
              "Content-Type": "text/plain",
          },
          method="POST",
      )
      _urlreq.urlopen(req, timeout=5)
  ```
  Remove the `import httpx` line. Keep all other logic unchanged.
- frontend: skip
- broker: skip
- doc: skip
- backend-test: Update `backend/tests/test_ntfy_alert.py`:
  - Remove `import httpx` (line 21) and the docstring reference to httpx (line 14).
  - Replace ALL `patch("httpx.post")` with `patch("urllib.request.urlopen")`.
  - The mock return value needs `.status = 200` (use `mock_post.return_value.status = 200`).
  - Urgent tests: assert `mock_post.call_count == 3` (unchanged).
  - High tests: assert `mock_post.call_count == 1` (unchanged).
  - No-op tests (missing topic): assert `mock_post.call_count == 0` (unchanged).
  - Exception test: change `mock_post.side_effect = httpx.ConnectError(...)` to
    `mock_post.side_effect = OSError("Connection failed")`.
  - Run `venv/bin/pytest backend/tests/test_ntfy_alert.py -v` to confirm green.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(alerts): ntfy IPv6 — use urllib instead of httpx to force IPv4 on prod server

## Done when
All test_ntfy_alert.py tests pass. From prod server, Python urllib call delivers
push to Android phone.
