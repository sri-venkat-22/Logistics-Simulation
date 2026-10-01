"""Database access: engine, master-data seeding, batched time-series writer (COPY), small helpers."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, UTC

import orjson
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from sim.macro.network import Network

log = logging.getLogger("aegis.db")


def make_engine(url: str) -> Engine | None:
    if not url:
        return None
    return create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)


def seed_master_data(engine: Engine, net: Network) -> None:
    """Idempotent upsert of nodes, lanes and SKUs from data/ (the network the twin runs on)."""
    with engine.begin() as c:
        for n in net.nodes.values():
            c.execute(text("insert into nodes(id, type, name, geom, capacity, attrs, status) values "
                           "(:id, :type, :name, ST_GeogFromText(:wkt), :cap, cast(:attrs as jsonb), 'up') "
                           "on conflict (id) do update set type = excluded.type, name = excluded.name, geom = excluded.geom, "
                           "capacity = excluded.capacity, attrs = excluded.attrs"),
                      {"id": n.id, "type": n.type, "name": n.name, "wkt": f"SRID=4326;POINT({n.lon} {n.lat})",
                       "cap": n.capacity if n.capacity != float("inf") else None, "attrs": orjson.dumps(n.attrs).decode()})
        for l in net.lanes.values():
            c.execute(text("insert into lanes(id, from_id, to_id, mode, distance_km, cost_per_unit, capacity, co2_per_tkm, lt_mu, lt_sigma) "
                           "values (:id, :f, :t, :m, :d, :c, :cap, :co2, :mu, :sg) on conflict (id) do update set "
                           "distance_km = excluded.distance_km, cost_per_unit = excluded.cost_per_unit, lt_mu = excluded.lt_mu, "
                           "lt_sigma = excluded.lt_sigma"),
                      {"id": l.id, "f": l.from_id, "t": l.to_id, "m": l.mode, "d": l.distance_km, "c": l.cost_per_unit,
                       "cap": l.capacity, "co2": l.co2_per_tkm, "mu": l.lt_mu, "sg": l.lt_sigma})
        for s in net.skus.values():
            c.execute(text("insert into skus(id, family, unit_value, perishable, shelf_life_h, cold_chain) values "
                           "(:id, :f, :v, :p, null, :cc) on conflict (id) do update set unit_value = excluded.unit_value"),
                      {"id": s.id, "f": s.family, "v": s.unit_value, "p": s.cold_chain, "cc": s.cold_chain})


RECENT_AUDIT: deque = deque(maxlen=1000)  # newest first; the audit read model when persistence is off


def audit(engine: Engine | None, user: str, action: str, target: str | None, details: dict) -> None:
    RECENT_AUDIT.appendleft({"ts": datetime.now(UTC).isoformat(timespec="seconds"), "user_id": user,
                             "action": action, "target": target, "details": details})
    if engine is None:
        return
    with engine.begin() as c:
        c.execute(text("insert into audit_log(user_id, action, target, details) values (:u, :a, :t, cast(:d as jsonb))"),
                  {"u": user, "a": action, "t": target, "d": orjson.dumps(details, option=orjson.OPT_NON_STR_KEYS).decode()})


def execute(engine: Engine | None, sql: str, **params) -> list[dict]:
    if engine is None:
        return []
    with engine.begin() as c:
        res = c.execute(text(sql), params)
        return [dict(r._mapping) for r in res] if res.returns_rows else []


class BatchWriter:
    """Buffers telemetry / inventory / quarantine rows and flushes them with COPY every second (or every
    `max_rows`), from a background thread, so the async consumers never block on the database."""

    TABLES = {
        "telemetry": ("ts", "vehicle_id", "lat", "lon", "speed", "heading", "source_id", "sig_ok", "trust", "flags", "msg_id"),
        "inventory": ("ts", "node_id", "sku_id", "on_hand", "on_order", "backorder", "source", "trust"),
        "quarantine": ("ts", "source_id", "payload", "reason_code", "layer"),
    }

    def __init__(self, engine: Engine | None, every_s: float = 1.0, max_rows: int = 20_000):
        self.engine, self.every_s, self.max_rows = engine, every_s, max_rows
        self.buf: dict[str, deque] = {t: deque() for t in self.TABLES}
        self.written = {t: 0 for t in self.TABLES}
        self.errors = 0
        self.last_flush_ms = 0.0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def add(self, table: str, row: tuple) -> None:
        if self.engine is None:
            return
        self.buf[table].append(row)
        if len(self.buf[table]) >= self.max_rows:
            self._wake.set()

    def start(self) -> None:
        if self.engine is None or self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="db-writer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.flush()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.every_s)
            self._wake.clear()
            self.flush()

    def flush(self) -> None:
        if self.engine is None:
            return
        t0 = time.perf_counter()
        for table, cols in self.TABLES.items():
            q = self.buf[table]
            rows = []
            while q:
                rows.append(q.popleft())
            if not rows:
                continue
            try:
                raw = self.engine.raw_connection()
                try:
                    with raw.cursor() as cur:  # type: ignore[attr-defined]  # psycopg 3 cursor
                        with cur.copy(f"copy {table} ({', '.join(cols)}) from stdin") as cp:
                            for r in rows:
                                cp.write_row(r)
                    raw.commit()
                finally:
                    raw.close()
                self.written[table] += len(rows)
            except Exception as e:  # never take the pipeline down because the DB hiccups
                self.errors += 1
                log.warning("batch write to %s failed (%d rows): %s", table, len(rows), e)
        self.last_flush_ms = (time.perf_counter() - t0) * 1000


def utcnow() -> datetime:
    return datetime.now(UTC)
