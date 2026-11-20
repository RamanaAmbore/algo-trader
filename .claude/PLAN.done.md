# Plan: Broker connection incident fixes (F1–F12) + Greeks/Risk & EV header sync

Two independent, previously-approved pieces of work, neither implemented yet. Combined here so a
single `/impl` run builds both. They touch disjoint files (backend/brokers + backend/api vs. one
derivatives frontend file) and can be dispatched to agents in parallel with no overlap.

---

## Part 1 — Broker connection incident fixes

### Task

Live incident (2026-11-20): ZG0790's Kite login was blocked by a Zerodha account-level CAPTCHA
challenge for ~8.5 hours. A three-way parallel audit (read-only, already completed this session)
plus direct investigation found 8 confirmed defects plus 2 operational gaps exposed by this real
incident. None of these have been fixed yet. This plan fixes all of them. The original
broker-resilience plan (commit `1115a7b4`, same day) already shipped and is confirmed working
correctly in this same incident (Phase C of the timeline, 10:47-12:30 UTC, was clean — zero
cross-account errors, failover correctly walked the candidate list) — that work is NOT being
redone here.

#### Confirmed defects (fix all)

**F1 — False-green health chip (most severe).** `backend/brokers/service/app.py`'s
`_health_heartbeat` (~L381-407) gates its token-validity recheck on
`hasattr(conn_obj, '_is_token_expired')`. `KiteConnection` never implements that method (only
Dhan/Groww do), so every Kite account gets `record_session_ok()` (`broker_apis.py` ~L1144-1146)
stamped every 90s **unconditionally**, regardless of real auth state. `_hlth_resolve_state`
(`backend/api/routes/health.py` ~L892-895) reads red only when `last_fail_at > last_ok_at`, so the
90s heartbeat kept winning the race against real (less frequent) fetch failures. Empirically:
ZG0790 had 1,503 `auth_fail` + 454 `fetch_fail` events yet 179 heartbeat `session_ok` stamps kept
firing throughout — confirmed red-for-5s/green-for-85s flapping, and near-solid green for
00:00-02:00 UTC. Login-cooloff state (`KiteConnection.is_login_cooling_off()`, already public per
the earlier fix) is completely invisible to this computation today.

Fix: (a) `_health_heartbeat` must not call `record_session_ok()` for a Kite account that is
currently cooling off (`conn_obj.is_login_cooling_off()`) — skip the stamp entirely in that case.
(b) Extend conn_service's internal health payload (`backend/brokers/service/routes.py`, whatever
serves `/internal/health/brokers`) to include each Kite account's live cooloff state
(`is_cooling_off: bool`, `blocked_until: float|None`). (c) `_hlth_resolve_state` in `health.py` must
read that new field and force `red` whenever an account is actively cooling off, overriding
whatever the heartbeat/fetch-history race would otherwise say. (d) Add the equivalent reasoning to
this function's docstring/comment so a future reader doesn't reintroduce the gap for a new broker
type that also lacks `_is_token_expired`.

**F2 — Zerodha's real error message is discarded.** In `backend/brokers/connections.py`,
`KiteConnection.login()`, `totp_authenticate()`, and `setup_access_token()` all call
`response.raise_for_status()` (or let the Kite SDK raise) without ever reading `response.text`/
`response.json()`. The actionable body (`{"status":"error","message":"Invalid CAPTCHA
values.",...}`) never reaches the exception, the log line, or `_emit_conn_event`'s detail —
operators only ever see `"400 Client Error: Bad Request"`.

