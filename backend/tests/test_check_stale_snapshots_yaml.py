"""scripts/check_stale_snapshots.py — YAML-loader and DSN-encoding regression
tests.

Background (confirmed live on both /opt/ramboq and /opt/ramboq_dev, password
never printed — only lengths/booleans/char-membership compared): the script's
former hand-rolled YAML reader, `_load_yaml_simple()`, unconditionally treated
a bare `#` anywhere in a line as a comment start — even inside a quoted
value. The real production `db_password` contains a literal `#`, so the old
parser silently truncated it (confirmed: real password length 10 vs
naive-parsed length 9, differing exactly on the `#`). Every DB connection
attempt then failed auth, which `_query_max_dates()` misreported as exit
code 3 ("DB unreachable") — a case `tools/tlm/snapcheck.py` treats as a
silent skip, giving a false sense of safety-net coverage for the
closed-hours snapshot-freshness invariant.

A second, related defect was found while fixing the first: even with correct
YAML parsing, `_build_dsn()` spliced the raw password into a
`postgresql://user:password@host:port/db` string that asyncpg parses via
`urllib.parse.urlparse`, where `#`/`:`/`@`/`/` are structural URL delimiters.
An unencoded `#` in the password would corrupt the DSN's own netloc/fragment
boundary, not just fail auth. Fixed by percent-encoding user/password via
`urllib.parse.quote(..., safe="")` before splicing them into the DSN.

These tests cover both fixes: the YAML loader itself, and the DSN round-trip
through the same `urlsplit`-based parsing asyncpg uses.
"""
from __future__ import annotations

import types
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

from scripts import check_stale_snapshots as mod

# A password containing all three hazard characters identified live:
# '#' (comment-start in the old parser; URL-fragment delimiter in a DSN),
# ':' (key/value separator in the old parser; user:pass separator in a DSN),
# "'" (quote-stripping character in the old parser).
_HAZARD_PASSWORD = "My#Pass:Word'99"

# A frozen, byte-for-byte copy of the pre-fix `_load_yaml_simple` function
# body, kept here ONLY so the characterization test below has something
# fixed to contrast against — independent of git history, which could be
# rebased/squashed later. This is not live code; it must never be imported
# by the real script.
_OLD_LOAD_YAML_SIMPLE_SOURCE = '''
def _old_load_yaml_simple(path):
    result = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        # Strip comments
        line = line.split("#")[0].rstrip()
        if not line or ":" not in line:
            continue
        # Only parse top-level keys (no leading spaces)
        if line[0] == " " or line[0] == "\\t":
            continue
        key, _, raw_value = line.partition(":")
        key = key.strip()
        value = raw_value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        # Try int conversion
        try:
            result[key] = int(value)
        except ValueError:
            result[key] = value
    return result
'''


def _old_load_yaml_simple_module() -> types.ModuleType:
    old_mod = types.ModuleType("check_stale_snapshots_old_sandbox")
    exec(compile(_OLD_LOAD_YAML_SIMPLE_SOURCE, "<old_parser_fixture>", "exec"), old_mod.__dict__)
    return old_mod


