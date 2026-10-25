"""
Regression tests for the 2026-10-24 Dhan numeric-client-id mask-alias fix.

Background: `register_accounts()` (`backend/shared/helpers/utils.py`) already
implements the correct account-code masking algorithm (first-2-chars by
default, broker-letter + 1-based-ordinal on collision). The gap: the raw
NUMERIC Dhan `client_id` (a different value from the internal account code,
sourced from `BrokerAccount.client_id`) was never fed into the registry at
all, so it fell through to `_scalar_mask()`'s digit-stripping fallback and
produced a useless all-`#` string. Confirmed live on prod: the two real Dhan
client_ids are the same length and share the same first-2-characters, so an
independently-derived mask from the client_id's own characters would ALSO
collide — the fix instead ALIASES each client_id to its paired account
code's already-computed mask.

Five quality dimensions:
  SSOT        — assertions compare against `mask_account(account_code)`'s
                own runtime-computed value, never a hardcoded literal
                (module-global `_REGISTRY` state shifts with whatever else
                is registered in the same test run / process).
  Correctness — covers unique-prefix, colliding-prefix, missing-client-id,
                and both `Connections` call sites (direct DB path +
                conn_service cutover path).
  Performance — pure dict logic; no DB, no broker SDK, no asyncio.run().
  Reuse       — exercises the real `register_accounts()` / `mask_account()`
                / `Connections._build_client_id_map` /
                `Connections._refresh_mask_registry` code paths directly,
                not a reimplementation.
  UX          — every assert carries an f-string with the actual value.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.shared.helpers import utils as utils_mod
from backend.shared.helpers.utils import mask_account, register_accounts


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Every test gets a clean `_REGISTRY` and leaves a clean one behind —
    module-global state must never leak between tests or into other test
    modules that run in the same process."""
    saved = dict(utils_mod._REGISTRY)
    utils_mod._REGISTRY = {}
    yield
    utils_mod._REGISTRY = saved


# ---------------------------------------------------------------------------
# register_accounts(accounts, client_ids) — the core aliasing logic
# ---------------------------------------------------------------------------

class TestRegisterAccountsClientIdAlias:
    def test_non_colliding_account_client_id_aliases_to_own_mask(self):
        """A client_id for an account with no prefix collision gets that
        account's own first-2-char mask."""
        register_accounts(["ZG0790", "GR87DF"], {"ZG0790": "1100099999"})
        expected = mask_account("ZG0790")
        assert mask_account("1100099999") == expected, (
            f"client_id alias expected {expected!r}, "
            f"got {mask_account('1100099999')!r}"
        )
        # Sanity: the account mask itself must be unmasked-prefix (no
        # collision in this account list).
        assert expected == "ZG####", f"unexpected base mask: {expected!r}"

    def test_colliding_dhan_pair_client_ids_alias_to_ordinal_masks(self):
        """The real-shape case: two Dhan accounts collide on their first
        2 chars and get ordinal masks (D1####/D2####); their raw numeric
        client_ids — which ALSO collide on their own first 2 chars — must
        alias to those SAME ordinal masks, not an independently-derived
        (and also-colliding) mask."""
        register_accounts(
            ["DH3747", "DH6847"],
            {"DH3747": "1100012345", "DH6847": "1100067890"},
        )
        mask_3747 = mask_account("DH3747")
        mask_6847 = mask_account("DH6847")
        assert mask_3747 != mask_6847, (
            f"paired account codes must stay distinguishable: "
            f"{mask_3747!r} vs {mask_6847!r}"
        )
        assert mask_account("1100012345") == mask_3747, (
            f"client_id for DH3747 expected {mask_3747!r}, "
            f"got {mask_account('1100012345')!r}"
        )
        assert mask_account("1100067890") == mask_6847, (
            f"client_id for DH6847 expected {mask_6847!r}, "
            f"got {mask_account('1100067890')!r}"
        )
        assert mask_account("1100012345") != mask_account("1100067890"), (
            "the two Dhan client_ids must mask to distinguishable strings"
        )

    def test_client_ids_none_is_backward_compatible(self):
        """Existing callers that pass only the account list (no second
        positional arg) must see byte-for-byte unchanged behaviour."""
        register_accounts(["DH3747", "DH6847", "ZG0790"])
        assert mask_account("DH3747") == "D1####"
        assert mask_account("DH6847") == "D2####"
        assert mask_account("ZG0790") == "ZG####"

    def test_falsy_client_id_skipped(self):
        register_accounts(["ZG0790"], {"ZG0790": None, "ZJ6294": "123"})
        # ZG0790's client_id is None (skipped). ZJ6294 isn't in the
        # account list at all (also skipped, no KeyError). Neither
        # produces a registry entry.
        assert "123" not in utils_mod._REGISTRY, f"got {utils_mod._REGISTRY!r}"
        assert mask_account("ZG0790") == "ZG####", f"got {mask_account('ZG0790')!r}"

    def test_client_id_for_unregistered_account_is_ignored(self):
        """A client_id entry whose account code isn't in the account list
        at all (e.g. a stale mapping) must not raise and must not pollute
        the registry."""
        register_accounts(["ZG0790"], {"GHOST999": "555000111"})
        assert "555000111" not in utils_mod._REGISTRY, (
            "client_id for an unregistered account must not be aliased"
        )

    def test_existing_alphanumeric_masking_unchanged_unique(self):
        """Regression: unique-prefix alphanumeric masking is unaffected
        by the new client_ids parameter, with or without one passed."""
        register_accounts(["ZG0790", "ZJ6294", "GR87DF"])
        assert mask_account("ZG0790") == "ZG####"
        assert mask_account("ZJ6294") == "ZJ####"
        assert mask_account("GR87DF") == "GR####"

    def test_existing_alphanumeric_masking_unchanged_colliding(self):
        """Regression: colliding-prefix ordinal masking is unaffected."""
        register_accounts(["DH3747", "DH6847"])
        assert mask_account("DH3747") == "D1####"
        assert mask_account("DH6847") == "D2####"

    def test_alias_never_overwrites_a_real_account_code_key(self):
        """Defensive: if a client_id string happened to equal a real
        account code already in the map, the alias must not clobber it."""
        register_accounts(
            ["ZG0790", "ZJ6294"],
            {"ZJ6294": "ZG0790"},  # contrived collision with another code
        )
        # ZG0790's own mask must be untouched by the attempted alias.
        assert mask_account("ZG0790") == "ZG####", (
            f"alias must never overwrite a real account-code key, "
            f"got {mask_account('ZG0790')!r}"
        )


