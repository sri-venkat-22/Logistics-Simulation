"""Scenario jobs: Monte Carlo in a worker pool with streamed progress and a spec-hash result cache.

POST /scenarios              {"spec": RunSpec | "template": name, "n": 200, "baseline": true} -> job
                             cache key = sha256(canonical JSON of {spec, n, seeds}); a repeated what-if is
                             answered from Redis at once (cached: true)
GET  /scenarios/{id}         status, progress, result (KPI bands, series bands, stock-out probability, TTS)
                             and, when the paired baseline is done, KPI deltas (P50 scenario - P50 baseline)
WS   /ws/scenarios/{id}      progress events {"status", "done", "n", ...} until done / error
POST /scenarios/{id}/optimize  candidate plans from sim.optimize.optimizer (reroute via k-shortest paths, switch
                             sourcing, min-cost-flow stock transfers, expedite by air, buffer, combinations),
                             each evaluated with Monte Carlo on the same seeds (common random numbers); Pareto
                             front + weighted score; an explanation from one evidence replication's event log
GET  /scenarios/{id}/plans   re-rank with the planner's weights (service, risk = CVaR95 shortfall, cost, CO2)
POST /plans/{id}/apply       planner+: push the plan's actions into the live twin and to the fleet (stream
                             plan.commands -> the Reality Emulator, whose SUMO trucks take the new routes); audit_log row
GET  /plans/dispatched       shipments the fleet dispatched for applied plans (the live map highlights them)
Jobs run replication chunks in a ProcessPoolExecutor (spawn), so the API event loop stays responsive.
"""
from __future__ import annotations

import asyncio
import contextlib
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
from sim.optimize import optimizer
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
    def __init__(self, ctx: Ctx, workers: int):
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

    @staticmethod
    def job_id(spec: RunSpec, n: int) -> str:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"aegis:{cache_key(spec, n)}").hex[:16]

    async def submit(self, spec: RunSpec, n: int, kind: str = "scenario", user: str = "anonymous", parent: str | None = None,
                     extra: dict | None = None) -> dict:
        """extra: fields stored with the job from the start (e.g. baseline_id), so the running job never drops them."""
        key = cache_key(spec, n)
        r = self.ctx.redis
        sid = self.job_id(spec, n)
        extra = extra or {}
        st = await self.get(sid)
        if st and st["status"] in ("queued", "running"):
            return st
        if st and st["status"] == "done" and await r.exists(f"scn:result:{key}"):
            M.SCENARIO_JOBS.labels("cache_hit").inc()
            if any(k not in st for k in extra):
                st.update({k: v for k, v in extra.items() if k not in st})
                await self.put(sid, st)
            return {**st, "cached": True}  # same spec, same job: never overwrite it (it may carry a baseline_id)
        cached = await r.get(f"scn:result:{key}")
        if cached:
            st = {"id": sid, "kind": kind, "status": "done", "cached": True, "done": n, "n": n, "spec_hash": key,
                  "spec": orjson.loads(spec.model_dump_json()), "parent": parent, "wall_s": 0.0, **extra}
            await self.put(sid, st)
            M.SCENARIO_JOBS.labels("cache_hit").inc()
            return st
        st = {"id": sid, "kind": kind, "status": "queued", "cached": False, "done": 0, "n": n, "spec_hash": key,
              "spec": orjson.loads(spec.model_dump_json()), "parent": parent, "created_by": user, **extra}
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


