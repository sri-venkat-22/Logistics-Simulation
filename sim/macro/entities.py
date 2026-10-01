"""Macro-twin entities (SimPy). Clock unit: hours.

    Supplier    production lines as a capacity `Resource`; hourly output x production factor, shared by the
                jobs holding a line; produced stock ships daily at DISPATCH_HOUR (partial shipments) and on
                completion; random failure / recovery (MTBF / MTTR) goes through the effect system
    Port        berths as a `Resource`; ships wait at anchorage while the port is closed, then berth
                (service time / capacity factor) and clear customs (imports only)
    Warehouse   per-SKU `Container` inventory with a storage capacity, backlog (FIFO), on-order pipeline,
                a replenishment policy process per SKU ((s,S), (R,Q) or forecast-driven (s,S)), daily
                dispatch budget
    Lane        transit process: lognormal lead time (or SUMO-calibrated handling + drive time) x the live
                multiplier, plus the overlap with disruption slow-downs; hands city legs to the micro-twin
                when a coupler is attached
    DemandZone  order generator per SKU (one order per day at a random hour, or a renewal process)
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING

import simpy

from sim.macro.network import Lane as LaneSpec
from sim.macro.network import Node
from sim.macro.policies import Policy

if TYPE_CHECKING:
    from sim.macro.engine import Twin

REVIEW_HOUR = 6.0
DISPATCH_HOUR = 18.0
NODE_HANDLING_H = {"port": 24.0, "dc": 12.0, "plant": 6.0, "supplier": 6.0, "zone": 0.0}
BERTH_MEAN_H, BERTH_SIGMA = 8.0, 0.35      # vessel unloading time per call
CUSTOMS_MEAN_H, CUSTOMS_SIGMA = 18.0, 0.5  # import clearance
# random outages: mean time between failures (days) and mean time to repair (hours) by node kind
FAILURES = {"plant": (45.0, 10.0), "supplier": (60.0, 12.0), "overseas": (90.0, 24.0)}


def lognormal_params(mean: float, sigma: float) -> tuple[float, float]:
    return math.log(mean) - sigma * sigma / 2, sigma


def lognormal_var(mean: float, sigma: float) -> float:
    return (math.exp(sigma * sigma) - 1) * mean * mean


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
    lost: int = 0


@dataclass
class Shipment:
    id: str
    kind: str                 # replenishment | delivery
    sku: str
    qty: float
    nodes: tuple[str, ...]
    lanes: tuple[str, ...]
    created_h: float
    order_id: int | None = None
    status: str = "planned"   # loading | transit | dwell | anchorage | berth | customs | city | delivered
    leg: int = 0
    where: str = ""           # node id while dwelling, lane id while moving
    seg_start_h: float = 0.0
    seg_end_h: float = 0.0    # expected end of the current segment
    depart_h: float | None = None
    arrive_h: float | None = None
    trucks: list[str] = field(default_factory=list)  # micro-twin vehicle ids while inside Hyderabad
    seg_from: tuple[float, float] | None = None  # (lat, lon) the current segment starts at (default: lane origin)
    seg_to: tuple[float, float] | None = None    # (lat, lon) it ends at (default: lane destination)

    @property
    def dest(self) -> str:
        return self.nodes[-1]

    def to_dict(self) -> dict:
        d = asdict(self)
        d["nodes"], d["lanes"] = list(self.nodes), list(self.lanes)
        return d


# ---------------------------------------------------------------------------------------------- Supplier
@dataclass
class Job:
    dc: str
    sku: str
    qty: float
    created_h: float
    made: float = 0.0
    shipped: float = 0.0
    started_h: float | None = None


class Supplier:
    def __init__(self, twin: Twin, node: Node, random_failures: bool = True):
        self.twin, self.node, self.id = twin, node, node.id
        env = twin.env
        self.infinite = math.isinf(node.capacity)
        self.n_lines = int(node.attrs.get("lines", 6))
        self.lines = simpy.Resource(env, capacity=self.n_lines)  # concurrent production jobs
        self.cap_h = node.capacity / 24.0
        self.jobs: list[Job] = []    # all open jobs (queued or on a line)
        self.active: list[Job] = []  # jobs holding a line: they share the hourly output
        self._done: dict[int, simpy.Event] = {}
        kind = "overseas" if node.attrs.get("overseas") else node.type
        mtbf_d = node.attrs.get("mtbf_days", FAILURES.get(kind, (0, 0))[0])
        self.mtbf_h, self.mttr_h = mtbf_d * 24.0, node.attrs.get("mttr_h", FAILURES.get(kind, (0, 0))[1])
        if not self.infinite:
            env.process(self.production_proc())
            if random_failures and self.mtbf_h > 0:
                env.process(self.failure_proc())

    def factor(self) -> float:
        return self.twin.production_factor(self.id)

    def order(self, dc: str, sku: str, qty: float) -> Job:
        job = Job(dc, sku, qty, self.twin.env.now)
        if self.infinite:  # uncapacitated source (toy networks): ships at once
            job.made = job.shipped = qty
            job.started_h = self.twin.env.now
            self.twin.dispatch_replenishment(self.id, dc, sku, qty)
            return job
        self.jobs.append(job)
        self.twin.env.process(self.job_proc(job))
        return job

    def job_proc(self, job: Job):
        env = self.twin.env
        with self.lines.request() as req:
            yield req
            job.started_h = env.now
            self.twin.log("production_start", supplier=self.id, dc=job.dc, sku=job.sku, qty=round(job.qty),
                          wait_h=round(env.now - job.created_h, 2))
            self.active.append(job)
            done = self._done[id(job)] = env.event()
            yield done
        self.active.remove(job)
        self.jobs.remove(job)

    def production_proc(self):
        """Hourly output x production factor, shared evenly by the jobs on a line; produced stock ships
        daily at DISPATCH_HOUR and whatever finishes a job ships at once."""
        env = self.twin.env
        while True:
            h = self.twin.hod()
            yield env.timeout(1 - h % 1 if h % 1 > 1e-9 else 1)  # on the (local) hour
            budget = self.cap_h * self.factor()
            while budget > 1e-9:
                open_jobs = [j for j in self.active if j.made < j.qty - 1e-9]
                if not open_jobs:
                    break
                share = budget / len(open_jobs)
                for j in open_jobs:
                    make = min(share, j.qty - j.made)
                    j.made += make
                    budget -= make
            dispatch_now = abs(self.twin.hod() - DISPATCH_HOUR) < 1e-6
            for j in list(self.active):
                ready = j.made - j.shipped
                finished = j.made >= j.qty - 1e-9
                if (ready >= 1 and dispatch_now) or (finished and ready > 1e-9):
                    j.shipped += ready
                    self.twin.dispatch_replenishment(self.id, j.dc, j.sku, ready)
                if finished and j.shipped >= j.qty - 1e-9:
                    ev = self._done.pop(id(j), None)
                    if ev is not None and not ev.triggered:
                        ev.succeed()

    def failure_proc(self):
        env = self.twin.env
        rng = self.twin.rng("failure", self.id)
        mu, sigma = lognormal_params(self.mttr_h, 0.5)
        while True:
            yield env.timeout(float(rng.exponential(self.mtbf_h)))
            dur = float(rng.lognormal(mu, sigma))
            self.twin.start_random_failure(self.id, dur)
            yield env.timeout(dur)

    def snapshot(self) -> dict:
        return {"factor": round(self.factor(), 3), "lines_busy": self.lines.count, "lines": self.n_lines,
                "queue": len(self.lines.queue), "open_jobs": len(self.jobs),
                "backlog_units": round(sum(j.qty - j.made for j in self.jobs))}


# ---------------------------------------------------------------------------------------------- Port
class Port:
    def __init__(self, twin: Twin, node: Node):
        self.twin, self.node, self.id = twin, node, node.id
        self.n_berths = int(node.attrs.get("berths", 3))
        self.berths = simpy.Resource(twin.env, capacity=self.n_berths)
        self.rng = twin.rng("port", self.id)
        self.anchorage = 0
        self.customs = 0
        self.berth_mu, self.berth_sigma = lognormal_params(BERTH_MEAN_H, BERTH_SIGMA)
        self.cust_mu, self.cust_sigma = lognormal_params(CUSTOMS_MEAN_H, CUSTOMS_SIGMA)

    @staticmethod
    def expected_dwell(import_: bool) -> tuple[float, float]:
        """Mean and variance (hours) of berth + customs time with no queueing."""
        m = BERTH_MEAN_H + (CUSTOMS_MEAN_H if import_ else 0.0)
        v = lognormal_var(BERTH_MEAN_H, BERTH_SIGMA) + (lognormal_var(CUSTOMS_MEAN_H, CUSTOMS_SIGMA) if import_ else 0.0)
        return m, v

    def handle(self, shp: Shipment, import_: bool, remaining_h: float | None = None):
        """Anchorage while closed -> berth (unload) -> customs. remaining_h: seeded mid-dwell."""
        env, twin = self.twin.env, self.twin
        if remaining_h is not None:
            twin.set_status(shp, "customs" if import_ else "berth", self.id, remaining_h)
            yield env.timeout(remaining_h)
            return
        if not twin.is_open(self.id):
            self.anchorage += 1
            twin.set_status(shp, "anchorage", self.id, 0)
            twin.log("anchorage", shipment=shp.id, port=self.id, sku=shp.sku, qty=round(shp.qty))
            while not twin.is_open(self.id):
                yield env.timeout(1)
            self.anchorage -= 1
        berth_draw = float(self.rng.lognormal(self.berth_mu, self.berth_sigma))
        customs_draw = float(self.rng.lognormal(self.cust_mu, self.cust_sigma)) if import_ else 0.0
        with self.berths.request() as req:
            yield req
            service = berth_draw / max(twin.node_factor(self.id), 1e-3)
            twin.set_status(shp, "berth", self.id, service)
            yield env.timeout(service)
        if import_:
            self.customs += 1
            twin.set_status(shp, "customs", self.id, customs_draw)
            yield env.timeout(customs_draw)
            self.customs -= 1

    def snapshot(self) -> dict:
        f = self.twin.node_factor(self.id)
        return {"factor": round(f, 3), "status": self.twin.node_status(self.id), "berths": self.n_berths,
                "berths_busy": self.berths.count, "berth_queue": len(self.berths.queue),
                "anchorage": self.anchorage, "customs": self.customs}


# ---------------------------------------------------------------------------------------------- Warehouse
class Warehouse:
    def __init__(self, twin: Twin, node: Node, storage_days: float = 30.0):
        self.twin, self.node, self.id = twin, node, node.id
        self.storage_days = storage_days
        self.stock: dict[str, simpy.Container] = {}
        self.policy: dict[str, Policy] = {}
        self.on_order: dict[str, float] = {}
        self.yard: dict[str, float] = {}           # delivered but waiting for storage space
        self.backlog: dict[str, deque[list]] = {}   # sku -> [order, remaining]
        self.stockout_since: dict[str, float | None] = {}
        self.dispatch_budget = node.capacity
        self.review_trigger: dict[str, simpy.Event] = {}

    def add_sku(self, sku: str, policy: Policy, init: float, capacity: float) -> None:
        env = self.twin.env
        self.stock[sku] = simpy.Container(env, capacity=max(capacity, init, 1.0), init=max(init, 0.0))
        self.policy[sku] = policy
        self.on_order[sku] = 0.0
        self.yard[sku] = 0.0
        self.backlog[sku] = deque()
        self.stockout_since[sku] = None
        env.process(self.review_proc(sku))

    # -------------------------------------------------------------- state
    def level(self, sku: str) -> float:
        return self.stock[sku].level

    def backlog_units(self, sku: str) -> float:
        return sum(r for _, r in self.backlog[sku])

    def position(self, sku: str) -> float:
        return self.level(sku) + self.on_order[sku] + self.yard[sku] - self.backlog_units(sku)

    def can_serve(self, sku: str, qty: float) -> bool:
        return self.twin.is_open(self.id) and not self.backlog[sku] and self.level(sku) >= qty

    def take(self, sku: str, qty: float) -> float:
        """Withdraw up to qty now (never ahead of the backlog); returns what was taken."""
        if not self.twin.is_open(self.id) or self.backlog[sku]:
            return 0.0
        amt = float(min(qty, math.floor(self.level(sku))))
        if amt > 0:
            self.stock[sku].get(amt)  # immediate: level >= amt
        self.after_withdrawal(sku)
        return amt

    def after_withdrawal(self, sku: str) -> None:
        if self.policy[sku].review_h == 0:  # continuous review
            ev = self.review_trigger.get(sku)
            if ev is not None and not ev.triggered:
                ev.succeed()

    # -------------------------------------------------------------- backlog / stock-out episodes
    def backorder(self, order: Order, qty: float) -> None:
        if not self.backlog[order.sku] and self.stockout_since[order.sku] is None:
            self.stockout_since[order.sku] = self.twin.env.now
            self.twin.stockout_started(self.id, order.sku)
        self.backlog[order.sku].append([order, qty])
        self.after_withdrawal(order.sku)

    def serve_backlog(self, sku: str) -> None:
        q = self.backlog[sku]
        while q and self.level(sku) >= 1 and self.twin.is_open(self.id):
            o, rem = q[0]
            give = float(min(rem, math.floor(self.level(sku))))
            self.stock[sku].get(give)
            self.twin.ship_to_zone(o, give, self.id, o.lane)
            if give >= rem:
                q.popleft()
            else:
                q[0][1] = rem - give
        if not q and self.stockout_since[sku] is not None:
            self.twin.stockout_ended(self.id, sku, self.stockout_since[sku])
            self.stockout_since[sku] = None

    # -------------------------------------------------------------- replenishment
    def review_proc(self, sku: str):
        env, pol = self.twin.env, self.policy[sku]
        if pol.review_h > 0:
            h = self.twin.hod()
            yield env.timeout(REVIEW_HOUR - h if h <= REVIEW_HOUR else 24 - h + REVIEW_HOUR)
            while True:
                self.review(sku)
                yield env.timeout(pol.review_h)
        else:
            while True:
                self.review(sku)
                self.review_trigger[sku] = env.event()
                yield self.review_trigger[sku]

    def review(self, sku: str) -> None:
        ip = self.position(sku)
        q = self.policy[sku].order_qty(self.twin, self.id, sku, ip, self.twin.env.now)
        if q > 0:
            self.on_order[sku] += q
            self.twin.place_replenishment(self.id, sku, q, ip)

    def receive(self, sku: str, qty: float):
        """Put a delivery on the shelf. Container.put raises the level synchronously, so the on-order
        bookkeeping must move in the same step (a review in the same instant would otherwise count the
        stock twice). If storage is full the overflow waits in the yard, still part of the position."""
        if qty <= 0:
            return
        c = self.stock[sku]
        self.on_order[sku] -= qty
        ev = c.put(qty)
        if not ev.triggered:
            self.yard[sku] += qty
            self.twin.log("yard_wait", dc=self.id, sku=sku, qty=round(qty))
            yield ev
            self.yard[sku] -= qty
        self.serve_backlog(sku)

    def snapshot(self) -> dict:
        out = {}
        for sku in self.stock:
            s, S = self.policy[sku].levels(self.twin, self.id, sku, self.twin.env.now)
            out[sku] = {"on_hand": round(self.level(sku), 1), "on_order": round(self.on_order[sku], 1),
                        "backlog": round(self.backlog_units(sku), 1), "s": round(s, 1), "S": round(S, 1),
                        "capacity": round(self.stock[sku].capacity), "stockout": self.stockout_since[sku] is not None}
        return out


# ---------------------------------------------------------------------------------------------- Lane
class Lane:
    def __init__(self, twin: Twin, spec: LaneSpec):
        self.twin, self.spec, self.id = twin, spec, spec.id
        self.rng = twin.rng("lane", spec.id)
        self.live_mult = 1.0      # observed multiplier (e.g. from SUMO corridor times after a road closure)
        self.calibration: dict | None = None  # {"handling_h", "drive_mu", "drive_sigma", ...} from sim/micro/calibrate.py

    def calibrate(self, cal: dict) -> None:
        self.calibration = cal

    def sample_h(self) -> float:
        c = self.calibration
        if c:
            return c["handling_h"] + float(self.rng.lognormal(c["drive_mu"], c["drive_sigma"]))
        return float(self.rng.lognormal(self.spec.lt_mu, self.spec.lt_sigma))

    def mean_var_h(self) -> tuple[float, float]:
        c = self.calibration
        if c:
            m = math.exp(c["drive_mu"] + c["drive_sigma"] ** 2 / 2)
            return (c["handling_h"] + m) * self.live_mult, lognormal_var(m, c["drive_sigma"]) * self.live_mult ** 2
        m = self.spec.lt_mean_h
        return m * self.live_mult, lognormal_var(m, self.spec.lt_sigma) * self.live_mult ** 2

    def transit(self, shp: Shipment, remaining_h: float | None = None):
        twin = self.twin
        if remaining_h is not None:  # seeded pipeline: already under way, part-way along the lane
            a, b = twin.net.nodes[self.spec.from_id], twin.net.nodes[self.spec.to_id]
            f0 = max(0.0, min(1.0, 1 - remaining_h / max(self.mean_var_h()[0], 1e-6)))
            twin.set_status(shp, "transit", self.id, remaining_h,
                            seg_from=(a.lat + (b.lat - a.lat) * f0, a.lon + (b.lon - a.lon) * f0))
            yield twin.env.timeout(remaining_h)
            return
        t = self.sample_h() * self.live_mult
        t += twin.lane_extra_h(self.id, t)
        if twin.coupler is not None:
            handled = yield from twin.coupler.transit(shp, self, t)
            if handled:
                return
        twin.set_status(shp, "transit", self.id, t)
        yield twin.env.timeout(t)


# ---------------------------------------------------------------------------------------------- DemandZone
class DemandZone:
    def __init__(self, twin: Twin, node: Node, skus: list[str]):
        self.twin, self.node, self.id = twin, node, node.id
        for sku in skus:
            gen = self.renewal if hasattr(twin.demand, "interarrival_h") else self.daily
            twin.env.process(gen(sku))

    def daily(self, sku: str):
        """One order per day at a random local hour between 08:00 and 20:00."""
        twin, env = self.twin, self.twin.env
        rng = twin.rng("demand", self.id, sku)
        c0 = twin.clock0_h
        day = int((env.now + c0) // 24)  # local day index from the start date
        while True:
            hour = rng.uniform(8, 20)
            qty = twin.demand.sample(rng, self.id, sku, (twin.start.replace(hour=0, minute=0, second=0, microsecond=0)
                                                        + timedelta(days=day)).date(), 1.0)
            t = day * 24 + hour - c0
            if t >= env.now:
                yield env.timeout(t - env.now)
                twin.new_order(self.id, sku, int(round(qty * twin.demand_mult(self.id, sku))))
            day += 1
            yield env.timeout(max(0.0, day * 24 - c0 - env.now))

    def renewal(self, sku: str):
        """Orders from a renewal process (toy models: Poisson arrivals, deterministic streams)."""
        twin, env = self.twin, self.twin.env
        rng = twin.rng("demand", self.id, sku)
        while True:
            yield env.timeout(twin.demand.interarrival_h(rng, self.id, sku))
            qty = twin.demand.quantity(rng, self.id, sku)
            twin.new_order(self.id, sku, int(round(qty * twin.demand_mult(self.id, sku))))
