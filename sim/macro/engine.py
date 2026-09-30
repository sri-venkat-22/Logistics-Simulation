"""SimPy macro twin (v0, Phase 2): suppliers -> ports -> DCs -> demand zones.

Clock unit: hours. Processes:
  demand      one order per zone x SKU per day (random hour), sent to the serving DC
  fulfilment  allocate on-hand stock; the shortfall is backordered (FIFO) and filled on receipt
  dispatch    DC outbound limited by a daily throughput budget (strikes reduce it)
  review      every 24 h at 06:00: dynamic (s, S) per DC x SKU from the forward-looking forecast
              (so stock is pre-built for known festivals, not for unannounced shocks)
  production  plants/suppliers produce hourly at capacity x factor, shared across open orders;
              whatever is produced ships daily at 18:00 (partial shipments)
  transit     multi-leg shipments along the sourcing path; each lane samples a lognormal lead
              time x disruption multiplier; closed nodes hold cargo (anchorage / yard) until reopened
  injector    applies each Scenario's effects between its start and end
Random streams are keyed by name (zone x SKU, lane, supplier), so a baseline and a disrupted run
with the same seed share common random numbers.
"""
from __future__ import annotations

import math
import zlib
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
import simpy

from sim.macro.demand import DemandModel
from sim.macro.network import Network
from sim.scenarios.dsl import Scenario, ScenarioType, haversine_km, point_in_polygon

REVIEW_HOUR = 6.0
DISPATCH_HOUR = 18.0
HANDLING_DAYS = 1.0  # production + loading time the policy plans for
CLOSED_BELOW = 0.25  # a node running below 25 % of capacity is effectively closed: cargo waits
NODE_HANDLING_H = {"port": 24.0, "dc": 12.0, "plant": 6.0, "supplier": 6.0, "zone": 0.0}
# Inventory policy per family: z = safety factor, cover_days = forecast days added to the order-up-to level.
# Cold-chain vaccine storage is scarce and expensive (lean); sea-imported electronics are ordered in batches.
POLICY = {"vaccine": {"z": 2.05, "cover_days": 2}, "fmcg": {"z": 1.65, "cover_days": 5},
          "electronics": {"z": 1.65, "cover_days": 10}}


@dataclass
class Order:
    id: int
    zone: str
    sku: str
    dc: str
    lane: str
    qty: int
    created_h: float
    promised_h: float
    immediate: int = 0
    delivered: int = 0
    done_h: float | None = None


@dataclass
class Effect:
    label: str
    end_h: float = float("inf")
    node_factor: dict[str, float] = field(default_factory=dict)       # port/DC/plant handling capacity (0 = closed)
    production_factor: dict[str, float] = field(default_factory=dict)  # plant/supplier output
    dispatch_factor: dict[str, float] = field(default_factory=dict)    # DC outbound throughput
    lane_mult: dict[str, float] = field(default_factory=dict)          # transit-time multiplier
    demand_mult: dict[tuple[str, str], float] = field(default_factory=dict)  # (zone|*, sku|*) -> multiplier


def _rng(seed: int, *names: str) -> np.random.Generator:
    return np.random.default_rng([seed, *(zlib.crc32(n.encode()) for n in names)])


