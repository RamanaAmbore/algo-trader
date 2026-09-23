# Plan: fix(derivatives): subscribe spot_anchor_contract to KiteTicker on strategy load

## Context
The strategy endpoint resolves `spot_anchor_contract` (e.g. `CRUDEOIL26OCTFUT` — a specific
expiry contract) and returns it to the frontend payoff overlay. But it never calls
`get_ticker().subscribe_with_sym()` for that contract.

The batchQuote side-effect (quote.py:702) only subscribes the *virtual front-month* resolution
(`MCX:CRUDEOIL` → `CRUDEOIL26JUNFUT`), which may be a different month. So the anchor's token
sits unsubscribed — no live KiteTicker ticks until the 5-minute background task cycle picks it
up (background.py:963).

This is the systemic gap that has manifested in multiple ways: overlay stale (152716 vs 152888
live), and other surfaces where a specific resolved contract's LTP is needed but wasn't in the
subscription set.

All required infrastructure exists:
- `_resolve_token_for_sym(tradingsymbol, exchange)` in `backend/api/routes/quote.py:362`
  — resolves tradingsymbol+exchange → instrument_token using day-cached map, no broker round-trip
- `get_ticker().subscribe_with_sym([(token, sym)])` — idempotent, cheap

## Agents
- backend: In `backend/api/routes/options.py`, add a small async helper
  `_subscribe_anchor_nowait(anchor_sym, exchange)` and fire it with
  `asyncio.create_task()` after every `_resolve_spot` / `_strategy_resolve_spot_impl`
  call that returns a non-null `_spot_anchor`. Do NOT await it — fire-and-forget so the
  strategy response is not delayed.

  ```python
  async def _subscribe_anchor_nowait(anchor_sym: str, exchange: str) -> None:
      """Subscribe the resolved spot anchor contract to KiteTicker.
      Called as a fire-and-forget task so strategy response isn't delayed."""
      try:
          from backend.api.routes.quote import _resolve_token_for_sym
          from backend.brokers.kite_ticker import get_ticker
          tok = await _resolve_token_for_sym(anchor_sym, exchange)
          if tok:
              get_ticker().subscribe_with_sym([(tok, anchor_sym)])
      except Exception:
          pass  # non-critical — background task will pick it up within 5 min
  ```

  Call sites — add `asyncio.create_task(_subscribe_anchor_nowait(...))` after each
  `_resolve_spot` call that returns a non-null anchor. Exchange is `"MCX"` when
  `is_mcx_underlying(underlying)` is True, else `"NFO"`.

  Specifically after:
  1. `options.py:3048` — main strategy endpoint (`_strategy_resolve_spot_impl` result)
  2. `options.py:2553` — single-expiry strategy path
  3. `options.py:2657` — chain snapshot path
  4. `options.py:2413` — `_chain_snapshot_resolve_spot`

  All four sites already compute `_is_commodity = is_mcx_underlying(underlying)` or have
  the underlying available to derive it.

- frontend: skip
- broker: skip
- doc: skip
- backend-test: Add a test in `backend/tests/test_options_route.py` (or nearest options
  test file) verifying that when the strategy endpoint returns a non-null
  `spot_anchor_contract`, `get_ticker().subscribe_with_sym` is called with a matching
  token. Mock `_resolve_token_for_sym` to return a fixed token; assert subscribe_with_sym
  called.
- playwright: skip

## Tests
- pytest: yes
- svelte-check: no
- playwright: no

## Commit message
fix(derivatives): subscribe spot_anchor_contract to KiteTicker on strategy load — fire-and-forget _subscribe_anchor_nowait at all four _resolve_spot call sites

## Done when
- Strategy endpoint: after loading any MCX/NFO strategy, the anchor contract's token is
  subscribed to KiteTicker within the same request cycle (before the 5-min background task)
- Overlay spot price updates at WebSocket tick rate immediately after strategy loads
- pytest: existing tests + new anchor-subscribe test pass
