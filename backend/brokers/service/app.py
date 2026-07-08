"""Connection service — Litestar app that owns every broker session.

Scope (what this process owns, and what nothing else gets to touch):

  • Connections singleton (Kite + Dhan + Groww — all 3 brokers).
  • KiteTicker WebSocket + tick map.
  • Dhan login state (token cache, rate-limit cool-off, recency guard).
  • Groww TOTP session + access-token cache.
  • Per-account fetch-health tracker (broker_apis._FETCH_HEALTH).

Why a separate process:

The main Litestar API restarts on every backend code change (NAV
math, mask registry, route logic). Each restart tears down the
broker sessions above and forces fresh logins, which:
  • Triggers Dhan's 2-min generate_token rate-limit on multi-account
    cold-starts (the DH3747 stuck-loop the operator saw today).
  • Rebinds the KiteTicker WebSocket (~30-90s tick blackout).
  • Re-mints Groww TOTP tokens via GrowwAPI.get_access_token.

Isolating broker lifecycle in its own process means backend changes
in the main API service don't reach these sessions.

Transport — Unix domain socket at /tmp/ramboq_conn.sock. The main
API (ramboq_api.service) speaks HTTP over the UDS via a thin client
in backend/conn_client/.

Process boundary:

  systemctl restart ramboq_api    ← happens often (every backend push)
      ↓ does NOT touch broker sessions
  systemctl restart ramboq_conn   ← rare (connection code change)
      ↓ tears down sessions, triggers fresh logins
"""

from __future__ import annotations

import logging
import time

from litestar import Litestar

from backend.brokers.service.routes import (
    BrokerDispatchController,
    HealthController,
    InternalBrokerController,
)

# Module-level logger — getLogger is singleton-per-name; hoisting it here
# avoids repeated lookups inside each startup/watchdog function body.
logger = logging.getLogger(__name__)


def create_app() -> Litestar:
    """Construct the conn_service Litestar app.

    Mirrors backend.api.app.create_app() shape so the systemd unit
    can keep the same uvicorn entrypoint (`backend.brokers.service.app:app`).
    """
    app = Litestar(
        route_handlers=[
            HealthController,
            InternalBrokerController,
            BrokerDispatchController,
        ],
        on_startup=[_init_connections_on_startup, _start_kite_ticker],
        debug=False,
    )
    return app


def _kite_failover_list(exclude: set[str] | None = None) -> list[str]:
    """Return the ordered list of Kite account codes eligible for the
    ticker WebSocket, sorted by broker_accounts.priority ASC (lowest
    number = tried first). Non-Kite accounts (Dhan / Groww) are
    filtered out — this list is for the WebSocket-owning account only.

    `exclude` is optional — the watchdog passes the currently-failing
    account here so `_pick_next_failover` returns None once every
    other account has been tried and failed within the cool-off window.
    """
    from backend.brokers.connections import Connections
    from backend.brokers.registry import _broker_id_for

    conns = Connections()
    priority_map: dict[str, int] = getattr(conns, "_priority_map", {}) or {}
    _KITE_IDS = {"zerodha_kite", "kite"}
    kite_accts = [
        acct for acct, conn in conns.conn.items()
        if (
            _broker_id_for(acct) in _KITE_IDS
            and getattr(conn, "get_access_token", None) is not None
            and getattr(conn, "api_key", None)
        )
    ]
    if exclude:
        kite_accts = [a for a in kite_accts if a not in exclude]
    # Sort by priority ASC (lower number first); insertion-order tie-break.
    kite_accts.sort(key=lambda a: (int(priority_map.get(a, 100) or 100), a))
    return kite_accts


def _resolve_kite_creds(account: str) -> tuple[str | None, str | None]:
    """Return (api_key, access_token) for a specific Kite account, or
    (None, None) when the token can't be minted (auth failure, network,
    etc.)."""
    from backend.brokers.connections import Connections

    conn = Connections().conn.get(account)
    if conn is None:
        return None, None
    tok_getter = getattr(conn, "get_access_token", None)
    ak = getattr(conn, "api_key", None)
    if tok_getter is None or not ak:
        return None, None
    try:
        tok = tok_getter()
    except Exception:
        logger.warning(
            "conn_service: get_access_token(%s) failed during failover selection",
            account,
        )
        return None, None
    return (ak, tok) if tok else (None, None)


