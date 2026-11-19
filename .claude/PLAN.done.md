# Plan: Alert-agent consolidation + frontend reload/SWR extension

## Context

Two tasks the operator approved earlier this session, now dispatched as their own
plan per "wait for dprod to finish, then plan both." Both were explicitly deferred
so they wouldn't collide with the just-shipped derivatives/alert-email batch.

1. **Alert-agent consolidation** — audit found that 3 remaining `generic_notice`
   stragglers could fold into the existing `ALERT_EVENT_DISPATCH` table (the
   2026-10-25 collapse pattern), and the NFO/MCX expiry-risk alerts could merge
   into one `any[]`-conditioned agent. Deep exploration (this session) found real
   mechanical blockers in both — decided below, not re-litigated.
2. **Reload/tab-switch reset-to-blank** — the `createDataStore` SWR pattern
   (Tier-1 `$state` + Tier-2 localStorage, already live for 8 broker-data domains
   in `marketDataStores.svelte.js`) isn't applied to the Orders page's visible
   grid, LogPanel's tabs, or Derivatives' `strategy`/underlying-spot state — so
   those surfaces blank to empty/"Loading…" on reload or tab-switch-back instead
   of showing last-good data while a background refetch happens.

## Part A — Backend: alert-agent consolidation

**A1. Fold `gtt-orphan-cancelled-alert` + `app-message-alert` into `ALERT_EVENT_DISPATCH`.**
`draft-sweep-alert` stays dedicated — its only channel is `telegram_info`, which
the 3 generic channel agents' `only_channel` filter (exact string match against
`"telegram"`/`"ntfy"`/`"email"`) cannot route to; this is the identical reason
`visitor-report-alert`/`deploy-sync-alert`/`market-summary` were deliberately kept
dedicated in the 2026-10-25 collapse. Building a 4th generic channel agent for one
straggler isn't worth it.

In `backend/api/algo/event_agents.py`:
- Move `GTT_ORPHAN_CANCELLED_AGENT` and `APP_MESSAGE_AGENT`'s dict literals into
  `_COLLAPSED_DETECTOR_SPECS` (byte-identical `conditions`/`actions`/`slug`/`name`,
  same shape every other collapsed spec already has there). Remove them from
  `SEEDED_AGENTS`.
- `app-message-alert` currently has **no** `alert_routing._PER_SLUG_OVERRIDES` row
  — it resolves live via `_TIER_DEFAULTS["high"]` (`[telegram, ntfy(high)]`).
  `_build_alert_event_dispatch_table()` defaults a missing override to `[]`, which
  would silently drop every CRITICAL AppMessage (the exact bug this agent was
  built to close in 2026-11-08). **Add `_PER_SLUG_OVERRIDES["app-message-alert"] =
  [{"channel":"telegram","enabled":True},{"channel":"ntfy","enabled":True,"priority":"high"}]`
  in `alert_routing.py` BEFORE the fold lands** — freezes exactly what it resolves
  to today, same convention every other migrated agent's override row followed.
  `gtt-orphan-cancelled-alert` already has a `_PER_SLUG_OVERRIDES` row
  (telegram-only) — no change needed there.
- `_RETIRED_SLUGS`/`_retire_collapsed_event_agents()` pick both up automatically
  (no separate list to touch) — they deactivate (never delete) the two rows.
- Bump `SEED_VERSION` (currently 10 → 11): `_CHANNEL_AGENT_CONDITION`'s hash
  changes for all 3 channel-agent rows since it gains 2 more `any[]` leaves.
- Update pinned literals in `backend/tests/test_alert_channel_collapse.py`:
  `SEEDED_AGENTS` count 16→14, `ALERT_EVENT_DISPATCH` count 18→20 (each spec
  contributes exactly one `alert_event` key).

**A2. Merge `expiry-nfo-risk-alert` + `expiry-mcx-risk-alert`.**
Edit `expiry-nfo-risk-alert`'s row **in place** (not a new slug) — builtin-agent
retirement (`_ae_prune_retired_builtins()`) **deletes** rows, unlike the
event-agent path, so in-place-edit-one keeps one row's history vs. a fresh slug
losing both.

