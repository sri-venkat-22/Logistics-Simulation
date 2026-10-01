"""Trust layers 5-9 (Phase 7): pure, Redis-free checks shared by the live trust stage and the offline benchmark.

    L5 OFFROAD          map-matching: a city fix must lie within ~150 m of a SUMO road edge (Hyderabad net,
                        sumolib); a national fix within ~12 km of a road corridor (lane chords + city gateways)
    L6 KALMAN_GATE      per-vehicle Kalman filter driven by the device's own speed + heading (dead reckoning)
                        with a slowly adapting velocity bias; the innovation's Mahalanobis distance is gated
                        (chi-square, 2 dof). Catches slow position drift that stays physically plausible
    L7 TWIN_ENVELOPE    twin oracle: (a) a GPS fix of a shipment with a known planned route (from its ASN) must
                        stay inside that route's corridor; (b) observed stock / port state outside the live twin's
                        envelope is a divergence (flagged + alerted, not quarantined: reality may have changed)
    L8 ASN_OUTLIER      feed anomaly: robust (MAD) z-score and IsolationForest on ASN features
       RECON_MISMATCH   cross-source reconciliation: a stock count that jumps by more than the inbound ASNs and
                        the twin's order-up-to level allow
    L9 LOW_REPUTATION   Beta reputation per source (content violations only); low trust -> quarantine
       (SLA_SILENT lives in pipeline.SlaMonitor)
Each layer returns (layer, code) to quarantine, or None. Flags (accepted, but marked) are appended to
TrustEngine.flags for the caller.
"""
from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any

import numpy as np

from sim.macro.network import Network
from sim.paths import DATA, HYDERABAD
from sim.scenarios.dsl import haversine_km

if TYPE_CHECKING:
    pass

TRUCK_CAP = {"SKU_VAX": 400, "SKU_FMCG": 1000, "SKU_ELEC": 1500}
ROAD_CELLS = HYDERABAD / "road_cells.npz"
CITY_CELL = 0.001      # degrees (~110 m); a fix is on-road if a road sample lies in its 3x3 neighbourhood
NAT_CELL = 0.05        # degrees (~5.5 km)
NAT_SAMPLE_KM = 1.0
M_PER_DEG = 111_320.0


# ============================================================================================ L5 map-matching
def _build_city_cells(step_m: float = 20.0) -> np.ndarray:
    """Sample every road edge of the Hyderabad SUMO net every step_m and return the occupied CITY_CELL grid cells."""
    import sumolib
    net = sumolib.net.readNet(str(HYDERABAD / "hyderabad.net.xml.gz"), withInternal=False)
    cells: set[tuple[int, int]] = set()
    for e in net.getEdges():
        shape = e.getShape()
        for (x0, y0), (x1, y1) in zip(shape, shape[1:]):
            n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step_m))
            for i in range(n + 1):
                lon, lat = net.convertXY2LonLat(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n)
                cells.add((math.floor(lat / CITY_CELL), math.floor(lon / CITY_CELL)))
    return np.array(sorted(cells), dtype=np.int32)


def load_city_cells(build: bool = True) -> set[tuple[int, int]] | None:
    if not ROAD_CELLS.exists():
        if not build or not (HYDERABAD / "hyderabad.net.xml.gz").exists():
            return None
        np.savez_compressed(ROAD_CELLS, cells=_build_city_cells())
    arr = np.load(ROAD_CELLS)["cells"]
    return set(map(tuple, arr.tolist()))


