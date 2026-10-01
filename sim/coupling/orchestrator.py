"""Coupling orchestrator: one clock, macro (SimPy) <-> micro (SUMO) bridge.

Clock
  The macro twin owns time. In demo mode it runs on simpy.rt.RealtimeEnvironment (Twin(realtime_factor=
  wall-seconds per sim-hour); 60 = 1 sim-minute per wall-second). A sync process inside the macro env
  steps SUMO in lock-step every `sync_s` sim-seconds: SUMO time = (macro hours - t0) x 3600. Spawns are
  sent with an absolute SUMO depart time (always >= SUMO's clock, which only trails the macro), so trucks
  enter SUMO at exactly the macro instant; arrivals are fed back at the next sync (lag <= sync_s).

Bridge
  city leg      a macro lane with both ends in the micro-twin (L015, L016, L030, L031, L047): loading at the
                origin in macro (handling_h), then the drive happens in SUMO
  inbound leg   a road / air lane from outside into a Hyderabad node: long haul in macro up to the city
                gateway (hub nearest to where the route crosses the SUMO bbox), then SUMO to the node
  outbound leg  a road lane from a Hyderabad node to outside: loading, SUMO to the exit gateway, rest macro
  macro -> micro  shipment entering the region => spawn_truck (one truck per truck-load, capped)
  micro -> macro  last truck of a shipment arrives => Twin.apply({"type": "receive", ...})
  closure       road_flood effects carry sumo_edges: SUMO closes them and reroutes; the macro lane-time
                multipliers are updated from SUMO corridor times (routed estimate at once, then EWMA of
                observed truck times), so macro-only sampling and Monte Carlo forks see the detour
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from sim.macro.disruptions import Effect
from sim.macro.engine import Twin
from sim.macro.entities import Shipment
from sim.micro.build_hyderabad import BBOX
from sim.micro.calibrate import LANE_CORRIDORS, NODE_HUB
from sim.micro.process import MicroProcess

TRUCK_CAP = {"SKU_VAX": 400, "SKU_FMCG": 1000, "SKU_ELEC": 1500}
GATEWAY_MIN_KM = 8.0      # a destination this close to the city boundary gets no city leg
EWMA = 0.3


def _km(lat1, lon1, lat2, lon2) -> float:
    kx = 111.32 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon1 - lon2) * kx, (lat1 - lat2) * 110.57)


def bbox_crossing(lat_out: float, lon_out: float, lat_in: float, lon_in: float) -> tuple[float, float]:
    """Point where the straight line from an outside point to an inside point enters the micro bbox."""
    w, s, e, n = BBOX
    lo, hi = 0.0, 1.0
    for _ in range(40):  # bisection on the segment parameter
        mid = (lo + hi) / 2
        lat, lon = lat_out + (lat_in - lat_out) * mid, lon_out + (lon_in - lon_out) * mid
        if w <= lon <= e and s <= lat <= n:
            hi = mid
        else:
            lo = mid
    return lat_out + (lat_in - lat_out) * hi, lon_out + (lon_in - lon_out) * hi


@dataclass
class LegPlan:
    kind: str                 # city | inbound | outbound
    from_hub: str | None      # None = pick per shipment (not used today)
    to_hubs: list[str]        # one hub, or the consumer hubs for Z_HYD
    corridors: list[tuple[str, str]] = field(default_factory=list)
    handling_h: float = 4.0


@dataclass
class CityShipment:
    shipment: str
    lane: str
    trucks: set[str]
    enter_h: float
    arrivals: list[dict] = field(default_factory=list)


class CoupledTwin:
    def __init__(self, twin: Twin, micro: MicroProcess, sync_s: float = 60.0, max_trucks: int = 4,
                 couple_inbound: bool = True, watchdog_h: float = 4.0):
        self.twin, self.micro, self.sync_s = twin, micro, sync_s
        self.max_trucks, self.watchdog_h = max_trucks, watchdog_h
        self.t0_h = twin.env.now
        self.hubs = micro.hubs
        self.plans: dict[str, LegPlan] = self._plan_lanes(couple_inbound)
        self.waiting: dict[str, CityShipment] = {}
        self.truck_to_shp: dict[str, str] = {}
        self.fcd: dict[str, dict] = {}        # latest FCD row per truck
        self.fcd_frames = 0
        self.arrivals: list[dict] = []
        self.handshakes: list[dict] = []      # completed macro->micro->macro round trips (FR-5 evidence)
        self.fallbacks = 0
        self.closed_edges: set[str] = set()
        self.listeners = []                   # fn(kind, payload) for live streaming (fcd, arrival, ...)
        corr = sorted({c for p in self.plans.values() for c in p.corridors})
        base = micro.call("corridor_times", pairs=[list(c) for c in corr]) if corr else {}
        self.base_corridor_s = {tuple(k.split("|")): v for k, v in base.items()}
        self.obs_ratio: dict[tuple[str, str], float] = {}
        twin.coupler = self
        twin.env.process(self.sync_proc())
        twin.sync_clock()  # real-time mode: start the wall clock now, not when the twin was built
        twin.log("coupling_start", t0_h=self.t0_h, sync_s=sync_s, lanes=sorted(self.plans))

    # ------------------------------------------------------------------ planning
    def _plan_lanes(self, couple_inbound: bool) -> dict[str, LegPlan]:
        net = self.twin.net
        plans: dict[str, LegPlan] = {}
        for lid, (a, b) in ((l, cs[0]) for l, cs in LANE_CORRIDORS.items()):
            to = [c for _, c in LANE_CORRIDORS[lid]] if net.lanes[lid].to_id == "Z_HYD" else [b]
            plans[lid] = LegPlan("city", a, to, list(LANE_CORRIDORS[lid]))
        if not couple_inbound:
            return plans
        for l in net.lanes.values():
            if l.id in plans or l.mode not in ("road", "air"):
                continue
            inside_to, inside_from = l.to_id in NODE_HUB, l.from_id in NODE_HUB
            if inside_to and inside_from and l.mode == "road" and NODE_HUB[l.from_id] != NODE_HUB[l.to_id]:
                # an uncalibrated city lane, e.g. the transfer-only L053 Shamshabad -> Medchal used by optimiser plans
                a, b = NODE_HUB[l.from_id], NODE_HUB[l.to_id]
                plans[l.id] = LegPlan("city", a, [b], [(a, b)])
                continue
            if inside_to and not inside_from:
                hub = NODE_HUB[l.to_id]
                gw = "RGIA Air Cargo" if l.mode == "air" else self._gateway(l.from_id, hub)
                if gw and gw != hub:
                    plans[l.id] = LegPlan("inbound", gw, [hub], [(gw, hub)], handling_h=0.0)
            elif inside_from and not inside_to and l.mode == "road" and l.to_id != "Z_HYD":
                hub = NODE_HUB[l.from_id]
                gw = self._gateway(l.to_id, hub)
                if gw and gw != hub:
                    plans[l.id] = LegPlan("outbound", hub, [gw], [(hub, gw)])
        return plans

    def _gateway(self, outside_node: str, hub: str) -> str | None:
        o = self.twin.net.nodes[outside_node]
        h = self.hubs[hub]
        lat, lon = bbox_crossing(o.lat, o.lon, h["lat"], h["lon"])
        if _km(lat, lon, h["lat"], h["lon"]) < GATEWAY_MIN_KM:
            return None
        return min(self.hubs, key=lambda k: _km(lat, lon, self.hubs[k]["lat"], self.hubs[k]["lon"]))

    # ------------------------------------------------------------------ clocks
    def sumo_t(self, t_h: float) -> float:
        return (t_h - self.t0_h) * 3600.0

    def macro_h(self, t_s: float) -> float:
        return self.t0_h + t_s / 3600.0

    def expected_city_h(self, plan: LegPlan) -> float:
        ts = [self.base_corridor_s.get(c, math.inf) for c in plan.corridors]
        ts = [t for t in ts if math.isfinite(t)]
        return sum(ts) / len(ts) / 3600.0 if ts else 0.0

    # ------------------------------------------------------------------ macro hook: Lane.transit
    def transit(self, shp: Shipment, lane, t_h: float):
        """Generator used by Lane.transit. Returns True if the leg went (partly) through SUMO."""
        plan = self.plans.get(lane.id)
        if plan is None:
            return False
        twin, env = self.twin, self.twin.env
        city_h = self.expected_city_h(plan)
        if plan.kind == "inbound":
            pre, post = max(0.0, t_h - city_h), 0.0
        elif plan.kind == "outbound":
            pre = min(plan.handling_h, t_h)
            post = max(0.0, t_h - pre - city_h)
        else:
            pre, post = min(plan.handling_h, t_h), 0.0
        hub_ll = lambda h: (self.hubs[h]["lat"], self.hubs[h]["lon"])  # noqa: E731
        if plan.kind == "inbound":  # long haul from the origin to the city gateway
            twin.set_status(shp, "transit", lane.id, pre, seg_to=hub_ll(plan.from_hub))
        else:                       # loading at the origin dock: the truck is not moving yet
            twin.set_status(shp, "loading", lane.spec.from_id, pre)
        if pre > 0:
            yield env.timeout(pre)
        dest = plan.to_hubs[0] if len(plan.to_hubs) == 1 else plan.to_hubs[int(twin.rng("city", shp.id).integers(len(plan.to_hubs)))]
        n = max(1, min(self.max_trucks, math.ceil(shp.qty / TRUCK_CAP.get(shp.sku, 1000))))
        depart = self.sumo_t(env.now)
        vids = []
        for i in range(n):
            vid = f"{shp.id}-T{i + 1}"
            ok = self.micro.call("spawn_truck", vid=vid, from_hub=plan.from_hub, to_hub=dest, depart=depart,
                                 meta={"shipment": shp.id, "sku": shp.sku, "lane": lane.id, "kind": shp.kind})
            if ok:
                vids.append(vid)
                self.truck_to_shp[vid] = shp.id
        if not vids:  # hubs not connected (e.g. every route closed): stay macro-only
            self.fallbacks += 1
            twin.log("city_fallback", shipment=shp.id, lane=lane.id, reason="no route")
            yield env.timeout(max(0.0, t_h - pre))
            return True
        shp.trucks = vids
        self.waiting[shp.id] = CityShipment(shp.id, lane.id, set(vids), env.now)
        ev = env.event()
        twin.pending_city[shp.id] = ev
        twin.set_status(shp, "city", lane.id, city_h)
        twin.log("city_enter", shipment=shp.id, lane=lane.id, leg=plan.kind, from_hub=plan.from_hub, to_hub=dest,
                 trucks=vids, sumo_t=round(depart, 1), expected_h=round(city_h, 3))
        timer = env.timeout(max(self.watchdog_h, 4 * city_h))
        res = yield ev | timer
        if ev not in res:  # watchdog: SUMO lost the trucks (gridlock) — continue in macro, keep the trucks
            self.fallbacks += 1
            twin.pending_city.pop(shp.id, None)
            self.waiting.pop(shp.id, None)
            twin.log("city_fallback", shipment=shp.id, lane=lane.id, reason="watchdog")
        shp.trucks = []
        if post > 0:  # outbound: from the exit gateway to the destination
            twin.set_status(shp, "transit", lane.id, post, seg_from=hub_ll(plan.to_hubs[0]))
            yield env.timeout(post)
        return True

    # ------------------------------------------------------------------ sync loop
    def sync_proc(self):
        env = self.twin.env
        while True:
            yield env.timeout(self.sync_s / 3600.0)
            self.sync()

    def sync(self) -> None:
        out = self.micro.step_to(self.sumo_t(self.twin.env.now))
        for frame in out["fcd"]:
            self.fcd_frames += 1
            for row in frame["vehicles"]:
                self.fcd[row["id"]] = {**row, "t": frame["t"]}
            for fn in self.listeners:
                fn("fcd", frame)
        for a in out["arrivals"]:
            self.on_micro_arrival(a)

    def on_micro_arrival(self, a: dict) -> None:
        twin = self.twin
        self.fcd.pop(a["vehicle"], None)
        self.arrivals.append(a)
        for fn in self.listeners:
            fn("arrival", a)
        corridor = (a["from_hub"], a["to_hub"])
        base = self.base_corridor_s.get(corridor)
        if base and math.isfinite(base) and base > 0:
            r = a["travel_s"] / base
            self.obs_ratio[corridor] = r if corridor not in self.obs_ratio else (1 - EWMA) * self.obs_ratio[corridor] + EWMA * r
        sid = self.truck_to_shp.pop(a["vehicle"], None)
        cs = self.waiting.get(sid) if sid else None
        if cs is None:
            return
        cs.trucks.discard(a["vehicle"])
        cs.arrivals.append(a)
        if cs.trucks:
            return
        del self.waiting[sid]
        micro_h = self.macro_h(max(x["t"] for x in cs.arrivals))
        lag_s = (twin.env.now - micro_h) * 3600.0
        self.handshakes.append({"shipment": sid, "lane": cs.lane, "enter_h": cs.enter_h, "micro_arrival_h": micro_h,
                                "macro_receive_h": twin.env.now, "lag_s": lag_s, "trucks": len(cs.arrivals),
                                "drive_s": max(x["travel_s"] for x in cs.arrivals)})
        twin.log("city_arrive", shipment=sid, lane=cs.lane, micro_arrival_h=round(micro_h, 4), lag_s=round(lag_s, 1),
                 drive_min=round(max(x["travel_s"] for x in cs.arrivals) / 60, 1))
        if sid in twin.pending_city:
            twin.apply({"type": "receive", "shipment_id": sid})
        self.update_lane_multipliers(observed=True)

    # ------------------------------------------------------------------ closures -> lane multipliers
    def on_effect(self, e: Effect, started: bool) -> None:
        if not e.sumo_edges:
            return
        if started:
            # SUMO is the source of truth for the city part of coupled lanes: drop the scenario's static
            # slow-down there and let the observed / routed SUMO times set the multiplier instead
            replaced = {l: e.lane_mult.pop(l) for l in list(e.lane_mult) if l in self.plans}
            if replaced:
                self.twin.log("lane_mult_replaced_by_sumo", effect=e.id, static=replaced)
            self.close_road(e.sumo_edges)
        else:
            self.reopen_road(e.sumo_edges)

    def close_road(self, edges: list[str]) -> dict:
        res = self.micro.call("close_road", edges=list(edges))
        self.closed_edges |= set(edges)
        self.twin.log("road_closed", edges=list(edges), rerouted=res["rerouted"])
        self.obs_ratio.clear()
        mults = self.update_lane_multipliers(observed=False)
        return {"rerouted": res["rerouted"], "lane_multipliers": mults}

    def reopen_road(self, edges: list[str]) -> dict:
        res = self.micro.call("reopen_road", edges=list(edges))
        self.closed_edges -= set(edges)
        self.twin.log("road_reopened", edges=list(edges), rerouted=res["rerouted"])
        self.obs_ratio.clear()
        mults = self.update_lane_multipliers(observed=False)
        return {"rerouted": res["rerouted"], "lane_multipliers": mults}

    def corridor_ratios(self, observed: bool) -> dict[tuple[str, str], float]:
        corr = list(self.base_corridor_s)
        now = self.micro.call("corridor_times", pairs=[list(c) for c in corr]) if corr else {}
        ratios = {}
        for c in corr:
            cur, base = now.get(f"{c[0]}|{c[1]}", math.inf), self.base_corridor_s[c]
            r = cur / base if base and math.isfinite(cur) else 20.0
            if observed and c in self.obs_ratio and self.closed_edges:
                r = self.obs_ratio[c]  # trucks' observed times override the router estimate while roads are closed
            ratios[c] = max(r, 1.0) if self.closed_edges else 1.0
        return ratios

    def update_lane_multipliers(self, observed: bool) -> dict[str, float]:
        """Macro lane multiplier = (lane time with the city part scaled by the SUMO corridor ratio) / lane time."""
        ratios = self.corridor_ratios(observed)
        changed = {f"{a}|{b}": round(r, 4) for (a, b), r in ratios.items() if abs(r - 1) > 1e-3}
        if changed or not observed:
            self.twin.log("sumo_corridors", source="observed" if observed else "routed", ratios=changed)
        out = {}
        for lid, plan in self.plans.items():
            lane = self.twin.lanes[lid]
            r = sum(ratios.get(c, 1.0) for c in plan.corridors) / max(len(plan.corridors), 1)
            city_h = self.expected_city_h(plan)
            base_mean = lane.mean_var_h()[0] / lane.live_mult
            m = (base_mean + city_h * (r - 1.0)) / base_mean if base_mean > 0 else 1.0
            if abs(m - lane.live_mult) > 0.002 * lane.live_mult:
                self.twin.apply({"type": "lane_multiplier", "lane": lid, "multiplier": round(m, 4),
                                 "source": "sumo-observed" if observed else "sumo-routed"})
                out[lid] = round(m, 4)
        return out

    # ------------------------------------------------------------------ run / state
    def run_until(self, t_h: float) -> None:
        self.twin.run_until(t_h)

    def snapshot(self) -> dict:
        s = self.twin.snapshot()
        s["city"] = {"sumo_t": round(self.sumo_t(self.twin.env.now), 1), "trucks": list(self.fcd.values()),
                     "waiting_shipments": len(self.waiting), "closed_edges": sorted(self.closed_edges),
                     "handshakes": len(self.handshakes), "fallbacks": self.fallbacks}
        return s
