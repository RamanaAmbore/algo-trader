import contextlib
import fcntl
import json
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import contextvars
import socket

import urllib3.util.connection
import requests
from requests.adapters import HTTPAdapter
from kiteconnect import KiteConnect

# IPv4 vs IPv6 — the server's outbound IPv6 routing works for Kite
# (api.kite.trade supports v6 and accounts are whitelisted on
# per-IP-address IPv6s) but hangs for other hosts (Gemini, Google
# publisher feeds). We want IPv4 by default everywhere AND IPv6
# specifically for the per-account-bound Kite calls.
#
# Earlier code achieved this by toggling the module-global
# `urllib3.util.connection.HAS_IPV6` flag from `_IPv6SourceAdapter.
# send()` — set True at entry, reset to False at exit. That was
# RACY: if two threads called Kite (or Kite + Gemini) concurrently,
# Thread A's True-set leaked into Thread B's connection
# establishment, mid-flight resets corrupted family selection, and
# the IPv6-source-bound call could end up using the server's
# default IPv4 source. Kite then sees an unwhitelisted IP and
# returns "Insufficient permission for that call" — the exact bug
# the user reported.
#
# New design: keep HAS_IPV6=False globally (default IPv4 only),
# patch `allowed_gai_family` to consult a ContextVar instead. Each
# IPv6-source adapter sets the ContextVar before super().send() and
# resets after. ContextVar scope is per-task / per-thread, so
# overlapping calls can't trip on each other's overrides. urllib3's
# socket creation reads the patched function, which returns
# AF_UNSPEC when the override is set and AF_INET otherwise.
_IPV6_FAMILY_OVERRIDE: contextvars.ContextVar[bool] = (
    contextvars.ContextVar('ramboq_ipv6_family_override', default=False)
)

urllib3.util.connection.HAS_IPV6 = False

_orig_allowed_gai_family = urllib3.util.connection.allowed_gai_family
def _ramboq_allowed_gai_family():
    # When an IPv6-source adapter is mid-request in this context, return
    # AF_UNSPEC so urllib3 honours the IPv6 source_address binding.
    # Otherwise force AF_INET — protects unrelated outbound calls.
    if _IPV6_FAMILY_OVERRIDE.get():
        return socket.AF_UNSPEC
    return socket.AF_INET
urllib3.util.connection.allowed_gai_family = _ramboq_allowed_gai_family

from backend.shared.helpers.date_time_utils import timestamp_indian
from backend.shared.helpers.decorators import retry_kite_conn
from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.singleton_base import SingletonBase
from backend.shared.helpers.utils import generate_totp, secrets, config

# Token cache — shared across processes via a single file on disk so a
# successful login from prod's process is reused by dev (and vice versa)
# instead of each starting their own login flow against the same Kite app.
# Coordination uses fcntl.flock on a per-account `.lock` file in the same
# directory so two processes can't run the login critical section at the
# same time. Without this Kite invalidates the older session whenever the
# newer one logs in for the same app key, which used to manifest as ~5
# minutes of 401s on every endpoint.
#
# Default path is `/opt/ramboq/.log/kite_tokens.json` — reachable by both
# `/opt/ramboq` (prod) and `/opt/ramboq_dev` (dev) since both services run
# as `www-data` on the same server. Override with the
# `RAMBOQ_KITE_TOKEN_CACHE` env var when running locally or in any layout
# where the prod path doesn't exist.
_DEFAULT_TOKEN_CACHE = '/opt/ramboq/.log/kite_tokens.json'
_FALLBACK_TOKEN_CACHE = (
    Path(__file__).resolve().parent.parent.parent.parent / '.log' / 'kite_tokens.json'
)
_env_path = os.environ.get('RAMBOQ_KITE_TOKEN_CACHE')
if _env_path:
    _TOKEN_CACHE_PATH = Path(_env_path)
elif Path(_DEFAULT_TOKEN_CACHE).parent.is_dir() or Path(_DEFAULT_TOKEN_CACHE).exists():
    _TOKEN_CACHE_PATH = Path(_DEFAULT_TOKEN_CACHE)
else:
    # Local dev / any environment where the prod path doesn't exist —
    # fall back to the per-process .log/ directory.
    _TOKEN_CACHE_PATH = _FALLBACK_TOKEN_CACHE


@contextlib.contextmanager
def _cross_process_login_lock(account: str):
    """
    Cross-process exclusive lock keyed by account. Pairs with each
    KiteConnection's in-process `_login_lock` to keep parallel logins
    serialized both within a process AND across processes (prod + dev
    sharing the same Kite app keys). The lock file lives next to the
    token cache; opening it in append mode is safe even if the file
    doesn't exist yet — `flock` works on the file descriptor.
    """
    lock_path = _TOKEN_CACHE_PATH.with_suffix(f'.{account}.lock')
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    fp = None
    try:
        fp = open(lock_path, 'a+')
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        if fp is not None:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            fp.close()


class _IPv6SourceAdapter(HTTPAdapter):
    """Force IPv6 with a specific source address for KiteConnect API calls.
    Used when an account needs a different IP than the server's default IPv4.
    Only applied to KiteConnect.reqsession (api.kite.trade), never to login.

    The IPv6 family selection is request-scoped via the
    `_IPV6_FAMILY_OVERRIDE` ContextVar — set at the start of send()
    and reset on exit. ContextVars are per-task/per-thread so two
    accounts calling Kite concurrently can't race each other or
    leak the override into an unrelated Gemini / Google call.
    """
    def __init__(self, source_ip, **kwargs):
        self._source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['source_address'] = (self._source_ip, 0)
        super().init_poolmanager(*args, **kwargs)

    def send(self, request, *args, **kwargs):
        # Set the IPv6-family override for this request's scope.
        # `_ramboq_allowed_gai_family` (installed at import time) reads
        # this ContextVar and returns AF_UNSPEC for the duration of
        # the request so urllib3 honours the IPv6 source_address.
        token = _IPV6_FAMILY_OVERRIDE.set(True)
        try:
            return super().send(request, *args, **kwargs)
        finally:
            _IPV6_FAMILY_OVERRIDE.reset(token)


# ── Groww source-IP binding ────────────────────────────────────────
#
# The Groww SDK (`growwapi.groww.client`) uses module-level
# `requests.get/post/put` calls — there's no `session` attribute on
# the `GrowwAPI` instance we can mount an adapter on. So we patch the
# `requests` reference inside the SDK's own module namespace at
# import time: every `requests.get(...)` inside the SDK becomes a
# call into `_RequestsProxy.get(...)` which reads the per-thread
# source IP from a ContextVar and routes the call through a
# source-bound `requests.Session`.
#
# The patcher is idempotent — multiple `GrowwConnection` instances on
# different IPs install it once; each call's source IP is bound via
# the ContextVar set inside `GrowwConnection`'s SDK callers (the
# `GrowwBroker._with_source_bind` decorator). Threads using different
# source IPs concurrently don't fight because each sees its own
# ContextVar value.

_GROWW_SOURCE_IP_OVERRIDE: contextvars.ContextVar[Optional[str]] = (
    contextvars.ContextVar("_GROWW_SOURCE_IP_OVERRIDE", default=None)
)
_GROWW_BOUND_SESSIONS: dict[str, Any] = {}
_GROWW_SESSION_LOCK = threading.Lock()
_GROWW_PATCHED = False


def _get_bound_session_for_ip(source_ip: str):
    """Return a `requests.Session` whose HTTPS adapter is bound to
    `source_ip`. Sessions are pooled per source IP so we don't burn
    a new TCP pool per call."""
    with _GROWW_SESSION_LOCK:
        sess = _GROWW_BOUND_SESSIONS.get(source_ip)
        if sess is not None:
            return sess
        new_sess = requests.Session()
        try:
            adapter = _IPv6SourceAdapter(source_ip)
            new_sess.mount("https://", adapter)
            new_sess.mount("http://", adapter)
        except Exception as e:
            logger.warning(
                f"Groww source-bound session for {source_ip!r} "
                f"failed to mount adapter ({e})"
            )
        _GROWW_BOUND_SESSIONS[source_ip] = new_sess
        return new_sess