class RoadIndex:
    """L5. City scope: SUMO road cells. National scope: road-lane chords, plus chords between each outside
    node and every Hyderabad hub (the coupled twin's city gateways)."""

    def __init__(self, net: Network, city_cells: set | None = None, hubs: dict | None = None):
        self.city = city_cells if city_cells is not None else load_city_cells()
        hubs = hubs if hubs is not None else json.loads((HYDERABAD / "hubs.json").read_text())
        self.nat: set[tuple[int, int]] = set()
        hub_ll = [(h["lat"], h["lon"]) for h in hubs.values()]
        inside = {n for n in net.nodes if n.startswith("DC_HYD") or n in ("PLANT_PATANCHERU", "Z_HYD")}
        for l in net.lanes.values():
            if l.mode != "road":
                continue
            a, b = net.nodes[l.from_id], net.nodes[l.to_id]
            self._chord(a.lat, a.lon, b.lat, b.lon)
            for out, inn in ((a, b.id), (b, a.id)):
                if inn in inside and out.id not in inside:
                    for hl in hub_ll:
                        self._chord(out.lat, out.lon, *hl)
        for hl in hub_ll:
            self.nat.add((math.floor(hl[0] / NAT_CELL), math.floor(hl[1] / NAT_CELL)))

    def _chord(self, lat0, lon0, lat1, lon1) -> None:
        n = max(1, int(haversine_km(lat0, lon0, lat1, lon1) / NAT_SAMPLE_KM))
        for i in range(n + 1):
            f = i / n
            self.nat.add((math.floor((lat0 + (lat1 - lat0) * f) / NAT_CELL), math.floor((lon0 + (lon1 - lon0) * f) / NAT_CELL)))

    @staticmethod
    def _near(cells: set, lat: float, lon: float, size: float, r: int = 1) -> bool:
        i, j = math.floor(lat / size), math.floor(lon / size)
        return any((i + di, j + dj) in cells for di in range(-r, r + 1) for dj in range(-r, r + 1))

    def on_road(self, lat: float, lon: float, scope: str) -> bool:
        if scope == "city":
            return True if self.city is None else self._near(self.city, lat, lon, CITY_CELL)
        return self._near(self.nat, lat, lon, NAT_CELL)

    def check(self, p: dict) -> tuple[str, str] | None:
        return None if self.on_road(p["lat"], p["lon"], p.get("scope", "national")) else ("L5", "OFFROAD")


# ============================================================================================ L6 Kalman gate
GATE_D2 = 18.42          # chi-square 2 dof, p = 0.9999
GATE_STREAK = 2          # consecutive gated fixes before a verdict (a single multipath jump is tolerated)
REANCHOR_AFTER = 720     # coast at most this many gated fixes (1 h at 5 s); a real relocation also resets via a gap


@dataclass
class _Track:
    lat0: float
    lon0: float
    x: float = 0.0           # position (m, local east / north of lat0, lon0)
    y: float = 0.0
    bx: float = 0.0          # velocity bias (m/s): observed motion minus the device's own speed + heading
    by: float = 0.0
    p00: float = 25.0        # covariance of [position, bias], shared by both axes (isotropic noise)
    p01: float = 0.0
    p11: float = 9.0
    vx: float = 0.0          # last reported velocity (m/s)
    vy: float = 0.0
    ux: float = 0.0          # cumulative dead-reckoning displacement since (re)initialisation (m)
    uy: float = 0.0
    t: datetime | None = None
    coast_from: datetime | None = None
    speed: float = 0.0
    streak: int = 0
    gated: int = 0
    odd: int = 0             # consecutive fixes with an implausible bias
    n: int = 0               # fixes fused since the bias was last (re)initialised
    hist: deque = field(default_factory=lambda: deque(maxlen=64))  # fused states for roll-back


