"""TimescaleDB for the time-series tables (telemetry, inventory).

    python -m services.api.app.db.timescale            # convert to hypertables now (after installing TimescaleDB)

With the extension available (preloaded via shared_preload_libraries), both tables become hypertables
(1-day chunks) with native compression: segment by vehicle / node x SKU, order by time descending, and a
compression policy that compresses chunks older than 2 days. Without it they stay plain tables with a BRIN
index on ts, which keeps time-range scans cheap. schema_info.timeseries_mode records which one is active.
"""
from __future__ import annotations

import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection

HYPERTABLES = {
    "telemetry": {"segmentby": "vehicle_id", "compress_after": "2 days"},
    "inventory": {"segmentby": "node_id, sku_id", "compress_after": "2 days"},
}


def timescale_available(conn: Connection) -> bool:
    if not conn.execute(text("select 1 from pg_available_extensions where name = 'timescaledb'")).scalar():
        return False
    try:
        with conn.begin_nested():
            conn.execute(text("create extension if not exists timescaledb"))
        return True
    except Exception:  # installed but not preloaded
        return False


def set_mode(conn: Connection, mode: str) -> None:
    conn.execute(text("insert into schema_info(key, value) values ('timeseries_mode', :m) "
                      "on conflict (key) do update set value = excluded.value"), {"m": mode})


def enable(conn: Connection) -> str:
    """Make the time-series tables hypertables with compression if possible; return the active mode."""
    if not timescale_available(conn):
        for table in HYPERTABLES:
            conn.execute(text(f"create index if not exists ix_{table}_ts_brin on {table} using brin (ts)"))
        set_mode(conn, "plain+brin")
        return "plain+brin"
    for table, cfg in HYPERTABLES.items():
        conn.execute(text(f"select create_hypertable('{table}', 'ts', chunk_time_interval => interval '1 day', "
                          f"if_not_exists => true, migrate_data => true)"))
        conn.execute(text(f"alter table {table} set (timescaledb.compress, timescaledb.compress_segmentby = "
                          f"'{cfg['segmentby']}', timescaledb.compress_orderby = 'ts desc')"))
        conn.execute(text(f"select add_compression_policy('{table}', interval '{cfg['compress_after']}', if_not_exists => true)"))
    set_mode(conn, "timescale")
    return "timescale"


def mode(conn: Connection) -> str:
    return conn.execute(text("select value from schema_info where key = 'timeseries_mode'")).scalar() or "unknown"


def main() -> int:
    from services.api.app.config import settings
    eng = create_engine(settings.db_url)
    with eng.begin() as conn:
        m = enable(conn)
    print(f"time-series mode: {m}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
