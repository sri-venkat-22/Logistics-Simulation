"""REST: network / nodes / shipments / KPIs, trust + eval read models, chaos injection, health, metrics."""
from __future__ import annotations

import asyncio
import json
import time
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
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


def ctx_of(request: Request) -> Ctx:
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
              "live_mult": snap.get("lanes", {}).get(l.id, {}).get("live_mult", 1.0),
              "extra_mult": snap.get("lanes", {}).get(l.id, {}).get("extra_mult", 1.0)} for l in ctx.net.lanes.values()]
    skus = [{"id": s.id, "family": s.family, "name": s.name, "unit_value": s.unit_value, "cold_chain": s.cold_chain}
            for s in ctx.net.skus.values()]
    return {"nodes": nodes, "lanes": lanes, "skus": skus, "counts": {"nodes": len(nodes), "lanes": len(lanes)}}


@lru_cache(maxsize=16)
def _criticality(alpha: float) -> dict:
    from sim.optimize.criticality import criticality
    return criticality(alpha=alpha)


@lru_cache(maxsize=1)
def _flow_model():
    from sim.macro.network import Network
    from sim.optimize.criticality import FlowModel
    return FlowModel(Network())


@router.get("/network/criticality", tags=["network"])
async def network_criticality(alpha: float = Query(0.25, ge=0, le=5)) -> dict:
    """Single points of failure: betweenness, flow share, Motter-Lai cascade reach, Simchi-Levi REI -> SPOF score."""
    return await asyncio.to_thread(_criticality, round(alpha, 3))


@router.get("/network/cascade/{node_id}", tags=["network"])
async def network_cascade(node_id: str, alpha: float = Query(0.25, ge=0, le=5)) -> dict:
    """The cascade after one node fails, step by step (which nodes and lanes fail when, unserved demand share)."""
    from sim.optimize.criticality import cascade
    fm = _flow_model()
    if node_id not in fm.net.nodes or fm.net.nodes[node_id].type == "zone":
        raise HTTPException(404, "unknown node (or a demand zone)")
    return await asyncio.to_thread(cascade, fm, node_id, round(alpha, 3))


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
    for (layer, _code), v in live.reject_reasons.items():
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


LAYERS = [("L1", "Schema", "Pydantic v2 strict envelope + typed payload, finite numbers, ranges"),
          ("L2", "Authenticity", "per-device HMAC-SHA256 (rotatable keys), publisher batch HMAC + nonce + timestamp window"),
          ("L3", "Temporal", "dedupe by message id; replays behind the source's clock; stale timestamps"),
          ("L4", "Physics", "implied speed <= 200 km/h between accepted fixes"),
          ("L5", "Map-matching", "city fixes within ~150 m of a SUMO road edge; national fixes on a road corridor"),
          ("L6", "State estimation", "dead-reckoning Kalman filter with learned velocity bias; chi-square innovation gate"),
          ("L7", "Twin oracle", "planned-route corridor from ASNs; stock / port / flow divergence from the live twin"),
          ("L8", "Feed anomaly", "MAD z-score + IsolationForest on ASNs; stock vs ASN reconciliation"),
          ("L9", "Reputation + SLA", "Beta reputation per source (time-decayed); SLA silence -> PREDICTED")]


@router.get("/trust/layers", tags=["trust"])
async def trust_layers(request: Request) -> dict:
    """The 9 layers with live reject counts, the lowest-reputation sources and recent twin divergences."""
    ctx = ctx_of(request)
    live = ctx.live
    by_layer: dict[str, dict] = {}
    for (layer, code), v in live.reject_reasons.items():
        by_layer.setdefault(layer, {})[code] = v
    eng = ctx.trust.engine if ctx.trust else None
    return {"layers": [{"id": lid, "name": name, "technique": tech, "rejected": sum(by_layer.get(lid, {}).values()),
                        "by_code": by_layer.get(lid, {})} for lid, name, tech in LAYERS],
            "divergences": [d.__dict__ for d in list(eng.divergences)[-50:]][::-1] if eng else [],
            "reputation": eng.rep.table(20) if eng else [],
            "silent_sources": sum(1 for s in live.sources.values() if s["state"] == "silent")}