In `backend/api/algo/agent_engine.py`:
- `expiry-nfo-risk-alert`'s `conditions` becomes:
  ```python
  {"any": [
      <verbatim current expiry-nfo-risk-alert "all" tree, unchanged>,
      <verbatim current expiry-mcx-risk-alert "all" tree, unchanged>,
  ]}
  ```
  Each branch keeps its own exchange-specific pre-close suppression leaf
  (`nse_*` vs `mcx_*` — do NOT hoist a shared outer `not{...}`, that would apply
  one exchange's suppression window to both). Update the agent's `name`/
  `description` to say "NFO or MCX" instead of just NFO.
- Remove `expiry-mcx-risk-alert`'s dict from `_EXPIRY_RISK_AGENTS` entirely — it
  gets pruned (deleted) from the DB on next boot via the existing
  `_ae_prune_retired_builtins()` mechanism. No new retirement code needed.
- Update `_AE_CONTENT_MATCHED_COND_MIGRATIONS["expiry-nfo-risk-alert"]`: its value
  must become the CURRENT (pre-merge) `all[...]` tree shown above (the one
  already live since 2026-11-12) so an already-seeded row force-upgrades to the
  merged shape on restart. Remove the now-irrelevant
  `_AE_CONTENT_MATCHED_COND_MIGRATIONS["expiry-mcx-risk-alert"]` entry (its slug
  no longer exists in `BUILTIN_AGENTS`).
- Leave `_compute_topic_suppression()` (the per-cycle `topic="expiry_warning"`
  tie-break/merge mechanism) untouched — it's generic and still needed for any
  future same-topic pair; it simply becomes inert for this specific pair since
  there's only one agent left in that topic.

**A3. Test ripple (backend-test agent):**
- `test_alert_channel_collapse.py` — update the two pinned counts (A1).
- `test_expiry_same_window_consolidation.py` — per-slug lookups for
  `expiry-mcx-risk-alert` need updating/removing; add coverage for the merged
  `any[]` shape firing on either exchange's leaf independently, and verify each
  branch's pre-close suppression is still exchange-scoped (an MCX pre-close
  window must not suppress an NFO breach, and vice versa).
- `test_agent_latch.py` (~line 280, `test_suppressed_rows_merged_into_winner_not_dropped`)
  — uses both slugs as its literal example; needs a different same-topic pair
  (or a synthetic one) since only one of these two agents exists after the merge.
- `test_mcx_preclose_agent.py` (~line 778) — check if it references
  `expiry-mcx-risk-alert`'s slug directly; update if so.
- New test: app-message-alert's `_PER_SLUG_OVERRIDES` row resolves to the same
  channels it resolved to via `_TIER_DEFAULTS["high"]` before the fold (parity
  check — the exact regression this fold must not reintroduce).

## Part B — Frontend: reload/SWR extension

Reuse `frontend/src/lib/data/persistentCache.js` (`cachedRead`/`cachedWrite`,
`rbq.cache.` prefix, TTL buckets) as the shared Tier-2 mechanism throughout —
not always the full `createDataStore()` wrapper, since two of the three targets
have per-instance (not module-singleton) state or existing hand-rolled
keep-last-good logic that `createDataStore`'s generation-guard/fetcher-must-throw
contract doesn't cleanly fit. Each target below picks the right-sized tool.

**B1. Orders page's visible grid.** Correcting the original premise: the page's
own `orders = $state([])` is nearly dead (only feeds an unused `_scopedOrders`).
The actual blank-on-reload grid is `OrderBook.svelte`'s own `orderRows`/`_loading`
— shown as "Loading…" until its first fetch resolves. Fix: promote this to a new
shared `createDataStore` singleton in `marketDataStores.svelte.js` (`md.orders`,
mirroring `positionsStore`'s shape), with a `fetcher` that wraps the existing
`fetchOrders()` + `fetchAlgoOrdersRecent()` merge and **throws** on a genuinely
failed broker leg (restructuring the current `Promise.allSettled`-swallows-errors
logic to surface failure to `createDataStore`'s keep-last-good path, rather than
re-inventing that freeze logic a second time). `OrderBook.svelte` reads
`ordersStore.value`/`.loading` instead of its own local state + `visibleInterval`
poll; the existing 5s poll cadence becomes `ordersStore.load()` on the same
interval (dedup'd for free by `createDataStore`'s in-flight guard). Leave
`HeldOrdersCard.svelte` and `ChaseCard.svelte` out of scope (lower-traffic,
not the reported pain point, and `OrderBook` benefits every page that mounts it,
not just `/orders`) — flag as a known residual gap, not silently fixed.

**B2. LogPanel tabs.** Given the gotchas found (mode-switch effect wipes state
right after mount; fetchers aren't key-stable across `mode`/`simScope`;
`_loadOrders` uses `Promise.allSettled` and doesn't throw on partial failure),
do NOT force these into full `createDataStore`. Instead, add a lightweight
Tier-2 hydrate-and-persist pair directly via `persistentCache.js`'s
`cachedRead`/`cachedWrite`, scoped per-instance at component init:
- On mount, before the first fetch, synchronously `cachedRead()` each tab's own
  key (`log.order.${mode}`, `log.agent.${simScope}`, `log.alerts`, etc. — key
  must encode the same discriminators the fetcher branches on, so a Tier-2 hit
  can never mix sim/live or wrong-mode rows) and seed the `$state` array from it
  if present, BEFORE the mode-switch-wipe effect's own guard (`_lastMode` must be
  initialized from the hydrated mode, not `null`, so the wipe effect doesn't
  immediately erase what was just hydrated).
- On every successful load (existing try/catch's success path, not the
  keep-last-good catch branch), `cachedWrite()` the same key.
- Short TTL bucket (`TTL.minute` or `TTL.short`) — this is about surviving a
  reload/tab-switch, not long-term caching; keep-last-good-on-error already
  covers the "network blip while open" case.
- Apply to `orderRows`/`orderEvents`/`gttRows`, `agentLog`, `alertsLog` (the
  three tabs started eagerly on mount — `systemLog`/`connEvents`/`simLog` start
  lazily on first click and are lower priority; include only if trivial once the
  pattern is established, otherwise flag as deferred).

**B3. Derivatives page — swap existing sessionStorage cache to the shared
persistentCache mechanism, keep everything else.** The page already has a
3-tier pattern for `strategy` (hand-rolled generation guard, "never null on
underlying switch" behavior, per-underlying staleness check via
`_strategyStale`) — only its Tier-2 storage backend is the gap: `sessionStorage`
(dies on tab close, 5-minute cap) instead of the shared `localStorage`-backed
`persistentCache.js` every other domain uses. Fix: replace `_saveCache()`/
`_loadCache()`'s raw `sessionStorage.setItem`/`getItem` calls with
`cachedWrite()`/`cachedRead()` from `persistentCache.js`, same key shape
(`{ts, strategy, drafts, selectedAccounts, selectedUnderlying, selectedExpiries,
enabledSymbols}`), a TTL bucket appropriate to its existing 5-minute max-age
semantics. Do NOT touch the generation guard, the "never null `strategy`"
comment-protected behavior, or the `legsKey`-based memoization — only the
storage tier changes. Also add Tier-2 to `underlyingSpotStore.svelte.js`'s
`_quotes` (currently zero persistence at all) via the same `cachedWrite`/
`cachedRead` pair, keyed per-symbol, short TTL — this directly fixes "payoff/spot
resets empty on reload" since derivatives' spot/prev-close math reads this store.
Leave `simActive`, `_payoffSnapshotByRoot`, and `payoffDrafts.svelte.js` out of
scope (lower priority, not the reported pain point) — flag as residual gaps.

**B4. Cache-invalidation note (no new code needed):** `authStore.login()`/
`.logout()` already wipe every `rbq.cache.*` localStorage key wholesale — any new
`persistentCache`-backed key from B1–B3 is automatically flushed on login/logout
with zero extra plumbing. Keep the derivatives page's own existing
`sessionStorage.removeItem(_CACHE_KEY)` auth-switch handler but update it to call
the new storage API's delete instead (don't leave a dead sessionStorage call next
to the new cachedWrite path).