class KalmanGate:
    """Per vehicle and axis, a 2-state Kalman filter over [position, velocity bias], driven by dead reckoning
    from the device's own speed and heading (trapezoidal between fixes). The bias absorbs steady, honest
    mismatches (road distance vs the straight line a truck is interpolated along); it is learned during a
    warm-up and then held almost constant, so a drift that *starts* mid-segment accumulates in the innovation
    until its Mahalanobis distance fails the chi-square gate. On a verdict the filter rolls back to its state
    `rollback_s` earlier (before the drift could leak into the bias) and coasts from there on dead reckoning;
    gated fixes are never fused, and honest fixes that match the coasted track are accepted again.
    R = (sigma * hdop)^2."""

    def __init__(self, sigma_m: float = 5.0, q_pos: float = 0.3, q_bias: float = 1e-6, gap_s: float = 120.0,
                 warmup_s: float = 60.0, bias0: float = 3.0, rollback_s: float = 300.0, q_coast: float = 1e-4,
                 coast_max_s: float = 120.0):
        self.sigma, self.q_pos, self.q_bias, self.gap_s = sigma_m, q_pos, q_bias, gap_s
        self.coast_max_s = coast_max_s
        self.warmup_s, self.bias0, self.rollback_s, self.q_coast = warmup_s, bias0, rollback_s, q_coast
        self.tracks: dict[str, _Track] = {}

    def _xy(self, tr: _Track, lat: float, lon: float) -> tuple[float, float]:
        return (lon - tr.lon0) * M_PER_DEG * math.cos(math.radians(tr.lat0)), (lat - tr.lat0) * M_PER_DEG

    def reset(self, vid: str) -> None:
        self.tracks.pop(vid, None)

    def _init(self, vid: str, p: dict, ts: datetime, vx: float, vy: float, speed: float) -> None:
        self.tracks[vid] = _Track(p["lat"], p["lon"], p00=(self.sigma * max(p.get("hdop", 1.0), 1.0)) ** 2,
                                  p11=self.bias0 ** 2, vx=vx, vy=vy, t=ts, speed=speed)

    def _rollback(self, tr: _Track, now: datetime) -> None:
        """Restore the fused state from about rollback_s ago and coast it forward to now on dead reckoning."""
        old = None
        for st in tr.hist:
            if (now - st[0]).total_seconds() <= self.rollback_s:
                break
            old = st
        old = old or (tr.hist[0] if tr.hist else None)
        if old is None:
            return
        t0, x, y, bx, by, p00, p01, p11, ux, uy = old
        d = (now - t0).total_seconds()
        tr.x, tr.y = x + bx * d + (tr.ux - ux), y + by * d + (tr.uy - uy)
        tr.bx, tr.by = bx, by
        tr.p00 = p00 + 2 * d * p01 + d * d * p11 + self.q_pos * d
        tr.p01, tr.p11 = p01 + d * p11, p11
        tr.hist.clear()

    def check(self, p: dict, ts: datetime) -> tuple[str, str] | None:
        vid = p["vehicle_id"]
        tr = self.tracks.get(vid)
        speed = p["speed_kmh"] / 3.6
        hd = math.radians(p["heading_deg"])
        vx, vy = speed * math.sin(hd), speed * math.cos(hd)
        if tr is None or tr.t is None or (ts - tr.t).total_seconds() > self.gap_s or tr.gated >= REANCHOR_AFTER:
            self._init(vid, p, ts, vx, vy, speed)
            return None
        dt = (ts - tr.t).total_seconds()
        if dt <= 0:
            return None
        if p.get("scope") == "national" and tr.speed > 1 and abs(speed - tr.speed) / tr.speed > 0.2:
            # a national truck's reported speed only changes on a new segment: its geometry bias is unknown again
            tr.p11, tr.p01, tr.n = self.bias0 ** 2, 0.0, 0
            tr.hist.clear()
        ux, uy = (tr.vx + vx) / 2 * dt, (tr.vy + vy) / 2 * dt
        tr.ux, tr.uy = tr.ux + ux, tr.uy + uy
        # predict  [p, b] <- [p + b dt + u, b]
        coast = self.q_coast if tr.streak else self.q_bias
        px, py = tr.x + tr.bx * dt + ux, tr.y + tr.by * dt + uy
        p00 = tr.p00 + 2 * dt * tr.p01 + dt * dt * tr.p11 + self.q_pos * dt
        p01 = tr.p01 + dt * tr.p11
        p11 = tr.p11 + coast * dt
        zx, zy = self._xy(tr, p["lat"], p["lon"])
        r = (self.sigma * max(p.get("hdop", 1.0), 1.0)) ** 2
        s = p00 + r
        nx_, ny_ = zx - px, zy - py
        d2 = (nx_ * nx_ + ny_ * ny_) / s
        tr.t, tr.speed, tr.vx, tr.vy = ts, speed, vx, vy
        if tr.n * dt >= self.warmup_s and d2 > GATE_D2:
            if tr.odd >= GATE_STREAK:  # the track itself is known-bad (drift absorbed into its bias): re-anchor here
                self._init(vid, p, ts, vx, vy, speed)
                return None
            if tr.streak and tr.coast_from is not None and (ts - tr.coast_from).total_seconds() > self.coast_max_s:
                self._init(vid, p, ts, vx, vy, speed)  # coasted long enough: follow the fixes again (warm-up re-learns)
                return None
            if not tr.streak:
                tr.coast_from = ts
            tr.x, tr.y, tr.p00, tr.p01, tr.p11 = px, py, p00, p01, p11  # coast: keep the prediction, don't fuse
            tr.streak += 1
            tr.gated += 1
            if tr.streak == GATE_STREAK:
                self._rollback(tr, ts)
            return ("L6", "KALMAN_GATE") if tr.streak >= GATE_STREAK else None
        k0, k1 = p00 / s, p01 / s
        tr.x, tr.y = px + k0 * nx_, py + k0 * ny_
        tr.bx, tr.by = tr.bx + k1 * nx_, tr.by + k1 * ny_
        tr.p00, tr.p01, tr.p11 = (1 - k0) * p00, (1 - k0) * p01, p11 - k1 * p01
        tr.streak = tr.gated = 0
        tr.n += 1
        tr.hist.append((ts, tr.x, tr.y, tr.bx, tr.by, tr.p00, tr.p01, tr.p11, tr.ux, tr.uy))
        if tr.n * dt >= self.warmup_s and self.implausible(tr, p.get("scope", "national"), hd):
            tr.odd += 1
            return ("L6", "KALMAN_GATE") if tr.odd >= GATE_STREAK else None
        tr.odd = 0
        return None

    @staticmethod
    def implausible(tr: _Track, scope: str, hd: float) -> bool:
        """Is the learned bias a shape honest motion can't produce? Along the reported heading, a national
        truck's observed motion can only be slower than its odometer speed (road distance >= straight line),
        and hardly sideways; a city truck (SUMO geometry) has almost no bias at all. A drift that began as
        the track started is absorbed into the bias, but usually with a sideways or forward component.
        Thresholds sit outside the 99.9th percentile of clean benchmark traffic (docs/trust/TRUST.md)."""
        if tr.speed < 3:
            return False
        hx, hy = math.sin(hd), math.cos(hd)
        along, cross = (tr.bx * hx + tr.by * hy), (-tr.bx * hy + tr.by * hx)
        if scope == "national":
            return (along / tr.speed > 0.04 and along > 0.4) or along / tr.speed < -0.5 or \
                (abs(cross) / tr.speed > 0.08 and abs(cross) > 0.6)
        return (abs(along) / tr.speed > 0.15 and abs(along) > 0.6) or (abs(cross) / tr.speed > 0.15 and abs(cross) > 0.8)


