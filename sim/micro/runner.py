"""In-process SUMO micro-twin (libsumo; TraCI + sumo-gui when gui=True for debugging).

The coupling orchestrator (Phase 3) drives this through a command queue:
    spawn_truck(shipment_id, from_hub, to_hub)   -> vehicle.add on a findRoute path, parks at the hub
    close_road(edges) / reopen_road(edges)       -> lanes disallowed + travel-time penalty, then reroute
    reroute_all()                                -> vehicle.rerouteTraveltime for every truck
    step(n)                                      -> advance, collect arrivals
    positions()                                  -> FCD-like rows: id, lon, lat, speed, angle, edge
    edge_travel_times(edges)                     -> current travel time per edge (calibration export)
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
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


class MicroTwin:
    def __init__(self, cfg: Path | None = CFG, gui: bool = False, seed: int = 42, extra: list[str] | None = None):
        home = _sumo_home()
        if gui:
            import traci as sumo_api  # socket-based; sumo-gui for visual debugging
        else:
            import libsumo as sumo_api  # in-process, much faster
        self.t = sumo_api
        self.gui = gui
        if cfg is not None:
            args = ["-c", str(cfg)]
        else:  # bare network (benchmarks spawn everything themselves)
            args = ["-n", str(NET), "-a", f"{HYDERABAD / 'vtypes.add.xml'},{HYDERABAD / 'parking.add.xml'}",
                    "--step-length", "1", "--time-to-teleport", "300", "--no-step-log", "true", "--no-warnings", "true"]
        binary = os.path.join(home, "bin", "sumo-gui" if gui else "sumo")
        self.t.start([binary, *args, "--seed", str(seed), *(extra or [])])
        self.hubs: dict[str, dict] = json.loads((HYDERABAD / "hubs.json").read_text())
        self.trucks: dict[str, tuple[str, str, float]] = {}  # id -> (from, to, depart)
        self.arrivals: list[Arrival] = []
        self.closed: dict[str, list[tuple[str, list[str]]]] = {}

    # ---------------------------------------------------------------- commands
    def spawn_truck(self, vid: str, from_hub: str, to_hub: str, park_s: float = 600) -> bool:
        a, b = self.hubs[from_hub]["edge"], self.hubs[to_hub]["edge"]
        route = self.t.simulation.findRoute(a, b, vType="truck")
        if not route.edges:
            return False
        rid = f"r_{vid}"
        self.t.route.add(rid, list(route.edges))
        self.t.vehicle.add(vid, rid, typeID="truck", departLane="best", departSpeed="max")
        try:
            self.t.vehicle.setParkingAreaStop(vid, self.hubs[to_hub]["parking_area"], duration=park_s)
        except self.t.TraCIException:
            pass  # parking area not on the final edge's reachable lane; truck just arrives
        self.trucks[vid] = (from_hub, to_hub, self.now())
        return True

    def add_vehicle(self, vid: str, edges: list[str], vtype: str = "car", depart: float | str = "now") -> None:
        rid = f"r_{vid}"
        self.t.route.add(rid, edges)
        self.t.vehicle.add(vid, rid, typeID=vtype, depart=str(depart), departLane="best", departSpeed="max")

    def close_road(self, edges: list[str], reroute: bool = True) -> None:
        for e in edges:
            lanes = []
            for i in range(self.t.edge.getLaneNumber(e)):
                lid = f"{e}_{i}"
                lanes.append((lid, list(self.t.lane.getAllowed(lid))))
                self.t.lane.setDisallowed(lid, ["all"])
            self.closed[e] = lanes
            self.t.edge.adaptTraveltime(e, 1e7)
        if reroute:
            self.reroute_all()

    def reopen_road(self, edges: list[str]) -> None:
        for e in edges:
            for lid, allowed in self.closed.pop(e, []):
                self.t.lane.setAllowed(lid, allowed)
            self.t.edge.adaptTraveltime(e, self.t.edge.getTraveltime(e))
        self.reroute_all()

    def reroute_all(self) -> int:
        n = 0
        for vid in self.t.vehicle.getIDList():
            if vid in self.trucks:
                try:
                    self.t.vehicle.rerouteTraveltime(vid)
                    n += 1
                except self.t.TraCIException:
                    pass
        return n

    # ---------------------------------------------------------------- stepping / outputs
    def now(self) -> float:
        return self.t.simulation.getTime()

    def step(self, n: int = 1) -> list[Arrival]:
        new = []
        for _ in range(n):
            self.t.simulationStep()
            for vid in self.t.simulation.getArrivedIDList():
                if vid in self.trucks:
                    a, b, dep = self.trucks[vid]
                    new.append(Arrival(vid, self.now(), dep, a, b))
        self.arrivals += new
        return new

    def positions(self, ids: list[str] | None = None) -> list[dict]:
        rows = []
        for vid in ids if ids is not None else self.t.vehicle.getIDList():
            x, y = self.t.vehicle.getPosition(vid)
            lon, lat = self.t.simulation.convertGeo(x, y)
            rows.append({"id": vid, "lon": round(lon, 6), "lat": round(lat, 6),
                         "speed": round(self.t.vehicle.getSpeed(vid), 2), "angle": round(self.t.vehicle.getAngle(vid), 1),
                         "edge": self.t.vehicle.getRoadID(vid)})
        return rows

    def street(self, edge: str) -> str:
        try:
            return self.t.edge.getStreetName(edge) or edge
        except self.t.TraCIException:
            return edge

    def edge_travel_times(self, edges: list[str]) -> dict[str, float]:
        return {e: self.t.edge.getTraveltime(e) for e in edges}

    def active_trucks(self) -> list[str]:
        return [v for v in self.t.vehicle.getIDList() if self.t.vehicle.getTypeID(v) == "truck"]

    def close(self) -> None:
        self.t.close()