class Twin:
    def __init__(self, net: Network, demand: DemandModel, start: datetime, seed: int = 42,
                 scenarios: list[Scenario] | tuple[Scenario, ...] = (), policy: dict | None = None):
        self.net, self.demand, self.start, self.seed = net, demand, start, seed
        self.policy_cfg = policy or POLICY
        self.env = simpy.Environment()
        self.effects: list[Effect] = []
        self.events: list[dict] = []
        self.orders: list[Order] = []

        self.pairs = sorted(net.replenishment)  # (dc, sku)
        self.on_hand: dict[tuple[str, str], float] = {}
        self.on_order: dict[tuple[str, str], float] = defaultdict(float)
        self.backlog: dict[tuple[str, str], deque[list]] = defaultdict(deque)  # [order, remaining]
        self.dispatch_budget: dict[str, float] = {}
        self.prod_queue: dict[str, list[list]] = defaultdict(list)  # supplier -> [dc, sku, qty, produced, shipped]
        self.lane_rng = {l: _rng(seed, "lane", l) for l in net.lanes}

        # KPI accumulators
        self.k = defaultdict(float)
        self.k_sku = defaultdict(lambda: defaultdict(float))
        self.stockout_h = defaultdict(float)

        for dc, sku in self.pairs:
            self.seed_pipeline(dc, sku)
        for dc in net.of_type("dc"):
            self.dispatch_budget[dc.id] = dc.capacity

        for zone in net.of_type("zone"):
            for sku in net.skus:
                self.env.process(self.demand_proc(zone.id, sku))
        for dc, sku in self.pairs:
            self.env.process(self.review_proc(dc, sku))
        for sup in net.of_type("plant", "supplier"):
            self.env.process(self.production_proc(sup.id))
        self.env.process(self.daily_proc())
        self.env.process(self.monitor_proc())
        for sc in scenarios:
            self.env.process(self.scenario_proc(sc))

    # ------------------------------------------------------------ effects
    def _f(self, table: str, key) -> float:
        v = 1.0
        for e in self.effects:
            v *= getattr(e, table).get(key, 1.0)
        return v

    def node_factor(self, n: str) -> float:
        return self._f("node_factor", n)

    def is_open(self, n: str) -> bool:
        return self.node_factor(n) >= CLOSED_BELOW

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

    def now_dt(self) -> datetime:
        return self.start + timedelta(hours=self.env.now)

    def log(self, kind: str, **kw) -> None:
        self.events.append({"t_h": round(self.env.now, 3), "ts": self.now_dt().isoformat(timespec="minutes"), "event": kind, **kw})

    # ------------------------------------------------------------ policy
    def lead_stats_days(self, dc: str, sku: str) -> tuple[float, float]:
        """Mean and variance (days, days^2) of the primary replenishment lead time."""
        path = self.net.replenishment[(dc, sku)][0]
        mean = var = 0.0
        for lid in path.lanes:
            l = self.net.lanes[lid]
            mean += l.lt_mean_h
            var += (math.exp(l.lt_sigma**2) - 1) * math.exp(2 * l.lt_mu + l.lt_sigma**2)
        return mean / 24.0 + HANDLING_DAYS, var / 576.0

    def policy(self, dc: str, sku: str, t_h: float) -> tuple[float, float]:
        """Dynamic (s, S): cover forecast demand over lead time + review period at service level z."""
        L, varL = self.lead_stats_days(dc, sku)
        R = 1.0
        horizon = L + R
        zones = [z for (z, s), (d, _) in self.net.serving.items() if s == sku and d == dc]
        day0 = (self.start + timedelta(hours=t_h)).date()
        days = int(math.ceil(horizon))
        mu_h, var_h = 0.0, 0.0
        for i in range(days):
            frac = min(1.0, horizon - i)
            for z in zones:
                m = self.demand.mean(z, sku, day0 + timedelta(days=i))
                mu_h += m * frac
                var_h += self.demand.variance(m, sku) * frac
        cfg = self.policy_cfg[self.net.skus[sku].family]
        mu_d = mu_h / horizon if horizon else 0.0
        s = mu_h + cfg["z"] * math.sqrt(var_h + mu_d * mu_d * varL)
        # order-up-to covers the *forecast* for the cover window after the lead time, so known
        # peaks (festivals) are pre-built; unannounced shocks (scenarios) are not foreseen
        cover = sum(self.demand.mean(z, sku, day0 + timedelta(days=days + i)) for i in range(int(cfg["cover_days"])) for z in zones)
        S = s + cover
        return s, S

    # ------------------------------------------------------------ processes
    def demand_proc(self, zone: str, sku: str):
        rng = _rng(self.seed, "demand", zone, sku)
        dc, lane = self.net.serving[(zone, sku)]
        promise = 24.0 + self.net.lanes[lane].lt_p90_h
        day = 0
        while True:
            hour = rng.uniform(8, 20)
            qty = self.demand.sample(rng, zone, sku, (self.start + timedelta(days=day)).date(), 1.0)
            yield self.env.timeout(day * 24 + hour - self.env.now)
            qty = int(round(qty * self.demand_mult(zone, sku)))
            if qty > 0:
                o = Order(len(self.orders), zone, sku, dc, lane, qty, self.env.now, self.env.now + promise)
                self.orders.append(o)
                self.k["units_demanded"] += qty
                self.k_sku[sku]["units_demanded"] += qty
                self.fulfil(o)
            day += 1
            yield self.env.timeout(day * 24 - self.env.now)

    def fulfil(self, o: Order) -> None:
        key = (o.dc, o.sku)
        alloc = min(o.qty, int(self.on_hand[key])) if self.is_open(o.dc) and not self.backlog[key] else 0
        self.on_hand[key] -= alloc
        o.immediate = alloc
        self.k["units_filled_immediately"] += alloc
        self.k_sku[o.sku]["units_filled_immediately"] += alloc
        if alloc:
            self.env.process(self.ship_to_zone(o, alloc))
        if alloc < o.qty:
            self.backlog[key].append([o, o.qty - alloc])
            self.k["units_backordered"] += o.qty - alloc
            self.k["penalty_cost"] += (o.qty - alloc) * self.net.skus[o.sku].stockout_penalty

    def serve_backlog(self, dc: str, sku: str) -> None:
        key = (dc, sku)
        q = self.backlog[key]
        while q and self.on_hand[key] >= 1 and self.is_open(dc):
            o, rem = q[0]
            give = min(rem, int(self.on_hand[key]))
            self.on_hand[key] -= give
            self.env.process(self.ship_to_zone(o, give))
            if give == rem:
                q.popleft()
            else:
                q[0][1] = rem - give

    def ship_to_zone(self, o: Order, qty: int):
        left = qty
        while left > 0:  # DC outbound throughput budget
            while not self.is_open(o.dc):
                yield self.env.timeout(1)
            take = min(left, self.dispatch_budget[o.dc])
            if take <= 0:
                yield self.env.timeout(24 - (self.env.now % 24))
                continue
            self.dispatch_budget[o.dc] -= take
            left -= take
            yield from self.traverse(o.lane, take, o.sku)
            o.delivered += int(take)
            if o.delivered >= o.qty and o.done_h is None:
                o.done_h = self.env.now

    def traverse(self, lane_id: str, qty: float, sku: str, remaining_h: float | None = None):
        """Move qty over one lane: wait while the origin is closed, transit (plus any overlap with
        lane slow-downs), then unload — waiting at anchorage/yard while the destination is closed and
        paying extra handling dwell while it runs at reduced capacity.
        remaining_h: the shipment is already under way (initial pipeline) with this much left."""
        lane = self.net.lanes[lane_id]
        if remaining_h is None:
            while not self.is_open(lane.from_id):
                yield self.env.timeout(1)
            f = self.node_factor(lane.from_id)
            if f < 1:
                yield self.env.timeout(NODE_HANDLING_H[self.net.nodes[lane.from_id].type] * (1 / f - 1))
        t = remaining_h if remaining_h is not None else float(self.lane_rng[lane_id].lognormal(lane.lt_mu, lane.lt_sigma))
        yield self.env.timeout(t + self.lane_extra_h(lane_id, t))
        while not self.is_open(lane.to_id):
            yield self.env.timeout(1)  # anchorage / holding yard
        f = self.node_factor(lane.to_id)
        if f < 1:
            yield self.env.timeout(NODE_HANDLING_H[self.net.nodes[lane.to_id].type] * (1 / f - 1))
        w_t = self.net.skus[sku].unit_weight_kg / 1000.0
        self.k["transport_cost"] += qty * lane.cost_per_unit
        self.k["co2_kg"] += qty * w_t * lane.distance_km * lane.co2_per_tkm
        self.k["unit_km"] += qty * lane.distance_km

    def review_proc(self, dc: str, sku: str):
        key = (dc, sku)
        yield self.env.timeout(REVIEW_HOUR)
        while True:
            s, S = self.policy(dc, sku, self.env.now)
            backlog = sum(r for _, r in self.backlog[key])
            ip = self.on_hand[key] + self.on_order[key] - backlog
            if ip < s:
                q = math.ceil(S - ip)
                path = self.net.replenishment[key][0]
                self.on_order[key] += q
                self.prod_queue[path.source].append([dc, sku, q, 0.0, 0.0])  # dc, sku, qty, produced, shipped
                self.log("replenish", dc=dc, sku=sku, qty=q, source=path.source, s=round(s), S=round(S), ip=round(ip))
            yield self.env.timeout(24)

    def production_proc(self, sup: str):
        """Hourly output shared evenly across open orders; produced stock ships daily at 18:00."""
        cap_h = self.net.nodes[sup].capacity / 24.0
        while True:
            yield self.env.timeout(1)
            q = self.prod_queue[sup]
            budget = cap_h * self._f("production_factor", sup)
            while q and budget > 1e-9:
                open_jobs = [j for j in q if j[3] < j[2]]
                if not open_jobs:
                    break
                share = budget / len(open_jobs)
                for j in open_jobs:
                    make = min(share, j[2] - j[3])
                    j[3] += make
                    budget -= make
            if self.env.now % 24 == DISPATCH_HOUR or any(j[3] >= j[2] for j in q):
                for j in list(q):
                    ready = j[3] - j[4]
                    if ready >= 1 and (self.env.now % 24 == DISPATCH_HOUR or j[3] >= j[2]):
                        j[4] += ready
                        self.env.process(self.ship_replenishment(j[0], j[1], ready))
                    if j[4] >= j[2] - 1e-9:
                        q.remove(j)

    def seed_pipeline(self, dc: str, sku: str) -> None:
        """Start in steady state: one shipment per past day already moving along the primary path,
        and on-hand stock equal to the order-up-to level minus what is in transit."""
        key = (dc, sku)
        s, S = self.policy(dc, sku, 0.0)
        path = self.net.replenishment[key][0]
        legs = [self.net.lanes[l].lt_mean_h for l in path.lanes]
        total = sum(legs)
        zones = [z for (z, k), (d, _) in self.net.serving.items() if k == sku and d == dc]
        daily = sum(self.demand.base_mean(z, sku) for z in zones)
        in_transit = 0.0
        for age_days in range(int(total // 24) + 1):
            elapsed = age_days * 24.0 + 12.0  # dispatched at 18:00 on a past day
            if elapsed >= total:
                break
            leg, before = 0, 0.0
            while before + legs[leg] <= elapsed:
                before += legs[leg]
                leg += 1
            self.env.process(self.ship_replenishment(dc, sku, daily, start_leg=leg, first_remaining=before + legs[leg] - elapsed))
            in_transit += daily
        self.on_order[key] = in_transit
        self.on_hand[key] = max(S - in_transit, s - in_transit + daily * 2, daily * 2)

    def ship_replenishment(self, dc: str, sku: str, qty: float, start_leg: int = 0, first_remaining: float | None = None):
        path = self.net.replenishment[(dc, sku)][0]
        for i, lane_id in enumerate(path.lanes[start_leg:], start=start_leg):
            yield from self.traverse(lane_id, qty, sku, remaining_h=first_remaining if i == start_leg else None)
        key = (dc, sku)
        self.on_hand[key] += qty
        self.on_order[key] -= qty
        self.log("receipt", dc=dc, sku=sku, qty=round(qty))
        self.serve_backlog(dc, sku)

    def daily_proc(self):
        while True:
            for dc in self.net.of_type("dc"):
                self.dispatch_budget[dc.id] = dc.capacity * self._f("dispatch_factor", dc.id)
            for dc, sku in self.pairs:  # reopened DCs / refilled budgets serve waiting orders
                self.serve_backlog(dc, sku)
            yield self.env.timeout(24)

    def monitor_proc(self):
        while True:
            for dc, sku in self.pairs:
                oh = self.on_hand[(dc, sku)]
                sk = self.net.skus[sku]
                self.k["holding_cost"] += oh * sk.unit_value * sk.holding_rate_yr / 8760.0
                self.k_sku[sku]["on_hand_h"] += oh
                if self.backlog[(dc, sku)]:
                    self.stockout_h[(dc, sku)] += 1
            self.k["hours"] += 1
            yield self.env.timeout(1)

    # ------------------------------------------------------------ disruptions
    def build_effect(self, sc: Scenario) -> Effect:
        e = Effect(label=sc.name or sc.type.value)
        net, p = self.net, sc.params
        if sc.type is ScenarioType.port_closure:
            e.node_factor[sc.target] = sc.scaled(p["capacity_factor"], 1.0)
        elif sc.type is ScenarioType.cyclone:
            if sc.polygon:
                inside = lambda lat, lon: point_in_polygon(lon, lat, sc.polygon)  # noqa: E731
            else:
                c = net.nodes[sc.target]
                inside = lambda lat, lon: haversine_km(lat, lon, c.lat, c.lon) <= p["radius_km"]  # noqa: E731
            if p["close_nodes"]:
                for n in net.nodes.values():
                    if n.type != "zone" and (inside(n.lat, n.lon) or n.id == sc.target):
                        e.node_factor[n.id] = sc.scaled(0.0, 1.0)
            for l in net.lanes.values():
                a, b = net.nodes[l.from_id], net.nodes[l.to_id]
                if any(inside(a.lat + (b.lat - a.lat) * i / 20, a.lon + (b.lon - a.lon) * i / 20) for i in range(21)):
                    e.lane_mult[l.id] = sc.scaled(p["lane_time_multiplier"], 1.0)
        elif sc.type is ScenarioType.road_flood:
            lanes = p["lanes"] or ([sc.target] if sc.target in net.lanes else
                                   [l.id for l in net.lanes.values() if l.mode == "road" and sc.target in (l.from_id, l.to_id)])
            for lid in lanes:
                e.lane_mult[lid] = 1.0 / sc.scaled(p["speed_factor"], 1.0)
        elif sc.type is ScenarioType.demand_spike:
            for sku in (p["skus"] or ["*"]):
                e.demand_mult[(sc.target, sku)] = sc.scaled(p["multiplier"], 1.0)
        elif sc.type is ScenarioType.supplier_failure:
            e.production_factor[sc.target] = sc.scaled(p["capacity_factor"], 1.0)
        elif sc.type is ScenarioType.strike:
            f = sc.scaled(p["throughput_factor"], 1.0)
            kind = net.nodes[sc.target].type
            if kind == "dc":  # outbound dispatch and cross-dock handling both slow down
                e.dispatch_factor[sc.target] = f
                e.node_factor[sc.target] = f
            elif kind in ("plant", "supplier"):
                e.production_factor[sc.target] = f
            else:
                e.node_factor[sc.target] = f
        # data_blackout: no physical effect on goods — handled by the trust layer / telemetry (Phase 7)
        return e

    def scenario_proc(self, sc: Scenario):
        sc.validate_against({n.id: n.type for n in self.net.nodes.values()}, self.net.lanes)
        yield self.env.timeout(sc.start_offset_h(self.start))
        eff = self.build_effect(sc)
        eff.end_h = self.env.now + sc.duration_h
        self.effects.append(eff)
        self.log("disruption_start", type=sc.type.value, target=sc.target, severity=sc.severity,
                 nodes=sorted(eff.node_factor), lanes=sorted(eff.lane_mult))
        yield self.env.timeout(sc.duration_h)
        self.effects.remove(eff)
        self.log("disruption_end", type=sc.type.value, target=sc.target)
        for dc, sku in self.pairs:
            self.serve_backlog(dc, sku)

    # ------------------------------------------------------------ run + KPIs
    def run(self, days: float) -> dict:
        self.env.run(until=days * 24)
        return self.kpis(days)

    def kpis(self, days: float) -> dict:
        end = self.env.now
        due = [o for o in self.orders if o.promised_h <= end]
        otif = [o for o in due if o.immediate == o.qty and o.done_h is not None and o.done_h <= o.promised_h]
        demanded = self.k["units_demanded"] or 1
        daily_demand = demanded / days
        on_hand_avg = sum(self.on_hand_avg().values())
        per_sku = {}
        for sku, v in self.k_sku.items():
            sku_due = [o for o in due if o.sku == sku]
            sku_otif = [o for o in otif if o.sku == sku]
            dd = v["units_demanded"] / days or 1
            per_sku[sku] = {
                "units_demanded": int(v["units_demanded"]),
                "fill_rate": v["units_filled_immediately"] / (v["units_demanded"] or 1),
                "otif": len(sku_otif) / (len(sku_due) or 1),
                "backorder_units_end": int(sum(r for (d, s), q in self.backlog.items() if s == sku for _, r in q)),
                "avg_on_hand": v["on_hand_h"] / (self.k["hours"] or 1),
                "days_of_cover": v["on_hand_h"] / (self.k["hours"] or 1) / dd,
            }
        per_dc = {f"{dc}/{sku}": {"on_hand_end": round(self.on_hand[(dc, sku)]),
                                  "backlog_end": int(sum(r for _, r in self.backlog[(dc, sku)])),
                                  "stockout_hours": self.stockout_h[(dc, sku)]} for dc, sku in self.pairs}
        cost = {"transport": self.k["transport_cost"], "holding": self.k["holding_cost"], "penalty": self.k["penalty_cost"]}
        cost["total"] = sum(cost.values())
        return {
            "days": days, "start": self.start.isoformat(timespec="minutes"), "seed": self.seed,
            "orders": len(self.orders), "orders_due": len(due),
            "fill_rate": self.k["units_filled_immediately"] / demanded,
            "otif": len(otif) / (len(due) or 1),
            "units_demanded": int(self.k["units_demanded"]),
            "backorder_units_end": int(sum(r for q in self.backlog.values() for _, r in q)),
            "avg_on_hand": on_hand_avg, "inventory_days": on_hand_avg / daily_demand,
            "cost_inr": cost, "co2_t": self.k["co2_kg"] / 1000.0,
            "per_sku": per_sku, "per_dc_sku": per_dc,
            "disruptions": [e for e in self.events if e["event"].startswith("disruption")],
            "replenishment_orders": sum(1 for e in self.events if e["event"] == "replenish"),
        }

    def on_hand_avg(self) -> dict[str, float]:
        return {sku: v["on_hand_h"] / (self.k["hours"] or 1) for sku, v in self.k_sku.items()}
