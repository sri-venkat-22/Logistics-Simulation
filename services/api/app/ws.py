"""WS /ws/live: live state fan-out at 5-10 Hz.

Frames are msgpack (binary) by default; ?format=json sends JSON text frames (handy with `wscat`).
The first frame is a full snapshot, every later frame a diff:
    {"type": "snapshot", "seq", "t_ms", "world_ts", "vehicles": [[id, lon, lat, speed_kmh, heading, t_ms, scope, status,
      shipment], ...], "inventory": {"DC/SKU": {...}}, "ports": {...}, "kpis": {...}, "alerts": [...]}
    {"type": "diff", "seq", "t_ms", "world_ts", "vehicles": {"u": [rows], "r": [ids]}, "inventory": {...changed},
      "ports": {...changed}, "alerts": [new], "kpis": {...} (once a second)}
scope 0 national / 1 city; status 0 live / 1 predicted (dead-reckoned after its source went silent).
Backpressure: each client has a small queue; when it is full the oldest frame is dropped (never the
connection) and the next snapshot re-syncs the client.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

import msgpack
import orjson
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services.api.app import metrics as M

if TYPE_CHECKING:
    from services.api.app.main import Ctx

log = logging.getLogger("aegis.ws")
router = APIRouter(tags=["live"])
QUEUE = 16


def _plain(o):
    """msgpack fallback for numpy scalars and other odd types."""
    return o.item() if hasattr(o, "item") else str(o)


class Client:
    def __init__(self, ws: WebSocket, fmt: str):
        self.ws, self.fmt = ws, fmt
        self.q: asyncio.Queue = asyncio.Queue(maxsize=QUEUE)
        self.resync = False

    def offer(self, frame: dict) -> None:
        if self.q.full():
            try:
                self.q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self.resync = True  # a diff was lost: send a fresh snapshot next
            M.WS_DROPPED.inc()
        self.q.put_nowait(frame)

    def encode(self, frame: dict):
        if self.fmt == "json":
            return orjson.dumps(frame, option=orjson.OPT_NON_STR_KEYS | orjson.OPT_SERIALIZE_NUMPY, default=str).decode()
        return msgpack.packb(frame, use_bin_type=True, default=_plain)


class Broadcaster:
    def __init__(self, ctx: "Ctx"):
        self.ctx = ctx
        self.clients: set[Client] = set()
        self.seq = 0
        self.last_kpi = 0.0
        self.frames = 0

    def kpis(self) -> dict:
        live, twin = self.ctx.live, self.ctx.twin
        vs = live.vehicles.values()
        out = {
            "ingest_rate": round(live.sample_rate(), 1),
            "ingest_accepted": live.counters["ingest_accepted"],
            "ingest_rejected": live.counters["ingest_rejected"],
            "quarantined": live.counters["quarantined"],
            "vehicles_live": sum(1 for v in vs if v["status"] == "live"),
            "vehicles_predicted": sum(1 for v in vs if v["status"] == "predicted"),
            "city_vehicles": sum(1 for v in vs if v.get("scope") == "city"),
            "sources_silent": sum(1 for s in live.sources.values() if s["state"] == "silent"),
            "world_ts": live.world_now.isoformat(timespec="seconds") if live.world_now else None,
            "ws_clients": len(self.clients),
        }
        if twin is not None:
            out["twin"] = twin.kpis()
        return out

    def snapshot(self) -> dict:
        live = self.ctx.live
        return {"type": "snapshot", "seq": self.seq, "t_ms": int(time.time() * 1000),
                "world_ts": live.world_now.isoformat(timespec="seconds") if live.world_now else None,
                "vehicles": [live.vehicle_row(k, v) for k, v in live.vehicles.items()],
                "inventory": live.inventory_view(), "ports": dict(live.ports), "kpis": self.kpis(),
                "alerts": [a.to_dict() for a in list(live.alerts)[:50]]}

    def tick(self) -> dict | None:
        veh, removed, inv, ports, alerts = self.ctx.live.take_dirty()
        now = time.monotonic()
        send_kpis = now - self.last_kpi >= 1.0
        if not (veh or removed or inv or ports or alerts or send_kpis):
            return None
        self.seq += 1
        frame = {"type": "diff", "seq": self.seq, "t_ms": int(time.time() * 1000),
                 "world_ts": self.ctx.live.world_now.isoformat(timespec="seconds") if self.ctx.live.world_now else None,
                 "vehicles": {"u": veh, "r": removed}, "inventory": inv, "ports": ports, "alerts": alerts}
        if send_kpis:
            frame["kpis"] = self.kpis()
            self.last_kpi = now
        return frame

    async def run(self, stop: asyncio.Event) -> None:
        period = 1.0 / max(1.0, min(10.0, self.ctx.settings.ws_hz))
        while not stop.is_set():
            t0 = time.monotonic()
            try:
                frame = self.tick()
                if frame is not None:
                    self.frames += 1
                    for c in list(self.clients):
                        if c.resync:
                            c.resync = False
                            c.offer(self.snapshot())
                        else:
                            c.offer(frame)
            except Exception as e:
                log.warning("broadcast tick failed: %s", e)
            await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))


@router.websocket("/ws/live")
async def live_ws(ws: WebSocket) -> None:
    ctx: Ctx = ws.app.state.ctx
    fmt = "json" if ws.query_params.get("format") == "json" else "msgpack"
    await ws.accept()
    client = Client(ws, fmt)
    b = ctx.broadcaster
    client.offer(b.snapshot())
    b.clients.add(client)
    M.WS_CLIENTS.labels("live").set(len(b.clients))

    async def sender():
        while True:
            frame = await client.q.get()
            try:
                data = client.encode(frame)
            except Exception as e:  # never let one bad frame kill the client's stream
                log.warning("could not encode a %s frame: %s", frame.get("type"), e)
                continue
            if isinstance(data, str):
                await ws.send_text(data)
            else:
                await ws.send_bytes(data)
            M.WS_FRAMES.labels("live").inc()

    task = asyncio.create_task(sender())
    try:
        while True:  # the client may send {"type": "resync"} to ask for a fresh snapshot
            msg = await ws.receive_text()
            if "resync" in msg:
                client.offer(b.snapshot())
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        b.clients.discard(client)
        M.WS_CLIENTS.labels("live").set(len(b.clients))
