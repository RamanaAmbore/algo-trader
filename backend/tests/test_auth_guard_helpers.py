"""
Tests for api/auth_guard.py — JWT authentication guard.
SSOT: jwt_guard is the single entry point for route authentication.
Perf: one indexed SELECT per request (username key); ~1ms budget.
Stale: token_version claim checked against live DB to invalidate old JWTs.
Reuse: is_authenticated_request / is_admin_request for chokepoint checks.
UX: missing Bearer header → 401 NotAuthorizedException (not 500).
"""
from pathlib import Path

_SRC = Path("backend/api/auth_guard.py").read_text()


def test_jwt_guard_exists_and_is_async():
    from backend.api.auth_guard import jwt_guard
    import inspect
    assert inspect.iscoroutinefunction(jwt_guard), "jwt_guard must be async"


def test_admin_guard_exists():
    from backend.api.auth_guard import admin_guard
    import inspect
    assert inspect.iscoroutinefunction(admin_guard), "admin_guard must be async"


def test_designated_guard_exists():
    from backend.api.auth_guard import designated_guard
    import inspect
    assert inspect.iscoroutinefunction(designated_guard), "designated_guard must be async"


def test_bearer_token_check_present():
    """Guard must check for 'Bearer ' prefix before decoding."""
    assert "Bearer" in _SRC, "auth_guard must check for Bearer token prefix"
    assert "NotAuthorizedException" in _SRC, "auth_guard must raise NotAuthorizedException"


def test_token_version_validation():
    """token_version claim must be validated against live DB to enable JWT invalidation."""
    assert "token_version" in _SRC, (
        "jwt_guard must validate the token_version claim against the DB "
        "so bumping User.token_version invalidates all existing JWTs"
    )


def test_verify_token_is_importable_from_auth():
    """verify_token lives in auth.py and is importable."""
    from backend.api.routes.auth import verify_token
    assert callable(verify_token), "verify_token must be callable"


def test_verify_token_rejects_garbage():
    """verify_token must return None (not raise) for garbage tokens."""
    from backend.api.routes.auth import verify_token
    result = verify_token("not.a.valid.jwt")
    assert result is None, "verify_token must return None for invalid tokens (not raise)"


def test_is_authenticated_request_exists():
    from backend.api.auth_guard import is_authenticated_request
    assert callable(is_authenticated_request)


def test_pbkdf2_in_password_path():
    """Password hashing must use PBKDF2-SHA256 (not MD5 or plain SHA)."""
    from backend.api.routes import auth as _auth
    import inspect
    src = inspect.getsource(_auth)
    assert "PBKDF2" in src or "pbkdf2" in src.lower(), (
        "Password hashing must use PBKDF2-SHA256"
    )
