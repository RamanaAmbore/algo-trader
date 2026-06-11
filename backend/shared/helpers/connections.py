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
        expired = (
            self._conn_created_at is None
            or now - self._conn_created_at > timedelta(hours=CONN_RESET_HOURS)
        )

        if not (expired or test_conn):
            return self.kite

        # Two layers of locking:
        #   1. self._login_lock — coordinates threads inside this process
        #   2. _cross_process_login_lock — coordinates with any other
        #      process holding open the same shared token cache file
        #      (typically prod ↔ dev on the same server).
        # The cross-process lock is acquired second so we don't hold an
        # OS-level fd while every concurrent thread waits in line.
        with self._login_lock, _cross_process_login_lock(self.account):
            # Double-check under both locks — a peer may have just
            # refreshed and written a new token to the shared cache
            # while we were waiting.
            now = timestamp_indian()
            expired = (
                self._conn_created_at is None
                or now - self._conn_created_at > timedelta(hours=CONN_RESET_HOURS)
            )
            if not (expired or test_conn):
                return self.kite

            if expired:
                self._conn_created_at = now
                formatted = self._conn_created_at.strftime('%A, %B %d, %Y, %I:%M %p')
                logger.info(f'Kite connection refreshed at {formatted}')

            # Try cached token first — avoids full login/2FA
            if not self._access_token:
                self._try_restore_token()
            if self._access_token:
                # Validate token with a lightweight API call
                try:
                    self.kite.profile()
                    return self.kite
                except Exception as e:
                    logger.warning(f"Cached token invalid for {self.account}: {e}")
                    self._access_token = None
                    _save_cached_token(self.account, '')  # clear stale cache

            # No cached token — do full login
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
        except Exception as e:
            logger.error(f"Failed to generate access token for account {self.account}: {e}")
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

    Login flow (one SDK call, no browser, no SMS/email OTP):

      DhanLogin(client_id).generate_token(pin, totp_code)
      → {"data": {"accessToken": "..."}}

      where `totp_code = pyotp.TOTP(totp_seed).now()` from the stored
      base32 seed. The token's validity is whatever the operator has
      set in the Dhan dashboard's "Token validity" dropdown (default
      24 h; settable 5 min / 1 hr / 24 hr / 30 d / 1 yr at Settings
      → DhanHQ Trading APIs → Token validity).

    The Partner-API consent flow (generate_login_session →
    consume_token_id) is intentionally NOT used — Dhan v2 moved that
    behind browser-based SMS/email-OTP + PIN consent which kills any
    unattended automation. The direct `generate_token` endpoint above
    is the only headless path.

    Access tokens are cached to `dhan_tokens.json` (next to
    `kite_tokens.json`) so a restart within the validity window skips
    the login flow entirely. Cross-process lock (same pattern as
    Kite) prevents prod + dev from racing two parallel logins.

    Rate-limit guard: Dhan's `generate_token` caps at one call per
    2 minutes per account. After a failed login we set
    `_login_blocked_until = now + 130 s` so a burst of auth-fail
    callers doesn't hammer the limit; the cool-off check sits in
    `get_dhan_conn()`.

    No IPv6 source-binding here — Dhan doesn't enforce per-IP
    whitelisting the way Kite does. `source_ip` is accepted on the
    constructor for future symmetry but unused today.

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
        without dhanhq installed (deploys land in stages)."""
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
        except ImportError:
            # dhanhq 1.x fallback (positional args).
            self._dhan = dhanhq(self.client_id, access_token)
        self._import_error = None

    # ── Login flow ───────────────────────────────────────────────────

    def _do_login(self) -> str:
        """Mint a fresh Dhan access_token end-to-end programmatically.

        Uses `DhanLogin.generate_token(pin, totp)` — single POST to
        `https://auth.dhan.co/app/generateAccessToken?dhanClientId=…&
        pin=…&totp=…`. No browser, no SMS/email OTP, no consent flow.
        Validity is whatever the Dhan dashboard's "Token validity"
        dropdown is set to (24 h default; can be extended to 30 d / 1 yr).

        The OLD path (Partner-API consent flow — generate_login_session →
        consume_token_id) was retired because Dhan v2 moved that flow
        behind browser-based SMS/email-OTP + PIN consent. The direct
        endpoint here is what the SDK actually exposes for headless use.
        """
        try:
            from dhanhq import DhanLogin  # type: ignore[import-not-found]
        except ImportError as e:
            raise RuntimeError(
                f"dhanhq SDK missing — cannot run Dhan login for "
                f"{self.account!r}: {e}"
            ) from e

        if not all([self.client_id, self._pin, self._totp_token]):
            raise RuntimeError(
                f"Dhan account {self.account!r} needs client_id + PIN + "
                f"TOTP seed for headless auth. Fill them in /admin/brokers."
            )

        login = DhanLogin(self.client_id)
        totp_code = generate_totp(self._totp_token)
        resp = login.generate_token(self._pin, totp_code)
        # Response shape: {"accessToken": "..."} (sometimes wrapped under
        # "data"). Tolerate both for resilience against minor SDK
        # changes.
        if isinstance(resp, dict):
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            access_token = (data.get("accessToken")
                            or data.get("access_token"))
        else:
            access_token = None
        if not access_token:
            raise RuntimeError(
                f"Dhan generate_token returned no accessToken: {resp!r}"
            )
        return str(access_token)

    def _try_renew(self) -> str | None:
        """Best-effort token refresh using `DhanLogin.renew_token`. Lets
        a still-valid-but-close-to-expiring token roll forward without
        the operator re-entering anything. Returns the new token or
        None if renewal isn't available (older SDK, missing token, …)."""
        if not self._access_token:
            return None
        try:
            from dhanhq import DhanLogin  # type: ignore[import-not-found]
        except ImportError:
            return None
        try:
            login = DhanLogin(self.client_id)
            resp = login.renew_token(self._access_token)
        except Exception as e:
            logger.warning(f"Dhan renew_token failed for {self.account!r}: {e}")
            return None
        if isinstance(resp, dict):
            data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
            new_token = (data.get("accessToken")
                         or data.get("access_token"))
            if new_token:
                return str(new_token)
        return None

    def get_dhan_conn(self, test_conn: bool = False):
        """Return a ready dhanhq client.

        Mirrors `KiteConnection.get_kite_conn` — refreshes when older
        than CONN_RESET_HOURS, or whenever `test_conn=True`. Re-auth
        runs under the per-account login lock + the cross-process file
        lock so concurrent callers don't race two logins against the
        same Partner-API app.
        """
        now = timestamp_indian()
        expired = (
            self._conn_created_at is None
            or now - self._conn_created_at > timedelta(hours=CONN_RESET_HOURS)
        )

        if not (expired or test_conn) and self._dhan is not None:
            return self._dhan

        with self._login_lock, _cross_process_login_lock(self._cache_key()):
            # Double-check under the lock — a peer may have just minted
            # a fresh token while we were waiting.
            now = timestamp_indian()
            expired = (
                self._conn_created_at is None
                or now - self._conn_created_at > timedelta(hours=CONN_RESET_HOURS)
            )
            if not (expired or test_conn) and self._dhan is not None:
                return self._dhan

            # Try the on-disk cache (peer may have just written a token).
            if not self._access_token:
                self._try_restore_token()

            if self._access_token and self._dhan is not None and not test_conn:
                return self._dhan

            # ── Recency guard for test_conn=True ─────────────────────
            # When `_safe_call` detects a DH-906 / "Invalid Token" it
            # calls us with `test_conn=True` to force a re-mint. But
            # multiple concurrent broker calls (background polling +
            # frontend polling + agent ticks) routinely fail in lockstep
            # against the same invalidated token — without this guard
            # each one walks past the cache check above and mints its
            # own NEW token via `generate_token`. Every Dhan
            # `generate_token` call invalidates the previously-issued
            # token, so the parade of fresh logins ends with only the
            # LAST token valid, all earlier tokens (already cached + in
            # use by other callers) silently invalidated. Operator saw
            # this as a 6-minute "Login complete → DH-906 → Login
            # complete" loop in the api log.
            #
            # If the cached token was minted in the last 60 s (by THIS
            # process via a peer thread, or by another process whose
            # write we read from disk), assume it's the freshest and
            # return it. The caller that triggered `test_conn=True`
            # already retried once via _safe_call so a brief return of
            # a known-recent token is the right tradeoff.
            if (test_conn
                    and self._access_token
                    and self._conn_created_at is not None
                    and self._dhan is not None
                    and (now - self._conn_created_at) < timedelta(seconds=60)):
                logger.info(
                    f"Dhan {self.account!r}: test_conn=True but token minted "
                    f"<60 s ago — skipping re-mint to avoid invalidation race"
                )
                return self._dhan

            # Login-rate-limit cool-off — Dhan's auth endpoint
            # rejects a second call within 2 minutes of the first.
            # When the previous _do_login raised because of that, we
            # set _login_blocked_until = now + 130s (2 min + 10s
            # safety). Re-login attempts within that window short-
            # circuit and either return the LAST KNOWN client
            # (possibly stale but better than nothing) or raise so
            # the broker layer can degrade gracefully.
            import time as _time_mod
            if _time_mod.time() < self._login_blocked_until:
                wait_s = int(self._login_blocked_until - _time_mod.time())
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

            # ── Renewal-first path ──────────────────────────────────
            # When the current token is STILL VALID (didn't raise
            # DH-906; just routine refresh as we approach
            # CONN_RESET_HOURS), prefer `renew_token` over
            # `generate_token`. `renew_token` extends the existing
            # token's validity without minting a new one, so other
            # threads / processes still holding the old token keep
            # working. `generate_token` always mints a fresh token AND
            # invalidates the prior — that's the right call for an
            # initial login or after a DH-906, but wrong for routine
            # rolling refreshes. test_conn=True (after auth failure)
            # skips this path because the existing token is dead.
            access_token: str | None = None
            if not test_conn and self._access_token:
                access_token = self._try_renew()
                if access_token:
                    logger.info(
                        f"Dhan {self.account!r}: token renewed (no re-mint, "
                        f"previous token stays valid)"
                    )

            if access_token is None:
                try:
                    access_token = self._do_login()
                except RuntimeError as e:
                    # _do_login failed — set the cool-off so subsequent
                    # callers don't pile up. 130s = Dhan's 2-min limit +
                    # a 10s safety margin against clock drift.
                    self._login_blocked_until = _time_mod.time() + 130.0
                    logger.error(
                        f"Dhan _do_login failed for {self.account!r}: {e!s:.200} — "
                        f"blocking re-login attempts for 130 s"
                    )
                    raise
                # Success path — clear any prior cool-off.
                self._login_blocked_until = 0.0
            self._access_token   = access_token
            self._conn_created_at = timestamp_indian()
            self._save_token(access_token)
            self._build_client(access_token)
            logger.info(f"Dhan login complete for {self.account} "
                        f"(token cached, valid ~24h)")

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
    survive a service restart within the validity window. The cache
    file is shared between prod + dev via the same `/opt/ramboq/.log`
    path used for `kite_tokens.json`.
    """

    def __init__(
        self,
        account: str,
        *,
        api_key: Optional[str] = None,
        totp_seed: Optional[str] = None,
        access_token: Optional[str] = None,
        # `secret` accepted but ignored — kept in the signature so
        # rebuild_from_db() callers don't have to special-case Groww.
        # The approval-secret flow was retired (see _mint_access_token).
        secret: Optional[str] = None,  # noqa: ARG002
    ) -> None:
        self.account       = account
        self._api_key      = api_key or ""
        self._totp_seed    = totp_seed or ""
        self._access_token = access_token or ""
        self._groww        = None
        self._import_error = None
        # Serialises concurrent re-mints — matches Kite + Dhan. Without
        # this, N parallel broker calls hitting an invalidated token
        # would each independently POST to Groww's mint endpoint
        # (waste + rate-limit exposure). The cross-process file lock
        # keeps the prod + dev services from racing each other too.
        self._login_lock   = threading.Lock()
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
        return GrowwAPI.get_access_token(self._api_key, totp=totp_code)

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
        # 2) Mint via api_key + totp_seed. Capture the mint failure
        #    so we can surface it in the final RuntimeError when no
        #    fallback works either.
        mint_error: Exception | None = None
        if self._api_key and self._totp_seed:
            try:
                token = self._mint_access_token()
                if token:
                    _save_cached_token(cache_key, token)
                    return token
            except Exception as e:
                mint_error = e
                logger.warning(
                    f"GrowwConnection {self.account!r} mint failed: {e}. "
                    f"Trying api_key as Bearer token directly."
                )
        # 3) Use the api_key JWT directly as a Bearer token. Groww's
        #    SDK puts whatever the operator passes to GrowwAPI(token)
        #    in the Authorization header verbatim — a vendor JWT
        #    (eyJ… prefix) authenticates just as well as a minted
        #    short-lived token, just without the auto-refresh path.
        #    Cache it under the same key so subsequent rebuilds skip
        #    re-trying the mint until the rate-limit clears.
        if self._api_key and self._api_key.startswith("eyJ"):
            logger.info(
                f"GrowwConnection {self.account!r}: using api_key JWT as "
                f"Bearer token directly (mint unavailable). Cached as "
                f"{cache_key} for {CONN_RESET_HOURS}h."
            )
            _save_cached_token(cache_key, self._api_key)
            return self._api_key
        # 4) Legacy 24 h manual-paste token.
        if self._access_token:
            return self._access_token
        # Final error — list which inputs we DO have so the operator can
        # spot the missing piece without exposing any value. If mint
        # was attempted + failed, include Groww's actual response so
        # the operator sees "Groww 400: Invalid TOTP" instead of guessing.
        present = {
            "api_key":      bool(self._api_key),
            "totp_seed":    bool(self._totp_seed),
            "access_token": bool(self._access_token),
        }
        present_summary = ", ".join(f"{k}={'✓' if v else '✗'}" for k, v in present.items())
        if mint_error is not None:
            raise RuntimeError(
                f"GrowwConnection {self.account!r}: mint failed — {mint_error!r}. "
                f"Provided: {present_summary}. Fix in /admin/brokers."
            )
        raise RuntimeError(
            f"GrowwConnection {self.account!r}: no working token. "
            f"Provided: {present_summary}. Need api_key + totp_seed "
            f"or a fresh 24 h access_token. "
            f"Edit credentials in /admin/brokers."
        )

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
            self._groww = None
            self._import_error = e
            return
        self._access_token = token
        self._groww = GrowwAPI(token)

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
        """
        from backend.api.database import async_session
        from backend.api.models    import BrokerAccount
        from backend.shared.helpers.broker_creds import decrypt
        from sqlalchemy            import select

        try:
            async with async_session() as s:
                rows = (await s.execute(
                    select(BrokerAccount).where(BrokerAccount.is_active.is_(True))
                )).scalars().all()
        except Exception as e:
            logger.warning(f"broker_accounts read failed; staying on YAML view: {e}")
            return

        if not rows:
            # First-run migration — copy secrets.yaml into the DB.
            seeded = await self._seed_db_from_yaml()
            if seeded:
                # Re-query so subsequent reloads see DB rows immediately.
                async with async_session() as s:
                    rows = (await s.execute(
                        select(BrokerAccount).where(BrokerAccount.is_active.is_(True))
                    )).scalars().all()
            if not rows:
                # Truly empty (no YAML either). Leave self.conn as-is.
                return

        # Build new credentials dict from DB rows + decrypt in-memory.
        # The connection type branches on broker_id — Dhan rows build a
        # DhanConnection (client_id + access_token), everything else
        # (Kite + Kite-legacy) builds a KiteConnection.
        new_conn: dict[str, Any] = {}
        for r in rows:
            broker_id = (r.broker_id or "zerodha_kite").lower()
            try:
                if broker_id == "dhan":
                    # Dhan path — Partner-API auto-login.
                    # broker_accounts columns reused for Dhan:
                    #   client_id       → Dhan client ID (plaintext)
                    #   api_key         → Partner-API app key (plaintext)
                    #   api_secret_enc  → Partner-API app secret
                    #   password_enc    → Dhan trading PIN (semantic reuse —
                    #                     "password" is the Kite analogue;
                    #                     for Dhan rows it stores the PIN)
                    #   totp_token_enc  → Dhan TOTP seed
                    # Connection runs the 4-step DhanLogin flow on first
                    # use + every 23 h; no operator paste needed after
                    # initial setup.
                    if not r.client_id:
                        logger.warning(f"Dhan account {r.account!r} missing "
                                       f"client_id; skipping.")
                        continue
                    try:
                        api_secret = decrypt(r.api_secret_enc) if r.api_secret_enc else ""
                        pin        = decrypt(r.password_enc)   if r.password_enc   else ""
                        totp_token = decrypt(r.totp_token_enc) if r.totp_token_enc else ""
                    except Exception as e:
                        logger.error(f"Dhan credential decrypt failed for "
                                     f"{r.account!r}: {e}")
                        continue
                    if not (r.api_key and api_secret and pin and totp_token):
                        logger.warning(
                            f"Dhan account {r.account!r} is missing one or "
                            f"more credentials (api_key / api_secret / pin / "
                            f"totp_token). Fill them in /admin/brokers and "
                            f"the connection will load on next save."
                        )
                        continue
                    new_conn[r.account] = DhanConnection(
                        r.account,
                        client_id=r.client_id,
                        api_key=r.api_key,
                        api_secret=api_secret,
                        pin=pin,
                        totp_token=totp_token,
                        source_ip=r.source_ip,
                    )
                    continue

                if broker_id == "groww":
                    # Groww path — three auth modes, tried in order:
                    #   (1) api_key + api_secret  → programmatic refresh
                    #   (2) api_key + totp_token  → programmatic refresh
                    #   (3) access_token alone    → manual 24 h paste
                    # The connection class picks whichever it can, mints
                    # a token from disk cache if fresh, and falls back to
                    # mint-on-build otherwise. Approval-secret flow was
                    # retired; only api_key + totp_seed (TOTP mint) or a
                    # manually-pasted access_token are accepted. The
                    # schema reuses the same totp_token_enc / access_token_enc
                    # columns Kite uses, so /admin/brokers paints them as
                    # plain text fields.
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
                        continue
                    new_conn[r.account] = GrowwConnection(
                        r.account,
                        api_key=(r.api_key or None),
                        totp_seed=(totp_token or None),
                        access_token=(access_token or None),
                    )
                    continue

                # Default — Kite (and "zerodha_kite" alias).
                creds_blob = {
                    "api_key":    r.api_key,
                    "api_secret": decrypt(r.api_secret_enc),
                    "password":   decrypt(r.password_enc),
                    "totp_token": decrypt(r.totp_token_enc),
                    "source_ip":  r.source_ip,
                }
                # Build a synthesized "secrets-shaped" dict so KiteConnection's
                # existing constructor still works without refactoring.
                synthetic = {
                    "kite_accounts":  {r.account: creds_blob},
                    "kite_login_url": secrets.get("kite_login_url"),
                    "kite_twofa_url": secrets.get("kite_twofa_url"),
                }
                new_conn[r.account] = KiteConnection(r.account, synthetic)
            except Exception as e:
                logger.error(f"{broker_id} connection init failed for "
                             f"{r.account!r}: {e}")
                continue

        if not new_conn:
            logger.warning("rebuild_from_db: every broker_accounts row failed to load; "
                           "leaving self.conn as YAML view.")
            return

        # Build broker_id lookup cache so registry._broker_id_for() never
        # needs a DB round-trip on the hot path.
        new_broker_id_map: dict[str, str] = {
            r.account: (r.broker_id or "zerodha_kite")
            for r in rows
            if r.account in new_conn
        }
        # Priority cache for PriceBroker fallback ordering — lower
        # priority value = tried first. Defaults to 100 (the schema
        # default) for any account where the column is null/missing
        # (e.g. just after migration, before the operator has tuned).
        new_priority_map: dict[str, int] = {
            r.account: int(getattr(r, "priority", 100) or 100)
            for r in rows
            if r.account in new_conn
        }
        # historical_data_enabled — per-account eligibility gate for the
        # /api/options/historical fallback loop. True by default so
        # all accounts participate unless the operator opts one out.
        new_hist_enabled_map: dict[str, bool] = {
            r.account: bool(getattr(r, "historical_data_enabled", True))
            for r in rows
            if r.account in new_conn
        }

        with Connections._init_lock:
            self.conn = new_conn
            self._broker_id_map     = new_broker_id_map
            self._priority_map      = new_priority_map
            self._hist_enabled_map  = new_hist_enabled_map
        logger.info(f"Connections: rebuilt from DB · accounts={sorted(new_conn.keys())}")

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

        from backend.api.database import async_session
        from backend.api.models    import BrokerAccount
        from backend.shared.helpers.broker_creds import encrypt

        n = 0
        async with async_session() as s:
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


if __name__ == "__main__":
    Connections()
