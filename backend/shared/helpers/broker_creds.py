"""
Encryption helpers for broker credentials stored in the DB.

Why this exists
  Operators add / edit / delete broker accounts via the /admin/brokers
  page; the api_secret / TOTP seed / login password live in the
  `broker_accounts` table. Storing them in the clear would mean an
  intruder reading a single Postgres row gets full broker control —
  unacceptable. We Fernet-encrypt the three secret columns at rest.

Key derivation
  We do NOT introduce a separate master key in `secrets.yaml`. Instead,
  we derive the Fernet key from the existing `cookie_secret` (already
  required for JWT signing) via HKDF-SHA256 with a fixed info tag. That
  way an operator who already has cookie_secret deployed can also use
  the broker CRUD without provisioning a new secret. Rotating
  cookie_secret invalidates the encrypted columns — the migration path
  is to decrypt-then-re-encrypt before the rotation; we don't auto-do
  that, since cookie_secret rotation is rare and explicit.

Public API
  encrypt(plaintext: str) -> str         (returns base64 ciphertext)
  decrypt(ciphertext: str) -> str
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from backend.shared.helpers.utils import secrets

_HKDF_INFO = b"ramboq-broker-creds-v1"
_HKDF_LEN  = 32


_cached_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    """Cache the Fernet instance — HKDF derivation is microseconds but
    we'd otherwise redo it on every read/write. Re-derives if the
    cached cookie_secret has been swapped (caller resets `_cached_fernet`
    on rotation; today we don't expose that, but it's a single-line fix
    when needed)."""
    global _cached_fernet
    if _cached_fernet is not None:
        return _cached_fernet

    cookie_secret = secrets.get("cookie_secret") or ""
    if not cookie_secret:
        raise RuntimeError(
            "secrets.yaml is missing `cookie_secret`. Required for broker "
            "credential encryption — set it before using /admin/brokers."
        )

    ikm = cookie_secret.encode("utf-8")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=_HKDF_LEN,
        salt=None,
        info=_HKDF_INFO,
    ).derive(ikm)
    # Fernet expects URL-safe base64 of a 32-byte key.
    import base64
    key = base64.urlsafe_b64encode(derived)
    _cached_fernet = Fernet(key)
    return _cached_fernet


def encrypt(plaintext: str) -> str:
    """Encrypt and return a UTF-8 string (Fernet-base64). Empty input
    encrypts to empty so the model can store empty strings without a
    cipher-blob round-trip."""
    if not plaintext:
        return ""
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext string. Empty input passes through.
    Bad ciphertext (key rotated, corrupted column) raises InvalidToken
    — the route layer maps that to a 500 with a clear message."""
    if not ciphertext:
        return ""
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as e:
        raise RuntimeError(
            "Failed to decrypt broker credential — likely cookie_secret "
            "rotated since the row was written. Re-add the account."
        ) from e
