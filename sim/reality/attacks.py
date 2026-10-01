"""Attack injector for the Reality Emulator - every injected attack carries a ground-truth label.

Types (expected trust layer / reason code from SRS Appendix B):
    gps_teleport     a truck's next 1-5 pings jump 300-800 km                       L4 PHYSICS_TELEPORT
    gps_drift        a truck's reported position drifts away at 0.5-3 m/s           L6 KALMAN_GATE
    replay           messages recorded 1-30 min earlier are re-sent verbatim        L3 REPLAY_NONCE
    duplicate        a source's next messages are sent twice (same msg_id)          L3 DUPLICATE_ID
    missing_fields   a required payload field is dropped                            L1 SCHEMA_MISSING
    nan_negative     a numeric field becomes NaN, infinite or negative              L1 SCHEMA_RANGE
    inflated_asn     a supplier's next ASN claims 10x the quantity                  L8 ASN_OUTLIER
    stale_timestamp  a source's next messages carry a timestamp 2-48 h old          L3 STALE_TS
    blackout         X % of sources go silent for Y minutes (messages dropped)      L9 SLA_SILENT
    cascade_failure  a node fails in the reality twin, then its downstream DCs     L7 TWIN_ENVELOPE
Spoofed / corrupted data comes from a compromised or buggy device, so it is re-signed with that device's
key (valid HMAC): the trust layers must catch it on content, not on the signature. Replays and duplicates
are byte-identical copies, so their labels name the occurrence (2 = the second time that msg_id appears).
"""
from __future__ import annotations

import copy
import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sim.reality.emulator import RealityEmulator

EXPECTED = {
    "gps_teleport": ("L4", "PHYSICS_TELEPORT"),
    "gps_drift": ("L6", "KALMAN_GATE"),
    "replay": ("L3", "REPLAY_NONCE"),
    "duplicate": ("L3", "DUPLICATE_ID"),
    "missing_fields": ("L1", "SCHEMA_MISSING"),
    "nan_negative": ("L1", "SCHEMA_RANGE"),
    "inflated_asn": ("L8", "ASN_OUTLIER"),
    "stale_timestamp": ("L3", "STALE_TS"),
    "blackout": ("L9", "SLA_SILENT"),
    "cascade_failure": ("L7", "TWIN_ENVELOPE"),
}
ATTACK_TYPES = tuple(EXPECTED)
# weights follow opportunity: ASNs and node failures are rare events in a day of telemetry
DEFAULT_MIX = {t: 1.0 for t in ATTACK_TYPES} | {"cascade_failure": 0.25, "blackout": 0.5, "inflated_asn": 0.4}
NUMERIC = {"gps": ("lat", "lon", "speed_kmh"), "stock": ("on_hand", "on_order", "backlog"), "asn": ("qty",),
           "port": ("berths_busy", "berth_queue")}
REQUIRED = {"gps": ("lat", "lon", "vehicle_id", "speed_kmh"), "stock": ("on_hand", "sku", "node"),
            "asn": ("qty", "dc", "eta_ts"), "port": ("status", "berths_busy")}
RETRY_H = 0.5  # give up on an attack whose target never shows up within this window


@dataclass
class Attack:
    type: str
    start_h: float
    duration_h: float = 0.0
    target: str | None = None
    params: dict = field(default_factory=dict)
    id: str = ""
    status: str = "scheduled"            # scheduled | active | done | skipped
    activated_h: float | None = None
    ended_h: float | None = None
    msgs: list[dict] = field(default_factory=list)   # [{"msg_id", "occurrence", "action"}]
    events: list[dict] = field(default_factory=list)  # physical sub-events (cascade)
    remaining: int = 0
    note: str = ""

    def label(self, start: datetime) -> dict:
        layer, reason = EXPECTED[self.type]
        ts = lambda h: None if h is None else (start + timedelta(hours=h)).isoformat(timespec="milliseconds")  # noqa: E731
        return {"attack_id": self.id, "type": self.type, "status": self.status, "target": self.target,
                "start_ts": ts(self.activated_h), "end_ts": ts(self.ended_h), "scheduled_ts": ts(self.start_h),
                "expected_layer": layer, "expected_reason": reason, "params": self.params,
                "n_msgs": len(self.msgs), "msgs": self.msgs, "events": self.events, "note": self.note}


