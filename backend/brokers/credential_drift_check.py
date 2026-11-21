"""One-shot startup check: secrets.yaml vs broker_accounts DB credential drift.

Background (2026-11-20 live incident, account ZJ6294): conn_service builds
every `KiteConnection`/`DhanConnection`/`GrowwConnection` from the
`broker_accounts` DB table (see `Connections.rebuild_from_db` /
`_build_kite_conn` / `_build_dhan_conn` / `_build_groww_conn` in
`connections.py`), never directly from `secrets.yaml`. `secrets.yaml`'s
`kite_accounts` / `dhan_accounts` / `groww_accounts` sections are read only
ONCE, to seed a brand-new account into the DB the first time it appears
(`Connections._seed_db_from_yaml()` — kite only today; Dhan/Groww rows are
created via the admin UI, but the same "DB row already exists → YAML is
inert" rule applies to them once a row exists). Any later `secrets.yaml`
edit to an account that already has a DB row is silently a no-op. On the
incident day, several `secrets.yaml` edits to ZJ6294's `api_secret` were
each followed by a `ramboq_conn` restart; none of them took effect, because
the DB row's stale secret was never touched. The real fix was the admin
`PATCH /api/admin/brokers/ZJ6294` endpoint.

This module is a startup-only, detection-only check for exactly that class
of mistake: for every account present in BOTH secrets.yaml and the DB,
compare the credential fields secrets.yaml can plausibly still influence
and warn (once, per differing field) when they disagree. It never
auto-overwrites either side — the DB might be deliberately ahead (an
operator edit via /admin/brokers) or the YAML might be a deliberately
untested future value — only the operator can tell, and either silent
"fix" would be wrong.

Hard invariants:
  - Runs ONCE at conn_service boot (registered in `app.py`'s on_startup
    list, next to `_preload_db_lkg_on_startup`), never periodically.
  - A DB-read failure, or a decrypt failure for one account/field, is
    caught and logged at WARNING; it must never crash startup or block
    any other account's check.
  - No raw secret value is ever logged, for either side of the
    comparison. Every compared value is reduced to a short SHA-256 hash
    prefix purely for human-readable log correlation
    ("yaml=2f3290... db=8c54fc...") — the hash itself is not reversible
    and is not a credential.
  - Never writes to the DB or to secrets.yaml. Detection and logging
    only.

Field-name caveat: the per-broker field map below mirrors the field
vocabulary the admin `/admin/brokers` schema (`backend/api/routes/brokers.py`
`BrokerAccountCreate`/`BrokerAccountUpdate`) and `_seed_db_from_yaml()`'s own
`kite_accounts` blob use uniformly across all three brokers (`api_key`,
`api_secret`, `password`, `totp_token`, `client_id`, `access_token`). This
codebase has no live `dhan_accounts`/`groww_accounts` secrets.yaml entries
to confirm the field names against at the time this was written — those
sections today are read only for `source_ip`
(`_get_source_ip_from_secrets`). If a real `dhan_accounts`/`groww_accounts`
blob on a given server uses different key names, that field is silently
never compared for that broker (no error — see `_check_one_account`, which
skips any field absent or empty in the YAML blob). This is a conservative
default: it only ever adds a comparison when the key names do line up.
"""
from __future__ import annotations

import hashlib

from backend.shared.helpers.ramboq_logger import get_logger
from backend.shared.helpers.utils import mask_account, secrets

logger = get_logger(__name__)

# section name in secrets.yaml → broker_id key used by _FIELD_MAP below.
_BROKER_SECTIONS: dict[str, str] = {
    "kite_accounts": "kite",
    "dhan_accounts": "dhan",
    "groww_accounts": "groww",
}

# Per-broker field map: YAML/plain field name → DB *_enc column name, or
# None when the DB column is stored in plaintext (api_key). Only fields
# this broker's own connection builder in connections.py actually reads
# are listed:
#   - Kite  (_build_kite_conn):  api_key, api_secret_enc.
#   - Dhan  (_build_dhan_conn):  api_key, api_secret_enc (+ password_enc /
#                                totp_token_enc — deliberately excluded,
#                                see note below).
#   - Groww (_build_groww_conn): api_key, totp_token_enc (api_secret is
#                                not read by Groww at all — its approval-
#                                secret flow was retired).
#
# `password` (Dhan's trading PIN) and `totp_token` (TOTP seed) are
# deliberately NOT compared for any broker, even though the builders read
# them: both are short, low-entropy secrets (a 4-6 digit PIN; a 16-32 char
# base32 TOTP seed with a constrained alphabet) where a 12-hex-char SHA-256
# prefix meaningfully narrows a brute-force search. `api_key`/`api_secret`
# are high-entropy strings for every broker in this codebase (Groww's
# `api_key` alone is a ~900-char JWT — see `BrokerAccount.api_key`'s own
# column comment in models.py), so hashing them for a log line is safe.
_FIELD_MAP: dict[str, dict[str, str | None]] = {
    "kite":  {"api_key": None, "api_secret": "api_secret_enc"},
    "dhan":  {"api_key": None, "api_secret": "api_secret_enc"},
    "groww": {"api_key": None, "totp_token": "totp_token_enc"},
}


