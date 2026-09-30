"""Scenario jobs: Monte Carlo in a worker pool with streamed progress and a spec-hash result cache.

POST /scenarios              {"spec": RunSpec | "template": name, "n": 200, "baseline": true} -> job
                             cache key = sha256(canonical JSON of {spec, n, seeds}); a repeated what-if is
                             answered from Redis at once (cached: true)
GET  /scenarios/{id}         status, progress, result (KPI bands, series bands, stock-out probability, TTS)
                             and, when the paired baseline is done, KPI deltas (P50 scenario - P50 baseline)
WS   /ws/scenarios/{id}      progress events {"status", "done", "n", ...} until done / error
POST /scenarios/{id}/optimize  candidate plans (do nothing, reroute the affected DC x SKU pairs onto each
                             alternate sourcing path, raise safety stock) evaluated with the same seeds
                             (common random numbers), scored on service / cost / CO2
POST /plans/{id}/apply       planner+: push the plan's actions into the live twin; audit_log row
Jobs run replication chunks in a ProcessPoolExecutor (spawn), so the API event loop stays responsive.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import multiprocessing as mp
import time
import uuid
from concurrent.futures import ProcessPoolExecutor
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from services.api.app import metrics as M
from services.api.app.auth import Identity, require
from services.api.app.db.store import audit, execute
from sim.macro.montecarlo import RunSpec, run_one, summarize
from sim.paths import SCENARIO_TEMPLATES
from sim.scenarios.dsl import Scenario

if TYPE_CHECKING:
    from services.api.app.main import Ctx

log = logging.getLogger("aegis.scenarios")
router = APIRouter(tags=["scenarios"])
CHUNK = 10


class ScenarioRequest(BaseModel):
    spec: RunSpec | None = None
    template: str | None = Field(None, description="scenario template name, e.g. 'cyclone' (instead of spec.scenarios)")
    n: int = Field(200, ge=1, le=2000)
    days: float = Field(30, gt=0, le=120)
    baseline: bool = Field(True, description="also run (or reuse) the no-disruption baseline for KPI deltas")


def _chunk(spec_json: str, seeds: list[int]) -> list[dict]:
    spec = RunSpec.model_validate_json(spec_json)
    return [run_one(spec, s) for s in seeds]


def cache_key(spec: RunSpec, n: int) -> str:
    body = orjson.dumps({"spec": orjson.loads(spec.canonical_json()), "n": n}, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(body).hexdigest()


def template(name: str) -> Scenario:
    p = SCENARIO_TEMPLATES / f"{name.removesuffix('.json')}.json"
    if not p.exists():
        raise HTTPException(404, f"unknown template {name!r}")
    return Scenario.load(p)


class ScenarioService:
    def __init__(self, ctx: "Ctx", workers: int):
        self.ctx = ctx
        self.workers = max(1, workers)
        self.pool: ProcessPoolExecutor | None = None
        self.tasks: set[asyncio.Task] = set()

    def _pool(self) -> ProcessPoolExecutor:
        if self.pool is None:
            self.pool = ProcessPoolExecutor(self.workers, mp_context=mp.get_context("spawn"))
        return self.pool

    def shutdown(self) -> None:
        for t in self.tasks:
            t.cancel()
        if self.pool:
            self.pool.shutdown(wait=False, cancel_futures=True)

    # ------------------------------------------------------------------ state in Redis
    async def get(self, sid: str) -> dict | None:
        raw = await self.ctx.redis.get(f"scn:{sid}")
        return orjson.loads(raw) if raw else None

    async def put(self, sid: str, st: dict) -> None:
        await self.ctx.redis.set(f"scn:{sid}", orjson.dumps(st), ex=self.ctx.settings.cache_ttl_s)
        await self.ctx.redis.publish(f"scn:progress:{sid}", orjson.dumps({k: v for k, v in st.items() if k != "result"}))

    async def submit(self, spec: RunSpec, n: int, kind: str = "scenario", user: str = "anonymous", parent: str | None = None) -> dict:
        key = cache_key(spec, n)
        r = self.ctx.redis
        sid = uuid.uuid5(uuid.NAMESPACE_URL, f"aegis:{key}").hex[:16]
        st = await self.get(sid)
        if st and st["status"] in ("queued", "running"):
            return st
        if st and st["status"] == "done" and await r.exists(f"scn:result:{key}"):
            M.SCENARIO_JOBS.labels("cache_hit").inc()
            return {**st, "cached": True}  # same spec, same job: never overwrite it (it may carry a baseline_id)
        cached = await r.get(f"scn:result:{key}")
        if cached:
            st = {"id": sid, "kind": kind, "status": "done", "cached": True, "done": n, "n": n, "spec_hash": key,
                  "spec": orjson.loads(spec.model_dump_json()), "parent": parent, "wall_s": 0.0}
            await self.put(sid, st)
            M.SCENARIO_JOBS.labels("cache_hit").inc()
            return st
        st = {"id": sid, "kind": kind, "status": "queued", "cached": False, "done": 0, "n": n, "spec_hash": key,
              "spec": orjson.loads(spec.model_dump_json()), "parent": parent, "created_by": user}
        await self.put(sid, st)
        execute(self.ctx.engine, "insert into scenarios(id, spec, spec_hash, n_reps, status, created_by) values "
                "(:id, cast(:spec as jsonb), :h, :n, 'queued', :u) on conflict (id) do update set status = 'queued'",
                id=sid, spec=spec.model_dump_json(), h=key, n=n, u=user)
        t = asyncio.create_task(self._run(sid, spec, n, key))
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return st

    async def _run(self, sid: str, spec: RunSpec, n: int, key: str) -> None:
        st = await self.get(sid)
        st["status"] = "running"
        await self.put(sid, st)
        t0 = time.perf_counter()
        loop = asyncio.get_running_loop()
        seeds = [spec.seed + i for i in range(n)]
        payload = spec.model_dump_json()
        try:
            futs = [loop.run_in_executor(self._pool(), _chunk, payload, seeds[i:i + CHUNK]) for i in range(0, n, CHUNK)]
            runs: list[dict] = []
            last_pub = 0.0
            for f in asyncio.as_completed(futs):
                runs.extend(await f)
                st["done"] = len(runs)
                if time.perf_counter() - last_pub > 0.15 or len(runs) == n:
                    await self.put(sid, st)
                    last_pub = time.perf_counter()
            runs.sort(key=lambda x: x["seed"])
            res = summarize(spec, runs)
            res["wall_s"] = round(time.perf_counter() - t0, 3)
            await self.ctx.redis.set(f"scn:result:{key}", orjson.dumps(res), ex=self.ctx.settings.cache_ttl_s)
            st.update(status="done", wall_s=res["wall_s"])
            await self.put(sid, st)
            execute(self.ctx.engine, "update scenarios set status = 'done', results = cast(:r as jsonb) where id = :id",
                    id=sid, r=orjson.dumps({k: res[k] for k in ("kpis", "per_sku_fill", "stockout_prob", "tts", "wall_s", "n")}).decode())
            M.SCENARIO_JOBS.labels("done").inc()
            M.SCENARIO_SECONDS.observe(res["wall_s"])
        except Exception as e:
            log.exception("scenario %s failed", sid)
            st.update(status="error", error=repr(e))
            await self.put(sid, st)
            M.SCENARIO_JOBS.labels("error").inc()

    async def result(self, st: dict) -> dict | None:
        raw = await self.ctx.redis.get(f"scn:result:{st['spec_hash']}")
        return orjson.loads(raw) if raw else None


def _deltas(res: dict, base: dict) -> dict:
    out = {}
    for k, v in res["kpis"].items():
        b = base["kpis"].get(k)
        if b:
            out[k] = {"scenario_p50": v["p50"], "baseline_p50": b["p50"], "delta_p50": v["p50"] - b["p50"],
                      "delta_mean": v["mean"] - b["mean"]}
    return out


def svc(request: Request) -> ScenarioService:
    return request.app.state.ctx.scenarios


@router.post("/api/v1/scenarios", status_code=202)
async def create_scenario(body: ScenarioRequest, request: Request, who: Identity = Depends(require("planner"))) -> dict:
    s = svc(request)
    if body.spec is not None:
        spec = body.spec
    elif body.template:
        spec = RunSpec(days=body.days, scenarios=[template(body.template)])
    else:
        raise HTTPException(422, "give a spec or a template")
    st = await s.submit(spec, body.n, "scenario", who.user)
    if body.baseline and spec.scenarios:
        base = spec.model_copy(update={"scenarios": [], "path_choice": {}})
        bst = await s.submit(base, body.n, "baseline", who.user, parent=st["id"])
        st["baseline_id"] = bst["id"]
        stored = await s.get(st["id"])
        stored["baseline_id"] = bst["id"]
        await s.put(st["id"], stored)
    return st


@router.get("/api/v1/scenarios/templates")
async def list_templates() -> list[dict]:
    out = []
    for p in sorted(SCENARIO_TEMPLATES.glob("*.json")):
        sc = Scenario.load(p)
        out.append({"template": p.stem, "type": sc.type.value, "name": sc.name, "target": sc.target, "start": sc.start,
                    "duration_h": sc.duration_h, "severity": sc.severity, "description": sc.description,
                    "params": sc.params, "polygon": sc.polygon})
    return out


@router.get("/api/v1/scenarios/{sid}")
async def get_scenario(sid: str, request: Request, series: bool = True) -> dict:
    s = svc(request)
    st = await s.get(sid)
    if st is None:
        raise HTTPException(404, "unknown scenario")
    if st["status"] == "done":
        res = await s.result(st)
        if res is not None:
            if not series:
                res = {k: v for k, v in res.items() if k not in ("series", "fill_rate_series")}
            st["result"] = res
            if st.get("baseline_id"):
                bst = await s.get(st["baseline_id"])
                if bst and bst["status"] == "done":
                    base = await s.result(bst)
                    if base:
                        st["deltas"] = _deltas(res, base)
    plans = await request.app.state.ctx.redis.get(f"scn:plans:{sid}")
    if plans:
        st["plans"] = orjson.loads(plans)
    return st


@router.websocket("/ws/scenarios/{sid}")
async def scenario_progress(ws: WebSocket, sid: str) -> None:
    ctx: Ctx = ws.app.state.ctx
    await ws.accept()
    pubsub = ctx.redis.pubsub()
    await pubsub.subscribe(f"scn:progress:{sid}")
    try:
        st = await ctx.scenarios.get(sid)
        if st is None:
            await ws.send_json({"error": "unknown scenario"})
            return
        await ws.send_json({k: v for k, v in st.items() if k != "result"})
        while st["status"] not in ("done", "error"):
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
            if msg is None:
                st = await ctx.scenarios.get(sid) or st  # also poll: the job may finish between subscribe and get
                if st["status"] in ("done", "error"):
                    await ws.send_json({k: v for k, v in st.items() if k != "result"})
                continue
            st = orjson.loads(msg["data"])
            await ws.send_json(st)
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe()
        await pubsub.aclose()
        try:
            await ws.close()
        except Exception:
            pass


# ------------------------------------------------------------------------------ optimisation (v1)
def candidate_plans(ctx: "Ctx", spec: RunSpec) -> list[dict]:
    """Do nothing; reroute every affected DC x SKU pair onto alternate path k; raise safety stock."""
    from sim.macro.disruptions import build_effect
    twin = ctx.twin.twin
    affected: set[tuple[str, str]] = set()
    for sc in spec.scenarios:
        affected |= twin.dependents(build_effect(sc, twin.net))
    plans = [{"name": "Do nothing", "actions": []}]
    max_alt = max((len(twin.net.replenishment[p]) for p in affected), default=1)
    for k in range(1, max_alt):
        acts = [{"type": "set_path", "dc": dc, "sku": sku, "path": k} for dc, sku in sorted(affected)
                if len(twin.net.replenishment[(dc, sku)]) > k]
        if acts:
            via = sorted({twin.net.replenishment[(a["dc"], a["sku"])][k].nodes[1] for a in acts})
            plans.append({"name": f"Reroute via {', '.join(v.replace('PORT_', '').replace('DC_', '') for v in via[:3])}",
                          "actions": acts})
    fams = sorted({twin.net.skus[sku].family for _, sku in affected})
    if fams:
        plans.append({"name": f"Buffer: +0.8 safety factor ({', '.join(fams)})",
                      "actions": [{"type": "policy", "family": f, "dz": 0.8} for f in fams]})
    return plans


def plan_spec(spec: RunSpec, actions: list[dict]) -> RunSpec:
    from sim.macro.policies import FAMILY_POLICY
    pc = dict(spec.path_choice)
    pol = {k: dict(v) for k, v in (spec.policy or {}).items()}
    for a in actions:
        if a["type"] == "set_path":
            pc[f"{a['dc']}/{a['sku']}"] = a["path"]
        elif a["type"] == "policy":
            base = {**FAMILY_POLICY[a["family"]], **pol.get(a["family"], {})}
            pol[a["family"]] = {**base, "z": base["z"] + a["dz"]}
    return spec.model_copy(update={"path_choice": pc, "policy": pol or None})


@router.post("/api/v1/scenarios/{sid}/optimize", status_code=202)
async def optimize(sid: str, request: Request, n: int = 100, who: Identity = Depends(require("planner"))) -> dict:
    ctx: Ctx = request.app.state.ctx
    st = await ctx.scenarios.get(sid)
    if st is None:
        raise HTTPException(404, "unknown scenario")
    spec = RunSpec.model_validate(st["spec"])
    if not spec.scenarios:
        raise HTTPException(422, "nothing to optimise: the scenario has no disruption")
    cands = candidate_plans(ctx, spec)
    jobs = []
    for c in cands:
        pst = await ctx.scenarios.submit(plan_spec(spec, c["actions"]), n, "plan", who.user, parent=sid)
        jobs.append({**c, "job": pst["id"]})
    t = asyncio.create_task(_collect_plans(ctx, sid, jobs))
    ctx.scenarios.tasks.add(t)
    t.add_done_callback(ctx.scenarios.tasks.discard)
    return {"scenario": sid, "candidates": [{"name": j["name"], "job": j["job"], "actions": j["actions"]} for j in jobs]}


async def _collect_plans(ctx: "Ctx", sid: str, jobs: list[dict]) -> None:
    results = {}
    while len(results) < len(jobs):
        for j in jobs:
            if j["job"] in results:
                continue
            st = await ctx.scenarios.get(j["job"])
            if st and st["status"] == "done":
                results[j["job"]] = await ctx.scenarios.result(st)
            elif st and st["status"] == "error":
                results[j["job"]] = None
        await asyncio.sleep(0.25)
    plans = []
    for i, j in enumerate(jobs):
        r = results[j["job"]]
        if r is None:
            continue
        k = r["kpis"]
        plans.append({"id": f"{sid}-P{i}", "name": j["name"], "actions": j["actions"], "job": j["job"],
                      "service": k["fill_rate"]["p50"], "service_p10": k["fill_rate"]["p10"], "otif": k["otif"]["p50"],
                      "cost_lakh": k["cost_total"]["p50"] / 1e5, "co2_t": k["co2_t"]["p50"],
                      "backorders_p90": k["units_backordered"]["p90"],
                      "stockout_p": max(r["stockout_prob"].values()) if r["stockout_prob"] else 0.0})
    if plans:
        cmin = min(p["cost_lakh"] for p in plans)
        cmax = max(p["cost_lakh"] for p in plans)
        for p in plans:  # service first, then cost; CO2 as a tie-breaker (weights adjustable in Phase 7)
            cost_n = (p["cost_lakh"] - cmin) / (cmax - cmin) if cmax > cmin else 0.0
            p["score"] = round(0.7 * p["service_p10"] + 0.25 * (1 - cost_n) + 0.05 * (1 - p["stockout_p"]), 4)
        plans.sort(key=lambda p: -p["score"])
    await ctx.redis.set(f"scn:plans:{sid}", orjson.dumps(plans), ex=ctx.settings.cache_ttl_s)
    for p in plans:
        await ctx.redis.set(f"plan:{p['id']}", orjson.dumps(p), ex=ctx.settings.cache_ttl_s)
        execute(ctx.engine, "insert into plans(id, scenario_id, actions, kpis, score) values (:id, :s, cast(:a as jsonb), "
                "cast(:k as jsonb), :sc) on conflict (id) do update set kpis = excluded.kpis, score = excluded.score",
                id=p["id"], s=sid, a=orjson.dumps(p["actions"]).decode(),
                k=orjson.dumps({x: p[x] for x in ("service", "otif", "cost_lakh", "co2_t", "stockout_p")}).decode(), sc=p["score"])
    await ctx.redis.publish(f"scn:progress:{sid}", orjson.dumps({"id": sid, "status": "done", "plans": len(plans)}))


@router.post("/api/v1/plans/{pid}/apply")
async def apply_plan(pid: str, request: Request, who: Identity = Depends(require("planner"))) -> dict[str, Any]:
    ctx: Ctx = request.app.state.ctx
    raw = await ctx.redis.get(f"plan:{pid}")
    if raw is None:
        raise HTTPException(404, "unknown plan")
    plan = orjson.loads(raw)
    acks = []
    for a in plan["actions"]:
        if a["type"] == "set_path":
            acks.append(ctx.twin.apply({"type": "set_path", "dc": a["dc"], "sku": a["sku"], "path": a["path"]}))
        elif a["type"] == "policy":
            acks.append(ctx.twin.apply({"type": "policy_buffer", "family": a["family"], "dz": a["dz"]}))
    audit(ctx.engine, who.user, "plan.apply", pid, {"actions": plan["actions"], "role": who.role})
    execute(ctx.engine, "update plans set applied_at = now(), applied_by = :u where id = :id", u=who.user, id=pid)
    ctx.live.alert("ai", f"Plan applied · {plan['name']}", f"{len(plan['actions'])} actions pushed to the live twin by {who.user}",
                   kind="plan")
    return {"plan": pid, "applied_by": who.user, "acks": acks}
