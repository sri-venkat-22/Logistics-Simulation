"""SimPy macro twin: suppliers -> ports -> DCs -> demand zones (entities in sim/macro/entities.py).

Clock unit: hours from `start`. Processes:
  demand       DemandZone: one order per zone x SKU per day (random hour), or a renewal process (toys)
  fulfilment   nearest DC with enough stock (serving DC first, then backups by distance); otherwise
               allocate what the serving DC has and backorder the rest there (FIFO; or lose it under a
               lost-sales policy)
  dispatch     DC outbound limited by a daily throughput budget (strikes reduce it)
  review       Warehouse policy per SKU: forecast-driven (s,S) by default, static (s,S) or (R,Q)
  production   Supplier lines (Resource); hourly output; ships daily at 18:00 and on completion
  transit      multi-leg shipments; Lane samples lead time x multipliers; Port berths + customs;
               closed nodes hold cargo (anchorage / yard) until reopened
  injector     disruptions (scenario DSL, live events, random supplier failures) as Effects
Instrumentation: an event log (one row per event, the evidence trail behind every KPI), daily series,
stock-out episodes and per-disruption TTS / TTR (Simchi-Levi).
API: run_until(t_h), snapshot(), apply(event), kpis().
Random streams are keyed by name (zone x SKU, lane, supplier, port), so runs with the same seed share
common random numbers across scenarios.
"""
from __future__ import annotations

import itertools
import json
import math
import zlib
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np
import simpy
import simpy.rt

from sim.macro.disruptions import CLOSED_BELOW, Effect, build_effect
from sim.macro.entities import (NODE_HANDLING_H, DemandZone, Lane, Order, Port, Shipment, Supplier, Warehouse)
from sim.macro.kpis import compute_kpis
from sim.macro.network import Network, Path_
from sim.macro.policies import FAMILY_POLICY, DynamicSSPolicy, Policy, default_policy
from sim.paths import HYDERABAD
from sim.scenarios.dsl import Scenario

POLICY = FAMILY_POLICY  # backwards-compatible alias
HANDLING_DAYS = 1.0     # production + loading time the policy plans for
CALIBRATION = HYDERABAD / "calibration.json"
ENGINE_VERSION = "macro-3.0"


def _rng(seed: int, *names: str) -> np.random.Generator:
    return np.random.default_rng([seed, *(zlib.crc32(n.encode()) for n in names)])


def load_calibration(path: Path = CALIBRATION) -> dict[str, dict]:
    """SUMO corridor calibration per macro lane (sim/micro/calibrate.py), or {} if not built."""
    if not path.exists():
        return {}
    return json.loads(path.read_text()).get("lanes", {})


