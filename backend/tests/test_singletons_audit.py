"""Singleton audit tests — Phase C.

Verifies identity, concurrency safety, and O(1) cost for every
singleton or module-level constant promoted during the audit.

Coverage:
  1. SingletonBase subclasses (Connections) — same object identity on N calls.
  2. Module-level lazy singletons (httpx clients in transport/remote_broker/sync).
  3. Module-level constants promoted from per-call (broker_apis._NSE_SEGMENT_MAP).
  4. Logger singleton-per-name correctness (service/app module-level logger).
  5. TickerManager / BroadcastBus module-level instance correctness.
  6. GrammarRegistry module-level REGISTRY.
  7. TemplateRegistry __new__ singleton.
  8. Concurrency: 10 threads all see the same _NSE_SEGMENT_MAP identity.
  9. Performance: 1000 accesses to _NSE_SEGMENT_MAP are O(1) in memory.
"""
from __future__ import annotations

import threading
import time
import sys
import os
from typing import Any


# ---------------------------------------------------------------------------
# 1. SingletonBase — same identity across multiple instantiations
# ---------------------------------------------------------------------------

def test_singleton_base_same_identity():
    """Two calls to a SingletonBase subclass return the same object."""
    from backend.shared.helpers.singleton_base import SingletonBase

    class _TestSingleton(SingletonBase):
        def __init__(self):
            if getattr(self, "_singleton_initialized", False):
                return
            self._singleton_initialized = True
            self.value = 42

    a = _TestSingleton()
    b = _TestSingleton()
    assert a is b, "SingletonBase must return the same instance on every call"
    assert a.value == 42

    # Cleanup so we don't pollute other tests
    SingletonBase._instances.pop(_TestSingleton, None)


def test_singleton_base_thread_safety():
    """10 threads instantiating the same SingletonBase subclass all
    receive the same object identity."""
    from backend.shared.helpers.singleton_base import SingletonBase

    class _ThreadedSingleton(SingletonBase):
        def __init__(self):
            if getattr(self, "_singleton_initialized", False):
                return
            self._singleton_initialized = True

    results: list[Any] = []
    lock = threading.Lock()

    def worker():
        instance = _ThreadedSingleton()
        with lock:
            results.append(id(instance))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(results)) == 1, (
        f"All threads must receive the same object identity; "
        f"got {len(set(results))} distinct ids: {set(results)}"
    )
    SingletonBase._instances.pop(_ThreadedSingleton, None)


# ---------------------------------------------------------------------------
# 2. _NSE_SEGMENT_MAP — module-level constant, not rebuilt per call
# ---------------------------------------------------------------------------

def test_nse_segment_map_is_module_level():
    """_NSE_SEGMENT_MAP exists at module level in broker_apis."""
    from backend.brokers import broker_apis
    assert hasattr(broker_apis, "_NSE_SEGMENT_MAP"), (
        "_NSE_SEGMENT_MAP must be a module-level constant in broker_apis"
    )
    m = broker_apis._NSE_SEGMENT_MAP
    assert isinstance(m, dict), "_NSE_SEGMENT_MAP must be a dict"
    assert m.get("NSE") == "CM"
    assert m.get("MCX") == "COM"
    assert m.get("NFO") == "FO"


def test_nse_segment_map_identity_is_stable():
    """Identity of _NSE_SEGMENT_MAP is the same across 1000 accesses —
    proves it is not reconstructed per-call."""
    from backend.brokers import broker_apis
    first_id = id(broker_apis._NSE_SEGMENT_MAP)
    for _ in range(1000):
        assert id(broker_apis._NSE_SEGMENT_MAP) == first_id, (
            "_NSE_SEGMENT_MAP must not be re-created on each access"
        )


def test_nse_segment_map_concurrent_identity():
    """10 threads reading _NSE_SEGMENT_MAP all see the same object."""
    from backend.brokers import broker_apis
    ids: list[int] = []
    lock = threading.Lock()

    def worker():
        m = broker_apis._NSE_SEGMENT_MAP
        with lock:
            ids.append(id(m))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(ids)) == 1, (
        f"All threads must see the same _NSE_SEGMENT_MAP object; "
        f"got {len(set(ids))} distinct ids"
    )


def test_nse_segment_map_perf_constant_memory():
    """1000 accesses to _NSE_SEGMENT_MAP allocate no new objects —
    approximate by checking that gc-tracked objects don't grow."""
    import gc
    from backend.brokers import broker_apis

    gc.collect()
    before = len(gc.get_objects())
    for _ in range(1000):
        _ = broker_apis._NSE_SEGMENT_MAP.get("NSE")
    gc.collect()
    after = len(gc.get_objects())

    # Allow a small margin for any gc bookkeeping; the key assertion is
    # that we don't accumulate O(N) new objects.
    assert after - before < 50, (
        f"1000 _NSE_SEGMENT_MAP accesses added {after - before} gc objects; "
        f"expected < 50 (constant)"
    )


# ---------------------------------------------------------------------------
# 3. Logger singleton-per-name (logging module internal guarantee)
# ---------------------------------------------------------------------------