# ============================================================================================ L7 twin oracle
class TwinOracle:
    """(a) route corridor: a national fix of a shipment whose planned lanes are known (from its ASN) must lie
    within corridor_km of those lanes' chords (or a city gateway). (b) divergence of observed stock / port state
    from the live twin (flags + alerts; see TrustEngine.divergence)."""

    def __init__(self, net: Network, hubs: dict | None = None, corridor_km: float = 25.0):
        self.net, self.corridor_km = net, corridor_km
        hubs = hubs if hubs is not None else json.loads((HYDERABAD / "hubs.json").read_text())
        self.hub_ll = [(h["lat"], h["lon"]) for h in hubs.values()]
        self.routes: dict[str, list[str]] = {}       # shipment id -> planned lanes
        self._corr: dict[str, set] = {}

    def learn_asn(self, p: dict) -> None:
        sid = p["asn_id"].removeprefix("ASN-").rsplit("-", 1)[0]
        if sid not in self.routes:
            self.routes[sid] = list(p["lanes"])
            if len(self.routes) > 20_000:
                self.routes.pop(next(iter(self.routes)))

    def _corridor(self, sid: str) -> set:
        c = self._corr.get(sid)
        if c is None:
            c = set()
            size = 0.1
            for lid in self.routes[sid]:
                lane = self.net.lanes.get(lid)
                if lane is None:
                    continue
                a, b = self.net.nodes[lane.from_id], self.net.nodes[lane.to_id]
                ends = [(a.lat, a.lon, b.lat, b.lon)] + [(a.lat, a.lon, *h) for h in self.hub_ll] + [(*h, b.lat, b.lon) for h in self.hub_ll]
                for la0, lo0, la1, lo1 in ends:
                    n = max(1, int(haversine_km(la0, lo0, la1, lo1) / 2))
                    for i in range(n + 1):
                        f = i / n
                        c.add((math.floor((la0 + (la1 - la0) * f) / size), math.floor((lo0 + (lo1 - lo0) * f) / size)))
            self._corr[sid] = c
            if len(self._corr) > 5000:
                self._corr.pop(next(iter(self._corr)))
        return c

    def check_gps(self, p: dict) -> tuple[str, str] | None:
        sid = p.get("shipment_id")
        if p.get("scope") != "national" or not sid or sid not in self.routes:
            return None
        return None if RoadIndex._near(self._corridor(sid), p["lat"], p["lon"], 0.1, r=2) else ("L7", "TWIN_ENVELOPE")


