# Plan: Auto-reload on deploy — SSE version event

## Context
After a prod redeployment, Vite generates new hashed chunk filenames. The browser's
in-memory entry point references old chunk URLs. SvelteKit's client-side navigation
tries to dynamic-import those old chunks → 404 → derivatives page garbles. This only
started recently because the page grew large enough for Vite to split it into multiple
async chunks.

Fix: send a `version` SSE event (before each `snapshot`) containing the server's git hash.
The frontend stores the first hash it sees. On every SSE reconnect (which happens naturally
when a deploy kills the server), it compares the new hash — if changed, `window.location.reload()`.
No new endpoint, no polling, no SvelteKit config changes.

## Agents

- backend: In `backend/api/routes/quote.py`, add a module-level `_SERVER_HASH` constant and
  yield a `version` ServerSentEvent before the `snapshot` event in `quote_stream()`.

  Add at module level (import subprocess already present in health.py pattern; add near top
  of quote.py after existing imports):
  ```python
  import subprocess as _sp

  def _read_server_hash() -> str:
      try:
          return _sp.run(
              ["git", "rev-parse", "--short", "HEAD"],
              capture_output=True, text=True, timeout=2
          ).stdout.strip() or "unknown"
      except Exception:
          return "unknown"

  _SERVER_HASH: str = _read_server_hash()
  ```

  In `quote_stream()` (around line 1821 where `snapshot` is yielded), add BEFORE the
  snapshot yield:
  ```python
  yield ServerSentEvent(
      data=json.dumps({"hash": _SERVER_HASH}),
      event="version",
  )
  ```

  `json` is already imported in quote.py. `ServerSentEvent` is already used.

- frontend: In `frontend/src/lib/data/quoteStream.js`, add a `version` event listener that
  detects hash change on reconnect and reloads.

  Add after the `_BACKOFF_MAX` constant:
  ```javascript
  let _serverHash = null;  // persists across reconnects; cleared only on page load
  ```

  Add handler:
  ```javascript
  function _onVersion(e) {
      const { hash } = JSON.parse(e.data);
      if (!hash || hash === 'unknown') return;
      if (_serverHash === null) {
          _serverHash = hash;          // first connection — record baseline
      } else if (_serverHash !== hash) {
          window.location.reload();    // deploy detected — reload with fresh chunks
      }
  }
  ```

  In `startQuoteStream()`, after the existing `_es.addEventListener('heartbeat', ...)` line,
  add:
  ```javascript
  _es.addEventListener('version', _onVersion);
  ```

  `_serverHash` must NOT be reset in `_onStreamError` or the reconnect path — it must
  survive reconnects to detect the hash change.

- backend-test: Add vitest test in `frontend/src/lib/__tests__/data/quoteStream.test.js`
  (or new file `quoteStreamVersion.test.js`) testing:
  - Test A: first `version` event sets `_serverHash`, does NOT reload
  - Test B: second `version` event with same hash does NOT reload
  - Test C: second `version` event with different hash calls `window.location.reload()`
  - Test D: hash === 'unknown' is ignored (no reload, no baseline set)

  Mock `EventSource` and `window.location.reload` (vi.spyOn).

- playwright: skip
- doc: skip

## Tests
- pytest: no
- svelte-check: yes
- playwright: no
- vitest: yes

## Commit message
fix(sse): auto-reload on deploy — version event detects git hash change on SSE reconnect

## Done when
- SSE `version` event sent before every `snapshot` on every new connection
- Frontend detects hash change on reconnect → `window.location.reload()`
- `_serverHash` persists across reconnects (survives network hiccups; only cleared on hard reload)
- Same hash on reconnect (network hiccup) → no reload
- `unknown` hash → no action
- svelte-check 0 errors, vitest passes including 4 new version-event tests