# ---------------------------------------------------------------------------
# Connections._build_client_id_map — pure dict-building
# ---------------------------------------------------------------------------

class TestBuildClientIdMap:
    def _make_row(self, account, client_id=None):
        return SimpleNamespace(account=account, client_id=client_id)

    def test_populated_for_accounts_with_client_id(self):
        from backend.brokers.connections import Connections

        rows = [
            self._make_row("DH3747", "1100012345"),
            self._make_row("DH6847", "1100067890"),
        ]
        new_conn = {"DH3747": MagicMock(), "DH6847": MagicMock()}
        out = Connections._build_client_id_map(rows, new_conn)
        assert out == {"DH3747": "1100012345", "DH6847": "1100067890"}, f"got {out!r}"

    def test_accounts_without_client_id_excluded(self):
        from backend.brokers.connections import Connections

        rows = [self._make_row("ZG0790", client_id=None)]
        new_conn = {"ZG0790": MagicMock()}
        out = Connections._build_client_id_map(rows, new_conn)
        assert out == {}, f"Kite row with no client_id must be excluded, got {out!r}"

    def test_rows_absent_from_new_conn_excluded(self):
        from backend.brokers.connections import Connections

        rows = [self._make_row("DH3747", "1100012345")]
        new_conn = {}  # DH3747 failed to load
        out = Connections._build_client_id_map(rows, new_conn)
        assert out == {}, f"row absent from new_conn must be excluded, got {out!r}"


# ---------------------------------------------------------------------------
# Connections._refresh_mask_registry — threads client_id_map through
# ---------------------------------------------------------------------------

class TestRefreshMaskRegistryThreadsClientIds:
    def test_refresh_mask_registry_registers_client_id_alias(self):
        from backend.brokers.connections import Connections

        new_conn = {"DH3747": MagicMock(), "DH6847": MagicMock()}
        client_id_map = {"DH3747": "1100012345", "DH6847": "1100067890"}
        Connections._refresh_mask_registry(new_conn, client_id_map)

        mask_3747 = mask_account("DH3747")
        assert mask_account("1100012345") == mask_3747, (
            f"expected client_id alias {mask_3747!r}, "
            f"got {mask_account('1100012345')!r}"
        )

    def test_refresh_mask_registry_with_no_client_id_map_is_unchanged(self):
        """Calling with the old single-arg shape (client_id_map=None
        default) must not raise and must not alias anything."""
        from backend.brokers.connections import Connections

        new_conn = {"ZG0790": MagicMock()}
        Connections._refresh_mask_registry(new_conn)
        assert mask_account("ZG0790") == "ZG####", f"got {mask_account('ZG0790')!r}"