def _pick_kite_account() -> tuple[str | None, str | None, str]:
    """Return credentials + code for the highest-priority Kite account
    whose token is live. Returns (None, None, "") when no Kite account
    is authenticated yet — caller decides whether to retry.

    Delegates to `_kite_failover_list()` for ordering so cold-start and
    watchdog paths share the same priority resolution.
    """
    for acct in _kite_failover_list():
        api_key, tok = _resolve_kite_creds(acct)
        if api_key and tok:
            return api_key, tok, acct
    return None, None, ""


def _try_start_ticker() -> bool:
    """Single attempt to start the KiteTicker against the first live
    Kite account. Returns True when the ticker is started (or already
    running). False when no token is available yet; caller should retry."""
    log = logger
    from backend.brokers.kite_ticker import get_ticker
    from backend.brokers.tick_buffer import TickBufferWriter

    ticker = get_ticker()
    # Already running — nothing to do. status()["started"] is the
    # authoritative flag inside TickerManager.
    if ticker.status().get("started"):
        return True

    # Buffer is a process singleton; safe to attach idempotently.
    if getattr(ticker, "_tick_buffer", None) is None:
        try:
            ticker.attach_tick_buffer(TickBufferWriter())
        except Exception:
            log.exception("conn_service: tick buffer attach failed")
            return False

    api_key, access_token, ticker_account = _pick_kite_account()
    if not api_key or not access_token:
        return False

    import asyncio as _asyncio
    ticker.set_loop(_asyncio.get_event_loop())
    ticker.start(api_key, access_token, account=ticker_account)
    log.info(
        "conn_service: KiteTicker started · account=%s · mmap=/dev/shm/ramboq_ticks",
        ticker_account,
    )
    return True


async def _start_kite_ticker(app: Litestar) -> None:
    """Start the KiteTicker WebSocket and spawn the supervising watchdog.

    Slice 4: KiteTicker lives in conn_service. ramboq_api consumes
    LTPs by mmap'ing the buffer — no UDS hop per read, no WS in the
    main process, no broker re-auth when ramboq_api restarts.

    First attempt happens inline so a successful boot doesn't waste
    a 30s tick. If no Kite account is authenticated yet (chicken-and-egg
    when rebuild_from_db just re-minted the token), the watchdog task
    keeps trying every 30s until it succeeds or the process exits.
    """
    log = logger
    try:
        if _try_start_ticker():
            return
        log.warning(
            "conn_service: no live Kite access_token at startup — "
            "watchdog will retry every 30s until success"
        )
    except Exception:
        log.exception("conn_service: KiteTicker startup failed")

    # Spawn the watchdog. Use create_task on the running loop so the
    # task outlives this on_startup hook. Litestar's lifespan keeps
    # the loop alive for the process's lifetime.
    import asyncio as _asyncio
    _asyncio.get_event_loop().create_task(_ticker_watchdog())


