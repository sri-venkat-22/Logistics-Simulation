"""REST: network / nodes / shipments / KPIs, trust + eval read models, chaos injection, health, metrics."""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, Field

from services.api.app import metrics as M
from services.api.app.auth import Identity, require
from services.api.app.db.store import audit, execute
from sim.paths import DATA, ROOT
from sim.reality.attacks import ATTACK_TYPES, EXPECTED

if TYPE_CHECKING:
    from services.api.app.main import Ctx

router = APIRouter(prefix="/api/v1")
ops = APIRouter(tags=["ops"])


def ctx_of(request: Request) -> "Ctx":
    return request.app.state.ctx


def _read_json(path) -> dict | None:
    return json.loads(path.read_text()) if path.exists() else None


RESILIENCE = {r["node"]: r for r in (_read_json(ROOT / "docs" / "sim" / "resilience.json") or {"nodes": []})["nodes"]}


# ------------------------------------------------------------------------------ network
@router.get("/network", tags=["network"])
async def network(request: Request) -> dict:
    ctx = ctx_of(request)
    snap = ctx.twin.snapshot() if ctx.twin else {"nodes": {}}
    nodes = []
    for n in ctx.net.nodes.values():
        sn = snap["nodes"].get(n.id, {})
        observed = ctx.live.ports.get(n.id, {}).get("status")
        r = RESILIENCE.get(n.id, {})
        nodes.append({"id": n.id, "type": n.type, "name": n.name, "lat": n.lat, "lon": n.lon,
                      "capacity": None if n.capacity == float("inf") else n.capacity, "attrs": n.attrs,
                      "status": observed or sn.get("status", "up"), "twin_status": sn.get("status", "up"),
                      "degree": ctx.graph.degree(n.id), "tts_d": r.get("tts_days"), "ttr_d": r.get("ttr_days"),
                      "rei": r.get("rei")})
    lanes = [{"id": l.id, "from_id": l.from_id, "to_id": l.to_id, "mode": l.mode, "distance_km": l.distance_km,
              "cost_per_unit": l.cost_per_unit, "capacity": l.capacity, "co2_per_tkm": l.co2_per_tkm, "lt_mu": l.lt_mu,
              "lt_sigma": l.lt_sigma, "lt_mean_h": l.lt_mean_h,
              "live_mult": snap.get("lanes", {}).get(l.id, {}).get("live_mult", 1.0)} for l in ctx.net.lanes.values()]
    skus = [{"id": s.id, "family": s.family, "name": s.name, "unit_value": s.unit_value, "cold_chain": s.cold_chain}
            for s in ctx.net.skus.values()]
    return {"nodes": nodes, "lanes": lanes, "skus": skus, "counts": {"nodes": len(nodes), "lanes": len(lanes)}}


@router.get("/nodes/{node_id}", tags=["network"])
async def node(node_id: str, request: Request) -> dict:
    ctx = ctx_of(request)
    n = ctx.net.nodes.get(node_id)
    if n is None:
        raise HTTPException(404, "unknown node")
    snap = ctx.twin.snapshot() if ctx.twin else {"nodes": {}, "inventory": {}, "shipments": []}
    inv = []
    for (dc, sku), row in sorted(ctx.live.inventory.items()):
        if dc == node_id:
            inv.append({"sku": sku, "observed": row, "twin": snap["inventory"].get(f"{dc}/{sku}")})
    if not inv:
        inv = [{"sku": k.split("/")[1], "observed": None, "twin": v} for k, v in snap["inventory"].items() if k.startswith(node_id + "/")]
    ships = snap["shipments"]
    r = RESILIENCE.get(node_id, {})
    return {"id": n.id, "type": n.type, "name": n.name, "lat": n.lat, "lon": n.lon, "attrs": n.attrs,
            "twin": snap["nodes"].get(node_id), "port": ctx.live.ports.get(node_id), "inventory": inv,
            "inbound": [s for s in ships if s["to"] == node_id][:20], "outbound": [s for s in ships if s["from"] == node_id][:20],
            "tts_ttr": {"tts_d": r.get("tts_days"), "ttr_d": r.get("ttr_days"), "exposed": r.get("exposed"), "rei": r.get("rei")},
            "alerts": [a.to_dict() for a in ctx.live.alerts if a.node == node_id][:20]}


@router.get("/shipments", tags=["network"])
async def shipments(request: Request, limit: int = 500) -> dict:
    ctx = ctx_of(request)
    snap = ctx.twin.snapshot() if ctx.twin else {"shipments": []}
    vehicles = [ctx.live.vehicle_row(k, v) for k, v in list(ctx.live.vehicles.items())[:limit]]
    return {"twin": snap["shipments"][:limit], "vehicles": vehicles,
            "vehicle_columns": ["id", "lon", "lat", "speed_kmh", "heading", "t_ms", "scope", "status", "shipment"]}


@router.get("/kpis", tags=["network"])
async def kpis(request: Request) -> dict:
    return ctx_of(request).broadcaster.kpis()


@router.get("/live/snapshot", tags=["live"])
async def live_snapshot(request: Request) -> dict:
    return ctx_of(request).broadcaster.snapshot()


# ------------------------------------------------------------------------------ trust
@router.get("/trust/quarantine", tags=["trust"])
async def quarantine(request: Request, limit: int = 100) -> list[dict]:
    return list(ctx_of(request).live.quarantine)[:limit]


