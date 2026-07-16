"""
Persistence-layer DDL. Called from init_db() at startup (idempotent).

Tables:
  ohlcv_daily          — daily OHLCV bars (slice V)
  instruments_snapshot — per-exchange instrument list, one row per (exchange, date)
  holidays_snapshot    — trading holidays, one row per (exchange, year)
  intraday_bars        — sub-daily OHLCV bars (30min / 5min / 15min)
"""

from __future__ import annotations


async def create_ohlcv_daily_table(conn) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS ohlcv_daily (
            symbol      VARCHAR(64)      NOT NULL,
            exchange    VARCHAR(16)      NOT NULL,
            date        DATE             NOT NULL,
            open        NUMERIC(18, 4)   NOT NULL,
            high        NUMERIC(18, 4)   NOT NULL,
            low         NUMERIC(18, 4)   NOT NULL,
            close       NUMERIC(18, 4)   NOT NULL,
            volume      BIGINT           NOT NULL DEFAULT 0,
            captured_at TIMESTAMPTZ      NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, exchange, date)
        )
    """))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_ohlcv_daily_sym_date "
        "ON ohlcv_daily (symbol, date DESC)"
    ))


async def create_instruments_snapshot_table(conn) -> None:  # type: ignore[no-untyped-def]
    """instruments_snapshot — one row per (exchange, date), DO UPDATE on conflict.

    payload JSONB holds a slim list of {tradingsymbol, exchange, instrument_token}
    objects — enough to rebuild the (tradingsymbol, exchange) → token map without
    carrying the full 500 kB per-exchange instrument dump.

    row_count mirrors len(payload) so a quick SELECT row_count skips JSONB
    deserialization when checking completeness.

    Retention: 7 days (purged by _task_purge_persistence_caches).
    """
    from sqlalchemy import text

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS instruments_snapshot (
            exchange     VARCHAR(16)  NOT NULL,
            date         DATE         NOT NULL,
            payload      JSONB        NOT NULL,
            row_count    INTEGER      NOT NULL DEFAULT 0,
            captured_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, date)
        )
    """))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_instruments_snapshot_date "
        "ON instruments_snapshot (date DESC)"
    ))


async def create_holidays_snapshot_table(conn) -> None:  # type: ignore[no-untyped-def]
    """holidays_snapshot — one row per (exchange, year).

    dates_json JSONB holds a sorted array of YYYY-MM-DD strings.
    No retention purge — holiday years are tiny and useful indefinitely
    (backtests reference past years).
    """
    from sqlalchemy import text

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS holidays_snapshot (
            exchange     VARCHAR(16)  NOT NULL,
            year         SMALLINT     NOT NULL,
            dates_json   JSONB        NOT NULL,
            captured_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
            PRIMARY KEY (exchange, year)
        )
    """))


async def create_intraday_bars_table(conn) -> None:  # type: ignore[no-untyped-def]
    """intraday_bars — sub-daily OHLCV bars keyed (symbol, exchange, date, interval, bar_ts).

    interval is '30minute' for now; '5minute' / '15minute' are forward-reserved.
    bar_ts is TIMESTAMPTZ of the bar's CLOSE (Kite convention).

    Retention: 90 days (purged by _task_purge_persistence_caches).
    """
    from sqlalchemy import text

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS intraday_bars (
            symbol      VARCHAR(64)    NOT NULL,
            exchange    VARCHAR(16)    NOT NULL,
            date        DATE           NOT NULL,
            interval    VARCHAR(8)     NOT NULL,
            bar_ts      TIMESTAMPTZ    NOT NULL,
            open        NUMERIC(18, 4) NOT NULL,
            high        NUMERIC(18, 4) NOT NULL,
            low         NUMERIC(18, 4) NOT NULL,
            close       NUMERIC(18, 4) NOT NULL,
            volume      BIGINT         NOT NULL DEFAULT 0,
            captured_at TIMESTAMPTZ    NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, exchange, date, interval, bar_ts)
        )
    """))
    # Slice AQ caught the prior ix_intraday_bars_sym_date_interval
    # index as an exact duplicate of the primary key (PG already
    # maintains a B-tree on the PK columns). Dropped to halve the
    # write overhead + storage for this table — every PK-prefix
    # query plan stays identical.
    await conn.execute(text(
        "DROP INDEX IF EXISTS ix_intraday_bars_sym_date_interval"
    ))


async def create_broker_connection_events_table(conn) -> None:  # type: ignore[no-untyped-def]
    """broker_connection_events — per-account broker session lifecycle events.

    Records auth_fail, fetch_fail, token_ok, rotation_detected,
    fetch_ok_recovery, circuit_open, circuit_close, ticker_close,
    ticker_error, ticker_reconnect events from connections.py,
    broker_apis.py, dhan.py, and kite_ticker.py. Indexed by
    (account, event_ts DESC), (event_type, event_ts DESC), and
    event_ts DESC for the admin /broker-connection-events endpoint.

    No retention purge defined — operator manages manually.
    """
    from sqlalchemy import text

    await conn.execute(text("""
        CREATE TABLE IF NOT EXISTS broker_connection_events (
            id          SERIAL PRIMARY KEY,
            account     VARCHAR(32)  NOT NULL,
            broker_id   VARCHAR(32)  NOT NULL,
            event_type  VARCHAR(32)  NOT NULL,
            event_ts    TIMESTAMPTZ  NOT NULL DEFAULT now(),
            detail      JSONB
        )
    """))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_bce_account_ts "
        "ON broker_connection_events (account, event_ts DESC)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_bce_event_type_ts "
        "ON broker_connection_events (event_type, event_ts DESC)"
    ))
    await conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_bce_ts "
        "ON broker_connection_events (event_ts DESC)"
    ))
