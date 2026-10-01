"""Reality Emulator: a second, separately seeded instance of the coupled engines that *plays the physical
world* and publishes telemetry like IoT devices would (ADR-0002).

    em = RealityEmulator(start, seed=1042, micro=MicroProcess(seed=1042, fcd_every=1).start(),
                         sink=JsonlSink("telemetry.jsonl.gz"), attacks=AttackInjector.random_campaign(50, 2))
    em.run_until(2.0); em.close()
    em.truth()   # hidden perturbations + attack labels (never published)

Hidden perturbations (the live twin does not know about them):
    lane bias        every road lane secretly 0-15 % slower than the twin's lead-time model
    incidents        Poisson (1/day) unannounced slow-downs: a random road/rail lane x1.5-3 for 4-24 h
    demand drift     per zone, a daily log-random-walk demand multiplier (+0.3 %/day mean, 2 % sd)
Telemetry (envelope + payload per sim/reality/schemas.py, HMAC-signed per device):
    gps     every truck on a road leg at 1 Hz by default: SUMO trucks inside Hyderabad (true micro
            positions, the micro worker's fcd_every sets their period) and national trucks (interpolated
            along their lane, `national_gps_period_s`), with 4 m noise, HDOP and occasional multipath jumps
    stock   warehouse cycle counts per DC x SKU every 15 min (count noise)
    asn     supplier advance shipping notices at dispatch, one per truck-load, with ETA
    port    berth / queue / customs status per port every hour
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from sim.coupling.orchestrator import TRUCK_CAP, CoupledTwin
from sim.macro.demand import DemandModel
from sim.macro.disruptions import Effect
from sim.macro.engine import Twin
from sim.macro.network import Network
from sim.micro.process import MicroProcess
from sim.reality.attacks import AttackInjector
from sim.reality.telemetry import DEFAULT_MASTER, MemorySink, MessageFactory, Signer, Sink


@dataclass
class Perturbations:
    lane_bias_max: float = 0.15
    incident_rate_per_day: float = 1.0
    incident_mult: tuple[float, float] = (1.5, 3.0)
    incident_hours: tuple[float, float] = (4.0, 24.0)
    demand_drift_mu: float = 0.003
    demand_drift_sigma: float = 0.02


def _bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2, dl = math.radians(lat1), math.radians(lat2), math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


class RealityEmulator:
    def __init__(self, start: datetime, seed: int = 1042, micro: MicroProcess | None = None, sink: Sink | None = None,
                 attacks: AttackInjector | None = None, perturb: Perturbations | None = Perturbations(),
                 gps_period_s: float = 1.0, national_gps_period_s: float | None = None, national_gps: bool = True,
                 stock_period_s: float = 900,
                 port_period_s: float = 3600, gps_noise_m: float = 4.0, multipath_p: float = 0.003,
                 master: bytes = DEFAULT_MASTER, realtime_factor: float | None = None, sync_s: float = 60,
                 telemetry_from_h: float = 0.0, run_salt: str = ""):
        """telemetry_from_h: warm the world up silently and publish from this simulated hour on."""
        self.start, self.seed = start, seed
        self.telemetry_from_h = telemetry_from_h
        self.chaos_poll = None  # callable returning live chaos commands (sim/reality/run.py --chaos-redis)
        self.plan_poll = None   # callable returning applied plans (POST /plans/{id}/apply -> stream plan.commands)
        self.plan_ack = None    # fn(plan_id, acks): report what the fleet dispatched (shipment ids of transfers)
        self.plan_routes: dict[tuple[str, str], tuple[str, tuple]] = {}  # (dc, sku) -> (plan id, lanes) of reroutes
        net = Network()
        self.twin = Twin(net, DemandModel(net), start, seed=seed, realtime_factor=realtime_factor)
        self.env = self.twin.env
        self.rng = np.random.default_rng([seed, 0x5EA1])
        self.sink = sink or MemorySink()
        self.signer = Signer(master)
        self.factory = MessageFactory(seed, self.signer, run_salt)  # salt: live runs never reuse a message id
        self.injector = attacks or AttackInjector([])
        self.injector.bind(self)
        self.gps_period_s, self.gps_noise_m, self.multipath_p = gps_period_s, gps_noise_m, multipath_p
        self.national_gps_period_s = national_gps_period_s or gps_period_s
        self.stock_period_s, self.port_period_s = stock_period_s, port_period_s
        self.truth_log: list[dict] = []
        self.counts = {"clean": 0, "attacked": 0, "dropped": 0, "by_kind": {}}
        self.coupled = CoupledTwin(self.twin, micro, sync_s=sync_s) if micro is not None else None
        if self.coupled:
            self.coupled.listeners.append(self._on_micro)
        self.twin.listeners.append(self._on_event)
        if perturb:
            self._perturb(perturb)
        if national_gps:
            self.env.process(self._national_gps())
        self.env.process(self._stock())
        self.env.process(self._ports())
        self.env.process(self._attack_clock())

    # ------------------------------------------------------------------ helpers
    def ts(self, t_h: float) -> str:
        return (self.start + timedelta(hours=t_h)).isoformat(timespec="milliseconds")

    def resign(self, m: dict) -> dict:
        return self.signer.sign(m)

    def publish_raw(self, msgs: list[dict], attack=None, action: str = "clean") -> None:
        """Publish without passing through the attack transforms (replays)."""
        for m in msgs:
            self.injector.record(m, attack, action)
            self._count(m, attack is not None)
        self.sink.publish(msgs)

    def _count(self, m: dict, attacked: bool) -> None:
        self.counts["attacked" if attacked else "clean"] += 1
        self.counts["by_kind"][m["kind"]] = self.counts["by_kind"].get(m["kind"], 0) + 1

    def live(self) -> bool:
        return self.env.now >= self.telemetry_from_h - 1e-9

    def emit(self, msgs: list[dict]) -> None:
        if not msgs or not self.live():
            return
        now = self.env.now
        out = []
        for m, attack, action in self.injector.transform(msgs, now):
            if action == "dropped":  # blackout: never published; the label keeps what went missing
                attack.msgs.append({"msg_id": m["msg_id"], "occurrence": 0, "action": "dropped", "source_id": m["source_id"]})
                self.counts["dropped"] += 1
                continue
            self.injector.record(m, attack, action)
            self._count(m, attack is not None)
            out.append(m)
        self.sink.publish(out)

    def _noisy(self, lat: float, lon: float) -> tuple[float, float, float]:
        sd = self.gps_noise_m
        hdop = float(self.rng.uniform(0.7, 1.6))
        if self.rng.random() < self.multipath_p:  # urban-canyon / multipath jump: honest but ugly
            sd, hdop = float(self.rng.uniform(15, 60)), float(self.rng.uniform(3, 8))
        dn, de = self.rng.normal(0, sd, 2)
        return (lat + dn / 111_320.0, lon + de / (111_320.0 * math.cos(math.radians(lat))), hdop)

    def _gps(self, vid: str, t_h: float, lat: float, lon: float, speed_kmh: float, heading: float, shipment: str | None,
             scope: str) -> dict:
        nlat, nlon, hdop = self._noisy(lat, lon)
        payload = {"vehicle_id": vid, "lat": round(float(nlat), 7), "lon": round(float(nlon), 7),
                   "speed_kmh": round(max(0.0, float(speed_kmh) + float(self.rng.normal(0, 0.8))), 2),
                   "heading_deg": round(float(heading) % 360, 1) % 360, "hdop": round(hdop, 2), "shipment_id": shipment, "scope": scope}
        return self.factory.make("gps", f"gps:{vid}", self.ts(t_h), payload)

    # ------------------------------------------------------------------ hidden perturbations
    def _perturb(self, p: Perturbations) -> None:
        twin = self.twin
        for lid, lane in twin.lanes.items():
            if lane.spec.mode == "road" and not lane.spec.transfer_only:
                b = 1.0 + float(self.rng.uniform(0, p.lane_bias_max))
                lane.live_mult *= b
                self.truth_log.append({"kind": "lane_bias", "lane": lid, "multiplier": round(b, 4), "t_h": 0.0})
        self.env.process(self._incidents(p))
        self.env.process(self._demand_drift(p))

    def _incidents(self, p: Perturbations):
        lanes = [l for l, v in self.twin.lanes.items() if v.spec.mode in ("road", "rail") and not v.spec.transfer_only]
        while True:
            yield self.env.timeout(float(self.rng.exponential(24.0 / p.incident_rate_per_day)))
            lid = str(self.rng.choice(lanes))
            m = float(self.rng.uniform(*p.incident_mult))
            dur = float(self.rng.uniform(*p.incident_hours))
            e = self.twin.add_effect(Effect(label=f"hidden incident {lid}", kind="hidden", type="incident", target=lid,
                                            lane_mult={lid: m}), dur)
            self.truth_log.append({"kind": "incident", "lane": lid, "multiplier": round(m, 3), "t_h": round(self.env.now, 4),
                                   "ts": self.ts(self.env.now), "duration_h": round(dur, 2), "effect": e.id})

    def _demand_drift(self, p: Perturbations):
        zones = [n.id for n in self.twin.net.of_type("zone")]
        level = {z: 1.0 for z in zones}
        current: Effect | None = None
        while True:
            for z in zones:
                level[z] *= math.exp(float(self.rng.normal(p.demand_drift_mu, p.demand_drift_sigma)))
            if current is not None:
                self.twin.end_effect(current)
            current = self.twin.add_effect(Effect(label="hidden demand drift", kind="hidden", type="demand_drift",
                                                  target="*", demand_mult={(z, "*"): level[z] for z in zones}), None)
            self.truth_log.append({"kind": "demand_drift", "t_h": round(self.env.now, 4), "ts": self.ts(self.env.now),
                                   "levels": {z: round(v, 4) for z, v in level.items()}})
            yield self.env.timeout(24.0)

    # ------------------------------------------------------------------ telemetry streams
    def _national_gps(self):
        dt = self.national_gps_period_s / 3600.0
        net = self.twin.net
        while True:
            yield self.env.timeout(dt)
            if not self.live():
                continue
            now = self.env.now
            msgs = []
            for shp in list(self.twin.shipments.values()):
                if shp.status != "transit":
                    continue
                lane = net.lanes[shp.lanes[min(shp.leg, len(shp.lanes) - 1)]]
                if lane.mode != "road":
                    continue
                lat, lon, _ = self.twin.shipment_position(shp)
                a, b = net.nodes[lane.from_id], net.nodes[lane.to_id]
                span = max(shp.seg_end_h - shp.seg_start_h, 1e-6)
                speed = min(90.0, lane.distance_km / span)
                msgs.append(self._gps(f"TRK-{shp.id}", now, lat, lon, speed, _bearing(a.lat, a.lon, b.lat, b.lon), shp.id, "national"))
            self.emit(msgs)

    def _on_micro(self, kind: str, payload: dict) -> None:
        if kind != "fcd" or not self.live():
            return
        t_h = self.coupled.macro_h(payload["t"])
        msgs = [self._gps(r["id"], t_h, r["lat"], r["lon"], r["speed"] * 3.6, r["angle"], r.get("shipment"), "city")
                for r in payload["vehicles"]]
        self.emit(msgs)

    def _stock(self):
        while True:
            now = self.env.now
            msgs = []
            for dc, wh in self.twin.warehouses.items():
                for sku in wh.stock:
                    oh = wh.level(sku)
                    counted = max(0.0, round(oh + float(self.rng.normal(0, 0.002 * oh + 0.3))))
                    msgs.append(self.factory.make("stock", f"wms:{dc}", self.ts(now), {
                        "node": dc, "sku": sku, "on_hand": float(counted), "on_order": float(round(wh.on_order[sku] + wh.yard[sku])),
                        "backlog": float(round(wh.backlog_units(sku)))}))
            self.emit(msgs)
            yield self.env.timeout(self.stock_period_s / 3600.0)

    def _ports(self):
        while True:
            now = self.env.now
            msgs = []
            for pid, port in self.twin.ports.items():
                s = port.snapshot()
                msgs.append(self.factory.make("port", f"port:{pid}", self.ts(now), {
                    "port": pid, "status": s["status"], "berths_total": s["berths"], "berths_busy": s["berths_busy"],
                    "berth_queue": s["berth_queue"], "anchorage": s["anchorage"], "customs": s["customs"]}))
            self.emit(msgs)
            yield self.env.timeout(self.port_period_s / 3600.0)

    def _on_event(self, e: dict) -> None:
        """One ASN per truck-load of every supplier dispatch (a consignment note per truck)."""
        if e["event"] != "asn" or not self.live():
            return
        dc, sku = e["dc"], e["sku"]
        route = self.plan_routes.get((dc, sku))
        if route and tuple(e["lanes"]) == route[1] and self.plan_ack is not None:  # a shipment on a plan's new route
            self.plan_ack(route[0], [{"ok": True, "type": "routed", "shipment": e["shipment"]}], record=False)
        lead_days, _ = self.twin.lead_stats_days(dc, sku)
        now = self.env.now
        qty = float(e["qty"])
        n = max(1, math.ceil(qty / TRUCK_CAP.get(sku, 1000)))
        msgs = []
        for i in range(n):
            q = round(qty / n, 1) if i < n - 1 else round(qty - round(qty / n, 1) * (n - 1), 1)
            if q <= 0:
                continue
            eta = now + lead_days * 24 * float(self.rng.uniform(0.95, 1.05))
            msgs.append(self.factory.make("asn", f"asn:{e['source']}", self.ts(now), {
                "asn_id": f"ASN-{e['shipment']}-{i + 1}", "supplier": e["source"], "dc": dc, "sku": sku, "qty": q,
                "ship_ts": self.ts(now), "eta_ts": self.ts(eta), "lanes": list(e["lanes"])}))
        self.emit(msgs)

    def _attack_clock(self):
        while True:
            if self.live():
                if self.chaos_poll is not None:
                    for cmd in self.chaos_poll():
                        try:
                            dur = cmd.get("duration_s")
                            self.injector.inject_now(cmd["type"], self.env.now, cmd.get("target"), cmd.get("params"),
                                                     None if dur is None else dur / 3600.0, cmd.get("id"))
                        except (ValueError, KeyError):
                            pass
                self.injector.tick(self.env.now)
                if self.plan_poll is not None:
                    for cmd in self.plan_poll():
                        self.dispatch_plan(cmd)
            yield self.env.timeout(10 / 3600.0)

    def dispatch_plan(self, cmd: dict) -> list[dict]:
        """An approved plan reaches the real fleet: its new routes and transfers are executed in reality, so city
        legs (transfers through Hyderabad, reroutes via its DCs) spawn SUMO trucks on the new corridors."""
        acks = []
        for ev in cmd.get("events", []):
            try:
                acks.append(self.twin.apply(ev))
                if ev.get("type") == "set_route":
                    self.plan_routes[(ev["dc"], ev["sku"])] = (cmd.get("id"), tuple(ev["lanes"]))
                elif ev.get("type") == "set_path":
                    self.plan_routes.pop((ev["dc"], ev["sku"]), None)
            except (ValueError, KeyError) as e:  # reality moved on (a donor ran dry, a DC closed): skip that one
                acks.append({"ok": False, "type": ev.get("type"), "error": str(e)})
        self.twin.log("plan_dispatched", plan=cmd.get("id"), events=len(acks), ok=sum(1 for a in acks if a.get("ok")))
        if self.plan_ack is not None:
            self.plan_ack(cmd.get("id"), acks)
        return acks

    # ------------------------------------------------------------------ run / truth
    def run_until(self, t_h: float) -> None:
        self.twin.run_until(t_h)
        self.injector.tick(self.env.now)

    def close(self) -> None:
        self.injector.finalize(self.env.now)
        self.sink.close()

    def truth(self) -> dict:
        return {"seed": self.seed, "start": self.start.isoformat(), "t_h": round(self.env.now, 4),
                "window": {"from_ts": self.ts(self.telemetry_from_h), "to_ts": self.ts(self.env.now)},
                "perturbations": self.truth_log, "attacks": self.injector.labels(self.start),
                "counts": self.counts, "hidden_effects": [e.summary() for e in self.twin.effect_history if e.kind == "hidden"]}

    def config(self) -> dict:
        return {"gps_period_s": self.gps_period_s, "national_gps_period_s": self.national_gps_period_s,
                "gps_noise_m": self.gps_noise_m, "telemetry_from_h": self.telemetry_from_h, "multipath_p": self.multipath_p,
                "stock_period_s": self.stock_period_s, "port_period_s": self.port_period_s,
                "micro": self.coupled is not None}