def _install_groww_source_binding() -> None:
    """Patch the `requests` reference inside `growwapi.groww.client`'s
    module namespace so module-level `requests.get/post/put` calls
    inside the SDK route through a per-thread source-bound session.

    Idempotent — repeated calls no-op. Safe to call from every
    `GrowwConnection.__init__`."""
    global _GROWW_PATCHED
    if _GROWW_PATCHED:
        return
    try:
        from growwapi.groww import client as _groww_client_mod  # type: ignore
    except Exception as e:
        logger.warning(
            f"Groww source-binding: SDK import failed ({e}); skipping patch"
        )
        return

    class _RequestsProxy:
        """Wraps the `requests` module so module-level calls inside the
        SDK route through a source-bound session when a per-thread IP
        override is in effect. Falls back to plain `requests` when no
        override is set (e.g. a stray helper that doesn't go through a
        GrowwConnection — preserves existing semantics)."""

        # Expose attribute access to the real `requests` module so SDK
        # code that does `requests.Timeout`, `requests.Response`, etc.
        # continues to work unmodified.
        def __getattr__(self, name):
            return getattr(requests, name)

        def _route(self, method, url, **kwargs):
            ip = _GROWW_SOURCE_IP_OVERRIDE.get()
            if not ip:
                return getattr(requests, method)(url, **kwargs)
            sess = _get_bound_session_for_ip(ip)
            return getattr(sess, method)(url, **kwargs)

        def get(self, url, **kwargs):
            return self._route("get", url, **kwargs)

        def post(self, url, **kwargs):
            return self._route("post", url, **kwargs)

        def put(self, url, **kwargs):
            return self._route("put", url, **kwargs)

        def delete(self, url, **kwargs):
            return self._route("delete", url, **kwargs)

    _groww_client_mod.requests = _RequestsProxy()  # type: ignore[attr-defined]
    _GROWW_PATCHED = True
    logger.info(
        "Groww source-binding installed: SDK module-level requests calls "
        "now route through per-thread source-bound sessions"
    )



# Resolved at every retry-decorator entry so live changes from
# /admin/settings → connections.retry_count take effect on the next
# call without a restart. Falls back to YAML, then 3.
def _retry_count() -> int:
    from backend.shared.helpers.settings import get_int
    return get_int("connections.retry_count",
                   int(config.get("retry_count", 3)))

CONN_RESET_HOURS = int(config['conn_reset_hours'])

logger = get_logger(__name__)


