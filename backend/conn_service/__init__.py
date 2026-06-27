"""Connection service — isolates broker session lifecycle from the
main Litestar API so backend restarts don't tear down Kite tokens,
KiteTicker, Dhan auth state, or Groww sessions.

See README in this directory for the architectural overview.
"""