# ============================================================================================ L8 feed anomaly
def asn_features(p: dict, daily: float | None = None) -> list[float]:
    """[log qty / truck capacity, planned lead time (h), lanes, log qty / daily demand of the dc x sku]."""
    cap = TRUCK_CAP.get(p["sku"], 1000)
    try:
        lead_h = (datetime.fromisoformat(p["eta_ts"]) - datetime.fromisoformat(p["ship_ts"])).total_seconds() / 3600
    except ValueError:
        lead_h = -1.0
    return [math.log(max(p["qty"], 1e-6) / cap), lead_h, float(len(p["lanes"])), math.log(max(p["qty"], 1e-6) / max(daily or cap, 1.0))]


class FeedAnomaly:
    """ASN_OUTLIER: more than a truck-load on one ASN (one note per truck), an upper-tail robust z-score
    (x - median) / (1.4826 MAD) > z_max on log(qty / truck capacity), an ETA before the ship time, or an
    IsolationForest outlier (on asn_features, fitted on clean training ASNs) with z > 3. RECON_MISMATCH: a stock count that
    rises by more than (ASN quantity due + 1.5 x the order-up-to level), to above 2 x that level."""

    def __init__(self, train: list[list[float]] | None = None, z_max: float = 6.0, seed: int = 0,
                 levels: Callable[[str, str], tuple[float, float] | None] | None = None):
        self.z_max = z_max
        self.levels = levels
        self.iforest = None
        train = train if train is not None else load_asn_training()
        x = np.array([t[0] for t in train]) if train else np.array([0.0, -0.1, -0.3])
        self.med = float(np.median(x))
        self.mad = max(float(np.median(np.abs(x - self.med))) * 1.4826, 0.05)
        if train and len(train) >= 30:
            from sklearn.ensemble import IsolationForest
            self.iforest = IsolationForest(n_estimators=100, contamination=0.005, random_state=seed).fit(np.array(train))
        self.due: dict[tuple[str, str], deque] = defaultdict(deque)   # (dc, sku) -> (eta, qty)
        self.last_stock: dict[tuple[str, str], float] = {}

    def z(self, p: dict) -> float:
        """Upper-tail robust z-score of log(qty / truck capacity): partial loads (low side) are normal."""
        return (asn_features(p)[0] - self.med) / self.mad

    def check_asn(self, p: dict) -> tuple[str, str] | None:
        f = asn_features(p)
        z = (f[0] - self.med) / self.mad
        # one consignment note per truck: more than a truck-load on one ASN is physically impossible
        if f[0] > math.log(1.05) or z > self.z_max or f[1] <= 0:
            return "L8", "ASN_OUTLIER"
        if self.iforest is not None and z > 3 and self.iforest.predict(np.array([f]))[0] == -1:
            return "L8", "ASN_OUTLIER"
        return None

    def accept_asn(self, p: dict) -> None:
        try:
            eta = datetime.fromisoformat(p["eta_ts"])
        except ValueError:
            return
        q = self.due[(p["dc"], p["sku"])]
        q.append((eta, p["qty"]))
        while len(q) > 500:
            q.popleft()

    def check_stock(self, p: dict, ts: datetime) -> tuple[str, str] | None:
        key = (p["node"], p["sku"])
        prev = self.last_stock.get(key)
        lv = self.levels(*key) if self.levels else None
        if prev is None or lv is None:
            return None
        S = max(lv[1], 1.0)
        due = sum(q for eta, q in self.due.get(key, ()) if abs((eta - ts).total_seconds()) < 36 * 3600)
        if p["on_hand"] - prev > due + 1.5 * S and p["on_hand"] > 2 * S:
            return "L8", "RECON_MISMATCH"
        return None

    def accept_stock(self, p: dict) -> None:
        self.last_stock[(p["node"], p["sku"])] = p["on_hand"]


