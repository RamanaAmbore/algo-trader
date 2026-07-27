# Plan: Payoff spot SSOT fix + Dhan TOTP wrapping + stepper spacing

## Context

### Issue 1 — Payoff "Resolving spot…" / curve never renders
The `liveSpot` derived (`derivatives/+page.svelte:1787`) resolves the underlying spot through
this chain:
  1. `getSnapshot(anchor)` — live SSE tick on the spot-anchor contract
  2. `getSnapshot(stratUnd)` — live SSE tick on the underlying itself
  3. `_underlyingQuotes[selectedUnderlying]?.ltp` — batchQuote (30 s poll)
  4. `strategy?.spot` — stale server-poll value → `undefined`

**The SSOT source is never consulted.** `candidatePositions[*].underlying_ltp` is explicitly
marked at line 771 with the comment *"SSOT: prefer backend-stamped underlying_ltp
(positions.py Pass 3)"* and is trusted everywhere else on the page (day-P&L at line 881,
Snapshot rows), but `liveSpot` skips it entirely. During the loading window — before the first
SSE tick lands and before batchQuote refreshes — `liveSpot` returns `undefined`, `spot == null`,
and `OptionsPayoff.svelte:692` shows "Resolving spot…" indefinitely even though
`candidatePositions.underlying_ltp` already holds the correct value from the initial positions
fetch.

### Issue 2 — Dhan TOTP path blocked by dead-token renewal attempt
In `_dhan_conn_under_lock` (`connections.py:1153`), `_try_renew()` is called before
`_mint_and_build()` unconditionally — even when `test_conn=True`. `test_conn=True`
means a DH-906 "Invalid Token" error was just received from Dhan; the current token is
**confirmed dead**. Sending that dead token to `/v2/RenewToken` either fails outright or
returns a token that Dhan immediately rejects again (same invalidated session). Only after
renewal fails does the code fall through to `_mint_and_build()` → `_do_login()` → TOTP.

The fix: gate `_try_renew()` on `not test_conn`. When the token is known-dead, skip renewal
entirely and go straight to the full TOTP re-mint. This was introduced in `fcbfd6ff`
(Jul 23) when `_try_renew()` was wired for the first time — renewal makes sense for the
normal expiry path (`test_conn=False`) but never for the dead-token recovery path.

### Issue 3 — Stepper − / + buttons too close
`QtyInput.svelte:.ot-lots-row { gap: 0.25rem }` leaves almost no breathing room between
the `−`, input, and `+` buttons. Increase to `0.5rem`.

---

## Fix Plan

### Change 1 — liveSpot SSOT fix (frontend)

**File:** `frontend/src/routes/(algo)/admin/derivatives/+page.svelte`

Insert `candidatePositions.underlying_ltp` as step 3 (before batchQuote) in the `liveSpot`
derived:

```js
const liveSpot = $derived.by(() => {
  void _throttledTick;
  const stratUnd = String(strategy?.underlying || '').toUpperCase();
  const stratMatchesSel = stratUnd && stratUnd === selectedUnderlying;

  if (stratMatchesSel) {
    const anchor = String(strategy?.spot_anchor_contract || '').toUpperCase();
    if (anchor) {
      const v = Number(untrack(() => getSnapshot(anchor)?.ltp));
      if (Number.isFinite(v) && v > 0) return v;
    }
    const v = Number(untrack(() => getSnapshot(stratUnd)?.ltp));
    if (Number.isFinite(v) && v > 0) return v;
  }

  // ── NEW: SSOT — backend-stamped underlying_ltp from positions (Pass 3).
  // Trusted everywhere else on the page; consulted before the 30 s
  // batchQuote so the payoff renders immediately on page load without
  // waiting for the first SSE tick or a batchQuote cycle.
  // untrack: candidatePositions changes on leg toggle, not on tick.
  const posUltp = untrack(() => {
    for (const p of candidatePositions) {
      const v = Number(p.underlying_ltp);
      if (v > 0) return v;
    }
    return null;
  });
  if (posUltp != null) return posUltp;

  const bqLtp = untrack(() => _underlyingQuotes[selectedUnderlying]?.ltp);
  if (bqLtp != null && Number.isFinite(bqLtp) && bqLtp > 0) return bqLtp;

  return stratMatchesSel ? strategy?.spot : undefined;
});
```

