"""Per-vendor Broker adapter implementations.

Each module here implements `backend.brokers.base.Broker` for one
vendor's SDK. Adapters are wired into the registry via
`backend/brokers/registry.py:_ADAPTERS`. New vendors add a module
here + a row in `_ADAPTERS`."""