ASN_TRAIN = DATA / "benchmark" / "asn_train.json"


def load_asn_training() -> list[list[float]]:
    """Clean ASN feature rows for L8 (written by python -m services.api.app.trust_bench --train-asn)."""
    return json.loads(ASN_TRAIN.read_text())["rows"] if ASN_TRAIN.exists() else []


# ============================================================================================ L9 reputation
@dataclass
class _Rep:
    a: float = 0.0
    b: float = 0.0
    t: datetime | None = None


class Reputation:
    """Beta reputation (Josang): trust = (a + 1) / (a + b + 2), where a and b count good messages and content
    violations with time-based forgetting (both decay by exp(-dt / tau), tau = 30 min of world time), so trust
    reflects roughly the last hour whatever a feed's message rate. Only content violations (L1, L4-L8) count,
    at most one per `penalty_gap_s`, so "persistently bad" means bad over time rather than one burst; forged
    signatures (L2) and replays / duplicates (L3) can't be attributed to the device. A source below `floor`
    with b >= min_bad is quarantined (LOW_REPUTATION); its messages are still content-checked, and clean
    ones lift it back above the floor."""

    PENALISE = {"L1", "L4", "L5", "L6", "L7", "L8"}

    def __init__(self, tau_s: float = 1800.0, floor: float = 0.4, min_bad: float = 5.0, penalty_gap_s: float = 60.0):
        self.tau, self.floor, self.min_bad, self.gap = tau_s, floor, min_bad, penalty_gap_s
        self.s: dict[str, _Rep] = defaultdict(_Rep)
        self.last_penalty: dict[str, datetime] = {}

    def _age(self, r: _Rep, ts: datetime | None) -> None:
        if ts is None:
            return
        if r.t is not None and ts > r.t:
            f = math.exp(-(ts - r.t).total_seconds() / self.tau)
            r.a *= f
            r.b *= f
        if r.t is None or ts > r.t:
            r.t = ts

    def trust(self, src: str) -> float:
        r = self.s.get(src)
        return 1.0 if r is None else (r.a + 1) / (r.a + r.b + 2)

    def check(self, src: str) -> tuple[str, str] | None:
        r = self.s.get(src)
        if r is not None and r.b >= self.min_bad and self.trust(src) < self.floor:
            return "L9", "LOW_REPUTATION"
        return None

    def good(self, src: str, ts: datetime | None = None) -> None:
        r = self.s[src]
        self._age(r, ts)
        r.a += 1

    def bad(self, src: str, layer: str, ts: datetime | None = None) -> None:
        if layer not in self.PENALISE:
            return
        last = self.last_penalty.get(src)
        if ts is not None and last is not None and abs((ts - last).total_seconds()) < self.gap:
            return
        if ts is not None:
            self.last_penalty[src] = ts
        r = self.s[src]
        self._age(r, ts)
        r.b += 1

    def table(self, limit: int = 50) -> list[dict]:
        rows: list[dict[str, Any]] = [{"source": k, "trust": round(self.trust(k), 3), "good": round(v.a, 1), "bad": round(v.b, 1)}
                                      for k, v in self.s.items()]
        rows.sort(key=lambda r: r["trust"])
        return rows[:limit]