**B5. Tests:**
- Vitest: `dataStore.svelte.js`/`persistentCache.js` already have test coverage
  for the shared mechanics — add cases for the new `md.orders` store (B1) mirroring
  the existing `positionsStore`/`moversStore` test shape, and for
  `underlyingSpotStore`'s new Tier-2 (B3).
- Playwright: one spec per surface confirming last-good data survives a hard
  reload before the first network response lands (mock/delay the fetch, assert
  the grid/chart shows cached data immediately, not "Loading…"/blank) — Orders
  grid (B1), one LogPanel tab (B2), Derivatives strategy+spot after switching
  underlying then reloading (B3, must also assert the per-underlying key
  prevents cross-underlying contamination per `_strategyStale`'s existing
  invariant).

## Agents

- **backend**: Part A (A1, A2) — `event_agents.py`, `alert_routing.py`,
  `agent_engine.py`.
- **backend-test**: A3 — all pinned-literal and ripple-test updates.
- **frontend**: Part B (B1, B2, B3, B4) — `marketDataStores.svelte.js`,
  `OrderBook.svelte`, `LogPanel.svelte`, derivatives `+page.svelte`,
  `underlyingSpotStore.svelte.js`.
- **playwright**: B5 Playwright specs (reload-survival per surface).
- Bundle backend-test's vitest-side work (dataStore/underlyingSpotStore cases)
  into the frontend agent's own commit instead of a separate dispatch — same
  files, avoids a second agent touching what the frontend agent just wrote.
- broker: skip.
- doc: after both land, one CLAUDE.md pass — new entries for the alert fold
  (counts, slug changes) and the SWR extension (which 3 surfaces, what's
  deliberately left out of scope per B1/B2/B3's residual-gap notes).

## Tests

- pytest: yes
- svelte-check: yes
- playwright: yes

## Commit message

Two separate commits (backend alert consolidation; frontend SWR extension) —
unrelated surfaces, no shared files, no reason to bundle per the one-issue-at-a-time
convention.

## Done when

- Backend: `radon cc` clean, pytest full suite green twice, the two pinned-count
  tests pass with new values, a live app-message CRITICAL alert still resolves to
  telegram+ntfy(high) (parity check), both expiry-risk branches fire independently
  with exchange-scoped pre-close suppression verified by test.
- Frontend: svelte-check 0 errors, vitest green, new Playwright reload-survival
  specs pass (genuinely fail against pre-fix code, not just pass trivially) for
  Orders grid, one LogPanel tab, and Derivatives strategy+spot — each showing
  last-good data immediately on reload instead of blank/"Loading…".