def _write_secrets_yaml(path: Path, password: str = _HAZARD_PASSWORD) -> None:
    path.write_text(
        'db_user: rambo_admin\n'
        f'db_password: "{password}"\n'
        'db_host: localhost\n'
        'db_port: 5432\n',
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _reset_config_diagnostic():
    """`_CONFIG_DIAGNOSTIC` is a module-level global the fix introduced to
    carry a non-secret diagnostic reason across calls; reset it around every
    test so tests can't leak state into each other."""
    mod._CONFIG_DIAGNOSTIC = None
    yield
    mod._CONFIG_DIAGNOSTIC = None


class TestOldParserCharacterizesTheBug:
    """Proves the pre-fix naive parser really did mis-parse a `#`-bearing
    secret — confirms the test fixture itself reproduces the live-observed
    failure mode, not just a hypothetical one."""

    def test_old_parser_truncates_password_at_hash(self, tmp_path):
        old_mod = _old_load_yaml_simple_module()
        secrets_path = tmp_path / "secrets.yaml"
        _write_secrets_yaml(secrets_path)

        parsed = old_mod._old_load_yaml_simple(secrets_path)

        assert parsed.get("db_password") != _HAZARD_PASSWORD
        # Truncated at the first '#': 'db_password: "My#Pass:Word\'99"'
        # split("#")[0] -> 'db_password: "My', then quote-stripped -> "My".
        assert parsed.get("db_password") == "My"


class TestNewYamlLoader:
    """Proves the real PyYAML-based loader handles every hazard character
    correctly, including nested/edge shapes the old parser never handled."""

    def test_password_with_hash_colon_quote_survives_intact(self, tmp_path):
        secrets_path = tmp_path / "secrets.yaml"
        _write_secrets_yaml(secrets_path)

        parsed = mod._load_yaml_simple(secrets_path)

        assert parsed.get("db_password") == _HAZARD_PASSWORD
        assert parsed.get("db_user") == "rambo_admin"
        assert parsed.get("db_host") == "localhost"
        assert parsed.get("db_port") == 5432
        assert isinstance(parsed.get("db_port"), int)
        assert mod._CONFIG_DIAGNOSTIC is None

    def test_missing_file_returns_empty_dict_with_diagnostic(self, tmp_path):
        missing = tmp_path / "does_not_exist.yaml"
        parsed = mod._load_yaml_simple(missing)
        assert parsed == {}
        assert mod._CONFIG_DIAGNOSTIC is not None
        assert "does_not_exist.yaml" in mod._CONFIG_DIAGNOSTIC

    def test_malformed_yaml_returns_empty_dict_with_diagnostic(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        # Unterminated quoted scalar — invalid YAML.
        bad.write_text('db_password: "unterminated\n', encoding="utf-8")
        parsed = mod._load_yaml_simple(bad)
        assert parsed == {}
        assert mod._CONFIG_DIAGNOSTIC is not None

    def test_empty_file_returns_empty_dict_no_diagnostic(self, tmp_path):
        empty = tmp_path / "empty.yaml"
        empty.write_text("", encoding="utf-8")
        parsed = mod._load_yaml_simple(empty)
        assert parsed == {}
        # An empty file is not a parse error — safe_load("") -> None, which
        # is a legitimate "no keys" result, not a diagnostic-worthy failure.
        assert mod._CONFIG_DIAGNOSTIC is None

    def test_non_mapping_top_level_returns_empty_dict(self, tmp_path):
        scalar = tmp_path / "scalar.yaml"
        scalar.write_text("just_a_string\n", encoding="utf-8")
        parsed = mod._load_yaml_simple(scalar)
        assert parsed == {}


class TestBuildDsnEncoding:
    """Proves the DSN round-trips the hazard password through the exact
    same urlsplit-based parsing asyncpg's own connect_utils.py uses
    (confirmed via `grep -n urlparse` on the server's installed asyncpg)."""

    def _write_fixture_tree(self, root: Path, password: str = _HAZARD_PASSWORD) -> None:
        cfg_dir = root / "backend" / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        _write_secrets_yaml(cfg_dir / "secrets.yaml", password=password)
        (cfg_dir / "backend_config.yaml").write_text(
            "deploy_branch: dev\n", encoding="utf-8"
        )

    def test_dsn_roundtrips_hazard_password(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        self._write_fixture_tree(tmp_path)
        monkeypatch.setattr(mod, "ROOT", tmp_path)

        dsn = mod._build_dsn()
        parsed = urlsplit(dsn)

        # If the password weren't percent-encoded, '#' would start a URL
        # fragment and corrupt netloc/path entirely (confirmed locally:
        # urlsplit('postgresql://u:ab#cd@h:5432/d') truncates netloc to
        # 'u:ab' and swallows '@h:5432/d' into the fragment). Assert the
        # structure survived intact first.
        assert parsed.hostname == "localhost"
        assert parsed.port == 5432
        assert parsed.path == "/ramboq_dev"
        assert parsed.fragment == ""

        recovered_password = unquote(parsed.password)
        assert recovered_password == _HAZARD_PASSWORD

    def test_dsn_name_resolves_by_branch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        self._write_fixture_tree(tmp_path, password="simple")
        cfg_dir = tmp_path / "backend" / "config"
        (cfg_dir / "backend_config.yaml").write_text(
            "deploy_branch: main\n", encoding="utf-8"
        )
        monkeypatch.setattr(mod, "ROOT", tmp_path)

        dsn = mod._build_dsn()
        assert urlsplit(dsn).path == "/ramboq"

    def test_redacted_dsn_never_leaks_password(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        self._write_fixture_tree(tmp_path)
        monkeypatch.setattr(mod, "ROOT", tmp_path)

        dsn = mod._build_dsn()
        redacted = mod._redacted_dsn(dsn)

        assert "***" in redacted
        assert _HAZARD_PASSWORD not in redacted
        from urllib.parse import quote as _quote
        assert _quote(_HAZARD_PASSWORD, safe="") not in redacted

    def test_missing_password_sets_diagnostic(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        cfg_dir = tmp_path / "backend" / "config"
        cfg_dir.mkdir(parents=True, exist_ok=True)
        (cfg_dir / "secrets.yaml").write_text(
            "db_user: rambo_admin\ndb_host: localhost\ndb_port: 5432\n",
            encoding="utf-8",
        )
        (cfg_dir / "backend_config.yaml").write_text(
            "deploy_branch: dev\n", encoding="utf-8"
        )
        monkeypatch.setattr(mod, "ROOT", tmp_path)

        mod._build_dsn()

        assert mod._CONFIG_DIAGNOSTIC is not None
        assert "db_password" in mod._CONFIG_DIAGNOSTIC

    def test_database_url_env_var_takes_priority_and_is_untouched(self, monkeypatch):
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/d"
        )
        dsn = mod._build_dsn()
        assert dsn == "postgresql://u:p@h:5432/d"
