"""Test that _read_server_hash returns a string and _SERVER_HASH is set."""
from backend.api.routes.quote import _SERVER_HASH, _read_server_hash


def test_server_hash_is_string():
    assert isinstance(_SERVER_HASH, str)
    assert len(_SERVER_HASH) > 0


def test_read_server_hash_returns_string():
    h = _read_server_hash()
    assert isinstance(h, str)
    assert len(h) > 0


def test_read_server_hash_never_raises():
    # Even if git is broken, must not raise
    h = _read_server_hash()
    assert h is not None
