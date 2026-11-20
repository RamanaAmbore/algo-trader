"""
Typed broker exception hierarchy — inspired by fenix's error classification.

Upstream callers (broker_apis.py, retry decorators) can catch specific
subclasses instead of scanning error message strings.

Attributes on every exception:
  broker  — vendor identifier ("zerodha_kite", "dhan", "groww")
  code    — vendor error code string ("DH-901", "TokenException", …)
  status  — HTTP status code int (where applicable)
"""


class BrokerError(Exception):
    def __init__(self, msg: str = "", *, broker: str | None = None,
                 code: str | None = None, status: int | None = None):
        super().__init__(msg)
        self.broker = broker
        self.code = code
        self.status = status


class BrokerAuthError(BrokerError):
    """Token expired, invalid credentials, session invalidated (401)."""


class BrokerRateLimitError(BrokerError):
    """Rate limit hit — caller should back off and retry (429)."""


class BrokerNetworkError(BrokerError):
    """Connection timeout, reset, or 5xx gateway error."""


class BrokerOrderError(BrokerError):
    """Order rejected, modified, or not found by the broker."""


class BrokerInputError(BrokerError):
    """Bad input — symbol not found, invalid quantity, missing field."""


class BrokerCapabilityError(BrokerError):
    """Broker does not support the requested capability or order type."""


class KiteLoginCoolingOff(RuntimeError):
    """Raised by `KiteConnection.get_kite_conn()` when a prior login
    attempt's escalating cool-off window is still active (see
    `_attempt_login_with_cooloff` in `connections.py`) — a deterministic,
    already-known block, not a fresh failure.

    Deliberately a `RuntimeError` subclass, NOT a `BrokerError` subclass
    like the exceptions above — `get_kite_conn()`'s cooloff raise has
    always been a bare `RuntimeError` (see its own docstring), and several
    callers already do broad `except Exception`/`except RuntimeError`
    handling around it; keeping this a `RuntimeError` preserves that
    compatibility unconditionally, with no caller migration needed.

    `retry_kite_conn` (`backend/shared/helpers/decorators.py`) catches
    this type specifically to fail fast (no retry — retrying 3x against a
    deterministic block gains nothing) and to log at DEBUG instead of
    ERROR (2026-11-20 fix — this record was flooding the Alerts tab with
    one ERROR per exhausted data-fetch attempt for the entire cooloff
    duration, redundant with `[KITE-LOGIN-COOLOFF]`'s own one-per-failure
    log line and the dedicated F3 escalation alert).
    """