def _hash_for_log(value: str | None) -> str:
    """Short, non-reversible SHA-256 prefix for a log line. Never returns
    the raw value. A genuinely empty/missing value renders as the literal
    string "<empty>" rather than hashlib.sha256(b"").hexdigest()[:12], so
    an operator reading the log line can immediately tell "this side had
    nothing" apart from "this side hashed to something starting with the
    same digits by coincidence"."""
    if not value:
        return "<empty>"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _check_one_field(masked: str, field: str, yaml_val: str, db_val: str) -> None:
    """Compare one already-resolved (plaintext) field pair and log a
    single WARNING line on mismatch. Never called with an empty yaml_val
    (callers skip those — nothing to compare)."""
    if yaml_val == db_val:
        return
    logger.warning(
        "[CREDENTIAL-DRIFT] %s: secrets.yaml and the database disagree "
        "on %s. The database is authoritative for an existing account — "
        "this YAML value is being silently ignored. To update "
        "credentials for an existing account, use PATCH "
        "/api/admin/brokers/%s (or the /admin/brokers UI), not "
        "secrets.yaml. yaml=%s db=%s",
        masked, field, masked, _hash_for_log(yaml_val), _hash_for_log(db_val),
    )


def _resolve_db_field(row, field: str, enc_col: str | None) -> str:
    """Return the plaintext DB-side value for one field. Raises on a
    decrypt failure (bad Fernet token / rotated cookie_secret) — caller
    catches per-field so one bad field/account never blocks the rest."""
    if enc_col is None:
        return str(getattr(row, field, "") or "")
    from backend.shared.helpers.broker_creds import decrypt
    enc_val = getattr(row, enc_col, "") or ""
    return decrypt(enc_val) if enc_val else ""


def _check_one_account(code: str, broker_id: str, yaml_blob: dict, row) -> None:
    """Compare every mapped field for one account. Never raises — a
    decrypt failure on one field is logged and skipped; the rest of this
    account's fields, and every other account, are unaffected."""
    field_map = _FIELD_MAP.get(broker_id)
    if not field_map:
        return
    masked = mask_account(code)
    for field, enc_col in field_map.items():
        yaml_val = str(yaml_blob.get(field) or "")
        if not yaml_val:
            # Nothing in secrets.yaml to compare for this field (e.g. a
            # dhan_accounts/groww_accounts entry that only carries
            # source_ip today) — comparing against the DB here would be
            # a guaranteed, meaningless mismatch. Skip it.
            continue
        try:
            db_val = _resolve_db_field(row, field, enc_col)
        except Exception as e:
            logger.warning(
                "[CREDENTIAL-DRIFT] %s: failed to decrypt the database's "
                "%s for comparison against secrets.yaml (%s). Skipping "
                "this field.",
                masked, field, e,
            )
            continue
        _check_one_field(masked, field, yaml_val, db_val)


async def run_credential_drift_check() -> None:
    """One-shot, best-effort entry point — call once at conn_service
    startup. For every account present in both secrets.yaml and the
    broker_accounts table (active or inactive — an existing row, even a
    disabled one, already makes the matching secrets.yaml entry inert per
    `_seed_db_from_yaml()`'s own "skip any already-present code" guard),
    compares the fields in `_FIELD_MAP` and logs one WARNING per differing
    field. Never raises: a DB-read failure degrades to a single WARNING
    and the check is skipped entirely for this boot; a per-account/per-field
    failure is caught inside `_check_one_account` and never reaches here.
    """
    try:
        from backend.api.database import shared_async_session
        from backend.api.models import BrokerAccount
        from sqlalchemy import select
    except Exception as e:
        logger.warning(
            "[CREDENTIAL-DRIFT] startup imports failed, skipping check: %s", e
        )
        return

    try:
        async with shared_async_session() as s:
            rows = (await s.execute(select(BrokerAccount))).scalars().all()
    except Exception as e:
        logger.warning(
            "[CREDENTIAL-DRIFT] broker_accounts read failed, skipping check: %s", e
        )
        return

    rows_by_account = {r.account: r for r in rows}

    for section, broker_id in _BROKER_SECTIONS.items():
        yaml_accounts = secrets.get(section) or {}
        if not isinstance(yaml_accounts, dict):
            continue
        for code, blob in yaml_accounts.items():
            if not isinstance(blob, dict):
                continue
            row = rows_by_account.get(code)
            if row is None:
                # Not yet in the DB — nothing to compare (this is the
                # genuinely-new-account, YAML-still-authoritative case).
                continue
            try:
                _check_one_account(code, broker_id, blob, row)
            except Exception as e:
                logger.warning(
                    "[CREDENTIAL-DRIFT] %s: check failed unexpectedly: %s",
                    mask_account(code), e,
                )
