"""In-process SUMO micro-twin (libsumo; TraCI + sumo-gui when gui=True for debugging).

libsumo allows one simulation per OS process, so the coupled twin and the Reality Emulator each run
this class inside their own worker process (sim/micro/process.py). The API:
    spawn_truck(vid, from_hub, to_hub, depart=None, meta=None)  -> vehicle.add on a findRoute path, parks at the hub
    close_road(edges) / reopen_road(edges)                      -> lanes disallowed + travel-time penalty, reroute
    reroute_all()                                               -> vehicle.rerouteTraveltime for every truck
    step(n)                                                     -> advance, collect truck arrivals at their hub
    positions(ids)                                              -> FCD-like rows: id, lon, lat, speed, angle, edge
    corridor_time(a, b)                                         -> current fastest truck travel time between hubs
    edge_stats(top)                                             -> observed per-edge truck travel times (Welford)
    edge_travel_times(edges)                                    -> current SUMO travel-time estimate per edge
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from sim.paths import HYDERABAD

CFG = HYDERABAD / "hyderabad.sumocfg"
NET = HYDERABAD / "hyderabad.net.xml.gz"


def _sumo_home() -> str:
    import sumo
    os.environ.setdefault("SUMO_HOME", sumo.SUMO_HOME)
    return sumo.SUMO_HOME


@dataclass
class Arrival:
    vehicle: str
    t: float
    depart: float
    from_hub: str
    to_hub: str
    meta: dict = field(default_factory=dict)

    @property
    def travel_s(self) -> float:
        return self.t - self.depart

    def to_dict(self) -> dict:
        return {**asdict(self), "travel_s": round(self.travel_s, 1)}


@dataclass
class EdgeStat:
    n: int = 0
    mean: float = 0.0
    m2: float = 0.0

    def add(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self.m2 += d * (x - self.mean)

    @property
    def std(self) -> float:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0


class MicroTwin:
    def __init__(self, cfg: Path | None = CFG, gui: bool = False, seed: int = 42, extra: list[str] | None = None,
                 track_edges: bool = True):
        home = _sumo_home()
        if gui:
            import traci as sumo_api  # socket-based; sumo-gui for visual debugging
        else:
            import libsumo as sumo_api  # in-process, much faster
        self.t = sumo_api
        self.gui = gui
        if cfg is not None:
            args = ["-c", str(cfg)]
        else:  # bare network: no background traffic or demo trucks (benchmarks, fast tests)
            args = ["-n", str(NET), "-a", f"{HYDERABAD / 'vtypes.add.xml'},{HYDERABAD / 'parking.add.xml'}",
                    "--step-length", "1", "--time-to-teleport", "300", "--no-step-log", "true", "--no-warnings", "true"]
        binary = os.path.join(home, "bin", "sumo-gui" if gui else "sumo")
        self.t.start([binary, *args, "--seed", str(seed), *(extra or [])])
        self.hubs: dict[str, dict] = json.loads((HYDERABAD / "hubs.json").read_text())
        self.trucks: dict[str, tuple[str, str, float]] = {}  # id -> (from, to, depart) for trucks still driving
        self.meta: dict[str, dict] = {}
        self.arrivals: list[Arrival] = []
        self.closed: dict[str, list[tuple[str, list[str]]]] = {}
        self.track_edges = track_edges
        self._on_edge: dict[str, tuple[str, float]] = {}  # vid -> (edge, entered at)
        self.edge_obs: dict[str, EdgeStat] = {}

    # ---------------------------------------------------------------- commands
    def spawn_truck(self, vid: str, from_hub: str, to_hub: str, park_s: float = 600, depart: float | None = None,
                    meta: dict | None = None) -> bool:
        """Add a truck on the fastest current route between two hubs. depart: absolute SUMO time
        (>= now; default now). Returns False if the hubs are not connected for trucks."""
        a, b = self.hubs[from_hub]["edge"], self.hubs[to_hub]["edge"]
        route = self.t.simulation.findRoute(a, b, vType="truck")
        if not route.edges:
            return False
        rid = f"r_{vid}"
        self.t.route.add(rid, list(route.edges))
        dep = "now" if depart is None or depart <= self.now() else f"{depart:.1f}"
        self.t.vehicle.add(vid, rid, typeID="truck", depart=dep, departLane="best", departSpeed="max")
        try:
            self.t.vehicle.setParkingAreaStop(vid, self.hubs[to_hub]["parking_area"], duration=park_s)
        except self.t.TraCIException:
            pass  # parking area not on the final edge's reachable lane; the truck simply arrives
        self.trucks[vid] = (from_hub, to_hub, self.now() if depart is None else max(depart, self.now()))
        self.meta[vid] = meta or {}
        return True

    def add_vehicle(self, vid: str, edges: list[str], vtype: str = "car", depart: float | str = "now") -> None:
        rid = f"r_{vid}"
        self.t.route.add(rid, edges)
        self.t.vehicle.add(vid, rid, typeID=vtype, depart=str(depart), departLane="best", departSpeed="max")

    def close_road(self, edges: list[str], reroute: bool = True) -> int:
        for e in edges:
            if e in self.closed:
                continue
            lanes = []
            for i in range(self.t.edge.getLaneNumber(e)):
                lid = f"{e}_{i}"
                lanes.append((lid, list(self.t.lane.getAllowed(lid))))
                self.t.lane.setDisallowed(lid, ["all"])
            self.closed[e] = lanes
            self.t.edge.adaptTraveltime(e, 1e7)
        return self.reroute_all() if reroute else 0

    def reopen_road(self, edges: list[str]) -> int:
        for e in edges:
            for lid, allowed in self.closed.pop(e, []):
                self.t.lane.setAllowed(lid, allowed)
            # back to the free-flow estimate (routing then follows live speeds again)
            self.t.edge.adaptTraveltime(e, self.t.lane.getLength(f"{e}_0") / self.t.lane.getMaxSpeed(f"{e}_0"))
        return self.reroute_all()

    def reroute_all(self) -> int:
        n = 0
        live = set(self.t.vehicle.getIDList())
        for vid in self.trucks:
            if vid in live:
                try:
                    self.t.vehicle.rerouteTraveltime(vid)
                    n += 1
                except self.t.TraCIException:
                    pass
        return n

    # ---------------------------------------------------------------- stepping / outputs
    def now(self) -> float:
        return self.t.simulation.getTime()

    def _arrive(self, vid: str) -> Arrival | None:
        info = self.trucks.pop(vid, None)
        self._on_edge.pop(vid, None)
        if info is None:
            return None
        a, b, dep = info
        arr = Arrival(vid, self.now(), dep, a, b, self.meta.pop(vid, {}))
        self.arrivals.append(arr)
        return arr

    def step(self, n: int = 1) -> list[Arrival]:
        """Advance n seconds. A truck counts as arrived when it pulls into its destination parking area
        (or leaves the network, if it had no parking stop)."""
        new = []
        for _ in range(n):
            self.t.simulationStep()
            now = self.now()
            for vid in self.t.simulation.getParkingStartingVehiclesIDList():
                if vid in self.trucks:
                    arr = self._arrive(vid)
                    if arr:
                        new.append(arr)
            for vid in self.t.simulation.getArrivedIDList():
                if vid in self.trucks:
                    arr = self._arrive(vid)
                    if arr:
                        new.append(arr)
            if self.track_edges and self.trucks:
                for vid in list(self.trucks):
                    try:
                        e = self.t.vehicle.getRoadID(vid)
                    except self.t.TraCIException:
                        continue  # not inserted yet
                    if not e or e.startswith(":"):
                        continue
                    prev = self._on_edge.get(vid)
                    if prev is None or prev[0] != e:
                        if prev is not None:
                            self.edge_obs.setdefault(prev[0], EdgeStat()).add(now - prev[1])
                        self._on_edge[vid] = (e, now)
        return new

    def step_to(self, t: float) -> list[Arrival]:
        n = int(round(t - self.now()))
        return self.step(n) if n > 0 else []

    def positions(self, ids: list[str] | None = None) -> list[dict]:
        rows = []
        for vid in ids if ids is not None else self.t.vehicle.getIDList():
            try:
                x, y = self.t.vehicle.getPosition(vid)
            except self.t.TraCIException:
                continue
            lon, lat = self.t.simulation.convertGeo(x, y)
            rows.append({"id": vid, "lon": round(lon, 6), "lat": round(lat, 6),
                         "speed": round(self.t.vehicle.getSpeed(vid), 2), "angle": round(self.t.vehicle.getAngle(vid), 1),
                         "edge": self.t.vehicle.getRoadID(vid)})
        return rows

    def truck_positions(self) -> list[dict]:
        live = set(self.t.vehicle.getIDList())
        rows = self.positions([v for v in self.trucks if v in live])
        for r in rows:
            r.update(self.meta.get(r["id"], {}))
        return rows

    def street(self, edge: str) -> str:
        try:
            return self.t.edge.getStreetName(edge) or edge
        except self.t.TraCIException:
            return edge

    def corridor_time(self, from_hub: str, to_hub: str) -> float:
        """Fastest truck route time (s) between two hubs under current conditions (inf if cut off)."""
        r = self.t.simulation.findRoute(self.hubs[from_hub]["edge"], self.hubs[to_hub]["edge"], vType="truck")
        return r.travelTime if r.edges else math.inf

    def corridor_route(self, from_hub: str, to_hub: str) -> list[str]:
        return list(self.t.simulation.findRoute(self.hubs[from_hub]["edge"], self.hubs[to_hub]["edge"], vType="truck").edges)

    def edge_travel_times(self, edges: list[str]) -> dict[str, float]:
        return {e: self.t.edge.getTraveltime(e) for e in edges}

    def edge_stats(self, top: int = 50, edges: list[str] | None = None) -> list[dict]:
        items = [(e, s) for e, s in self.edge_obs.items() if edges is None or e in edges]
        items.sort(key=lambda kv: -kv[1].n)
        return [{"edge": e, "n": s.n, "mean_s": round(s.mean, 2), "std_s": round(s.std, 2),
                 "free_flow_s": round(self.t.lane.getLength(f"{e}_0") / max(self.t.lane.getMaxSpeed(f"{e}_0"), 0.1), 2)}
                for e, s in items[:top]]

    def active_trucks(self) -> list[str]:
        return [v for v in self.t.vehicle.getIDList() if self.t.vehicle.getTypeID(v) == "truck"]

    def close(self) -> None:
        self.t.close()