Fix: in `login()` and `totp_authenticate()`, catch the HTTP error path explicitly, attempt
`response.json().get("message")` (fall back to `response.text[:200]` on a non-JSON body, fall back
to the exception's own `str(e)` on any parse failure), and use that captured message in place of
the bare exception string in both the `logger.error(...)` call and the `_emit_conn_event(...,
{"error": ..., "stage": ...})` detail dict. `setup_access_token()`'s exception comes from the Kite
SDK itself (`generate_session`) — capture whatever message attribute the SDK exception carries the
same way, if present, else leave it as today's `str(e)`.

**F3 — Persistent broker-connection failures never escalate past Telegram.** conn_service's logs
DO reach `log_events`/dispatch (confirmed: 17 real `"Telegram alert sent"` HTTP-success
confirmations during the incident), and `ERROR_AGENT`'s blanket catch-all did match and fire. But
`error-alert`'s routing (`backend/api/algo/alert_routing.py` `_PER_SLUG_OVERRIDES`) is Telegram-only
by deliberate design — there is no dedicated agent for broker-login/connection failures that can
escalate severity once a failure persists, so a multi-hour real outage never reached ntfy or email.

Fix (same architecture as every other `generic_notice`-style producer in this file, see
`_attempt_login_with_cooloff` which already has the escalating-cooloff bookkeeping):
- When `_attempt_login_with_cooloff` (`connections.py`) escalates to its own worst tier (cooloff
  streak reaches the 1800s/30-min cap), log ONE additional record through `_DISPATCH_LOGGER` (the
  same non-skip-listed logger `event_agents.py`'s own dispatch-triggering calls use — see the
  "Alert message format" CLAUDE.md entry for the convention), carrying `source_type="process"`,
  `source_name="Broker login"`, `alert_event="broker_login_blocked"`, `action_required=True`, and
  the F2-captured real error message in the body. Do NOT log this on every escalation step — only
  once per account per "reached max cooloff" transition (track via a simple in-memory flag on the
  `KiteConnection` instance, cleared when the streak resets to 0 on a successful login), so this
  doesn't itself become spam.
- New seeded `generic_notice` agent in `backend/api/algo/event_agents.py` (follow the exact pattern
  of `MARKET_STATUS_ALERT_AGENT`/`ORDER_CANCELLED_AGENT` from the "Alert-agent audit, all 4 waves"
  CLAUDE.md entry — new slug, e.g. `broker-login-blocked-alert`, tier `critical`), with a per-slug
  routing row in `alert_routing.py` sending to `telegram` + `ntfy` + `email` explicitly (this one
  genuinely needs all three — it's rare and serious by construction, since it only fires once per
  account per worst-tier cooloff escalation, not per retry).
- `action_required=True` additionally forces the binary ntfy/email gates per the existing
  `_apply_action_required_email_gate`/ntfy gate mechanism — belt and suspenders with the explicit
  channel list above.

**F4 — Margins/funds have no last-known-good that survives a restart.**
`_preload_db_lkg_cache` (`backend/api/background.py` ~L2521) only covers
`kind IN ('holdings','positions')`. ZG0790 had a real 11-19 `daily_book` funds row sitting unused.
Both conn_service restarts during the incident left margins cold with zero substitution.

Fix: extend the query in `_preload_db_lkg_cache` to also include `kind='funds'`, and populate the
in-memory LKG cache keyed the same way holdings/positions already are (check the exact cache-key
shape `_stale_substitute_frame("funds", account)`/`_lkg_or_fallback("funds", ...)` expect — reuse
those helpers, do not invent a new substitution mechanism). conn_service's own startup preload call
(added by the earlier `1115a7b4` fix, `_preload_db_lkg_on_startup` in
`backend/brokers/service/app.py`) calls this same function, so no second call site needs updating.

**F5 — Audit log is broken (unrelated root cause, but zero audit trail for every admin action
today, including the `is_active` toggles this whole incident revolved around).**
`audit_log` writes fail with `CharacterNotInRepertoireError`, 414 times today, across every
mutating admin action. Root cause per the error class: gzip/compressed bytes are ending up in the
`summary` text column instead of decoded text.

Fix: locate the audit-log write path (grep for `audit_log`/`AuditLog` insert call, likely in
`backend/api/routes/` middleware or a shared audit helper) and find where a response/request body
is captured for the `summary` field — it's almost certainly reading raw (possibly gzip-encoded)
bytes without checking/stripping `Content-Encoding` or without decoding before truncating/storing.
Fix at the capture site (decode gzip if present, or read the already-decoded body object instead of
raw bytes) rather than papering over it with an encoding-tolerant column type change.

**F6 — Native `confirm()` dialog doesn't match house style; z-index stacking risk.**
`BrokerHealthBadge.svelte:235` is the only native `window.confirm()` left in the frontend. Design
(already worked out, confirmed no other similar native dialog exists):
- Swap for the existing `ConfirmModal.svelte` (same pattern as `admin/+page.svelte`'s usage) via its
  Promise-returning `ask()`.
- Disable: title `"Disable {account}'s connection?"`, message stating the real consequence
  ("This removes {account} from login attempts, the multi-account data fetch, and ticker failover.
  Other accounts are unaffected."), `danger: true`, `confirmLabel: "Disable"`.
- Enable: title `"Enable {account}'s connection?"`, simple message, `danger: false`,
  `confirmLabel: "Enable"`.
- Z-index: `BrokerHealthBadge`'s own popup renders at `var(--z-drawer)` (20001, per `app.css`'s
  documented ordered z-index scale). `ConfirmModal`'s default `zIndex` prop (400) would render
  behind it. Add one new named tier to `app.css`'s existing scale (do not hardcode a literal in the
  component) — e.g. `--z-confirm-over-drawer: 20050` — positioned and documented in the same ordered
  comment block that already enumerates every tier (tiers 1-9 plus the "pre-existing vars kept"
  section), with a one-line rationale matching the style of every other tier's entry there. Pass
  `zIndex="var(--z-confirm-over-drawer)"` explicitly on this one `<ConfirmModal>` mount only.

**F7 — Market-data calls can bypass the login-cooloff gate.** Even after the resilience fix was
fully live (Phase C, 10:47-12:30 UTC), 436 `ZG0790.quote`/`historical_data`/`ltp` calls still failed
— i.e. still attempted a real network round-trip to Kite during an active cooloff window, which is
exactly the repeated-automated-hammering pattern that tripped Zerodha's anomaly detection in the
first place (see the pre-existing `[KITE-LOGIN-COOLOFF]` CLAUDE.md entry). Confirmed by reading
`PriceBroker._try()` (`backend/brokers/registry.py` ~L366-433): it already walks every underlying
broker in priority order and falls through to the next on ANY exception (including the cooloff
`RuntimeError`), and already proactively skips a broker rate-limited by `_is_rate_limited()` with no
network call — but that proactive-skip check is keyed only on this module's own generic rate-limit
cooloff, not on `KiteConnection.is_login_cooling_off()`. The fallthrough-on-exception behavior
already works correctly today (this is NOT a functional gap — confirmed via the live incident
itself, market-data correctly fell over to other accounts throughout). This fix is a pure efficiency
cleanup: add a proactive `conn_obj.is_login_cooling_off()` check alongside the existing
`_is_rate_limited()` check in `_try()`'s per-broker loop, so a cooling-off Kite broker is skipped
immediately (no even-short-circuited call into `get_kite_conn()`) rather than reactively caught via
the exception path.

**F8 — Misclassified auth error.** `"Invalid \`api_key\` or \`access_token\`."` is classified as
`fetch_fail` instead of `auth_fail` by `is_auth_error_str` (`broker_apis.py` ~L1198/1220) — add this
exact message pattern to that classifier's match list. Taxonomy-only; does not change health-state
color (that's F1), but event-type accuracy matters for F1's new cooloff-aware logic and for any
future consumer of `broker_connection_events`.

**F9 — NAV silently omits a disabled/currently-missing account instead of flagging understated.**
`nav.py`'s `_resolve_conn_keys` (~L547) builds its expected-accounts list from conn_service's live
registry, which excludes a disabled account entirely — not flagged as stale/understated, just
silently absent. **Still live right now**: ZG0790 is currently `is_active=false`
(re-enabling it is F12 below, which also addresses this operationally) — but the code-level gap
(any account temporarily absent from the registry for ANY reason silently vanishes from NAV with no
understated flag) should be fixed regardless of tonight's specific instance.

Fix: `_resolve_conn_keys` (or its caller) should cross-reference the full `broker_accounts` table
(not just conn_service's currently-active registry) and mark the NAV write `understated=True` when
any known account — active or not — has no data contributing to the total. This mirrors the
existing "missing-vs-zero" convention already used elsewhere in this file for genuinely absent
fields; apply the same philosophy to "genuinely absent account" at the NAV-write level.

**F10 — Silent poll-failure in the frontend health store.** `frontend/src/lib/stores.js`
(~L1466-1468) swallows a `/api/admin/broker-health` poll error with no stale marker, so a stale
green can persist indefinitely if the poll itself starts failing. Fix: on a poll error, set/keep an
explicit `stale: true` (or equivalent) flag the popup can render (e.g. a small "health data
stale since HH:MM" note), instead of silently keeping the last-good payload with no indication.

#### Operational fixes (not just code — also apply the live state change)

**F11 — Re-enable ZG0790 now that the connection is confirmed working** (both password-login and
TOTP stages tested clean from the server itself earlier this session). Use the proper admin path
(`PATCH /api/admin/brokers/ZG0790` with `is_active: true`, which triggers
`Connections().rebuild_from_db()` + conn_service propagation) — not a raw DB write.

**F12 — Re-enabling a disabled connection must force a full, unconditional books refresh**
(operator's explicit new requirement). Today, `update_account`'s PATCH handler
(`backend/api/routes/brokers.py` ~L431) calls `_reload_connections()` (→
`Connections().rebuild_from_db()`) on any field change, but does nothing beyond that — a
just-re-enabled account's positions/holdings/margins/funds stay whatever was last cached (possibly
hours stale, or the LKG substitute) until the next routine poll cycle, which is additionally gated
by closed-hours logic (`closed_hours_or_broker()`) that has no reason to treat "operator just
explicitly re-enabled this account" as a reason to bypass itself.

Fix: in `update_account`, when `data.is_active` transitions `False → True` specifically (compare
old vs new value before applying, same convention as `_apply_nonsecret_fields`), after
`_reload_connections()` completes, trigger an immediate forced refresh of that one account's
positions/holdings/margins/funds — reuse the existing `force_refresh=True` plumbing documented in
CLAUDE.md's "Post-fill position freshness" entry (`broker_apis.fetch_positions(...,
force_refresh=True)` and friends, threaded through to conn_service's own `/internal/*?force=1`
query param where applicable), called directly (not through the normal closed-hours-gated route
handler) so it runs unconditionally regardless of market hours. This is explicitly an
operator-triggered action correcting a known-stale account, not a routine poll, so bypassing the
closed-hours gate here is correct and intentional — document this distinction with a one-line
comment at the call site so it isn't "corrected" back to a gated call later. No debounce/rate-limit
is added for repeated rapid toggling — confirmed acceptable since this hits ordinary authenticated
data-fetch endpoints, not the login flow that actually trips Zerodha's anomaly detection.

#### Part 1 agents

- **broker**: F1(a,b) in `backend/brokers/service/app.py` + `backend/brokers/service/routes.py`;
  F2 in `backend/brokers/connections.py`; F3's producer half (the one new tagged log call + the
  per-account "already alerted this escalation" flag) in `connections.py`; F4 in
  `backend/api/background.py`'s `_preload_db_lkg_cache` (bundle with this agent — one logical
  change, reuses `broker_apis.py` LKG helpers); F7 in `backend/brokers/registry.py`; F8 in
  `backend/brokers/broker_apis.py`.
- **backend**: F1(c) in `backend/api/routes/health.py`; F3's consumer half (new seeded agent +
  `alert_routing.py` row) in `backend/api/algo/event_agents.py` + `backend/api/algo/alert_routing.py`;
  F5 (locate + fix the audit-log encoding bug, file TBD by grep); F9 in `backend/api/algo/nav.py`;
  F11 (re-enable ZG0790 via the real admin PATCH endpoint) and F12 in
  `backend/api/routes/brokers.py`.
- **frontend**: F6 in `frontend/src/lib/BrokerHealthBadge.svelte` + `frontend/src/app.css` (new
  z-index tier); F10 in `frontend/src/lib/stores.js`.
- **backend-test**: tests for F1 (cooloff forces red regardless of heartbeat timing), F2 (captured
  Zerodha body appears in logged error/event detail), F3 (escalation-tier alert fires once, routes
  to telegram+ntfy+email, doesn't spam on every retry), F4 (funds LKG substitution survives a
  restart), F7 (a cooling-off account's quote/historical call is skipped with no network call), F8
  (classifier), F9 (NAV flags understated when a known account is absent).
- **playwright**: e2e for F6 (ConfirmModal renders styled correctly over the broker popup, z-index
  visually on top, Enable/Disable labels and danger-styling correct) — extend the existing
  `BrokerHealthBadge` enable/disable spec from the earlier broker-resilience plan rather than adding
  a new file.
- **doc**: one dated CLAUDE.md entry documenting this whole incident + fix set (F1-F12).

#### Part 1 — done when

- A Kite account actively cooling off always reads red in the health chip, never green, regardless
  of heartbeat timing.
- The real Zerodha response message (e.g. "Invalid CAPTCHA values.") reaches the log line, the DB
  event, and any resulting alert — never just "400 Bad Request".
- A broker account whose login cooloff escalates to its worst tier fires one ntfy + one email +
  one Telegram alert (not per-retry spam) carrying the real error text.
- `_preload_db_lkg_cache` also preloads funds/margins; a conn_service restart no longer leaves
  margins cold with zero substitution.
- Audit log writes succeed again for every admin action.
- `BrokerHealthBadge.svelte`'s enable/disable action uses `ConfirmModal` with account-specific
  title/message/danger styling, rendered visibly above the popup with no z-index clipping.
- A cooling-off Kite account's quote/historical/ltp calls are skipped locally (no outbound network
  call), confirmed via log inspection.
- `"Invalid api_key or access_token"` classifies as `auth_fail`.
- NAV marks itself `understated=True` when any known `broker_accounts` row — active or not — has no
  data in a given write.
- The frontend health store shows a stale indicator when its own poll fails.
- ZG0790 is re-enabled via the real admin API path and, in the same action, gets an immediate
  unconditional (non-market-hours-gated) refresh of positions/holdings/margins/funds.

---

## Part 2 — Greeks / Risk & EV header row sync

### Task

Operator report: the Greeks and Risk & EV cards' header rows
(`frontend/src/routes/(algo)/admin/derivatives/+page.svelte`, `.opt-kv-greeks .kv-k-greek` and
`.opt-kv-risk .kv-risk-colhdr`) don't match the rest of the app's grid headers (e.g. `.cand-headrow`,
the canonical reference for this file's own `--grid-header-*` token convention). Two confirmed
defects:

1. **Background is per-cell, not continuous.** `.cand-headrow` paints `background:
   var(--grid-header-bg)` on ONE row-spanning element (a CSS subgrid row — one box, full width, no
   internal gaps). `.opt-kv-greeks`/`.opt-kv-risk` instead paint the background on each individual
   header cell, separated by a real `column-gap` (0.55rem / 0.6rem) — a row of disconnected color
   blocks instead of one solid band end-to-end.
2. **Header height is unpinned and inconsistent.** `.cand-headrow` is pinned to
   `min-height: var(--grid-row-height)` (26px desktop / 34px mobile) with `font-size:
   var(--grid-header-font-size)` (0.65rem) and matching weight/letter-spacing/uppercase/color.
   `.opt-kv-risk .kv-risk-colhdr` already uses the correct font tokens but has no `min-height` pin
   and different padding. `.opt-kv-greeks .kv-k-greek` never applies the grid-header font tokens at
   all — it inherits the base `.kv-k-greek` rule's `font-size: var(--fs-lg); font-weight: 700;`
   (meant for Greek symbol glyphs, not grid-header labels), so it renders both visually different
   AND at a different height than every other header in the app.

### Approach

**Height fix.** `.opt-kv-greeks .kv-k-greek` gains the same `font-size: var(--grid-header-font-size);
font-weight: var(--grid-header-font-weight); letter-spacing: var(--grid-header-letter-spacing);
text-transform: uppercase; color: var(--text-muted);` block `.opt-kv-risk .kv-risk-colhdr` already
has. Both cells get `min-height: var(--grid-row-height); box-sizing: border-box;` added, and padding
aligned to the canonical `0.1rem 0.2rem 0.15rem`. Verify the Greek SYMBOL glyphs (Δ Γ Θ 𝒱 ρ) stay
legible at the smaller grid-header font-size in the browser; only deviate from the shared token if
visual verification shows it's genuinely necessary, and document why if so.

**Background-continuity fix.** Neither card has a dedicated header-row wrapper element the way
`.cand-headrow` does — use the standard gapless-background-across-a-grid-gap technique: each header
cell bleeds its background outward by half the column-gap via a negative margin, compensated by
equal extra padding so the visible text position doesn't move:

```css
.opt-kv-greeks .kv-k-greek {
  margin: 0 calc(0.55rem / -2);
  padding: 0.1rem calc(0.2rem + 0.55rem / 2) 0.15rem;
}
.opt-kv-greeks .kv-k-greek:first-child { margin-left: 0; padding-left: 0.2rem; }
.opt-kv-greeks .kv-k-greek:last-child  { margin-right: 0; padding-right: 0.2rem; }
```
(same pattern for `.opt-kv-risk .kv-risk-colhdr`, with its own 0.6rem column-gap, respecting its
existing `:nth-child(5)/(6)` responsive show/hide at the 1180px breakpoint). Verify in the browser
at both Risk & EV breakpoints and at the Greeks card's mobile horizontal-scroll width that the band
reads as one continuous strip with no seams and no orphaned bleed at either card's own edge.

**Scope discipline.** The shared base `.kv-k-greek`/`.kv-k`/`.kv-v` rules stay untouched — every
change is scoped to the `.opt-kv-greeks .kv-k-greek` / `.opt-kv-risk .kv-risk-colhdr` descendant
selectors only.

### Part 2 agents

- **frontend**: all CSS changes, single file —
  `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`. Verify live in the dev server at
  desktop width, at the Risk & EV 1180px breakpoint (both sides), and at Greeks' mobile
  horizontal-scroll width, before reporting done.
- **playwright**: extend `derivatives_greeks_risk_restructure.spec.js` to assert the background is
  visually continuous, all three header rows' computed heights are equal, and both Risk & EV
  breakpoints still look correct with no orphaned bleed.
- **doc**: none — this file documents its own CSS decisions inline; add the equivalent inline
  comment following that established convention.

### Part 2 — done when

- Greeks and Risk & EV header rows render as one continuous background band, no visible gaps, at
  every tested viewport/breakpoint.
- Both header rows' computed height matches `--grid-row-height`, confirmed equal to
  `.cand-headrow`'s own computed height.
- Greeks' header cells use the same font tokens as every other grid header, not the larger
  Greek-symbol font.
- No regression to the Risk & EV responsive split or the Greeks card's mobile horizontal scroll.

---

## Combined tests / commit

- pytest: yes (Part 1 only) — broker coverage ≥80%, API coverage ≥45%.
- svelte-check: yes (both parts touch `.svelte`/`.js`).
- playwright: yes (both parts).
- Three separate commits, one per part (they touch disjoint files and are logically unrelated):
  1. `fix(brokers): health chip reflects real cooloff state, surface Zerodha's actual error text, escalate persistent connection failures to ntfy/email, preload funds LKG, fix audit-log encoding, themed confirm dialog, force a books refresh on re-enable`
  2. `style(derivatives): sync Greeks and Risk & EV header rows to the app's grid-header convention (continuous background, shared row height)`
  3. `refactor(brokers): shared exponential-backoff formula for retry_kite_conn and the Kite login cooloff`

---

## Part 3 — Shared exponential-backoff utility

### Task

Operator-requested audit (completed, read-only) found the codebase's retry/backoff mechanisms are
each bespoke. Two of them — `retry_kite_conn` (`backend/shared/helpers/decorators.py`) and the Kite
login cooloff (`_attempt_login_with_cooloff`, `backend/brokers/connections.py`, part of this same
Part 1 batch) — already use the identical shape (`min(base * 2**n, cap)`) with different literal
numbers, and are safe, mechanical migration candidates onto one shared formula with zero behavior
change. Everything else audited (Dhan's flat 2-minute cooloff, `PriceBroker`'s flat rate-limit
cooloff, chase's fixed re-quote backoff, KiteTicker's vendor-internal SDK reconnect) has a genuine
reason to stay as-is and must NOT be touched by this change — see the audit findings already
delivered in conversation for the full reasoning per mechanism.

### Approach

New `backend/shared/helpers/backoff.py`:
```python
def exponential_backoff_seconds(attempt: int, *, base: float = 2.0, cap: float = 30.0,
                                 jitter: bool = False) -> float:
    """attempt is 0-indexed. Returns min(base * 2**attempt, cap), optionally +/-10% jitter."""
```
Pure formula only — no sleeping, no state, no persistence. Each call site keeps deciding HOW to use
the number:
- `retry_kite_conn` is an **active, blocking retry** (sleeps and retries within one call) — keep
  that behavior, just replace its inline `min(2**attempt, 30)` with
  `exponential_backoff_seconds(attempt, base=2.0, cap=30.0)`. Its separate flat-30s rate-limit
  branch (triggered by provider rate-limit error text) is NOT touched — that's a real provider
  signal, not a generic backoff curve.
- The Kite login cooloff is a **passive gate** (computes a future `blocked_until`, raises
  immediately, never sleeps) — replace its inline `min(120 * 2**(streak-1), 1800)` with
  `exponential_backoff_seconds(streak - 1, base=120.0, cap=1800.0)`. This is the preferred pattern
  generally for broker/external-API retry surfaces (operator-confirmed design preference this
  session) — do not introduce any NEW active-sleep-and-retry loops for broker/external-API call
  sites; prefer the passive-gate shape for anything new.

Both are pure formula swaps — the actual delay values produced must be byte-identical to today's
values at every attempt count (verify with a table-driven test comparing old formula vs. new
utility across attempts 0-10 for each call site's own base/cap).

### Part 3 agent

- **broker**: `backend/shared/helpers/backoff.py` (new) + its test, plus the two formula-swap edits
  in `backend/shared/helpers/decorators.py` (`retry_kite_conn`) and `backend/brokers/connections.py`
  (`_attempt_login_with_cooloff` — note: this function was already edited once this session for the
  `_conn_created_at` fix; this is an additional, separate change to the same function, just the
  backoff formula line). Run the full broker suite + CC gate afterward.

### Part 3 — done when

- `exponential_backoff_seconds()` exists, pure, tested across a range of attempt counts.
- `retry_kite_conn` and the Kite login cooloff both use it; their actual produced delay values are
  unchanged at every attempt count (regression-tested against the old formulas).
- No other retry/backoff mechanism in the codebase is touched.
- No new active-sleep-and-retry pattern is introduced anywhere in this change.