def impact(ctx: Ctx, spec: RunSpec) -> dict:
    """What the scenario disrupts, for drawing it on a map: nodes (with remaining capacity), lanes (time
    multiplier), zones (demand multiplier) and the DC x SKU pairs whose stock depends on them."""
    from sim.macro.disruptions import build_effect
    twin = ctx.twin.twin
    nodes: dict[str, float] = {}
    lanes: dict[str, float] = {}
    zones: dict[str, float] = {}
    deps: set[tuple[str, str]] = set()
    for sc in spec.scenarios:
        e = build_effect(sc, twin.net)
        for table in (e.node_factor, e.production_factor, e.dispatch_factor):
            for n, f in table.items():
                nodes[n] = min(nodes.get(n, 1.0), f)
        lanes.update(e.lane_mult)
        for (z, _), m in e.demand_mult.items():
            zones[z] = m
        deps |= twin.dependents(e)
    return {"nodes": nodes, "lanes": lanes, "zones": zones, "dependents": sorted(f"{d}/{k}" for d, k in deps),
            "polygons": [sc.polygon for sc in spec.scenarios if sc.polygon]}


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
    base = spec.model_copy(update={"scenarios": [], "path_choice": {}, "routes": {}, "transfers": []})
    with_base = body.baseline and bool(spec.scenarios)
    extra = {"baseline_id": s.job_id(base, body.n)} if with_base else None
    st = await s.submit(spec, body.n, "scenario", who.user, extra=extra)
    if with_base:
        await s.submit(base, body.n, "baseline", who.user, parent=st["id"])
        st["baseline_id"] = extra["baseline_id"]
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
    ctx = request.app.state.ctx
    plans = await ctx.redis.get(f"scn:plans:{sid}")
    if plans:
        st["plans"] = orjson.loads(plans)
    if ctx.twin is not None:
        st["impact"] = impact(ctx, RunSpec.model_validate(st["spec"]))
    return st


@router.websocket("/ws/scenarios/{sid}")
async def scenario_progress(ws: WebSocket, sid: str) -> None:
    ctx: Ctx = ws.app.state.ctx
    from services.api.app.auth import ws_allowed
    if not await ws_allowed(ws):
        await ws.close(code=1008)
        return
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
        with contextlib.suppress(Exception):
            await ws.close()


# ------------------------------------------------------------------------------ optimisation (Phase 7.1)
def _levels(ctx: Ctx) -> dict:
    return {k: v for k, v in ctx.twin._levels.items()} if ctx.twin else {}


def _evidence(spec_json: str, seed: int) -> list[dict]:
    from sim.optimize.optimizer import evidence_run
    return evidence_run(RunSpec.model_validate_json(spec_json), seed)


class Weights(BaseModel):
    service: float = Field(optimizer.DEFAULT_WEIGHTS["service"], ge=0, le=10)
    risk: float = Field(optimizer.DEFAULT_WEIGHTS["risk"], ge=0, le=10)
    cost: float = Field(optimizer.DEFAULT_WEIGHTS["cost"], ge=0, le=10)
    co2: float = Field(optimizer.DEFAULT_WEIGHTS["co2"], ge=0, le=10)


@router.post("/api/v1/scenarios/{sid}/optimize", status_code=202)
async def optimize(sid: str, request: Request, n: int = 100, lam: float = 1.0,
                   who: Identity = Depends(require("planner"))) -> dict:
    """Generate candidate plans (reroute, switch sourcing, reallocate, expedite, buffer, combinations) and evaluate
    each with Monte Carlo on the scenario's seeds. Ranked plans appear on GET /scenarios/{id} as `plans`."""
    ctx: Ctx = request.app.state.ctx
    st = await ctx.scenarios.get(sid)
    if st is None:
        raise HTTPException(404, "unknown scenario")
    spec = RunSpec.model_validate(st["spec"])
    if not spec.scenarios:
        raise HTTPException(422, "nothing to optimise: the scenario has no disruption")
    if st["status"] != "done":
        raise HTTPException(409, "the scenario is still running: optimise once its result is in")
    result = await ctx.scenarios.result(st)
    cands = await asyncio.to_thread(optimizer.candidates, ctx.twin.twin, list(spec.scenarios), result, _levels(ctx), lam)
    jobs = []
    for i, c in enumerate(cands):
        pst = await ctx.scenarios.submit(optimizer.apply_to_spec(spec, c["actions"]), n, "plan", who.user, parent=sid)
        jobs.append({**c, "job": pst["id"], "id": f"{sid}-P{i}"})
    await ctx.redis.delete(f"scn:plans:{sid}")
    t = asyncio.create_task(_collect_plans(ctx, sid, spec, jobs))
    ctx.scenarios.tasks.add(t)
    t.add_done_callback(ctx.scenarios.tasks.discard)
    audit(ctx.engine, who.user, "scenario.optimize", sid, {"candidates": [c["name"] for c in cands], "n": n})
    return {"scenario": sid, "candidates": [{"id": j["id"], "name": j["name"], "kind": j["kind"], "job": j["job"],
                                             "actions": j["actions"]} for j in jobs]}


