"""
Test suite for auth rate-limit constants SSOT refactor.

Verifies that rate-limit constants are properly extracted and the
guard objects reference them correctly.
"""

import pytest


def test_rate_login_constants():
    """Verify _RATE_LOGIN_LIMIT and _RATE_LOGIN_WINDOW constants are defined."""
    from backend.api.routes import auth

    assert hasattr(auth, '_RATE_LOGIN_LIMIT'), (
        "_RATE_LOGIN_LIMIT constant should exist in auth module"
    )
    assert hasattr(auth, '_RATE_LOGIN_WINDOW'), (
        "_RATE_LOGIN_WINDOW constant should exist in auth module"
    )

    # Verify values match the intended rate limits
    assert auth._RATE_LOGIN_LIMIT == 5, (
        f"expected _RATE_LOGIN_LIMIT=5, got {auth._RATE_LOGIN_LIMIT}"
    )
    assert auth._RATE_LOGIN_WINDOW == 60, (
        f"expected _RATE_LOGIN_WINDOW=60, got {auth._RATE_LOGIN_WINDOW}"
    )


def test_rate_forgot_constants():
    """Verify _RATE_FORGOT_LIMIT and _RATE_FORGOT_WINDOW constants are defined."""
    from backend.api.routes import auth

    assert hasattr(auth, '_RATE_FORGOT_LIMIT'), (
        "_RATE_FORGOT_LIMIT constant should exist in auth module"
    )
    assert hasattr(auth, '_RATE_FORGOT_WINDOW'), (
        "_RATE_FORGOT_WINDOW constant should exist in auth module"
    )

    # Verify values
    assert auth._RATE_FORGOT_LIMIT == 3, (
        f"expected _RATE_FORGOT_LIMIT=3, got {auth._RATE_FORGOT_LIMIT}"
    )
    assert auth._RATE_FORGOT_WINDOW == 60, (
        f"expected _RATE_FORGOT_WINDOW=60, got {auth._RATE_FORGOT_WINDOW}"
    )


def test_rate_reset_constants():
    """Verify _RATE_RESET_LIMIT and _RATE_RESET_WINDOW constants are defined."""
    from backend.api.routes import auth

    assert hasattr(auth, '_RATE_RESET_LIMIT'), (
        "_RATE_RESET_LIMIT constant should exist in auth module"
    )
    assert hasattr(auth, '_RATE_RESET_WINDOW'), (
        "_RATE_RESET_WINDOW constant should exist in auth module"
    )

    # Verify values
    assert auth._RATE_RESET_LIMIT == 3, (
        f"expected _RATE_RESET_LIMIT=3, got {auth._RATE_RESET_LIMIT}"
    )
    assert auth._RATE_RESET_WINDOW == 60, (
        f"expected _RATE_RESET_WINDOW=60, got {auth._RATE_RESET_WINDOW}"
    )


def test_rate_register_constants():
    """Verify _RATE_REGISTER_LIMIT and _RATE_REGISTER_WINDOW constants are defined."""
    from backend.api.routes import auth

    assert hasattr(auth, '_RATE_REGISTER_LIMIT'), (
        "_RATE_REGISTER_LIMIT constant should exist in auth module"
    )
    assert hasattr(auth, '_RATE_REGISTER_WINDOW'), (
        "_RATE_REGISTER_WINDOW constant should exist in auth module"
    )

    # Verify values
    assert auth._RATE_REGISTER_LIMIT == 3, (
        f"expected _RATE_REGISTER_LIMIT=3, got {auth._RATE_REGISTER_LIMIT}"
    )
    assert auth._RATE_REGISTER_WINDOW == 300, (
        f"expected _RATE_REGISTER_WINDOW=300, got {auth._RATE_REGISTER_WINDOW}"
    )


def test_rate_verify_constants():
    """Verify _RATE_VERIFY_LIMIT and _RATE_VERIFY_WINDOW constants are defined."""
    from backend.api.routes import auth

    assert hasattr(auth, '_RATE_VERIFY_LIMIT'), (
        "_RATE_VERIFY_LIMIT constant should exist in auth module"
    )
    assert hasattr(auth, '_RATE_VERIFY_WINDOW'), (
        "_RATE_VERIFY_WINDOW constant should exist in auth module"
    )

    # Verify values
    assert auth._RATE_VERIFY_LIMIT == 10, (
        f"expected _RATE_VERIFY_LIMIT=10, got {auth._RATE_VERIFY_LIMIT}"
    )
    assert auth._RATE_VERIFY_WINDOW == 60, (
        f"expected _RATE_VERIFY_WINDOW=60, got {auth._RATE_VERIFY_WINDOW}"
    )


def test_rate_limit_guards_exist():
    """Verify that the rate-limit guard objects are defined and non-None."""
    from backend.api.routes import auth

    # Check that all the guard objects exist
    assert hasattr(auth, '_login_rate_limit'), (
        "_login_rate_limit guard should exist in auth module"
    )
    assert auth._login_rate_limit is not None, (
        "_login_rate_limit should be initialized with make_rate_limit_guard"
    )

    assert hasattr(auth, '_forgot_pw_rate_limit'), (
        "_forgot_pw_rate_limit guard should exist in auth module"
    )
    assert auth._forgot_pw_rate_limit is not None, (
        "_forgot_pw_rate_limit should be initialized"
    )

    assert hasattr(auth, '_reset_pw_rate_limit'), (
        "_reset_pw_rate_limit guard should exist in auth module"
    )
    assert auth._reset_pw_rate_limit is not None, (
        "_reset_pw_rate_limit should be initialized"
    )

    assert hasattr(auth, '_register_rate_limit'), (
        "_register_rate_limit guard should exist in auth module"
    )
    assert auth._register_rate_limit is not None, (
        "_register_rate_limit should be initialized"
    )

    assert hasattr(auth, '_verify_email_rate_limit'), (
        "_verify_email_rate_limit guard should exist in auth module"
    )
    assert auth._verify_email_rate_limit is not None, (
        "_verify_email_rate_limit should be initialized"
    )