# ============================================================================================ engine
@dataclass
class Divergence:
    node: str
    what: str
    observed: float | str
    twin: float | str
    ts: str


@dataclass
class TrustEngine:
    """Layers 5-9 in pipeline order for one message that already passed L1-L4. `levels(dc, sku)` gives the
    live twin's (reorder point, order-up-to); `twin_state()` gives {"inventory": {"DC/SKU": {...}}, "nodes": {...}}."""

    net: Network
    levels: Callable[[str, str], tuple[float, float] | None] | None = None
    twin_state: Callable[[], dict] | None = None
    twin_flows: Callable[[str, str, float], int] | None = None
    city_cells: set | None = None
    road: RoadIndex = field(init=False)
    kalman: KalmanGate = field(default_factory=KalmanGate)
    oracle: TwinOracle = field(init=False)
    feed: FeedAnomaly = field(init=False)
    rep: Reputation = field(default_factory=Reputation)
    divergences: deque = field(default_factory=lambda: deque(maxlen=500))
    counts: defaultdict = field(default_factory=lambda: defaultdict(int))
    _div_state: dict = field(default_factory=dict)
    flow_window_h: float = 8.0
    flow_min_twin: int = 4
    _flows: dict = field(default_factory=lambda: defaultdict(deque))   # (node, kind) -> observed event times
    _shipments_seen: set = field(default_factory=set)
    _dcs: list = field(default_factory=list)

    def __post_init__(self):
        self.road = RoadIndex(self.net, self.city_cells)
        self.oracle = TwinOracle(self.net)
        self.feed = FeedAnomaly(levels=self.levels)
        self._dcs = [(n.id, n.lat, n.lon) for n in self.net.of_type("dc")]

    def check(self, m: dict, ts: datetime) -> tuple[str, str] | None:
        kind, p, src = m["kind"], m["payload"], m["source_id"]
        low = self.rep.check(src)
        v = None
        if kind == "gps":
            v = self.road.check(p) or self.kalman.check(p, ts) or self.oracle.check_gps(p)
        elif kind == "asn":
            v = self.feed.check_asn(p)
        elif kind == "stock":
            v = self.feed.check_stock(p, ts)
        if v is None:
            self.rep.good(src, ts)  # a low-reputation source earns its way back with clean messages
            v = low
        else:
            self.rep.bad(src, v[0], ts)
        if v is not None:
            self.counts[v[1]] += 1
        return v

    def reject(self, m: dict, layer: str, ts: datetime | None = None) -> None:
        """A message quarantined by L1-L4: count it against the source's reputation where attributable."""
        if isinstance(m, dict) and isinstance(m.get("source_id"), str):
            self.rep.bad(m["source_id"], layer, ts)

    def accept(self, m: dict, ts: datetime) -> list[Divergence]:
        """Bookkeeping for an accepted message; returns new L7 divergences (stock / port vs the live twin)."""
        kind, p = m["kind"], m["payload"]
        if kind == "gps":
            self._note_departure(p, ts)
            return []
        if kind == "asn":
            self.oracle.learn_asn(p)
            self.feed.accept_asn(p)
            self._flows[(p["supplier"], "asn")].append(ts)
            return []
        if kind == "stock":
            self.feed.accept_stock(p)
            return self._stock_divergence(p, ts)
        if kind == "port":
            return self._port_divergence(p, ts)
        return []

    def _note_departure(self, p: dict, ts: datetime) -> None:
        """A shipment's first fix within 20 km of a DC is a departure from that DC (observed outbound flow)."""
        sid = p.get("shipment_id")
        if not sid or sid in self._shipments_seen:
            return
        self._shipments_seen.add(sid)
        if len(self._shipments_seen) > 100_000:
            self._shipments_seen.clear()
        for dc, la, lo in self._dcs:
            if haversine_km(la, lo, p["lat"], p["lon"]) < 20:
                self._flows[(dc, "depart")].append(ts)
                return

    def sweep(self, now: datetime) -> list[Divergence]:
        """L7 flow divergence, run periodically: the twin dispatched >= flow_min_twin deliveries from a DC (or
        sent ASNs from a supplier) in the last flow_window_h, reality none, although that flow has been observed
        before. A closed DC or failed supplier shows up here: it never reports its own status."""
        if self.twin_flows is None:
            return []
        out = []
        w = self.flow_window_h
        for (node, kind), q in list(self._flows.items()):
            while q and (now - q[0]).total_seconds() > 48 * 3600:
                q.popleft()
            if not q:
                continue
            silent = (now - q[-1]).total_seconds() > w * 3600
            need = self.flow_min_twin if kind == "depart" else 2
            if self._flag(("flow", node, kind), silent and self.twin_flows(node, kind, w) >= need, need=1):
                label = "outbound deliveries" if kind == "depart" else "ASNs"
                out.append(Divergence(node, label, 0, self.twin_flows(node, kind, w), now.isoformat(timespec="seconds")))
        self.divergences.extend(out)
        return out

    def _flag(self, key: tuple, bad: bool, need: int = 2) -> bool:
        """True once `need` consecutive reports disagree (and again only after an agreeing report)."""
        n = self._div_state.get(key, 0)
        if not bad:
            self._div_state[key] = 0
            return False
        self._div_state[key] = n + 1
        return n + 1 == need

    def _stock_divergence(self, p: dict, ts: datetime) -> list[Divergence]:
        if self.twin_state is None:
            return []
        tw = self.twin_state().get("inventory", {}).get(f"{p['node']}/{p['sku']}")
        lv = self.levels(p["node"], p["sku"]) if self.levels else None
        if not tw or lv is None:
            return []
        tol = max(0.5 * lv[1], 50.0)
        bl_twin = tw.get("backlog", 0.0)
        out = []
        if self._flag(("backlog", p["node"], p["sku"]), p["backlog"] > bl_twin + tol):
            out.append(Divergence(p["node"], f"backlog {p['sku']}", p["backlog"], bl_twin, ts.isoformat(timespec="seconds")))
        self.divergences.extend(out)
        return out

    def _port_divergence(self, p: dict, ts: datetime) -> list[Divergence]:
        if self.twin_state is None:
            return []
        tw = self.twin_state().get("nodes", {}).get(p["port"], {}).get("status", "up")
        out = []
        if self._flag(("port", p["port"]), p["status"] != tw and p["status"] == "closed", need=1):
            out.append(Divergence(p["port"], "status", p["status"], tw, ts.isoformat(timespec="seconds")))
        self.divergences.extend(out)
        return out


def iter_benchmark(path: Path) -> Iterator[dict]:
    with gzip.open(path, "rt") as f:
        for line in f:
            yield json.loads(line)