async def _collect_plans(ctx: Ctx, sid: str, spec: RunSpec, jobs: list[dict]) -> None:
    results: dict[str, dict | None] = {}
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
    for j in jobs:
        r = results[j["job"]]
        if r is not None:
            plans.append({"id": j["id"], "name": j["name"], "kind": j["kind"], "actions": j["actions"], "job": j["job"],
                          **optimizer.metrics(r)})
    if not plans:
        return
    optimizer.rank(plans)
    # explanations: one evidence replication per plan (event log on), in the worker pool
    loop = asyncio.get_running_loop()
    specs = {p["id"]: optimizer.apply_to_spec(spec, p["actions"]).model_dump_json() for p in plans}
    try:
        evs = await asyncio.gather(*[loop.run_in_executor(ctx.scenarios._pool(), _evidence, specs[p["id"]], spec.seed)
                                     for p in plans])
        base = next((p for p in plans if p["kind"] == "baseline"), None)
        base_ev = evs[plans.index(base)] if base else []
        for p, ev in zip(plans, evs):
            p["explanation"] = optimizer.explain(p, base, ev, base_ev, ctx.net)
    except Exception as e:  # explanations are a courtesy: never lose the ranked plans over them
        log.warning("plan explanations failed: %s", e)
    await ctx.redis.set(f"scn:plans:{sid}", orjson.dumps(plans), ex=ctx.settings.cache_ttl_s)
    for p in plans:
        await ctx.redis.set(f"plan:{p['id']}", orjson.dumps(p), ex=ctx.settings.cache_ttl_s)
        execute(ctx.engine, "insert into plans(id, scenario_id, actions, kpis, score) values (:id, :s, cast(:a as jsonb), "
                "cast(:k as jsonb), :sc) on conflict (id) do update set actions = excluded.actions, kpis = excluded.kpis, "
                "score = excluded.score",
                id=p["id"], s=sid, a=orjson.dumps(p["actions"]).decode(),
                k=orjson.dumps({x: p[x] for x in ("service", "otif", "cost_lakh", "co2_t", "cvar95_lakh", "stockout_p", "pareto")}).decode(),
                sc=p["score"])
    await ctx.redis.publish(f"scn:progress:{sid}", orjson.dumps({"id": sid, "status": "done", "plans": len(plans)}))


@router.get("/api/v1/scenarios/{sid}/plans")
async def ranked_plans(sid: str, request: Request, service: float = optimizer.DEFAULT_WEIGHTS["service"],
                       risk: float = optimizer.DEFAULT_WEIGHTS["risk"], cost: float = optimizer.DEFAULT_WEIGHTS["cost"],
                       co2: float = optimizer.DEFAULT_WEIGHTS["co2"]) -> dict:
    """The evaluated plans re-ranked with the planner's weights (Pareto front + weighted score)."""
    w = Weights(service=service, risk=risk, cost=cost, co2=co2)
    raw = await request.app.state.ctx.redis.get(f"scn:plans:{sid}")
    if raw is None:
        raise HTTPException(404, "no plans yet: POST /scenarios/{id}/optimize first")
    return {"weights": w.model_dump(), "plans": optimizer.rank(orjson.loads(raw), w.model_dump())}


@router.get("/api/v1/plans/dispatched")
async def plans_dispatched(request: Request) -> dict[str, Any]:
    """Shipments the fleet dispatched for applied plans ({shipment id: plan id}), so the map can highlight them."""
    raw = await request.app.state.ctx.redis.hgetall("plan.dispatch")
    return {"shipments": {k.decode(): v.decode() for k, v in raw.items()}}


@router.get("/api/v1/plans/{pid}")
async def get_plan(pid: str, request: Request) -> dict:
    raw = await request.app.state.ctx.redis.get(f"plan:{pid}")
    if raw is None:
        raise HTTPException(404, "unknown plan")
    return orjson.loads(raw)