@router.get("/trust/stats", tags=["trust"])
async def trust_stats(request: Request) -> dict:
    live = ctx_of(request).live
    by_layer: dict[str, int] = {}
    for (layer, code), v in live.reject_reasons.items():
        by_layer[layer] = by_layer.get(layer, 0) + v
    return {"accepted": live.counters["ingest_accepted"], "rejected_l1": live.counters["ingest_rejected"],
            "quarantined": live.counters["quarantined"], "by_layer": by_layer,
            "by_reason": {f"{l}:{c}": v for (l, c), v in live.reject_reasons.items()}}


@router.get("/trust/sources", tags=["trust"])
async def trust_sources(request: Request, state: str | None = None, limit: int = 500) -> list[dict]:
    live = ctx_of(request).live
    out = [{"id": k, "kind": s["kind"], "state": s["state"], "count": s["count"],
            "last_ts": s["last_ts"].isoformat(timespec="seconds")} for k, s in live.sources.items() if state in (None, s["state"])]
    return out[:limit]


# ------------------------------------------------------------------------------ eval
@router.get("/eval/verification", tags=["eval"])
async def eval_verification() -> dict:
    return _read_json(ROOT / "docs" / "sim" / "engine_verification.json") or {}


@router.get("/eval/benchmark", tags=["eval"])
async def eval_benchmark() -> dict:
    return _read_json(DATA / "benchmark" / "manifest.json") or {}


@router.get("/eval/resilience", tags=["eval"])
async def eval_resilience() -> dict:
    return _read_json(ROOT / "docs" / "sim" / "resilience.json") or {}


@router.get("/eval/pipeline", tags=["eval"])
async def eval_pipeline(request: Request) -> dict:
    ctx = ctx_of(request)
    r = ctx.redis
    out = {"streams": {}, "db": {"written": dict(ctx.writer.written), "errors": ctx.writer.errors,
                                 "last_flush_ms": round(ctx.writer.last_flush_ms, 1), "timeseries_mode": ctx.ts_mode},
           "trust_processed": ctx.trust.processed if ctx.trust else 0,
           "twin_processed": ctx.twin_state.processed if ctx.twin_state else 0, "ws_frames": ctx.broadcaster.frames}
    for stream, group in (("telemetry.raw", "trust"), ("telemetry.clean", "twin")):
        length = await r.xlen(stream)
        try:
            pending = (await r.xpending(stream, group))["pending"]
        except Exception:
            pending = None
        out["streams"][stream] = {"length": length, "pending": pending}
        if pending is not None:
            M.STREAM_LAG.labels(stream, group).set(pending)
    return out


# ------------------------------------------------------------------------------ chaos
class ChaosRequest(BaseModel):
    type: str = Field(description=f"one of {', '.join(ATTACK_TYPES)}")
    target: str | None = None
    params: dict = Field(default_factory=dict)
    duration_s: float | None = Field(None, ge=0, le=86_400, description="simulated seconds (timed attacks)")


@router.post("/chaos/inject", status_code=202, tags=["chaos"])
async def chaos_inject(body: ChaosRequest, request: Request, who: Identity = Depends(require("admin"))) -> dict:
    if body.type not in ATTACK_TYPES:
        raise HTTPException(422, f"unknown attack type; use one of {ATTACK_TYPES}")
    ctx = ctx_of(request)
    aid = f"CH{int(time.time() * 1000) % 10**10:010d}"
    cmd = {"id": aid, "type": body.type, "target": body.target, "params": body.params, "duration_s": body.duration_s,
           "by": who.user}
    await ctx.redis.xadd("chaos.commands", {"c": orjson.dumps(cmd)}, maxlen=10_000, approximate=True)
    execute(ctx.engine, "insert into attack_labels(id, type, target, injected_by) values (:id, :t, :g, :u)",
            id=aid, t=body.type, g=body.target, u=who.user)
    audit(ctx.engine, who.user, "chaos.inject", body.target, cmd)
    layer, reason = EXPECTED[body.type]
    ctx.live.alert("sec", f"Chaos injected · {body.type.replace('_', ' ')}",
                   f"{aid} by {who.user}; expected detection {layer} {reason}", kind="chaos")
    return {"id": aid, "queued": True, "expected_layer": layer, "expected_reason": reason}


@router.get("/chaos/attack-types", tags=["chaos"])
async def attack_types() -> list[dict]:
    return [{"type": t, "expected_layer": l, "expected_reason": r} for t, (l, r) in EXPECTED.items()]


# ------------------------------------------------------------------------------ ops
@ops.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@ops.get("/readyz")
async def readyz(request: Request, response: Response) -> dict:
    ctx = ctx_of(request)
    checks = {}
    try:
        checks["redis"] = bool(await ctx.redis.ping())
    except Exception:
        checks["redis"] = False
    if ctx.engine is not None:
        try:
            execute(ctx.engine, "select 1")
            checks["db"] = True
        except Exception:
            checks["db"] = False
    checks["twin"] = ctx.twin is not None
    ok = all(checks.values())
    response.status_code = 200 if ok else 503
    return {"ready": ok, "checks": checks, "timeseries_mode": ctx.ts_mode}


@ops.get("/metrics")
async def metrics(request: Request) -> Response:
    ctx = ctx_of(request)
    for t, n in ctx.writer.written.items():
        M.DB_ROWS.labels(t).set(n)
    return Response(generate_latest(M.REGISTRY), media_type=CONTENT_TYPE_LATEST)