def _shift(lat: float, lon: float, dist_m: float, bearing_deg: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    dlat = dist_m * math.cos(b) / 111_320.0
    dlon = dist_m * math.sin(b) / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return lat + dlat, lon + dlon


def default_params(t: str, rng: np.random.Generator) -> tuple[float, dict]:
    """(duration_h, params) for an attack of type t, drawn like the random campaign does."""
    prm: dict = {}
    dur = 0.0
    if t == "gps_teleport":
        prm = {"n_pings": int(rng.integers(1, 6)), "distance_km": float(rng.uniform(300, 800)),
               "bearing_deg": float(rng.uniform(0, 360))}
    elif t == "gps_drift":
        dur = float(rng.uniform(5, 20)) / 60
        prm = {"rate_mps": float(rng.uniform(0.5, 3.0)), "bearing_deg": float(rng.uniform(0, 360))}
    elif t == "replay":
        prm = {"k": int(rng.integers(5, 31)), "min_age_s": 60, "max_age_s": 1800}
    elif t in ("duplicate", "missing_fields", "nan_negative", "stale_timestamp"):
        prm = {"k": int(rng.integers(1, 11)), "kind": str(rng.choice(["gps", "stock", "port", "asn"], p=[0.5, 0.3, 0.1, 0.1]))}
        if t == "stale_timestamp":
            prm["age_h"] = float(rng.uniform(2, 48))
    elif t == "inflated_asn":
        prm = {"factor": 10.0}
    elif t == "blackout":
        dur = float(rng.uniform(5, 20)) / 60
        prm = {"fraction": float(rng.uniform(0.1, 0.4)), "kinds": ["gps"] if rng.random() < 0.6 else ["gps", "stock", "port"]}
    elif t == "cascade_failure":
        dur = float(rng.uniform(4, 24))
        prm = {"depth": int(rng.integers(1, 3)), "delay_h": [1.0, 6.0]}
    return dur, prm


class AttackInjector:
    def __init__(self, attacks: list[Attack] | None = None, seed: int = 7):
        self.attacks = sorted(attacks or [], key=lambda a: a.start_h)
        for i, a in enumerate(self.attacks):
            a.id = a.id or f"A{i + 1:04d}"
        self.rng = np.random.default_rng([seed, 0xA77AC4])
        self.history: deque[tuple[float, dict]] = deque(maxlen=40_000)  # (t_h, published clean msg) for replays
        self.seen_at: dict[str, float] = {}        # source_id -> last time seen
        self.occurrence: dict[str, int] = {}       # msg_id -> times published
        self.em: RealityEmulator | None = None

    # ------------------------------------------------------------------ campaign
    @classmethod
    def random_campaign(cls, n: int, hours: float, seed: int = 7, mix: dict | None = None, t0_h: float = 0.0) -> AttackInjector:
        rng = np.random.default_rng([seed, 0xC4A1])
        mix = mix or DEFAULT_MIX
        types = list(mix)
        p = np.array([mix[t] for t in types], dtype=float)
        p /= p.sum()
        attacks = []
        lo, hi = t0_h + 0.05 * hours, t0_h + 0.9 * hours
        for _ in range(n):
            t = str(rng.choice(types, p=p))
            start = float(rng.uniform(lo, hi))
            dur, prm = default_params(t, rng)
            attacks.append(Attack(t, start, dur, params=prm))
        return cls(attacks, seed=seed)

    @classmethod
    def from_blackout_scenario(cls, sc, start: datetime, seed: int = 7) -> Attack:
        """A data_blackout scenario (DSL) as a labelled blackout attack."""
        return Attack("blackout", sc.start_offset_h(start), sc.duration_h, target=",".join(sc.params["sources"]) or None,
                      params={"fraction": sc.params["fraction"] * sc.severity, "kinds": ["gps", "stock", "port", "asn"],
                              "sources": sc.params["sources"]})

    def inject_now(self, t: str, now_h: float, target: str | None = None, params: dict | None = None,
                   duration_h: float | None = None, id: str | None = None) -> Attack:
        """A live chaos command (Chaos Console / POST /chaos/inject): starts at the next tick."""
        if t not in EXPECTED:
            raise ValueError(f"unknown attack type {t!r}")
        dur, prm = default_params(t, self.rng)
        a = Attack(t, now_h, duration_h if duration_h is not None else dur, target=target, params={**prm, **(params or {})},
                   id=id or f"A{len(self.attacks) + 1:04d}")
        self.attacks.append(a)
        return a

    def bind(self, em: RealityEmulator) -> None:
        self.em = em

    # ------------------------------------------------------------------ time-driven
    def tick(self, now_h: float) -> None:
        for a in self.attacks:
            if a.status == "scheduled" and now_h >= a.start_h:
                if now_h - a.start_h > RETRY_H:
                    a.status, a.note = "skipped", "no eligible target appeared"
                    continue
                self._activate(a, now_h)
            elif a.status == "active":
                timed = a.type in ("gps_drift", "blackout", "cascade_failure")
                if (timed and now_h >= a.activated_h + a.duration_h) or (not timed and a.remaining <= 0):
                    a.status, a.ended_h = "done", now_h
                    if not a.msgs and not a.events and a.type != "cascade_failure":
                        a.status, a.note = "skipped", "target stopped reporting: no message affected"
                if a.type == "cascade_failure":
                    self._cascade_step(a, now_h)

    def _recent_sources(self, now_h: float, kinds: tuple[str, ...], window_h: float = 120 / 3600) -> list[str]:
        return sorted(s for s, t in self.seen_at.items() if now_h - t <= window_h and s.split(":", 1)[0] in
                      {{"gps": "gps", "stock": "wms", "asn": "asn", "port": "port"}[k] for k in kinds})

    def _busy_targets(self) -> set[str]:
        return {a.target for a in self.attacks if a.status == "active" and a.target}

    def _activate(self, a: Attack, now_h: float) -> None:
        em = self.em
        if a.type in ("gps_teleport", "gps_drift"):
            cands = [s for s in self._recent_sources(now_h, ("gps",)) if s not in self._busy_targets()]
            if not cands:
                return
            a.target = a.target or str(self.rng.choice(cands))
            a.remaining = a.params.get("n_pings", 0)
        elif a.type in ("duplicate", "missing_fields", "nan_negative", "stale_timestamp"):
            cands = []
            for kind in dict.fromkeys([a.params["kind"], "gps", "stock", "port", "asn"]):  # fall back to a live feed
                cands = [s for s in self._recent_sources(now_h, (kind,), window_h=2.0) if s not in self._busy_targets()]
                if cands:
                    a.params["kind"] = kind
                    break
            if not cands:
                return
            a.target = a.target or str(self.rng.choice(cands))
            a.remaining = a.params["k"]
        elif a.type == "inflated_asn":
            a.remaining = 1  # the next ASN, from whichever supplier ships next (target filled on claim)
        elif a.type == "replay":
            lo, hi = now_h - a.params["max_age_s"] / 3600, now_h - a.params["min_age_s"] / 3600
            pool = [m for t, m in self.history if lo <= t <= hi]
            if len(pool) < a.params["k"]:
                return
            idx = self.rng.choice(len(pool), size=a.params["k"], replace=False)
            a.target = "mixed"
            a.params["replayed"] = [pool[i] for i in sorted(idx)]
            a.remaining = 0
        elif a.type == "blackout":
            srcs = a.params.get("sources") or []
            if not srcs:
                cands = self._recent_sources(now_h, tuple(a.params["kinds"]), window_h=1.0)
                if not cands:
                    return
                n = max(1, int(round(a.params["fraction"] * len(cands))))
                srcs = sorted(str(x) for x in self.rng.choice(cands, size=n, replace=False))
            a.params["silenced"] = list(srcs)
            a.target = f"{len(srcs)} sources"
        elif a.type == "cascade_failure":
            twin = em.twin
            if not a.target:
                cands = [n.id for n in twin.net.nodes.values() if n.type in ("port", "dc", "plant", "supplier")
                         and twin.node_status(n.id) == "up"]
                if not cands:  # everything already down: try again later (or give up after RETRY_H)
                    return
                a.target = str(self.rng.choice(cands))
            a.params["chain"] = self._chain(a.target, a.params.get("depth", 1))
            a.params["next"] = 0
            a.params["t_next"] = now_h
        a.status, a.activated_h = "active", now_h
        if a.type == "replay":  # replays are injected immediately
            for m in a.params.pop("replayed"):
                em.publish_raw([m], attack=a, action="replay")
            a.status, a.ended_h = "done", now_h

    def _chain(self, node: str, depth: int) -> list[str]:
        net = self.em.twin.net
        chain, frontier = [node], [node]
        for _ in range(depth):
            nxt = sorted({l.to_id for l in net.lanes.values() if l.from_id in frontier and not l.transfer_only
                          and net.nodes[l.to_id].type == "dc"} - set(chain))
            if not nxt:
                break
            pick = str(self.rng.choice(nxt))
            chain.append(pick)
            frontier = [pick]
        return chain

    def _cascade_step(self, a: Attack, now_h: float) -> None:
        chain, i = a.params["chain"], a.params["next"]
        if i >= len(chain) or now_h < a.params["t_next"]:
            return
        node = chain[i]
        dur = max(0.5, a.activated_h + a.duration_h - now_h)
        ack = self.em.twin.apply({"type": "node_status", "node": node, "factor": 0.0, "duration_h": dur,
                                  "label": f"cascade {a.id} step {i + 1}"})
        a.events.append({"node": node, "t_h": round(now_h, 4), "ts": self.em.ts(now_h), "duration_h": round(dur, 3),
                         "effect": ack["id"]})
        lo, hi = a.params["delay_h"]
        a.params["next"] = i + 1
        a.params["t_next"] = now_h + float(self.rng.uniform(lo, hi))

    # ------------------------------------------------------------------ message path
    def observe(self, msgs: list[dict], now_h: float) -> None:
        for m in msgs:
            self.seen_at[m["source_id"]] = now_h

    def transform(self, msgs: list[dict], now_h: float) -> list[tuple[dict, Attack | None, str]]:
        """Clean messages in, (message, attack or None, action) out - possibly dropped, altered or doubled."""
        self.observe(msgs, now_h)
        out: list[tuple[dict, Attack | None, str]] = []
        active = [a for a in self.attacks if a.status == "active"]
        for m in msgs:
            src, kind, p = m["source_id"], m["kind"], m["payload"]
            handled = False
            for a in active:
                t = a.type
                if t == "blackout" and src in a.params["silenced"]:
                    out.append((m, a, "dropped"))
                    handled = True
                    break
                if a.target != src and t not in ("inflated_asn",):
                    continue
                if t == "gps_teleport" and a.remaining > 0:
                    mm = self._resign(m)
                    mm["payload"]["lat"], mm["payload"]["lon"] = _shift(p["lat"], p["lon"], a.params["distance_km"] * 1000, a.params["bearing_deg"])
                    a.remaining -= 1
                    out.append((self.em.resign(mm), a, "teleport"))
                    handled = True
                elif t == "gps_drift":
                    mm = self._resign(m)
                    d = a.params["rate_mps"] * (now_h - a.activated_h) * 3600
                    mm["payload"]["lat"], mm["payload"]["lon"] = _shift(p["lat"], p["lon"], d, a.params["bearing_deg"])
                    out.append((self.em.resign(mm), a, "drift"))
                    handled = True
                elif t == "duplicate" and a.remaining > 0:
                    out.append((m, None, "clean"))
                    out.append((m, a, "duplicate"))
                    a.remaining -= 1
                    handled = True
                elif t == "missing_fields" and a.remaining > 0:
                    mm = self._resign(m)
                    fields = [f for f in REQUIRED[kind] if f in mm["payload"]]
                    f = str(self.rng.choice(fields))
                    del mm["payload"][f]
                    a.remaining -= 1
                    out.append((self.em.resign(mm), a, f"missing:{f}"))
                    handled = True
                elif t == "nan_negative" and a.remaining > 0:
                    mm = self._resign(m)
                    f = str(self.rng.choice(NUMERIC[kind]))
                    bad = [float("nan"), float("inf"), -abs(float(mm["payload"].get(f, 1)) or 1.0) - 1][int(self.rng.integers(3))]
                    mm["payload"][f] = bad
                    a.remaining -= 1
                    out.append((self.em.resign(mm), a, f"bad:{f}"))
                    handled = True
                elif t == "stale_timestamp" and a.remaining > 0:
                    mm = self._resign(m)
                    mm["ts"] = (datetime.fromisoformat(m["ts"]) - timedelta(hours=a.params["age_h"])).isoformat(timespec="milliseconds")
                    a.remaining -= 1
                    out.append((self.em.resign(mm), a, "stale"))
                    handled = True
                elif t == "inflated_asn" and a.remaining > 0 and kind == "asn":
                    mm = self._resign(m)
                    mm["payload"]["qty"] = p["qty"] * a.params["factor"]
                    a.target = src
                    a.remaining -= 1
                    out.append((self.em.resign(mm), a, "inflated"))
                    handled = True
                if handled:
                    break
            if not handled:
                out.append((m, None, "clean"))
        return out

    @staticmethod
    def _resign(m: dict) -> dict:
        return copy.deepcopy(m)

    def record(self, m: dict, attack: Attack | None, action: str) -> None:
        n = self.occurrence.get(m["msg_id"], 0) + 1
        self.occurrence[m["msg_id"]] = n
        if attack is None and n == 1:  # replays re-send what honest devices really published
            self.history.append((self.em.env.now if self.em else 0.0, m))
        if attack is not None:
            attack.msgs.append({"msg_id": m["msg_id"], "occurrence": n, "action": action, "source_id": m["source_id"]})

    def finalize(self, now_h: float) -> None:
        """End of the run: close timed attacks, mark untriggered ones as skipped."""
        for a in self.attacks:
            if a.status == "scheduled":
                a.status, a.note = "skipped", "window ended before activation"
            elif a.status == "active":
                if a.msgs or a.events:
                    a.status, a.ended_h = "done", now_h
                    a.note = a.note or "window ended while active"
                else:
                    a.status, a.ended_h, a.note = "skipped", now_h, "no matching message before the window ended"

    def labels(self, start: datetime) -> list[dict]:
        return [a.label(start) for a in self.attacks]
