"""backend.api.middleware — optional / opt-in ASGI middleware.

Everything in this package is dev-only or gated behind an env flag.
Production paths pay zero cost when the flag is not set (middleware
is not registered at all in `backend/api/app.py`).
"""
