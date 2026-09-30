"""In-memory live state of the observed world (from clean telemetry), with dirty tracking for WS diffs.

The twin-state service writes here (and mirrors to Redis hashes for other processes); the WebSocket
broadcaster reads the dirty sets at 5-10 Hz and turns them into msgpack diffs.
"""
from __future__ import annotations

import itertools
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime

SCOPE_CODE = {"national": 0, "city": 1}
STATUS_CODE = {"live": 0, "predicted": 1}


@dataclass
class Alert:
    id: str
    sev: str            # ok | warn | bad | ai | good | sec
    t: str              # simulated-world time (ISO)
    title: str
    body: str
    node: str | None = None
    kind: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "sev": self.sev, "t": self.t, "title": self.title, "body": self.body, "node": self.node,
                "kind": self.kind}


@dataclass
class LiveState:
    vehicles: dict[str, dict] = field(default_factory=dict)
    inventory: dict[tuple[str, str], dict] = field(default_factory=dict)
    ports: dict[str, dict] = field(default_factory=dict)
    asns: deque = field(default_factory=lambda: deque(maxlen=500))
    sources: dict[str, dict] = field(default_factory=dict)
    alerts: deque = field(default_factory=lambda: deque(maxlen=300))
    quarantine: deque = field(default_factory=lambda: deque(maxlen=500))
    dirty_vehicles: set = field(default_factory=set)
    removed_vehicles: set = field(default_factory=set)
    dirty_inventory: set = field(default_factory=set)
    dirty_ports: set = field(default_factory=set)
    new_alerts: list = field(default_factory=list)
    counters: Counter = field(default_factory=Counter)
    reject_reasons: Counter = field(default_factory=Counter)
    world_now: datetime | None = None          # latest telemetry timestamp = the observed world's clock
    rate: deque = field(default_factory=lambda: deque(maxlen=120))  # (wall s, accepted total) samples
    _alert_ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _alert_last: dict = field(default_factory=dict)

    # ------------------------------------------------------------------ updates
    def upsert_vehicle(self, vid: str, row: dict) -> None:
        self.vehicles[vid] = row
        self.dirty_vehicles.add(vid)
        self.removed_vehicles.discard(vid)

    def remove_vehicle(self, vid: str) -> None:
        if self.vehicles.pop(vid, None) is not None:
            self.removed_vehicles.add(vid)
            self.dirty_vehicles.discard(vid)

    def alert(self, sev: str, title: str, body: str, node: str | None = None, kind: str = "", key: str | None = None,
              min_gap_s: float = 20.0) -> Alert | None:
        """Raise an alert; alerts with the same key are rate-limited (wall-clock) so a flood doesn't drown the feed."""
        now = time.monotonic()
        if key is not None:
            last = self._alert_last.get(key)
            if last is not None and now - last < min_gap_s:
                return None
            self._alert_last[key] = now
        a = Alert(f"AL{next(self._alert_ids):06d}", sev, self.world_now.isoformat(timespec="seconds") if self.world_now else "",
                  title, body, node, kind)
        self.alerts.appendleft(a)
        self.new_alerts.append(a)
        return a

    def sample_rate(self) -> float:
        now = time.monotonic()
        self.rate.append((now, self.counters["ingest_accepted"]))
        while len(self.rate) > 2 and now - self.rate[0][0] > 10:
            self.rate.popleft()
        if len(self.rate) < 2:
            return 0.0
        (t0, a0), (t1, a1) = self.rate[0], self.rate[-1]
        return (a1 - a0) / (t1 - t0) if t1 > t0 else 0.0

    # ------------------------------------------------------------------ views
    @staticmethod
    def vehicle_row(vid: str, v: dict) -> list:
        """Compact wire row: [id, lon, lat, speed_kmh, heading, t_ms, scope, status, shipment]."""
        return [vid, round(v["lon"], 6), round(v["lat"], 6), round(v["speed"], 1), round(v["heading"], 1), v["t_ms"],
                SCOPE_CODE.get(v.get("scope"), 0), STATUS_CODE.get(v.get("status", "live"), 0), v.get("shipment")]

    def inventory_view(self) -> dict[str, dict]:
        return {f"{n}/{s}": v for (n, s), v in self.inventory.items()}

    def take_dirty(self) -> tuple[list, list, dict, dict, list]:
        veh = [self.vehicle_row(v, self.vehicles[v]) for v in self.dirty_vehicles if v in self.vehicles]
        removed = list(self.removed_vehicles)
        inv = {f"{n}/{s}": self.inventory[(n, s)] for (n, s) in self.dirty_inventory if (n, s) in self.inventory}
        ports = {p: self.ports[p] for p in self.dirty_ports if p in self.ports}
        alerts = [a.to_dict() for a in self.new_alerts]
        self.dirty_vehicles.clear()
        self.removed_vehicles.clear()
        self.dirty_inventory.clear()
        self.dirty_ports.clear()
        self.new_alerts.clear()
        return veh, removed, inv, ports, alerts
