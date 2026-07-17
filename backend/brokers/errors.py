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
