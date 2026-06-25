"""
OHLCV daily table + indexes. Called from init_db() at startup (idempotent).
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