**Why this position in the chain:** SSE ticks are live and always preferred. `underlying_ltp`
is stamped by the backend positions fetch (on page load + postbacks), so it's available
immediately — before any SSE tick or batchQuote cycle arrives. batchQuote stays as a fallback
for pages without positions (e.g., a fresh strategy with no open legs).

### Change 2 — Skip _try_renew when token is known dead (broker)

**File:** `backend/brokers/connections.py`

In `_dhan_conn_under_lock()` around line 1153, gate renewal on `not test_conn`:

```python
# Before:
if self._access_token:
    new_token = self._try_renew()
    ...

# After:
# Skip renewal when token is confirmed dead (test_conn=True = DH-906 just fired).
# Sending a dead token to /v2/RenewToken either fails outright or returns a token
# that Dhan immediately rejects — both cases waste time before the TOTP re-mint.
if self._access_token and not test_conn:
    new_token = self._try_renew()
    ...
```

Also add a single log line to `_do_login()` capturing the HTTP response status + body
truncated to 200 chars, so auth failures are visible in the log without needing a
debugger:
```python
response = session.post(url, params=params, timeout=30)
logger.debug(
    "[DHAN-LOGIN] %r: status=%s body=%.200s",
    self.account, response.status_code,
    response.text if response.content else '',
)
resp = response.json() if response.content else {}
```

### Change 3 — Stepper gap (frontend)

**File:** `frontend/src/lib/order/QtyInput.svelte`

```css
/* Before */
.ot-lots-row { gap: 0.25rem; }

/* After */
.ot-lots-row { gap: 0.5rem; }
```

### Change 4 — Dashboard header-to-card gap (frontend)

**File:** `frontend/src/routes/(algo)/dashboard/+page.svelte`

The performance card and nav card (`.dash-row1-split`) have an uneven visual gap relative
to the page header row. When `.dash-open-orders` is hidden, the cards abut the header
too closely. Add `margin-top: 0.6rem` to `.dash-row1-split` so the gap is consistent
whether the open-orders bar is visible or not. Also verify the gap is consistent when
`.dash-open-orders` is shown (it already has `margin-bottom: 0.6rem`, so the two
`margin-top` + `margin-bottom` values should together give ~1.2rem total breathing room,
matching other inter-section gaps on the page).

```css
/* Before */
.dash-row1-split {
  gap: 0.6rem;
  margin-bottom: 0.75rem;
}

/* After */
.dash-row1-split {
  gap: 0.6rem;
  margin-top: 0.6rem;
  margin-bottom: 0.75rem;
}
```

---

## Agents
- backend: skip
- frontend: Changes 1 + 3 + 4 (derivatives page liveSpot + QtyInput gap + dashboard card gap)
- broker: Change 2 (skip _try_renew when test_conn=True + add _do_login response logging)
- doc: skip
- backend-test: Add test verifying that DH-906 → test_conn=True path skips _try_renew() and calls _mint_and_build() directly
- playwright: skip

## Tests
- pytest: yes
- svelte-check: yes
- playwright: no

## Commit message
fix(payoff+broker+ui): liveSpot SSOT from positions; Dhan skip renew on dead token; stepper gap; dashboard card gap

## Done when
- Payoff curve shows spot immediately from `candidatePositions.underlying_ltp` without waiting for SSE tick or batchQuote
- "Resolving spot…" no longer appears when positions are loaded
- DH-906 "Invalid Token" path goes directly to TOTP re-mint, not through `_try_renew()` first
- `_do_login()` logs HTTP status + body on every call at DEBUG level
- QtyInput stepper buttons have 0.5 rem gap
- Dashboard performance + nav cards have consistent gap (0.6rem) with the page header row
- pytest green, svelte-check 0 errors

## Critical files
- `frontend/src/routes/(algo)/admin/derivatives/+page.svelte` — `liveSpot` derived (~line 1787)
- `backend/brokers/connections.py` — `DhanConnection._do_login()` (~line 924)
- `frontend/src/lib/order/QtyInput.svelte` — `.ot-lots-row { gap }` (~line 98)
- `frontend/src/routes/(algo)/dashboard/+page.svelte` — `.dash-row1-split` margins
- `backend/tests/broker/` — new Dhan TOTP bad-secret test