@router.get("/trust/benchmark", tags=["trust"])
async def trust_benchmark() -> dict:
    """Red-team benchmark score of the full pipeline (python -m services.api.app.trust_bench)."""
    return _read_json(ROOT / "docs" / "trust" / "benchmark.json") or {}


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
    out: dict[str, Any] = {"streams": {}, "db": {"written": dict(ctx.writer.written), "errors": ctx.writer.errors,
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


# ------------------------------------------------------------------------------ ML (Phase 7.3)
@lru_cache(maxsize=1)
def _eta_model():
    from ml.eta import EtaModel
    return EtaModel() if EtaModel.available() else None


@router.get("/ml/metrics", tags=["ml"])
async def ml_metrics() -> dict:
    """ETA (twin + DataCo), demand forecast (+ the (s,S) policy test) and feed-anomaly evaluation reports."""
    d = ROOT / "docs" / "ml"
    return {k: _read_json(d / f"{k}.json") for k in ("eta", "forecast", "anomaly")}


@router.get("/ml/eta", tags=["ml"])
async def ml_eta(request: Request, lane: str, weather: float = Query(1.0, ge=0.1, le=10),
                 at: str | None = Query(None, description="departure time (ISO); default: the live twin's clock")) -> dict:
    """P10 / P50 / P90 transit time for a lane departure (LightGBM quantile models on reality-emulator history)."""
    ctx = ctx_of(request)
    if lane not in ctx.net.lanes:
        raise HTTPException(404, "unknown lane")
    m = _eta_model()
    if m is None:
        raise HTTPException(503, "ETA models not trained: python -m ml.eta")
    from datetime import datetime
    when = datetime.fromisoformat(at) if at else datetime.fromisoformat(ctx.twin.snapshot()["ts"])
    cal = bool(ctx.twin and ctx.twin.twin.lanes[lane].calibration)
    return m.predict(ctx.net, lane, when, weather, cal)


@router.get("/ml/eta/shipments", tags=["ml"])
async def ml_eta_shipments(request: Request, limit: int = Query(50, ge=1, le=500)) -> list[dict]:
    """ETA bands for shipments in transit in the live twin (the current leg)."""
    ctx = ctx_of(request)
    m = _eta_model()
    if m is None or ctx.twin is None:
        return []
    from datetime import datetime, timedelta
    snap = ctx.twin.snapshot()
    now = datetime.fromisoformat(snap["ts"])
    out = []
    for s in snap["shipments"]:
        if s["status"] != "transit":
            continue
        mult = snap["lanes"].get(s["lane"], {}).get("extra_mult", 1.0)
        p = m.predict(ctx.net, s["lane"], now, mult)
        left = max(0.0, 1 - s["progress"])
        out.append({"shipment": s["id"], "sku": s["sku"], "lane": s["lane"], "from": s["from"], "to": s["to"], "progress": s["progress"],
                    "twin_eta": (now + timedelta(hours=max(0.0, s["eta_leg_h"] - snap["t_h"]))).isoformat(timespec="minutes"),
                    "p10_h": round(p["p10_h"] * left, 2), "p50_h": round(p["p50_h"] * left, 2), "p90_h": round(p["p90_h"] * left, 2)})
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------------------ chaos
class ChaosRequest(BaseModel):
    type: str = Field(description=f"one of {', '.join(ATTACK_TYPES)}")
    target: str | None = None
    params: dict = Field(default_factory=dict)
    duration_s: float | None = Field(None, ge=0, le=86_400, description="simulated seconds (timed attacks)")


@router.post("/chaos/inject", status_code=202, tags=["chaos"])
async def chaos_inject(body: ChaosRequest, request: Request, who: Identity = Depends(require("security"))) -> dict:
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
