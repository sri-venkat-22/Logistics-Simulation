"""Stream workers (Redis Streams consumer groups, at-least-once, idempotent):

    telemetry.raw  --[trust stage]-->  telemetry.clean  --[twin-state stage]-->  live state / Redis / DB
                          \\--> quarantine (reason code + layer)

Trust stage (Phase 4 core of the 9-layer pipeline; Phase 7 adds map-matching, Kalman, twin oracle,
feed anomaly and reputation):
    L2 HMAC_INVALID     device signature (per-device key = HMAC(master, source_id))
    L3 DUPLICATE_ID     msg_id already seen (Redis SET NX, 1 h) - catches duplicates and verbatim replays
    L3 STALE_TS         older than the source's last accepted message by > 60 s, or > 1 h behind the world clock
    L4 PHYSICS_TELEPORT GPS jump implying > 200 km/h since the vehicle's last accepted position
Twin-state stage: vehicles (hash veh:{id}), inventory (hash inv:{node} sku -> json), ports (hash port:{id}),
ASNs, source last-seen; NetworkX node attributes (status, stock_<sku>); batched COPY into telemetry /
inventory; alerts on stock below the reorder point, backorders, port status changes.
SLA monitor: vehicles silent > sla_s (world time) become PREDICTED; > 10 min they drop out; a burst of
newly silent sources raises a blackout alert (SLA_SILENT).
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import orjson

from services.api.app import metrics as M
from services.api.app.db.store import BatchWriter
from sim.reality.telemetry import Signer
from sim.scenarios.dsl import haversine_km

if TYPE_CHECKING:
    from services.api.app.main import Ctx

log = logging.getLogger("aegis.pipeline")
RAW, CLEAN, QUAR = "telemetry.raw", "telemetry.clean", "quarantine"
MAX_SPEED_KMH = 200.0
REACQUIRE_PINGS = 10  # a genuinely relocated vehicle is re-anchored after this many consistent fixes (teleport attacks: <= 5)


async def ensure_group(redis, stream: str, group: str) -> None:
    try:
        await redis.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as e:  # BUSYGROUP: already exists
        if "BUSYGROUP" not in str(e):
            raise


def _parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s)


class TrustStage:
    group = "trust"

    def __init__(self, ctx: "Ctx", consumer: str = "trust-1"):
        self.ctx, self.consumer = ctx, consumer
        self.signer = Signer(ctx.settings.master_key)
        self.last_src: dict[str, datetime] = {}
        self.last_pos: dict[str, tuple[float, float, datetime]] = {}
        self.candidate: dict[str, tuple[float, float, datetime, int]] = {}  # rejected-but-consistent track per vehicle
        self.processed = 0

    def check(self, m: dict, fresh: bool) -> tuple[str, str] | None:
        if not self.signer.verify(m):
            return "L2", "HMAC_INVALID"
        if not fresh:
            return "L3", "DUPLICATE_ID"
        ts = _parse_ts(m["ts"])
        src = m["source_id"]
        last = self.last_src.get(src)
        world = self.ctx.live.world_now
        if (last is not None and ts < last - timedelta(seconds=60)) or (world is not None and ts < world - timedelta(hours=1)):
            return "L3", "STALE_TS"
        if m["kind"] == "gps":
            p = m["payload"]
            prev = self.last_pos.get(p["vehicle_id"])
            if prev is not None and not self._plausible(prev, p, ts):
                vid = p["vehicle_id"]
                cand = self.candidate.get(vid)
                n = cand[3] + 1 if cand is not None and self._plausible(cand, p, ts) else 1
                self.candidate[vid] = (p["lat"], p["lon"], ts, n)
                if n < REACQUIRE_PINGS:
                    return "L4", "PHYSICS_TELEPORT"
                self.candidate.pop(vid, None)  # the "impossible" track has been consistent for long enough: re-anchor
                self.ctx.live.alert("warn", f"Track re-acquired · {vid}", f"{REACQUIRE_PINGS} consecutive self-consistent fixes "
                                    "after a physically impossible jump; the vehicle's track was re-anchored", kind="trust",
                                    key=f"reacq:{vid}", min_gap_s=60)
        return None

    @staticmethod
    def _plausible(prev: tuple, p: dict, ts: datetime) -> bool:
        d_km = haversine_km(prev[0], prev[1], p["lat"], p["lon"])
        dt_h = (ts - prev[2]).total_seconds() / 3600
        return d_km <= 1.0 or (dt_h > 0 and d_km / dt_h <= MAX_SPEED_KMH)

    def accept(self, m: dict) -> None:
        ts = _parse_ts(m["ts"])
        src = m["source_id"]
        if src not in self.last_src or ts > self.last_src[src]:
            self.last_src[src] = ts
        if m["kind"] == "gps":
            p = m["payload"]
            self.last_pos[p["vehicle_id"]] = (p["lat"], p["lon"], ts)

    async def step(self, block_ms: int = 200, count: int = 2000) -> int:
        r = self.ctx.redis
        resp = await r.xreadgroup(self.group, self.consumer, {RAW: ">"}, count=count, block=block_ms)
        if not resp:
            return 0
        entries = resp[0][1]
        msgs = [orjson.loads(f[b"m"]) for _, f in entries]
        pipe = r.pipeline(transaction=False)
        for m in msgs:
            pipe.set(f"seen:{m['msg_id']}", 1, nx=True, ex=3600)
        fresh = await pipe.execute()
        pipe = r.pipeline(transaction=False)
        for (eid, _), m, ok in zip(entries, msgs, fresh):
            verdict = self.check(m, bool(ok))
            if verdict is None:
                self.accept(m)
                pipe.xadd(CLEAN, {"m": orjson.dumps(m)}, maxlen=self.ctx.settings.stream_maxlen, approximate=True)
                M.CLEAN_MSGS.labels(m["kind"]).inc()
            else:
                layer, code = verdict
                self.ctx.quarantine(m, code, layer)
                pipe.xadd(QUAR, {"m": orjson.dumps(m)[:4000], "reason": code, "layer": layer}, maxlen=100_000, approximate=True)
                M.REJECTS.labels(layer, code).inc()
        pipe.xack(RAW, self.group, *[eid for eid, _ in entries])
        await pipe.execute()
        self.processed += len(entries)
        return len(entries)


class TwinStateStage:
    group = "twin"

    def __init__(self, ctx: "Ctx", writer: BatchWriter, consumer: str = "twin-1"):
        self.ctx, self.writer, self.consumer = ctx, writer, consumer
        self.stock_flags: dict[tuple[str, str], str] = {}
        self.processed = 0

    def apply(self, m: dict, pipe) -> None:
        live, kind, p = self.ctx.live, m["kind"], m["payload"]
        ts = _parse_ts(m["ts"])
        if live.world_now is None or ts > live.world_now:
            live.world_now = ts
        src = live.sources.setdefault(m["source_id"], {"kind": kind, "count": 0, "state": "live"})
        src["last_ts"], src["count"] = ts, src["count"] + 1
        if src.get("state") != "live":
            src["state"] = "live"
        t_ms = int(ts.timestamp() * 1000)
        if kind == "gps":
            vid = p["vehicle_id"]
            v = {"lon": p["lon"], "lat": p["lat"], "speed": p["speed_kmh"], "heading": p["heading_deg"], "t_ms": t_ms,
                 "scope": p.get("scope", "national"), "shipment": p.get("shipment_id"), "status": "live", "ts": ts}
            live.upsert_vehicle(vid, v)
            pipe.hset(f"veh:{vid}", mapping={"lon": p["lon"], "lat": p["lat"], "speed": p["speed_kmh"],
                                            "heading": p["heading_deg"], "ts": m["ts"], "scope": v["scope"],
                                            "shipment": v["shipment"] or ""})
            pipe.expire(f"veh:{vid}", 3600)
            self.writer.add("telemetry", (ts, vid, p["lat"], p["lon"], p["speed_kmh"], p["heading_deg"], m["source_id"],
                                          True, 1.0, [], m["msg_id"]))
        elif kind == "stock":
            key = (p["node"], p["sku"])
            row = {"on_hand": p["on_hand"], "on_order": p["on_order"], "backlog": p["backlog"], "ts": m["ts"]}
            rp = self.ctx.twin.reorder_point(*key) if self.ctx.twin else None
            if rp is not None:
                row["s"] = round(float(rp), 1)  # the twin's current reorder point, so clients can flag stock at risk
            live.inventory[key] = row
            live.dirty_inventory.add(key)
            pipe.hset(f"inv:{p['node']}", p["sku"], orjson.dumps(row))
            self.writer.add("inventory", (ts, p["node"], p["sku"], p["on_hand"], p["on_order"], p["backlog"], m["source_id"], 1.0))
            g = self.ctx.graph
            if p["node"] in g:
                g.nodes[p["node"]][f"stock_{p['sku']}"] = p["on_hand"]
            self._stock_alert(key, row)
        elif kind == "port":
            prev = live.ports.get(p["port"], {}).get("status")
            live.ports[p["port"]] = {**p, "ts": m["ts"]}
            live.dirty_ports.add(p["port"])
            pipe.hset(f"port:{p['port']}", mapping={k: str(v) for k, v in p.items()})
            if p["port"] in self.ctx.graph:
                self.ctx.graph.nodes[p["port"]]["status"] = p["status"]
            if prev is not None and prev != p["status"]:
                sev = {"closed": "bad", "degraded": "warn", "up": "good"}[p["status"]]
                live.alert(sev, f"{p['port']} {p['status']}", f"Port status changed {prev} → {p['status']} · "
                           f"{p['anchorage']} ships at anchorage, {p['berth_queue']} waiting for a berth",
                           node=p["port"], kind="port")
        elif kind == "asn":
            live.asns.appendleft({**p, "ts": m["ts"]})
            pipe.hset(f"asn:{p['asn_id']}", mapping={k: str(v) for k, v in p.items()})
            pipe.expire(f"asn:{p['asn_id']}", 7 * 24 * 3600)
        M.TWIN_APPLIED.labels(kind).inc()

    def _stock_alert(self, key: tuple[str, str], row: dict) -> None:
        s = self.ctx.twin.reorder_point(*key) if self.ctx.twin else None
        flag = "bad" if row["backlog"] > 0 else ("warn" if s is not None and row["on_hand"] < s else "ok")
        prev = self.stock_flags.get(key)
        self.stock_flags[key] = flag
        if prev is None or prev == flag or flag == "ok":
            return
        node, sku = key
        if flag == "bad":
            self.ctx.live.alert("bad", f"Stock-out · {node.replace('DC_', '')} {sku.replace('SKU_', '')}",
                                f"{row['backlog']:,.0f} units backordered, on hand {row['on_hand']:,.0f}", node=node, kind="stock")
        else:
            self.ctx.live.alert("warn", f"Below reorder point · {node.replace('DC_', '')} {sku.replace('SKU_', '')}",
                                f"On hand {row['on_hand']:,.0f} < reorder point {s:,.0f}", node=node, kind="stock")

    async def step(self, block_ms: int = 200, count: int = 2000) -> int:
        r = self.ctx.redis
        resp = await r.xreadgroup(self.group, self.consumer, {CLEAN: ">"}, count=count, block=block_ms)
        if not resp:
            return 0
        entries = resp[0][1]
        pipe = r.pipeline(transaction=False)
        for _, f in entries:
            try:
                self.apply(orjson.loads(f[b"m"]), pipe)
            except Exception as e:  # a bad message must never stop the consumer
                log.warning("twin-state could not apply a message: %s", e)
        pipe.xack(CLEAN, self.group, *[eid for eid, _ in entries])
        await pipe.execute()
        self.processed += len(entries)
        return len(entries)


class SlaMonitor:
    def __init__(self, ctx: "Ctx", gps_sla_s: float = 30.0, other_sla_s: float = 1800.0, drop_s: float = 600.0):
        self.ctx, self.gps_sla, self.other_sla, self.drop = ctx, gps_sla_s, other_sla_s, drop_s

    def sweep(self) -> int:
        live = self.ctx.live
        now = live.world_now
        if now is None:
            return 0
        newly = 0
        for vid, v in list(live.vehicles.items()):
            age = (now - v["ts"]).total_seconds()
            if age > self.drop:
                live.remove_vehicle(vid)
            elif age > self.gps_sla and v["status"] != "predicted":
                v["status"] = "predicted"
                live.dirty_vehicles.add(vid)
        for sid, s in live.sources.items():
            sla = self.gps_sla if s["kind"] == "gps" else self.other_sla
            age = (now - s["last_ts"]).total_seconds()
            if age > sla and s["state"] == "live" and age < self.drop:
                s["state"] = "silent"
                newly += 1
            elif age >= self.drop and s["state"] != "gone":
                s["state"] = "gone"
        if newly >= 3:
            live.alert("sec", "Telemetry silent (SLA)", f"{newly} sources stopped reporting within {self.gps_sla:.0f} s; "
                       "affected vehicles switched to PREDICTED", kind="sla", key="sla", min_gap_s=10)
        n_live = sum(1 for v in live.vehicles.values() if v["status"] == "live")
        M.VEHICLES.labels("live").set(n_live)
        M.VEHICLES.labels("predicted").set(len(live.vehicles) - n_live)
        return newly


async def run_forever(name: str, step, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("%s failed a step: %s", name, e)
            await asyncio.sleep(0.5)


def finite(x) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x)
