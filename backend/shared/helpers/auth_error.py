"""Shared auth-error signal detection helper.

No broker imports — this module has zero dependencies on the broker layer
so it can be imported from both broker_apis and decorators without any
circular-import risk.
"""

# Auth-error signal strings shared across Kite + Dhan + Groww error messages.
_AUTH_ERROR_HINTS_LOWER: tuple[str, ...] = (
    "invalid access token",
    "invalid token",
    "token expired",
    "unauthorized",
    "unauthorised",
    "auth failed",
    "invalid api key",
    # F8 (2026-11-20 incident audit) — Kite's own literal message
    # "Invalid `api_key` or `access_token`." was previously classified as
    # fetch_fail (no hint above matches the backtick/underscore form),
    # not auth_fail, mislabeling a real auth failure in
    # `broker_connection_events`. Lowercased exact phrase per the fix.
    "invalid `api_key` or `access_token`",
    "403",
    "401",
    "dh-901",
    "dh-906",
)


def is_auth_error_str(err: str) -> bool:
    """Return True when the stringified error message looks like an auth / token
    failure (401 / 403 class).  Used to select event_type='auth_fail' vs
    'fetch_fail' in _record_fetch without requiring the original exception object,
    and to decide whether to attempt a token renewal on exception.

    No broker imports — safe to call from any layer.
    """
    low = err.lower()
    return any(hint in low for hint in _AUTH_ERROR_HINTS_LOWER)
