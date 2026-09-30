"""AEGIS API (modular monolith, ADR-0001): ingest -> trust -> twin state -> WebSocket, REST, scenario jobs.

    .venv/bin/uvicorn services.api.app.main:app --port 8000
    docs at http://localhost:8000/docs

One process hosts: the ingest gateway, the trust stage (telemetry.raw -> telemetry.clean), the twin-state
stage (telemetry.clean -> live state / Redis / Postgres), the SLA monitor, the WebSocket broadcaster, the
live macro twin (its own thread) and the scenario worker pool (its own processes). Phase 10 splits these
into containers; the Redis Streams consumer groups already allow several trust / twin-state workers.
"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import networkx as nx
import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from services.api.app import ingest, routes, scenarios, ws
from services.api.app.config import Settings, settings as default_settings
from services.api.app.db import timescale
from services.api.app.db.store import BatchWriter, make_engine, seed_master_data
from services.api.app.live_state import LiveState
from services.api.app.live_twin import LiveTwin
from services.api.app.pipeline import CLEAN, RAW, SlaMonitor, TrustStage, TwinStateStage, ensure_group, run_forever
from sim.macro.network import Network

log = logging.getLogger("aegis")


class Ctx:
    """Everything the handlers and workers share."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.redis = aioredis.Redis.from_url(settings.redis_url, decode_responses=False)
        self.engine = make_engine(settings.db_url)
        self.net = Network()
        self.graph: nx.MultiDiGraph = self.net.graph.copy()
        self.live = LiveState()
        self.writer = BatchWriter(self.engine)
        self.twin: LiveTwin | None = None
        self.trust: TrustStage | None = None
        self.twin_state: TwinStateStage | None = None
        self.sla = SlaMonitor(self, gps_sla_s=settings.source_sla_s)
        self.broadcaster = ws.Broadcaster(self)
        self.scenarios = scenarios.ScenarioService(self, settings.scenario_workers)
        self.stop = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.ts_mode = "none"

    def quarantine(self, m, code: str, layer: str) -> None:
        live = self.live
        src = m.get("source_id") if isinstance(m, dict) else None
        entry = {"t": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                 "world_ts": m.get("ts") if isinstance(m, dict) and isinstance(m.get("ts"), str) else None,
                 "source": src, "code": code, "layer": layer, "msg_id": m.get("msg_id") if isinstance(m, dict) else None,
                 "kind": m.get("kind") if isinstance(m, dict) else None}
        live.quarantine.appendleft(entry)
        live.counters["quarantined"] += 1
        live.reject_reasons[(layer, code)] += 1
        self.writer.add("quarantine", (datetime.now(timezone.utc), src, json.dumps(m, allow_nan=False, default=str)
                                       if _jsonable(m) else json.dumps({"raw": str(m)[:2000]}), code, layer))
        live.alert("sec", f"Quarantined · {code}", f"{layer} rejected a message from {src or 'unknown source'}",
                   kind="trust", key=f"q:{code}", min_gap_s=15)


def _jsonable(m) -> bool:
    try:
        json.dumps(m, allow_nan=False, default=str)
        return True
    except ValueError:
        return False


async def start(ctx: Ctx) -> None:
    s = ctx.settings
    await ctx.redis.ping()
    if ctx.engine is not None:
        try:
            seed_master_data(ctx.engine, ctx.net)
            with ctx.engine.connect() as c:
                ctx.ts_mode = timescale.mode(c)
        except Exception as e:
            log.warning("database unavailable, running without persistence: %s", e)
            ctx.engine = None
            ctx.writer.engine = None
    ctx.twin = LiveTwin(ctx.net, datetime.fromisoformat(s.twin_start), s.twin_factor, warmup_h=s.twin_warmup_h)
    await ensure_group(ctx.redis, RAW, TrustStage.group)
    await ensure_group(ctx.redis, CLEAN, TwinStateStage.group)
    ctx.trust = TrustStage(ctx)
    ctx.twin_state = TwinStateStage(ctx, ctx.writer)
    if not s.background:
        return
    ctx.writer.start()
    ctx.twin.start()

    async def sla_loop():
        while not ctx.stop.is_set():
            ctx.sla.sweep()
            await asyncio.sleep(1.0)

    ctx.tasks = [asyncio.create_task(run_forever("trust", ctx.trust.step, ctx.stop)),
                 asyncio.create_task(run_forever("twin-state", ctx.twin_state.step, ctx.stop)),
                 asyncio.create_task(sla_loop()),
                 asyncio.create_task(ctx.broadcaster.run(ctx.stop))]


async def stop(ctx: Ctx) -> None:
    ctx.stop.set()
    for t in ctx.tasks:
        t.cancel()
    await asyncio.gather(*ctx.tasks, return_exceptions=True)
    ctx.scenarios.shutdown()
    if ctx.twin:
        ctx.twin.stop()
    ctx.writer.stop()
    await ctx.redis.aclose()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        ctx = Ctx(settings)
        app.state.ctx = ctx
        await start(ctx)
        try:
            yield
        finally:
            await stop(ctx)

    app = FastAPI(title="AEGIS Twin API", version="0.4.0", lifespan=lifespan,
                  description="Ingest -> trust -> twin state -> WebSocket; scenarios; chaos; trust + eval read models.")
    # local dev: any localhost port (Vite picks 5173+); Phase 8 pins CORS to the deployed web origin
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
                       allow_methods=["*"], allow_headers=["*"])
    app.include_router(ingest.router)
    app.include_router(routes.router)
    app.include_router(routes.ops)
    app.include_router(scenarios.router)
    app.include_router(ws.router)
    return app


app = create_app()