# File-system lock around the shared token cache file. The
# per-account login locks (`_cross_process_login_lock`) only serialise
# mint *calls*; this serialises the read-modify-write of the cache
# JSON itself. Both prod and dev mint tokens for different accounts
# into the same `kite_tokens.json` / `groww_tokens.json` — without
# this, a fast-enough save by prod while dev is mid-write could lose
# either side's update.
@contextlib.contextmanager
def _cache_file_lock(shared: bool = False):
    """Acquire an advisory lock on a sibling .lock file. `shared=True`
    grants a read lock (multiple readers, no writers); `shared=False`
    grants an exclusive lock (one writer, no readers). The lock is
    released when the context exits."""
    lock_path = _TOKEN_CACHE_PATH.with_suffix('.cache.lock')
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    fp = None
    try:
        fp = open(lock_path, 'a+')
        fcntl.flock(fp.fileno(),
                    fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        if fp is not None:
            try:
                fcntl.flock(fp.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            fp.close()


def _load_cached_token(account: str) -> tuple[str | None, datetime | None]:
    """Load a cached access token for an account. Returns
    (token, created_at) or (None, None). Reads under a shared lock so
    a concurrent writer's read-modify-write doesn't surface a
    half-written file."""
    try:
        with _cache_file_lock(shared=True):
            if not _TOKEN_CACHE_PATH.exists():
                return None, None
            data = json.loads(_TOKEN_CACHE_PATH.read_text())
        entry = data.get(account)
        if not entry:
            return None, None
        created = datetime.fromisoformat(entry['created_at'])
        age = datetime.now(timezone.utc) - created
        if age > timedelta(hours=CONN_RESET_HOURS):
            return None, None  # expired
        return entry['access_token'], created
    except Exception as e:
        logger.debug(f"Token cache read failed for {account}: {e}")
        return None, None


def _save_cached_token(account: str, access_token: str) -> None:
    """Persist an access token for an account. Empty token removes
    the entry. The whole read-modify-write happens under an exclusive
    lock and the final write is atomic (tempfile + os.replace), so:
      * a partial / crash-interrupted write can never surface to a reader
      * two concurrent writers for different accounts can't lose updates
      * a writer never overwrites a fresher entry written between the
        read and the write."""
    try:
        _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _cache_file_lock(shared=False):
            data = {}
            if _TOKEN_CACHE_PATH.exists():
                try:
                    data = json.loads(_TOKEN_CACHE_PATH.read_text())
                except json.JSONDecodeError:
                    # Corrupt file (interrupted legacy write?) — start
                    # fresh rather than crashing. Other accounts will
                    # re-mint on their next call.
                    logger.warning(
                        f"Token cache file unparseable; recreating: "
                        f"{_TOKEN_CACHE_PATH}"
                    )
                    data = {}
            if not access_token:
                data.pop(account, None)
            else:
                data[account] = {
                    'access_token': access_token,
                    'created_at':  datetime.now(timezone.utc).isoformat(),
                }
            # Atomic write — POSIX rename is atomic, so a reader either
            # sees the old file or the new file, never a partial.
            tmp_path = _TOKEN_CACHE_PATH.with_suffix(
                _TOKEN_CACHE_PATH.suffix + '.tmp')
            tmp_path.write_text(json.dumps(data, indent=2))
            os.replace(tmp_path, _TOKEN_CACHE_PATH)
    except Exception as e:
        logger.debug(f"Token cache write failed for {account}: {e}")


class KiteConnection:
    """Singleton class to handle Kite API authentication and access token management."""

    def __init__(self, account, secrets):

        self.account = account

        credentials = secrets['kite_accounts'][account]

        self._password = credentials['password']
        self.api_key = credentials["api_key"]
        self._api_secret = credentials["api_secret"]
        self.totp_token = credentials['totp_token']
        self._source_ip = credentials.get('source_ip', None)

        self.login_url = secrets['kite_login_url']
        self.twofa_url = secrets['kite_twofa_url']
        self._access_token = None

        self._initialized = True

        # Serialises re-auth so two threads that discover the cached
        # token is invalid at the same moment don't race to log in —
        # Kite rejects parallel login()s for the same app key and can
        # invalidate BOTH tokens, which then forces a full re-auth
        # cycle (~5 min per account) for every caller in the window.
        self._login_lock = threading.Lock()

        self.kite = self._new_kite()

        # Login session uses plain requests (no source_ip binding).
        # Login goes to kite.zerodha.com which doesn't need IP whitelisting.
        # Only KiteConnect API calls (api.kite.trade) need the source IP.
        self.session = requests.Session()

        # track connection creation time
        self._conn_created_at = None

        # Try to restore from cached token (avoids full login on restart)
        self._try_restore_token()

    def _try_restore_token(self):
        """Try to restore access token from cache. If valid, skip full login."""
        token, created = _load_cached_token(self.account)
        if token:
            self.kite = self._new_kite()
            self.kite.set_access_token(token)
            self._access_token = token
            self._conn_created_at = timestamp_indian()
            logger.info(f"Restored cached token for {self.account} (age: "
                        f"{(datetime.now(timezone.utc) - created).seconds // 3600}h)")

    def _new_kite(self):
        """Create a KiteConnect instance, with IPv6 source binding if configured."""
        kite = KiteConnect(api_key=self.api_key)
        if self._source_ip and ':' in self._source_ip and hasattr(kite, 'reqsession'):
            adapter = _IPv6SourceAdapter(self._source_ip)
            kite.reqsession.mount('https://', adapter)
            kite.reqsession.mount('http://', adapter)
        return kite

    def init_kite_conn(self, test_conn=False):
        """Returns KiteConnect instance, initializing it if necessary."""

        if not test_conn:
            return

        self.kite = self._new_kite()
        request_id = self.login()

        self.totp_authenticate(request_id)

        kite_url = self.kite.login_url()
        logger.info("Kite login URL received.")
        request_token = self._extract_request_token(kite_url)
        if not request_token:
            raise RuntimeError("Failed to extract request_token from Kite redirect")
        logger.info(f"Request Token received: {request_token}")
        self.setup_access_token(request_token)

    def _extract_request_token(self, kite_url):
        """Extract request_token from Kite OAuth redirect.

        The Kite redirect URL must point to a non-running port (e.g.
        http://localhost:8080) so the redirect always fails with a
        ConnectionError. The request_token is extracted from the error
        message which contains the full redirect URL.

        IMPORTANT: The Kite redirect URL in the developer console MUST use
        a port that is NOT running on the server (8080 recommended).
        Using a port that IS running (e.g. 8000/8001) causes SSL hangs.
        """
        try:
            self.session.get(kite_url)
        except Exception as e:
            err = str(e)
            if 'request_token=' in err:
                try:
                    return err.split("request_token=")[1].split("&")[0].split()[0]
                except (IndexError, ValueError):
                    pass
        return None

    def _is_kite_conn_expired(self, now) -> bool:
        """True when the Kite session timestamp is absent or past CONN_RESET_HOURS."""
        return (
            self._conn_created_at is None
            or now - self._conn_created_at > timedelta(hours=CONN_RESET_HOURS)
        )

    def _validate_or_clear_kite_token(self) -> bool:
        """Try a lightweight profile() call to validate the cached token.

        Returns True when the token is valid (caller can return self.kite).
        Clears the token and disk cache when invalid.
        """
        if not self._access_token:
            self._try_restore_token()
        if not self._access_token:
            return False
        try:
            self.kite.profile()
            return True
        except Exception as e:
            logger.warning(f"Cached token invalid for {self.account}: {e}")
            self._access_token = None
            _save_cached_token(self.account, '')
            return False

    @retry_kite_conn(_retry_count)
    def get_kite_conn(self, test_conn=False):
        """Return kite connection, refreshing if older than CONN_RESET_HOURS.

        Re-auth (cache-probe + full login) runs under `_login_lock` so
        concurrent callers can't race two login() + 2FA flows at once.
        Kite rejects parallel logins for the same app and invalidates
        both tokens — the symptom was ~5 min of 401s on every request
        until the retry loop cleared.
        """
        now = timestamp_indian()
        if not (self._is_kite_conn_expired(now) or test_conn):
            return self.kite

        with self._login_lock, _cross_process_login_lock(self.account):
            now = timestamp_indian()
            if not (self._is_kite_conn_expired(now) or test_conn):
                return self.kite
            if self._is_kite_conn_expired(now):
                self._conn_created_at = now
                logger.info(f"Kite connection refreshed at "
                            f"{now.strftime('%a, %b %d, %Y, %I:%M %p')}")
            if self._validate_or_clear_kite_token():
                return self.kite
            self.init_kite_conn(test_conn=True)
            return self.kite

    @retry_kite_conn(_retry_count)
    def login(self):
        try:
            response = self.session.post(self.login_url, data={"user_id": self.account, "password": self._password})
            response.raise_for_status()
            request_id = response.json()["data"]["request_id"]
            logger.info(f"Login successful, Request ID: {request_id}")
        except Exception as e:
            logger.error(f"Login failed: {e}")
            _emit_conn_event(
                self.account, "zerodha_kite", "auth_fail",
                {"error": str(e)[:200], "stage": "login"},
            )
            raise
        return request_id

    @retry_kite_conn(_retry_count)
    def totp_authenticate(self, request_id):
        try:
            totp = generate_totp(self.totp_token)
            response = self.session.post(self.twofa_url,
                                         data={"user_id": self.account, "request_id": request_id, "twofa_value": totp})
            response.raise_for_status()
            logger.info("2FA authentication successful")
        except Exception as e:
            logger.error(f"2FA authentication failed: {e}")
            _emit_conn_event(
                self.account, "zerodha_kite", "auth_fail",
                {"error": str(e)[:200], "stage": "totp_authenticate"},
            )
            raise

    @retry_kite_conn(_retry_count)
    def setup_access_token(self, request_token):
        try:
            self.kite = self._new_kite()
            session_data = self.kite.generate_session(request_token, api_secret=self._api_secret)
            self._access_token = session_data["access_token"]
            self.kite.set_access_token(self._access_token)
            _save_cached_token(self.account, self._access_token)
            logger.info(f"Token cached for {self.account}")
            _emit_conn_event(self.account, "zerodha_kite", "token_ok")
        except Exception as e:
            logger.error(f"Failed to generate access token for account {self.account}: {e}")
            _emit_conn_event(
                self.account, "zerodha_kite", "auth_fail",
                {"error": str(e)[:200], "stage": "setup_access_token"},
            )
            raise

    def get_access_token(self):
        return self._access_token

    @property
    def api_secret(self) -> str:
        """Public accessor for the Kite API secret.

        Exposes the private `_api_secret` via a stable property so callers
        aren't depending on the private attribute name.  If the SDK ever
        renames the underlying field we get a clear AttributeError on a
        defined name rather than a silent breakage.
        """
        return self._api_secret


class DhanConnection:
    """Dhan client wrapper with headless TOTP-based auto-login.

    Mirrors KiteConnection's lifecycle: long-lived credentials
    (`client_id + pin + totp_token`, all ~1-year valid) are
    persisted in `broker_accounts`; this class mints a Dhan access
    token on first use and re-mints when the token expires.

    Login flow (direct REST POST, no browser, no SMS/email OTP):

      POST https://auth.dhan.co/app/generateAccessToken
        ?dhanClientId=<id>&pin=<pin>&totp=<code>
      → {"accessToken": "..."}  (sometimes wrapped under "data")

      where `totp_code = pyotp.TOTP(totp_seed).now()` from the stored
      base32 seed. The token's validity is whatever the operator has
      set in the Dhan dashboard's "Token validity" dropdown (default
      24 h; settable 5 min / 1 hr / 24 hr / 30 d / 1 yr at Settings
      → DhanHQ Trading APIs → Token validity).

    The Partner-API consent flow (generate_login_session →
    consume_token_id) is intentionally NOT used — Dhan v2 moved that
    behind browser-based SMS/email-OTP + PIN consent which kills any
    unattended automation. The direct REST endpoint above is the
    only headless path.

    `dhanhq.auth.DhanLogin` is bypassed too — its `generate_token` /
    `renew_token` methods use module-level `requests.post`/
    `requests.get` calls with no session hook, so we'd have no way
    to mount the IPv6 source-binding adapter (see below). We call
    the same two REST endpoints directly via `_login_session()`
    which returns a `requests.Session` with the per-account
    source_ip adapter pre-mounted.

    Access tokens are cached to `dhan_tokens.json` (next to
    `kite_tokens.json`) so a restart within the validity window skips
    the login flow entirely. Cross-process lock (same pattern as
    Kite) prevents prod + dev from racing two parallel logins.

    Rate-limit guard: Dhan's `generate_token` caps at one call per
    2 minutes per account. After a failed login we set
    `_login_blocked_until = now + 130 s` so a burst of auth-fail
    callers doesn't hammer the limit; the cool-off check sits in
    `get_dhan_conn()`.

    IPv6 source-binding: Dhan doesn't whitelist source IPs the way
    Kite does, but the v2 auth backend enforces "one active token
    per partner app per source IP". In a multi-Dhan-account
    deployment routed through the server's default outgoing IPv4,
    every successful login from one account invalidates the prior
    token of every other account — the operator sees a 3-minute
    token rotation loop in the prod log (`DH-906: Invalid Token`
    alternating between accounts). Mitigation: bind each Dhan
    account to its own IPv6 from the server's /48 subnet (same
    `2a02:4780:12:9e1d::N` pattern as Kite). `source_ip` is mounted
    on BOTH the login session (generate_token / renew_token) and
    the runtime SDK session (positions / holdings / orders /
    margins / quote) so the per-IP session affinity is preserved
    end-to-end.

    `api_key` / `api_secret` are also accepted on the constructor for
    historical compatibility with the retired Partner-API flow; the
    direct TOTP path doesn't read them. Future code paths that need
    them (e.g. Partner-API-only endpoints) can still pull them from
    `self._api_key` / `self._api_secret`.
    """

    def __init__(self, account: str, *,
                 client_id: str, api_key: str, api_secret: str,
                 pin: str, totp_token: str,
                 source_ip: str | None = None) -> None:
        self.account       = account
        self.client_id     = client_id
        self._api_key      = api_key
        self._api_secret   = api_secret
        self._pin          = pin
        self._totp_token   = totp_token
        self._source_ip    = source_ip
        self._access_token: str | None = None
        self._conn_created_at: datetime | None = None
        self._dhan         = None
        self._import_error = None
        self._login_lock   = threading.Lock()
        # Login-rate-limit cool-off — Dhan's `generate_token` endpoint
        # caps at one call per 2 minutes per account; when we hit that
        # cap, every subsequent call within the window also fails. We
        # gate re-login attempts behind this timestamp so a burst of
        # auth-fail callers from broker_apis don't hammer the rate
        # limit. Set after a failed _do_login; checked before retrying.
        self._login_blocked_until: float = 0.0
        # Try to restore from on-disk cache so a restart within the
        # 23 h window doesn't re-run the login dance.
        self._try_restore_token()

    # ── Token cache ──────────────────────────────────────────────────
    # Reuses the Kite token cache file layout — separate top-level
    # account key prefixed with `dhan:` to avoid collision with Kite
    # accounts. Both prod + dev write to the same file (locked).

    def _cache_key(self) -> str:
        return f"dhan:{self.account}"

    def _try_restore_token(self) -> None:
        token, created = _load_cached_token(self._cache_key())
        if token:
            self._access_token   = token
            self._conn_created_at = timestamp_indian()
            self._build_client(token)
            logger.info(f"Restored cached Dhan token for {self.account} "
                        f"(age: {(datetime.now(timezone.utc) - created).seconds // 3600}h)")

    def _save_token(self, token: str) -> None:
        _save_cached_token(self._cache_key(), token)

    # ── SDK client construction ──────────────────────────────────────

    def _build_client(self, access_token: str) -> None:
        """Construct the `dhanhq` runtime client from a known-good
        access_token. SDK import is deferred so the module is loadable
        without dhanhq installed (deploys land in stages).

        After construction, mount the per-account IPv6 source-binding
        adapter on the SDK's internal `requests.Session` so every
        runtime call (positions / holdings / orders / margins / quote)
        egresses from this account's dedicated source IP. Without
        this, multi-Dhan-account deployments hit Dhan's "one active
        token per partner app per source IP" semantic and tokens
        rotate every few minutes — see the `Dhan rotation pattern`
        diagnostic in dhan.py for the symptom shape.
        """
        try:
            from dhanhq import dhanhq  # type: ignore[import-not-found]
        except ImportError as e:
            logger.error(
                f"dhanhq SDK not installed; run `pip install dhanhq`. "
                f"Account {self.account!r} will stay inactive until "
                f"the dependency is available."
            )
            self._dhan = None
            self._import_error = e
            return
        try:
            from dhanhq import DhanContext  # type: ignore[import-not-found]
            ctx = DhanContext(self.client_id, access_token)
            self._dhan = dhanhq(ctx)
            # Mount source-binding adapter on the SDK's internal session.
            # DhanHTTP exposes its session as `dhan_http.session` (verified
            # against dhanhq 2.x source). Skipped when no source_ip is
            # configured — falls back to the OS default route.
            self._mount_source_ip_adapter(getattr(ctx, "dhan_http", None))
        except ImportError:
            # dhanhq 1.x fallback (positional args). Older shape has the
            # session at `self._dhan._http` or similar; best-effort mount.
            self._dhan = dhanhq(self.client_id, access_token)
            self._mount_source_ip_adapter(
                getattr(self._dhan, "dhan_http", None)
                or getattr(self._dhan, "_http", None)
            )
        self._import_error = None

    def _mount_source_ip_adapter(self, http_holder: Any) -> None:
        """Replace `http_holder.session`'s default HTTPS adapter with an
        `_IPv6SourceAdapter` bound to this account's source_ip. No-op
        when source_ip is unset or the holder doesn't expose `session`
        (defensive against future SDK refactors).
        """
        if not self._source_ip:
            return
        if http_holder is None:
            logger.warning(
                f"Dhan {self.account!r}: SDK doesn't expose dhan_http "
                f"holder; source_ip binding skipped. Token rotation "
                f"pattern may persist."
            )
            return
        session = getattr(http_holder, "session", None)
        if session is None:
            logger.warning(
                f"Dhan {self.account!r}: SDK http holder has no session "
                f"attribute; source_ip binding skipped."
            )
            return
        try:
            adapter = _IPv6SourceAdapter(self._source_ip)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            logger.info(
                f"Dhan {self.account!r}: source_ip {self._source_ip} "
                f"bound to SDK runtime session"
            )
        except Exception as e:
            logger.warning(
                f"Dhan {self.account!r}: failed to mount source_ip "
                f"adapter ({e}); falling back to default route"
            )

    # ── Login flow ───────────────────────────────────────────────────

    def _login_session(self):
        """Build a fresh `requests.Session` for the Dhan login call,
        with the per-account IPv6 source-binding adapter mounted when
        source_ip is configured. Used for both `_do_login`
        (generate_token) and `_try_renew` (renew_token) so the auth
        endpoint sees the same source IP as our runtime SDK calls —
        a prerequisite for Dhan's per-IP session tracking to recognise
        login + subsequent calls as one logical session.

        Bypasses `dhanhq.auth.DhanLogin` (which uses module-level
        `requests.post`/`requests.get` and offers no session hook).
        We call the same two REST endpoints directly so the source-
        binding adapter is the canonical path, not a monkey-patched
        sidestep that could leak into unrelated HTTP calls.
        """
        import requests  # local import — keeps module loadable without it
        session = requests.Session()
        if self._source_ip:
            try:
                adapter = _IPv6SourceAdapter(self._source_ip)
                session.mount("https://", adapter)
                session.mount("http://", adapter)
            except Exception as e:
                logger.warning(
                    f"Dhan {self.account!r}: login source_ip mount failed "
                    f"({e}); falling back to default route"
                )
        return session

    _DHAN_AUTH_BASE = "https://auth.dhan.co"
    _DHAN_API_BASE  = "https://api.dhan.co/v2"

    def _do_login(self) -> str:
        """Mint a fresh Dhan access_token end-to-end programmatically.

        POST `https://auth.dhan.co/app/generateAccessToken?dhanClientId=…
        &pin=…&totp=…` — no browser, no SMS/email OTP, no consent flow.
        Validity is whatever the Dhan dashboard's "Token validity"
        dropdown is set to (24 h default; can be extended to 30 d / 1 yr).

        The OLD path (Partner-API consent flow — generate_login_session →
        consume_token_id) was retired because Dhan v2 moved that flow
        behind browser-based SMS/email-OTP + PIN consent. The direct
        endpoint here is what the SDK actually exposes for headless use.

        Source-binding: when `source_ip` is configured on this account,
        the login POST egresses from that IP. Critical for multi-Dhan-
        account deployments where Dhan's per-IP session tracking would
        otherwise invalidate the previously-issued token every time a
        peer account logs in from the same default IP.
        """
        if not all([self.client_id, self._pin, self._totp_token]):
            raise RuntimeError(
                f"Dhan account {self.account!r} needs client_id + PIN + "
                f"TOTP seed for headless auth. Fill them in /admin/brokers."
            )

        totp_code = generate_totp(self._totp_token)
        session = self._login_session()
        url = f"{self._DHAN_AUTH_BASE}/app/generateAccessToken"
        params = {
            "dhanClientId": self.client_id,
            "pin":          self._pin,
            "totp":         totp_code,
        }
        try:
            response = session.post(url, params=params, timeout=30)
            resp = response.json() if response.content else {}
        except Exception as e:
            raise RuntimeError(
                f"Dhan generate_token HTTP call failed: {e}"
            ) from e

        # Response shape: {"accessToken": "..."} (sometimes wrapped under
        # "data"). Tolerate both for resilience against minor API changes.
        access_token = None
        if isinstance(resp, dict):
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            access_token = (data.get("accessToken")
                            or data.get("access_token"))
        if not access_token:
            raise RuntimeError(
                f"Dhan generate_token returned no accessToken: {resp!r}"
            )
        return str(access_token)

    def _try_renew(self) -> str | None:
        """Best-effort token refresh via `GET /v2/RenewToken`. Lets a
        still-valid-but-close-to-expiring token roll forward without
        re-entering PIN+TOTP. Returns the new token or None on any
        failure (older API, missing token, network blip, …).

        Same source-binding as `_do_login` — the renewal HTTP call
        egresses from this account's source_ip so the per-IP session
        affinity is preserved.

        Operator: "why did auth expire. with kite we are getting
        renewing the tokens. same mechanism is used for dhan." —
        renewal was silently failing for Dhan because the success path
        had no log line and the failure paths returned None without
        surfacing the response. Now every branch logs at warning level
        so the operator can see WHY a renewal fell through to
        `_do_login` (which mints a new token + invalidates the old
        and is rate-limited to once per 2 minutes per Dhan account).
        """
        if not self._access_token:
            logger.warning(
                f"Dhan renew_token skipped for {self.account!r}: "
                f"no current access_token cached"
            )
            return None
        session = self._login_session()
        url = f"{self._DHAN_API_BASE}/RenewToken"
        headers = {
            "access-token": self._access_token,
            "dhanClientId": self.client_id,
        }
        try:
            response = session.get(url, headers=headers, timeout=30)
            resp = response.json() if response.content else {}
        except Exception as e:
            logger.warning(
                f"Dhan renew_token HTTP failed for {self.account!r}: {e}"
            )
            return None
        if not isinstance(resp, dict):
            logger.warning(
                f"Dhan renew_token returned non-dict for {self.account!r}: "
                f"type={type(resp).__name__} body={str(resp)[:200]}"
            )
            return None
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        new_token = (data.get("accessToken")
                     or data.get("access_token"))
        if new_token:
            logger.info(
                f"Dhan renew_token success for {self.account!r}: "
                f"new token issued (no re-mint required)"
            )
            return str(new_token)
        # Renewal endpoint reached us but didn't return a token —
        # surface what it actually said. Common cases: 401 (token
        # already expired), 404 (endpoint changed), {"error": "..."}.
        logger.warning(
            f"Dhan renew_token no accessToken for {self.account!r}: "
            f"status={response.status_code} body={str(resp)[:200]}"
        )
        return None

    def _is_token_expired(self, now) -> bool:
        """True when the connection is older than CONN_RESET_HOURS or was
        never created."""
        return (
            self._conn_created_at is None
            or now - self._conn_created_at > timedelta(hours=CONN_RESET_HOURS)
        )

    def _check_recency_guard(self, now, test_conn: bool) -> bool:
        """Return True when the recent-token guard applies and the caller
        should skip the re-mint.

        When ``test_conn=True`` (caller hit DH-906 / "Invalid Token") but
        the cached token was minted in the last 60 s, another thread already
        minted a fresh token under this same lock — returning ``_dhan``
        avoids a cascade of concurrent re-mints, each invalidating the
        previous. Caller logs at INFO level when this fires.
        """
        return (
            test_conn
            and self._access_token is not None
            and self._conn_created_at is not None
            and self._dhan is not None
            and (now - self._conn_created_at) < timedelta(seconds=60)
        )

    def _check_login_rate_limit(self, test_conn: bool):
        """Enforce Dhan's 2-min generate_token rate-limit cool-off.

        Raises RuntimeError when inside the cool-off window and no cached
        client is available. Returns the cached client (or None) to signal
        the caller to skip the mint.  Caller must import ``time`` before
        invoking.
        """
        import time as _time_mod
        if _time_mod.time() >= self._login_blocked_until:
            return None  # not in cool-off — proceed to mint
        wait_s = int(self._login_blocked_until - _time_mod.time())
        if test_conn:
            # Token is known-dead; raise so the broker layer surfaces the
            # failure rather than returning empty DataFrames for 130 s.
            raise RuntimeError(
                f"Dhan login rate-limited for {self.account!r} — "
                f"token known dead; wait {wait_s}s before retrying"
            )
        if self._dhan is not None:
            logger.warning(
                f"Dhan login blocked for {self.account!r} "
                f"({wait_s}s left in rate-limit window); "
                f"returning last-known client"
            )
            return self._dhan
        raise RuntimeError(
            f"Dhan login rate-limited for {self.account!r} — "
            f"wait {wait_s}s before retrying"
        )

    def _mint_and_build(self) -> None:
        """Run _do_login → persist token → build SDK client → record login event.

        Must be called inside ``_login_lock`` + ``_cross_process_login_lock``.
        Sets ``_login_blocked_until`` on failure (130 s cool-off).
        """
        import time as _time_mod
        try:
            access_token = self._do_login()
        except RuntimeError as e:
            # 130 s = Dhan's 2-min limit + 10 s safety margin.
            self._login_blocked_until = _time_mod.time() + 130.0
            logger.error(
                f"Dhan _do_login failed for {self.account!r}: {e!s:.200} — "
                f"blocking re-login attempts for 130 s"
            )
            _emit_conn_event(
                self.account, "dhan", "auth_fail",
                {"error": str(e)[:200], "stage": "_do_login"},
            )
            raise
        # Success — clear any prior cool-off.
        self._login_blocked_until = 0.0
        self._access_token = access_token
        self._conn_created_at = timestamp_indian()
        self._save_token(access_token)
        self._build_client(access_token)
        # Side-channel: stamp this account's login moment in the
        # cross-account ledger so the rotation-pattern detector in
        # DhanBroker._safe_call can correlate "this account's token
        # died" with "that other account's recent login". Imported
        # lazily to avoid the connections ↔ brokers circular import.
        try:
            from backend.brokers.adapters.dhan import record_dhan_login_event
            record_dhan_login_event(self.account)
        except Exception:
            pass
        logger.info(
            f"Dhan login complete for {self.account} (token cached, valid ~24h)"
        )
        _emit_conn_event(self.account, "dhan", "token_ok")

    def _dhan_conn_under_lock(self, now, test_conn: bool):
        """Inner body of get_dhan_conn — runs under both login locks.

        Returns the cached client when no re-mint is needed.  Returns
        None when _mint_and_build() was called (caller must validate
        self._dhan after releasing the lock).
        """
        if not (self._is_token_expired(now) or test_conn) and self._dhan is not None:
            return self._dhan
        if not self._access_token:
            self._try_restore_token()
        if self._access_token and self._dhan is not None and not test_conn:
            return self._dhan
        if self._check_recency_guard(now, test_conn):
            logger.info(
                f"Dhan {self.account!r}: test_conn=True but token minted "
                f"<60 s ago — skipping re-mint to avoid invalidation race"
            )
            return self._dhan
        cached = self._check_login_rate_limit(test_conn)
        if cached is not None:
            return cached
        self._mint_and_build()
        return None  # signal: mint was attempted; validate after lock release

    def get_dhan_conn(self, test_conn: bool = False):
        """Return a ready dhanhq client.

        Mirrors `KiteConnection.get_kite_conn` — refreshes when older
        than CONN_RESET_HOURS, or whenever `test_conn=True`. Re-auth
        runs under the per-account login lock + the cross-process file
        lock so concurrent callers don't race two logins against the
        same Partner-API app.
        """
        now = timestamp_indian()
        if not (self._is_token_expired(now) or test_conn) and self._dhan is not None:
            return self._dhan

        with self._login_lock, _cross_process_login_lock(self._cache_key()):
            now = timestamp_indian()
            result = self._dhan_conn_under_lock(now, test_conn)
            if result is not None:
                return result

        if self._dhan is None:
            raise RuntimeError(
                f"DhanConnection for {self.account!r} failed to build "
                f"after login (dhanhq SDK missing?)"
            )
        return self._dhan

    def get_access_token(self) -> str | None:
        return self._access_token


class GrowwConnection:
    """Groww client wrapper. Holds a `growwapi.GrowwAPI(access_token)`
    handle. Two auth modes, tried in order:

      1. api_key + totp_seed (TOTP-based, programmatic refresh) —
         the SDK computes a fresh 6-digit code from the seed on every
         mint call; renewals are silent.
      2. access_token alone (legacy 24 h manual-refresh path —
         operator pastes a fresh token from Groww's developer
         dashboard when the current one expires)

    The approval-secret flow (`api_key + secret`) was retired:
    approval mints prompt the operator to OK the request in the Groww
    app/web every 24 h, which is incompatible with an unattended
    trading service.

    Mode 1 mints a token via `GrowwAPI.get_access_token(api_key,
    totp=<code>)` on first use, then caches it to disk
    (`.log/groww_tokens.json`) keyed by account. Cached tokens
    survive a service restart within the validity window. Path
    resolves per-deployment (prod under `/opt/ramboq/.log/`, dev
    under `/opt/ramboq_dev/.log/`); the fcntl file lock guards
    same-deployment multi-worker races.

    IPv6 source-binding: the SDK uses module-level `requests.get/
    post/put` calls (no session attribute on `GrowwAPI` we can
    mount an adapter on). When `source_ip` is configured we install
    `_install_groww_source_binding()` once at first construction —
    it replaces the `requests` reference inside the SDK's module
    namespace with a proxy that reads a per-thread ContextVar and
    routes the call through a source-bound `requests.Session` from
    a pooled per-IP cache. The ContextVar is set by
    `GrowwBroker._retry_groww_auth` for every method call AND by
    `_mint_access_token` for the login POST, so both login + runtime
    egress from this account's dedicated IPv6. Defensive — Groww
    has not (yet) shown the per-IP session affinity that Dhan v2
    enforces, but this proactively closes the gap.
    """

    def __init__(
        self,
        account: str,
        *,
        api_key: Optional[str] = None,
        totp_seed: Optional[str] = None,
        access_token: Optional[str] = None,
        source_ip: Optional[str] = None,
        # `secret` accepted but ignored — kept in the signature so
        # rebuild_from_db() callers don't have to special-case Groww.
        # The approval-secret flow was retired (see _mint_access_token).
        secret: Optional[str] = None,  # noqa: ARG002
    ) -> None:
        self.account       = account
        self._api_key      = api_key or ""
        self._totp_seed    = totp_seed or ""
        self._access_token = access_token or ""
        self._source_ip    = source_ip
        self._groww        = None
        self._import_error = None
        # Serialises concurrent re-mints — matches Kite + Dhan. Without
        # this, N parallel broker calls hitting an invalidated token
        # would each independently POST to Groww's mint endpoint
        # (waste + rate-limit exposure). The cross-process file lock
        # keeps the prod + dev services from racing each other too.
        self._login_lock   = threading.Lock()
        # IPv6 source binding install — patches the `requests` module
        # inside `growwapi.groww.client`'s namespace so module-level
        # `requests.get/post/put` calls go through a source-bound
        # session. Set on construction (covers every account loaded
        # at startup); idempotent (same patcher class for every
        # GrowwConnection — the patched module-level functions read
        # the per-thread source IP from a ContextVar at call time so
        # parallel GrowwConnection instances don't fight each other).
        # See `_install_groww_source_binding` for the patcher.
        if self._source_ip:
            try:
                _install_groww_source_binding()
            except Exception as e:
                logger.warning(
                    f"GrowwConnection {self.account!r}: source_ip patch "
                    f"install failed ({e}); falling back to default route"
                )
        self._build()

    # ── Token mint + cache ────────────────────────────────────────────

    def _mint_access_token(self) -> str:
        """Mint a fresh access token via the TOTP flow (only).

        The approval-secret flow was retired in favour of TOTP only:
        approval mints prompt the operator to OK the request in the
        Groww app/web every 24 h, which is incompatible with an
        unattended trading service. TOTP renewals are silent — the
        SDK computes a fresh 6-digit code from `self._totp_seed` on
        every mint call.

        Requires `api_key` (vendor TOTP api key) + `totp_seed` (base32
        seed paired with that api key in Groww's developer dashboard).
        Raises if either is missing."""
        from growwapi import GrowwAPI  # type: ignore[import-not-found]
        if not self._api_key or not self._totp_seed:
            raise RuntimeError(
                f"GrowwConnection {self.account!r} needs api_key + "
                f"totp_seed to mint a token, or a manually-pasted "
                f"access_token. Fill credentials in /admin/brokers."
            )
        import pyotp  # type: ignore[import-not-found]
        totp_code = pyotp.TOTP(self._totp_seed).now()
        # Bind the login POST to this account's source IP via the
        # ContextVar — the patched `requests` reference inside
        # `growwapi.groww.client` reads this and routes through a
        # source-bound session. No-op when source_ip is unset.
        if self._source_ip:
            token = _GROWW_SOURCE_IP_OVERRIDE.set(self._source_ip)
            try:
                return GrowwAPI.get_access_token(self._api_key, totp=totp_code)
            finally:
                _GROWW_SOURCE_IP_OVERRIDE.reset(token)
        return GrowwAPI.get_access_token(self._api_key, totp=totp_code)

    def _try_mint_and_cache(self, cache_key: str) -> tuple[str | None, Exception | None]:
        """Attempt TOTP mint; cache on success. Returns (token, error)."""
        if not (self._api_key and self._totp_seed):
            return None, None
        try:
            token = self._mint_access_token()
            if token:
                _save_cached_token(cache_key, token)
                return token, None
        except Exception as e:
            logger.warning(
                f"GrowwConnection {self.account!r} mint failed: {e}. "
                f"Trying api_key as Bearer token directly."
            )
            return None, e
        return None, None

    def _resolve_token_error(self, mint_error: Exception | None) -> RuntimeError:
        """Build the final RuntimeError with credential presence summary."""
        present = {
            "api_key":      bool(self._api_key),
            "totp_seed":    bool(self._totp_seed),
            "access_token": bool(self._access_token),
        }
        present_summary = ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in present.items())
        if mint_error is not None:
            return RuntimeError(
                f"GrowwConnection {self.account!r}: mint failed — {mint_error!r}. "
                f"Provided: {present_summary}. Fix in /admin/brokers."
            )
        return RuntimeError(
            f"GrowwConnection {self.account!r}: no working token. "
            f"Provided: {present_summary}. Need api_key + totp_seed "
            f"or a fresh 24 h access_token. "
            f"Edit credentials in /admin/brokers."
        )

    def _resolve_token(self) -> str:
        """Pick a working access token, preferring (in order):
          1. cached fresh mint
          2. fresh mint via api_key + totp_seed (TOTP flow only —
             approval-secret flow was retired)
          3. api_key used directly as Bearer token — Groww's vendor
             integration keys ARE long-lived JWTs that can be passed
             as the Authorization Bearer header, same shape the
             SDK's mint endpoint accepts (`_build_headers`). This is
             the fallback path when the mint endpoint rate-limits us
             — common after multiple service restarts in a short
             window. Without this fallback every rate-limit response
             killed the account until Groww's counter reset.
          4. manually-pasted 24 h access_token
        Caches every successful mint so a restart within the
        validity window skips the round-trip.
        """
        cache_key = f"groww:{self.account}"
        token, _created = _load_cached_token(cache_key)
        if token:
            return token
        token, mint_error = self._try_mint_and_cache(cache_key)
        if token:
            return token
        if self._api_key and self._api_key.startswith("eyJ"):
            logger.info(
                f"GrowwConnection {self.account!r}: using api_key JWT as "
                f"Bearer token directly (mint unavailable). Cached as "
                f"{cache_key} for {CONN_RESET_HOURS}h."
            )
            _save_cached_token(cache_key, self._api_key)
            return self._api_key
        if self._access_token:
            return self._access_token
        raise self._resolve_token_error(mint_error)

    def _build(self) -> None:
        try:
            from growwapi import GrowwAPI  # type: ignore[import-not-found]
        except ImportError as e:
            logger.error(
                f"growwapi SDK not installed; run `pip install growwapi`. "
                f"Account {self.account!r} will be inactive until "
                f"the dependency is available."
            )
            self._groww = None
            self._import_error = e
            return
        try:
            token = self._resolve_token()
        except Exception as e:
            logger.error(f"GrowwConnection {self.account!r} token resolve "
                         f"failed: {e}")
            _emit_conn_event(
                self.account, "groww", "auth_fail",
                {"error": str(e)[:200], "stage": "_resolve_token"},
            )
            self._groww = None
            self._import_error = e
            return
        self._access_token = token
        self._groww = GrowwAPI(token)
        _emit_conn_event(self.account, "groww", "token_ok")

    def refresh(self) -> None:
        """Force-evict the cached token + re-mint. Call when an SDK
        call fails with auth error. Caller retries the SDK call once
        with the new handle.

        Serialised by `_login_lock` (in-process) + a cross-process file
        lock (so prod + dev services don't race). A peer thread that
        already re-minted while we were waiting for the lock will have
        written a fresh token to the file cache — the inner check uses
        that token instead of running another HTTP mint."""
        cache_key = f"groww:{self.account}"
        with self._login_lock, _cross_process_login_lock(cache_key):
            # Did a peer just refresh? If the cached token is fresh and
            # different from the one we're holding, use it and skip the
            # mint entirely.
            cached, _ = _load_cached_token(cache_key)
            if cached and cached != self._access_token:
                self._access_token = cached
                try:
                    from growwapi import GrowwAPI  # type: ignore[import-not-found]
                    self._groww = GrowwAPI(cached)
                    return
                except Exception:
                    # Fall through to a full re-mint if the SDK rejects.
                    pass
            # No peer mint observed — clear cache + re-build (which will
            # mint via TOTP under `_resolve_token`).
            try:
                _save_cached_token(cache_key, "")
            except Exception:
                pass
            self._build()

    def get_groww_conn(self):
        if self._groww is None:
            raise RuntimeError(
                f"GrowwConnection for {self.account!r} is not initialised "
                f"(growwapi SDK missing? {self._import_error})"
            )
        return self._groww

    def get_access_token(self) -> str:
        return self._access_token


def _nudge_restart_ticker(ticker, current: str, new_conn: dict, kite_accounts: list) -> None:
    """Attempt to restart the ticker on the first eligible Kite account."""
    for acct in kite_accounts:
        kc = new_conn[acct].kite
        api_key = getattr(kc, "api_key", None)
        access_token = (getattr(kc, "_access_token", None)
                        or getattr(kc, "access_token", None))
        if api_key and access_token:
            ok = ticker.restart_with_account(api_key, access_token, acct)
            logger.warning(
                f"Connections: ticker was bound to deleted {current!r}; "
                f"restarting on {acct!r} (ok={ok})"
            )
            return
    logger.warning(
        f"Connections: ticker was bound to deleted {current!r}; "
        "no eligible Kite account to restart on — ticker will idle"
    )


class Connections(SingletonBase):
    # Serialises the one-time init — SingletonBase's own lock protects
    # the _instances dict, not the body of this __init__. Two concurrent
    # Connections() callers could otherwise both see `_singleton_initialized`
    # as False and both run the KiteConnection dict build, which would
    # kick off parallel logins and race Kite's session tracker.
    _init_lock = threading.Lock()

    def __init__(self):
        # SingletonBase.__new__ returns the same instance on every call,
        # but Python always re-invokes __init__ after __new__. Without
        # this guard we'd rebuild KiteConnection per account on every
        # Connections() access — which re-does the token-cache restore
        # (+2 Kite calls each) and was adding ~14 s of latency to every
        # /api/holdings · /positions · /funds request.
        if getattr(self, '_singleton_initialized', False):
            return
        with Connections._init_lock:
            # Double-check under the lock — another thread may have
            # completed the init while we were waiting.
            if getattr(self, '_singleton_initialized', False):
                return
            # Sync seed from secrets.yaml — works during module imports
            # before any DB session exists. The async `rebuild_from_db()`
            # called on app startup swaps this for the DB-backed view if
            # the `broker_accounts` table has rows (and seeds the table
            # from this YAML on first run).
            self._rebuild_from_yaml()
            self._singleton_initialized = True

    def _rebuild_from_yaml(self) -> None:
        """Build the per-account KiteConnection map from `secrets.yaml`.
        Used as the initial sync seed AND as the fallback when the DB
        table is empty. Also seeds `_broker_id_map` from the YAML
        `broker:` key (defaults to "zerodha_kite")."""
        accts = secrets.get("kite_accounts") or {}
        self.conn = {
            account: KiteConnection(account, secrets)
            for account in accts.keys()
        }
        # Seed broker_id map from YAML so get_broker() works before
        # rebuild_from_db() runs (e.g. during module-level imports).
        self._broker_id_map: dict[str, str] = {
            account: str(blob.get("broker") or "zerodha_kite")
            for account, blob in accts.items()
        }
        # Priority map — defaults to 100 per account. Populated from
        # broker_accounts.priority via rebuild_from_db.
        self._priority_map: dict[str, int] = {
            account: 100 for account in accts.keys()
        }
        # historical_data_enabled — all YAML-seeded accounts default True
        # (same as the DB column default). Overridden by rebuild_from_db.
        self._hist_enabled_map: dict[str, bool] = {
            account: True for account in accts.keys()
        }

    def _build_conn_map(self, rows, dhan_deferred: set) -> dict:
        """Build the account→connection map from active DB rows.

        Dhan accounts in ``dhan_deferred`` are skipped (multi-account
        IP-stabilizer).  Per-row errors are logged and skipped.
        """
        new_conn: dict[str, Any] = {}
        for r in rows:
            broker_id = (r.broker_id or "zerodha_kite").lower()
            if broker_id == "dhan" and r.account in dhan_deferred:
                continue
            try:
                conn_obj = self._build_conn_for_row(r, broker_id)
                if conn_obj is not None:
                    new_conn[r.account] = conn_obj
            except Exception as e:
                logger.error(f"{broker_id} connection init failed for {r.account!r}: {e}")
        return new_conn

    async def rebuild_from_db(self) -> None:
        """
        Switch to the DB-backed view of broker accounts. Behaviour:

          1. Query `broker_accounts` (admin-CRUD-managed table).
          2. If empty AND `secrets.yaml` has `kite_accounts`: SEED the
             table from YAML, then fall through to use the YAML view
             we already loaded synchronously in __init__. (One-time
             migration on first deploy of the broker CRUD feature.)
          3. If non-empty: decrypt each row's secrets and rebuild
             `self.conn` from those.
          4. If both are empty: leave `self.conn = {}`.

        Safe to call multiple times — every CRUD mutation on
        `/api/admin/brokers/*` runs this so subsequent broker calls see
        fresh credentials without a service restart.

        When RAMBOQ_USE_CONN_SERVICE=1 is set on this process, the
        local Connections singleton is left empty — broker sessions
        live in conn_service, not here. The cutover delegates
        downstream to a POST /internal/rebuild call against the
        conn_service so credential changes still propagate without
        either service restarting. self._broker_id_map is populated
        from the /internal/accounts response so registry.get_broker
        can resolve broker_id without a per-call hop.
        """
        import os
        if os.environ.get("RAMBOQ_USE_CONN_SERVICE", "").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            self._rebuild_via_conn_service()
            return

        rows = await self._load_active_broker_rows()
        if rows is None:
            return  # DB read failed; keep YAML view
        if not rows:
            # Truly empty (no DB rows and YAML seed had nothing). Leave self.conn as-is.
            return

        _dhan_deferred = self._compute_dhan_deferred_accounts(rows)
        new_conn = self._build_conn_map(rows, _dhan_deferred)

        if not new_conn:
            logger.warning("rebuild_from_db: every broker_accounts row failed to load; "
                           "leaving self.conn as YAML view.")
            return

        new_broker_id_map, new_priority_map, new_hist_enabled_map = \
            self._build_row_lookup_maps(rows, new_conn)

        with Connections._init_lock:
            self.conn = new_conn
            self._broker_id_map     = new_broker_id_map
            self._priority_map      = new_priority_map
            self._hist_enabled_map  = new_hist_enabled_map

        self._refresh_mask_registry(new_conn)
        self._refresh_dhan_priority_caches(rows, new_conn)
        logger.info(f"Connections: rebuilt from DB · accounts={sorted(new_conn.keys())}")

        self._nudge_ticker_on_rebind(new_conn, new_broker_id_map, new_priority_map)

    def _rebuild_via_conn_service(self) -> None:
        """Cutover flag-on path. Don't touch any broker SDKs in this
        process. Pull the account list from conn_service so
        registry / mask_account / navbar surfaces still work.
        """
        from backend.brokers.client.remote_broker import (
            list_remote_accounts, trigger_rebuild,
        )
        try:
            # Hot-rebuild on the canonical side first (this is the
            # CRUD-after-write path); if conn_service is unreachable
            # we still update the local id_map for any cached rows.
            trigger_rebuild()
            rows = list_remote_accounts()
        except Exception as e:
            logger.warning(
                "rebuild_from_db: conn_service delegation failed: %s", e
            )
            rows = []
        self.conn = {}  # canonical: no local broker sessions
        self._broker_id_map = {
            r["account"]: r.get("broker_id", "zerodha_kite")
            for r in rows
            if r.get("account")
        }
        # Mirror to register_accounts so mask_account() / utils
        # functions that key on the live account list still resolve.
        try:
            from backend.shared.helpers.utils import register_accounts
            register_accounts(list(self._broker_id_map.keys()))
        except Exception:
            pass

    async def _load_active_broker_rows(self):
        """Return list of active BrokerAccount rows or None on DB error.

        On first run (empty table), attempts to seed from secrets.yaml
        and re-queries. Empty list = truly empty (no YAML either).
        """
        from backend.api.database import shared_async_session
        from backend.api.models    import BrokerAccount
        from sqlalchemy            import select

        try:
            async with shared_async_session() as s:
                rows = (await s.execute(
                    select(BrokerAccount).where(BrokerAccount.is_active.is_(True))
                )).scalars().all()
        except Exception as e:
            logger.warning(f"broker_accounts read failed; staying on YAML view: {e}")
            return None

        if not rows:
            # First-run migration — copy secrets.yaml into the DB.
            seeded = await self._seed_db_from_yaml()
            if seeded:
                # Re-query so subsequent reloads see DB rows immediately.
                async with shared_async_session() as s:
                    rows = (await s.execute(
                        select(BrokerAccount).where(BrokerAccount.is_active.is_(True))
                    )).scalars().all()
        return rows

    @staticmethod
    def _defer_dhan_ip_group(ip_key: str, group: list) -> set[str]:
        """For one source-IP group with >1 Dhan row, keep the highest-priority
        row and return the accounts to defer. Logs a warning."""
        group.sort(key=lambda x: (int(getattr(x, "priority", 100) or 100), x.account or ""))
        keep = group[0]
        deferred = {r.account for r in group[1:]}
        logger.warning(
            "Dhan multi-account stabilizer: %d Dhan rows share "
            "source_ip=%s. Keeping %r (priority=%s); deferring %s. "
            "Reason: Dhan's per-IP one-session limit + Hostinger "
            "edge filter on non-primary IPv6 addresses. Edit "
            "broker_accounts.priority in /admin/brokers to swap "
            "which account is active.",
            len(group), ip_key or "<OS default>", keep.account,
            getattr(keep, "priority", 100),
            ", ".join(repr(x.account) for x in group[1:]),
        )
        return deferred

    @staticmethod
    def _compute_dhan_deferred_accounts(rows) -> set[str]:
        """Dhan multi-account stabilizer — permanent fix for the rotation
        loop discovered 2026-06-15. Background: Dhan enforces "one
        active session per partner app per source IP" at the v2 auth
        backend. The documented solution (CLAUDE.md → Multi-Account
        IPv6 Source Binding) is to bind each Dhan account to its own
        IPv6 in the server's /48. That binding is blocked on this VPS
        by Hostinger's upstream — only `::1` actually egresses; binds
        to `::2`-`::5` time out on TCP connect (curl --interface diag
        confirmed). Until upstream routing is unblocked, multiple
        Dhan accounts sharing the OS-default route invalidate each
        other's tokens every ~5 min → 'DH-906 Invalid Token' loop.

        Permanent stabilization: when two or more Dhan rows would land
        on the same source IP (incl. blank = OS-default), keep only
        the highest-priority row in `self.conn`. The deferred rows
        stay `is_active=true` in the DB but don't get a connection
        built. Operator swaps the active one via the `priority`
        column in /admin/brokers (lowest number wins). Eliminates the
        rotation cycle at the connection layer so positions/holdings
        for the active Dhan account stay stable across every refresh.
        """
        from collections import defaultdict
        dhan_by_ip: dict[str, list] = defaultdict(list)
        for r in rows:
            if (r.broker_id or "").lower() == "dhan":
                dhan_by_ip[(r.source_ip or "").strip().lower()].append(r)
        deferred: set[str] = set()
        for ip_key, group in dhan_by_ip.items():
            if len(group) > 1:
                deferred |= Connections._defer_dhan_ip_group(ip_key, group)
        return deferred

    def _build_conn_for_row(self, r, broker_id: str):
        """Dispatch to the per-broker connection builder. Returns the
        connection object or None if the row was skipped for any
        credential-missing reason (already logged inside the builder)."""
        if broker_id == "dhan":
            return self._build_dhan_conn(r)
        if broker_id == "groww":
            return self._build_groww_conn(r)
        # Default — Kite (and "zerodha_kite" alias).
        return self._build_kite_conn(r)

    @staticmethod
    def _build_dhan_conn(r):
        """Dhan path — Partner-API auto-login.
        broker_accounts columns reused for Dhan:
          client_id       → Dhan client ID (plaintext)
          api_key         → Partner-API app key (plaintext)
          api_secret_enc  → Partner-API app secret
          password_enc    → Dhan trading PIN (semantic reuse —
                            "password" is the Kite analogue;
                            for Dhan rows it stores the PIN)
          totp_token_enc  → Dhan TOTP seed
        Connection runs the direct REST auth flow
        (POST auth.dhan.co/app/generateAccessToken) on
        first use + every 23 h; no operator paste needed
        after initial setup.
        """
        from backend.shared.helpers.broker_creds import decrypt
        if not r.client_id:
            logger.warning(f"Dhan account {r.account!r} missing "
                           f"client_id; skipping.")
            return None
        try:
            api_secret = decrypt(r.api_secret_enc) if r.api_secret_enc else ""
            pin        = decrypt(r.password_enc)   if r.password_enc   else ""
            totp_token = decrypt(r.totp_token_enc) if r.totp_token_enc else ""
        except Exception as e:
            logger.error(f"Dhan credential decrypt failed for "
                         f"{r.account!r}: {e}")
            return None
        if not (r.api_key and api_secret and pin and totp_token):
            logger.warning(
                f"Dhan account {r.account!r} is missing one or "
                f"more credentials (api_key / api_secret / pin / "
                f"totp_token). Fill them in /admin/brokers and "
                f"the connection will load on next save."
            )
            return None
        return DhanConnection(
            r.account,
            client_id=r.client_id,
            api_key=r.api_key,
            api_secret=api_secret,
            pin=pin,
            totp_token=totp_token,
            source_ip=r.source_ip,
        )

    @staticmethod
    def _build_groww_conn(r):
        """Groww path — three auth modes, tried in order:
          (1) api_key + api_secret  → programmatic refresh
          (2) api_key + totp_token  → programmatic refresh
          (3) access_token alone    → manual 24 h paste
        The connection class picks whichever it can, mints
        a token from disk cache if fresh, and falls back to
        mint-on-build otherwise. Approval-secret flow was
        retired; only api_key + totp_seed (TOTP mint) or a
        manually-pasted access_token are accepted. The
        schema reuses the same totp_token_enc / access_token_enc
        columns Kite uses, so /admin/brokers paints them as
        plain text fields.
        """
        from backend.shared.helpers.broker_creds import decrypt
        totp_token  = (decrypt(r.totp_token_enc)
                       if r.totp_token_enc else "")
        access_token = (decrypt(r.access_token_enc)
                        if r.access_token_enc else "")
        if not (
            (r.api_key and totp_token)
            or access_token
        ):
            logger.warning(
                f"Groww account {r.account!r} has no usable "
                f"credentials. Provide api_key + totp_seed "
                f"(TOTP flow), OR paste a 24 h access_token "
                f"from Groww's developer dashboard. Edit in "
                f"/admin/brokers."
            )
            return None
        return GrowwConnection(
            r.account,
            api_key=(r.api_key or None),
            totp_seed=(totp_token or None),
            access_token=(access_token or None),
            source_ip=r.source_ip,
        )

    @staticmethod
    def _build_kite_conn(r):
        """Default — Kite (and "zerodha_kite" alias).
        Builds a synthesized "secrets-shaped" dict so KiteConnection's
        existing constructor still works without refactoring."""
        from backend.shared.helpers.broker_creds import decrypt
        creds_blob = {
            "api_key":    r.api_key,
            "api_secret": decrypt(r.api_secret_enc),
            "password":   decrypt(r.password_enc),
            "totp_token": decrypt(r.totp_token_enc),
            "source_ip":  r.source_ip,
        }
        synthetic = {
            "kite_accounts":  {r.account: creds_blob},
            "kite_login_url": secrets.get("kite_login_url"),
            "kite_twofa_url": secrets.get("kite_twofa_url"),
        }
        return KiteConnection(r.account, synthetic)

    @staticmethod
    def _build_row_lookup_maps(rows, new_conn: dict) -> tuple[dict, dict, dict]:
        """Build broker_id / priority / hist-enabled lookup caches so
        registry._broker_id_for() and PriceBroker fallback ordering
        never need a DB round-trip on the hot path.
        - broker_id_map: registry lookup (defaults to "zerodha_kite")
        - priority_map: lower value = tried first (defaults to 100)
        - hist_enabled_map: /api/options/historical eligibility gate
          (True by default so all accounts participate unless opted out).
        """
        new_broker_id_map: dict[str, str] = {
            r.account: (r.broker_id or "zerodha_kite")
            for r in rows
            if r.account in new_conn
        }
        new_priority_map: dict[str, int] = {
            r.account: int(getattr(r, "priority", 100) or 100)
            for r in rows
            if r.account in new_conn
        }
        new_hist_enabled_map: dict[str, bool] = {
            r.account: bool(getattr(r, "historical_data_enabled", True))
            for r in rows
            if r.account in new_conn
        }
        return new_broker_id_map, new_priority_map, new_hist_enabled_map

    @staticmethod
    def _refresh_mask_registry(new_conn: dict) -> None:
        """Refresh the masked-account registry so every downstream
        surface (funds.py, holdings.py, telegram alerts, audit log)
        sees the new ordinal-per-broker masking scheme. Operator:
        "update the dhan accounts as d1#### and d2#### …"
        """
        try:
            from backend.shared.helpers.utils import register_accounts
            register_accounts(new_conn.keys())
        except Exception as e:
            # Mask registry refresh isn't load-critical — fall back to
            # the scalar mask. Log and continue.
            logger.warning(f"mask_account registry refresh failed: {e}")

    @staticmethod
    def _refresh_dhan_priority_caches(rows, new_conn: dict) -> None:
        """Populate the in-process poll-priority cache for Dhan accounts.
        This replaces the broken async-from-thread DB read that the
        interval gate previously used (_get_dhan_poll_priority now does
        an O(1) dict lookup instead of an asyncio.run_coroutine_threadsafe
        call which fails on Python 3.10+ in ThreadPoolExecutor workers).
        Also populates the breaker opt-in cache for every broker type.
        """
        try:
            from backend.brokers.broker_apis import set_dhan_priority_cache, set_breaker_optin_cache
            for r in rows:
                if r.account in new_conn:
                    broker_id_val = (r.broker_id or "zerodha_kite").lower()
                    if broker_id_val == "dhan":
                        pp = str(getattr(r, "poll_priority", "hot") or "hot")
                        set_dhan_priority_cache(r.account, pp)
                    # Populate breaker opt-in cache for all broker types.
                    cb_enabled = bool(getattr(r, "circuit_breaker_enabled", False))
                    set_breaker_optin_cache(r.account, cb_enabled)
        except Exception as _pp_err:
            logger.warning(f"poll_priority cache refresh failed: {_pp_err}")

    @staticmethod
    def _nudge_ticker_on_rebind(new_conn: dict, new_broker_id_map: dict,
                                new_priority_map: dict) -> None:
        """Notify the KiteTicker if the account it's currently bound to
        disappeared from the rebuilt conn map. Without this nudge the
        ticker keeps running against dead credentials, recycle() resolves
        nothing, and the WebSocket stays bound until the watchdog's 90s
        disconnect threshold (or a manual restart). The new active
        account is picked from the rebuilt conn map (lowest-priority
        Kite/Dhan account first). Best-effort — Groww / no-Kite-accounts
        operators don't trigger this path.
        """
        try:
            from backend.brokers.kite_ticker import get_ticker
            ticker = get_ticker()
            current = ticker.current_account()
            if not current or current in new_conn:
                return
            kite_accounts = sorted(
                [a for a, bid in new_broker_id_map.items()
                 if (bid or "zerodha_kite") == "zerodha_kite"],
                key=lambda a: new_priority_map.get(a, 100),
            )
            _nudge_restart_ticker(ticker, current, new_conn, kite_accounts)
        except Exception as e:
            logger.warning(f"Connections: ticker rebuild-nudge failed: {e}")

    async def _seed_db_from_yaml(self) -> int:
        """
        First-run migration: copy `secrets.yaml::kite_accounts` into the
        `broker_accounts` table, encrypting the three secret columns.
        Returns the number of rows inserted. No-op when YAML is empty
        or the table already has rows (caller checks emptiness first).
        """
        accts = secrets.get("kite_accounts") or {}
        if not accts:
            return 0

        from backend.api.database import shared_async_session
        from backend.api.models    import BrokerAccount
        from backend.shared.helpers.broker_creds import encrypt

        n = 0
        async with shared_async_session() as s:
            for code, blob in accts.items():
                row = BrokerAccount(
                    account=code,
                    broker_id=str(blob.get("broker") or "kite"),
                    api_key=str(blob.get("api_key") or ""),
                    api_secret_enc=encrypt(str(blob.get("api_secret") or "")),
                    password_enc=encrypt(str(blob.get("password") or "")),
                    totp_token_enc=encrypt(str(blob.get("totp_token") or "")),
                    source_ip=blob.get("source_ip"),
                    is_active=True,
                    notes="seeded from secrets.yaml",
                )
                s.add(row)
                n += 1
            await s.commit()
        logger.warning(
            f"broker_accounts: seeded {n} account(s) from secrets.yaml. "
            f"Subsequent edits should go through /admin/brokers; "
            f"the YAML rows remain as a recovery backup."
        )
        return n


def _emit_conn_event(
    account: str,
    broker_id: str,
    event_type: str,
    detail: dict | None = None,
) -> None:
    """Lazy-import shim that forwards to the conn_events module.

    Lazy import avoids a circular dependency at module load time —
    conn_events imports from backend.api which in turn imports from
    backend.brokers.connections.  The try/except swallows any import
    error so a missing conn_service environment (main API process)
    never breaks the login flow.
    """
    try:
        from backend.brokers.service.conn_events import _emit_conn_event as _fire
        _fire(account, broker_id, event_type, detail)
    except Exception:
        pass


if __name__ == "__main__":
    Connections()