async def _ticker_watchdog() -> None:
    """Background supervisor that runs the auto-failover state machine.

    State machine (fires every INTERVAL_S seconds):

      1. Not started yet → `_try_start_ticker()` (idempotent, cheap).
         Handles the boot chicken-and-egg — rebuild_from_db may still
         be minting tokens when on_startup fires.

      2. Started + healthy → reset the unhealthy counter and idle.
         Healthy = WS connected AND at least one tick landed within
         the tick heartbeat window (default 60 s).

      2b. Market-hours gate (checked after boot grace): if all segments
          are closed (NSE + MCX + CDS all outside session windows),
          tick silence is expected → reset unhealthy counter and idle.
          Prevents false-positive failovers at NSE close (15:30 IST)
          and MCX close (23:30 IST).

      3. Started + one bad cycle → bump the unhealthy counter. Try
         `_try_start_ticker()` against the SAME account (idempotent —
         Twisted's reconnect logic + KiteTicker's own retry loop
         normally recover from a single 30 s blip).

      4. Started + N consecutive bad cycles (>= unhealthy_threshold) →
         attempt to swap to the next Kite account in the failover list.
         Gated by:
           - swap_cooldown_seconds since the last swap (5 min default),
           - the account isn't in per-account cool-off (also 5 min),
           - the account has a live api_key + access_token.
         All accounts exhausted → mark `_ticker_unavailable` mode
         (log + slowed cadence). Main API's mmap read path already
         degrades gracefully to broker.ltp() REST fallback for LTPs.

      5. Boot grace period (`supervisor_uptime_seconds()` < BOOT_GRACE_S)
         → suppress swap decisions entirely. Prevents a swap during the
         first ~60 s while `rebuild_from_db` is still re-minting the
         primary account's token.

    Settings-backed tunables (backend_config.yaml → `kite_ticker`):
      * `unhealthy_threshold`   — default 2 (60 s to swap at 30 s interval)
      * `swap_cooldown_seconds` — default 300 (min inter-swap window)
      * `all_down_watchdog_seconds` — default 60 (slowed cadence when
        every account has been exhausted)

    Cheap — no broker calls; only status() dict reads and (rarely) a
    Connections cache lookup for credential resolution.
    """
    import asyncio
    log = logger
    from backend.brokers.kite_ticker import get_ticker
    from backend.shared.helpers.settings import get_int

    INTERVAL_S = 30.0
    BOOT_GRACE_S = 60.0     # suppress swap decisions during first minute

    # Read tunables once per iteration so an operator edit via /admin/settings
    # takes effect on the next cycle without a restart. Cheap: get_int hits
    # an in-process cache when the settings table hasn't been touched.
    def _threshold() -> int:
        # Minimum 1: 0 would swap on every cycle including transient blips.
        return max(1, get_int("kite_ticker.unhealthy_threshold", 2))

    def _cooldown_s() -> float:
        return float(max(30, get_int("kite_ticker.swap_cooldown_seconds", 300)))

    def _slowed_interval_s() -> float:
        return float(max(30, get_int("kite_ticker.all_down_watchdog_seconds", 60)))

    ticker = get_ticker()
    ticker.mark_supervisor_started()
    # Sticky state: once `_ticker_unavailable` is True, we sleep at the
    # slowed cadence and keep trying the primary. Cleared as soon as any
    # account comes back up.
    unavailable_mode = False

    while True:
        try:
            await asyncio.sleep(_slowed_interval_s() if unavailable_mode else INTERVAL_S)
            ticker = get_ticker()

            # ── Reactor-dead exit (must check before all other phases) ──
            # Twisted reactor is a process-level singleton. Once it stops
            # independently (ReactorNotRunning on kws.stop()), reactor.run()
            # raises ReactorNotRestartable in every subsequent attempt — the
            # connect thread dies silently, _started stays False, and no
            # callbacks ever fire. The only recovery is a fresh process.
            # systemd (Restart=always, RestartSec=5) handles the restart.
            if ticker.is_reactor_dead():
                import sys
                log.critical(
                    "ticker_watchdog: Twisted reactor dead (ReactorNotRunning) — "
                    "exiting so systemd spawns a fresh process (Restart=always)"
                )
                sys.exit(1)

            # ── Phase 1: nothing running yet ────────────────────────────
            if not ticker.status().get("started"):
                if _try_start_ticker():
                    log.info(
                        "ticker_watchdog: ticker started · account=%s",
                        ticker.current_account() or "?",
                    )
                    unavailable_mode = False
                    ticker.reset_unhealthy()
                continue

            # ── Phase 2: boot grace ─────────────────────────────────────
            in_grace = ticker.supervisor_uptime_seconds() < BOOT_GRACE_S

            # ── Phase 2b: market-hours gate ──────────────────────────────
            # Kite legitimately sends no ticks when all segments are closed.
            # Silence is expected — reset the counter and idle rather than
            # treating it as an unhealthy connection and triggering failover.
            # Prevents the false-positive swap storm that fires at NSE close
            # (15:30 IST) and MCX close (23:30 IST) every session.
            from backend.shared.helpers.date_time_utils import (
                is_any_segment_open, timestamp_indian,
            )
            if not is_any_segment_open(timestamp_indian()):
                ticker.reset_unhealthy()
                continue

            # ── Phase 3: health check ───────────────────────────────────
            if ticker.is_active_ticker_healthy():
                ticker.reset_unhealthy()
                if unavailable_mode:
                    log.info(
                        "ticker_watchdog: recovered from all-accounts-down "
                        "(active=%s)", ticker.current_account() or "?",
                    )
                    unavailable_mode = False
                continue

            unhealthy_count = ticker.bump_unhealthy()
            log.warning(
                "ticker_watchdog: active=%s unhealthy cycle %d (threshold=%d)",
                ticker.current_account() or "?", unhealthy_count, _threshold(),
            )

            # Under threshold: keep prodding the SAME account. _try_start_ticker
            # is idempotent — no-op if the socket is already connected. This
            # covers the transient-blip case where Twisted's own reconnect
            # loop catches up within one watchdog tick.
            if unhealthy_count < _threshold():
                _try_start_ticker()
                continue

            # Boot grace suppresses swap decisions; keep retrying primary
            # until the grace window expires. Prevents a swap fire while
            # rebuild_from_db is still minting the primary's token.
            if in_grace:
                _try_start_ticker()
                continue

            # ── Phase 4: swap eligibility ───────────────────────────────
            # Ping-pong prevention: enforce a minimum window between
            # consecutive swaps. Use the ticker's own swap history so
            # cross-restart state doesn't leak (Kite tokens are per-
            # session; a swap on a fresh boot is fine even if the
            # previous process just swapped).
            if ticker.swaps_since(_cooldown_s()) > 0:
                last_swap = ticker.last_swap_at()
                since_last = (time.time() - last_swap) if last_swap else 0.0
                log.info(
                    "ticker_watchdog: swap suppressed by cooldown "
                    "(%.0f s since last)", since_last,
                )
                continue

            current = ticker.current_account() or ""
            eligible = _kite_failover_list(exclude={current})
            eligible = [
                a for a in eligible
                if not ticker.is_account_in_failover_cooloff(a, _cooldown_s())
            ]

            next_account = eligible[0] if eligible else None
            if next_account is None:
                if not unavailable_mode:
                    unavailable_mode = True
                    log.critical(
                        "ticker_watchdog: all Kite accounts unhealthy — "
                        "LTP REST poll fallback active (slowed cadence %.0fs)",
                        _slowed_interval_s(),
                    )
                # Twisted reactor dead: the reactor stopped on its own and
                # cannot be restarted in this process. Exit so systemd
                # (Restart=always, RestartSec=5) spawns a fresh process with
                # a clean reactor state. This is the only safe recovery path.
                if ticker.is_reactor_dead():
                    import sys
                    log.critical(
                        "ticker_watchdog: Twisted reactor is dead and all Kite "
                        "accounts are unavailable — exiting for systemd restart "
                        "(Restart=always, RestartSec=5s)"
                    )
                    sys.exit(1)
                # Keep trying the primary at the slowed cadence.
                _try_start_ticker()
                continue

            api_key, access_token = _resolve_kite_creds(next_account)
            if not (api_key and access_token):
                log.warning(
                    "ticker_watchdog: %s picked as next failover but has no "
                    "live token — skipping this cycle",
                    next_account,
                )
                continue

            ok = ticker.restart_with_account(api_key, access_token, next_account)
            _write_ticker_swap_audit(current, next_account, ok)
            if ok:
                log.info(
                    "ticker_watchdog: failover %s → %s (started, awaiting connect)",
                    current or "?", next_account,
                )
                unavailable_mode = False
            else:
                log.warning(
                    "ticker_watchdog: failover to %s did not start",
                    next_account,
                )

        except asyncio.CancelledError:
            return
        except Exception:
            # Don't let the watchdog die on a transient error; log and
            # continue. The next iteration will retry.
            log.exception("ticker_watchdog: loop error")