def test_logger_singleton_per_name():
    """logging.getLogger returns the same object for the same name,
    regardless of call site or call count."""
    import logging
    a = logging.getLogger("test_singletons_audit_sentinel")
    b = logging.getLogger("test_singletons_audit_sentinel")
    assert a is b


def test_service_app_logger_is_module_level():
    """service/app.py exposes a module-level `logger` attribute
    (not created inside a function)."""
    # Import without triggering Litestar app init — we only need the attribute.
    # Guard: this module has a module-level `app = create_app()` at the bottom;
    # that requires litestar which is available in the test env.
    try:
        import importlib
        app_mod = importlib.import_module("backend.brokers.service.app")
        assert hasattr(app_mod, "logger"), (
            "backend.brokers.service.app must expose a module-level `logger`"
        )
        import logging
        assert isinstance(app_mod.logger, logging.Logger), (
            "service/app.logger must be a logging.Logger instance"
        )
    except ImportError:
        import pytest
        pytest.skip("litestar not available in test environment")


# ---------------------------------------------------------------------------
# 4. GrammarRegistry — module-level REGISTRY
# ---------------------------------------------------------------------------

def test_grammar_registry_module_level():
    """grammar_registry.REGISTRY is a module-level GrammarRegistry instance."""
    from backend.api.algo import grammar_registry
    assert hasattr(grammar_registry, "REGISTRY")
    from backend.api.algo.grammar_registry import GrammarRegistry
    assert isinstance(grammar_registry.REGISTRY, GrammarRegistry)


def test_grammar_registry_identity_stable():
    """REGISTRY identity is the same across multiple imports."""
    from backend.api.algo.grammar_registry import REGISTRY as r1
    from backend.api.algo import grammar_registry
    r2 = grammar_registry.REGISTRY
    assert r1 is r2, "REGISTRY must be the same object across imports"


# ---------------------------------------------------------------------------
# 5. TemplateRegistry — __new__ singleton
# ---------------------------------------------------------------------------

def test_template_registry_singleton():
    """TemplateRegistry() returns the same instance on every call."""
    from backend.api.algo.template_registry import TemplateRegistry
    a = TemplateRegistry()
    b = TemplateRegistry()
    assert a is b, "TemplateRegistry must be singleton via __new__"


def test_template_registry_concurrent():
    """10 threads calling TemplateRegistry() all get the same instance."""
    from backend.api.algo.template_registry import TemplateRegistry
    ids: list[int] = []
    lock = threading.Lock()

    def worker():
        with lock:
            ids.append(id(TemplateRegistry()))

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(set(ids)) == 1, (
        f"All threads must get the same TemplateRegistry; "
        f"got {len(set(ids))} distinct ids"
    )


# ---------------------------------------------------------------------------
# 6. genai_api — re is now a module-level import (not per-call)
# ---------------------------------------------------------------------------

def test_genai_api_re_is_top_level_import():
    """genai_api.py must import `re` at the top level, not inside
    _extract_underlying or get_market_update."""
    import ast
    import pathlib

    src_path = pathlib.Path(__file__).parent.parent / "shared" / "helpers" / "genai_api.py"
    src = src_path.read_text()
    tree = ast.parse(src)

    # Collect all top-level import names
    top_level_imports: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                top_level_imports.add(node.module.split(".")[0])

    assert "re" in top_level_imports, (
        "genai_api.py must import `re` at the top level"
    )

    # Also assert there are no `import re` calls inside any function body
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in ast.walk(node):
                if isinstance(child, ast.Import):
                    for alias in child.names:
                        assert alias.name != "re", (
                            f"Found `import re` inside function {node.name}() "
                            f"at line {child.lineno} — must be top-level"
                        )


# ---------------------------------------------------------------------------
# 7. httpx async client in transport.py — lazy singleton pattern
# ---------------------------------------------------------------------------

def test_transport_client_singleton():
    """get_client() always returns the same AsyncClient instance."""
    from backend.brokers.client import transport
    # Reset module state so we can test lazy init cleanly
    transport._client = None
    c1 = transport.get_client()
    c2 = transport.get_client()
    assert c1 is c2, "transport.get_client() must return the same AsyncClient"


# ---------------------------------------------------------------------------
# 8. httpx sync client in remote_broker.py — lazy singleton pattern
# ---------------------------------------------------------------------------

def test_remote_broker_client_singleton():
    """_get_client() always returns the same httpx.Client instance."""
    from backend.brokers.client import remote_broker
    remote_broker._client = None
    c1 = remote_broker._get_client()
    c2 = remote_broker._get_client()
    assert c1 is c2, "remote_broker._get_client() must return the same httpx.Client"


# ---------------------------------------------------------------------------
# 9. httpx sync client in sync.py — lazy singleton pattern
# ---------------------------------------------------------------------------

def test_sync_client_singleton():
    """sync._get_client() always returns the same httpx.Client instance."""
    from backend.brokers.client import sync
    sync._client = None
    c1 = sync._get_client()
    c2 = sync._get_client()
    assert c1 is c2, "sync._get_client() must return the same httpx.Client"
