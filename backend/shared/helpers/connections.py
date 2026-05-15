import contextlib
import fcntl
import json
import os
import socket
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import urllib3.util.connection
import requests
from requests.adapters import HTTPAdapter
from kiteconnect import KiteConnect

# Force IPv4 globally. Server's outbound IPv6 hangs for most hosts.
# KiteConnect API sessions for accounts with source_ip get a per-session
# override to use IPv6 (api.kite.trade supports IPv6 and it works).
urllib3.util.connection.HAS_IPV6 = False

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
    """
    def __init__(self, source_ip, **kwargs):
        self._source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['source_address'] = (self._source_ip, 0)
        super().init_poolmanager(*args, **kwargs)

    def send(self, request, *args, **kwargs):
        # Temporarily re-enable IPv6 for this request
        old = urllib3.util.connection.HAS_IPV6
        urllib3.util.connection.HAS_IPV6 = True
        try:
            return super().send(request, *args, **kwargs)
        finally:
            urllib3.util.connection.HAS_IPV6 = old



# Resolved at every retry-decorator entry so live changes from
# /admin/settings → connections.retry_count take effect on the next
# call without a restart. Falls back to YAML, then 3.
def _retry_count() -> int:
    from backend.shared.helpers.settings import get_int
    return get_int("connections.retry_count",
                   int(config.get("retry_count", 3)))

CONN_RESET_HOURS = int(config['conn_reset_hours'])

logger = get_logger(__name__)


def _load_cached_token(account: str) -> tuple[str | None, datetime | None]:
    """Load a cached access token for an account. Returns (token, created_at) or (None, None)."""
    try:
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
    """Persist an access token for an account. Empty token removes the entry."""
    try:
        data = {}
        if _TOKEN_CACHE_PATH.exists():
            data = json.loads(_TOKEN_CACHE_PATH.read_text())
        if not access_token:
            data.pop(account, None)
        else:
            data[account] = {
                'access_token': access_token,
                'created_at': datetime.now(timezone.utc).isoformat(),
            }
        _TOKEN_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_CACHE_PATH.write_text(json.dumps(data, indent=2))
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
        new_conn: dict[str, "KiteConnection"] = {}
        for r in rows:
            try:
                creds_blob = {
                    "api_key":    r.api_key,
                    "api_secret": decrypt(r.api_secret_enc),
                    "password":   decrypt(r.password_enc),
                    "totp_token": decrypt(r.totp_token_enc),
                    "source_ip":  r.source_ip,
                }
            except Exception as e:
                logger.error(f"broker_accounts decrypt failed for {r.account!r}: {e}")
                continue
            # Build a synthesized "secrets-shaped" dict so KiteConnection's
            # existing constructor still works without refactoring.
            synthetic = {
                "kite_accounts":  {r.account: creds_blob},
                "kite_login_url": secrets.get("kite_login_url"),
                "kite_twofa_url": secrets.get("kite_twofa_url"),
            }
            try:
                new_conn[r.account] = KiteConnection(r.account, synthetic)
            except Exception as e:
                logger.error(f"KiteConnection init failed for {r.account!r}: {e}")
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

        with Connections._init_lock:
            self.conn = new_conn
            self._broker_id_map = new_broker_id_map
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