class Twin:
    def __init__(self, net: Network, demand, start: datetime, seed: int = 42,
                 scenarios: list[Scenario] | tuple[Scenario, ...] = (), policy: dict | None = None, *,
                 policies: dict[tuple[str, str], Policy] | None = None, realtime_factor: float | None = None,
                 random_failures: bool = True, calibration: dict | str | None = "auto", log_events: bool = True,
                 storage_days: float = 30.0, initial_state: str = "steady"):
        """policy: per-family overrides of FAMILY_POLICY; policies: explicit Policy per (dc, sku).
        realtime_factor: wall-clock seconds per simulated hour (simpy.rt.RealtimeEnvironment, strict=False).
        calibration: "auto" loads SUMO-calibrated Hyderabad lanes if built, a dict uses it, None disables.
        initial_state: "steady" seeds the pipeline in transit; "full" starts each DC at its order-up-to level."""
        self.net, self.demand, self.start, self.seed = net, demand, start, seed
        self.clock0_h = start.hour + start.minute / 60 + start.second / 3600  # local time of day at t = 0
        self.env = simpy.rt.RealtimeEnvironment(factor=realtime_factor, strict=False) if realtime_factor else simpy.Environment()
        self.realtime_factor = realtime_factor
        self.log_events = log_events
        self.coupler = None  # set by sim.coupling.orchestrator.CoupledTwin
        self.effects: list[Effect] = []
        self.effect_history: list[Effect] = []
        self.events: list[dict] = []
        self.orders: list[Order] = []
        self.shipments: dict[str, Shipment] = {}
        self.delivered_shipments = 0
        self.stockouts: list[dict] = []           # episodes {dc, sku, start_h, end_h}
        self._open_stockout: dict[tuple[str, str], dict] = {}
        self.series: list[dict] = []
        self.listeners: list[Callable[[dict], None]] = []  # called with every event-log row (live streaming)
        self._ids = itertools.count(1)
        self._eff_ids = itertools.count(1)
        self.pending_city: dict[str, simpy.Event] = {}
        self.path_choice: dict[tuple[str, str], int] = {}
        self.custom_paths: dict[tuple[str, str], Path_] = {}   # optimiser routes (set_route), override path_choice
        self.forecaster: Any = None  # an ML demand forecaster (ml.forecast) for the dynamic (s,S) policies

        self.k: defaultdict[str, float] = defaultdict(float)
        self.k_sku: defaultdict[str, defaultdict[str, float]] = defaultdict(lambda: defaultdict(float))
        self.stockout_h: defaultdict[tuple[str, str], float] = defaultdict(float)
        self.stats_since_h = 0.0

        # ------------------------------------------------------------ entities
        self.lanes = {lid: Lane(self, spec) for lid, spec in net.lanes.items()}
        cal = load_calibration() if calibration == "auto" else (calibration or {})
        for lid, c in cal.items():
            if lid in self.lanes:
                self.lanes[lid].calibrate(c)
        self.calibrated_lanes = sorted(l for l in cal if l in self.lanes)
        self.suppliers = {n.id: Supplier(self, n, random_failures) for n in net.of_type("plant", "supplier")}
        self.ports = {n.id: Port(self, n) for n in net.of_type("port")}
        self.warehouses = {n.id: Warehouse(self, n, storage_days) for n in net.of_type("dc")}
        self.pairs = sorted(net.replenishment)  # (dc, sku)
        self._zones_served = defaultdict(list)
        for (z, sku), (dc, _) in net.serving.items():
            self._zones_served[(dc, sku)].append(z)

        for dc, sku in self.pairs:
            pol = (policies or {}).get((dc, sku)) or default_policy(net.skus[sku].family, policy)
            wh = self.warehouses[dc]
            cap = storage_days * max(1.0, self.base_daily(dc, sku)) * 3 if isinstance(pol, DynamicSSPolicy) else math.inf
            if initial_state == "steady" and isinstance(pol, DynamicSSPolicy):
                wh.add_sku(sku, pol, 0.0, cap)
                self.seed_pipeline(dc, sku)
            else:
                wh.add_sku(sku, pol, pol.levels(self, dc, sku, 0.0)[1], cap)
        self.zones = {n.id: DemandZone(self, n, list(net.skus)) for n in net.of_type("zone")}
        self.env.process(self.daily_proc())
        self.env.process(self.monitor_proc())
        for sc in scenarios:
            self.env.process(self.scenario_proc(sc))

    # ================================================================ helpers
    def rng(self, *names: str) -> np.random.Generator:
        return _rng(self.seed, *names)

    def hod(self) -> float:
        """Local hour of day (0-24) at the current simulated time."""
        return (self.env.now + self.clock0_h) % 24

    def now_dt(self) -> datetime:
        return self.start + timedelta(hours=self.env.now)

    def log(self, event: str, /, **kw) -> None:
        if not self.log_events and not self.listeners:
            return
        row = {"t_h": round(self.env.now, 4), "ts": self.now_dt().isoformat(timespec="seconds"), "event": event, **kw}
        if self.log_events:
            self.events.append(row)
        for fn in self.listeners:
            fn(row)

    def base_daily(self, dc: str, sku: str) -> float:
        return sum(self.demand.base_mean(z, sku) for z in self.zones_served(dc, sku))

    def zones_served(self, dc: str, sku: str) -> list[str]:
        return self._zones_served[(dc, sku)]

    def path(self, dc: str, sku: str):
        custom = self.custom_paths.get((dc, sku))
        if custom is not None:
            return custom
        opts = self.net.replenishment[(dc, sku)]
        return opts[min(self.path_choice.get((dc, sku), 0), len(opts) - 1)]

    def sku_sources(self, sku: str) -> set[str]:
        """Suppliers / plants that make a SKU (the sources of any of its sourcing paths)."""
        return {p.source for (_, s), paths in self.net.replenishment.items() if s == sku for p in paths}

    def lane_chain(self, lanes: list[str], start: str | None = None, end: str | None = None) -> list[str]:
        """Validate a contiguous lane sequence and return its node sequence."""
        if not lanes:
            raise ValueError("empty route")
        nodes = []
        for i, lid in enumerate(lanes):
            if lid not in self.net.lanes:
                raise ValueError(f"unknown lane {lid!r}")
            ln = self.net.lanes[lid]
            if i == 0:
                nodes.append(ln.from_id)
            elif ln.from_id != nodes[-1]:
                raise ValueError(f"lane {lid} does not continue from {nodes[-1]}")
            nodes.append(ln.to_id)
        if start is not None and nodes[0] != start:
            raise ValueError(f"route starts at {nodes[0]}, expected {start}")
        if end is not None and nodes[-1] != end:
            raise ValueError(f"route ends at {nodes[-1]}, expected {end}")
        if any(self.net.nodes[n].type == "zone" for n in nodes[:-1]) or len(set(nodes)) != len(nodes):
            raise ValueError("route passes through a demand zone or revisits a node")
        return nodes

    def _transfer(self, shp: Shipment):
        yield from self.move(shp)
        yield from self.warehouses[shp.dest].receive(shp.sku, shp.qty)
        self.log("receipt", dc=shp.dest, sku=shp.sku, qty=round(shp.qty), shipment=shp.id, transfer=True,
                 lead_h=round(self.env.now - shp.created_h, 2))

    # ================================================================ effects
    def _f(self, table: str, key) -> float:
        v = 1.0
        for e in self.effects:
            v *= getattr(e, table).get(key, 1.0)
        return v

    def node_factor(self, n: str) -> float:
        return self._f("node_factor", n)

    def production_factor(self, n: str) -> float:
        return self._f("production_factor", n)

    def is_open(self, n: str) -> bool:
        return self.node_factor(n) >= CLOSED_BELOW

    def node_status(self, n: str) -> str:
        f = min(self.node_factor(n), self.production_factor(n), self._f("dispatch_factor", n))
        return "closed" if f < CLOSED_BELOW else "degraded" if f < 1 - 1e-9 else "up"

    def lane_extra_h(self, lane: str, t: float) -> float:
        """Extra transit time from lane slow-downs, counting only the overlap with each disruption."""
        extra = 0.0
        for e in self.effects:
            m = e.lane_mult.get(lane)
            if m and m > 1:
                overlap = max(0.0, min(self.env.now + t, e.end_h) - self.env.now)
                extra += (m - 1.0) * overlap
        return extra

    def demand_mult(self, zone: str, sku: str) -> float:
        return (self._f("demand_mult", (zone, sku)) * self._f("demand_mult", (zone, "*"))
                * self._f("demand_mult", ("*", sku)) * self._f("demand_mult", ("*", "*")))

    def add_effect(self, e: Effect, duration_h: float | None) -> Effect:
        e.id = e.id or f"E{next(self._eff_ids)}"
        e.start_h = self.env.now
        e.end_h = self.env.now + duration_h if duration_h is not None else math.inf
        self.effects.append(e)
        self.effect_history.append(e)
        self.log("disruption_start", id=e.id, kind=e.kind, type=e.type, target=e.target, label=e.label,
                 nodes=sorted(e.nodes()), lanes=sorted(e.lane_mult), end_h=None if math.isinf(e.end_h) else round(e.end_h, 3))
        if self.coupler is not None:
            self.coupler.on_effect(e, started=True)
        if duration_h is not None:
            self.env.process(self._expire(e))
        return e

    def _expire(self, e: Effect):
        yield self.env.timeout(max(0.0, e.end_h - self.env.now))
        self.end_effect(e)

    def end_effect(self, e: Effect) -> None:
        if e not in self.effects:
            return
        self.effects.remove(e)
        e.end_h = self.env.now
        self.log("disruption_end", id=e.id, kind=e.kind, type=e.type, target=e.target)
        if self.coupler is not None:
            self.coupler.on_effect(e, started=False)
        for dc, sku in self.pairs:  # reopened DCs serve waiting orders
            self.warehouses[dc].serve_backlog(sku)

    def scenario_proc(self, sc: Scenario, relative_to_now: bool = False):
        sc.validate_against({n.id: n.type for n in self.net.nodes.values()}, self.net.lanes)
        offset = sc.start_offset_h(self.start)
        if relative_to_now and (sc.start == "now" or sc.start.startswith("+")):
            offset += self.env.now
        yield self.env.timeout(max(0.0, offset - self.env.now))
        self.add_effect(build_effect(sc, self.net), sc.duration_h)

    def start_random_failure(self, node: str, duration_h: float) -> None:
        e = Effect(label=f"random outage {node}", kind="random_failure", type="supplier_failure", target=node,
                   production_factor={node: 0.0})
        self.add_effect(e, duration_h)

    def dependents(self, e: Effect) -> set[tuple[str, str]]:
        """DC x SKU pairs whose stock depends on what the effect disrupts."""
        nodes, lanes = e.nodes(), set(e.lane_mult)
        zones = {z for z, _ in e.demand_mult}
        out = set()
        for dc, sku in self.pairs:
            p = self.path(dc, sku)
            if dc in nodes or nodes & set(p.nodes) or lanes & set(p.lanes):
                out.add((dc, sku))
            elif zones and ("*" in zones or zones & set(self.zones_served(dc, sku))):
                skus = {s for z, s in e.demand_mult}
                if "*" in skus or sku in skus:
                    out.add((dc, sku))
        return out

    # ================================================================ demand -> fulfilment
    def promise_h(self, lane: str) -> float:
        return 24.0 + self.net.lanes[lane].lt_p90_h

    def new_order(self, zone: str, sku: str, qty: int) -> Order | None:
        if qty <= 0:
            return None
        options = self.net.serving_options.get((zone, sku)) or [self.net.serving[(zone, sku)]]
        dc, lane = next(((d, l) for d, l in options if self.warehouses[d].can_serve(sku, qty)), options[0])
        o = Order(len(self.orders), zone, sku, dc, lane, qty, self.env.now, self.env.now + self.promise_h(options[0][1]))
        self.orders.append(o)
        self.k["units_demanded"] += qty
        self.k_sku[sku]["units_demanded"] += qty
        self.fulfil(o, fallback=(dc, lane) != options[0])
        return o

    def fulfil(self, o: Order, fallback: bool = False) -> None:
        wh = self.warehouses[o.dc]
        alloc = int(wh.take(o.sku, o.qty))
        o.immediate = alloc
        self.k["units_filled_immediately"] += alloc
        self.k_sku[o.sku]["units_filled_immediately"] += alloc
        short = o.qty - alloc
        self.log("order", order=o.id, zone=o.zone, sku=o.sku, dc=o.dc, qty=o.qty, filled=alloc,
                 **({"fallback_dc": True} if fallback else {}))
        if alloc:
            self.ship_to_zone(o, alloc, o.dc, o.lane)
        if short > 0:
            penalty = short * self.net.skus[o.sku].stockout_penalty
            self.k["penalty_cost"] += penalty
            if wh.policy[o.sku].lost_sales:
                o.lost = short
                self.k["units_lost"] += short
                self.k_sku[o.sku]["units_lost"] += short
                self.log("lost_sale", order=o.id, zone=o.zone, sku=o.sku, dc=o.dc, qty=short, penalty=penalty)
            else:
                wh.backorder(o, short)
                self.k["units_backordered"] += short
                self.k_sku[o.sku]["units_backordered"] += short
                self.log("backorder", order=o.id, zone=o.zone, sku=o.sku, dc=o.dc, qty=short, penalty=penalty)

    def ship_to_zone(self, o: Order, qty: float, dc: str, lane: str) -> None:
        self.env.process(self._deliver(o, qty, dc, lane))

    def _deliver(self, o: Order, qty: float, dc: str, lane: str):
        wh = self.warehouses[dc]
        left = qty
        while left > 0:  # DC outbound throughput budget
            while not self.is_open(dc):
                yield self.env.timeout(1)
            take = min(left, wh.dispatch_budget)
            if take <= 0:
                yield self.env.timeout(24 - self.hod())
                continue
            wh.dispatch_budget -= take
            left -= take
            shp = self.new_shipment("delivery", o.sku, take, (dc, o.zone), (lane,), order_id=o.id)
            yield from self.move(shp)
            o.delivered += int(take)
            if o.delivered >= o.qty - o.lost and o.done_h is None:
                o.done_h = self.env.now
                self.log("order_complete", order=o.id, zone=o.zone, sku=o.sku, on_time=o.done_h <= o.promised_h,
                         lead_h=round(o.done_h - o.created_h, 2))

    # ================================================================ replenishment -> shipments
    def lead_stats_days(self, dc: str, sku: str) -> tuple[float, float]:
        """Mean and variance (days, days^2) of the replenishment lead time on the current path."""
        p = self.path(dc, sku)
        mean = var = 0.0
        for i, lid in enumerate(p.lanes):
            m, v = self.lanes[lid].mean_var_h()
            mean, var = mean + m, var + v
            nxt = p.nodes[i + 1]
            if nxt in self.ports:
                pm, pv = Port.expected_dwell(self.net.lanes[lid].mode == "sea")
                mean, var = mean + pm, var + pv
        handling = 0.0 if self.suppliers[p.source].infinite else HANDLING_DAYS
        return mean / 24.0 + handling, var / 576.0

    def place_replenishment(self, dc: str, sku: str, qty: float, ip: float) -> None:
        p = self.path(dc, sku)
        s, S = self.warehouses[dc].policy[sku].levels(self, dc, sku, self.env.now)
        cost = self.net.skus[sku].order_cost
        self.k["ordering_cost"] += cost
        self.k["replenishment_orders"] += 1
        self.log("replenish", dc=dc, sku=sku, qty=round(qty), source=p.source, s=round(s), S=round(S), ip=round(ip),
                 order_cost=cost)
        self.suppliers[p.source].order(dc, sku, qty)

    def dispatch_replenishment(self, source: str, dc: str, sku: str, qty: float) -> None:
        p = self.path(dc, sku)
        shp = self.new_shipment("replenishment", sku, qty, p.nodes, p.lanes)
        self.env.process(self._replenish(shp))

    def new_shipment(self, kind: str, sku: str, qty: float, nodes, lanes, order_id: int | None = None) -> Shipment:
        shp = Shipment(f"SH{next(self._ids):06d}", kind, sku, qty, tuple(nodes), tuple(lanes), self.env.now, order_id)
        self.shipments[shp.id] = shp
        return shp

    def _replenish(self, shp: Shipment, start_leg: int = 0, remaining_h: float | None = None, in_port: bool = False):
        if start_leg == 0 and remaining_h is None:
            self.log("asn", shipment=shp.id, source=shp.nodes[0], dc=shp.dest, sku=shp.sku, qty=round(shp.qty),
                     lanes=list(shp.lanes))
        yield from self.move(shp, start_leg, remaining_h, in_port)
        wh = self.warehouses[shp.dest]
        yield from wh.receive(shp.sku, shp.qty)
        self.log("receipt", dc=shp.dest, sku=shp.sku, qty=round(shp.qty), shipment=shp.id,
                 lead_h=round(self.env.now - shp.created_h, 2))

    def set_status(self, shp: Shipment, status: str, where: str, dur_h: float,
                   seg_from: tuple[float, float] | None = None, seg_to: tuple[float, float] | None = None) -> None:
        shp.status, shp.where = status, where
        shp.seg_start_h, shp.seg_end_h = self.env.now, self.env.now + dur_h
        shp.seg_from, shp.seg_to = seg_from, seg_to

    def move(self, shp: Shipment, start_leg: int = 0, remaining_h: float | None = None, in_port: bool = False):
        """Carry a shipment along its lanes. Seeded pipeline shipments start mid-leg (remaining_h) or
        mid-dwell in a port (in_port)."""
        shp.depart_h = shp.depart_h if shp.depart_h is not None else self.env.now
        for i in range(start_leg, len(shp.lanes)):
            lane_id = shp.lanes[i]
            lane = self.net.lanes[lane_id]
            shp.leg = i
            rem = None
            if i == start_leg and in_port:  # finish the port dwell, then depart normally
                yield from self.ports[lane.from_id].handle(shp, True, remaining_h)
            elif i == start_leg:
                rem = remaining_h
            if rem is None:  # legs dispatched before the window started are not booked in it
                yield from self.depart(shp, lane.from_id)
                self.book_transport(shp, lane_id)
            yield from self.lanes[lane_id].transit(shp, rem)
            if lane.to_id in self.ports and i < len(shp.lanes) - 1:
                yield from self.ports[lane.to_id].handle(shp, lane.mode == "sea")
            else:
                yield from self.arrive(shp, lane.to_id)
        shp.status, shp.where, shp.arrive_h = "delivered", shp.dest, self.env.now
        self.shipments.pop(shp.id, None)
        self.delivered_shipments += 1

    def depart(self, shp: Shipment, node: str):
        if not self.is_open(node):
            self.set_status(shp, "held", node, 0)
            while not self.is_open(node):
                yield self.env.timeout(1)
        f = self.node_factor(node)
        if f < 1:
            dwell = NODE_HANDLING_H[self.net.nodes[node].type] * (1 / f - 1)
            self.set_status(shp, "dwell", node, dwell)
            yield self.env.timeout(dwell)

    def arrive(self, shp: Shipment, node: str):
        if self.net.nodes[node].type == "zone":
            return
        yield from self.depart(shp, node)  # same rule inbound: wait while closed, extra dwell while degraded

    def book_transport(self, shp: Shipment, lane_id: str) -> None:
        """Transport cost and CO2 are booked at dispatch of each leg."""
        lane = self.net.lanes[lane_id]
        w_t = self.net.skus[shp.sku].unit_weight_kg / 1000.0
        cost = shp.qty * lane.cost_per_unit
        co2 = shp.qty * w_t * lane.distance_km * lane.co2_per_tkm
        self.k["transport_cost"] += cost
        self.k["co2_kg"] += co2
        self.k["unit_km"] += shp.qty * lane.distance_km
        self.log("depart", shipment=shp.id, kind=shp.kind, sku=shp.sku, qty=round(shp.qty), lane=lane_id,
                 mode=lane.mode, cost=round(cost, 2), co2_kg=round(co2, 3))

    def seed_pipeline(self, dc: str, sku: str) -> None:
        """Start in steady state: one shipment per past day already moving along the primary path, and
        on-hand stock equal to the order-up-to level minus what is in transit."""
        wh = self.warehouses[dc]
        s, S = wh.policy[sku].levels(self, dc, sku, 0.0)
        p = self.path(dc, sku)
        segs: list[tuple[str, int, float]] = []  # ("lane"|"port", leg index, expected hours)
        for i, lid in enumerate(p.lanes):
            segs.append(("lane", i, self.lanes[lid].mean_var_h()[0]))
            if p.nodes[i + 1] in self.ports and i < len(p.lanes) - 1:
                segs.append(("port", i + 1, Port.expected_dwell(self.net.lanes[lid].mode == "sea")[0]))
        total = sum(x[2] for x in segs)
        daily = self.base_daily(dc, sku)
        in_transit = 0.0
        for age_days in range(int(total // 24) + 1 if daily > 0 else 0):
            elapsed = age_days * 24.0 + 12.0  # dispatched at 18:00 on a past day
            if elapsed >= total:
                break
            j, before = 0, 0.0
            while before + segs[j][2] <= elapsed:
                before += segs[j][2]
                j += 1
            kind, leg, dur = segs[j]
            shp = self.new_shipment("replenishment", sku, daily, p.nodes, p.lanes)
            shp.created_h = shp.depart_h = -elapsed
            self.env.process(self._replenish(shp, start_leg=leg, remaining_h=before + dur - elapsed, in_port=kind == "port"))
            in_transit += daily
        wh.on_order[sku] = in_transit
        level = max(S - in_transit, s - in_transit + daily * 2, daily * 2)
        c = wh.stock[sku]
        if level > c.capacity:
            wh.stock[sku] = c = simpy.Container(self.env, capacity=level * 1.5, init=0)
        if level > 0:
            c.put(level)

    # ================================================================ stock-out episodes
    def stockout_started(self, dc: str, sku: str) -> None:
        ep = {"dc": dc, "sku": sku, "start_h": self.env.now, "end_h": None}
        self.stockouts.append(ep)
        self._open_stockout[(dc, sku)] = ep
        self.log("stockout_start", dc=dc, sku=sku)

    def stockout_ended(self, dc: str, sku: str, since: float) -> None:
        ep = self._open_stockout.pop((dc, sku), None)
        if ep is not None:
            ep["end_h"] = self.env.now
        self.log("stockout_end", dc=dc, sku=sku, duration_h=round(self.env.now - since, 2))

    # ================================================================ periodic processes
    def daily_proc(self):
        while True:
            for wh in self.warehouses.values():
                wh.dispatch_budget = wh.node.capacity * self._f("dispatch_factor", wh.id)
            for dc, sku in self.pairs:
                self.warehouses[dc].serve_backlog(sku)
            self.record_series()
            yield self.env.timeout(24 - self.hod())

    def monitor_proc(self):
        while True:
            for dc, sku in self.pairs:
                wh = self.warehouses[dc]
                oh = wh.level(sku)
                sk = self.net.skus[sku]
                self.k["holding_cost"] += oh * sk.unit_value * sk.holding_rate_yr / 8760.0
                self.k_sku[sku]["on_hand_h"] += oh
                if wh.backlog[sku]:
                    self.stockout_h[(dc, sku)] += 1
            self.k["hours"] += 1
            yield self.env.timeout(1)

    def record_series(self) -> None:
        dem = self.k["units_demanded"] or 1
        if self.log_events:
            for dc, sku in self.pairs:
                wh = self.warehouses[dc]
                self.log("inventory", dc=dc, sku=sku, on_hand=round(wh.level(sku), 1), on_order=round(wh.on_order[sku], 1),
                         backlog=round(wh.backlog_units(sku), 1))
        self.series.append({
            "t_h": round(self.env.now, 3),
            "fill_rate": self.k["units_filled_immediately"] / dem,
            "on_hand": {f"{dc}/{sku}": round(self.warehouses[dc].level(sku), 1) for dc, sku in self.pairs},
            "backlog": {f"{dc}/{sku}": round(self.warehouses[dc].backlog_units(sku), 1) for dc, sku in self.pairs},
        })

    def reset_stats(self) -> None:
        """Forget KPI accumulators (warm-up for steady-state verification runs)."""
        self.k.clear()
        self.k_sku.clear()
        self.stockout_h.clear()
        self.orders = [o for o in self.orders if o.done_h is None]
        for o in self.orders:
            o.immediate = o.qty  # carried-over orders don't count against service after warm-up
        self.series.clear()
        self.stats_since_h = self.env.now

    # ================================================================ public API
    def fast_forward(self, t_h: float) -> None:
        """Real-time mode: run to t_h as fast as possible (warm-up), then re-align the wall clock."""
        if not self.realtime_factor:
            self.run_until(t_h)
            return
        self.env._factor = 1e-9  # simpy exposes factor read-only
        try:
            if t_h > self.env.now:
                self.env.run(until=t_h)
        finally:
            self.env._factor = self.realtime_factor
            self.sync_clock()

    def sync_clock(self) -> None:
        """Real-time mode: align the wall clock with the current simulated time (call before running)."""
        if self.realtime_factor:
            self.env.env_start = self.env.now  # simpy's sync() re-anchors only the wall clock; re-anchor sim time too
            self.env.sync()
            self._clock_synced = True

    def run_until(self, t_h: float) -> None:
        """Advance the simulation to absolute time t_h (hours since start); wall-clock paced when realtime."""
        if self.realtime_factor and not getattr(self, "_clock_synced", False):
            self.sync_clock()
        if t_h > self.env.now:
            self.env.run(until=t_h)

    def run(self, days: float) -> dict:
        self.run_until(days * 24)
        return self.kpis(days)

    def kpis(self, days: float | None = None) -> dict:
        return compute_kpis(self, days if days is not None else (self.env.now - self.stats_since_h) / 24.0)

    def evidence(self, kpi: str, **match) -> list[dict]:
        """Event-log rows behind a KPI (optionally filtered, e.g. dc="DC_BLR")."""
        from sim.macro.kpis import EVIDENCE
        kinds = EVIDENCE[kpi]
        return [e for e in self.events if e["event"] in kinds and all(e.get(k) == v for k, v in match.items())]

    def shipment_position(self, shp: Shipment) -> tuple[float, float, float]:
        """(lat, lon, progress 0..1 along the current leg) by linear interpolation between node coordinates."""
        lane = self.net.lanes[shp.lanes[min(shp.leg, len(shp.lanes) - 1)]]
        a, b = self.net.nodes[lane.from_id], self.net.nodes[lane.to_id]
        if shp.status == "loading":
            return a.lat, a.lon, 0.0
        if shp.status != "transit" and shp.status != "city":
            n = self.net.nodes.get(shp.where) or (b if shp.status in ("anchorage", "berth", "customs") else a)
            return n.lat, n.lon, 0.0 if n is a else 1.0
        (la0, lo0) = shp.seg_from or (a.lat, a.lon)
        (la1, lo1) = shp.seg_to or (b.lat, b.lon)
        span = max(shp.seg_end_h - shp.seg_start_h, 1e-9)
        f = min(1.0, max(0.0, (self.env.now - shp.seg_start_h) / span))
        return la0 + (la1 - la0) * f, lo0 + (lo1 - lo0) * f, f

    def snapshot(self) -> dict:
        """JSON-serialisable state of the whole network at the current simulated time."""
        nodes = {}
        for nid, n in self.net.nodes.items():
            d = {"type": n.type, "status": self.node_status(nid), "lat": n.lat, "lon": n.lon}
            if nid in self.suppliers:
                d.update(self.suppliers[nid].snapshot())
            elif nid in self.ports:
                d.update(self.ports[nid].snapshot())
            elif nid in self.warehouses:
                d["dispatch_budget"] = round(self.warehouses[nid].dispatch_budget)
            nodes[nid] = d
        inventory = {f"{dc}/{sku}": v for dc, wh in self.warehouses.items() for sku, v in wh.snapshot().items()}
        ships = []
        for shp in self.shipments.values():
            lat, lon, prog = self.shipment_position(shp)
            lane = self.net.lanes[shp.lanes[min(shp.leg, len(shp.lanes) - 1)]]
            ships.append({"id": shp.id, "kind": shp.kind, "sku": shp.sku, "qty": round(shp.qty, 1), "status": shp.status,
                          "lane": lane.id, "mode": lane.mode, "from": lane.from_id, "to": lane.to_id, "dest": shp.dest,
                          "leg": shp.leg, "legs": len(shp.lanes), "progress": round(prog, 3), "lat": round(lat, 5),
                          "lon": round(lon, 5), "eta_leg_h": round(shp.seg_end_h, 3), "trucks": list(shp.trucks)})
        dem = self.k["units_demanded"] or 1
        return {
            "t_h": round(self.env.now, 4), "ts": self.now_dt().isoformat(timespec="seconds"), "seed": self.seed,
            "nodes": nodes, "inventory": inventory, "shipments": ships,
            "lanes": {lid: {"live_mult": round(l.live_mult, 3), "extra_mult": round(1 + self.lane_extra_h(lid, 1.0), 3),
                            "calibrated": l.calibration is not None}
                      for lid, l in self.lanes.items() if l.live_mult != 1 or l.calibration or self.lane_extra_h(lid, 1.0)},
            "effects": [e.summary() for e in self.effects],
            "kpis_to_date": {"fill_rate": self.k["units_filled_immediately"] / dem, "units_demanded": int(self.k["units_demanded"]),
                             "backorder_units": int(sum(wh.backlog_units(s) for wh in self.warehouses.values() for s in wh.stock)),
                             "shipments_active": len(self.shipments), "shipments_delivered": self.delivered_shipments},
        }

    def apply(self, event: dict[str, Any]) -> dict:
        """Apply a live event to the running twin. Returns an acknowledgement (with an effect id if one was created).

        {"type": "scenario", "scenario": {...DSL...}}           relative starts ("+6h", "now") count from now
        {"type": "disruption_end", "id": "E3"}
        {"type": "lane_multiplier", "lane": "L015", "multiplier": 1.4, "source": "sumo"}
        {"type": "node_status", "node": "PORT_CHENNAI", "factor": 0.0, "duration_h": 12}
        {"type": "demand_multiplier", "zone": "Z_HYD", "sku": "*", "multiplier": 1.3, "duration_h": 48}
        {"type": "set_path", "dc": "DC_BLR", "sku": "SKU_ELEC", "path": 1}
        {"type": "order", "zone": "Z_HYD", "sku": "SKU_VAX", "qty": 50}
        {"type": "policy_buffer", "family": "vaccine", "dz": 0.8}  raise the safety factor z of a family
        {"type": "receive", "shipment_id": "SH000123"}          micro arrival (coupling)
        """
        kind = event.get("type")
        now = self.env.now
        if kind == "scenario":
            sc = event["scenario"] if isinstance(event["scenario"], Scenario) else Scenario.model_validate(event["scenario"])
            sc.validate_against({n.id: n.type for n in self.net.nodes.values()}, self.net.lanes)
            self.env.process(self.scenario_proc(sc, relative_to_now=True))
            self.log("apply", type=kind, scenario=sc.type.value, target=sc.target)
            return {"ok": True, "type": kind, "t_h": now}
        if kind == "disruption_end":
            for e in list(self.effects):
                if e.id == event["id"]:
                    self.end_effect(e)
                    return {"ok": True, "type": kind, "id": e.id}
            raise ValueError(f"no active effect {event['id']!r}")
        if kind == "lane_multiplier":
            lane = self.lanes[event["lane"]]
            m = float(event["multiplier"])
            if not (0 < m <= 100):
                raise ValueError("multiplier must be in (0, 100]")
            lane.live_mult = m
            self.log("lane_multiplier", lane=lane.id, multiplier=round(m, 3), source=event.get("source", "live"))
            return {"ok": True, "type": kind, "lane": lane.id}
        if kind == "node_status":
            node = event["node"]
            if node not in self.net.nodes:
                raise ValueError(f"unknown node {node!r}")
            f = float(event["factor"])
            e = Effect(label=event.get("label", f"{node} status {f:g}"), kind="live", type="node_status", target=node)
            ntype = self.net.nodes[node].type
            if ntype in ("plant", "supplier"):
                e.production_factor[node] = f
            else:
                e.node_factor[node] = f
                if ntype == "dc":
                    e.dispatch_factor[node] = f
            self.add_effect(e, event.get("duration_h"))
            return {"ok": True, "type": kind, "id": e.id}
        if kind == "demand_multiplier":
            e = Effect(label=event.get("label", "demand change"), kind=event.get("kind", "live"), type="demand_multiplier",
                       target=event["zone"], demand_mult={(event["zone"], event.get("sku", "*")): float(event["multiplier"])})
            self.add_effect(e, event.get("duration_h"))
            return {"ok": True, "type": kind, "id": e.id}
        if kind == "set_path":
            key = (event["dc"], event["sku"])
            if key not in self.net.replenishment or not 0 <= int(event["path"]) < len(self.net.replenishment[key]):
                raise ValueError(f"no path {event.get('path')} for {key}")
            self.path_choice[key] = int(event["path"])
            self.custom_paths.pop(key, None)
            self.log("set_path", dc=key[0], sku=key[1], path=int(event["path"]))
            return {"ok": True, "type": kind}
        if kind == "set_route":  # optimiser reroute: any contiguous lane sequence from a source of the SKU to the DC
            key = (event["dc"], event["sku"])
            if key not in self.net.replenishment:
                raise ValueError(f"{key} is not a stocked DC x SKU")
            nodes = self.lane_chain(list(event["lanes"]), end=key[0])
            if nodes[0] not in self.sku_sources(key[1]):
                raise ValueError(f"{nodes[0]} does not make {key[1]}")
            if self.net.skus[key[1]].cold_chain and any(self.net.nodes[n].type == "dc" and not self.net.nodes[n].cold_chain
                                                        for n in nodes):
                raise ValueError("cold-chain SKU routed through a DC without cold storage")
            lead = sum(self.net.lanes[l].lt_mean_h for l in event["lanes"])
            self.custom_paths[key] = Path_(nodes[0], tuple(nodes), tuple(event["lanes"]), lead)
            self.log("set_route", dc=key[0], sku=key[1], lanes=list(event["lanes"]), source=nodes[0])
            return {"ok": True, "type": kind, "nodes": nodes}
        if kind == "transfer":  # lateral transshipment: move stock now from one DC to another along lanes
            src, dst, sku, qty = event["from"], event["to"], event["sku"], float(event["qty"])
            if src not in self.warehouses or dst not in self.warehouses or sku not in self.warehouses[src].stock \
                    or sku not in self.warehouses[dst].stock:
                raise ValueError(f"both {src} and {dst} must stock {sku}")
            nodes = self.lane_chain(list(event["lanes"]), start=src, end=dst)
            if not self.is_open(src):
                raise ValueError(f"{src} is closed")
            wh = self.warehouses[src]
            amt = float(min(qty, math.floor(wh.level(sku)))) if not wh.backlog[sku] else 0.0
            if amt <= 0:
                return {"ok": True, "type": kind, "qty": 0.0}
            wh.stock[sku].get(amt)
            wh.after_withdrawal(sku)
            self.warehouses[dst].on_order[sku] += amt
            shp = self.new_shipment("transfer", sku, amt, nodes, event["lanes"])
            self.log("transfer", shipment=shp.id, src=src, dc=dst, sku=sku, qty=round(amt), lanes=list(event["lanes"]))
            self.env.process(self._transfer(shp))
            return {"ok": True, "type": kind, "qty": amt, "shipment": shp.id}
        if kind == "policy_buffer":  # plan action: raise the safety factor of a SKU family's forecast policies
            fam, dz = event["family"], float(event["dz"])
            if not -2 <= dz <= 3:
                raise ValueError("dz must be in [-2, 3]")
            changed = 0
            for dc, sku in self.pairs:
                pol = self.warehouses[dc].policy[sku]
                if self.net.skus[sku].family == fam and isinstance(pol, DynamicSSPolicy):
                    pol.z += dz
                    changed += 1
            if not changed:
                raise ValueError(f"no forecast-driven policy for family {fam!r}")
            self.log("policy_buffer", family=fam, dz=dz, policies=changed)
            return {"ok": True, "type": kind, "policies": changed}
        if kind == "order":
            o = self.new_order(event["zone"], event["sku"], int(event["qty"]))
            return {"ok": True, "type": kind, "order": o.id if o else None}
        if kind == "receive":
            ev = self.pending_city.pop(event["shipment_id"], None)
            if ev is None:
                raise ValueError(f"shipment {event['shipment_id']!r} is not waiting in the city")
            if not ev.triggered:
                ev.succeed(event)
            return {"ok": True, "type": kind, "shipment_id": event["shipment_id"]}
        raise ValueError(f"unknown event type {kind!r}")