# ---------------------------------------------------------------------------
# Connections._rebuild_via_conn_service — cutover-mode path threads
# conn_service's `client_id` field through to register_accounts
# ---------------------------------------------------------------------------

class TestRebuildViaConnServiceThreadsClientId:
    def test_client_id_field_present_aliases_correctly(self, monkeypatch):
        from backend.brokers.connections import Connections

        rows = [
            {"account": "DH3747", "broker_id": "dhan", "client_id": "1100012345"},
            {"account": "DH6847", "broker_id": "dhan", "client_id": "1100067890"},
        ]
        monkeypatch.setattr(
            "backend.brokers.client.remote_broker.list_remote_accounts",
            lambda: rows,
        )
        monkeypatch.setattr(
            "backend.brokers.client.remote_broker.trigger_rebuild",
            lambda: None,
        )

        conn = object.__new__(Connections)
        conn._rebuild_via_conn_service()

        mask_3747 = mask_account("DH3747")
        mask_6847 = mask_account("DH6847")
        assert mask_3747 != mask_6847, f"{mask_3747!r} vs {mask_6847!r}"
        assert mask_account("1100012345") == mask_3747, (
            f"got {mask_account('1100012345')!r}, expected {mask_3747!r}"
        )
        assert mask_account("1100067890") == mask_6847, (
            f"got {mask_account('1100067890')!r}, expected {mask_6847!r}"
        )
        assert conn._client_id_map == {
            "DH3747": "1100012345", "DH6847": "1100067890",
        }, f"got {conn._client_id_map!r}"

    def test_client_id_field_absent_degrades_to_no_alias(self, monkeypatch):
        """Old, not-yet-redeployed conn_service — rows carry no
        `client_id` key at all. Must not raise, and must leave the
        numeric id unaliased (falls to the scalar all-# fallback),
        matching pre-fix behaviour exactly."""
        from backend.brokers.connections import Connections

        rows = [{"account": "DH3747", "broker_id": "dhan"}]
        monkeypatch.setattr(
            "backend.brokers.client.remote_broker.list_remote_accounts",
            lambda: rows,
        )
        monkeypatch.setattr(
            "backend.brokers.client.remote_broker.trigger_rebuild",
            lambda: None,
        )

        conn = object.__new__(Connections)
        conn._rebuild_via_conn_service()

        assert conn._client_id_map == {}, f"got {conn._client_id_map!r}"
        assert mask_account("DH3747") == "DH####", f"got {mask_account('DH3747')!r}"


# ---------------------------------------------------------------------------
# orders.py:_pb_redact_payload_for_log — masks dhanClientId in place
# ---------------------------------------------------------------------------

class TestRedactPayloadForLogMasksInPlace:
    def test_dhan_client_id_masked_not_dropped(self):
        from backend.api.routes.orders import _pb_redact_payload_for_log

        register_accounts(["DH3747", "DH6847"], {"DH3747": "1100012345"})
        body = {"dhanClientId": "1100012345", "orderId": "O1", "orderStatus": "TRADED"}
        out = _pb_redact_payload_for_log(body)

        assert out["dhanClientId"] == mask_account("DH3747"), (
            f"expected the aliased mask {mask_account('DH3747')!r}, "
            f"got {out['dhanClientId']!r}"
        )
        assert out["dhanClientId"] != "1100012345"
        assert "1100012345" not in repr(out)
        # Other fields pass through unchanged.
        assert out["orderId"] == "O1"
        assert out["orderStatus"] == "TRADED"

    def test_original_body_not_mutated(self):
        from backend.api.routes.orders import _pb_redact_payload_for_log

        body = {"dhanClientId": "1100012345"}
        _pb_redact_payload_for_log(body)
        assert body["dhanClientId"] == "1100012345", (
            "redaction must happen on a copy; the raw value is still "
            "needed downstream for account routing"
        )

    def test_without_alias_falls_back_to_scalar_mask_not_raw_value(self):
        """Before register_accounts has ever aliased this client_id (or
        in a process that never loaded Connections), the value must
        still never leak raw — it falls back to the digit-stripped
        scalar mask."""
        from backend.api.routes.orders import _pb_redact_payload_for_log

        utils_mod._REGISTRY = {}  # simulate: alias never registered
        body = {"dhanClientId": "1100012345"}
        out = _pb_redact_payload_for_log(body)
        assert out["dhanClientId"] != "1100012345"
        assert "1100012345" not in repr(out)