def _write_ticker_swap_audit(prev_account: str, next_account: str, ok: bool) -> None:
    """Fire an audit-log row for a ticker failover.

    Conn_service doesn't own the audit DB session directly; we call
    the shared `write_audit_event` helper which schedules an async
    task on the running loop. On failure (loop closed, DB down) the
    helper logs a warning and drops the row silently — the system
    stays correct without it.
    """
    try:
        from backend.api.audit import write_audit_event
        summary = f"{prev_account or '?'} → {next_account}" + (
            "" if ok else " (start failed)"
        )
        write_audit_event(
            category="ticker.swap",
            action="auto_failover",
            actor_username="conn_service",
            actor_role="system",
            target_type="broker_account",
            target_id=next_account,
            summary=summary,
        )
    except Exception:
        # Audit write must never break the watchdog — never even warn.
        pass


async def _init_connections_on_startup(app: Litestar) -> None:
    """Build the Connections singleton on service boot.

    Called by the Litestar startup hook. Loads broker_accounts from
    the DB and constructs each KiteConnection / DhanConnection /
    GrowwConnection. After this returns, fetch_* calls in routes.py
    can use the loaded singleton.
    """
    from backend.brokers.connections import Connections

    log = logger
    log.info("conn_service: rebuilding Connections singleton on startup")
    try:
        await Connections().rebuild_from_db()
        log.info(
            "conn_service: Connections ready · accounts=%s",
            sorted(Connections().conn.keys()),
        )
    except Exception as e:
        # Don't refuse to start — partial connectivity (e.g. Dhan
        # rate-limited at cold-start) is fine; subsequent fetch calls
        # will retry through the existing Connections-level retry
        # logic. Log loudly so the operator sees the boot state.
        log.exception("conn_service: rebuild_from_db failed: %s", e)


# Module-level `app` is the uvicorn entrypoint.
app = create_app()