@router.post("/api/v1/plans/{pid}/apply")
async def apply_plan(pid: str, request: Request, who: Identity = Depends(require("planner"))) -> dict[str, Any]:
    """Human-approved apply: push the plan's actions into the live twin (routes, transfers, policy buffers)."""
    ctx: Ctx = request.app.state.ctx
    raw = await ctx.redis.get(f"plan:{pid}")
    if raw is None:
        raise HTTPException(404, "unknown plan")
    plan = orjson.loads(raw)
    acks = []
    for ev in optimizer.to_twin_events(plan["actions"]):
        try:
            acks.append(ctx.twin.apply(ev))
        except ValueError as e:  # e.g. a donor DC closed since the plan was made: apply the rest, report this one
            acks.append({"ok": False, "type": ev["type"], "error": str(e)})
    # the approved plan also goes to the fleet (the Reality Emulator in the demo): reality executes the new routes
    # and transfers, so SUMO trucks take the new city corridors and their GPS shows the reroute
    events = optimizer.to_twin_events(plan["actions"])
    await ctx.redis.xadd("plan.commands", {"c": orjson.dumps({"id": pid, "name": plan["name"], "events": events,
                                                                "by": who.user})}, maxlen=1000, approximate=True)
    audit(ctx.engine, who.user, "plan.apply", pid, {"actions": plan["actions"], "role": who.role, "acks": acks})
    execute(ctx.engine, "update plans set applied_at = now(), applied_by = :u where id = :id", u=who.user, id=pid)
    ok = sum(1 for a in acks if a.get("ok"))
    ctx.live.alert("ai", f"Plan applied · {plan['name']}", f"{ok}/{len(acks)} actions pushed to the live twin by {who.user}",
                   kind="plan")
    return {"plan": pid, "applied_by": who.user, "acks": acks}


# ------------------------------------------------------------------------------ live disruptions
class DisruptionRequest(BaseModel):
    template: str | None = None
    scenario: dict | None = Field(None, description="a scenario DSL object (instead of a template)")
    start: str = Field("now", description='when it begins in the live twin: "now", "+6h", ...')
    duration_h: float | None = Field(None, gt=0, le=24 * 90, description="override the template's duration")


@router.post("/api/v1/disruptions", status_code=202, tags=["scenarios"])
async def push_disruption(body: DisruptionRequest, request: Request, who: Identity = Depends(require("planner"))) -> dict:
    """Push a scenario into the *live* twin (not just a what-if): its effect shows on the Control Tower map."""
    ctx: Ctx = request.app.state.ctx
    if body.template:
        sc = template(body.template)
    elif body.scenario:
        sc = Scenario.model_validate(body.scenario)
    else:
        raise HTTPException(422, "give a template or a scenario")
    upd: dict[str, Any] = {"start": body.start}
    if body.duration_h:
        upd["duration_h"] = body.duration_h
    sc = Scenario.model_validate({**sc.model_dump(mode="json"), **upd})
    try:
        ack = ctx.twin.apply({"type": "scenario", "scenario": sc})
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    did = uuid.uuid4().hex[:16]
    execute(ctx.engine, "insert into disruptions(id, spec, start_ts, status) values (:id, cast(:s as jsonb), now(), 'active')",
            id=did, s=sc.model_dump_json())
    audit(ctx.engine, who.user, "disruption.push", sc.target, {"spec": orjson.loads(sc.model_dump_json()), "id": did})
    ctx.live.alert("bad", f"Disruption pushed to the live twin · {sc.name or sc.type.value}",
                   f"{sc.type.value} @ {sc.target} · {sc.duration_h:g} h · start {sc.start} · by {who.user}",
                   node=sc.target if sc.target in ctx.net.nodes else None, kind="disruption")
    return {"id": did, "scenario": orjson.loads(sc.model_dump_json()), "twin": ack,
            "impact": impact(ctx, RunSpec(days=30, scenarios=[sc]))}


@router.get("/api/v1/disruptions", tags=["scenarios"])
async def list_disruptions(request: Request) -> list[dict]:
    """Disruptions active in the live twin (scenario pushes, live events and random outages)."""
    ctx: Ctx = request.app.state.ctx
    return ctx.twin.snapshot()["effects"] if ctx.twin else []


@router.delete("/api/v1/disruptions/{effect_id}", tags=["scenarios"])
async def end_disruption(effect_id: str, request: Request, who: Identity = Depends(require("planner"))) -> dict:
    ctx: Ctx = request.app.state.ctx
    try:
        ack = ctx.twin.apply({"type": "disruption_end", "id": effect_id})
    except ValueError as e:
        raise HTTPException(404, str(e)) from None
    audit(ctx.engine, who.user, "disruption.end", effect_id, {})
    ctx.live.alert("good", "Disruption ended", f"{effect_id} lifted in the live twin by {who.user}", kind="disruption")
    return ack
